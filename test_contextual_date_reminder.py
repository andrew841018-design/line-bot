"""Context-bound exact-date reminder batches.

The follow-up command names reminder dates while the immediately preceding
same-sender message provides the two appointments those dates refer to.  This
suite keeps that narrow contract separate from the general reminder parser.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from unittest.mock import MagicMock
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    import memory

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = Path(tmp.name)
    monkeypatch.setattr(memory, "_DB_PATH", path)
    memory._init_db()
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _insert_raw(
    db_path: Path,
    *,
    group_id: str,
    message_id: str,
    user_id: str,
    text: str,
    created_at: int,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO raw_messages(group_id,message_id,user_id,text,created_at) "
            "VALUES (?,?,?,?,?)",
            (group_id, message_id, user_id, text, created_at),
        )


def test_shared_month_medical_source_parser_preserves_appointment_time():
    import calendar_regex

    parsed = calendar_regex.extract_contextual_appointment_pair(
        "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。",
        date(2099, 8, 28),
    )

    assert parsed == [
        {
            "date": "2099-11-16",
            "time": "14:30",
            "title": "測試牙科例行檢查",
            "event_type": "medical",
        },
        {
            "date": "2099-11-24",
            "time": None,
            "title": "測試回診確認結果",
            "event_type": "medical",
        },
    ]


@pytest.mark.parametrize(
    "source",
    [
        "爸爸說11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。",
        "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果嗎？",
        "我不要11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。",
        "我11月16日下午兩點半測試牙科例行檢查，2月30日測試回診確認結果。",
        "我11月16日下午兩點半測試牙科例行檢查，12月24日測試回診確認結果。",
    ],
)
def test_shared_month_medical_source_parser_fails_closed(source):
    import calendar_regex

    assert (
        calendar_regex.extract_contextual_appointment_pair(
            source,
            date(2099, 8, 28),
        )
        == []
    )


def test_context_lookup_is_anchored_same_sender_and_allows_one_bot_ack(temp_db):
    import memory

    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="SOURCE1",
        user_id="U_DAD",
        text="我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。",
        created_at=100,
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="BOT1",
        user_id="__bot__",
        text="已新增提醒",
        created_at=101,
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="COMMAND1",
        user_id="U_DAD",
        text="咪寶：麻煩11月15日及23日以及當天提醒，謝謝！",
        created_at=184,
    )

    source = memory.get_contextual_reminder_source(
        "G1",
        "COMMAND1",
        max_age_sec=180,
    )

    assert source is not None
    assert source["message_id"] == "SOURCE1"
    assert source["user_id"] == "U_DAD"


def test_context_lookup_rejects_other_human_intervention(temp_db):
    import memory

    for mid, uid, text, ts in (
        ("SOURCE1", "U_DAD", "11月16日看牙，24日回診", 100),
        ("OTHER1", "U_MOM", "我也有事", 130),
        ("COMMAND1", "U_DAD", "麻煩11月15日及23日以及當天提醒", 150),
    ):
        _insert_raw(
            temp_db,
            group_id="G1",
            message_id=mid,
            user_id=uid,
            text=text,
            created_at=ts,
        )

    assert (
        memory.get_contextual_reminder_source(
            "G1",
            "COMMAND1",
            max_age_sec=180,
        )
        is None
    )


def test_context_lookup_requires_one_recognized_reminder_ack(temp_db):
    import memory

    for mid, uid, text, ts in (
        ("SOURCE1", "U_DAD", "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。", 100),
        ("COMMAND1", "U_DAD", "麻煩11月15日及23日以及當天提醒", 120),
    ):
        _insert_raw(
            temp_db,
            group_id="G1",
            message_id=mid,
            user_id=uid,
            text=text,
            created_at=ts,
        )

    assert memory.get_contextual_reminder_source("G1", "COMMAND1") is None


def test_contextual_followup_plan_builds_exact_four_default_time_slots(
    temp_db,
    monkeypatch,
):
    import main

    source_ts = int(
        datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="SOURCE1",
        user_id="U_DAD",
        text="我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。",
        created_at=source_ts,
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="BOT1",
        user_id="__bot__",
        text="已新增提醒",
        created_at=source_ts + 1,
    )
    command = "咪寶：麻煩11月15日及23日以及當天提醒，謝謝！"
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="COMMAND1",
        user_id="U_DAD",
        text=command,
        created_at=source_ts + 84,
    )
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")

    plan = main._contextual_date_reminder_plan(
        command,
        "G1",
        "U_DAD",
        "COMMAND1",
    )

    assert plan is not None
    assert plan["source_message_id"] == "SOURCE1"
    assert [
        datetime.fromtimestamp(row["remind_at"], ZoneInfo("Asia/Taipei")).strftime(
            "%Y-%m-%d %H:%M"
        )
        for row in plan["reminders"]
    ] == [
        "2099-11-15 12:00",
        "2099-11-16 12:00",
        "2099-11-23 12:00",
        "2099-11-24 12:00",
    ]
    assert [row["action"] for row in plan["reminders"]] == [
        "爸爸 11/16 14:30 測試牙科例行檢查（前一天提醒）",  # privacy-safe-fixture
        "爸爸 11/16 14:30 測試牙科例行檢查（當天提醒）",  # privacy-safe-fixture
        "爸爸 11/24 測試回診確認結果（前一天提醒）",
        "爸爸 11/24 測試回診確認結果（當天提醒）",
    ]
    assert all(row["mention_aliases"] == ["爸爸"] for row in plan["reminders"])
    assert all(row["source_kind"] == "contextual_date_once" for row in plan["reminders"])


def test_contextual_followup_allows_same_day_catchup_after_default_time(
    temp_db,
    monkeypatch,
):
    import main

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 15, 12, 0, tzinfo=tz)

    source_ts = int(
        datetime(2099, 11, 15, 11, 58, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    for mid, uid, text, ts in (
        ("SOURCE1", "U_DAD", "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。", source_ts),
        ("BOT1", "__bot__", "已新增提醒", source_ts + 1),
        ("COMMAND1", "U_DAD", "麻煩11月15日及23日以及當天提醒", source_ts + 60),
    ):
        _insert_raw(
            temp_db,
            group_id="G1",
            message_id=mid,
            user_id=uid,
            text=text,
            created_at=ts,
        )
    monkeypatch.setattr(main, "datetime", FixedDateTime)
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")

    plan = main._contextual_date_reminder_plan(
        "麻煩11月15日及23日以及當天提醒",
        "G1",
        "U_DAD",
        "COMMAND1",
    )

    assert plan is not None
    first = datetime.fromtimestamp(
        plan["reminders"][0]["remind_at"], ZoneInfo("Asia/Taipei")
    )
    assert first.strftime("%Y-%m-%d %H:%M") == "2099-11-15 12:00"


def test_contextual_followup_plan_rejects_mismatched_lead_dates(temp_db, monkeypatch):
    import main

    source = "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。"
    command = "咪寶：麻煩11月14日及22日以及當天提醒，謝謝！"
    source_ts = int(
        datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="SOURCE1",
        user_id="U_DAD",
        text=source,
        created_at=source_ts,
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="COMMAND1",
        user_id="U_DAD",
        text=command,
        created_at=source_ts + 20,
    )
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")

    assert (
        main._contextual_date_reminder_plan(
            command,
            "G1",
            "U_DAD",
            "COMMAND1",
        )
        is None
    )


def test_contextual_batch_rejects_source_bound_calendar_mirror(
    temp_db,
    monkeypatch,
):
    import main
    import memory

    source_text = "我11月16日下午兩點半看牙醫，24日回診拆線。"
    command_text = "咪寶：麻煩11月15日及23日以及當天提醒，謝謝！"
    source_ts = int(
        datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    for mid, uid, text, ts in (
        ("SOURCE1", "U_DAD", source_text, source_ts),
        ("BOT1", "__bot__", "已新增提醒", source_ts + 1),
        ("COMMAND1", "U_DAD", command_text, source_ts + 84),
    ):
        _insert_raw(
            temp_db,
            group_id="G1",
            message_id=mid,
            user_id=uid,
            text=text,
            created_at=ts,
        )
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")
    plan = main._contextual_date_reminder_plan(
        command_text,
        "G1",
        "U_DAD",
        "COMMAND1",
    )
    assert plan is not None
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "CREATE TABLE events(event_id TEXT PRIMARY KEY,group_id TEXT NOT NULL,"
            "source_msg_id TEXT,status TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO events(event_id,group_id,source_msg_id,status) "
            "VALUES ('EVENT1','G1','SOURCE1','active')"
        )
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref,source_text) "
            "VALUES ('G1','U_DAD','看牙醫',1,1,'pending','calendar_event','EVENT1',?)",
            (source_text,),
        )

    with pytest.raises(RuntimeError, match="calendar-bound"):
        memory.complete_contextual_date_reminder_batch(
            group_id="G1",
            user_id="U_DAD",
            plan=plan,
        )

    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute(
            "SELECT source_kind,source_ref FROM reminders ORDER BY reminder_id"
        ).fetchall()
    assert rows == [("calendar_event", "EVENT1")]


def test_contextual_batch_reconciles_legacy_atomically_and_is_idempotent(
    temp_db,
    tmp_path,
    monkeypatch,
):
    import main
    import memory
    from scripts import repair_contextual_date_reminder

    source_text = "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。"
    command_text = "咪寶：麻煩11月15日及23日以及當天提醒，謝謝！"
    source_ts = int(datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    command_ts = source_ts + 84
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="SOURCE1",
        user_id="U_DAD",
        text=source_text,
        created_at=source_ts,
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="COMMAND1",
        user_id="U_DAD",
        text=command_text,
        created_at=command_ts,
    )
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref,source_text,mention_aliases) "
            "VALUES (?,?,?,?,?,'pending','','',?,?)",
            (
                "G1",
                "U_DAD",
                "爸爸測試牙科例行檢查，24日測試回診確認結果。",
                int(datetime(2099, 11, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()),
                source_ts,
                source_text,
                json.dumps(["爸爸"], ensure_ascii=False),
            ),
        )
        legacy_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO pending_reminder_extract(group_id,user_id,message_id,text,created_at,status) "
            "VALUES (?,?,?,?,?,'pending')",
            ("G1", "U_DAD", "COMMAND1", command_text, command_ts),
        )
        pending_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    backup = tmp_path / "before-contextual-repair.sqlite3"
    repair_contextual_date_reminder._backup_database(backup)

    claim = memory.claim_pending_reminder(pending_id)
    assert claim
    plan = {
        "source_message_id": "SOURCE1",
        "command_message_id": "COMMAND1",
        "source_text": source_text,
        "command_text": command_text,
        "legacy_expected_remind_at": int(
            datetime(2099, 11, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
        ),
        "reminders": [
            {
                "action": action,
                "remind_at": int(datetime.fromisoformat(when).replace(tzinfo=ZoneInfo("Asia/Taipei")).timestamp()),
                "source_kind": "contextual_date_once",
                "source_ref": f"COMMAND1:{slot}",
                "mention_aliases": ["爸爸"],
            }
            for slot, when, action in (
                ("lead:0", "2099-11-15T12:00", "爸爸 11/16 14:30 測試牙科例行檢查（前一天提醒）"),  # privacy-safe-fixture
                ("same:0", "2099-11-16T12:00", "爸爸 11/16 14:30 測試牙科例行檢查（當天提醒）"),  # privacy-safe-fixture
                ("lead:1", "2099-11-23T12:00", "爸爸 11/24 測試回診確認結果（前一天提醒）"),  # privacy-safe-fixture
                ("same:1", "2099-11-24T12:00", "爸爸 11/24 測試回診確認結果（當天提醒）"),  # privacy-safe-fixture
            )
        ],
    }
    plan["legacy_expected"] = memory.get_contextual_legacy_reminder(
        "G1",
        "U_DAD",
        source_text,
    )
    assert plan["legacy_expected"] is not None

    first = memory.complete_contextual_date_reminder_batch(
        group_id="G1",
        user_id="U_DAD",
        plan=plan,
        pending_id=pending_id,
        pending_claim_token=claim,
        legacy_reminder_id=legacy_id,
    )
    second = memory.complete_contextual_date_reminder_batch(
        group_id="G1",
        user_id="U_DAD",
        plan=plan,
        pending_id=None,
        pending_claim_token=None,
        legacy_reminder_id=None,
    )

    assert first["outcome"] == "created"
    assert second["outcome"] == "duplicate"
    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute(
            "SELECT reminder_id,action,remind_at,source_kind,source_ref,status "
            "FROM reminders WHERE group_id='G1' ORDER BY remind_at"
        ).fetchall()
        pending_status = conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
        outbox_count = conn.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox"
        ).fetchone()[0]
    assert len(rows) == 4
    assert rows[1][0] == legacy_id
    assert all(row[3] == "contextual_date_once" for row in rows)
    assert pending_status == "done"
    assert outbox_count == 0

    first_contextual_id = int(first["reminder_ids"][0])
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status) "
            "VALUES ('OTHER','OTHER','unrelated',1,1,'pending')"
        )
        unrelated_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "UPDATE reminders SET user_id='DRIFTED' WHERE reminder_id=?",
            (first_contextual_id,),
        )
    with pytest.raises(RuntimeError, match="payload drifted"):
        repair_contextual_date_reminder._rollback_database(
            backup,
            {
                "pending_id": pending_id,
                "group_id": "G1",
                "user_id": "U_DAD",
                "text": command_text,
            },
            plan,
        )
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute(
            "SELECT action FROM reminders WHERE reminder_id=?", (unrelated_id,)
        ).fetchone() == ("unrelated",)
        assert conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE source_kind='contextual_date_once'"
        ).fetchone()[0] == 4
        conn.execute(
            "UPDATE reminders SET user_id='U_DAD' WHERE reminder_id=?",
            (first_contextual_id,),
        )
        conn.execute(
            "INSERT INTO sent_reminder_refs(group_id,message_id,reminder_id,created_at) "
            "VALUES ('G1','SENT1',?,1)",
            (first_contextual_id,),
        )
    with pytest.raises(RuntimeError, match="delivery history"):
        repair_contextual_date_reminder._rollback_database(
            backup,
            {
                "pending_id": pending_id,
                "group_id": "G1",
                "user_id": "U_DAD",
                "text": command_text,
            },
            plan,
        )
    with sqlite3.connect(temp_db) as conn:
        conn.execute("DELETE FROM sent_reminder_refs WHERE message_id='SENT1'")

    rolled_back = repair_contextual_date_reminder._rollback_database(
        backup,
        {
            "pending_id": pending_id,
            "group_id": "G1",
            "user_id": "U_DAD",
            "text": command_text,
        },
        plan,
    )
    assert sorted(rolled_back) == sorted(first["reminder_ids"])
    with sqlite3.connect(temp_db) as conn:
        restored = conn.execute(
            "SELECT reminder_id,action,remind_at,source_kind,source_ref,status "
            "FROM reminders"
        ).fetchall()
        restored_pending = conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
    assert sorted(restored, key=lambda item: item[0]) == sorted([
        (legacy_id, "爸爸測試牙科例行檢查，24日測試回診確認結果。", int(
            datetime(2099, 11, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
        ), "", "", "pending"),
        (unrelated_id, "unrelated", 1, "", "", "pending"),
    ], key=lambda item: item[0])
    assert restored_pending == "dropped"
    main._drain_pending_reminders("G1", local_only=True)
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone() == ("dropped",)
        assert conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE source_kind='contextual_date_once'"
        ).fetchone()[0] == 0
        conn.execute(
            "INSERT INTO inbound_events(group_id,message_id,status,created_at,updated_at) "
            "VALUES ('G1','COMMAND1','processing',1,1)"
        )
    reply = MagicMock()
    monkeypatch.setattr(main, "_reply", reply)
    event = MagicMock()
    event.reply_token = "REDLIVERY_TOKEN"
    assert main._try_handle_contextual_date_reminder(
        event,
        "G1",
        command_text,
        "U_DAD",
        "COMMAND1",
    )
    reply.assert_not_called()
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute(
            "SELECT status FROM inbound_events WHERE group_id='G1' AND message_id='COMMAND1'"
        ).fetchone() == ("completed_no_reply",)
        assert conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE source_kind='contextual_date_once'"
        ).fetchone()[0] == 0


def test_contextual_duplicate_refuses_late_source_less_legacy(temp_db):
    import memory

    source_text = "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。"
    command_text = "咪寶：麻煩11月15日及23日以及當天提醒，謝謝！"
    source_ts = int(
        datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    for mid, text, ts in (
        ("SOURCE1", source_text, source_ts),
        ("COMMAND1", command_text, source_ts + 84),
    ):
        _insert_raw(
            temp_db,
            group_id="G1",
            message_id=mid,
            user_id="U_DAD",
            text=text,
            created_at=ts,
        )
    plan = {
        "source_message_id": "SOURCE1",
        "command_message_id": "COMMAND1",
        "source_text": source_text,
        "command_text": command_text,
        "legacy_expected_remind_at": int(
            datetime(2099, 11, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
        ),
        "reminders": [
            {
                "action": f"爸爸 slot {slot}",
                "remind_at": int(
                    datetime(2099, 11, day, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
                ),
                "source_kind": "contextual_date_once",
                "source_ref": f"COMMAND1:{slot}",
                "mention_aliases": ["爸爸"],
            }
            for slot, day in (
                ("lead:0", 15),
                ("same:0", 16),
                ("lead:1", 23),
                ("same:1", 24),
            )
        ],
    }
    created = memory.complete_contextual_date_reminder_batch(
        group_id="G1",
        user_id="U_DAD",
        plan=plan,
    )
    assert created["outcome"] == "created"
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref,source_text) VALUES (?,?,?,?,1,'pending','','',?)",
            (
                "G1",
                "U_DAD",
                "late bundled row",
                plan["legacy_expected_remind_at"],
                source_text,
            ),
        )

    with pytest.raises(RuntimeError, match="legacy residue"):
        memory.complete_contextual_date_reminder_batch(
            group_id="G1",
            user_id="U_DAD",
            plan=plan,
        )


@pytest.mark.parametrize(
    ("seconds_after", "expected"),
    [
        (-1, None),
        (0, "now"),
        (3 * 3600, "now"),
        (14 * 3600 + 59 * 60, "now"),
        (24 * 3600, None),
    ],
)
def test_contextual_date_once_only_sends_on_or_after_time_same_day(
    seconds_after,
    expected,
):
    import reminder_push

    remind_dt = datetime(2099, 11, 15, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    row = {
        "remind_at": int(remind_dt.timestamp()),
        "source_kind": "contextual_date_once",
        "source_ref": "COMMAND1:lead:0",
        "pushed_now": 0,
        "pushed_1hr": 0,
        "pushed_2hr": 0,
        "pushed_4hr": 0,
        "pushed_1d": 0,
        "pushed_3d": 0,
        "weekly_count": 0,
        "last_weekly_at": 0,
    }

    assert (
        reminder_push._decide_stage(
            row,
            int(remind_dt.timestamp()) + seconds_after,
        )
        == expected
    )


def test_contextual_late_same_day_delivery_is_labeled_as_catchup():
    import reminder_push

    remind_dt = datetime(2099, 11, 15, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    row = {
        "remind_at": int(remind_dt.timestamp()),
        "source_kind": "contextual_date_once",
        "action": "爸爸 看牙",
        "mention_aliases": [],
        "user_id": "",
    }

    text = reminder_push._format_push_text(
        row,
        "now",
        now=int(datetime(2099, 11, 15, 23, 59, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()),
    )

    assert "今天，補送" in text
    assert "即將到時" not in text


def test_contextual_batch_rolls_back_legacy_and_partial_inserts(temp_db):
    import memory

    source_text = "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。"
    command_text = "咪寶：麻煩11月15日及23日以及當天提醒，謝謝！"
    source_ts = int(datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="SOURCE1",
        user_id="U_DAD",
        text=source_text,
        created_at=source_ts,
    )
    _insert_raw(
        temp_db,
        group_id="G1",
        message_id="COMMAND1",
        user_id="U_DAD",
        text=command_text,
        created_at=source_ts + 84,
    )
    legacy_action = "爸爸測試牙科例行檢查，24日測試回診確認結果。"
    legacy_time = int(
        datetime(2099, 11, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref,source_text,mention_aliases) "
            "VALUES (?,?,?,?,?,'pending','','',?,?)",
            (
                "G1", "U_DAD", legacy_action, legacy_time, source_ts,
                source_text, json.dumps(["爸爸"], ensure_ascii=False),
            ),
        )
        legacy_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO pending_reminder_extract(group_id,user_id,message_id,text,created_at,status) "
            "VALUES (?,?,?,?,?,'pending')",
            ("G1", "U_DAD", "COMMAND1", command_text, source_ts + 84),
        )
        pending_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "CREATE TRIGGER fail_contextual_batch BEFORE INSERT ON reminders "
            "WHEN NEW.source_ref='COMMAND1:lead:1' "
            "BEGIN SELECT RAISE(ABORT,'injected batch failure'); END"
        )
    specs = []
    for slot, day, action in (
        ("lead:0", 15, "爸爸 11/16 14:30 測試牙科例行檢查（前一天提醒）"),  # privacy-safe-fixture
        ("same:0", 16, "爸爸 11/16 14:30 測試牙科例行檢查（當天提醒）"),  # privacy-safe-fixture
        ("lead:1", 23, "爸爸 11/24 測試回診確認結果（前一天提醒）"),
        ("same:1", 24, "爸爸 11/24 測試回診確認結果（當天提醒）"),
    ):
        specs.append(
            {
                "action": action,
                "remind_at": int(
                    datetime(2099, 11, day, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
                ),
                "source_kind": "contextual_date_once",
                "source_ref": f"COMMAND1:{slot}",
                "mention_aliases": ["爸爸"],
            }
        )
    plan = {
        "source_message_id": "SOURCE1",
        "command_message_id": "COMMAND1",
        "source_text": source_text,
        "command_text": command_text,
        "legacy_expected_remind_at": legacy_time,
        "reminders": specs,
    }
    plan["legacy_expected"] = memory.get_contextual_legacy_reminder(
        "G1", "U_DAD", source_text
    )
    claim = memory.claim_pending_reminder(pending_id)
    assert claim

    with pytest.raises(sqlite3.IntegrityError, match="injected batch failure"):
        memory.complete_contextual_date_reminder_batch(
            group_id="G1",
            user_id="U_DAD",
            plan=plan,
            pending_id=pending_id,
            pending_claim_token=claim,
            legacy_reminder_id=legacy_id,
        )

    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute(
            "SELECT reminder_id,action,remind_at,source_kind,source_ref FROM reminders"
        ).fetchall()
        pending_status = conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
    assert rows == [(legacy_id, legacy_action, legacy_time, "", "")]
    assert pending_status == "processing"


def test_stale_cleanup_preserves_same_day_contextual_catchup(temp_db, monkeypatch):
    import memory

    now_dt = datetime(2099, 11, 15, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    remind_at = int(
        datetime(2099, 11, 15, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    monkeypatch.setattr(memory._time, "time", lambda: now_dt.timestamp())
    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref) VALUES (?,?,?,?,1,'pending',?,?)",
            [
                ("G1", "U1", "指定日期", remind_at, "contextual_date_once", "C1:lead:0"),
                ("G1", "U1", "一般來源提醒", remind_at, "other_source", "O1"),
            ],
        )

    memory.delete_stale_pending_reminders(grace_seconds=3600, group_id="G1")

    with sqlite3.connect(temp_db) as conn:
        statuses = dict(conn.execute("SELECT action,status FROM reminders"))
    assert statuses == {"指定日期": "pending", "一般來源提醒": "expired"}


def test_repair_backup_is_created_private_and_consistent(temp_db, tmp_path):
    from scripts import repair_contextual_date_reminder

    destination = tmp_path / "contextual-reminder.sqlite3"
    repair_contextual_date_reminder._backup_database(destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_contextual_handler_commits_once_and_never_queues_or_calls_model(
    temp_db,
    monkeypatch,
):
    import main

    source_text = "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。"
    command_text = "咪寶：麻煩11月15日及23日以及當天提醒，謝謝！"
    source_ts = int(datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    for mid, uid, text, ts in (
        ("SOURCE1", "U_DAD", source_text, source_ts),
        ("BOT1", "__bot__", "已新增提醒", source_ts + 1),
        ("COMMAND1", "U_DAD", command_text, source_ts + 84),
    ):
        _insert_raw(
            temp_db,
            group_id="G1",
            message_id=mid,
            user_id=uid,
            text=text,
            created_at=ts,
        )
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")
    reply = MagicMock()
    monkeypatch.setattr(main, "_reply", reply)
    event = MagicMock()
    event.reply_token = "TOKEN1"

    assert main._try_handle_contextual_date_reminder(
        event,
        "G1",
        command_text,
        "U_DAD",
        "COMMAND1",
    )

    reply.assert_called_once()
    assert reply.call_args.args[1].startswith("已新增 4 筆提醒\n")
    assert "2099-11-16 12:00 爸爸 11/16 14:30" in reply.call_args.args[1]
    assert reply.call_args.kwargs["allow_push_fallback"] is False
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM pending_reminder_extract").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reminder_confirmation_outbox").fetchone()[0] == 0


def test_local_pending_drain_completes_contextual_batch_without_model_or_outbox(
    temp_db,
    monkeypatch,
):
    import gemini_client
    import main

    source_text = "我11月16日下午兩點半測試牙科例行檢查，24日測試回診確認結果。"
    command_text = "咪寶：麻煩11月15日及23日以及當天提醒，謝謝！"
    source_ts = int(datetime(2099, 8, 28, 14, 22, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    for mid, uid, text, ts in (
        ("SOURCE1", "U_DAD", source_text, source_ts),
        ("BOT1", "__bot__", "已新增提醒", source_ts + 1),
        ("COMMAND1", "U_DAD", command_text, source_ts + 84),
    ):
        _insert_raw(
            temp_db,
            group_id="G1",
            message_id=mid,
            user_id=uid,
            text=text,
            created_at=ts,
        )
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref,source_text,mention_aliases) "
            "VALUES (?,?,?,?,?,'pending','','',?,?)",
            (
                "G1", "U_DAD", "爸爸測試牙科例行檢查，24日測試回診確認結果。",
                int(datetime(2099, 11, 16, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()),
                source_ts, source_text, json.dumps(["爸爸"], ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO pending_reminder_extract(group_id,user_id,message_id,text,created_at,status) "
            "VALUES (?,?,?,?,?,'pending')",
            ("G1", "U_DAD", "COMMAND1", command_text, source_ts + 84),
        )
    monkeypatch.setattr(main, "_alias_from_user_id", lambda _uid: "爸爸")
    monkeypatch.setattr(
        gemini_client,
        "extract_reminder",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("model called")),
    )

    main._drain_pending_reminders("G1", local_only=True)

    with sqlite3.connect(temp_db) as conn:
        reminders = conn.execute(
            "SELECT action,source_kind FROM reminders ORDER BY remind_at"
        ).fetchall()
        pending_status = conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE message_id='COMMAND1'"
        ).fetchone()[0]
        outbox_count = conn.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox"
        ).fetchone()[0]
    assert len(reminders) == 4
    assert all(source_kind == "contextual_date_once" for _action, source_kind in reminders)
    assert pending_status == "done"
    assert outbox_count == 0
