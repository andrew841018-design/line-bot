import burst_filter
import main

from unittest.mock import MagicMock, patch

from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent


def test_short_substantive_text_should_trigger_reply():
    assert burst_filter._heuristic_decision("我買好了") == "respond"


def test_exact_chitchat_still_skips_reply():
    assert burst_filter._heuristic_decision("好") == "skip"
    assert burst_filter._heuristic_decision("哈哈") == "skip"


def test_quota_exhausted_text_still_enters_text_handler():
    msg = MagicMock(spec=TextMessageContent)
    msg.id = "MSG001"
    msg.text = "我買好了"
    msg.type = "text"

    src = MagicMock(spec=GroupSource)
    src.group_id = "GRP001"
    src.user_id = "USR001"

    event = MagicMock(spec=MessageEvent)
    event.message = msg
    event.source = src
    event.reply_token = "TOKEN001"
    event.delivery_context = MagicMock(is_redelivery=False)

    main.settings.allowed_group_id = "GRP001"
    main.settings.allowed_group_ids_raw = ""

    with (
        patch("main._quota_exhausted", return_value=True),
        patch("main._handle_text_message") as mock_text_handler,
        patch("main._save_pending_any") as mock_save_pending,
        patch("main._try_piggyback_drain_with_reply_token") as mock_piggyback,
        patch("main._spawn_piggyback_drain"),
    ):
        main._handle_event(event)

    mock_text_handler.assert_called_once_with(event, "GRP001")
    mock_save_pending.assert_not_called()
    mock_piggyback.assert_not_called()
