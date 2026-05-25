"""Feature E tests — TODO extractor + reminder (Andrew 2026-05-25)."""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")


@pytest.fixture
def temp_todo_db(monkeypatch):
    import todo
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    monkeypatch.setattr(todo, "_DB_PATH", tmp_path)
    todo.init_db()
    yield tmp_path
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def test_init_db_creates_table(temp_todo_db):
    import sqlite3
    with sqlite3.connect(temp_todo_db) as c:
        tables = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
    assert "todos" in tables


def test_insert_and_list_due_today(temp_todo_db):
    import todo
    today_iso = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    tid = todo.insert_todo(
        group_id="GRP", task="交報告", sender_user_id="U1", due_date=today_iso,
    )
    assert tid
    due = todo.list_due_today("GRP")
    assert len(due) == 1
    assert due[0]["task"] == "交報告"


def test_insert_empty_task_returns_empty():
    import todo
    assert not todo.insert_todo("GRP", "")
    assert not todo.insert_todo("GRP", "   ")


def test_insert_dedup(temp_todo_db):
    import todo
    a = todo.insert_todo("GRP", "task1", "U1", "2026-06-04")
    b = todo.insert_todo("GRP", "task1", "U1", "2026-06-04")
    assert a
    assert not b


def test_complete_todo(temp_todo_db):
    import todo
    tid = todo.insert_todo("GRP", "task", "U1", "2026-06-04")
    assert todo.complete_todo(tid) is True
    # 已 completed 再 complete → False
    assert todo.complete_todo(tid) is False


def test_cancel_todo(temp_todo_db):
    import todo
    tid = todo.insert_todo("GRP", "task", "U1", "2026-06-04")
    assert todo.cancel_todo(tid) is True
    assert todo.cancel_todo(tid) is False


def test_list_overdue(temp_todo_db):
    import todo
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    future = (today + timedelta(days=1)).isoformat()
    todo.insert_todo("GRP", "overdue1", "U1", yesterday)
    todo.insert_todo("GRP", "future1", "U1", future)
    overdue = todo.list_overdue("GRP")
    assert len(overdue) == 1
    assert overdue[0]["task"] == "overdue1"


def test_list_overdue_skips_very_old(temp_todo_db):
    """Overdue 超過 MAX_OVERDUE_DAYS_TO_REMIND 不再 reminder."""
    import todo
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    very_old = (today - timedelta(days=14)).isoformat()
    todo.insert_todo("GRP", "very_old", "U1", very_old)
    overdue = todo.list_overdue("GRP")
    assert not any(t["task"] == "very_old" for t in overdue)


def test_extract_from_text_empty():
    import todo
    assert todo.extract_from_text("") == []


def test_push_daily_reminders_skips_when_no_todos(temp_todo_db):
    import todo
    with patch.object(todo, "_push") as mock_push:
        n = todo.push_daily_reminders("GRP_EMPTY")
    assert n == 0
    assert not mock_push.called


def test_push_daily_reminders_sends_when_due(temp_todo_db):
    import todo
    today_iso = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    tid = todo.insert_todo("GRP", "today task", "U1", today_iso)
    assert tid

    with patch.object(todo, "GROUP_ID", "GRP"), \
         patch.object(todo, "_push", return_value=True) as mock_push:
        n = todo.push_daily_reminders("GRP")
    assert n >= 1
    assert mock_push.called


def test_main_reminder_requires_group_id():
    import todo
    with patch.object(todo, "GROUP_ID", ""):
        assert todo.main_reminder() == 1


def test_main_extractor_requires_group_id():
    import todo
    with patch.object(todo, "GROUP_ID", ""):
        assert todo.main_extractor_sweep() == 1
