"""Andrew 2026-10-09：「取消妹妹不回覆這個設定，從現在起妹妹的言論要回覆」。

2026-09-19 起設定裡「妹妹」的訊息一律零回覆，10/05 只放行她引用咪寶的留言。
這兩條都已取消：她的每一則訊息（有沒有引用、文字或媒體）都和其他家人走同一條
路由、一樣寫進可回想的紀錄。內容全是合成的。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
from linebot.v3.webhooks import (  # noqa: E402
    GroupSource,
    ImageMessageContent,
    MessageEvent,
    TextMessageContent,
    VideoMessageContent,
)


def _event(text="咪寶你剛剛說的那個要怎麼做？", *, user_id="U_SISTER", quoted=None, message=None):
    if message is None:
        message = MagicMock(spec=TextMessageContent)
        message.id = "MSG_TEST"
        message.text = text
        message.quoted_message_id = quoted
        message.mention = None
    source = MagicMock(spec=GroupSource)
    source.group_id = "G_TEST"
    source.user_id = user_id
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.message = message
    event.reply_token = "TOKEN_TEST"
    event.delivery_context = None
    return event


class _Calls:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _run(event, *, quoted_raw=("__bot__", "咪寶之前的留言")):
    with (
        # 角色設定還在（提及、週報會用到），只是不再拿來擋回覆。
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main.memory.log_raw_message") as log_raw,
        patch("main.memory.get_raw_message", return_value=quoted_raw),
        patch("main.memory.mark_inbound_events_completed_no_reply") as complete,
        patch("main._handle_text_message") as handle_text,
        patch("main._submit_media_handler") as submit_media,
        patch("main._spawn_piggyback_drain"),
        patch("main._register_reply_mention_targets"),
        patch("main._clear_reply_mention_targets"),
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(event)
    return _Calls(handle_text=handle_text, submit_media=submit_media, complete=complete, log_raw=log_raw)


@pytest.mark.parametrize(
    "quoted, quoted_raw",
    [
        (None, None),
        ("", None),
        ("BOT_MSG", ("__bot__", "咪寶之前的留言")),
        ("FAMILY_MSG", ("U_MOM", "家人的留言")),
        ("UNKNOWN", None),
    ],
    ids=["no-quote", "empty-quote-id", "quotes-bot", "quotes-family", "quote-not-archived"],
)
@pytest.mark.parametrize("user_id", ["U_SISTER", "U_OTHER"])
def test_every_text_message_is_routed_and_indexed(user_id, quoted, quoted_raw):
    event = _event("請幫我查這件事", user_id=user_id, quoted=quoted)

    calls = _run(event, quoted_raw=quoted_raw)

    calls.handle_text.assert_called_once_with(event, "G_TEST")
    calls.complete.assert_not_called()
    assert calls.log_raw.call_args.kwargs.get("index_for_recall", True) is True


def test_redelivered_sister_message_is_handled_normally():
    event = _event()
    event.delivery_context = MagicMock(is_redelivery=True)

    calls = _run(event)

    calls.handle_text.assert_called_once()
    calls.complete.assert_not_called()


@pytest.mark.parametrize(
    "message_type, handler",
    [(ImageMessageContent, "_handle_image_message"), (VideoMessageContent, "_handle_video_message")],
)
def test_sister_media_goes_to_the_media_handlers(message_type, handler):
    message = MagicMock(spec=message_type)
    message.id = "MSG_TEST"

    calls = _run(_event(message=message))

    calls.submit_media.assert_called_once()
    assert calls.submit_media.call_args.args[0] is getattr(main, handler)
    calls.complete.assert_not_called()


def test_zero_reply_gate_is_gone():
    assert not hasattr(main, "_is_silenced_sender")
    assert not hasattr(main, "_quotes_bot_message")


def test_completed_no_reply_redelivery_stops_before_routing():
    event = _event()
    event.delivery_context = MagicMock(is_redelivery=True)
    with (
        patch("main.memory.begin_inbound_event", return_value="completed_no_reply"),
        patch("main._handle_text_message") as handler,
        patch("main._reply") as reply,
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(event)
    handler.assert_not_called()
    reply.assert_not_called()
