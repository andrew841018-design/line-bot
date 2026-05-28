"""T-3 / T-2 / T-1 reminder tests — codex critical 都要驗到。"""

from __future__ import annotations

import importlib
from datetime import date, timedelta

import pytest


@pytest.fixture
def tmp_cal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_reminder.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_file))
    import config

    importlib.reload(config)
    config.settings.sqlite_path = str(db_file)
    import calendar_db

    importlib.reload(calendar_db)
    return calendar_db


# ── Test: Migration adds reminded_3d/2d/1d (idempotent) ─────────────────
def test_migration_adds_reminded_columns(tmp_cal_db):
    """init_db 重複跑後 reminded_3d/2d/1d 三欄都在。"""
    tmp_cal_db.init_db()
    tmp_cal_db.init_db()
    with tmp_cal_db._conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
    for off in (30, 7, 3, 2, 1):
        assert f"reminded_{off}d" in cols, f"reminded_{off}d missing"


# ── Test: REMINDER_OFFSETS constant ───────────────────────────────────
def test_reminder_offsets_constant(tmp_cal_db):
    assert tmp_cal_db.REMINDER_OFFSETS == (30, 7, 3, 2, 1)


# ── Test: _reminded_column whitelist (SQL injection guard) ────────────
def test_reminded_column_whitelist(tmp_cal_db):
    """合法 offset 回欄位名，非法 raise（防 SQL injection via column name）。"""
    assert tmp_cal_db._reminded_column(3) == "reminded_3d"
    assert tmp_cal_db._reminded_column(1) == "reminded_1d"
    with pytest.raises(ValueError):
        tmp_cal_db._reminded_column(0)
    with pytest.raises(ValueError):
        tmp_cal_db._reminded_column(4)  # 4 not in REMINDER_OFFSETS
    with pytest.raises(ValueError):
        # 試攻擊：注入 SQL — whitelist 必須擋
        tmp_cal_db._reminded_column(99)


# ── Test: list_due_for_reminder(offset) 抓 T+offset ─────────────────────
def test_list_due_for_offset_1(tmp_cal_db):
    """today + 1 的 active event 且 reminded_1d IS NULL 被抓到。"""
    GID = "G1"
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(
        group_id=GID, title="拿蛋糕", event_date=tomorrow
    )
    assert eid

    due = tmp_cal_db.list_due_for_reminder(GID, days_ahead=1)
    assert len(due) == 1
    assert due[0]["event_id"] == eid


def test_list_due_for_offset_3(tmp_cal_db):
    GID = "G1"
    today = date.today()
    t3 = (today + timedelta(days=3)).isoformat()
    eid = tmp_cal_db.insert_event(group_id=GID, title="3 天後活動", event_date=t3)
    due = tmp_cal_db.list_due_for_reminder(GID, days_ahead=3)
    assert len(due) == 1
    assert due[0]["event_id"] == eid


def test_list_due_does_not_match_wrong_offset(tmp_cal_db):
    """T+1 event 不會出現在 T+3 list。"""
    GID = "G1"
    today = date.today()
    t1 = (today + timedelta(days=1)).isoformat()
    tmp_cal_db.insert_event(group_id=GID, title="明天活動", event_date=t1)

    due_3 = tmp_cal_db.list_due_for_reminder(GID, days_ahead=3)
    assert due_3 == []


# ── Test: mark_reminded(event_id, offset) 寫對欄位 + Idempotency invariant
def test_mark_reminded_writes_correct_column(tmp_cal_db):
    GID = "G1"
    today = date.today()
    t1 = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(group_id=GID, title="蛋糕", event_date=t1)

    tmp_cal_db.mark_reminded(eid, days_ahead=1, group_id=GID)
    with tmp_cal_db._conn() as c:
        row = c.execute(
            "SELECT reminded_1d, reminded_2d, reminded_3d FROM events WHERE event_id=?",
            (eid,),
        ).fetchone()
    assert row[0] is not None  # reminded_1d 有寫
    assert row[1] is None
    assert row[2] is None


def test_mark_reminded_idempotency_per_offset(tmp_cal_db):
    """mark 過後 list_due 同 offset 不再回該 event（idempotency invariant）。"""
    GID = "G1"
    today = date.today()
    t1 = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(group_id=GID, title="蛋糕", event_date=t1)

    # 第一次 list_due — 抓到
    due1 = tmp_cal_db.list_due_for_reminder(GID, days_ahead=1)
    assert len(due1) == 1

    # mark 過後
    tmp_cal_db.mark_reminded(eid, days_ahead=1, group_id=GID)

    # 第二次 list_due 同 offset — 抓不到（已 mark）
    due2 = tmp_cal_db.list_due_for_reminder(GID, days_ahead=1)
    assert due2 == []


def test_mark_per_offset_independent(tmp_cal_db):
    """mark T-3 不影響 T-2/T-1 query。"""
    GID = "G1"
    today = date.today()
    # 同一 event 同時 T-3 / T-2 / T-1 可推（假設多日活動或 reschedule）— 但 event 只一個日期
    # 改驗：T-3 event 推完 T-3 後，DB row 的 reminded_3d 有值但 reminded_1d/2d 仍 NULL
    t3 = (today + timedelta(days=3)).isoformat()
    eid = tmp_cal_db.insert_event(group_id=GID, title="A", event_date=t3)

    tmp_cal_db.mark_reminded(eid, days_ahead=3, group_id=GID)
    with tmp_cal_db._conn() as c:
        r = c.execute(
            "SELECT reminded_1d, reminded_2d, reminded_3d FROM events WHERE event_id=?",
            (eid,),
        ).fetchone()
    assert r[0] is None and r[1] is None and r[2] is not None


# ── Test: update_event_date resets ALL reminded columns (codex critical #3) ──
def test_update_event_date_resets_reminders(tmp_cal_db):
    """Reschedule 後 reminded_3d/2d/1d 都歸零，下次 launchd 會重推。"""
    GID = "G1"
    today = date.today()
    t1 = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(group_id=GID, title="A", event_date=t1)
    tmp_cal_db.mark_reminded(eid, days_ahead=1, group_id=GID)

    # Reschedule 到 T+5
    new_date = (today + timedelta(days=5)).isoformat()
    assert tmp_cal_db.update_event_date(eid, new_date) is True

    with tmp_cal_db._conn() as c:
        r = c.execute(
            "SELECT reminded_1d, reminded_2d, reminded_3d, reminded_at FROM events "
            "WHERE event_id=?",
            (eid,),
        ).fetchone()
    assert r[0] is None and r[1] is None and r[2] is None and r[3] is None


# ── Test: cancelled event 不會出現在 list_due ────────────────────────────
def test_cancelled_excluded_from_due(tmp_cal_db):
    GID = "G1"
    today = date.today()
    t1 = (today + timedelta(days=1)).isoformat()
    eid = tmp_cal_db.insert_event(group_id=GID, title="A", event_date=t1)
    tmp_cal_db.cancel_event(eid)

    due = tmp_cal_db.list_due_for_reminder(GID, days_ahead=1)
    assert due == []


# ── Test: event_reminder.main() 跑兩次 idempotent ────────────────────────
def test_main_run_twice_same_day_idempotent(tmp_cal_db, monkeypatch):
    """main() 跑兩次相同 input → 第二次 push count = 0 (idempotency invariant)。"""
    import event_reminder

    GID = "G1"
    monkeypatch.setattr(event_reminder, "GROUP_ID", GID)
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "fake_token")

    push_count = {"n": 0}

    def fake_push(text):
        push_count["n"] += 1
        return True

    monkeypatch.setattr(event_reminder, "_push", fake_push)
    monkeypatch.setattr(event_reminder.calendar_db, "REMINDER_OFFSETS", tmp_cal_db.REMINDER_OFFSETS)
    monkeypatch.setattr(event_reminder, "calendar_db", tmp_cal_db)

    today = date.today()
    tmp_cal_db.insert_event(
        group_id=GID, title="蛋糕", event_date=(today + timedelta(days=1)).isoformat()
    )
    tmp_cal_db.insert_event(
        group_id=GID, title="聚餐", event_date=(today + timedelta(days=2)).isoformat()
    )

    # 第一次 run
    rc = event_reminder.main()
    assert rc == 0
    first_pushes = push_count["n"]
    assert first_pushes == 2  # T-1 蛋糕 + T-2 聚餐

    # 第二次 run（同一天）
    rc = event_reminder.main()
    assert rc == 0
    assert push_count["n"] == first_pushes  # 沒新增 push


# ── Test: _format_event offset label (3/後天/明天) ─────────────────────
def test_format_event_offset_labels():
    import event_reminder

    e = {
        "event_id": "x",
        "event_date": "2026-05-22",
        "event_time": "14:00",
        "title": "拿蛋糕",
        "location": "喜來登",
        "participants": "[]",
    }
    assert "明天活動提醒" in event_reminder._format_event(e, 1)
    assert "後天活動提醒" in event_reminder._format_event(e, 2)
    assert "3 天後活動提醒" in event_reminder._format_event(e, 3)


# ── Test: push 失敗時不 mark (at-least-once 保證) ────────────────────────
def test_main_push_failure_does_not_mark(tmp_cal_db, monkeypatch):
    import event_reminder

    GID = "G1"
    monkeypatch.setattr(event_reminder, "GROUP_ID", GID)
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "fake")
    monkeypatch.setattr(event_reminder, "_push", lambda text: False)  # always fail
    monkeypatch.setattr(event_reminder, "calendar_db", tmp_cal_db)

    today = date.today()
    eid = tmp_cal_db.insert_event(
        group_id=GID, title="蛋糕",
        event_date=(today + timedelta(days=1)).isoformat(),
    )

    event_reminder.main()

    # reminded_1d 仍 NULL（push 失敗不 mark），下次跑會 retry
    with tmp_cal_db._conn() as c:
        r = c.execute(
            "SELECT reminded_1d FROM events WHERE event_id=?", (eid,)
        ).fetchone()
    assert r[0] is None
