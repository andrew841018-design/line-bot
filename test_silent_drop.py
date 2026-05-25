"""
Silent drop regression tests — verify bot 一定回覆 (Andrew 2026-05-25 rule).

Per memory `feedback_bot_reply_always`: webhook handler 兩條出口必擇一
(直接 reply OR enqueue pending)，絕不 silent drop。

3 silent drop suspects from main.py audit (2026-05-25):
- S4: unknown message type (Sticker / Location / Template) fall through _handle_event
- S5a: burst flush + Gemini primary + retry 都 quota 爆 → silent return
- S5b: burst flush + Gemini 回 empty reply → skip LINE send (silent)
"""

import os
import time
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
from linebot.v3.webhooks import (  # noqa: E402
    GroupSource,
    MessageEvent,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_group_source(group_id="GRP001", user_id="USR001"):
    src = MagicMock(spec=GroupSource)
    src.group_id = group_id
    src.user_id = user_id
    src.type = "group"
    return src


def _make_message_event(msg, source=None, redelivery=False, reply_token="TOKEN001"):
    evt = MagicMock(spec=MessageEvent)
    evt.message = msg
    evt.source = source or _make_group_source()
    evt.reply_token = reply_token
    evt.timestamp = int(time.time() * 1000)
    dctx = MagicMock()
    dctx.is_redelivery = redelivery
    evt.delivery_context = dctx
    return evt


def _make_sticker_like_msg():
    """合成一個 'unknown' message type (e.g. StickerMessage / LocationMessage)。

    不是 TextMessageContent / ImageMessageContent / VideoMessageContent /
    AudioMessageContent / FileMessageContent — 純 MagicMock 不帶任何 known spec，
    以模擬 _handle_event 全部 isinstance check 都不 match 的情境。
    """
    msg = MagicMock()
    msg.id = "MSG_STICKER_001"
    msg.type = "sticker"
    return msg


# ═══════════════════════════════════════════════════════════════════════════════
# S4: unknown message type
# ═══════════════════════════════════════════════════════════════════════════════


def test_s4_unknown_message_type_must_not_silent_drop():
    """Sticker / Location / Template 訊息必須得到 reply 不能 silent drop。"""
    msg = _make_sticker_like_msg()
    evt = _make_message_event(msg)

    with patch("main._reply") as mock_reply, \
         patch("main._save_pending_any") as mock_save_pending, \
         patch("main.memory.log_raw_message"), \
         patch("main._quota_exhausted", return_value=False), \
         patch("main._spawn_piggyback_drain"), \
         patch("main.settings.allowed_group_id", "GRP001"):
        main._handle_event(evt)

    called_count = mock_reply.call_count + mock_save_pending.call_count
    assert called_count > 0, (
        f"Silent drop: unknown message type 未呼叫 _reply 也沒 _save_pending_any "
        f"(_reply={mock_reply.call_count}, _save_pending_any={mock_save_pending.call_count})"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# S5a: burst flush + Gemini quota retry miss
# ═══════════════════════════════════════════════════════════════════════════════


def test_s5a_burst_flush_quota_retry_miss_must_not_silent():
    """burst flush + Gemini primary + retry 都 quota 爆 → 必須 reply 不能 silent。

    違反 _handle_burst_flush docstring 自己寫的「會回應的情境不能靜默」。
    """
    with patch(
        "main._llm_chat",
        side_effect=Exception("quota exceeded for quota metric 'gemini'"),
    ), \
         patch("main._is_quota_error", return_value=True), \
         patch("main._mark_quota_exhausted"), \
         patch("main.memory.check_fact_cache", return_value=None), \
         patch("main.memory.get_context", return_value=[]), \
         patch("main.memory.top_facts", return_value=[]), \
         patch("main._get_persona_notes", return_value=""), \
         patch("main._prefetch_urls", return_value="家人聊天 message"), \
         patch("main._reply") as mock_reply, \
         patch("main._save_pending_any") as mock_save_pending, \
         patch("main._maybe_capture_calendar_event"), \
         patch("main._thinking_indicator"):
        main._handle_burst_flush("GRP001", "家人聊天 message", "TOKEN001")

    called_count = mock_reply.call_count + mock_save_pending.call_count
    assert called_count > 0, (
        f"Silent drop: burst quota retry miss 未呼叫 _reply 也沒 _save_pending_any "
        f"(_reply={mock_reply.call_count}, _save_pending_any={mock_save_pending.call_count})"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# S5b: burst flush + Gemini empty reply
# ═══════════════════════════════════════════════════════════════════════════════


def test_s5b_burst_flush_empty_reply_must_not_silent():
    """burst flush + Gemini 回 empty → 必須 fallback reply 不能 silent。"""
    with patch("main._llm_chat", return_value=""), \
         patch("main.memory.check_fact_cache", return_value=None), \
         patch("main.memory.get_context", return_value=[]), \
         patch("main.memory.top_facts", return_value=[]), \
         patch("main._get_persona_notes", return_value=""), \
         patch("main._prefetch_urls", return_value="家人閒聊 message"), \
         patch("main._reply") as mock_reply, \
         patch("main._save_pending_any") as mock_save_pending, \
         patch("main._maybe_capture_calendar_event"), \
         patch("main.memory.store_fact_cache"), \
         patch("main.memory.append_turn"), \
         patch("main._maybe_extract_facts"), \
         patch("main._thinking_indicator"):
        main._handle_burst_flush("GRP001", "家人閒聊 message", "TOKEN001")

    called_count = mock_reply.call_count + mock_save_pending.call_count
    assert called_count > 0, (
        f"Silent drop: burst empty reply 未呼叫 _reply 也沒 _save_pending_any "
        f"(_reply={mock_reply.call_count}, _save_pending_any={mock_save_pending.call_count})"
    )
