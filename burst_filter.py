"""
filter.py — 主動過濾 + burst 偵測。

Flow：
1. 收到非 @mention 的文字訊息時，先 queue 到 _pending[group_id]
2. 啟動 / 重置一個 30 秒 debouncer
3. debouncer 到期 → 把這段 burst 合併成一段大文字 → 分類：
   (a) 先試 filter_rules (Layer 1/2 學到的規則)
   (b) 啟發式：太短直接 skip；含 URL 直接 respond
   (c) 呼叫 gemini_client.classify_burst() 做最終判斷
4. 若決定 respond → 呼叫 main.py 注冊進來的 on_flush(group_id, combined, reply_token)

設計注記：
- 用 threading.Timer 而不是 asyncio，因為 webhook handler 是 sync；
  Timer callback 會在自己的 thread 跑，memory / gemini / LINE API 都是 thread-safe。
- reply_token 有效期 ~60 秒。burst 窗 30 秒 + 分類 ~5 秒 + chat ~10 秒 << 60 秒，
  直接用最後一則訊息的 reply_token 回就好；失敗 _reply() 會 log warning 不會炸。
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
BURST_WINDOW_SECONDS = 30.0
HEURISTIC_SHORT_LEN = 10       # 低於這個長度直接 skip（除非含 URL）
HEURISTIC_LONG_LEN = 80        # 高於這個長度觸發「可能是轉貼長文」

# ── 共享狀態（全程由 _lock 保護）──────────────────────────────────────────────
_lock = threading.Lock()
_pending: dict[str, list[tuple[str, str, str | None, float]]] = {}
_timers: dict[str, threading.Timer] = {}
_last_reply_tokens: dict[str, str] = {}

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
        # 重置 debouncer
        old = _timers.pop(group_id, None)
        if old is not None:
            old.cancel()
        t = threading.Timer(BURST_WINDOW_SECONDS, _flush_burst, args=[group_id])
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


def _flush_burst(group_id: str) -> None:
    """Timer callback — 跑在自己的 thread。"""
    with _lock:
        pending = _pending.pop(group_id, None)
        _timers.pop(group_id, None)
        reply_token = _last_reply_tokens.pop(group_id, None)

    if not pending or reply_token is None:
        return

    try:
        _classify_and_maybe_respond(group_id, pending, reply_token)
    except Exception as e:
        logger.exception("burst flush failed: %s", e)


def _classify_and_maybe_respond(
    group_id: str,
    pending: list[tuple[str, str, str | None, float]],
    reply_token: str,
) -> None:
    # 把 pending 合成一段連續的對話文字
    combined_text = "\n".join(text for _, text, _, _ in pending if text)
    combined_text = combined_text.strip()
    if not combined_text:
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

    # Step 2: 啟發式捷徑
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
    should_respond, reason = gemini_client.classify_burst(combined_text, rules)
    logger.info(
        "burst classifier decision=%s reason=%s text=%s",
        should_respond, reason, _truncate(combined_text, 80),
    )
    if should_respond:
        _invoke_flush(group_id, combined_text, reply_token)


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

    # 含連結 → respond（包含新聞、TikTok、YouTube 等所有連結都交給主模型處理）
    if has_url:
        return "respond"

    # 單一則就超過 HEURISTIC_LONG_LEN → 很可能是轉貼文章 → respond
    if len(stripped) >= HEURISTIC_LONG_LEN:
        return "respond"

    # 其餘情境交給 classifier
    return None


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
