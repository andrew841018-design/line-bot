from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


_TW = ZoneInfo("Asia/Taipei")


@pytest.fixture
def correction_db(tmp_path, monkeypatch):
    import calendar_db
    import memory

    db_path = tmp_path / "quoted-correction.db"
    monkeypatch.setattr(memory, "_DB_PATH", db_path)
    monkeypatch.setattr(calendar_db, "_DB_PATH", db_path)
    memory._init_db()
    calendar_db.init_db()
    return db_path, memory, calendar_db


def _insert_source_event(
    db_path,
    calendar_db,
    *,
    group_id: str = "G1",
    source_msg_id: str = "source-message",
    title: str = "全家打球",
    event_date: str = "2026-08-02",
    event_time: str = "19:00",
) -> tuple[str, int]:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO raw_messages("
            "group_id, message_id, user_id, text, created_at"
            ") VALUES (?, ?, 'U1', ?, 1)",
            (
                group_id,
                source_msg_id,
                f"{event_date} {event_time} {title}",
            ),
        )
    event_id = calendar_db.insert_event(
        group_id=group_id,
        title=title,
        event_date=event_date,
        event_time=event_time,
        participants=["全家"],
        source_msg_id=source_msg_id,
    )
    assert event_id
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT reminder_id FROM reminders "
            "WHERE group_id=? AND source_kind='calendar_event' AND source_ref=?",
            (group_id, event_id),
        ).fetchone()
    assert row is not None
    return event_id, int(row[0])


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "更正為 20:00 全家羽球",
            {"new_date": None, "new_time": "20:00", "new_title": "全家羽球"},
        ),
        (
            "日期改成8/3",
            {"new_date": "08-03", "new_time": None, "new_title": None},
        ),
        (
            "時間改成2000",
            {"new_date": None, "new_time": "20:00", "new_title": None},
        ),
        (
            "標題改成全家壁球",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "更正為8/3 2000-2100 全家壁球",
            {"new_date": "08-03", "new_time": "20:00", "new_title": "全家壁球"},
        ),
        (
            "更正為下午3:00",
            {"new_date": None, "new_time": "15:00", "new_title": None},
        ),
        (
            "更正為晚上2000",
            {"new_date": None, "new_time": "20:00", "new_title": None},
        ),
        (
            "時間改成晚上八點",
            {"new_date": None, "new_time": "20:00", "new_title": None},
        ),
        (
            "更正時間為晚上八點",
            {"new_date": None, "new_time": "20:00", "new_title": None},
        ),
        (
            "更正日期為8/3",
            {"new_date": "08-03", "new_time": None, "new_title": None},
        ),
        (
            "改成8/20可以嗎",
            {"new_date": "08-20", "new_time": None, "new_title": None},
        ),
        (
            "更正標題為全家壁球",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "更正標題改成全家壁球",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "更正事項改為全家壁球",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "更正標題：改成全家壁球",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "更正標題為改成全家壁球",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "標題改成2025全家聚餐",
            {"new_date": None, "new_time": None, "new_title": "2025全家聚餐"},
        ),
        (
            "標題改成8/3全家壁球",
            {"new_date": None, "new_time": None, "new_title": "8/3全家壁球"},
        ),
        (
            "標題改成在家聚餐",
            {"new_date": None, "new_time": None, "new_title": "在家聚餐"},
        ),
        (
            "標題改成到台北聚餐",
            {"new_date": None, "new_time": None, "new_title": "到台北聚餐"},
        ),
        (
            "標題改成是全家聚餐",
            {"new_date": None, "new_time": None, "new_title": "是全家聚餐"},
        ),
        (
            "標題改成成為更好的全家",
            {
                "new_date": None,
                "new_time": None,
                "new_title": "成為更好的全家",
            },
        ),
        (
            "更正標題改成成為更好的全家",
            {
                "new_date": None,
                "new_time": None,
                "new_title": "成為更好的全家",
            },
        ),
        (
            "更正為8/3 1900–2000 全家壁球",
            {"new_date": "08-03", "new_time": "19:00", "new_title": "全家壁球"},
        ),
        (
            "更正為全家壁球可以嗎",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "更正為成為更好的全家",
            {
                "new_date": None,
                "new_time": None,
                "new_title": "成為更好的全家",
            },
        ),
        (
            "更正為在家聚餐",
            {"new_date": None, "new_time": None, "new_title": "在家聚餐"},
        ),
        (
            "更正為全家壁球嗎",
            {"new_date": None, "new_time": None, "new_title": "全家壁球"},
        ),
        (
            "更正為全家去酒吧",
            {"new_date": None, "new_time": None, "new_title": "全家去酒吧"},
        ),
        (
            "更正為全家去茶吧",
            {"new_date": None, "new_time": None, "new_title": "全家去茶吧"},
        ),
        (
            "更正為全家去書吧",
            {"new_date": None, "new_time": None, "new_title": "全家去書吧"},
        ),
        (
            "更正為全家買毛呢",
            {"new_date": None, "new_time": None, "new_title": "全家買毛呢"},
        ),
    ),
)
def test_parse_quoted_calendar_correction_supports_partial_fields(text, expected):
    import main

    assert main._parse_quoted_calendar_correction(text) == {
        "status": "ok",
        **expected,
    }


@pytest.mark.parametrize(
    "text",
    (
        "時間改成20000",
        "更正為2360",
        "標題改成時間",
        "更正一下",
        "時間改成1900-20000",
        "更正為1900-20000 全家壁球",
        "時間改成2360-2000",
        "時間改成2360-2000全家壁球",
        "時間改成晚上八點-二十五點",
        "時間改成晚上八點-二十五點全家壁球",
        "時間改成晚上12點",
        "時間改成約1900-20000全家壁球",
        "時間改成從2360-2000全家壁球",
        "時間改成101-102",
        "更正為101-102號房",
        "請把這句改成正式一點",
        "標題更正一下",
        "事項修正這個",
        "更正標題為那個",
    ),
)
def test_parse_quoted_calendar_correction_rejects_invalid_or_empty_fields(text):
    import main

    assert main._parse_quoted_calendar_correction(text)["status"] == "invalid"


def test_wallball_incident_passes_auto_capture_prefilter(monkeypatch):
    import main

    captured: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        main,
        "_capture_calendar_events_regex_only",
        lambda group_id, text, user_id, message_id: captured.append(
            (group_id, text, user_id, message_id)
        )
        or 1,
    )

    text = "8/2 1900-2000\n全家壁球"
    assert main._auto_capture_text_if_important("G1", text, "U1", "M1") is True
    assert captured == [("G1", text, "U1", "M1")]


def test_wallball_incident_persists_source_event_and_confirmation(correction_db):
    db_path, _memory, calendar_db = correction_db
    import main

    text = "8/2 1900-2000\n全家壁球"
    created_at = int(datetime(2026, 7, 29, 12, 38, tzinfo=_TW).timestamp())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO raw_messages("
            "group_id, message_id, user_id, text, created_at"
            ") VALUES ('G1', 'wallball-source', 'U1', ?, ?)",
            (text, created_at),
        )

    assert main._capture_calendar_events_regex_only(
        "G1",
        text,
        "U1",
        "wallball-source",
    ) == 1
    events = calendar_db.find_events_by_source_message("G1", "wallball-source")
    assert [
        (event["event_date"], event["event_time"], event["title"])
        for event in events
    ] == [("2026-08-02", "19:00", "全家壁球")]
    confirmation = main._format_source_calendar_capture_confirmation(
        "G1",
        "wallball-source",
        text,
    )
    assert "2026-08-02 19:00" in confirmation
    assert "全家壁球" in confirmation


def test_atomic_schedule_correction_preserves_identity_and_resets_delivery_flags(
    correction_db,
):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET reminded_at=11, reminded_7d=12, reminded_1d=13 "
            "WHERE event_id=?",
            (event_id,),
        )
        conn.execute(
            "UPDATE reminders SET last_pushed_at=21, weekly_count=2, "
            "last_weekly_at=22, pushed_3d=1, pushed_1d=1 "
            "WHERE reminder_id=?",
            (reminder_id,),
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=(8, 3),
        new_time="20:00",
        new_title="全家壁球",
    )

    assert result["status"] == "updated"
    assert result["event"]["event_id"] == event_id
    assert result["event"]["event_date"] == "2026-08-03"
    assert result["event"]["event_time"] == "20:00"
    assert result["event"]["title"] == "全家壁球"
    assert result["reminder"]["reminder_id"] == reminder_id
    assert result["reminder"]["action"] == "全家壁球"
    assert datetime.fromtimestamp(
        result["reminder"]["remind_at"], tz=_TW
    ).strftime("%Y-%m-%d %H:%M") == "2026-08-03 20:00"

    with sqlite3.connect(db_path) as conn:
        event_flags = conn.execute(
            "SELECT reminded_at, reminded_7d, reminded_1d FROM events "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        reminder_flags = conn.execute(
            "SELECT last_pushed_at, weekly_count, last_weekly_at, "
            "pushed_3d, pushed_1d FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
        counts = (
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
        )
    assert event_flags == (None, None, None)
    assert reminder_flags == (0, 0, 0, 0, 0)
    assert counts == (1, 1)


def test_atomic_title_only_correction_preserves_delivery_flags(correction_db):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET reminded_at=11, reminded_7d=12, reminded_1d=13 "
            "WHERE event_id=?",
            (event_id,),
        )
        conn.execute(
            "UPDATE reminders SET last_pushed_at=21, weekly_count=2, "
            "last_weekly_at=22, pushed_3d=1, pushed_1d=1 "
            "WHERE reminder_id=?",
            (reminder_id,),
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time=None,
        new_title="全家壁球",
    )

    assert result["status"] == "updated"
    with sqlite3.connect(db_path) as conn:
        event_flags = conn.execute(
            "SELECT reminded_at, reminded_7d, reminded_1d FROM events "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        reminder_flags = conn.execute(
            "SELECT last_pushed_at, weekly_count, last_weekly_at, "
            "pushed_3d, pushed_1d FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
    assert event_flags == (11, 12, 13)
    assert reminder_flags == (21, 2, 22, 1, 1)


def test_title_workflow_change_resets_only_natural_delivery_progress(
    correction_db,
):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(
        db_path,
        calendar_db,
        title="全家聚餐",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET reminded_at=11, reminded_7d=12, reminded_1d=13 "
            "WHERE event_id=?",
            (event_id,),
        )
        conn.execute(
            "UPDATE reminders SET last_pushed_at=21, weekly_count=2, "
            "last_weekly_at=22, pushed_3d=1, pushed_1d=1 "
            "WHERE reminder_id=?",
            (reminder_id,),
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time=None,
        new_title="全家羽球",
    )

    assert result["status"] == "updated"
    with sqlite3.connect(db_path) as conn:
        event_flags = conn.execute(
            "SELECT reminded_at, reminded_7d, reminded_1d FROM events "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        reminder_flags = conn.execute(
            "SELECT last_pushed_at, weekly_count, last_weekly_at, "
            "pushed_3d, pushed_1d FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
    assert event_flags == (11, 12, 13)
    assert reminder_flags == (0, 0, 0, 0, 0)


def test_title_actor_correction_updates_participants_and_mentions_atomically(
    correction_db,
):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(
        db_path,
        calendar_db,
        title="媽媽打球",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET participants=? WHERE event_id=?",
            (json.dumps(["媽媽"], ensure_ascii=False), event_id),
        )
        conn.execute(
            "UPDATE reminders SET mention_aliases=? WHERE reminder_id=?",
            (json.dumps(["媽媽"], ensure_ascii=False), reminder_id),
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time=None,
        new_title="全家壁球",
    )

    assert result["status"] == "updated"
    assert json.loads(result["event"]["participants"]) == ["全家"]
    assert json.loads(result["reminder"]["mention_aliases"]) == ["全家"]
    assert "參加人：全家" in result["reminder"]["source_text"]


def test_title_correction_removes_superseded_actor_and_mentions(correction_db):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(
        db_path,
        calendar_db,
        title="媽媽打球",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET participants=? WHERE event_id=?",
            (json.dumps(["媽媽"], ensure_ascii=False), event_id),
        )
        conn.execute(
            "UPDATE reminders SET mention_aliases=? WHERE reminder_id=?",
            (json.dumps(["媽媽"], ensure_ascii=False), reminder_id),
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time=None,
        new_title="壁球",
    )

    assert result["status"] == "updated"
    assert json.loads(result["event"]["participants"]) == []
    assert json.loads(result["reminder"]["mention_aliases"]) == []
    assert "參加人：" not in result["reminder"]["source_text"]


def test_title_correction_does_not_readd_actor_from_original_raw_text(
    correction_db,
):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(
        db_path,
        calendar_db,
        title="全家壁球",
    )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time=None,
        new_title="媽媽壁球",
    )

    assert result["status"] == "updated"
    assert json.loads(result["event"]["participants"]) == ["媽媽"]
    assert json.loads(result["reminder"]["mention_aliases"]) == ["媽媽"]


def test_sent_reminder_quote_resolves_same_event_and_replay_is_idempotent(
    correction_db,
):
    db_path, memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    assert memory.log_sent_reminder_reference(
        "G1",
        "bot-reminder-message",
        reminder_id=reminder_id,
        source_kind="calendar_event",
        source_ref=event_id,
    )

    first = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "bot-reminder-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )
    second = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "bot-reminder-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )

    assert first["status"] == "updated"
    assert second["status"] == "unchanged"
    assert second["event"]["event_id"] == event_id
    assert second["reminder"]["reminder_id"] == reminder_id
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_concurrent_identical_corrections_keep_one_identity(correction_db):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)

    def correct():
        return calendar_db.correct_quoted_event_and_reminder(
            "G1",
            "source-message",
            new_date=None,
            new_month_day=None,
            new_time="20:00",
            new_title="全家壁球",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: correct(), range(2)))

    assert sorted(result["status"] for result in results) == [
        "unchanged",
        "updated",
    ]
    with sqlite3.connect(db_path) as conn:
        events = conn.execute(
            "SELECT event_id, title, event_time FROM events"
        ).fetchall()
        reminders = conn.execute(
            "SELECT reminder_id, action FROM reminders"
        ).fetchall()
    assert events == [(event_id, "全家壁球", "20:00")]
    assert reminders == [(reminder_id, "全家壁球")]


@pytest.mark.parametrize("terminal_status", ("cancelled", "done", "expired"))
def test_atomic_correction_rejects_terminal_mirror(correction_db, terminal_status):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE reminders SET status=? WHERE reminder_id=?",
            (terminal_status, reminder_id),
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )

    assert result["status"] == "terminal"
    assert calendar_db.get_active_event_by_id("G1", event_id)["event_time"] == "19:00"


def test_atomic_correction_rejects_ambiguous_source_and_cross_group(
    correction_db,
):
    db_path, _memory, calendar_db = correction_db
    first_id, _ = _insert_source_event(db_path, calendar_db)
    second_id, _ = _insert_source_event(
        db_path,
        calendar_db,
        title="全家壁球",
        event_time="20:00",
    )

    ambiguous = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="21:00",
        new_title=None,
    )
    cross_group = calendar_db.correct_quoted_event_and_reminder(
        "G2",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="21:00",
        new_title=None,
    )

    assert ambiguous["status"] == "ambiguous"
    assert cross_group["status"] == "not_found"
    assert calendar_db.get_active_event_by_id("G1", first_id)["event_time"] == "19:00"
    assert calendar_db.get_active_event_by_id("G1", second_id)["event_time"] == "20:00"


def test_sent_reference_identity_conflict_fails_closed(correction_db):
    db_path, memory, calendar_db = correction_db
    first_id, first_reminder = _insert_source_event(db_path, calendar_db)
    second_id, _ = _insert_source_event(
        db_path,
        calendar_db,
        source_msg_id="second-source",
        title="全家壁球",
        event_time="20:00",
    )
    assert memory.log_sent_reminder_reference(
        "G1",
        "conflicting-sent-reference",
        reminder_id=first_reminder,
        source_kind="calendar_event",
        source_ref=second_id,
    )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "conflicting-sent-reference",
        new_date=None,
        new_month_day=None,
        new_time="21:00",
        new_title=None,
    )

    assert result["status"] == "ambiguous"
    assert calendar_db.get_active_event_by_id("G1", first_id)["event_time"] == "19:00"
    assert calendar_db.get_active_event_by_id("G1", second_id)["event_time"] == "20:00"


def test_sent_reference_with_blank_reminder_identity_fails_closed(
    correction_db,
):
    db_path, memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE reminders SET source_kind='', source_ref='' "
            "WHERE reminder_id=?",
            (reminder_id,),
        )
    assert memory.log_sent_reminder_reference(
        "G1",
        "blank-reminder-identity",
        reminder_id=reminder_id,
        source_kind="calendar_event",
        source_ref=event_id,
    )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "blank-reminder-identity",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )

    assert result["status"] == "not_found"


def test_atomic_correction_rejects_duplicate_pending_mirror(correction_db):
    db_path, _memory, calendar_db = correction_db
    event_id, _ = _insert_source_event(db_path, calendar_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_reminders_pending_source_unique")
        conn.execute(
            "INSERT INTO reminders("
            "group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases"
            ") SELECT group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases "
            "FROM reminders WHERE source_ref=?",
            (event_id,),
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )

    assert result["status"] == "ambiguous"
    assert calendar_db.get_active_event_by_id("G1", event_id)["event_time"] == "19:00"


def test_atomic_correction_rejects_inflight_claim(correction_db):
    db_path, memory, calendar_db = correction_db
    event_id, _ = _insert_source_event(db_path, calendar_db)
    claim = memory.claim_calendar_reminder_delivery(
        "G1",
        "calendar_event",
        event_id,
        7,
        expected_title="全家打球",
        expected_event_date="2026-08-02",
        expected_event_time="19:00",
        expected_location="",
        expected_participants=json.dumps(["全家"], ensure_ascii=False),
        transport="push",
    )
    assert claim is not None

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )

    assert result["status"] == "busy"
    assert calendar_db.get_active_event_by_id("G1", event_id)["event_time"] == "19:00"


@pytest.mark.parametrize("claim_state", ("stale_sending", "uncertain"))
def test_atomic_correction_allows_nonlive_claim_but_preserves_fence(
    correction_db,
    claim_state,
):
    db_path, memory, calendar_db = correction_db
    event_id, _ = _insert_source_event(db_path, calendar_db)
    claim = memory.claim_calendar_reminder_delivery(
        "G1",
        "calendar_event",
        event_id,
        7,
        expected_title="全家打球",
        expected_event_date="2026-08-02",
        expected_event_time="19:00",
        expected_location="",
        expected_participants=json.dumps(["全家"], ensure_ascii=False),
        transport="push",
    )
    assert claim is not None
    if claim_state == "stale_sending":
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE reminder_delivery_claims SET claimed_at=1 "
                "WHERE claim_token=?",
                (claim["claim_token"],),
            )
    else:
        assert memory.mark_reminder_delivery_claim_uncertain(claim)

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )

    assert result["status"] == "updated"
    with sqlite3.connect(db_path) as conn:
        state = conn.execute(
            "SELECT state FROM reminder_delivery_claims WHERE claim_token=?",
            (claim["claim_token"],),
        ).fetchone()
    assert state == ("uncertain",)
    assert memory.finalize_calendar_reminder_delivery(claim) is False


def test_atomic_correction_rejects_natural_inflight_claim(correction_db):
    db_path, memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    reminder = memory.get_reminder(reminder_id)
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action=str(reminder["action"]),
        expected_remind_at=int(reminder["remind_at"]),
        transport="push",
    )
    assert claim is not None

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title=None,
    )

    assert result["status"] == "busy"
    assert calendar_db.get_active_event_by_id("G1", event_id)["event_time"] == "19:00"


def test_atomic_correction_rolls_back_on_unique_event_conflict(correction_db):
    db_path, _memory, calendar_db = correction_db
    first_id, first_reminder = _insert_source_event(db_path, calendar_db)
    _insert_source_event(
        db_path,
        calendar_db,
        source_msg_id="second-source",
        title="全家壁球",
        event_time="20:00",
    )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title="全家壁球",
    )

    assert result["status"] == "conflict"
    assert calendar_db.get_active_event_by_id("G1", first_id)["title"] == "全家打球"
    assert memory_row(db_path, first_reminder, "action") == "全家打球"


def test_atomic_correction_rolls_back_when_reminder_update_aborts(correction_db):
    db_path, _memory, calendar_db = correction_db
    event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER abort_quoted_reminder_update "
            "BEFORE UPDATE OF action ON reminders "
            "BEGIN SELECT RAISE(ABORT, 'forced reminder failure'); END"
        )

    result = calendar_db.correct_quoted_event_and_reminder(
        "G1",
        "source-message",
        new_date=None,
        new_month_day=None,
        new_time="20:00",
        new_title="全家壁球",
    )

    assert result["status"] == "unavailable"
    with sqlite3.connect(db_path) as conn:
        event = conn.execute(
            "SELECT title, event_time FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        reminder = conn.execute(
            "SELECT action, remind_at FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
    assert event == ("全家打球", "19:00")
    assert reminder[0] == "全家打球"


def memory_row(db_path, reminder_id: int, column: str):
    assert column in {"action"}
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {column} FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def test_month_day_correction_uses_nearest_year(correction_db):
    _db_path, _memory, calendar_db = correction_db

    assert calendar_db._resolve_corrected_date(
        "2026-12-30",
        None,
        (1, 6),
    ) == "2027-01-06"
    assert calendar_db._resolve_corrected_date(
        "2026-01-02",
        None,
        (12, 30),
    ) == "2025-12-30"


def test_calendar_claim_rejects_stale_render_snapshot(correction_db):
    db_path, memory, calendar_db = correction_db
    event_id, _ = _insert_source_event(db_path, calendar_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET title='全家壁球', event_time='20:00' "
            "WHERE event_id=?",
            (event_id,),
        )

    assert memory.claim_calendar_reminder_delivery(
        "G1",
        "calendar_event",
        event_id,
        7,
        expected_title="全家打球",
        expected_event_date="2026-08-02",
        expected_event_time="19:00",
        expected_location="",
        expected_participants=json.dumps(["全家"], ensure_ascii=False),
        transport="push",
    ) is None


def test_calendar_claim_rejects_stale_participant_snapshot(correction_db):
    db_path, memory, calendar_db = correction_db
    event_id, _ = _insert_source_event(db_path, calendar_db)
    old_participants = json.dumps(["全家"], ensure_ascii=False)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET participants=? WHERE event_id=?",
            (json.dumps(["媽媽"], ensure_ascii=False), event_id),
        )

    assert memory.claim_calendar_reminder_delivery(
        "G1",
        "calendar_event",
        event_id,
        7,
        expected_title="全家打球",
        expected_event_date="2026-08-02",
        expected_event_time="19:00",
        expected_location="",
        expected_participants=old_participants,
        transport="push",
    ) is None


def test_natural_claim_rejects_stale_mention_snapshot(correction_db):
    db_path, memory, calendar_db = correction_db
    _event_id, reminder_id = _insert_source_event(db_path, calendar_db)
    reminder = memory.get_reminder(reminder_id)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE reminders SET mention_aliases=? WHERE reminder_id=?",
            (json.dumps(["媽媽"], ensure_ascii=False), reminder_id),
        )

    assert memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action=str(reminder["action"]),
        expected_remind_at=int(reminder["remind_at"]),
        expected_user_id=str(reminder.get("user_id") or ""),
        expected_source_kind=str(reminder.get("source_kind") or ""),
        expected_source_ref=str(reminder.get("source_ref") or ""),
        expected_source_text=str(reminder.get("source_text") or ""),
        expected_mention_aliases=list(reminder.get("mention_aliases") or []),
        transport="push",
    ) is None


def test_quoted_route_replies_with_persisted_result_and_disables_push_fallback(
    monkeypatch,
):
    import main
    import calendar_db

    replies: list[tuple[str, dict]] = []
    cancelled_bursts: list[str] = []
    monkeypatch.setattr(
        calendar_db,
        "correct_quoted_event_and_reminder",
        lambda *a, **k: {
            "status": "updated",
            "event": {
                "event_date": "2026-08-03",
                "event_time": "20:00",
                "title": "全家壁球",
            },
            "reminder": {"action": "全家壁球"},
        },
    )
    monkeypatch.setattr(
        main,
        "_reply",
        lambda token, text, **kwargs: replies.append((text, kwargs)),
    )
    monkeypatch.setattr(
        main.burst_filter,
        "cancel_burst",
        lambda group_id: cancelled_bursts.append(group_id),
    )

    class Message:
        quoted_message_id = "source-message"

    class Event:
        message = Message()
        reply_token = "reply-token"

    assert main._try_handle_quoted_calendar_correction(
        Event(),
        "G1",
        "時間改成2000",
    )
    assert "2026-08-03 20:00" in replies[0][0]
    assert "全家壁球" in replies[0][0]
    assert replies[0][1]["allow_push_fallback"] is False
    assert replies[0][1]["include_auxiliary"] is False
    assert cancelled_bursts == ["G1"]


def test_invalid_quoted_correction_is_consumed_without_mutation(monkeypatch):
    import main
    import calendar_db

    replies: list[str] = []
    monkeypatch.setattr(
        calendar_db,
        "correct_quoted_event_and_reminder",
        lambda *a, **k: pytest.fail("invalid correction must not mutate"),
    )
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    class Message:
        quoted_message_id = "source-message"

    class Event:
        message = Message()
        reply_token = "reply-token"

    assert main._try_handle_quoted_calendar_correction(
        Event(),
        "G1",
        "更正一下",
    )
    assert "沒有變更" in replies[0]


def test_quoted_route_reports_unavailable_when_db_call_raises(monkeypatch):
    import main
    import calendar_db

    replies: list[str] = []
    cancelled_bursts: list[str] = []

    def raise_unavailable(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(
        calendar_db,
        "correct_quoted_event_and_reminder",
        raise_unavailable,
    )
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(
        main.burst_filter,
        "cancel_burst",
        lambda group_id: cancelled_bursts.append(group_id),
    )

    class Message:
        quoted_message_id = "source-message"

    class Event:
        message = Message()
        reply_token = "reply-token"

    assert main._try_handle_quoted_calendar_correction(
        Event(),
        "G1",
        "時間改成2000",
    )
    assert "未能一起完成更新" in replies[0]
    assert cancelled_bursts == ["G1"]


def test_legacy_correction_route_uses_atomic_claim_aware_update(monkeypatch):
    import main
    import calendar_db

    replies: list[str] = []
    target = {
        "event_id": "event-1",
        "title": "全家壁球",
        "event_date": "2026-08-02",
        "event_time": "19:00",
        "location": "",
    }
    monkeypatch.setattr(
        main,
        "_parse_calendar_correction",
        lambda _text: {
            "keywords": ["壁球"],
            "target_date": "2026-08-02",
            "new_date": "2026-08-02",
            "new_date_explicit": True,
            "new_time": "20:00",
        },
    )
    monkeypatch.setattr(
        main,
        "_find_calendar_correction_event",
        lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        main,
        "_calendar_correction_new_title",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        calendar_db,
        "correct_event_and_reminder_by_id",
        lambda *args, **kwargs: {"status": "busy"},
    )
    monkeypatch.setattr(
        calendar_db,
        "update_event_schedule",
        lambda *args, **kwargs: pytest.fail("legacy update must not run"),
    )
    monkeypatch.setattr(
        main,
        "_update_calendar_correction_reminders",
        lambda *args, **kwargs: pytest.fail("fuzzy reminder update must not run"),
    )
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    class Event:
        reply_token = "reply-token"

    assert main._try_handle_calendar_correction(
        Event(),
        "G1",
        "壁球改成20:00",
    )
    assert "正在推送中" in replies[0]


def test_handler_stops_after_invalid_quoted_correction(monkeypatch):
    import main

    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(
        main,
        "_try_one_shot_reply",
        lambda *a, **k: pytest.fail("one-shot must not run"),
    )
    monkeypatch.setattr(
        main,
        "_auto_capture_text_if_important",
        lambda *a, **k: pytest.fail("auto capture must not run"),
    )

    class Message:
        id = "correction-message"
        text = "更正一下"
        quoted_message_id = "source-message"

    class Source:
        user_id = "U1"

    class Event:
        message = Message()
        source = Source()
        reply_token = "reply-token"

    main._handle_text_message(Event(), "G1")
    assert "沒有變更" in replies[0]


def test_pending_source_identity_has_unique_index(correction_db):
    db_path, _memory, _calendar_db = correction_db
    with sqlite3.connect(db_path) as conn:
        indexes = conn.execute("PRAGMA index_list(reminders)").fetchall()
    assert any(
        row[1] == "idx_reminders_pending_source_unique" and row[2] == 1
        for row in indexes
    )


def test_init_fails_closed_on_legacy_pending_source_duplicates(
    correction_db,
):
    db_path, memory, _calendar_db = correction_db
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_reminders_pending_source_unique")
        for action, remind_at, created_at, pushed_1d, aliases in (
            ("舊內容", 100, 2, 0, '["媽媽"]'),
            ("新內容", 200, 1, 1, '["爸爸"]'),
        ):
            conn.execute(
                "INSERT INTO reminders("
                "group_id, user_id, action, remind_at, created_at, status, "
                "source_kind, source_ref, source_text, mention_aliases, pushed_1d"
                ") VALUES ('G1', 'U1', ?, ?, ?, 'pending', "
                "'calendar_event', 'legacy-event', ?, ?, ?)",
                (action, remind_at, created_at, action, aliases, pushed_1d),
            )

    with pytest.raises(
        RuntimeError,
        match="pending reminder source identity is not unique",
    ):
        memory._init_db()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT action, remind_at, created_at, pushed_1d, mention_aliases "
            "FROM reminders WHERE source_ref='legacy-event' "
            "ORDER BY reminder_id"
        ).fetchall()
        indexes = conn.execute("PRAGMA index_list(reminders)").fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("舊內容", 100),
        ("新內容", 200),
    ]
    assert not any(
        row[1] == "idx_reminders_pending_source_unique" and row[2] == 1
        for row in indexes
    )
