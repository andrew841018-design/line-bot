"""Intentional-silence regression tests.

Useful content still replies normally, while unknown/unsupported input and the
user-rejected generic degraded replies must finish durably without a LINE
message.  The tests distinguish that approved silent outcome from accidental
loss of an OCR/full analysis result.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
from linebot.v3.webhooks import (  # noqa: E402
    AudioMessageContent,
    FileMessageContent,
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
    """Sticker / Location / Template 訊息只記 raw audit；不 pending、不回低價值 fallback。"""
    msg = _make_sticker_like_msg()
    evt = _make_message_event(msg)

    with patch("main._reply") as mock_reply, \
         patch("main._save_pending_any") as mock_save_pending, \
         patch("main.memory.log_raw_message"), \
         patch("main.memory.log_raw_message_meta") as mock_log_meta, \
         patch(
             "main.memory.mark_inbound_events_completed_no_reply"
         ) as mock_complete, \
         patch("main._quota_exhausted", return_value=False), \
         patch("main._spawn_piggyback_drain"), \
         patch("main.settings.allowed_group_id", "GRP001"):
        main._handle_event(evt)

    mock_save_pending.assert_not_called()
    mock_reply.assert_not_called()
    mock_log_meta.assert_called_once_with(
        "GRP001", "MSG_STICKER_001", media_type="unknown"
    )
    mock_complete.assert_called_once_with("GRP001", ["MSG_STICKER_001"])


@pytest.mark.parametrize("message_type", [FileMessageContent, AudioMessageContent])
def test_quota_drop_without_pending_marks_inbound_terminal(message_type):
    """Intentional quota suppression must not leave the delivery lease stale."""
    msg = MagicMock(spec=message_type)
    msg.id = "MSG_QUOTA_DROP_001"
    if message_type is FileMessageContent:
        msg.file_name = "synthetic.txt"
    event = _make_message_event(msg)

    with patch("main.memory.begin_inbound_event", return_value="new"), \
         patch("main.memory.mark_inbound_events_completed_no_reply") as complete, \
         patch("main._quota_exhausted", return_value=True), \
         patch("main._pending_reply_enabled", return_value=False), \
         patch("main._save_pending_any") as save_pending, \
         patch("main._try_piggyback_drain_with_reply_token") as piggyback, \
         patch("main._handle_file_message") as file_handler, \
         patch("main._handle_audio_message") as audio_handler, \
         patch("main.settings.allowed_group_ids_raw", ""), \
         patch("main.settings.allowed_group_id", ""):
        main._handle_event(event)

    complete.assert_called_once_with("GRP001", ["MSG_QUOTA_DROP_001"])
    save_pending.assert_not_called()
    piggyback.assert_not_called()
    file_handler.assert_not_called()
    audio_handler.assert_not_called()


@pytest.mark.parametrize("message_type", [FileMessageContent, AudioMessageContent])
def test_quota_drop_with_pending_keeps_existing_queue_path(message_type):
    """Pending-enabled quota handling must queue, not mark the event terminal."""
    msg = MagicMock(spec=message_type)
    msg.id = "MSG_QUOTA_PENDING_001"
    if message_type is FileMessageContent:
        msg.file_name = "synthetic.txt"
    event = _make_message_event(msg)

    with patch("main.memory.begin_inbound_event", return_value="new"), \
         patch("main.memory.mark_inbound_events_completed_no_reply") as complete, \
         patch("main._quota_exhausted", return_value=True), \
         patch("main._pending_reply_enabled", return_value=True), \
         patch("main._save_pending_any") as save_pending, \
         patch("main._try_piggyback_drain_with_reply_token") as piggyback, \
         patch("main._handle_file_message") as file_handler, \
         patch("main._handle_audio_message") as audio_handler, \
         patch("main.settings.allowed_group_ids_raw", ""), \
         patch("main.settings.allowed_group_id", ""):
        main._handle_event(event)

    complete.assert_not_called()
    save_pending.assert_called_once()
    piggyback.assert_called_once_with("TOKEN001", "GRP001")
    file_handler.assert_not_called()
    audio_handler.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# S5a: burst flush + Gemini quota retry miss
# ═══════════════════════════════════════════════════════════════════════════════


def test_s5a_burst_flush_quota_retry_miss_routes_to_silent_sink():
    """burst primary + cached fallback 都 miss 時只路由到中央 silent sink。"""
    with patch(
        "main._llm_chat",
        side_effect=Exception("quota exceeded for quota metric 'gemini'"),
    ), \
         patch("main._is_quota_error", return_value=True), \
         patch("main._mark_quota_exhausted"), \
         patch("main._gemini_llm_chat", return_value=""), \
         patch("main.memory.check_fact_cache", return_value=None), \
         patch("main.memory.get_context", return_value=[]), \
         patch("main.memory.top_facts", return_value=[]), \
         patch("main._get_persona_notes", return_value=""), \
         patch("main._prefetch_urls", return_value="家人聊天 message"), \
         patch("main._reply") as mock_reply, \
         patch("main._save_pending_any") as mock_save_pending, \
         patch("main._save_pending_burst_text") as mock_save_pending_burst, \
         patch("main._maybe_capture_calendar_event"), \
         patch("main._thinking_indicator"):
        main._handle_burst_flush("GRP001", "家人聊天 message", "TOKEN001")

    mock_reply.assert_called_once_with(
        "TOKEN001",
        main._visible_llm_degraded_reply(),
        group_id="GRP001",
        allow_push_fallback=True,
    )
    mock_save_pending.assert_not_called()
    mock_save_pending_burst.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# S5b: burst flush + Gemini empty reply
# ═══════════════════════════════════════════════════════════════════════════════


def test_s5b_burst_flush_empty_reply_is_silently_terminalized():
    """burst empty reply must not cross the real outbound suppression gate."""
    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    with patch("main._llm_chat", return_value=""), \
         patch("main._quota_exhausted", return_value=False), \
         patch("main.memory.check_fact_cache", return_value=None), \
         patch("main.memory.get_context", return_value=[]), \
         patch("main.memory.top_facts", return_value=[]), \
         patch("main._get_persona_notes", return_value=""), \
         patch("main._prefetch_urls", return_value="家人閒聊 message"), \
         patch("main._pending_reply_enabled", return_value=False), \
         patch("main._reminder_reply_piggyback_enabled", return_value=False), \
         patch("main.memory.claim_reminder_confirmations", return_value=[]), \
         patch("main.memory.log_raw_message"), \
         patch("main.ApiClient", return_value=mock_api), \
         patch("main.MessagingApi", return_value=mock_messaging), \
         patch("main.settings.bot_muted", False), \
         patch("main._maybe_capture_calendar_event"), \
         patch("main.memory.store_fact_cache"), \
         patch("main.memory.append_turn"), \
         patch("main._maybe_extract_facts"), \
         patch("main._thinking_indicator"), \
         patch("main._inbound_reply_by_token", {}), \
         patch(
             "main.memory.mark_inbound_events_completed_no_reply",
             return_value=2,
         ) as complete:
        main._handle_burst_flush(
            "GRP001",
            "家人閒聊 message",
            "TOKEN001",
            message_ids=["MSG001", "MSG002"],
        )

    mock_messaging.reply_message.assert_not_called()
    complete.assert_called_once_with("GRP001", ["MSG001", "MSG002"])
