"""Reply-token reminder behavior tests.

Due reminders are still delivered by scheduled jobs first, but can also
piggyback on ordinary group reply tokens when LINE push quota is exhausted.
"""

from __future__ import annotations

import importlib
import json
from datetime import date, timedelta

import pytest


@pytest.fixture
def tmp_cal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_pgb.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    import config

    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)
    import calendar_db

    importlib.reload(calendar_db)
    return calendar_db


def test_reply_bundles_calendar_reminder_on_user_message(
    tmp_cal_db, monkeypatch
):
    """家人普通發言時，_reply 可把 due calendar reminder 塞進同一個 reply。"""
    GID = "G1"
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(
        group_id=GID, title="拿生日蛋糕",
        event_date=tomorrow, event_time="14:00",
        location="喜來登", event_type="family_gathering",
    )
    assert eid

    import main

    # mute 守門先解開
    monkeypatch.setattr(main.settings, "bot_muted", False)
    # 攔截 LINE API — 模擬 reply 成功
    captured: dict = {}

    class _FakeMessagingApi:
        def __init__(self, _):
            pass
        def reply_message(self, req):
            captured["messages"] = req.messages
            captured["reply_token"] = req.reply_token

            class _Resp:
                sent_messages = []
            return _Resp()
        def push_message(self, req):
            captured["push_called"] = True

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)
    # main 用 calendar_db / event_reminder local import，重 patch
    monkeypatch.setattr(main, "calendar_db", tmp_cal_db, raising=False)
    import event_reminder
    monkeypatch.setattr(event_reminder, "calendar_db", tmp_cal_db)
    import reminder_push
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *a, **kw: [])
    # 確保 _reply 內 `import calendar_db` 拿到 tmp_cal_db（已 reload via fixture）

    # 跑 reply
    main._reply("fake_reply_token", "嗨", group_id=GID)

    texts = [m.text for m in captured.get("messages", [])]
    assert texts[0] == "嗨"
    assert len(texts) == 2
    assert "拿生日蛋糕" in texts[1]

    # reply 成功後才 mark，避免 scheduled job 重複推同一階段。
    with tmp_cal_db._conn() as c:
        row = c.execute(
            "SELECT reminded_1d FROM events WHERE event_id=?", (eid,),
        ).fetchone()
    assert row[0] is not None


def test_piggyback_not_marked_on_reply_failure(tmp_cal_db, monkeypatch):
    """reply API throw/fallback still must not mark reminder stages."""
    GID = "G1"
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(
        group_id=GID, title="拿蛋糕", event_date=tomorrow,
        event_type="family_gathering",
    )
    assert eid

    import main

    monkeypatch.setattr(main.settings, "bot_muted", False)

    class _FakeMessagingApi:
        def __init__(self, _):
            pass
        def reply_message(self, req):
            raise RuntimeError("reply_token expired (simulated)")
        def push_message(self, req):
            pass  # push fallback noop

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)
    monkeypatch.setattr(main, "calendar_db", tmp_cal_db, raising=False)
    import event_reminder
    monkeypatch.setattr(event_reminder, "calendar_db", tmp_cal_db)
    import reminder_push
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *a, **kw: [])

    main._reply("fake_token", "嗨", group_id=GID)

    # reminded_1d 仍 NULL（reply 失敗不 mark）
    with tmp_cal_db._conn() as c:
        row = c.execute(
            "SELECT reminded_1d FROM events WHERE event_id=?", (eid,),
        ).fetchone()
    assert row[0] is None


def _seed_text_pending(pending_store, group_id: str, message_id: str = "M1") -> None:
    pending_store.save_full(
        {
            group_id: [
                {
                    "type": "text",
                    "message_id": message_id,
                    "user_id": "U1",
                    "timestamp": 1,
                    "text": "quota 時漏掉的訊息",
                }
            ]
        }
    )


def _patch_reply_pending_fast_path(main, monkeypatch):
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_drop_stale_pending", lambda *a, **kw: 0)
    monkeypatch.setattr(main, "_llm_chat", lambda *a, **kw: "pending reply")
    monkeypatch.setattr(main, "_get_persona_notes", lambda group_id: [])
    monkeypatch.setattr(main.memory, "get_context", lambda group_id: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda group_id: [])
    monkeypatch.setattr(main.memory, "log_raw_message", lambda *a, **kw: None)

    import calendar_db
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *a, **kw: [])
    import reminder_push
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *a, **kw: [])


def test_reply_failure_preserves_pending_piggyback(monkeypatch):
    """Normal _reply piggyback must be peek-then-confirm, same as quota path."""
    import main
    import pending_store

    _seed_text_pending(pending_store, "G1")
    _patch_reply_pending_fast_path(main, monkeypatch)

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            raise RuntimeError("reply token expired")

        def push_message(self, req):
            return None

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    ids = [it["message_id"] for it in pending_store.load().get("G1", [])]
    assert ids == ["M1"]


def test_reply_ambiguous_failure_does_not_fallback_push(monkeypatch):
    """Timeout/5xx after reply_message may already be accepted by LINE; avoid duplicate push."""
    import main
    import pending_store

    _seed_text_pending(pending_store, "G1")
    _patch_reply_pending_fast_path(main, monkeypatch)
    pushed = []

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            raise TimeoutError("Read timed out")

        def push_message(self, req):
            pushed.append(req)

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert pushed == []
    ids = [it["message_id"] for it in pending_store.load().get("G1", [])]
    assert ids == ["M1"]


def test_fast_path_uses_reply_token_for_reminder_push(monkeypatch):
    """Normal text messages can carry due reminder_push items."""
    import main
    from linebot.v3.messaging import (
        MentionSubstitutionObject,
        TextMessageV2,
        UserMentionTarget,
    )

    captured: dict = {}
    marked: list[tuple[int, str]] = []
    reminder_message = TextMessageV2(
        text="{target}\n⏰ 提醒（明天）\n2026-06-16 08:00 禁食",
        substitution={
            "target": MentionSubstitutionObject(
                mentionee=UserMentionTarget(userId="U1")
            )
        },
    )

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            captured["messages"] = req.messages
            captured["reply_token"] = req.reply_token

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import calendar_db
    import reminder_push

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(calendar_db, "REMINDER_OFFSETS", (1,))
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *a, **kw: [])
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *a, **kw: [
            {
                "reminder_id": 21,
                "group_id": "G1",
                "stage": "3d",
                "text": "@當事人\n⏰ 提醒（3 天後）\n2026-06-16 08:00 禁食",
                "message": reminder_message,
                "action": "禁食",
            }
        ],
    )
    monkeypatch.setattr(
        reminder_push,
        "mark_reminders_pushed",
        lambda pushes: marked.extend(pushes) or len(pushes),
    )
    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    assert main._try_piggyback_reminders_fast_path("reply-token", "G1") is True

    assert captured["reply_token"] == "reply-token"
    assert len(captured["messages"]) == 1
    assert captured["messages"][0].text.startswith("{target}\n⏰ 提醒（明天）")
    assert marked == [(21, "3d")]


def test_reply_success_commits_pending_piggyback(monkeypatch):
    import main
    import pending_store

    _seed_text_pending(pending_store, "G1")
    _patch_reply_pending_fast_path(main, monkeypatch)

    captured = {}

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            captured["messages"] = req.messages

            class _Resp:
                sent_messages = []

            return _Resp()

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert len(captured["messages"]) == 2
    assert pending_store.load().get("G1", []) == []


def test_reply_piggybacks_and_commits_reminder_creation_confirmation(monkeypatch):
    import line_mentions
    import main
    import pending_store

    pending_store.save_full({})
    _patch_reply_pending_fast_path(main, monkeypatch)
    monkeypatch.setattr(
        main.memory,
        "claim_reminder_confirmations",
        lambda group_id, limit: [
            {
                "confirmation_id": 41,
                "source_ref": "pending_reminder:7",
                "text": "已新增提醒\n時間：2026-07-16 09:00\n事項：買按摩油\n對象：@爸爸",
                "created_at": 1,
                "claim_token": "claim-41",
            }
        ],
    )
    deleted = []
    released = []
    monkeypatch.setattr(
        main.memory,
        "delete_sent_reminder_confirmations",
        lambda group_id, ids: deleted.extend(ids) or len(ids),
    )
    monkeypatch.setattr(
        main.memory,
        "release_reminder_confirmations",
        lambda group_id, ids: released.extend(ids) or len(ids),
    )
    monkeypatch.setattr(
        line_mentions,
        "user_id_for_alias",
        lambda alias: "U_DAD" if alias == "爸爸" else None,
    )
    captured = {}

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            captured["messages"] = req.messages

            class _Resp:
                sent_messages = []

            return _Resp()

        def push_message(self, req):
            raise AssertionError("confirmation must not use push_message")

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert len(captured["messages"]) == 2
    assert type(captured["messages"][1]).__name__ == "TextMessageV2"
    assert deleted == [(41, "claim-41")]
    assert released == []


def test_reply_failure_releases_reminder_creation_confirmation(monkeypatch):
    import main
    import pending_store

    pending_store.save_full({})
    _patch_reply_pending_fast_path(main, monkeypatch)
    monkeypatch.setattr(
        main.memory,
        "claim_reminder_confirmations",
        lambda group_id, limit: [
            {
                "confirmation_id": 42,
                "source_ref": "pending_reminder:8",
                "text": "已新增提醒\n時間：2026-07-16 09:00\n事項：買按摩油",
                "created_at": 1,
                "claim_token": "claim-42",
            }
        ],
    )
    deleted = []
    released = []
    monkeypatch.setattr(
        main.memory,
        "delete_sent_reminder_confirmations",
        lambda group_id, ids: deleted.extend(ids) or len(ids),
    )
    monkeypatch.setattr(
        main.memory,
        "release_reminder_confirmations",
        lambda group_id, ids: released.extend(ids) or len(ids),
    )

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            raise TimeoutError("ambiguous reply failure")

        def push_message(self, req):
            raise AssertionError("ambiguous reply failure must not push")

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert deleted == []
    assert released == [(42, "claim-42")]


def test_outbox_failure_does_not_block_primary_reply(monkeypatch):
    import main
    import pending_store

    pending_store.save_full({})
    _patch_reply_pending_fast_path(main, monkeypatch)
    monkeypatch.setattr(
        main.memory,
        "claim_reminder_confirmations",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("outbox unavailable")),
    )
    captured = {}

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            captured["texts"] = [message.text for message in req.messages]

            class _Resp:
                sent_messages = []

            return _Resp()

        def push_message(self, req):
            raise AssertionError("primary reply should use reply_message")

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert captured["texts"] == ["primary reply"]


def test_reply_bundles_reminder_push_piggyback(monkeypatch):
    import main
    import pending_store
    import reminder_push

    pending_store.save_full({})
    _patch_reply_pending_fast_path(main, monkeypatch)
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *a, **kw: [
            {
                "reminder_id": 6,
                "group_id": "G1",
                "stage": "1d",
                "text": "⏰ 提醒（明天）\n2026-06-02 00:00 去看醫生",
                "action": "去看醫生",
            }
        ],
    )
    marked = []
    monkeypatch.setattr(
        reminder_push,
        "mark_reminders_pushed",
        lambda reminders: marked.extend(reminders) or len(reminders),
    )

    captured = {}

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            captured["texts"] = [m.text for m in req.messages]

            class _Resp:
                sent_messages = []

            return _Resp()

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert captured["texts"] == [
        "primary reply",
        "⏰ 提醒（明天）\n2026-06-02 00:00 去看醫生",
    ]
    assert marked == [(6, "1d")]


def test_reply_failure_does_not_mark_reminder_push_piggyback(monkeypatch):
    import main
    import pending_store
    import reminder_push

    pending_store.save_full({})
    _patch_reply_pending_fast_path(main, monkeypatch)
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *a, **kw: [
            {
                "reminder_id": 6,
                "group_id": "G1",
                "stage": "1d",
                "text": "⏰ 提醒（明天）\n2026-06-02 00:00 去看醫生",
                "action": "去看醫生",
            }
        ],
    )
    marked = []
    monkeypatch.setattr(
        reminder_push,
        "mark_reminders_pushed",
        lambda reminders: marked.extend(reminders) or len(reminders),
    )

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            raise TimeoutError("Read timed out")

        def push_message(self, req):
            raise AssertionError("ambiguous reply failure must not fallback push")

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert marked == []


def test_reply_piggyback_skips_media_entries(monkeypatch, tmp_path):
    """Normal reply-token piggyback must stay bounded and leave media to background drain."""
    import main
    import pending_store

    media = tmp_path / "pending.bin"
    media.write_bytes(b"x")
    pending_store.save_full(
        {
            "G1": [
                {
                    "type": "file",
                    "message_id": "F1",
                    "user_id": "U1",
                    "timestamp": 1,
                    "file_name": "a.pdf",
                    "media_path": str(media),
                }
            ]
        }
    )
    _patch_reply_pending_fast_path(main, monkeypatch)
    monkeypatch.setattr(
        main,
        "_peek_pending_for_piggyback",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("media path used")),
    )
    captured = {}

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            captured["messages"] = req.messages

            class _Resp:
                sent_messages = []

            return _Resp()

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id="G1")

    assert [m.text for m in captured["messages"]] == ["primary reply"]
    ids = [it["message_id"] for it in pending_store.load().get("G1", [])]
    assert ids == ["F1"]


def test_reply_piggyback_skips_when_drain_lock_busy(monkeypatch):
    import main
    import pending_store

    _seed_text_pending(pending_store, "G1")
    _patch_reply_pending_fast_path(main, monkeypatch)

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            class _Resp:
                sent_messages = []

            return _Resp()

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    slot = main._try_acquire_drain_slot("G1")
    assert slot is not None
    try:
        main._reply("fake_token", "primary reply", group_id="G1")
    finally:
        slot.release()

    ids = [it["message_id"] for it in pending_store.load().get("G1", [])]
    assert ids == ["M1"]


def test_reminder_piggyback_uses_event_reminder_one_off_policy(
    tmp_cal_db, tmp_path, monkeypatch
):
    """Piggyback reminders must not bypass launchd's one-off skip/ask policy."""
    GID = "G1"
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(
        group_id=GID, title="private one-off",
        event_date=tomorrow, event_time="08:00",
        event_type="family_gathering",
    )
    cfg = tmp_path / "event_reminder_private.json"
    cfg.write_text(
        json.dumps(
            {
                "event_ids": [eid],
                "offset": 2,
                "placeholder": "target",
                "mention_text": "{target} confirm this private event",
                "plain_text": "confirm this private event",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVENT_REMINDER_PRIVATE_CONFIG", str(cfg))

    import main
    import event_reminder

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    monkeypatch.setattr(main, "calendar_db", tmp_cal_db, raising=False)
    monkeypatch.setattr(event_reminder, "calendar_db", tmp_cal_db)
    import reminder_push
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *a, **kw: [])
    captured: dict = {}

    class _FakeMessagingApi:
        def __init__(self, _):
            pass

        def reply_message(self, req):
            captured["texts"] = [m.text for m in req.messages]

            class _Resp:
                sent_messages = []

            return _Resp()

    class _FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", _FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)

    main._reply("fake_token", "primary reply", group_id=GID)

    assert captured["texts"] == ["primary reply"]
