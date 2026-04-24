"""
filter.py — 主動過濾 + burst 偵測。

Flow：
1. 收到非 @mention 的文字訊息時，先 queue 到 _pending[group_id]
2. 啟動 / 重置一個 8 秒 debouncer（快速收集 burst）
3. 8 秒到期 → 把這段 burst 合併成一段大文字 → 分類：
   (a) 先試 filter_rules (Layer 1/2 學到的規則)
   (b) 啟發式：太短直接 skip；含 URL / 末句有結束標點 → 直接 respond
   (c) 呼叫 gemini_client.classify_burst() 做最終判斷：
       - "respond" → 立刻回
       - "skip"    → 略過
       - "wait"    → Gemini 判斷對方還沒說完，設 1 分鐘 timer，到期強制回
4. 若在「等待」期間有新訊息 → 重置 1 分鐘 timer（對方繼續打字）
5. 若決定 respond → 呼叫 main.py 注冊進來的 on_flush(group_id, combined, reply_token)

設計注記：
- 用 threading.Timer 而不是 asyncio，因為 webhook handler 是 sync；
  Timer callback 會在自己的 thread 跑，memory / gemini / LINE API 都是 thread-safe。
- reply_token 有效期 ~60 秒。初始 8 秒 + 分類 ~5 秒 << 60 秒；
  wait 模式下到期後用最後一則的 token 回，若失效 _reply() 會 log warning 不會炸。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

import gemini_client
import memory

logger = logging.getLogger(__name__)

# ── 可調參數 ──────────────────────────────────────────────────────────────────
SHORT_WINDOW_SECONDS = 8.0     # 初始等待窗口：快速收集 burst，然後問 Gemini
BURST_WINDOW_SECONDS = 60.0    # Gemini 判斷對方還沒說完時，等 1 分鐘再強制回
HEURISTIC_SHORT_LEN = 10       # 低於這個長度直接 skip（除非含 URL）
HEURISTIC_LONG_LEN = 80        # 高於這個長度觸發「可能是轉貼長文」

# ── 共享狀態（全程由 _lock 保護）──────────────────────────────────────────────
_lock = threading.Lock()
_pending: dict[str, list[tuple[str, str, str | None, float]]] = {}
_timers: dict[str, threading.Timer] = {}
_last_reply_tokens: dict[str, str] = {}
_waiting_groups: set[str] = set()   # Gemini 說「還沒說完」的 group，等 1 分鐘

# main.py 在 import 後注入的 callback；簽名 = (group_id, combined_text, reply_token)
_on_flush: Callable[[str, str, str], None] | None = None


def register_on_flush(fn: Callable[[str, str, str], None]) -> None:
    """讓 main.py 在 import filter 時把 flush callback 注入進來。"""
    global _on_flush
    _on_flush = fn


def add_to_burst(
    group_id: str,
    message_id: str,
    text: str,
    user_id: str | None,
    reply_token: str,
) -> None:
    """把一則訊息加入待處理 burst，順便重置 debouncer。"""
    if not text:
        return
    now = time.time()
    with _lock:
        _pending.setdefault(group_id, []).append((message_id, text, user_id, now))
        _last_reply_tokens[group_id] = reply_token
        old = _timers.pop(group_id, None)
        if old is not None:
            old.cancel()
        if group_id in _waiting_groups:
            # Gemini 已說還沒說完 — 新訊息重置 1 分鐘等待
            t = threading.Timer(BURST_WINDOW_SECONDS, _flush_burst, args=[group_id, True])
        else:
            # 初始 8 秒快速收集
            t = threading.Timer(SHORT_WINDOW_SECONDS, _flush_burst, args=[group_id, False])
        t.daemon = True
        _timers[group_id] = t
        t.start()


def cancel_burst(group_id: str) -> None:
    """取消待處理的 burst（使用者後來直接 @mention，explicit 會接手）。"""
    with _lock:
        t = _timers.pop(group_id, None)
        if t is not None:
            t.cancel()
        _pending.pop(group_id, None)
        _last_reply_tokens.pop(group_id, None)
        _waiting_groups.discard(group_id)


def _flush_burst(group_id: str, force_respond: bool = False) -> None:
    """Timer callback — 跑在自己的 thread。"""
    with _lock:
        pending = _pending.pop(group_id, None)
        _timers.pop(group_id, None)
        reply_token = _last_reply_tokens.pop(group_id, None)
        _waiting_groups.discard(group_id)

    if not pending or reply_token is None:
        return

    try:
        _classify_and_maybe_respond(group_id, pending, reply_token, force_respond)
    except Exception as e:
        logger.exception("burst flush failed: %s", e)


def _classify_and_maybe_respond(
    group_id: str,
    pending: list[tuple[str, str, str | None, float]],
    reply_token: str,
    force_respond: bool = False,
) -> None:
    # 把 pending 合成一段連續的對話文字
    combined_text = "\n".join(text for _, text, _, _ in pending if text)
    combined_text = combined_text.strip()
    if not combined_text:
        return

    # 等了 1 分鐘 → 直接回，不再問 Gemini
    if force_respond:
        logger.info(
            "burst force respond after 1-min wait (group=%s, text=%s)",
            group_id, _truncate(combined_text, 80),
        )
        _invoke_flush(group_id, combined_text, reply_token)
        return

    rules = memory.list_filter_rules(group_id)

    # Step 1: Layer 1/2 學到的規則優先
    rule_decision = _match_rules(combined_text, rules)
    if rule_decision == "skip":
        logger.info(
            "burst skipped by rule (group=%s, text=%s)",
            group_id, _truncate(combined_text, 80),
        )
        return
    if rule_decision == "must_answer":
        logger.info(
            "burst must_answer by rule (group=%s, text=%s)",
            group_id, _truncate(combined_text, 80),
        )
        _invoke_flush(group_id, combined_text, reply_token)
        return

    # Step 2: 啟發式捷徑（越快回越好，不耗 Gemini quota）
    heur = _heuristic_decision(combined_text)
    if heur == "skip":
        logger.info(
            "burst skipped by heuristic (group=%s, text=%s)",
            group_id, _truncate(combined_text, 80),
        )
        return
    if heur == "respond":
        logger.info(
            "burst respond by heuristic (group=%s, text=%s)",
            group_id, _truncate(combined_text, 80),
        )
        _invoke_flush(group_id, combined_text, reply_token)
        return

    # Step 3: 交給 Gemini 分類器
    decision, reason = gemini_client.classify_burst(combined_text, rules)
    logger.info(
        "burst classifier decision=%s reason=%s text=%s",
        decision, reason, _truncate(combined_text, 80),
    )

    if decision == "respond":
        _invoke_flush(group_id, combined_text, reply_token)
    elif decision == "wait":
        # Gemini 說對方還沒說完 → 把訊息放回，等 1 分鐘後強制回
        with _lock:
            _waiting_groups.add(group_id)
            existing = _pending.get(group_id, [])
            _pending[group_id] = pending + existing   # 舊訊息在前，保留順序
            if group_id not in _last_reply_tokens:
                _last_reply_tokens[group_id] = reply_token
            old = _timers.pop(group_id, None)
            if old is not None:
                old.cancel()
            t = threading.Timer(BURST_WINDOW_SECONDS, _flush_burst, args=[group_id, True])
            t.daemon = True
            _timers[group_id] = t
            t.start()
        logger.info(
            "burst waiting 1 min (group=%s, reason=%s, text=%s)",
            group_id, reason, _truncate(combined_text, 80),
        )
    # else "skip": 不回


def _invoke_flush(group_id: str, combined_text: str, reply_token: str) -> None:
    if _on_flush is None:
        logger.warning("filter._on_flush not registered; dropping burst")
        return
    _on_flush(group_id, combined_text, reply_token)


# ── 規則匹配 ──────────────────────────────────────────────────────────────────

def _match_rules(text: str, rules: list[dict]) -> str | None:
    """回傳 'skip' / 'must_answer' / None。must_answer 優先。"""
    must_hit = False
    skip_hit = False
    for r in rules:
        pattern = r.get("pattern", "")
        if not pattern:
            continue
        if pattern in text:
            if r["kind"] == "must_answer":
                must_hit = True
            elif r["kind"] == "skip":
                skip_hit = True
    if must_hit:
        return "must_answer"
    if skip_hit:
        return "skip"
    return None


# ── 啟發式 ────────────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://\S+")
_TOPIC_END_RE = re.compile(r"[？?！!。～~…]+\s*$")  # 末句結束信號

# 常見純閒聊語助詞，整句等於這些就直接 skip
_CHITCHAT_EXACT = {
    "哈哈", "哈哈哈", "XD", "LOL", "好", "好喔", "好的", "ok", "OK", "Ok",
    "讚", "嗯", "嗯嗯", "晚安", "早安", "午安", "謝謝", "感謝", "Thanks",
    "收到", "了解", "知道了", "辛苦了",
}


def _heuristic_decision(text: str) -> str | None:
    """回傳 'skip' / 'respond' / None（交給 classifier 決定）。"""
    stripped = text.strip()

    # 整則 = 固定閒聊短語 → skip
    if stripped in _CHITCHAT_EXACT:
        return "skip"

    has_url = bool(_URL_RE.search(stripped))

    # 太短 + 無連結 → skip
    if len(stripped) < HEURISTIC_SHORT_LEN and not has_url:
        return "skip"

    # 含連結 → respond
    if has_url:
        return "respond"

    # 單一則就超過 HEURISTIC_LONG_LEN → 很可能是轉貼文章 → respond
    if len(stripped) >= HEURISTIC_LONG_LEN:
        return "respond"

    # 末句有明確結束信號 → 話說完了，直接回不等 Gemini
    if _TOPIC_END_RE.search(stripped):
        return "respond"

    # 其餘情境交給 classifier
    return None


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
