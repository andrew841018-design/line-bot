"""calendar_db / calendar_extractor / event_reminder 測試。

DB 用 monkeypatch 換成 tmp_path 下的 sqlite，避免污染 line_bot.db。
extractor / reminder 都用 mock 不打 Gemini / LINE API。
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def tmp_calendar_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_cal.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    # 重 import 讓 module 重新讀 settings
    import config

    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)

    import calendar_db

    importlib.reload(calendar_db)
    return calendar_db


def test_insert_and_list(tmp_calendar_db):
    cd = tmp_calendar_db
    today = date.today().isoformat()
    eid = cd.insert_event(
        group_id="G1",
        title="家族聚餐",
        event_date=today,
        event_time="18:00",
        location="鼎泰豐",
        participants=["媽媽", "爸爸"],
    )
    assert eid

    events = cd.list_upcoming("G1", days=30)
    assert len(events) == 1
    assert events[0]["title"] == "家族聚餐"
    assert events[0]["status"] == "active"
    assert json.loads(events[0]["participants"]) == ["媽媽", "爸爸"]


def test_cancel(tmp_calendar_db):
    cd = tmp_calendar_db
    eid = cd.insert_event(
        group_id="G1",
        title="去花蓮",
        event_date=(date.today() + timedelta(days=10)).isoformat(),
    )
    target = cd.find_active_event("G1", keyword="花蓮")
    assert target["event_id"] == eid

    assert cd.cancel_event(eid) is True
    # 取消後 list_upcoming 不應再列出
    assert cd.list_upcoming("G1", days=30) == []
    # 取消過再取消 → 回 False
    assert cd.cancel_event(eid) is False


def test_reschedule(tmp_calendar_db):
    cd = tmp_calendar_db
    today = date.today()
    eid = cd.insert_event(
        group_id="G1",
        title="聚餐",
        event_date=today.isoformat(),
        event_time="18:00",
    )
    new_date = (today + timedelta(days=5)).isoformat()
    assert cd.update_event_date(eid, new_date, "19:00") is True

    events = cd.list_upcoming("G1", days=30)
    assert events[0]["event_date"] == new_date
    assert events[0]["event_time"] == "19:00"
    # reschedule 應重置 reminded_at
    assert events[0]["reminded_at"] is None


def test_due_for_reminder_only_picks_7days_out(tmp_calendar_db):
    cd = tmp_calendar_db
    today = date.today()
    seven = (today + timedelta(days=7)).isoformat()
    eight = (today + timedelta(days=8)).isoformat()
    eid_7 = cd.insert_event(group_id="G1", title="A", event_date=seven)
    cd.insert_event(group_id="G1", title="B", event_date=eight)

    due = cd.list_due_for_reminder(days_ahead=7)
    assert len(due) == 1
    assert due[0]["event_id"] == eid_7

    cd.mark_reminded(eid_7)
    # mark 過後不再被掃到
    assert cd.list_due_for_reminder(days_ahead=7) == []


def test_extractor_normalize_clamps_bad_date():
    import calendar_extractor

    out = calendar_extractor._normalize(
        {
            "has_event": True,
            "is_cancellation": False,
            "title": "  聚餐  ",
            "date": "not-a-date",
            "time": "18:00",
            "location": None,
            "participants": ["媽媽", "", None, 123],
            "cancel_target_keyword": None,
        }
    )
    assert out["title"] == "聚餐"
    assert out["date"] is None
    assert out["time"] == "18:00"
    assert out["participants"] == ["媽媽", "123"]


def test_extractor_normalize_invalid_time():
    import calendar_extractor

    out = calendar_extractor._normalize(
        {"has_event": False, "is_cancellation": False, "time": "下午 6 點"}
    )
    assert out["time"] is None


def test_extractor_empty_text():
    import calendar_extractor

    out = calendar_extractor.extract("")
    assert out["has_event"] is False
    assert out["is_cancellation"] is False


def test_event_reminder_main_no_events(tmp_calendar_db, monkeypatch):
    import event_reminder

    importlib.reload(event_reminder)
    # 監聽 _push 不該被呼叫
    called = []
    monkeypatch.setattr(event_reminder, "_push", lambda text: called.append(text) or True)
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")

    rc = event_reminder.main()
    assert rc == 0
    assert called == []


def test_event_reminder_pushes_and_marks(tmp_calendar_db, monkeypatch):
    cd = tmp_calendar_db
    seven = (date.today() + timedelta(days=7)).isoformat()
    eid = cd.insert_event(
        group_id="G1",
        title="家族聚餐",
        event_date=seven,
        event_time="18:00",
        location="鼎泰豐",
        participants=["全家"],
    )

    import event_reminder

    importlib.reload(event_reminder)
    sent = []
    monkeypatch.setattr(event_reminder, "_push", lambda text: sent.append(text) or True)
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(event_reminder, "TOKEN", "dummy")

    rc = event_reminder.main()
    assert rc == 0
    assert len(sent) == 1
    assert "家族聚餐" in sent[0]
    assert "鼎泰豐" in sent[0]
    assert seven in sent[0]

    # 第二次跑：不該再推
    sent.clear()
    rc = event_reminder.main()
    assert rc == 0
    assert sent == []


def test_event_reminder_skips_cancelled(tmp_calendar_db, monkeypatch):
    cd = tmp_calendar_db
    seven = (date.today() + timedelta(days=7)).isoformat()
    eid = cd.insert_event(group_id="G1", title="X", event_date=seven)
    cd.cancel_event(eid)

    import event_reminder

    importlib.reload(event_reminder)
    sent = []
    monkeypatch.setattr(event_reminder, "_push", lambda text: sent.append(text) or True)
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(event_reminder, "TOKEN", "dummy")

    rc = event_reminder.main()
    assert rc == 0
    assert sent == []
