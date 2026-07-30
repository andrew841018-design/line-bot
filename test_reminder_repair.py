from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import calendar_db
import main
import memory


TW = ZoneInfo("Asia/Taipei")
GROUP_ID = "G1"
ORIGINAL_ID = "original-medical-message"
ORIGINAL_TEXT = "8/18 早上看胸腔外科陳晉興，看M R I跟Pet掃描結果"


def _event(
    text: str = "咪寶，這個漏掉了",
    *,
    quoted_message_id: str | None = ORIGINAL_ID,
) -> MessageEvent:
    message = MagicMock(spec=TextMessageContent)
    message.id = "repair-request"
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted_message_id
    message.quote_token = "quote-token"
    message.type = "text"

    source = MagicMock(spec=GroupSource)
    source.group_id = GROUP_ID
    source.user_id = "U_REQUESTER"

    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "reply-token"
    return event


def _seed_original(
    group_id: str = GROUP_ID,
    *,
    enqueue: bool = True,
) -> int | None:
    created_at = int(datetime(2026, 7, 29, 11, 27, tzinfo=TW).timestamp())
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, 'U_MOM', ?, ?)",
            (group_id, ORIGINAL_ID, ORIGINAL_TEXT, created_at),
        )
    pending_id = None
    if enqueue:
        pending_id = memory.enqueue_pending_reminder(
            group_id,
            "U_MOM",
            ORIGINAL_TEXT,
            ORIGINAL_ID,
        )
        assert pending_id is not None
        with memory._conn() as conn:
            conn.execute(
                "UPDATE pending_reminder_extract SET created_at=? WHERE pending_id=?",
                (created_at, pending_id),
            )
    return int(pending_id) if pending_id is not None else None


def _patch_sender_alias(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "_alias_from_user_id",
        lambda user_id: "媽媽" if user_id == "U_MOM" else "Andrew",
    )


def _must_not_run(*_args, **_kwargs):
    raise AssertionError("quoted repair must stop later routing")


def test_quote_missed_repairs_original_without_llm_or_quota(monkeypatch):
    pending_id = _seed_original()
    assert pending_id is not None
    _patch_sender_alias(monkeypatch)
    replies: list[str] = []

    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main, "_try_one_shot_reply", _must_not_run)
    monkeypatch.setattr(main, "_auto_capture_text_if_important", _must_not_run)
    monkeypatch.setattr(main, "_maybe_extract_reminder", _must_not_run)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    events = calendar_db.find_active_events_by_source_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert len(events) == 1
    assert events[0]["event_date"] == "2026-08-18"
    assert events[0]["event_time"] == "09:00"
    assert events[0]["event_type"] == "medical"
    assert events[0]["title"] == "媽媽看胸腔外科陳晉興，看MRI跟PET掃描結果"
    assert events[0]["location"] is None
    assert "媽媽" in events[0]["participants"]

    mirrors = memory.list_reminder_source_cancellation_candidates(
        GROUP_ID,
        calendar_db.EVENT_REMINDER_SOURCE_KIND,
        events[0]["event_id"],
    )
    assert len(mirrors) == 1
    assert mirrors[0]["status"] == "pending"
    assert mirrors[0]["user_id"] == "U_MOM"
    with sqlite3.connect(memory._DB_PATH) as conn:
        pending_status = conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (pending_id,),
        ).fetchone()[0]
    assert pending_status == "done"
    assert replies == [
        "已補上提醒\n"
        "時間：2026-08-18 09:00（依「早上」預設）\n"
        "事項：媽媽看胸腔外科陳晉興，看MRI跟PET掃描結果"
    ]


def test_original_medical_message_auto_captures_without_repair(monkeypatch):
    pending_id = _seed_original()
    assert pending_id is not None
    _patch_sender_alias(monkeypatch)

    inserted = main._capture_calendar_events_regex_only(
        GROUP_ID,
        ORIGINAL_TEXT,
        "U_MOM",
        ORIGINAL_ID,
    )

    assert inserted == 1
    events = calendar_db.find_active_events_by_source_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert len(events) == 1
    assert events[0]["title"] == "媽媽看胸腔外科陳晉興，看MRI跟PET掃描結果"
    assert events[0]["event_time"] == "09:00"
    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None and pending["status"] == "done"


def test_original_auto_capture_defers_to_existing_pending_worker(monkeypatch):
    pending_id = _seed_original()
    assert pending_id is not None
    _patch_sender_alias(monkeypatch)
    claim_token = memory.claim_pending_reminder(pending_id)
    assert claim_token

    result = main._capture_calendar_events_regex_only(
        GROUP_ID,
        ORIGINAL_TEXT,
        "U_MOM",
        ORIGINAL_ID,
    )

    assert result == -1
    assert calendar_db.find_active_events_by_source_message(
        GROUP_ID,
        ORIGINAL_ID,
    ) == []
    assert memory.release_pending_reminder(pending_id, claim_token)


def test_original_auto_capture_and_reminder_extract_leave_one_calendar_mirror(
    monkeypatch,
):
    _seed_original(enqueue=False)
    _patch_sender_alias(monkeypatch)
    monkeypatch.setattr(
        main.gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("deterministic auto-capture must not call Gemini")
        ),
    )

    captured = main._auto_capture_text_if_important(
        GROUP_ID,
        ORIGINAL_TEXT,
        "U_MOM",
        ORIGINAL_ID,
    )
    confirmation = main._format_source_calendar_capture_confirmation(
        GROUP_ID,
        ORIGINAL_ID,
        ORIGINAL_TEXT,
    )

    events = calendar_db.find_active_events_by_source_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    reminders = memory.list_pending_reminders(GROUP_ID)
    assert captured is True
    assert len(events) == 1
    assert len(reminders) == 1
    assert reminders[0]["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND
    assert reminders[0]["source_ref"] == events[0]["event_id"]
    assert confirmation is not None


def test_quote_missed_without_exact_quote_fails_closed(monkeypatch):
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(quoted_message_id=None), GROUP_ID)

    assert calendar_db.list_upcoming(GROUP_ID, days=3650) == []
    assert len(replies) == 1
    assert "回覆原本那則訊息" in replies[0]


def test_unquoted_ordinary_missed_item_does_not_trigger_repair():
    assert (
        main._try_handle_missed_reminder_repair(
            _event("我有東西漏掉了", quoted_message_id=None),
            GROUP_ID,
            "我有東西漏掉了",
        )
        is False
    )


def test_negated_or_nonrequest_missed_phrases_do_not_trigger_repair():
    for text in (
        "我沒有漏掉這個",
        "這個沒新增就好",
        "漏了也沒關係",
        "不用補，這個漏掉了",
        "不要新增，這個漏掉了",
        "先別補，這個漏掉了",
        "不需要補，這個漏掉了",
        "請勿新增，這個漏掉了",
    ):
        assert (
            main._try_handle_missed_reminder_repair(
                _event(text, quoted_message_id=ORIGINAL_ID),
                GROUP_ID,
                text,
            )
            is False
        )


def test_quote_missed_cannot_read_same_message_id_from_other_group(monkeypatch):
    _seed_original(group_id="G_OTHER")
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    assert calendar_db.list_upcoming(GROUP_ID, days=3650) == []
    assert len(replies) == 1
    assert "找不到原本那則訊息" in replies[0]


def test_quote_missed_does_not_duplicate_already_drained_pending(monkeypatch):
    pending_id = _seed_original()
    assert pending_id is not None
    with memory._conn() as conn:
        conn.execute(
            "UPDATE pending_reminder_extract SET status='done' WHERE pending_id=?",
            (pending_id,),
        )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    assert calendar_db.list_upcoming(GROUP_ID, days=3650) == []
    assert replies == ["這則訊息已處理過，先不重複建立；請用提醒清單確認。"]


def test_quote_missed_repairs_dropped_pending_row(monkeypatch):
    pending_id = _seed_original()
    assert pending_id is not None
    with memory._conn() as conn:
        conn.execute(
            "UPDATE pending_reminder_extract SET status='dropped' WHERE pending_id=?",
            (pending_id,),
        )
    _patch_sender_alias(monkeypatch)
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None and pending["status"] == "done"
    assert replies[0].startswith("已補上提醒")


def test_quote_missed_reuses_unique_legacy_event_with_presentation_differences(
    monkeypatch,
):
    _seed_original()
    _patch_sender_alias(monkeypatch)
    legacy_title = "媽媽早上看胸腔外科陳晉興，看 MRI 跟 PET 掃描結果"
    legacy_id = calendar_db.insert_event(
        group_id=GROUP_ID,
        title=legacy_title,
        event_date="2026-08-18",
        event_time="09:00",
        location="胸腔外科 陳晉興",
        participants=["媽媽"],
        event_type="medical",
    )
    assert legacy_id
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    with calendar_db._conn() as conn:
        rows = conn.execute(
            "SELECT event_id, title, source_msg_id FROM events "
            "WHERE group_id=? AND status='active'",
            (GROUP_ID,),
        ).fetchall()
    assert rows == [(legacy_id, legacy_title, ORIGINAL_ID)]
    mirrors = memory.list_reminder_source_cancellation_candidates(
        GROUP_ID,
        calendar_db.EVENT_REMINDER_SOURCE_KIND,
        legacy_id,
    )
    assert len(mirrors) == 1
    assert mirrors[0]["user_id"] == "U_MOM"
    assert replies[0].startswith("這則提醒已存在，未重複新增")


def test_quote_missed_does_not_resurrect_cancelled_source(monkeypatch):
    pending_id = _seed_original()
    assert pending_id is not None
    _patch_sender_alias(monkeypatch)
    event_id = calendar_db.insert_event(
        group_id=GROUP_ID,
        title="媽媽看胸腔外科",
        event_date="2026-08-18",
        event_time="09:00",
        participants=["媽媽"],
        source_msg_id=ORIGINAL_ID,
        event_type="medical",
    )
    assert event_id
    assert calendar_db.cancel_event(event_id)
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    with calendar_db._conn() as conn:
        rows = conn.execute(
            "SELECT event_id, status FROM events WHERE group_id=?",
            (GROUP_ID,),
        ).fetchall()
    assert rows == [(event_id, "cancelled")]
    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None and pending["status"] == "dropped"
    assert replies == [
        "這則提醒已取消，先不重新建立；若要恢復，請明確說「恢復這則提醒」。"
    ]


def test_quote_missed_does_not_confirm_when_pending_completion_loses_claim(
    monkeypatch,
):
    _seed_original()
    _patch_sender_alias(monkeypatch)
    replies: list[str] = []
    monkeypatch.setattr(memory, "mark_pending_reminder", lambda *_a, **_k: False)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    assert len(replies) == 1
    assert "尚未完成" in replies[0]
    assert "已補上提醒" not in replies[0]


def test_quote_missed_redelivery_is_idempotent_even_if_first_reply_fails(monkeypatch):
    _seed_original()
    _patch_sender_alias(monkeypatch)

    monkeypatch.setattr(
        main,
        "_reply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("reply failed")),
    )
    try:
        main._handle_text_message(_event(), GROUP_ID)
    except RuntimeError as exc:
        assert str(exc) == "reply failed"
    else:
        raise AssertionError("first reply should fail")

    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    main._handle_text_message(_event(), GROUP_ID)

    events = calendar_db.find_active_events_by_source_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert len(events) == 1
    mirrors = memory.list_reminder_source_cancellation_candidates(
        GROUP_ID,
        calendar_db.EVENT_REMINDER_SOURCE_KIND,
        events[0]["event_id"],
    )
    assert len(mirrors) == 1
    assert replies[0].startswith("這則提醒已存在，未重複新增")


def test_original_auto_capture_redelivery_uses_source_identity_without_llm(
    monkeypatch,
):
    _seed_original(enqueue=False)
    _patch_sender_alias(monkeypatch)
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", _must_not_run)
    monkeypatch.setattr(main.gemini_client, "extract_reminder", _must_not_run)

    first = main._auto_capture_text_if_important(
        GROUP_ID,
        ORIGINAL_TEXT,
        "U_MOM",
        ORIGINAL_ID,
    )
    second = main._auto_capture_text_if_important(
        GROUP_ID,
        ORIGINAL_TEXT,
        "U_MOM",
        ORIGINAL_ID,
    )

    assert first is True
    assert second is True
    events = calendar_db.find_active_events_by_source_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert len(events) == 1
    mirrors = memory.list_reminder_source_cancellation_candidates(
        GROUP_ID,
        calendar_db.EVENT_REMINDER_SOURCE_KIND,
        events[0]["event_id"],
    )
    assert len(mirrors) == 1


def test_partial_multi_event_capture_blocks_all_fallbacks(monkeypatch):
    text = "8/1下午 打羽球\n8/2晚上打壁球19:00-20:00"
    message_id = "multi-event-source"
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, 'U_MOM', ?, 1)",
            (GROUP_ID, message_id, text),
        )
    original_insert = calendar_db.insert_event_with_outcome
    calls = 0

    def flaky_insert(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second event failed")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(calendar_db, "insert_event_with_outcome", flaky_insert)
    result = main._capture_calendar_events_regex_only(
        GROUP_ID,
        text,
        "U_MOM",
        message_id,
    )
    assert result == -1
    assert len(
        calendar_db.find_active_events_by_source_message(
            GROUP_ID,
            message_id,
        )
    ) == 1

    monkeypatch.setattr(main, "_maybe_capture_calendar_event", _must_not_run)
    assert (
        main._auto_capture_text_if_important(
            GROUP_ID,
            text,
            "U_MOM",
            message_id,
        )
        is None
    )


def test_quote_missed_partial_multi_event_is_not_reported_complete(monkeypatch):
    text = "8/1 18:00 全家聚餐\n8/2 09:00 看胸腔外科"
    created_at = int(datetime(2026, 7, 29, 11, 27, tzinfo=TW).timestamp())
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, 'U_MOM', ?, ?)",
            (GROUP_ID, ORIGINAL_ID, text, created_at),
        )
    pending_id = memory.enqueue_pending_reminder(
        GROUP_ID,
        "U_MOM",
        text,
        ORIGINAL_ID,
    )
    assert pending_id is not None
    event_id = calendar_db.insert_event(
        group_id=GROUP_ID,
        title="全家聚餐",
        event_date="2026-08-01",
        event_time="18:00",
        event_type="family_gathering",
        source_msg_id=ORIGINAL_ID,
    )
    assert event_id
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, reply_text, **_kwargs: replies.append(reply_text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None and pending["status"] == "pending"
    assert len(
        calendar_db.find_active_events_by_source_message(
            GROUP_ID,
            ORIGINAL_ID,
        )
    ) == 1
    assert len(replies) == 1
    assert "尚未完成" in replies[0]
    assert "已存在" not in replies[0]
    assert "已補上提醒" not in replies[0]

    main._drain_pending_reminders(GROUP_ID)
    pending_after_drain = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending_after_drain is not None
    assert pending_after_drain["status"] == "pending"
    assert pending_after_drain["retries"] == 1


def test_quote_missed_multiple_source_events_does_not_drop_pending(monkeypatch):
    text = "8/1 18:00 全家聚餐\n8/2 09:00 看胸腔外科"
    created_at = int(datetime(2026, 7, 29, 11, 27, tzinfo=TW).timestamp())
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, 'U_MOM', ?, ?)",
            (GROUP_ID, ORIGINAL_ID, text, created_at),
        )
    pending_id = memory.enqueue_pending_reminder(
        GROUP_ID,
        "U_MOM",
        text,
        ORIGINAL_ID,
    )
    assert pending_id is not None
    for title, event_date, event_time, event_type in (
        ("全家聚餐", "2026-08-01", "18:00", "family_gathering"),
        ("看胸腔外科", "2026-08-02", "09:00", "medical"),
    ):
        assert calendar_db.insert_event(
            group_id=GROUP_ID,
            title=title,
            event_date=event_date,
            event_time=event_time,
            event_type=event_type,
            source_msg_id=ORIGINAL_ID,
        )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, reply_text, **_kwargs: replies.append(reply_text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None and pending["status"] == "pending"
    assert len(replies) == 1
    assert "尚未完成" in replies[0]


def test_drain_recovers_multiple_source_events_only_after_all_mirrors_exist():
    text = "8/1 18:00 全家聚餐\n8/2 09:00 看胸腔外科"
    created_at = int(datetime(2026, 7, 29, 11, 27, tzinfo=TW).timestamp())
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, 'U_MOM', ?, ?)",
            (GROUP_ID, ORIGINAL_ID, text, created_at),
        )
    pending_id = memory.enqueue_pending_reminder(
        GROUP_ID,
        "U_MOM",
        text,
        ORIGINAL_ID,
    )
    assert pending_id is not None
    event_ids = [
        calendar_db.insert_event(
            group_id=GROUP_ID,
            title=title,
            event_date=event_date,
            event_time=event_time,
            event_type=event_type,
            source_msg_id=ORIGINAL_ID,
        )
        for title, event_date, event_time, event_type in (
            ("全家聚餐", "2026-08-01", "18:00", "family_gathering"),
            ("看胸腔外科", "2026-08-02", "09:00", "medical"),
        )
    ]
    assert all(event_ids)
    with memory._conn() as conn:
        conn.execute(
            "DELETE FROM reminders WHERE group_id=? AND source_kind=? "
            "AND source_ref IN (?, ?)",
            (
                GROUP_ID,
                calendar_db.EVENT_REMINDER_SOURCE_KIND,
                *event_ids,
            ),
        )

    main._drain_pending_reminders(GROUP_ID)

    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None and pending["status"] == "done"
    for event_id in event_ids:
        mirrors = memory.list_reminder_source_cancellation_candidates(
            GROUP_ID,
            calendar_db.EVENT_REMINDER_SOURCE_KIND,
            event_id,
        )
        assert len(mirrors) == 1
        assert mirrors[0]["status"] == "pending"


def test_mirror_sync_preserves_delivery_flags_and_terminal_tombstones():
    _seed_original(enqueue=False)
    event_id = calendar_db.insert_event(
        group_id=GROUP_ID,
        title="媽媽看胸腔外科",
        event_date="2026-08-18",
        event_time="09:00",
        participants=["媽媽"],
        source_msg_id=ORIGINAL_ID,
        event_type="medical",
    )
    assert event_id
    event = calendar_db.get_active_event_by_id(GROUP_ID, event_id)
    assert event is not None
    with memory._conn() as conn:
        conn.execute(
            "UPDATE reminders SET last_pushed_at=123, weekly_count=4, "
            "pushed_1d=456 WHERE group_id=? AND source_kind=? AND source_ref=?",
            (GROUP_ID, calendar_db.EVENT_REMINDER_SOURCE_KIND, event_id),
        )

    assert calendar_db.synchronize_pending_event_reminder_mirror(event)
    with memory._conn() as conn:
        flags = conn.execute(
            "SELECT last_pushed_at, weekly_count, pushed_1d FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=?",
            (GROUP_ID, calendar_db.EVENT_REMINDER_SOURCE_KIND, event_id),
        ).fetchone()
        conn.execute(
            "UPDATE reminders SET status='cancelled' WHERE group_id=? "
            "AND source_kind=? AND source_ref=?",
            (GROUP_ID, calendar_db.EVENT_REMINDER_SOURCE_KIND, event_id),
        )
    assert flags == (123, 4, 456)

    assert not calendar_db.synchronize_pending_event_reminder_mirror(event)
    mirrors = memory.list_reminder_source_cancellation_candidates(
        GROUP_ID,
        calendar_db.EVENT_REMINDER_SOURCE_KIND,
        event_id,
    )
    assert len(mirrors) == 1
    assert mirrors[0]["status"] == "cancelled"


def test_quote_missed_does_not_claim_success_without_reminder_mirror(monkeypatch):
    _seed_original()
    _patch_sender_alias(monkeypatch)
    replies: list[str] = []

    monkeypatch.setattr(calendar_db, "_upsert_event_reminder", lambda *_a, **_k: False)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )

    main._handle_text_message(_event(), GROUP_ID)

    assert len(replies) == 1
    assert "尚未完成" in replies[0]
    assert "已補上提醒" not in replies[0]


def test_drain_recovers_source_bound_event_without_llm_and_retries_mirror(
    monkeypatch,
):
    pending_id = _seed_original()
    assert pending_id is not None
    _patch_sender_alias(monkeypatch)
    event_id = calendar_db.insert_event(
        group_id=GROUP_ID,
        title="媽媽看胸腔外科陳晉興，看MRI跟PET掃描結果",
        event_date="2026-08-18",
        event_time="09:00",
        participants=["媽媽"],
        source_msg_id=ORIGINAL_ID,
        event_type="medical",
    )
    assert event_id
    with memory._conn() as conn:
        conn.execute(
            "DELETE FROM reminders WHERE group_id=? AND source_kind=? AND source_ref=?",
            (GROUP_ID, calendar_db.EVENT_REMINDER_SOURCE_KIND, event_id),
        )

    original_sync = calendar_db.synchronize_pending_event_reminder_mirror
    monkeypatch.setattr(
        calendar_db,
        "synchronize_pending_event_reminder_mirror",
        lambda _event: False,
    )
    monkeypatch.setattr(
        main.gemini_client,
        "extract_reminder",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("source recovery must not call Gemini")
        ),
    )
    main._drain_pending_reminders(GROUP_ID)

    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None
    assert pending["status"] == "pending"
    assert pending["retries"] == 1

    monkeypatch.setattr(
        calendar_db,
        "synchronize_pending_event_reminder_mirror",
        original_sync,
    )
    main._drain_pending_reminders(GROUP_ID)

    pending = memory.get_pending_reminder_extract_by_message(
        GROUP_ID,
        ORIGINAL_ID,
    )
    assert pending is not None and pending["status"] == "done"
    mirrors = memory.list_reminder_source_cancellation_candidates(
        GROUP_ID,
        calendar_db.EVENT_REMINDER_SOURCE_KIND,
        event_id,
    )
    assert len(mirrors) == 1
    assert mirrors[0]["status"] == "pending"
