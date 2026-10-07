"""Andrew 2026-10-05：妹妹引用咪寶的留言時，和其他家人一樣照常處理；其他訊息照舊零回覆。"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
from linebot.v3.webhooks import GroupSource, ImageMessageContent, MessageEvent, TextMessageContent  # noqa: E402


def _event(text="咪寶你剛剛說的那個要怎麼做？", *, user_id="U_SISTER", quoted="BOT_MSG"):
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


def _run(event, *, quoted_raw=("__bot__", "咪寶之前的留言"), lookup_error=None):
    lookup = MagicMock(return_value=quoted_raw, side_effect=lookup_error)
    with (
        patch("main.line_mentions.user_id_for_family_role", return_value="U_SISTER"),
        patch("main.memory.begin_inbound_event", return_value="new"),
        patch("main.memory.log_raw_message") as log_raw,
        patch("main.memory.get_raw_message", lookup),
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
    return _Calls(
        handle_text=handle_text, submit_media=submit_media, complete=complete, log_raw=log_raw, lookup=lookup
    )


@pytest.mark.parametrize("text", ["咪寶你剛剛說的那個要怎麼做？", "改到週日下午三點", "謝謝"])
def test_sister_quoting_bot_message_is_handled_like_other_members(text):
    calls = _run(_event(text))

    calls.handle_text.assert_called_once()
    calls.complete.assert_not_called()
    calls.lookup.assert_called_once_with("G_TEST", "BOT_MSG")
    assert calls.log_raw.call_args.kwargs["index_for_recall"] is True


@pytest.mark.parametrize(
    "event, quoted_raw",
    [
        (_event(quoted="FAMILY_MSG"), ("U_MOM", "媽媽的留言")),
        (_event(quoted="UNKNOWN"), None),
        (_event(quoted=None), ("__bot__", "咪寶之前的留言")),
        (_event(quoted=""), ("__bot__", "咪寶之前的留言")),
    ],
    ids=["quotes-family", "quote-not-archived", "no-quote", "empty-quote-id"],
)
def test_other_sister_messages_stay_silent(event, quoted_raw):
    calls = _run(event, quoted_raw=quoted_raw)

    calls.handle_text.assert_not_called()
    calls.complete.assert_called_once_with("G_TEST", ["MSG_TEST"])
    assert calls.log_raw.call_args.kwargs["index_for_recall"] is False


def test_sister_stays_silent_when_quote_lookup_fails():
    calls = _run(_event(), lookup_error=RuntimeError("db locked"))

    calls.handle_text.assert_not_called()
    calls.complete.assert_called_once_with("G_TEST", ["MSG_TEST"])
    assert calls.log_raw.call_args.kwargs["index_for_recall"] is False


def test_redelivered_sister_quote_of_bot_is_still_handled_normally():
    event = _event()
    event.delivery_context = MagicMock(is_redelivery=True)

    calls = _run(event)

    calls.handle_text.assert_called_once()
    calls.complete.assert_not_called()


def test_sister_media_stays_silent():
    message = MagicMock(spec=ImageMessageContent)
    message.id = "MSG_TEST"
    event = _event()
    event.message = message

    with patch("main.memory.log_raw_message_meta", create=True):
        calls = _run(event)

    calls.submit_media.assert_not_called()
    calls.lookup.assert_not_called()
    calls.complete.assert_called_once_with("G_TEST", ["MSG_TEST"])


def test_other_member_quoting_bot_keeps_normal_routing():
    calls = _run(_event(user_id="U_OTHER"))

    calls.handle_text.assert_called_once()
    calls.lookup.assert_not_called()
    calls.complete.assert_not_called()
