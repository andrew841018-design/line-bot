"""Prove-It (2026-11-13): the TODO and anniversary sweeps never see a family message.

memory.log_raw_message stores raw_messages.created_at in seconds (strftime('%s','now')).  The
weekly TODO sweep (todo.py extract) and the monthly anniversary sweep (anniversary.py extract)
compared that column with a millisecond cutoff, so they matched 0 rows on every run.  The
production DB, counted read-only on 11/12 and 11/13, had 0 todos and 0 anniversaries.

Both tests go through the launchd entry points with synthetic messages only."""

import os
import time

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

GROUP = "GRP_FAMILY"


@pytest.fixture
def extractor_dbs(tmp_path, monkeypatch):
    import anniversary
    import todo

    monkeypatch.setattr(todo, "_DB_PATH", tmp_path / "todo.db")
    monkeypatch.setattr(anniversary, "_DB_PATH", tmp_path / "anniversary.db")
    monkeypatch.setenv("ALLOWED_GROUP_IDS", GROUP)
    todo.init_db()
    anniversary.init_db()


def _log(message_id: str, user_id: str, text: str) -> None:
    import memory

    memory.log_raw_message(GROUP, message_id, user_id, text, index_for_recall=False)


def test_the_weekly_todo_sweep_sees_a_message_logged_today(extractor_dbs, monkeypatch):
    import todo

    _log("m_today", "user-a", "我下週三要交報告")
    seen: list[str] = []

    def extract(text, today_iso=None):
        seen.append(text)
        return [{"task": "交報告", "due_date": "2026-11-19", "sender_hint": "我"}]

    monkeypatch.setattr(todo, "extract_from_text", extract)
    assert todo.main_extractor_sweep() == 0
    assert seen == ["我下週三要交報告"]
    assert [t["task"] for t in todo.list_pending(GROUP)] == ["交報告"]


def test_the_monthly_anniversary_sweep_sees_a_message_logged_today(extractor_dbs, monkeypatch):
    import anniversary

    _log("m_today", "user-a", "成員甲 3/5 生日")
    seen: list[str] = []

    def extract(text):
        seen.append(text)
        return [{"person": "成員甲", "anniversary_type": "birthday", "month": 3, "day": 5}]

    monkeypatch.setattr(anniversary, "extract_from_text", extract)
    assert anniversary.main_extractor() == 0
    assert seen == ["成員甲 3/5 生日"]
    assert [a["person"] for a in anniversary.get_anniversaries_on(GROUP, 3, 5)] == ["成員甲"]


def test_the_stored_time_really_is_seconds(extractor_dbs):
    """The writer's unit, pinned: a row logged now reads back within a minute of time.time()."""
    import memory

    _log("m_now", "user-a", "現在這一則")
    with memory._conn() as c:
        (created_at,) = c.execute(
            "SELECT created_at FROM raw_messages WHERE group_id=? AND message_id=?", (GROUP, "m_now")
        ).fetchone()
    assert abs(int(created_at) - time.time()) < 60
