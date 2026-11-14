"""Prove-It (2026-11-13): the TODO and anniversary jobs never saw a family message.

memory.log_raw_message stores raw_messages.created_at in seconds (strftime('%s','now')).  The weekly
TODO sweep (todo.py extract) and the monthly anniversary sweep (anniversary.py extract) compared that
column with a millisecond cutoff, so they matched 0 rows on every run.  The production DB, counted
read-only on 11/12 and 11/13, had 0 todos and 0 anniversaries.

Andrew chose 「B：兩個都開」: both now read through family_extract.  These tests go through the launchd
entry points with a message said yesterday at 20:00, run at today 08:00 (Taipei), the model faked at
its one seam.  Synthetic messages only."""

import json
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

GROUP = "GRP_FAMILY"
TW = ZoneInfo("Asia/Taipei")


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    import anniversary
    import config
    import line_push_client
    import todo

    monkeypatch.setattr(todo, "_DB_PATH", tmp_path / "todo.db")
    monkeypatch.setattr(anniversary, "_DB_PATH", tmp_path / "anniversary.db")
    monkeypatch.setenv("ALLOWED_GROUP_IDS", GROUP)
    monkeypatch.delenv("LINE_BOT_TODO_MODE", raising=False)
    monkeypatch.delenv("LINE_BOT_ANNIVERSARY_EXTRACT", raising=False)
    # every settings object the jobs may hold: test_main_calendar_query reloads config, so the module copies
    # can differ from config.settings (suite-order dependence found 11/14)
    for holder in {id(s): s for s in (config.settings, todo.settings, anniversary.settings)}.values():
        monkeypatch.setattr(holder, "family_group_id", GROUP)
    aliases = tmp_path / "user_aliases.json"
    aliases.write_text(json.dumps({"user-a": "成員甲"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    todo.init_db()
    anniversary.init_db()


@pytest.fixture
def model(monkeypatch):
    import gemini_client

    calls: dict = {"prompts": [], "answers": []}

    def side_task(prompt, config, *, label):
        calls["prompts"].append(prompt)
        items = calls["answers"].pop(0) if calls["answers"] else []
        return SimpleNamespace(
            text=json.dumps(items, ensure_ascii=False),
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        )

    monkeypatch.setattr(gemini_client, "side_task_last_tier", side_task)
    monkeypatch.setattr(gemini_client, "side_task_last_tier_ready", lambda: True)
    return calls


def _today_0800() -> datetime:
    return datetime.now(TW).replace(hour=8, minute=0, second=0, microsecond=0)


def _log_yesterday_evening(message_id: str, text: str) -> None:
    """Logged by the real writer, then moved to yesterday 20:00 (the writer stamps 'now')."""
    import memory

    memory.log_raw_message(GROUP, message_id, "user-a", text, index_for_recall=False)
    said = _today_0800() - timedelta(hours=12)
    with memory._conn() as c:
        c.execute("UPDATE raw_messages SET created_at=? WHERE message_id=?", (int(said.timestamp()), message_id))


def test_the_daily_todo_job_reads_yesterdays_promise_and_pushes_it_today(jobs, model, monkeypatch):
    import todo

    _log_yesterday_evening("m_todo", "我明天要交報告")
    today = _today_0800().date()
    model["answers"].append([{"i": 1, "task": "交報告", "due_date": today.isoformat()}])
    pushed: list[str] = []
    monkeypatch.setattr(
        todo, "_push", lambda group_id, text, targets=(), *, retry_key: pushed.append(text) or True
    )

    assert todo.main_reminder(now=_today_0800().timestamp()) == 0

    assert len(model["prompts"]) == 1 and "我明天要交報告" in model["prompts"][0]
    assert len(pushed) == 1 and "交報告" in pushed[0]


def test_the_anniversary_job_reads_the_message_into_a_candidate_it_does_not_push(jobs, model):
    import anniversary

    _log_yesterday_evening("m_anniv", "12/6 是成員甲生日")
    model["answers"].append(
        [{"i": 1, "person": "成員甲", "anniversary_type": "birthday", "month": 12, "day": 6, "lunar": False}]
    )

    assert anniversary.main_extractor(now=_today_0800().timestamp()) == 0

    assert len(model["prompts"]) == 1 and "成員甲生日" in model["prompts"][0]
    assert [r["status"] for r in anniversary.review_rows()["candidates"]] == ["pending"]
    assert anniversary.get_anniversaries_on(GROUP, 12, 6) == []


def test_the_stored_time_really_is_seconds(jobs):
    """The writer's unit, pinned: a row logged now reads back within a minute of time.time()."""
    import memory

    memory.log_raw_message(GROUP, "m_now", "user-a", "現在這一則", index_for_recall=False)
    with memory._conn() as c:
        (created_at,) = c.execute(
            "SELECT created_at FROM raw_messages WHERE group_id=? AND message_id=?", (GROUP, "m_now")
        ).fetchone()
    assert abs(int(created_at) - time.time()) < 60
