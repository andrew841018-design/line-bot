"""calendar_db / calendar_extractor / event_reminder 測試。

DB 用 monkeypatch 換成 tmp_path 下的 sqlite，避免污染 line_bot.db。
extractor / reminder 都用 mock 不打 Gemini / LINE API。
"""

from __future__ import annotations

import importlib
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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

    due = cd.list_due_for_reminder("G1", days_ahead=7)
    assert len(due) == 1
    assert due[0]["event_id"] == eid_7

    cd.mark_reminded(eid_7, days_ahead=7, group_id="G1")
    # mark 過後不再被掃到
    assert cd.list_due_for_reminder("G1", days_ahead=7) == []


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
    # 監聽 send 不該被呼叫
    called = []
    monkeypatch.setattr(
        event_reminder,
        "_send_reminder_message_spec",
        lambda spec, **_kw: called.append(spec["text"]) or event_reminder.POST_OK,
    )
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")

    rc = event_reminder.main()
    assert rc == 0
    assert called == []


def test_event_reminder_pushes_and_marks(tmp_calendar_db, monkeypatch):
    """T-3/T-2/T-1 reminders (2026-05-21 user directive)。"""
    cd = tmp_calendar_db
    one = (date.today() + timedelta(days=1)).isoformat()
    cd.insert_event(
        group_id="G1",
        title="家族聚餐",
        event_date=one,
        event_time="18:00",
        location="鼎泰豐",
        participants=["全家"],
    )

    import event_reminder

    importlib.reload(event_reminder)
    sent = []
    monkeypatch.setattr(
        event_reminder,
        "_send_reminder_message_spec",
        lambda spec, **_kw: sent.append(spec["text"]) or event_reminder.POST_OK,
    )
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "dummy")

    rc = event_reminder.main()
    assert rc == 0
    assert len(sent) == 1
    assert "家族聚餐" in sent[0]
    assert "鼎泰豐" in sent[0]
    assert "明天" in sent[0]  # T-1 label
    assert one in sent[0]

    # 第二次跑：不該再推（idempotency invariant）
    sent.clear()
    rc = event_reminder.main()
    assert rc == 0
    assert sent == []


def _to_calendar_remind_ts(event_date: str, event_time: str | None = None) -> int:
    hhmm = event_time or "00:00"
    y, m, d = (int(part) for part in event_date.split("-"))
    h, minute = (int(x) for x in hhmm.split(":"))
    return int(datetime(y, m, d, h, minute, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())


def _month_day_label(event_date: str) -> str:
    _, month_s, day_s = event_date.split("-")
    return f"{int(month_s)}/{int(day_s)}"


def _align_memory_db_with_calendar(calendar_db_module) -> None:
    import memory

    memory._DB_PATH = calendar_db_module._DB_PATH
    memory._init_db()


def test_insert_event_syncs_to_reminder(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=3)).isoformat()
    event_id = cd.insert_event(
        group_id="G1",
        title="爸爸健檢",
        event_date=event_date,
        event_time="08:00",
        location="榮總",
    )
    assert event_id

    reminders = memory.list_pending_reminders("G1")
    assert len(reminders) == 1
    assert reminders[0]["source_kind"] == "calendar_event"
    assert reminders[0]["source_ref"] == event_id
    assert reminders[0]["action"] == "爸爸健檢"
    assert reminders[0]["remind_at"] == _to_calendar_remind_ts(event_date, "08:00")


def test_event_sync_removes_duplicate_generic_reminder(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=1)).isoformat()
    remind_at = _to_calendar_remind_ts(event_date, "20:00")
    generic_id = memory.add_reminder(
        "G1",
        "U_GENERIC",
        "全家 南港運動中心打壁球",
        remind_at,
        source_text="咪寶，幫我記得：\n7/4 全家 南港運動中心 20:00~21:00打壁球",
        mention_aliases=["媽媽"],
    )
    assert generic_id
    with memory._conn() as c:
        c.execute(
            "UPDATE reminders SET last_pushed_at=?, weekly_count=?, "
            "last_weekly_at=?, pushed_1d=? WHERE reminder_id=?",
            (123, 2, 122, 1, generic_id),
        )

    event_id = cd.insert_event(
        group_id="G1",
        title="全家 南港運動中心打壁球",
        event_date=event_date,
        event_time="20:00",
        participants=["全家"],
    )
    assert event_id

    reminders = memory.list_pending_reminders_full("G1")
    assert len(reminders) == 1
    assert reminders[0]["source_kind"] == "calendar_event"
    assert reminders[0]["source_ref"] == event_id
    assert reminders[0]["action"] == "全家 南港運動中心打壁球"
    assert reminders[0]["remind_at"] == remind_at
    assert reminders[0]["mention_aliases"] == ["全家", "媽媽"]
    assert "咪寶" not in reminders[0]["source_text"]
    assert reminders[0]["last_pushed_at"] == 123
    assert reminders[0]["weekly_count"] == 2
    assert reminders[0]["last_weekly_at"] == 122
    assert reminders[0]["pushed_1d"] == 1


def test_duplicate_cleanup_keeps_distinct_same_time_reminders(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=1)).isoformat()
    remind_at = _to_calendar_remind_ts(event_date, "18:00")
    assert memory.add_reminder("G1", "U1", "買牛奶", remind_at)
    assert memory.add_reminder("G1", "U1", "成功高中活動", remind_at)

    deleted = memory.delete_duplicate_pending_reminders("G1")

    reminders = memory.list_pending_reminders("G1")
    assert deleted == 0
    assert [r["action"] for r in reminders] == ["買牛奶", "成功高中活動"]


def test_duplicate_cleanup_keeps_lowest_id_for_same_priority(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=1)).isoformat()
    remind_at = _to_calendar_remind_ts(event_date, "18:00")
    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, "
            "status, source_kind, source_ref, source_text, mention_aliases) "
            "VALUES (?, ?, ?, ?, ?, 'pending', '', '', ?, ?)",
            ("G1", "U1", "同優先級提醒", remind_at, 10, "first", "[]"),
        )
        first_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, "
            "status, source_kind, source_ref, source_text, mention_aliases) "
            "VALUES (?, ?, ?, ?, ?, 'pending', '', '', ?, ?)",
            ("G1", "U2", "同優先級提醒", remind_at, 20, "second", "[\"爸爸\"]"),
        )
        second_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    assert first_id < second_id

    deleted = memory.delete_duplicate_pending_reminders("G1")

    reminders = memory.list_pending_reminders("G1")
    assert deleted == 1
    assert len(reminders) == 1
    assert reminders[0]["reminder_id"] == first_id
    assert reminders[0]["source_text"] == "first\n---\nsecond"
    assert reminders[0]["mention_aliases"] == ["爸爸"]


def test_badminton_event_syncs_to_booking_reminder_on_booking_window(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=10)).isoformat()
    event_id = cd.insert_event(
        group_id="G1",
        title="全家打羽球",
        event_date=event_date,
        event_time="16:00",
        participants=["黃聖雅"],
    )
    assert event_id

    reminders = memory.list_pending_reminders("G1")
    assert len(reminders) == 1
    expected_at = _to_calendar_remind_ts(event_date, "18:00") - 7 * 86400
    assert reminders[0]["source_kind"] == "calendar_event"
    assert reminders[0]["source_ref"] == event_id
    assert reminders[0]["remind_at"] == expected_at
    assert reminders[0]["action"] == f"黃聖雅負責預約{_month_day_label(event_date)}打羽球場地"
    assert reminders[0]["mention_aliases"] == ["黃聖雅"]

    cd.sync_active_events_to_reminders("G1")
    resynced = memory.list_pending_reminders("G1")
    assert len(resynced) == 1
    assert resynced[0]["remind_at"] == expected_at


def test_badminton_event_can_override_booking_lead_days(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=10)).isoformat()
    event_id = cd.insert_event(
        group_id="G1",
        title="全家打羽球",
        event_date=event_date,
        event_time="16:00",
        participants=["黃聖雅"],
    )
    assert event_id

    assert cd.update_event_reminder_lead_days(event_id, 5) is True

    expected_at = _to_calendar_remind_ts(event_date, "18:00") - 5 * 86400
    reminders = memory.list_pending_reminders("G1")
    assert len(reminders) == 1
    assert reminders[0]["source_ref"] == event_id
    assert reminders[0]["remind_at"] == expected_at
    assert reminders[0]["action"] == f"黃聖雅負責預約{_month_day_label(event_date)}打羽球場地"

    cd.sync_active_events_to_reminders("G1")
    resynced = memory.list_pending_reminders("G1")
    assert len(resynced) == 1
    assert resynced[0]["remind_at"] == expected_at


def test_badminton_event_with_zero_lead_uses_event_reminder_text(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=1)).isoformat()
    event_id = cd.insert_event(
        group_id="G1",
        title="成功高中打羽球",
        event_date=event_date,
        event_time="18:00",
        location="成功高中",
        participants=["全家"],
    )
    assert event_id

    assert cd.update_event_reminder_lead_days(event_id, 0) is True

    reminders = memory.list_pending_reminders("G1")
    assert len(reminders) == 1
    assert reminders[0]["source_ref"] == event_id
    assert reminders[0]["action"] == "成功高中打羽球"
    assert reminders[0]["remind_at"] == _to_calendar_remind_ts(event_date, "18:00")
    assert "預約" not in reminders[0]["action"]
    assert "地點：成功高中" in reminders[0]["source_text"]
    assert reminders[0]["mention_aliases"] == ["全家"]


def test_badminton_event_with_family_marker_keeps_all_mention(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=10)).isoformat()
    event_id = cd.insert_event(
        group_id="G1",
        title="全家打羽球",
        event_date=event_date,
        event_time="18:00",
        participants=["全家"],
    )
    assert event_id

    reminders = memory.list_pending_reminders("G1")
    assert len(reminders) == 1
    assert reminders[0]["action"] == f"預約{_month_day_label(event_date)}打羽球場地"
    assert reminders[0]["mention_aliases"] == ["全家"]


def test_update_event_syncs_reminder_time(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    today = date.today()
    event_id = cd.insert_event(
        group_id="G1",
        title="聚餐改期",
        event_date=today.isoformat(),
        event_time="18:00",
    )
    initial = memory.list_pending_reminders("G1")
    assert len(initial) == 1
    assert initial[0]["remind_at"] == _to_calendar_remind_ts(today.isoformat(), "18:00")

    new_date = (today + timedelta(days=4)).isoformat()
    assert cd.update_event_date(event_id, new_date, "19:00") is True

    reminders = memory.list_pending_reminders("G1")
    assert len(reminders) == 1
    assert reminders[0]["remind_at"] == _to_calendar_remind_ts(new_date, "19:00")


def test_cancel_event_marks_reminder_done(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=1)).isoformat()
    event_id = cd.insert_event(
        group_id="G1",
        title="取消同步測試",
        event_date=event_date,
    )
    assert memory.list_pending_reminders("G1"), "insert_event 應同步 pending reminder"

    assert cd.cancel_event(event_id) is True
    assert memory.list_pending_reminders("G1") == []


def test_sync_active_events_to_reminders_backfills_existing(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    eid = "seeded-event-id"
    event_date = (date.today() + timedelta(days=2)).isoformat()
    with cd._conn() as c:
        c.execute(
            "INSERT INTO events (event_id, group_id, title, event_date, event_time, "
            "location, participants, status, created_at, event_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 'family_gathering')",
            (
                eid,
                "G1",
                "回家聚餐",
                event_date,
                "17:30",
                "社區",
                "[\"媽媽\"]",
                int(time.time()),
            ),
        )

    assert memory.list_pending_reminders("G1") == []

    synced = cd.sync_active_events_to_reminders("G1")
    reminders = memory.list_pending_reminders("G1")
    assert synced >= 1
    assert len(reminders) >= 1
    assert any(
        r["source_kind"] == "calendar_event" and r["source_ref"] == eid
        for r in reminders
    )


def test_sync_active_events_to_reminders_removes_stale(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    event_date = (date.today() + timedelta(days=2)).isoformat()
    eid = cd.insert_event(
        group_id="G1",
        title="持續保留",
        event_date=event_date,
        event_time="09:00",
        location="機場",
    )
    assert eid

    memory.upsert_reminder_for_source(
        group_id="G1",
        user_id="",
        action="已過期提醒",
        remind_at=int(time.time()) + 3600,
        source_kind=cd.EVENT_REMINDER_SOURCE_KIND,
        source_ref="orphan-event",
        source_text="請刪掉我",
    )
    assert any(
        r["source_ref"] == "orphan-event" and r["source_kind"] == "calendar_event"
        for r in memory.list_pending_reminders("G1")
    )

    cd.sync_active_events_to_reminders("G1")
    assert all(
        not (
            r["source_kind"] == "calendar_event"
            and r["source_ref"] == "orphan-event"
        )
        for r in memory.list_pending_reminders("G1")
    )
    assert any(
        r["source_ref"] == eid and r["source_kind"] == "calendar_event"
        for r in memory.list_pending_reminders("G1")
    )


def test_event_reminder_payload_includes_source_msg_reference(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    raw_message_id = "msg-source-001"
    raw_text = (
        "高鐵接送：到機場前請點 https://example.com/pickup "
        "驗證碼: ABCD1234"
    )
    memory.log_raw_message("G1", raw_message_id, "U_MOM", raw_text)

    event_date = (date.today() + timedelta(days=3)).isoformat()
    eid = cd.insert_event(
        group_id="G1",
        title="接爸爸",
        event_date=event_date,
        event_time="10:30",
        location="台北松山機場",
        source_msg_id=raw_message_id,
    )
    assert eid

    reminders = memory.list_pending_reminders("G1")
    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder["source_ref"] == eid
    assert reminder["source_kind"] == cd.EVENT_REMINDER_SOURCE_KIND
    assert "接送網址：" in reminder["source_text"]
    assert "https://example.com/pickup" in reminder["source_text"]
    assert "驗證碼" in reminder["source_text"]
    assert "ABCD1234" in reminder["source_text"]


def test_event_reminder_uses_raw_sender_and_participant_aliases(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    raw_message_id = "msg-source-actor"
    memory.log_raw_message("G1", raw_message_id, "U_MOM", "爸爸(就醫) 明天 10:30 出發")

    event_date = (date.today() + timedelta(days=4)).isoformat()
    eid = cd.insert_event(
        group_id="G1",
        title="爸爸體檢",
        event_date=event_date,
        event_time="10:30",
        source_msg_id=raw_message_id,
        participants=["黃聖雅", "媽媽"],
    )
    assert eid

    reminders = memory.list_pending_reminders_full("G1")
    reminder = next(r for r in reminders if r["source_ref"] == eid)

    assert reminder["user_id"] == "U_MOM"
    assert reminder["mention_aliases"] == ["黃聖雅", "媽媽"]


def test_event_reminder_merges_mentioned_names_from_raw_text(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    raw_message_id = "msg-source-mentions"
    memory.log_raw_message(
        "G1",
        raw_message_id,
        "U_MOM",
        "這波家事有 @黃聖雅 陪同參與",
    )

    event_date = (date.today() + timedelta(days=4)).isoformat()
    eid = cd.insert_event(
        group_id="G1",
        title="家庭聚會",
        event_date=event_date,
        event_time="10:30",
        source_msg_id=raw_message_id,
        participants=["媽媽"],
    )
    assert eid

    reminders = memory.list_pending_reminders_full("G1")
    reminder = next(r for r in reminders if r["source_ref"] == eid)

    assert reminder["mention_aliases"] == ["媽媽", "黃聖雅"]


def test_event_reminder_treats_raw_text_family_all_as_all(tmp_calendar_db):
    cd = tmp_calendar_db
    _align_memory_db_with_calendar(cd)
    import memory

    raw_message_id = "msg-source-all"
    memory.log_raw_message(
        "G1",
        raw_message_id,
        "U_MOM",
        "今天全家去機場集合",
    )

    event_date = (date.today() + timedelta(days=4)).isoformat()
    eid = cd.insert_event(
        group_id="G1",
        title="機場接機",
        event_date=event_date,
        event_time="10:30",
        source_msg_id=raw_message_id,
    )
    assert eid

    reminders = memory.list_pending_reminders_full("G1")
    reminder = next(r for r in reminders if r["source_ref"] == eid)

    assert reminder["mention_aliases"] == ["全家"]


def test_event_reminder_skips_cancelled(tmp_calendar_db, monkeypatch):
    cd = tmp_calendar_db
    one = (date.today() + timedelta(days=1)).isoformat()
    eid = cd.insert_event(group_id="G1", title="X", event_date=one)
    cd.cancel_event(eid)

    import event_reminder

    importlib.reload(event_reminder)
    sent = []
    monkeypatch.setattr(
        event_reminder,
        "_send_reminder_message_spec",
        lambda spec, **_kw: sent.append(spec["text"]) or event_reminder.POST_OK,
    )
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "dummy")

    rc = event_reminder.main()
    assert rc == 0
    assert sent == []
