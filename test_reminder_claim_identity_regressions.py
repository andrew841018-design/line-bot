"""Regression coverage for reminder identity, dedupe, and legacy event mirrors."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import timedelta

import pytest


@pytest.fixture
def isolated_reminder_db(tmp_path, monkeypatch):
    import calendar_db
    import memory

    db_path = tmp_path / "reminder-claim-identity.db"
    monkeypatch.setattr(memory, "_DB_PATH", db_path)
    monkeypatch.setattr(calendar_db, "_DB_PATH", db_path)
    memory._init_db()
    calendar_db.init_db()
    return db_path, memory, calendar_db


def _insert_reminder(
    db_path,
    *,
    action: str,
    remind_at: int,
    status: str = "pending",
    source_kind: str = "",
    source_ref: str = "",
) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO reminders("
            "group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases"
            ") VALUES ('G1', 'U1', ?, ?, 1, ?, ?, ?, ?, '[]')",
            (
                action,
                remind_at,
                status,
                source_kind,
                source_ref,
                action,
            ),
        )
        return int(cursor.lastrowid)


def _insert_legacy_event(
    db_path,
    calendar_db,
    *,
    event_id: str,
    days_ahead: int = 1,
) -> dict:
    event_date = (calendar_db._today_tw() + timedelta(days=days_ahead)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO events("
            "event_id, group_id, title, event_date, event_time, location, "
            "participants, source_msg_id, status, created_at, event_type, "
            "reminder_lead_days"
            ") VALUES (?, 'G1', '舊行事曆聚餐', ?, '18:00', '台北', ?, '', "
            "'active', ?, 'family_gathering', 0)",
            (
                event_id,
                event_date,
                json.dumps(["媽媽"], ensure_ascii=False),
                int(time.time()),
            ),
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.parametrize("claim_state", ["sending", "uncertain"])
def test_dedupe_keeps_claimed_identity_and_moves_source_onto_it(
    isolated_reminder_db,
    claim_state,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    action = "查看租金是否入帳"
    remind_at = 1_800_000_000
    claimed_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
    )
    source_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-claimed-cluster",
    )

    claim = memory.claim_natural_reminder_delivery(
        "G1",
        claimed_id,
        "1d",
        expected_action=action,
        expected_remind_at=remind_at,
        transport="push",
    )
    assert claim is not None
    if claim_state == "uncertain":
        assert memory.mark_reminder_delivery_claim_uncertain(claim)

    assert memory.delete_duplicate_pending_reminders("G1") == 1
    with sqlite3.connect(db_path) as conn:
        reminder_rows = conn.execute(
            "SELECT reminder_id, status, source_kind, source_ref "
            "FROM reminders WHERE reminder_id IN (?, ?) ORDER BY reminder_id",
            (claimed_id, source_id),
        ).fetchall()
    assert reminder_rows == [
        (
            claimed_id,
            "pending",
            "calendar_event",
            "event-claimed-cluster",
        )
    ]

    source_rows = memory.list_reminder_source_cancellation_candidates(
        "G1",
        "calendar_event",
        "event-claimed-cluster",
    )
    assert [row["reminder_id"] for row in source_rows] == [claimed_id]
    cancelled = memory.cancel_reminder_for_source(
        "G1",
        claimed_id,
        action,
        remind_at,
        "calendar_event",
        "event-claimed-cluster",
        "pending",
    )
    assert cancelled is not None
    assert cancelled["_delivery_in_flight"] is True
    assert cancelled["_delivery_state"] == claim_state
    with sqlite3.connect(db_path) as conn:
        persisted_claim = conn.execute(
            "SELECT subject_ref, state, source_kind, source_ref "
            "FROM reminder_delivery_claims "
            "WHERE group_id='G1'",
        ).fetchone()
    assert persisted_claim == (
        str(claimed_id),
        claim_state,
        "calendar_event",
        "event-claimed-cluster",
    )
    assert memory.claim_natural_reminder_delivery(
        "G1",
        source_id,
        "1d",
        expected_action=action,
        expected_remind_at=remind_at,
        transport="push",
    ) is None


def test_semantic_duplicate_cannot_claim_same_occurrence_before_dedupe(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    action = "查看租金是否入帳"
    remind_at = 1_800_000_000
    first_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
    )
    second_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at + 30,
        source_kind="calendar_event",
        source_ref="event-racing-insert",
    )
    first_claim = memory.claim_natural_reminder_delivery(
        "G1",
        first_id,
        "1d",
        expected_action=action,
        expected_remind_at=remind_at,
        transport="push",
    )
    assert first_claim is not None

    assert memory.claim_natural_reminder_delivery(
        "G1",
        second_id,
        "1d",
        expected_action=action,
        expected_remind_at=remind_at + 30,
        transport="push",
    ) is None


def test_asr_equivalent_duplicate_cannot_claim_same_occurrence(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    remind_at = 1_800_000_000
    first_id = _insert_reminder(
        db_path,
        action="查經",
        remind_at=remind_at,
    )
    second_id = _insert_reminder(
        db_path,
        action="茶几",
        remind_at=remind_at + 30,
    )
    assert memory.claim_natural_reminder_delivery(
        "G1",
        first_id,
        "1d",
        expected_action="查經",
        expected_remind_at=remind_at,
        transport="push",
    )
    assert memory.claim_natural_reminder_delivery(
        "G1",
        second_id,
        "1d",
        expected_action="茶几",
        expected_remind_at=remind_at + 30,
        transport="push",
    ) is None


def test_weekly_duplicate_with_reset_counter_cannot_claim_concurrently(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    action = "遠期租金確認"
    remind_at = 1_900_000_000
    first_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
    )
    second_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-weekly-race",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE reminders SET weekly_count=1 WHERE reminder_id=?",
            (first_id,),
        )

    first_claim = memory.claim_natural_reminder_delivery(
        "G1",
        first_id,
        "weekly",
        expected_action=action,
        expected_remind_at=remind_at,
        expected_weekly_count=1,
        transport="push",
    )
    assert first_claim is not None
    assert first_claim["occurrence"] == "weekly:1"

    assert memory.claim_natural_reminder_delivery(
        "G1",
        second_id,
        "weekly",
        expected_action=action,
        expected_remind_at=remind_at,
        expected_weekly_count=0,
        transport="push",
    ) is None


def test_weekly_claim_blocks_merged_counter_on_same_claimed_identity(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    action = "遠期租金確認"
    remind_at = 1_900_000_000
    claimed_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
    )
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        claimed_id,
        "weekly",
        expected_action=action,
        expected_remind_at=remind_at,
        expected_weekly_count=0,
        transport="push",
    )
    assert claim is not None

    duplicate_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-weekly-merged-counter",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE reminders SET weekly_count=1 WHERE reminder_id=?",
            (duplicate_id,),
        )
    assert memory.delete_duplicate_pending_reminders("G1") == 1
    survivor = memory.get_reminder(claimed_id)
    assert survivor is not None
    assert survivor["source_ref"] == "event-weekly-merged-counter"
    with sqlite3.connect(db_path) as conn:
        weekly_count = conn.execute(
            "SELECT weekly_count FROM reminders WHERE reminder_id=?",
            (claimed_id,),
        ).fetchone()[0]
    assert weekly_count == 0

    assert memory.claim_natural_reminder_delivery(
        "G1",
        claimed_id,
        "weekly",
        expected_action=action,
        expected_remind_at=remind_at,
        expected_weekly_count=0,
        transport="push",
    ) is None
    with sqlite3.connect(db_path) as conn:
        active_claims = conn.execute(
            "SELECT occurrence FROM reminder_delivery_claims "
            "WHERE group_id='G1' AND delivery_kind='natural'",
        ).fetchall()
    assert active_claims == [("weekly:0",)]

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE reminder_delivery_claims SET claimed_at=1 "
            "WHERE group_id='G1' AND subject_ref=?",
            (str(claimed_id),),
        )
    reclaimed = memory.claim_natural_reminder_delivery(
        "G1",
        claimed_id,
        "weekly",
        expected_action=action,
        expected_remind_at=remind_at,
        expected_weekly_count=0,
        transport="push",
    )
    assert reclaimed is not None
    assert reclaimed["retry_key"] == claim["retry_key"]


def test_claimed_duplicate_cancel_finalize_cannot_leave_second_sender(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    action = "查看租金是否入帳"
    remind_at = 1_800_000_000
    claimed_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
    )
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        claimed_id,
        "1d",
        expected_action=action,
        expected_remind_at=remind_at,
        transport="push",
    )
    assert claim is not None
    memory.upsert_reminder_for_source(
        "G1",
        "U1",
        action,
        remind_at,
        "calendar_event",
        "event-finalize-race",
        action,
    )

    source_rows = memory.list_reminder_source_cancellation_candidates(
        "G1",
        "calendar_event",
        "event-finalize-race",
    )
    assert [row["reminder_id"] for row in source_rows] == [claimed_id]
    cancelled = memory.cancel_reminder_for_source(
        "G1",
        claimed_id,
        action,
        remind_at,
        "calendar_event",
        "event-finalize-race",
        "pending",
    )
    assert cancelled is not None
    assert cancelled["_delivery_in_flight"] is True
    assert memory.finalize_natural_reminder_delivery(claim)
    assert memory.get_reminder(claimed_id)["status"] == "cancelled"
    assert memory.list_reminder_cancellation_candidates("G1") == []


def test_source_cancel_before_dedupe_tombstones_claimed_generic_peer(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    action = "查看租金是否入帳"
    remind_at = 1_800_000_000
    generic_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
    )
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        generic_id,
        "1d",
        expected_action=action,
        expected_remind_at=remind_at,
        transport="push",
    )
    assert claim is not None
    source_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-cancel-before-dedupe",
    )

    cancelled = memory.cancel_reminder_for_source(
        "G1",
        source_id,
        action,
        remind_at,
        "calendar_event",
        "event-cancel-before-dedupe",
        "pending",
    )
    assert cancelled is not None
    assert cancelled["_delivery_in_flight"] is True
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT reminder_id, status FROM reminders "
            "WHERE reminder_id IN (?, ?) ORDER BY reminder_id",
            (generic_id, source_id),
        ).fetchall()
    assert rows == [(generic_id, "cancelled"), (source_id, "cancelled")]

    assert memory.finalize_natural_reminder_delivery(claim)
    assert memory.get_reminder(generic_id)["status"] == "cancelled"
    assert memory.list_reminder_cancellation_candidates("G1") == []


def test_source_cancel_covers_nfkc_equivalent_generic_peer(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    remind_at = 1_800_000_000
    generic_id = _insert_reminder(
        db_path,
        action="ABC",
        remind_at=remind_at,
    )
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        generic_id,
        "1d",
        expected_action="ABC",
        expected_remind_at=remind_at,
        transport="push",
    )
    assert claim is not None
    source_id = _insert_reminder(
        db_path,
        action="ＡＢＣ",
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-nfkc-race",
    )

    cancelled = memory.cancel_reminder_for_source(
        "G1",
        source_id,
        "ＡＢＣ",
        remind_at,
        "calendar_event",
        "event-nfkc-race",
        "pending",
    )
    assert cancelled is not None
    assert cancelled["_delivery_in_flight"] is True
    with sqlite3.connect(db_path) as conn:
        statuses = conn.execute(
            "SELECT status FROM reminders WHERE reminder_id IN (?, ?) "
            "ORDER BY reminder_id",
            (generic_id, source_id),
        ).fetchall()
    assert statuses == [("cancelled",), ("cancelled",)]


def test_dedupe_never_merges_distinct_nfkc_equivalent_source_identities(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    remind_at = 1_800_000_000
    first_id = _insert_reminder(
        db_path,
        action="ABC",
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-ascii",
    )
    second_id = _insert_reminder(
        db_path,
        action="ＡＢＣ",
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-fullwidth",
    )

    assert memory.delete_duplicate_pending_reminders("G1") == 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT reminder_id, source_ref FROM reminders "
            "WHERE reminder_id IN (?, ?) ORDER BY reminder_id",
            (first_id, second_id),
        ).fetchall()
    assert rows == [
        (first_id, "event-ascii"),
        (second_id, "event-fullwidth"),
    ]


def test_generic_cancel_does_not_cross_multiple_distinct_source_identities(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    remind_at = 1_800_000_000
    generic_id = _insert_reminder(
        db_path,
        action="ABC",
        remind_at=remind_at,
    )
    first_source_id = _insert_reminder(
        db_path,
        action="ＡＢＣ",
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-source-one",
    )
    second_source_id = _insert_reminder(
        db_path,
        action="abc",
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-source-two",
    )

    assert memory.cancel_pending_reminder(
        "G1",
        generic_id,
        "ABC",
        remind_at,
    )
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT reminder_id, status FROM reminders "
            "WHERE reminder_id IN (?, ?, ?) ORDER BY reminder_id",
            (generic_id, first_source_id, second_source_id),
        ).fetchall()
    assert rows == [
        (generic_id, "cancelled"),
        (first_source_id, "pending"),
        (second_source_id, "pending"),
    ]


def test_safe_dedupe_migrates_sent_refs_to_source_backed_keeper(
    isolated_reminder_db,
):
    db_path, memory, _calendar_db = isolated_reminder_db
    action = "全家聚餐"
    remind_at = 1_800_100_000
    generic_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
    )
    source_id = _insert_reminder(
        db_path,
        action=action,
        remind_at=remind_at,
        source_kind="calendar_event",
        source_ref="event-source-keeper",
    )
    assert generic_id < source_id
    assert memory.log_sent_reminder_reference(
        "G1",
        "outbound-duplicate",
        reminder_id=generic_id,
    )

    assert memory.delete_duplicate_pending_reminders("G1") == 1
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT reminder_id, source_kind, source_ref FROM reminders "
            "WHERE group_id='G1'",
        ).fetchall()
    assert rows == [(source_id, "calendar_event", "event-source-keeper")]
    migrated_ref = memory.get_sent_reminder_reference(
        "G1",
        "outbound-duplicate",
    )
    assert migrated_ref is not None
    assert migrated_ref["reminder_id"] == source_id
    with sqlite3.connect(db_path) as conn:
        resolved_source = conn.execute(
            "SELECT source_kind, source_ref FROM reminders WHERE reminder_id=?",
            (migrated_ref["reminder_id"],),
        ).fetchone()
    assert resolved_source == ("calendar_event", "event-source-keeper")


def test_legacy_due_event_mirror_is_created_once_and_can_be_claimed(
    isolated_reminder_db,
):
    db_path, memory, calendar_db = isolated_reminder_db
    event_id = "legacy-due-event"
    event = _insert_legacy_event(
        db_path,
        calendar_db,
        event_id=event_id,
    )

    assert calendar_db.ensure_event_reminder_mirror(event)
    assert calendar_db.ensure_event_reminder_mirror(event)
    due = calendar_db.list_due_for_reminder("G1", 1)
    assert [row["event_id"] for row in due] == [event_id]

    with sqlite3.connect(db_path) as conn:
        mirrors = conn.execute(
            "SELECT reminder_id, status FROM reminders "
            "WHERE group_id='G1' AND source_kind='calendar_event' "
            "AND source_ref=?",
            (event_id,),
        ).fetchall()
    assert len(mirrors) == 1
    assert mirrors[0][1] == "pending"

    claim = memory.claim_calendar_reminder_delivery(
        "G1",
        "calendar_event",
        event_id,
        1,
        expected_title=str(event["title"]),
        expected_event_date=str(event["event_date"]),
        expected_event_time=str(event["event_time"]),
        expected_location=str(event.get("location") or ""),
        expected_participants=str(event.get("participants") or "[]"),
        transport="push",
    )
    assert claim is not None
    assert memory.release_reminder_delivery_claim(claim)


def test_calendar_offset_zero_claim_can_be_finalized(
    isolated_reminder_db,
):
    db_path, memory, calendar_db = isolated_reminder_db
    event_id = "calendar-offset-zero"
    event = _insert_legacy_event(
        db_path,
        calendar_db,
        event_id=event_id,
        days_ahead=0,
    )
    assert calendar_db.ensure_event_reminder_mirror(event)

    claim = memory.claim_calendar_reminder_delivery(
        "G1",
        "calendar_event",
        event_id,
        0,
        expected_title=str(event["title"]),
        expected_event_date=str(event["event_date"]),
        expected_event_time=str(event["event_time"]),
        expected_location=str(event.get("location") or ""),
        expected_participants=str(event.get("participants") or "[]"),
        transport="reply",
    )
    assert claim is not None
    assert claim["offset"] == 0

    assert memory.finalize_calendar_reminder_delivery(claim)
    with sqlite3.connect(db_path) as conn:
        event_state = conn.execute(
            "SELECT reminded_0d, reminded_1d, reminded_2d, reminded_3d, "
            "reminded_7d, reminded_30d FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        persisted_claim = conn.execute(
            "SELECT 1 FROM reminder_delivery_claims WHERE claim_token=?",
            (claim["claim_token"],),
        ).fetchone()
    assert event_state is not None
    assert event_state[0] is not None
    assert event_state[1:] == (None, None, None, None, None)
    assert persisted_claim is None
    assert memory.finalize_calendar_reminder_delivery(claim) is False


@pytest.mark.parametrize("invalid_offset", [None, "", "invalid", False, 4])
def test_calendar_finalize_rejects_invalid_offset_without_releasing_claim(
    isolated_reminder_db,
    invalid_offset,
):
    db_path, memory, calendar_db = isolated_reminder_db
    event_id = f"invalid-calendar-offset-{invalid_offset!r}"
    event = _insert_legacy_event(
        db_path,
        calendar_db,
        event_id=event_id,
        days_ahead=0,
    )
    assert calendar_db.ensure_event_reminder_mirror(event)
    claim = memory.claim_calendar_reminder_delivery(
        "G1",
        "calendar_event",
        event_id,
        0,
        expected_title=str(event["title"]),
        expected_event_date=str(event["event_date"]),
        expected_event_time=str(event["event_time"]),
        expected_location=str(event.get("location") or ""),
        expected_participants=str(event.get("participants") or "[]"),
        transport="reply",
    )
    assert claim is not None
    malformed_claim = {**claim, "offset": invalid_offset}

    assert memory.finalize_calendar_reminder_delivery(malformed_claim) is False
    with sqlite3.connect(db_path) as conn:
        event_state = conn.execute(
            "SELECT reminded_0d FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        claim_state = conn.execute(
            "SELECT state FROM reminder_delivery_claims WHERE claim_token=?",
            (claim["claim_token"],),
        ).fetchone()
    assert event_state == (None,)
    assert claim_state == ("sending",)
    assert memory.release_reminder_delivery_claim(claim)


@pytest.mark.parametrize("terminal_status", ["done", "expired", "cancelled"])
def test_legacy_mirror_ensure_never_revives_terminal_source_rows(
    isolated_reminder_db,
    terminal_status,
):
    db_path, _memory, calendar_db = isolated_reminder_db
    event_id = f"legacy-{terminal_status}"
    event = _insert_legacy_event(
        db_path,
        calendar_db,
        event_id=event_id,
    )
    reminder_id = _insert_reminder(
        db_path,
        action="保留舊提醒內容",
        remind_at=123,
        status=terminal_status,
        source_kind="calendar_event",
        source_ref=event_id,
    )
    with sqlite3.connect(db_path) as conn:
        before = conn.execute(
            "SELECT reminder_id, action, remind_at, status, source_kind, source_ref "
            "FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()

    assert calendar_db.ensure_event_reminder_mirror(event)
    assert calendar_db.ensure_event_reminder_mirror(event)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT reminder_id, action, remind_at, status, source_kind, source_ref "
            "FROM reminders WHERE group_id='G1' AND source_kind='calendar_event' "
            "AND source_ref=?",
            (event_id,),
        ).fetchall()
    assert rows == [before]
