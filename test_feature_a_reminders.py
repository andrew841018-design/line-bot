"""Feature A regression tests — multi-tier reminder (30/7/3/2/1) for events.

Andrew 2026-05-25 directive: 重要對話自動 multi-tier scheduled reminder.
擴 REMINDER_OFFSETS 從 (3, 2, 1) → (30, 7, 3, 2, 1)。
"""

import os
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")


@pytest.fixture
def temp_db(monkeypatch):
    """Isolate calendar_db._DB_PATH to a temp SQLite file."""
    import calendar_db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    monkeypatch.setattr(calendar_db, "_DB_PATH", tmp_path)
    calendar_db.init_db()
    yield tmp_path
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


def test_reminder_offsets_includes_30_and_7():
    """REMINDER_OFFSETS 必須含 30 (1-month) + 7 (1-week) tier."""
    import calendar_db
    assert 30 in calendar_db.REMINDER_OFFSETS, (
        f"missing 30-day tier: {calendar_db.REMINDER_OFFSETS}"
    )
    assert 7 in calendar_db.REMINDER_OFFSETS, (
        f"missing 7-day tier: {calendar_db.REMINDER_OFFSETS}"
    )


def test_30day_reminder_lifecycle(temp_db):
    """Insert event 30 天後 → list_due(30) returns it → mark → no longer due."""
    import calendar_db
    target_date = (date.today() + timedelta(days=30)).isoformat()
    eid = calendar_db.insert_event(
        group_id="GRP_TEST",
        title="媽媽 30 天後出國",
        event_date=target_date,
        event_type="personal_trip",
    )
    assert eid, "insert_event 應成功"

    due_before = calendar_db.list_due_for_reminder("GRP_TEST", days_ahead=30)
    assert any(e["event_id"] == eid for e in due_before), (
        "30 天後 event 應出現在 list_due_for_reminder(30)"
    )

    calendar_db.mark_reminded(eid, days_ahead=30, group_id="GRP_TEST")
    due_after = calendar_db.list_due_for_reminder("GRP_TEST", days_ahead=30)
    assert not any(e["event_id"] == eid for e in due_after), (
        "mark_reminded(30) 後不應再出現"
    )


def test_7day_reminder_lifecycle(temp_db):
    """Same lifecycle for 7-day reminder."""
    import calendar_db
    target_date = (date.today() + timedelta(days=7)).isoformat()
    eid = calendar_db.insert_event(
        group_id="GRP_TEST",
        title="爸爸 7 天後生日聚餐",
        event_date=target_date,
    )
    assert eid

    due_before = calendar_db.list_due_for_reminder("GRP_TEST", days_ahead=7)
    assert any(e["event_id"] == eid for e in due_before)

    calendar_db.mark_reminded(eid, days_ahead=7, group_id="GRP_TEST")
    due_after = calendar_db.list_due_for_reminder("GRP_TEST", days_ahead=7)
    assert not any(e["event_id"] == eid for e in due_after)


def test_update_event_date_resets_30d_and_7d_reminded(temp_db):
    """Rescheduled event must reset reminded_30d + reminded_7d (codex critical pattern)."""
    import calendar_db
    initial_date = (date.today() + timedelta(days=30)).isoformat()
    eid = calendar_db.insert_event(
        group_id="GRP_TEST",
        title="再排程 event",
        event_date=initial_date,
    )

    for offset in calendar_db.REMINDER_OFFSETS:
        calendar_db.mark_reminded(eid, days_ahead=offset, group_id="GRP_TEST")

    new_date = (date.today() + timedelta(days=45)).isoformat()
    ok = calendar_db.update_event_date(eid, new_date)
    assert ok

    with sqlite3.connect(temp_db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM events WHERE event_id = ?", (eid,)
        ).fetchone()
    for offset in calendar_db.REMINDER_OFFSETS:
        col = f"reminded_{offset}d"
        assert row[col] is None, (
            f"After reschedule, {col} 應 reset 為 NULL (got {row[col]})"
        )
