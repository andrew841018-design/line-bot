"""Reply-token piggyback reminder tests — 2 case (advisor minimal)。

5/22 cake scenario：今天 5/21 是 T-1，原本 launchd push 因 LINE 月配額 200/200 爆住，
透過 _reply 在 user 講話時 piggyback reminder 進同一個 reply_message API call（免費）。
"""

from __future__ import annotations

import importlib
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


# ── Test 1: reply 成功 → reminder mark ✓ ────────────────────────────────
def test_piggyback_marks_reminded_on_reply_success(tmp_cal_db, monkeypatch):
    """模擬 5/22 cake T-1 場景：家人講話 → _reply 把 reminder 塞 reply_message → 成功後 mark_reminded_1d。"""
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
    # 確保 _reply 內 `import calendar_db` 拿到 tmp_cal_db（已 reload via fixture）

    # 跑 reply
    main._reply("fake_reply_token", "嗨", group_id=GID)

    # assert: messages_to_send 含 reminder
    msgs = captured.get("messages", [])
    assert len(msgs) >= 2  # 原 reply + reminder
    reminder_texts = [m.text for m in msgs[1:]]
    assert any("明天活動提醒" in t for t in reminder_texts)
    assert any("拿生日蛋糕" in t for t in reminder_texts)

    # assert: reminded_1d 已 mark
    with tmp_cal_db._conn() as c:
        row = c.execute(
            "SELECT reminded_1d FROM events WHERE event_id=?", (eid,),
        ).fetchone()
    assert row[0] is not None


# ── Test 2: reply 失敗（throws）→ reminder NOT marked ──────────────────
def test_piggyback_not_marked_on_reply_failure(tmp_cal_db, monkeypatch):
    """reply API throw → fall back push → reminder mark 不能執行（peek-then-confirm）。
    下次 user 講話再試。
    """
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

    main._reply("fake_token", "嗨", group_id=GID)

    # reminded_1d 仍 NULL（reply 失敗不 mark）
    with tmp_cal_db._conn() as c:
        row = c.execute(
            "SELECT reminded_1d FROM events WHERE event_id=?", (eid,),
        ).fetchone()
    assert row[0] is None
