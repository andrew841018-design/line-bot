"""Regression tests for disabled pending reply mechanism."""

from __future__ import annotations

from unittest.mock import MagicMock

import pending_store


def _patch_pending_store(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / ".pending.lock")
    pending_store.save_full({})


def _mk_text(msg_id: str, text: str):
    from linebot.v3.webhooks import TextMessageContent

    msg = TextMessageContent.__new__(TextMessageContent)
    object.__setattr__(msg, "id", msg_id)
    object.__setattr__(msg, "text", text)
    object.__setattr__(msg, "quoteToken", None)
    object.__setattr__(msg, "quotedMessageId", None)
    return msg


def test_disabled_save_pending_any_noops(tmp_path, monkeypatch):
    import main

    _patch_pending_store(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", False)

    event = MagicMock()
    msg = _mk_text("M1", "不要排 pending")

    main._save_pending_any(event, "G1", "U1", msg)

    assert pending_store.load() == {}


def test_disabled_startup_drain_preserves_existing_backlog(tmp_path, monkeypatch):
    import main

    _patch_pending_store(tmp_path, monkeypatch)
    pending_store.save_full({"G1": [{"message_id": "M1", "type": "text", "text": "舊訊息"}]})
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", False)
    drain_calls = []
    monkeypatch.setattr(
        main,
        "_drain_pending_for_group",
        lambda *args, **kwargs: drain_calls.append((args, kwargs)) or True,
    )

    main._process_pending_on_startup()

    assert drain_calls == []
    assert pending_store.load().get("G1", [])[0]["message_id"] == "M1"


def test_reply_does_not_bundle_pending_text_when_disabled(monkeypatch):
    import main

    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", False)
    monkeypatch.setattr(
        main,
        "_load_pending_explicit",
        lambda: {"G1": [{"message_id": "M1", "type": "text", "text": "舊訊息"}]},
    )
    peek_calls = []
    monkeypatch.setattr(
        main,
        "_peek_text_pending_for_drain",
        lambda *args, **kwargs: peek_calls.append((args, kwargs)) or [],
    )
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    monkeypatch.setattr(main, "_get_line_config", lambda: MagicMock())
    monkeypatch.setattr(main.memory, "log_raw_message", lambda *args, **kwargs: None)
    import calendar_db
    import reminder_push

    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *args, **kwargs: [])
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *args, **kwargs: [])

    sent_counts = []

    class FakeApi:
        def __init__(self, _client):
            pass

        def reply_message(self, req):
            sent_counts.append(len(req.messages))
            resp = MagicMock()
            resp.sent_messages = []
            return resp

    class FakeClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", FakeClient)
    monkeypatch.setattr(main, "MessagingApi", FakeApi)

    main._reply("TOKEN", "即時回覆", group_id="G1")

    assert sent_counts == [1]
    assert peek_calls == []
