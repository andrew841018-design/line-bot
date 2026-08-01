"""Focused persistence tests for natural-language reminder cancellation."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def reminder_db(tmp_path, monkeypatch):
    import memory

    db_path = tmp_path / "reminder-cancel.db"
    monkeypatch.setattr(memory, "_DB_PATH", db_path)
    memory._init_db()
    return db_path


def _insert_reminder(
    db_path,
    *,
    group_id: str = "G1",
    action: str = "查看租金是否入帳",
    remind_at: int = 1_800_000_000,
    status: str = "pending",
    source_kind: str = "",
    source_ref: str = "",
) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO reminders("
            "group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases"
            ") VALUES (?, 'U1', ?, ?, 1, ?, ?, ?, ?, '[]')",
            (
                group_id,
                action,
                remind_at,
                status,
                source_kind,
                source_ref,
                action,
            ),
        )
        return int(cursor.lastrowid)


def _row(db_path, reminder_id: int):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT action, remind_at, status, pushed_1d, weekly_count "
            "FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()


def test_cancellation_candidate_read_is_group_scoped_pure_and_has_no_cutoff(
    reminder_db,
):
    import memory

    stale_id = _insert_reminder(
        reminder_db,
        action="很久以前仍待處理",
        remind_at=1,
    )
    duplicate_id = _insert_reminder(
        reminder_db,
        action="很久以前仍待處理",
        remind_at=1,
    )
    cancelled_id = _insert_reminder(
        reminder_db,
        action="已取消",
        status="cancelled",
    )
    _insert_reminder(reminder_db, group_id="G2", action="別群提醒")
    done_id = _insert_reminder(reminder_db, action="已完成", status="done")

    before = _all_rows(reminder_db)
    pending = memory.list_reminder_cancellation_candidates("G1")
    after = _all_rows(reminder_db)

    assert [row["reminder_id"] for row in pending] == [stale_id, duplicate_id]
    assert all(row["status"] == "pending" for row in pending)
    assert before == after, "候選讀取不得順便去重、過期或修改 reminder"

    with_cancelled = memory.list_reminder_cancellation_candidates(
        "G1", include_cancelled=True
    )
    assert {row["reminder_id"] for row in with_cancelled} == {
        stale_id,
        duplicate_id,
        cancelled_id,
    }
    assert {row["group_id"] for row in with_cancelled} == {"G1"}

    with_terminal = memory.list_reminder_cancellation_candidates(
        "G1",
        include_cancelled=True,
        include_terminal=True,
    )
    assert {row["reminder_id"] for row in with_terminal} == {
        stale_id,
        duplicate_id,
        cancelled_id,
        done_id,
    }


def test_sent_reminder_reference_is_group_scoped(reminder_db):
    import memory

    assert memory.log_sent_reminder_reference(
        "G1",
        "sent-1",
        reminder_id=42,
    )
    assert memory.get_sent_reminder_reference("G1", "sent-1") == {
        "reminder_id": 42,
        "source_kind": "",
        "source_ref": "",
    }
    assert memory.get_sent_reminder_reference("G2", "sent-1") is None
    assert memory.log_sent_reminder_reference(
        "G1",
        "sent-event",
        source_kind="calendar_event",
        source_ref="event-7",
    )
    assert memory.get_sent_reminder_reference("G1", "sent-event") == {
        "reminder_id": None,
        "source_kind": "calendar_event",
        "source_ref": "event-7",
    }


def test_cancel_first_blocks_natural_delivery_claim(reminder_db):
    import memory

    reminder_id = _insert_reminder(reminder_db)
    cancelled = memory.cancel_pending_reminder(
        "G1",
        reminder_id,
        "查看租金是否入帳",
        1_800_000_000,
    )

    assert cancelled is not None
    assert memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action="查看租金是否入帳",
        expected_remind_at=1_800_000_000,
        transport="push",
    ) is None


def test_cancel_unique_reminder_for_local_date_is_group_scoped(reminder_db):
    import memory

    target_id = _insert_reminder(
        reminder_db,
        group_id="G1",
        action="目標",
        remind_at=2_000,
    )
    adjacent_id = _insert_reminder(
        reminder_db,
        group_id="G1",
        action="目標",
        remind_at=999,
    )
    other_group_id = _insert_reminder(
        reminder_db,
        group_id="G2",
        action="別群",
        remind_at=2_000,
    )

    result = memory.cancel_unique_reminder_for_local_date(
        "G1", "2030-08-08", 1_000, 3_000
    )

    assert result["status"] == "cancelled"
    assert result["reminder"]["reminder_id"] == target_id
    assert _row(reminder_db, target_id)[2] == "cancelled"
    assert _row(reminder_db, adjacent_id)[2] == "pending"
    assert _row(reminder_db, other_group_id)[2] == "pending"


def test_cancel_unique_reminder_for_local_date_refuses_multiple(reminder_db):
    import memory

    first_id = _insert_reminder(
        reminder_db,
        action="第一件",
        remind_at=2_000,
    )
    second_id = _insert_reminder(
        reminder_db,
        action="第二件",
        remind_at=2_500,
    )

    result = memory.cancel_unique_reminder_for_local_date(
        "G1", "2030-08-08", 1_000, 3_000
    )

    assert result == {"status": "ambiguous", "count": 2, "reminder": None}
    assert _row(reminder_db, first_id)[2] == "pending"
    assert _row(reminder_db, second_id)[2] == "pending"


def test_cancel_unique_reminder_for_local_date_cancels_semantic_cluster(
    reminder_db,
):
    import memory

    first_id = _insert_reminder(
        reminder_db,
        action="同一件事",
        remind_at=2_000,
    )
    duplicate_id = _insert_reminder(
        reminder_db,
        action="同一件事",
        remind_at=2_050,
    )

    result = memory.cancel_unique_reminder_for_local_date(
        "G1", "2030-08-08", 1_000, 3_000
    )

    assert result["status"] == "cancelled"
    assert _row(reminder_db, first_id)[2] == "cancelled"
    assert _row(reminder_db, duplicate_id)[2] == "cancelled"


def test_claim_first_cancellation_is_truthful_and_preserved_on_finalize(
    reminder_db,
):
    import memory

    reminder_id = _insert_reminder(reminder_db)
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action="查看租金是否入帳",
        expected_remind_at=1_800_000_000,
        transport="push",
    )
    assert claim is not None

    cancelled = memory.cancel_pending_reminder(
        "G1",
        reminder_id,
        "查看租金是否入帳",
        1_800_000_000,
    )
    assert cancelled is not None
    assert cancelled["_delivery_in_flight"] is True

    assert memory.finalize_natural_reminder_delivery(claim)
    row = _row(reminder_db, reminder_id)
    assert row[2] == "cancelled"
    assert row[3] == 1


def test_generic_row_cancellation_sees_active_source_delivery(reminder_db):
    import memory

    reminder_id = _insert_reminder(
        reminder_db,
        source_kind="calendar_event",
        source_ref="event-1",
    )
    with sqlite3.connect(reminder_db) as conn:
        conn.execute(
            "INSERT INTO reminder_delivery_claims("
            "group_id, delivery_kind, subject_ref, occurrence, source_kind, "
            "source_ref, transport, state, claim_token, retry_key, claimed_at"
            ") VALUES ("
            "'G1', 'calendar', 'event-1', 'calendar:1', 'calendar_event', "
            "'event-1', 'push', 'sending', 'token', 'retry', 1"
            ")"
        )

    cancelled = memory.cancel_pending_reminder(
        "G1",
        reminder_id,
        "查看租金是否入帳",
        1_800_000_000,
    )

    assert cancelled is not None
    assert cancelled["_delivery_in_flight"] is True


def test_only_one_sender_claims_same_natural_occurrence(reminder_db):
    import memory

    reminder_id = _insert_reminder(reminder_db)

    def claim_once():
        return memory.claim_natural_reminder_delivery(
            "G1",
            reminder_id,
            "1d",
            expected_action="查看租金是否入帳",
            expected_remind_at=1_800_000_000,
            transport="push",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _index: claim_once(), range(2)))

    assert sum(claim is not None for claim in claims) == 1


def test_definite_failure_release_reuses_stable_retry_key(reminder_db):
    import memory

    reminder_id = _insert_reminder(reminder_db)
    first = memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action="查看租金是否入帳",
        expected_remind_at=1_800_000_000,
        transport="push",
    )
    assert first is not None
    assert memory.release_reminder_delivery_claim(first)

    second = memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action="查看租金是否入帳",
        expected_remind_at=1_800_000_000,
        transport="push",
    )
    assert second is not None
    assert second["claim_token"] != first["claim_token"]
    assert second["retry_key"] == first["retry_key"]


def test_wrong_claim_token_cannot_release_delivery(reminder_db):
    import memory

    reminder_id = _insert_reminder(reminder_db)
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action="查看租金是否入帳",
        expected_remind_at=1_800_000_000,
        transport="push",
    )
    assert claim is not None
    forged = {**claim, "claim_token": "wrong"}

    assert not memory.release_reminder_delivery_claim(forged)
    assert memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action="查看租金是否入帳",
        expected_remind_at=1_800_000_000,
        transport="push",
    ) is None


def test_stale_push_claim_reuses_retry_key_but_stale_reply_is_fenced(
    reminder_db,
):
    import memory

    push_id = _insert_reminder(reminder_db, action="推播提醒")
    push_claim = memory.claim_natural_reminder_delivery(
        "G1",
        push_id,
        "1d",
        expected_action="推播提醒",
        expected_remind_at=1_800_000_000,
        transport="push",
    )
    assert push_claim is not None
    with sqlite3.connect(reminder_db) as conn:
        conn.execute(
            "UPDATE reminder_delivery_claims SET claimed_at=1 "
            "WHERE subject_ref=?",
            (str(push_id),),
        )
    reclaimed = memory.claim_natural_reminder_delivery(
        "G1",
        push_id,
        "1d",
        expected_action="推播提醒",
        expected_remind_at=1_800_000_000,
        transport="push",
    )
    assert reclaimed is not None
    assert reclaimed["retry_key"] == push_claim["retry_key"]

    reply_id = _insert_reminder(reminder_db, action="回覆提醒")
    reply_claim = memory.claim_natural_reminder_delivery(
        "G1",
        reply_id,
        "1d",
        expected_action="回覆提醒",
        expected_remind_at=1_800_000_000,
        transport="reply",
    )
    assert reply_claim is not None
    with sqlite3.connect(reminder_db) as conn:
        conn.execute(
            "UPDATE reminder_delivery_claims SET claimed_at=1 "
            "WHERE subject_ref=?",
            (str(reply_id),),
        )
    assert memory.claim_natural_reminder_delivery(
        "G1",
        reply_id,
        "1d",
        expected_action="回覆提醒",
        expected_remind_at=1_800_000_000,
        transport="reply",
    ) is None
    with sqlite3.connect(reminder_db) as conn:
        state = conn.execute(
            "SELECT state FROM reminder_delivery_claims WHERE subject_ref=?",
            (str(reply_id),),
        ).fetchone()[0]
    assert state == "uncertain"


def _all_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT reminder_id, group_id, action, remind_at, status "
            "FROM reminders ORDER BY reminder_id"
        ).fetchall()


def test_cancel_pending_reminder_is_atomic_group_scoped_compare_and_set(reminder_db):
    import memory

    reminder_id = _insert_reminder(reminder_db)

    assert (
        memory.cancel_pending_reminder(
            "G2", reminder_id, "查看租金是否入帳", 1_800_000_000
        )
        is None
    )
    assert (
        memory.cancel_pending_reminder(
            "G1", reminder_id, "不同事項", 1_800_000_000
        )
        is None
    )
    assert (
        memory.cancel_pending_reminder(
            "G1", reminder_id, "查看租金是否入帳", 1_800_000_001
        )
        is None
    )
    assert _row(reminder_db, reminder_id)[2] == "pending"

    cancelled = memory.cancel_pending_reminder(
        "G1", reminder_id, "查看租金是否入帳", 1_800_000_000
    )

    assert cancelled == {
        "reminder_id": reminder_id,
        "group_id": "G1",
        "user_id": "U1",
        "action": "查看租金是否入帳",
        "remind_at": 1_800_000_000,
        "status": "cancelled",
        "source_kind": "",
        "source_ref": "",
        "source_text": "查看租金是否入帳",
        "mention_aliases": [],
    }
    assert _row(reminder_db, reminder_id)[2] == "cancelled"
    assert (
        memory.cancel_pending_reminder(
            "G1", reminder_id, "查看租金是否入帳", 1_800_000_000
        )
        is None
    ), "第二個競爭者或重送不得再次取得 pending CAS"


def test_cancel_pending_reminder_wrong_status_is_noop(reminder_db):
    import memory

    done_id = _insert_reminder(reminder_db, status="done")
    cancelled_id = _insert_reminder(reminder_db, status="cancelled")

    for reminder_id in (done_id, cancelled_id):
        assert (
            memory.cancel_pending_reminder(
                "G1", reminder_id, "查看租金是否入帳", 1_800_000_000
            )
            is None
        )
    assert _row(reminder_db, done_id)[2] == "done"
    assert _row(reminder_db, cancelled_id)[2] == "cancelled"


def test_cancel_pending_reminder_race_has_exactly_one_winner(reminder_db):
    import memory

    reminder_id = _insert_reminder(reminder_db)
    start = threading.Barrier(2)

    def compete():
        start.wait()
        return memory.cancel_pending_reminder(
            "G1", reminder_id, "查看租金是否入帳", 1_800_000_000
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: compete(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert _row(reminder_db, reminder_id)[2] == "cancelled"


def test_pending_and_cancelled_source_helpers_are_group_scoped(reminder_db):
    import memory

    reminder_id = _insert_reminder(
        reminder_db,
        source_kind="calendar_event",
        source_ref="event-7",
    )

    assert memory.is_reminder_pending("G1", reminder_id)
    assert not memory.is_reminder_pending("G2", reminder_id)
    assert not memory.is_reminder_source_cancelled(
        "G1", "calendar_event", "event-7"
    )

    assert memory.cancel_pending_reminder(
        "G1", reminder_id, "查看租金是否入帳", 1_800_000_000
    )
    assert not memory.is_reminder_pending("G1", reminder_id)
    assert memory.is_reminder_source_cancelled(
        "G1", "calendar_event", "event-7"
    )
    assert not memory.is_reminder_source_cancelled(
        "G2", "calendar_event", "event-7"
    )


def test_done_calendar_source_can_be_atomically_tombstoned(reminder_db):
    import memory

    reminder_id = _insert_reminder(
        reminder_db,
        action="預約羽球場",
        remind_at=1_800_000_123,
        status="done",
        source_kind="calendar_event",
        source_ref="event-done",
    )
    _insert_reminder(
        reminder_db,
        group_id="G2",
        action="別群活動",
        status="done",
        source_kind="calendar_event",
        source_ref="event-done",
    )

    source_rows = memory.list_reminder_source_cancellation_candidates(
        "G1",
        "calendar_event",
        "event-done",
    )
    assert [row["reminder_id"] for row in source_rows] == [reminder_id]
    assert source_rows[0]["status"] == "done"

    assert (
        memory.cancel_reminder_for_source(
            "G1",
            reminder_id,
            "預約羽球場",
            1_800_000_123,
            "calendar_event",
            "wrong-event",
            "done",
        )
        is None
    )
    cancelled = memory.cancel_reminder_for_source(
        "G1",
        reminder_id,
        "預約羽球場",
        1_800_000_123,
        "calendar_event",
        "event-done",
        "done",
    )

    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert memory.is_reminder_source_cancelled(
        "G1",
        "calendar_event",
        "event-done",
    )
    assert (
        memory.cancel_pending_reminder(
            "G2",
            reminder_id,
            "預約羽球場",
            1_800_000_123,
        )
        is None
    )


def test_source_upsert_does_not_resurrect_cancelled_tombstone(reminder_db):
    import memory

    reminder_id = memory.upsert_reminder_for_source_any_status(
        "G1",
        "U1",
        "原提醒",
        1_800_000_000,
        "calendar_event",
        "event-7",
        source_text="原提醒",
    )
    assert memory.cancel_pending_reminder(
        "G1", reminder_id, "原提醒", 1_800_000_000
    )

    upserted_id = memory.upsert_reminder_for_source_any_status(
        "G1",
        "U2",
        "同步後內容",
        1_800_100_000,
        "calendar_event",
        "event-7",
        source_text="同步後內容",
    )

    assert upserted_id == reminder_id
    assert _row(reminder_db, reminder_id)[:3] == (
        "原提醒",
        1_800_000_000,
        "cancelled",
    )
    assert memory.is_reminder_source_cancelled(
        "G1", "calendar_event", "event-7"
    )


def test_mark_reminder_pushed_only_updates_pending_and_reports_rowcount(reminder_db):
    import memory

    pending_id = _insert_reminder(reminder_db, action="待發送")
    cancelled_id = _insert_reminder(
        reminder_db, action="已取消", status="cancelled"
    )
    done_id = _insert_reminder(reminder_db, action="已完成", status="done")

    assert memory.mark_reminder_pushed(pending_id, "1d")
    assert _row(reminder_db, pending_id)[3] == 1

    assert not memory.mark_reminder_pushed(cancelled_id, "1d")
    assert not memory.mark_reminder_pushed(cancelled_id, "weekly")
    assert not memory.mark_reminder_pushed(done_id, "1d")
    assert not memory.mark_reminder_pushed(999_999, "1d")
    assert _row(reminder_db, cancelled_id)[3:] == (0, 0)
    assert _row(reminder_db, done_id)[3:] == (0, 0)


def test_mark_reminder_pushed_now_finishes_only_pending_row(reminder_db):
    import memory

    pending_id = _insert_reminder(reminder_db, action="現在")
    cancelled_id = _insert_reminder(
        reminder_db, action="不要發", status="cancelled"
    )

    assert memory.mark_reminder_pushed(pending_id, "now")
    assert _row(reminder_db, pending_id)[2] == "done"
    assert not memory.mark_reminder_pushed(cancelled_id, "now")
    assert _row(reminder_db, cancelled_id)[2] == "cancelled"
