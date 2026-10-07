from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch

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
    AudioMessageContent,
    FileMessageContent,
    LocationMessageContent,
    StickerMessageContent,
)


def _event(message, *, user_id="U_SISTER"):
    source = MagicMock(spec=GroupSource)
    source.group_id = "G_TEST"
    source.user_id = user_id
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.message = message
    event.reply_token = "TOKEN_TEST"
    return event


def _text_message():
    message = MagicMock(spec=TextMessageContent)
    message.id = "MSG_TEST"
    message.text = "請幫我查這件事"
    return message


def _image_message():
    message = MagicMock(spec=ImageMessageContent)
    message.id = "MSG_TEST"
    return message


def test_configured_sister_sender_is_terminalized_without_any_reply_route():
    event = _event(_text_message())

    with (
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main.memory.mark_inbound_events_completed_no_reply") as complete,
        patch("main._handle_text_message") as handle_text,
        patch("main._spawn_piggyback_drain") as drain,
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(event)

    handle_text.assert_not_called()
    drain.assert_not_called()
    complete.assert_called_once_with("G_TEST", ["MSG_TEST"])


@pytest.mark.parametrize("message_type", [
    ImageMessageContent, VideoMessageContent, AudioMessageContent,
    FileMessageContent, LocationMessageContent, StickerMessageContent,
])
def test_configured_sister_media_sender_is_also_terminalized(message_type):
    message = MagicMock(spec=message_type)
    message.id = "MSG_TEST"
    event = _event(message)

    with (
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main.memory.mark_inbound_events_completed_no_reply") as complete,
        patch("main._handle_image_message") as handle_image,
        patch("main._reply") as reply,
        patch("main._spawn_piggyback_drain") as drain,
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(event)

    handle_image.assert_not_called()
    reply.assert_not_called()
    drain.assert_not_called()
    complete.assert_called_once_with("G_TEST", ["MSG_TEST"])


def test_other_sender_is_not_silenced():
    event = _event(_text_message(), user_id="U_OTHER")

    with (
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main.memory.log_raw_message"),
        patch("main._handle_text_message") as handle_text,
        patch("main._spawn_piggyback_drain"),
        patch("main._register_reply_mention_targets"),
        patch("main._clear_reply_mention_targets"),
        patch.object(main.settings, "allowed_group_ids_raw", ""),
        patch.object(main.settings, "allowed_group_id", ""),
    ):
        main._handle_event(event)

    handle_text.assert_called_once_with(event, "G_TEST")


def test_completed_no_reply_redelivery_stops_before_routing():
    event = _event(_text_message())
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
