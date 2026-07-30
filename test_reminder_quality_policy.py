from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize(
    ("source", "action"),
    [
        ("8/2晚上可以改到7/31晚上嗎", "晚上嗎"),
        ("你們8/1下午幾點方便打球", "下午幾點方便打球"),
        ("8/2早上可以打球嗎", "早上可以打球嗎"),
        ("爸爸你說8/15要訂哪一家餐廳", "訂餐廳"),
        ("8/15早上九點要不要！", "要不要！"),
        ("8/8早上也可以", "8/8"),
        ("不要提醒我8/18早上看醫生", "看醫生"),
        ("不用新增提醒，8/18早上看醫生", "看醫生"),
    ],
)
def test_noncommittal_candidates_are_rejected(source, action):
    import reminder_intent

    assert reminder_intent.should_reject_reminder_candidate(source, action)


@pytest.mark.parametrize(
    ("source", "action"),
    [
        ("請提醒我8/2早上9點打球好嗎？", "打球"),
        (
            "@雅 @黃將修 @曾美惠 8/8還是15早上你們有空嗎 九點桃園高鐵上皮拉提斯",
            "桃園高鐵上皮拉提斯",
        ),
        ("8/1 17:00 羽球\n你要不要來？", "羽球"),
        ("明天19:00嗎哪小組查經", "嗎哪小組查經"),
    ],
)
def test_explicit_or_independent_committed_candidates_are_kept(source, action):
    import reminder_intent

    assert not reminder_intent.should_reject_reminder_candidate(source, action)


def test_internal_prompt_artifacts_are_rejected():
    import reminder_intent

    assert reminder_intent.has_internal_prompt_artifact(
        "找江福田醫師加號--- 原始訊息 結束 ---(下面是使用者目"
    )


def test_model_calendar_output_with_prompt_artifact_is_rejected():
    import calendar_extractor

    result = calendar_extractor._normalize(
        {
            "has_event": True,
            "is_cancellation": False,
            "title": "看醫生--- 原始訊息 結束 ---(下面是使用者目",
            "date": "2026-08-14",
            "time": "09:00",
            "event_type": "medical",
        }
    )
    assert result["has_event"] is False
    assert result["title"] is None


def test_calendar_regex_rejects_question_candidates():
    import calendar_regex

    today = date(2026, 7, 29)
    assert calendar_regex.extract_many_regex_only(
        "8/1 下午幾點方便打球", today, require_time=False
    ) == []
    assert calendar_regex.extract_many_regex_only(
        "8/2 早上可以打球嗎", today, require_time=False
    ) == []


def test_question_gate_skips_model_queue_and_persistence(monkeypatch, tmp_path):
    import main
    import memory

    db_path = tmp_path / "question-gate.db"
    monkeypatch.setattr(memory, "_DB_PATH", Path(db_path))
    memory._init_db()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("question must be rejected before side effects")

    monkeypatch.setattr(main.gemini_client, "extract_reminder", forbidden)
    monkeypatch.setattr(memory, "add_reminder_with_outcome", forbidden)
    monkeypatch.setattr(memory, "enqueue_pending_reminder", forbidden)
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_args: True)

    assert (
        main._maybe_extract_reminder(
            "爸爸你說8/15要訂哪一家餐廳",
            "G1",
            "U1",
            "m-question",
        )
        is None
    )


def test_pending_question_is_dropped_before_model_call(monkeypatch, tmp_path):
    import main
    import memory

    db_path = tmp_path / "pending-question.db"
    monkeypatch.setattr(memory, "_DB_PATH", Path(db_path))
    memory._init_db()
    memory.enqueue_pending_reminder(
        "G1",
        "U1",
        "8/2早上可以打球嗎",
        "m-question",
    )

    monkeypatch.setattr(
        main.gemini_client,
        "extract_reminder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pending question must not call model")
        ),
    )
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders("G1")

    with memory._conn() as c:
        status = c.execute(
            "SELECT status FROM pending_reminder_extract "
            "WHERE group_id='G1' AND message_id='m-question'"
        ).fetchone()[0]
        reminder_count = c.execute(
            "SELECT COUNT(*) FROM reminders WHERE group_id='G1'"
        ).fetchone()[0]
        outbox_count = c.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox WHERE group_id='G1'"
        ).fetchone()[0]
    assert status == "dropped"
    assert reminder_count == 0
    assert outbox_count == 0


@pytest.fixture
def semantic_calendar_db(tmp_path, monkeypatch):
    import calendar_db
    import config
    import memory

    db_path = tmp_path / "semantic-calendar.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    config.settings.sqlite_path = str(db_path)
    monkeypatch.setattr(memory, "_DB_PATH", Path(db_path))
    memory._init_db()
    monkeypatch.setattr(calendar_db, "_DB_PATH", Path(db_path))
    calendar_db.init_db()
    return calendar_db, memory


def test_semantic_calendar_duplicate_prefers_sourceful_richer_event(
    semantic_calendar_db,
):
    calendar_db, memory = semantic_calendar_db
    event_date = (date.today() + timedelta(days=10)).isoformat()

    synthetic_id, synthetic_outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="回台北",
        event_date=event_date,
        event_time="19:00",
        source_msg_id=None,
        event_type="medical",
    )
    canonical_id, canonical_outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="晚上回台北",
        event_date=event_date,
        event_time=None,
        source_msg_id="m-source",
        event_type="personal_trip",
    )

    assert synthetic_outcome == "created"
    assert canonical_outcome == "merged"
    assert canonical_id == synthetic_id
    event = calendar_db.get_active_event_by_id("G1", canonical_id)
    assert event["title"] == "晚上回台北"
    assert event["event_time"] is None
    assert event["source_msg_id"] == "m-source"
    mirrors = [
        row
        for row in memory.list_pending_reminders_full("G1")
        if row["source_kind"] == "calendar_event"
    ]
    assert len(mirrors) == 1
    assert mirrors[0]["source_ref"] == canonical_id
    assert mirrors[0]["action"] == "晚上回台北"


def test_sourceful_calendar_event_ignores_source_less_synthetic_duplicate(
    semantic_calendar_db,
):
    calendar_db, _memory = semantic_calendar_db
    event_date = (date.today() + timedelta(days=10)).isoformat()

    canonical_id, _ = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="早上找江福田醫師加號 看心律不整",
        event_date=event_date,
        event_time=None,
        source_msg_id="m-source",
        event_type="medical",
    )
    duplicate_id, outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="找江福田醫師加號 看心律不整",
        event_date=event_date,
        event_time="09:00",
        source_msg_id=None,
        event_type="medical",
    )

    assert outcome == "duplicate"
    assert duplicate_id == canonical_id
    event = calendar_db.get_active_event_by_id("G1", canonical_id)
    assert event["title"] == "早上找江福田醫師加號 看心律不整"
    assert event["event_time"] is None


def test_same_title_at_different_explicit_times_remains_distinct(
    semantic_calendar_db,
):
    calendar_db, _memory = semantic_calendar_db
    event_date = (date.today() + timedelta(days=10)).isoformat()

    first_id, first_outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="全家打羽球",
        event_date=event_date,
        event_time="15:00",
    )
    second_id, second_outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="全家打羽球",
        event_date=event_date,
        event_time="17:00",
    )

    assert first_outcome == second_outcome == "created"
    assert first_id != second_id


def test_distinct_sourceful_messages_are_not_silently_merged(
    semantic_calendar_db,
):
    calendar_db, _memory = semantic_calendar_db
    event_date = (date.today() + timedelta(days=10)).isoformat()

    first_id, first_outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="全家壁球",
        event_date=event_date,
        event_time="19:00",
        source_msg_id="m-first",
    )
    second_id, second_outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="全家壁球",
        event_date=event_date,
        event_time="19:00",
        source_msg_id="m-second",
    )

    assert first_outcome == "created"
    assert first_id
    assert second_outcome == "conflict"
    assert second_id == ""


def test_semantic_calendar_dedupe_never_crosses_groups(semantic_calendar_db):
    calendar_db, _memory = semantic_calendar_db
    event_date = (date.today() + timedelta(days=10)).isoformat()

    first_id, _ = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="全家打壁球（19:00-20:00）",
        event_date=event_date,
        event_time="19:00",
    )
    second_id, outcome = calendar_db.insert_event_with_outcome(
        group_id="G2",
        title="全家壁球",
        event_date=event_date,
        event_time="19:00",
    )

    assert outcome == "created"
    assert first_id != second_id


def test_live_delivery_claim_blocks_semantic_event_merge(semantic_calendar_db):
    calendar_db, memory = semantic_calendar_db
    event_date = (date.today() + timedelta(days=10)).isoformat()
    event_id, _ = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="回台北",
        event_date=event_date,
        event_time="19:00",
        source_msg_id=None,
        event_type="personal_trip",
    )
    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminder_delivery_claims("
            "group_id, delivery_kind, subject_ref, occurrence, source_kind, "
            "source_ref, transport, state, claim_token, retry_key, claimed_at"
            ") VALUES ('G1', 'calendar', ?, '0d', 'calendar_event', ?, "
            "'push', 'sending', 'claim', 'retry', ?)",
            (event_id, event_id, int(time.time())),
        )

    merged_id, outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="晚上回台北",
        event_date=event_date,
        event_time=None,
        source_msg_id="m-source",
        event_type="personal_trip",
    )

    assert merged_id == event_id
    assert outcome == "busy"
    event = calendar_db.get_active_event_by_id("G1", event_id)
    assert event["title"] == "回台北"
    assert event["event_time"] == "19:00"
    assert event["source_msg_id"] is None


def test_stale_delivery_claim_is_fenced_during_semantic_merge(
    semantic_calendar_db,
):
    calendar_db, memory = semantic_calendar_db
    event_date = (date.today() + timedelta(days=10)).isoformat()
    event_id, _ = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="回台北",
        event_date=event_date,
        event_time="19:00",
        source_msg_id=None,
        event_type="personal_trip",
    )
    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminder_delivery_claims("
            "group_id, delivery_kind, subject_ref, occurrence, source_kind, "
            "source_ref, transport, state, claim_token, retry_key, claimed_at"
            ") VALUES ('G1', 'calendar', ?, '0d', 'calendar_event', ?, "
            "'push', 'sending', 'claim', 'retry', ?)",
            (event_id, event_id, int(time.time()) - 3600),
        )

    merged_id, outcome = calendar_db.insert_event_with_outcome(
        group_id="G1",
        title="晚上回台北",
        event_date=event_date,
        event_time=None,
        source_msg_id="m-source",
        event_type="personal_trip",
    )

    assert merged_id == event_id
    assert outcome == "merged"
    with memory._conn() as c:
        state = c.execute(
            "SELECT state FROM reminder_delivery_claims "
            "WHERE group_id='G1' AND delivery_kind='calendar' AND subject_ref=?",
            (event_id,),
        ).fetchone()[0]
    assert state == "uncertain"


def test_weak_generic_reminder_is_replaced_by_unique_richer_peer(
    monkeypatch, tmp_path
):
    import memory

    db_path = tmp_path / "weak-reminder.db"
    monkeypatch.setattr(memory, "_DB_PATH", Path(db_path))
    memory._init_db()
    remind_at = int(
        datetime.now(ZoneInfo("Asia/Taipei")).replace(
            microsecond=0
        ).timestamp()
    ) + 86400

    weak_id, weak_outcome = memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "8/8",
        remind_at,
        source_text="8/8早上也可以",
    )
    rich_id, rich_outcome = memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "桃園高鐵上皮拉提斯",
        remind_at,
        source_text="九點桃園高鐵上皮拉提斯",
    )

    assert weak_outcome == "created"
    assert rich_outcome == "merged"
    assert rich_id == weak_id
    assert memory.get_reminder(rich_id)["action"] == "桃園高鐵上皮拉提斯"


def test_weak_incoming_reminder_does_not_replace_richer_peer(
    monkeypatch, tmp_path
):
    import memory

    db_path = tmp_path / "weak-incoming.db"
    monkeypatch.setattr(memory, "_DB_PATH", Path(db_path))
    memory._init_db()
    remind_at = int(datetime.now(ZoneInfo("Asia/Taipei")).timestamp()) + 86400

    rich_id, _ = memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "桃園高鐵上皮拉提斯",
        remind_at,
    )
    weak_id, outcome = memory.add_reminder_with_outcome(
        "G1",
        "U1",
        "8/8",
        remind_at,
    )

    assert outcome == "duplicate"
    assert weak_id == rich_id
    assert memory.get_reminder(rich_id)["action"] == "桃園高鐵上皮拉提斯"
