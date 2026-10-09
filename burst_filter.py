"""
filter.py — 主動過濾 + burst 偵測。

Flow：
1. 收到非 @mention 的文字訊息時，先 queue 到 _pending[group_id]
2. 啟動 / 重置一個 8 秒 debouncer（快速收集 burst）
3. 8 秒到期 → 把這段 burst 合併成一段大文字 → 分類：
   (a) 先試 filter_rules (Layer 1/2 學到的規則)
   (b) 啟發式：明確閒聊短語 skip；其餘非空文字直接 respond
   (c) classifier 保留為 fallback，但目前「有人留言就回」政策下通常不會走到：
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
import sqlite3
import threading
import time
from typing import Callable

import gemini_client
import memory
from quote_context import QUOTE_CONTEXT_RULE, has_quote_context

logger = logging.getLogger(__name__)

# ── 可調參數 ──────────────────────────────────────────────────────────────────
SHORT_WINDOW_SECONDS = 8.0  # 初始等待窗口：快速收集 burst，然後問 Gemini
BURST_WINDOW_SECONDS = 60.0  # Gemini 判斷對方還沒說完時，等 1 分鐘再強制回
# ── 共享狀態（全程由 _lock 保護）──────────────────────────────────────────────
_lock = threading.Lock()
_pending: dict[str, list[tuple[str, str, str | None, float]]] = {}
_timers: dict[str, threading.Timer] = {}
_last_reply_tokens: dict[str, str] = {}
_waiting_groups: set[str] = set()  # Gemini 說「還沒說完」的 group，等 1 分鐘
_generations: dict[str, int] = {}
_cancelled_generations: dict[str, int] = {}
_claimed_generations: set[tuple[str, int]] = set()
_retry_attempts: dict[str, tuple[int, int]] = {}
_MAX_RETRY_SAFE_ATTEMPTS = 1

# main.py 在 import 後注入的 callback；簽名包含整個 burst 的 message_ids。
_on_flush: Callable[[str, str, str, list[str]], None] | None = None
# main.py 注入：(group_id, user_id) → 家人稱呼（2026-10-09 記憶要分得出是誰說的）。
_speaker_label: Callable[[str, str | None], str] | None = None


class RetryableBurstError(RuntimeError):
    """A burst failed before any outbound delivery could have started."""


def _merge_pending(
    older: list[tuple[str, str, str | None, float]],
    newer: list[tuple[str, str, str | None, float]],
) -> list[tuple[str, str, str | None, float]]:
    merged: list[tuple[str, str, str | None, float]] = []
    seen_message_ids: set[str] = set()
    for item in [*older, *newer]:
        message_id = item[0]
        if message_id and message_id in seen_message_ids:
            continue
        if message_id:
            seen_message_ids.add(message_id)
        merged.append(item)
    return merged


def _restore_retryable_burst(
    group_id: str,
    pending: list[tuple[str, str, str | None, float]],
    reply_token: str,
    force_respond: bool,
    generation: int,
) -> bool:
    """Restore a provably pre-delivery failure without clobbering newer work."""
    with _lock:
        current_generation = _generations.get(group_id, 0)
        claimed = (group_id, generation) in _claimed_generations
        if (
            _cancelled_generations.get(group_id, -1) >= generation
            and not claimed
        ):
            if _retry_attempts.get(group_id, (None, 0))[0] == generation:
                _retry_attempts.pop(group_id, None)
            return False

        newer = _pending.get(group_id, [])
        if current_generation != generation:
            if not newer or group_id not in _timers:
                if not claimed:
                    return False
            else:
                _pending[group_id] = _merge_pending(pending, newer)
                if force_respond:
                    _waiting_groups.add(group_id)
                return True

        retry_generation, attempts = _retry_attempts.get(
            group_id, (generation, 0)
        )
        if retry_generation != generation:
            attempts = 0
        if attempts >= _MAX_RETRY_SAFE_ATTEMPTS:
            _pending[group_id] = list(pending)
            _last_reply_tokens[group_id] = reply_token
            _retry_attempts.pop(group_id, None)
            if force_respond:
                _waiting_groups.add(group_id)
            return False

        _pending[group_id] = list(pending)
        _last_reply_tokens[group_id] = reply_token
        if force_respond:
            _waiting_groups.add(group_id)
        retry_generation = _schedule_locked(
            group_id, SHORT_WINDOW_SECONDS, force_respond
        )
        _retry_attempts[group_id] = (retry_generation, attempts + 1)
        return True


def _schedule_locked(group_id: str, delay: float, force_respond: bool) -> int:
    generation = _generations.get(group_id, 0) + 1
    _generations[group_id] = generation
    timer = threading.Timer(
        delay,
        _flush_burst,
        args=[group_id, force_respond, generation],
    )
    timer.daemon = True
    _timers[group_id] = timer
    timer.start()
    return generation


def register_speaker_label(callback: Callable[[str, str | None], str] | None) -> None:
    """main.py 注入家人稱呼查詢（2026-10-09）；None 代表不加稱呼。"""
    global _speaker_label
    _speaker_label = callback


def register_on_flush(fn: Callable[[str, str, str, list[str]], None]) -> None:
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
        _retry_attempts.pop(group_id, None)
        if group_id in _waiting_groups:
            # Gemini 已說還沒說完 — 新訊息重置 1 分鐘等待
            _schedule_locked(group_id, BURST_WINDOW_SECONDS, True)
        else:
            # 初始 8 秒快速收集
            _schedule_locked(group_id, SHORT_WINDOW_SECONDS, False)


def cancel_burst(group_id: str) -> list[tuple[str, str, str | None, float]]:
    """取消待處理的 burst（使用者後來直接 @mention，explicit 會接手）。

    2026-09-27: the cancelled messages stay in the conversation and are
    returned, so an @mention right after a shared link can still see it.
    """
    with _lock:
        generation = _generations.get(group_id, 0) + 1
        _generations[group_id] = generation
        _cancelled_generations[group_id] = generation
        t = _timers.pop(group_id, None)
        if t is not None:
            t.cancel()
        pending = _pending.pop(group_id, None)
        _last_reply_tokens.pop(group_id, None)
        _waiting_groups.discard(group_id)
        _retry_attempts.pop(group_id, None)
    if pending:
        _complete_without_reply(group_id, pending)
        _remember_cancelled(group_id, pending)
    return list(pending or [])


def _remember_cancelled(
    group_id: str,
    pending: list[tuple[str, str, str | None, float]],
) -> None:
    """Plain append only: no fact or calendar extraction before the reply.

    Same text a flush would store; chit-chat a flush would skip is not kept.
    """
    text = _combine(pending)
    if not text or _heuristic_decision(text) == "skip":
        return
    try:
        labelled = _combine(pending, group_id=group_id, label_of=_speaker_label)
        memory.append_turn(group_id, "user", f"[burst]\n{labelled or text}")
    except Exception as exc:
        logger.warning(
            "cancelled burst not remembered group=%s error_type=%s",
            group_id, type(exc).__name__,
        )


def _flush_burst(
    group_id: str,
    force_respond: bool = False,
    generation: int | None = None,
) -> None:
    """Timer callback — 跑在自己的 thread。"""
    with _lock:
        current_generation = _generations.get(group_id, 0)
        if generation is not None and generation != current_generation:
            return
        processing_generation = current_generation
        pending = _pending.pop(group_id, None)
        _timers.pop(group_id, None)
        reply_token = _last_reply_tokens.pop(group_id, None)

    if not pending or reply_token is None:
        with _lock:
            if _retry_attempts.get(group_id, (None, 0))[0] == processing_generation:
                _retry_attempts.pop(group_id, None)
        return

    try:
        _classify_and_maybe_respond(
            group_id,
            pending,
            reply_token,
            force_respond,
            processing_generation,
        )
    except RetryableBurstError as e:
        _restore_retryable_burst(
            group_id,
            pending,
            reply_token,
            force_respond,
            processing_generation,
        )
        logger.exception("burst flush failed before delivery: %s", e)
    except Exception as e:
        with _lock:
            if _retry_attempts.get(group_id, (None, 0))[0] == processing_generation:
                _retry_attempts.pop(group_id, None)
            if _generations.get(group_id, 0) == processing_generation:
                _waiting_groups.discard(group_id)
        logger.exception("burst flush failed: %s", e)
    else:
        with _lock:
            if _retry_attempts.get(group_id, (None, 0))[0] == processing_generation:
                _retry_attempts.pop(group_id, None)
            if (
                _generations.get(group_id, 0) == processing_generation
                and group_id not in _pending
                and group_id not in _timers
            ):
                _waiting_groups.discard(group_id)
    finally:
        with _lock:
            _claimed_generations.discard((group_id, processing_generation))


def _classify_and_maybe_respond(
    group_id: str,
    pending: list[tuple[str, str, str | None, float]],
    reply_token: str,
    force_respond: bool = False,
    generation: int | None = None,
) -> None:
    # 把 pending 合成一段連續的對話文字
    combined_text = _combine(pending)
    if not combined_text:
        _complete_without_reply(group_id, pending)
        return

    # 等了 1 分鐘 → 直接回，不再問 Gemini
    if force_respond:
        logger.info(
            "burst force respond after 1-min wait (group=%s, text=%s)",
            group_id,
            _truncate(combined_text, 80),
        )
        _invoke_flush(
            group_id, combined_text, reply_token, pending, generation=generation
        )
        return

    try:
        rules = memory.list_filter_rules(group_id)
    except sqlite3.OperationalError as exc:
        if "unable to open database file" in str(exc).lower():
            raise RetryableBurstError(
                "filter-rule store unavailable before delivery"
            ) from exc
        raise

    # Step 1: Layer 1/2 學到的規則優先
    rule_decision = _match_rules(combined_text, rules)
    if rule_decision == "skip":
        logger.info(
            "burst skipped by rule (group=%s, text=%s)",
            group_id,
            _truncate(combined_text, 80),
        )
        _complete_without_reply(group_id, pending)
        return
    if rule_decision == "must_answer":
        logger.info(
            "burst must_answer by rule (group=%s, text=%s)",
            group_id,
            _truncate(combined_text, 80),
        )
        _invoke_flush(
            group_id, combined_text, reply_token, pending, generation=generation
        )
        return

    # Step 2: 啟發式捷徑（越快回越好，不耗 Gemini quota）
    heur = _heuristic_decision(combined_text)
    if heur == "skip":
        logger.info(
            "burst skipped by heuristic (group=%s, text=%s)",
            group_id,
            _truncate(combined_text, 80),
        )
        _complete_without_reply(group_id, pending)
        return
    if heur == "respond":
        logger.info(
            "burst respond by heuristic (group=%s, text=%s)",
            group_id,
            _truncate(combined_text, 80),
        )
        _invoke_flush(
            group_id, combined_text, reply_token, pending, generation=generation
        )
        return

    # Step 3: 交給 Gemini 分類器
    decision, reason = gemini_client.classify_burst(combined_text, rules)
    logger.info(
        "burst classifier decision=%s reason=%s text=%s",
        decision,
        reason,
        _truncate(combined_text, 80),
    )

    if decision == "respond":
        _invoke_flush(
            group_id, combined_text, reply_token, pending, generation=generation
        )
    elif decision == "wait":
        # Gemini 說對方還沒說完 → 把訊息放回，等 1 分鐘後強制回
        stale = False
        superseded = False
        with _lock:
            if generation is not None and _cancelled_generations.get(
                group_id, -1
            ) >= generation:
                stale = True
            elif (
                generation is not None
                and _generations.get(group_id, 0) != generation
            ):
                newer = _pending.get(group_id, [])
                if newer and group_id in _timers:
                    _pending[group_id] = _merge_pending(pending, newer)
                    _waiting_groups.add(group_id)
                    old = _timers.pop(group_id)
                    old.cancel()
                    _schedule_locked(group_id, BURST_WINDOW_SECONDS, True)
                    superseded = True
                else:
                    stale = True
            else:
                _waiting_groups.add(group_id)
                existing = _pending.get(group_id, [])
                _pending[group_id] = pending + existing  # 舊訊息在前，保留順序
                if group_id not in _last_reply_tokens:
                    _last_reply_tokens[group_id] = reply_token
                old = _timers.pop(group_id, None)
                if old is not None:
                    old.cancel()
                _schedule_locked(group_id, BURST_WINDOW_SECONDS, True)
        if stale:
            _complete_without_reply(group_id, pending)
            _remember_cancelled(group_id, pending)  # cancel_burst found it already taken
            return
        if superseded:
            return
        logger.info(
            "burst waiting 1 min (group=%s, reason=%s, text=%s)",
            group_id,
            reason,
            _truncate(combined_text, 80),
        )
    else:
        _complete_without_reply(group_id, pending)


def _combine(
    pending: list[tuple[str, str, str | None, float]],
    *,
    group_id: str = "",
    label_of: Callable[[str, str | None], str] | None = None,
) -> str:
    """The burst as one text; quoted messages keep their own boundaries.

    ``label_of`` (group_id, user_id) → who said it: each message then starts
    with 「稱呼：」 (2026-10-09, for the conversation memory only).  When some
    writers have a name and another has none, that one's message says
    「（不確定是誰）：」 so it is never read as the previous speaker's.
    """
    names: dict[str | None, str] = {}
    if label_of is not None:
        for _, text, user_id, _ in pending:
            if text and user_id not in names:
                names[user_id] = _label(group_id, user_id, label_of)
    unknown = UNKNOWN_SPEAKER if any(names.values()) and not all(names.values()) else ""
    texts = [
        _with_name(text, names.get(user_id, "") or unknown) if label_of is not None else text
        for _, text, user_id, _ in pending
        if text
    ]
    if any(has_quote_context(text) for text in texts):
        combined_text = QUOTE_CONTEXT_RULE + "\n" + "\n".join(
            f"--- 群組訊息 {index} 開始 ---\n{text}\n--- 群組訊息 {index} 結束 ---"
            for index, text in enumerate(texts, 1)
        )
    else:
        combined_text = "\n".join(texts)
    return combined_text.strip()


UNKNOWN_SPEAKER = "（不確定是誰）"


def _label(group_id: str, user_id: str | None, label_of: Callable[[str, str | None], str]) -> str:
    try:
        return (label_of(group_id, user_id) or "").strip()
    except Exception:
        return ""


def _with_name(text: str, name: str) -> str:
    return f"{name}：{text}" if name else text


# 2026-10-09：main 把這批存進對話紀錄時要逐則寫是誰說的。交給 main 前先算好加了
# 稱呼的合併文字，main 用同一批 message_ids 取回；最多留 32 批。
_labelled_lock = threading.Lock()
_labelled_texts: dict[tuple[str, tuple[str, ...]], str] = {}
_LABELLED_KEEP = 32


def _stash_labelled(
    group_id: str, message_ids: list[str], pending: list[tuple[str, str, str | None, float]]
) -> None:
    if _speaker_label is None or not message_ids:
        return
    text = _combine(pending, group_id=group_id, label_of=_speaker_label)
    key = (group_id, tuple(str(m) for m in message_ids))
    with _labelled_lock:
        _labelled_texts.pop(key, None)
        _labelled_texts[key] = text
        while len(_labelled_texts) > _LABELLED_KEEP:
            _labelled_texts.pop(next(iter(_labelled_texts)))


def labelled_text(group_id: str, message_ids: list[str] | None) -> str | None:
    """這批訊息加了稱呼的合併文字；沒有（未注入稱呼、太舊）回 None。"""
    key = (group_id, tuple(str(m) for m in message_ids or ()))
    with _labelled_lock:
        return _labelled_texts.get(key)


def _complete_without_reply(
    group_id: str,
    pending: list[tuple[str, str, str | None, float]],
) -> None:
    """Close a burst that policy deliberately chose not to answer."""
    message_ids = [message_id for message_id, *_rest in pending if message_id]
    try:
        memory.mark_inbound_events_completed_no_reply(group_id, message_ids)
    except Exception as exc:
        # Leave the events open on bookkeeping failure so monitoring fails safe.
        logger.warning("burst no-reply completion failed group=%s: %s", group_id, exc)


def _invoke_flush(
    group_id: str,
    combined_text: str,
    reply_token: str,
    pending: list[tuple[str, str, str | None, float]],
    generation: int | None = None,
) -> None:
    if _on_flush is None:
        logger.warning("filter._on_flush not registered; dropping burst")
        return
    message_ids = list(
        dict.fromkeys(message_id for message_id, *_rest in pending if message_id)
    )
    if generation is not None:
        stale = False
        superseded = False
        with _lock:
            if _cancelled_generations.get(group_id, -1) >= generation:
                stale = True
            elif _generations.get(group_id, 0) != generation:
                newer = _pending.get(group_id, [])
                if newer and group_id in _timers:
                    _pending[group_id] = _merge_pending(pending, newer)
                    superseded = True
                else:
                    stale = True
            else:
                # This lock-protected claim is the handoff linearization point:
                # cancellation before it wins; after it, the callback owns delivery.
                _claimed_generations.add((group_id, generation))
        if stale:
            _complete_without_reply(group_id, pending)
            _remember_cancelled(group_id, pending)  # cancel_burst found it already taken
            return
        if superseded:
            return
    # 延遲觀測：這批最早／最晚一則進 burst 到交給 main 等了幾秒（不記內容）。
    # main 的 "burst flush triggered" 只拿得到 message_ids，所以記在這裡。
    first_age_s, last_age_s = _pending_ages(pending)
    logger.info(
        "burst flush handoff group=%s n_msgs=%d first_age_s=%.2f last_age_s=%.2f",
        group_id,
        len(pending),
        first_age_s,
        last_age_s,
    )
    try:
        _stash_labelled(group_id, message_ids, pending)
    except Exception as exc:
        logger.debug("burst labels skipped: %s", exc)
    _on_flush(group_id, combined_text, reply_token, message_ids)


def _pending_ages(
    pending: list[tuple[str, str, str | None, float]],
) -> tuple[float, float]:
    """(oldest, newest) seconds since add_to_burst queued them; logs only."""
    stamps = [item[3] for item in pending]
    if not stamps:
        return 0.0, 0.0
    now = time.time()
    return now - min(stamps), now - max(stamps)


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

# 常見純閒聊語助詞，整句等於這些就直接 skip
_CHITCHAT_EXACT = {
    "哈哈",
    "哈哈哈",
    "XD",
    "LOL",
    "好",
    "好喔",
    "好的",
    "ok",
    "OK",
    "Ok",
    "讚",
    "嗯",
    "嗯嗯",
    "晚安",
    "早安",
    "午安",
    "謝謝",
    "感謝",
    "Thanks",
    "收到",
    "了解",
    "知道了",
    "辛苦了",
}


def _heuristic_decision(text: str) -> str | None:
    """回傳 'skip' / 'respond' / None（交給 classifier 決定）。

    Andrew 的 current rule: 家人/群組裡有人留言就要回。唯一 cheap skip 是
    明確閒聊短語，避免「好 / 哈哈」這類 reaction 造成噪音。
    """
    stripped = text.strip()

    # 整則 = 固定閒聊短語 → skip
    if stripped in _CHITCHAT_EXACT:
        return "skip"

    # 其餘非空文字都回。8 秒 debounce 已經會把連續訊息合併成一次回覆。
    if stripped:
        return "respond"

    # 其餘情境交給 classifier
    return None


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
