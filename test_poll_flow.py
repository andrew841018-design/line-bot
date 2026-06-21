import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest

import main
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

G = "C_poll_flow_test"
FATHER = "U38f817726f256ec1fdfa51cf57f4a645"


@pytest.fixture()
def poll_db(tmp_path, monkeypatch):
    import config

    db_path = tmp_path / "poll_flow.db"
    monkeypatch.setattr(config.settings, "sqlite_path", str(db_path))
    import family_poll

    monkeypatch.setattr(family_poll, "_DB_PATH", db_path)
    family_poll.init_db()
    family_poll.clear_group(G)
    yield family_poll
    family_poll.clear_group(G)


def _make_group_source(group_id: str = G, user_id: str = FATHER):
    src = MagicMock(spec=GroupSource)
    src.group_id = group_id
    src.user_id = user_id
    src.type = "group"
    return src


def _make_text_event(text: str, msg_id: str = "MSG001", user_id: str = FATHER):
    msg = MagicMock(spec=TextMessageContent)
    msg.id = msg_id
    msg.text = text
    msg.mention = None
    msg.quoted_message_id = None
    msg.quote_token = "qt"
    msg.type = "text"

    evt = MagicMock(spec=MessageEvent)
    evt.message = msg
    evt.source = _make_group_source(user_id=user_id)
    evt.reply_token = "TOKEN001"
    evt.timestamp = int(time.time() * 1000)
    dctx = MagicMock()
    dctx.is_redelivery = False
    evt.delivery_context = dctx
    return evt


def _install_noop_side_modules(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "knowledge_graph",
        types.SimpleNamespace(auto_extract_kg_async=lambda *a, **kw: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "food_signals",
        types.SimpleNamespace(extract_and_store_async=lambda *a, **kw: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "message_classifier",
        types.SimpleNamespace(classify_async=lambda *a, **kw: None),
    )


def test_text_flow_creates_poll_before_burst(poll_db, monkeypatch):
    _install_noop_side_modules(monkeypatch)
    replies: list[str] = []
    evt = _make_text_event("爸爸想知道，今天晚上有誰可以去吃凱薩", msg_id="m1")

    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._try_handle_calendar_correction", return_value=False),
        patch("main._detect_user_correction"),
        patch("main._auto_capture_text_if_important"),
        patch("main._maybe_extract_reminder"),
        patch("main.burst_filter.add_to_burst") as add_to_burst,
        patch("main._reply", side_effect=lambda _token, text, **_kw: replies.append(text)),
    ):
        main._handle_text_message(evt, G)

    assert not add_to_burst.called
    assert replies and replies[0].startswith("@all 民調開好了")
    assert poll_db.get_active_poll(G) is not None


def test_text_flow_reads_active_poll_reply_before_burst(poll_db, monkeypatch):
    _install_noop_side_modules(monkeypatch)
    poll_db.create_poll(G, "今天晚上吃凱薩誰可以", user_id=FATHER)
    replies: list[str] = []
    evt = _make_text_event("可以", msg_id="m2")

    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._try_handle_calendar_correction", return_value=False),
        patch("main._detect_user_correction"),
        patch("main._auto_capture_text_if_important"),
        patch("main._maybe_extract_reminder"),
        patch("main.burst_filter.add_to_burst") as add_to_burst,
        patch("main._llm_chat") as llm_chat,
        patch("main._reply", side_effect=lambda _token, text, **_kw: replies.append(text)),
    ):
        main._handle_text_message(evt, G)

    assert not add_to_burst.called
    assert not llm_chat.called
    assert replies and "爸爸 → 可以" in replies[0]
