from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from linebot.v3.messaging import TextMessage
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import main
import memory
import reminder_push


TW = ZoneInfo("Asia/Taipei")


def _ts(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TW).timestamp())


def _seed(
    group_id: str,
    action: str,
    when: str,
    *,
    source_text: str = "",
) -> int:
    reminder_id = memory.add_reminder(
        group_id,
        "U1",
        action,
        _ts(when),
        source_text=source_text or action,
    )
    assert reminder_id is not None
    return int(reminder_id)


def _event(text: str, *, quoted_message_id: str | None = None) -> MessageEvent:
    message = MagicMock(spec=TextMessageContent)
    message.id = "incoming-cancel"
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted_message_id
    message.quote_token = "quote-token"
    message.type = "text"

    source = MagicMock(spec=GroupSource)
    source.group_id = "G1"
    source.user_id = "U1"

    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "reply-token"
    return event


def _status(reminder_id: int) -> str:
    with memory._conn() as conn:
        row = conn.execute(
            "SELECT status FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _archive_bot_message(
    message_id: str,
    text: str,
    *,
    reminder_id: int | None = None,
    source_kind: str = "",
    source_ref: str = "",
) -> None:
    """Insert a quote fixture without starting the async embedding worker."""
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES ('G1', ?, '__bot__', ?, 1)",
            (message_id, text),
        )
    if reminder_id is not None or (source_kind and source_ref):
        assert memory.log_sent_reminder_reference(
            "G1",
            message_id,
            reminder_id=reminder_id,
            source_kind=source_kind,
            source_ref=source_ref,
        )


def test_quote_cancel_routes_before_creation_and_cancels_exact_reminder(monkeypatch):
    action = "查看租金是否入帳，若未入帳就催繳"
    reminder_id = _seed("G1", action, "2030-07-25 04:00")
    _archive_bot_message(
        "sent-reminder",
        f"@媽媽\n⏰ 提醒（明天）\n2030-07-25 04:00 {action}",
    )
    replies: list[tuple[str, dict]] = []

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("cancellation must stop all later routing")

    monkeypatch.setattr(main, "_try_one_shot_reply", must_not_run)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", must_not_run)
    monkeypatch.setattr(main, "_auto_capture_text_if_important", must_not_run)
    monkeypatch.setattr(main, "_maybe_extract_reminder", must_not_run)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **kwargs: replies.append((text, kwargs)),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="sent-reminder"),
        "G1",
    )

    assert _status(reminder_id) == "cancelled"
    assert len(replies) == 1
    assert "已取消提醒" in replies[0][0]
    assert "2030-07-25 04:00" in replies[0][0]
    assert action in replies[0][0]
    assert replies[0][1]["include_auxiliary"] is False


def test_pasted_event_location_terminal_command_cancels_only_exact_source(
    monkeypatch,
):
    import calendar_db

    title = "紐西蘭一望無際的公路旅行中活動"
    target_ref = calendar_db.insert_event(
        group_id="G1",
        title=title,
        event_date="2030-07-25",
        event_time="01:10",
        location="紐西蘭一望無際的公路旅行中",
    )
    distractor_ref = calendar_db.insert_event(
        group_id="G1",
        title="其他活動",
        event_date="2030-07-25",
        event_time="01:10",
        location="紐西蘭一望無際的公路旅行中",
    )
    other_group_ref = calendar_db.insert_event(
        group_id="G2",
        title=title,
        event_date="2030-07-25",
        event_time="01:10",
        location="紐西蘭一望無際的公路旅行中",
    )
    rows = {
        row["source_ref"]: row
        for group_id in ("G1", "G2")
        for row in memory.list_reminder_cancellation_candidates(group_id)
        if row["source_ref"] in {target_ref, distractor_ref, other_group_ref}
    }
    replies: list[tuple[str, dict]] = []

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("cancellation must stop all later routing")

    monkeypatch.setattr(main, "_try_one_shot_reply", must_not_run)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", must_not_run)
    monkeypatch.setattr(main, "_auto_capture_text_if_important", must_not_run)
    monkeypatch.setattr(main, "_maybe_extract_reminder", must_not_run)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **kwargs: replies.append((text, kwargs)),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "**後天活動提醒**\n"
            "📅 2030-07-25 01:10\n"
            f"🎯 {title}\n"
            "📍 紐西蘭一望無際的公路旅行中。這個取消"
        ),
        "G1",
    )

    assert _status(rows[target_ref]["reminder_id"]) == "cancelled"
    assert _status(rows[distractor_ref]["reminder_id"]) == "pending"
    assert _status(rows[other_group_ref]["reminder_id"]) == "pending"
    with calendar_db._conn() as conn:
        event_status = conn.execute(
            "SELECT status FROM events WHERE event_id=?",
            (target_ref,),
        ).fetchone()
    assert event_status == ("active",)
    assert len(replies) == 1
    assert "已取消提醒" in replies[0][0]
    assert title in replies[0][0]
    assert replies[0][1]["include_auxiliary"] is False


def test_pasted_calendar_location_mismatch_fails_closed(monkeypatch):
    import calendar_db

    title = "地點身份測試活動"
    event_id = calendar_db.insert_event(
        group_id="G1",
        title=title,
        event_date="2030-07-25",
        event_time="01:10",
        location="真正地點",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    replies: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **kwargs: replies.append((text, kwargs)),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "**後天活動提醒**\n"
            "📅 2030-07-25 01:10\n"
            f"🎯 {title}\n"
            "📍 完全不同地點。這個取消"
        ),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "pending"
    assert replies and "無法確認這則活動提醒的來源" in replies[0][0]
    assert replies[0][1]["include_auxiliary"] is False


@pytest.mark.parametrize("quoted_location", ["地點A", ""])
def test_bound_quote_with_conflicting_current_location_is_ambiguous(
    monkeypatch,
    quoted_location,
):
    import calendar_db

    title = "同名活動"
    event_id = calendar_db.insert_event(
        group_id="G1",
        title=title,
        event_date="2030-07-25",
        event_time="01:10",
        location="地點A",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    _archive_bot_message(
        "bound-location-a",
        "🔔 **後天活動提醒**\n"
        "📅 2030-07-25 01:10\n"
        f"🎯 {title}"
        + (f"\n📍 {quoted_location}" if quoted_location else ""),
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    replies: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **kwargs: replies.append((text, kwargs)),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n"
            "📅 2030-07-25 01:10\n"
            f"🎯 {title}\n"
            "📍 地點B",
            quoted_message_id="bound-location-a",
        ),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "pending"
    assert replies and "沒有唯一鎖定" in replies[0][0]
    assert replies[0][1]["include_auxiliary"] is False


def test_bound_natural_quote_validates_explicit_current_calendar_location(
    monkeypatch,
):
    import calendar_db

    title = "自然格式來源活動"
    event_id = calendar_db.insert_event(
        group_id="G1",
        title=title,
        event_date="2030-07-25",
        event_time="01:10",
        location="地點A",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    _archive_bot_message(
        "bound-natural-location-a",
        f"⏰ 提醒（明天）\n2030-07-25 01:10 {title}",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    replies: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **kwargs: replies.append((text, kwargs)),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "**後天活動提醒**\n"
            "📅 2030-07-25 01:10\n"
            f"🎯 {title}\n"
            "📍 地點B。這個取消",
            quoted_message_id="bound-natural-location-a",
        ),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "pending"
    assert replies and "無法確認這則活動提醒的來源" in replies[0][0]
    assert replies[0][1]["include_auxiliary"] is False


@pytest.mark.parametrize(
    "source_state",
    ["missing", "mirror_failure", "mirror_exception"],
)
def test_pasted_calendar_never_falls_back_to_generic_when_source_unavailable(
    monkeypatch,
    source_state,
):
    import calendar_db

    title = "來源綁定測試活動"
    event_id = None
    if source_state != "missing":
        event_id = calendar_db.insert_event(
            group_id="G1",
            title=title,
            event_date="2030-07-25",
            event_time="01:10",
            location="來源地點",
        )
        with memory._conn() as conn:
            conn.execute(
                "DELETE FROM reminders "
                "WHERE group_id='G1' AND source_kind='calendar_event' "
                "AND source_ref=?",
                (event_id,),
            )
        if source_state == "mirror_failure":
            monkeypatch.setattr(
                calendar_db,
                "ensure_event_reminder_mirror",
                lambda _event: False,
            )
        else:
            def fail_mirror(_event):
                raise RuntimeError("mirror unavailable")

            monkeypatch.setattr(
                calendar_db,
                "ensure_event_reminder_mirror",
                fail_mirror,
            )

    generic_id = _seed("G1", title, "2030-07-25 01:10")
    replies: list[tuple[str, dict]] = []

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("ambiguous cancellation must stop later routing")

    monkeypatch.setattr(main, "_try_one_shot_reply", must_not_run)
    monkeypatch.setattr(main, "_try_handle_calendar_correction", must_not_run)
    monkeypatch.setattr(main, "_auto_capture_text_if_important", must_not_run)
    monkeypatch.setattr(main, "_maybe_extract_reminder", must_not_run)
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **kwargs: replies.append((text, kwargs)),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "**後天活動提醒**\n"
            "📅 2030-07-25 01:10\n"
            f"🎯 {title}\n"
            "📍 來源地點。這個取消"
        ),
        "G1",
    )

    assert _status(generic_id) == "pending"
    if event_id is not None:
        with calendar_db._conn() as conn:
            event_status = conn.execute(
                "SELECT status FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        assert event_status == ("active",)
    assert len(replies) == 1
    assert "無法確認這則活動提醒的來源" in replies[0][0]
    assert "先沒有取消" in replies[0][0]
    assert replies[0][1]["include_auxiliary"] is False


def test_old_mapped_natural_quote_never_cancels_recreated_identical_reminder(
    monkeypatch,
):
    action = "繳交管理費"
    old_id = _seed("G1", action, "2030-07-25 04:00")
    _archive_bot_message(
        "old-natural-reminder",
        f"⏰ 提醒（明天）\n2030-07-25 04:00 {action}",
        reminder_id=old_id,
    )
    assert memory.mark_reminder_pushed(old_id, "now")
    new_id = _seed("G1", action, "2030-07-25 04:00")
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="old-natural-reminder"),
        "G1",
    )

    assert _status(old_id) == "done"
    assert _status(new_id) == "pending"
    assert replies and "沒有找到" in replies[0]


def test_cancellation_reports_truthfully_when_delivery_claim_won(monkeypatch):
    action = "繳交管理費"
    when = "2030-07-25 04:00"
    reminder_id = _seed("G1", action, when)
    claim = memory.claim_natural_reminder_delivery(
        "G1",
        reminder_id,
        "1d",
        expected_action=action,
        expected_remind_at=_ts(when),
        transport="push",
    )
    assert claim is not None
    _archive_bot_message(
        "claimed-natural-reminder",
        f"⏰ 提醒（明天）\n{when} {action}",
        reminder_id=reminder_id,
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="claimed-natural-reminder"),
        "G1",
    )

    assert _status(reminder_id) == "cancelled"
    assert replies and "已取消後續提醒" in replies[0]
    assert "仍可能送達" in replies[0]
    assert memory.release_reminder_delivery_claim(claim)


def test_old_unbound_natural_quote_treats_done_row_as_ambiguity(monkeypatch):
    action = "繳交停車費"
    old_id = _seed("G1", action, "2030-07-25 04:00")
    _archive_bot_message(
        "old-unbound-natural-reminder",
        f"⏰ 提醒（明天）\n2030-07-25 04:00 {action}",
    )
    assert memory.mark_reminder_pushed(old_id, "now")
    new_id = _seed("G1", action, "2030-07-25 04:00")
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "這則取消",
            quoted_message_id="old-unbound-natural-reminder",
        ),
        "G1",
    )

    assert _status(old_id) == "done"
    assert _status(new_id) == "pending"
    assert replies and "多筆" in replies[0]


def test_old_mapped_event_quote_cannot_cancel_recreated_identical_event(
    monkeypatch,
):
    import calendar_db

    old_event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐",
        event_date="2030-07-25",
        event_time="18:00",
    )
    old_source = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == old_event_id
    )
    _archive_bot_message(
        "old-event-reminder",
        "🔔 **明天活動提醒**\n"
        "📅 2030-07-25 18:00\n"
        "🎯 家庭聚餐",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=old_event_id,
    )
    assert calendar_db.cancel_event(old_event_id)
    assert _status(old_source["reminder_id"]) == "done"

    new_event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐",
        event_date="2030-07-25",
        event_time="18:00",
    )
    new_source = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == new_event_id
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="old-event-reminder"),
        "G1",
    )

    assert _status(old_source["reminder_id"]) == "cancelled"
    assert _status(new_source["reminder_id"]) == "pending"
    assert replies and "已取消提醒" in replies[0]


def test_bound_one_off_event_quote_does_not_require_standard_visible_format(
    monkeypatch,
):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="確認接送時間",
        event_date="2030-08-03",
        event_time="12:00",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    _archive_bot_message(
        "one-off-event-reminder",
        "媽媽\n請確認明天接送時間",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("取消", quoted_message_id="one-off-event-reminder"),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert replies and "已取消提醒" in replies[0]


def test_expired_calendar_mirror_still_cancels_later_offsets(monkeypatch):
    import calendar_db

    event_date = (calendar_db._today_tw() + timedelta(days=3)).isoformat()
    event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭打羽球",
        event_date=event_date,
        event_time="20:00",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    assert memory.delete_stale_pending_reminders(grace_seconds=3600) >= 1
    assert _status(source_row["reminder_id"]) == "expired"
    assert {
        row["event_id"]
        for row in calendar_db.list_due_for_reminder("G1", days_ahead=3)
    } == {event_id}

    _archive_bot_message(
        "expired-source-reminder",
        "🔔 活動提醒\n"
        f"📅 {event_date} 20:00\n"
        "🎯 家庭打羽球",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="expired-source-reminder"),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert calendar_db.list_due_for_reminder("G1", days_ahead=3) == []
    assert replies and "已取消提醒" in replies[0]


def test_bound_nonstandard_quote_and_explicit_other_target_change_nothing(
    monkeypatch,
):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="確認接送時間",
        event_date="2030-08-03",
        event_time="12:00",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    other_id = _seed("G1", "繳信用卡", "2030-08-04 09:00")
    _archive_bot_message(
        "one-off-event-reminder",
        "媽媽\n請確認明天接送時間",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n2030-08-04 09:00 繳信用卡",
            quoted_message_id="one-off-event-reminder",
        ),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "pending"
    assert _status(other_id) == "pending"
    assert replies and "沒有唯一鎖定" in replies[0]


def test_pasted_calendar_action_variants_with_two_sources_are_ambiguous(
    monkeypatch,
):
    import calendar_db

    short_event = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐",
        event_date="2030-08-03",
        event_time="18:00",
    )
    raw_event = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐 這則取消",
        event_date="2030-08-03",
        event_time="18:00",
    )
    rows = {
        row["source_ref"]: row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] in {short_event, raw_event}
    }
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n"
            "📅 2030-08-03 18:00\n"
            "🎯 家庭聚餐 這則取消"
        ),
        "G1",
    )

    assert _status(rows[short_event]["reminder_id"]) == "pending"
    assert _status(rows[raw_event]["reminder_id"]) == "pending"
    assert replies and "多筆" in replies[0]


def test_pasted_calendar_nfkc_equivalent_titles_are_ambiguous(monkeypatch):
    import calendar_db

    ascii_event = calendar_db.insert_event(
        group_id="G1",
        title="ABC",
        event_date="2030-08-03",
        event_time="18:00",
    )
    fullwidth_event = calendar_db.insert_event(
        group_id="G1",
        title="ＡＢＣ",
        event_date="2030-08-03",
        event_time="18:00",
    )
    rows = {
        row["source_ref"]: row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] in {ascii_event, fullwidth_event}
    }
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n"
            "📅 2030-08-03 18:00\n"
            "🎯 ABC"
        ),
        "G1",
    )

    assert _status(rows[ascii_event]["reminder_id"]) == "pending"
    assert _status(rows[fullwidth_event]["reminder_id"]) == "pending"
    assert replies and "多筆" in replies[0]


def test_pasted_calendar_equivalent_minute_formats_are_ambiguous(monkeypatch):
    import calendar_db

    padded_event = calendar_db.insert_event(
        group_id="G1",
        title="ABC",
        event_date="2030-08-03",
        event_time="04:00",
    )
    unpadded_event = calendar_db.insert_event(
        group_id="G1",
        title="ＡＢＣ",
        event_date="2030-08-03",
        event_time="4:00",
    )
    rows = {
        row["source_ref"]: row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] in {padded_event, unpadded_event}
    }
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n"
            "📅 2030-08-03 04:00\n"
            "🎯 ABC"
        ),
        "G1",
    )

    assert _status(rows[padded_event]["reminder_id"]) == "pending"
    assert _status(rows[unpadded_event]["reminder_id"]) == "pending"
    assert replies and "多筆" in replies[0]


def test_pasted_calendar_equivalent_date_formats_are_ambiguous(monkeypatch):
    import calendar_db

    padded_event = calendar_db.insert_event(
        group_id="G1",
        title="ABC",
        event_date="2030-08-03",
        event_time="04:00",
    )
    unpadded_event = calendar_db.insert_event(
        group_id="G1",
        title="ＡＢＣ",
        event_date="2030-8-3",
        event_time="04:00",
    )
    rows = {
        row["source_ref"]: row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] in {padded_event, unpadded_event}
    }
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n"
            "📅 2030-08-03 04:00\n"
            "🎯 ABC"
        ),
        "G1",
    )

    assert _status(rows[padded_event]["reminder_id"]) == "pending"
    assert _status(rows[unpadded_event]["reminder_id"]) == "pending"
    assert replies and "多筆" in replies[0]


def test_quoted_action_suffix_does_not_cancel_different_shorter_action(monkeypatch):
    shorter_id = _seed("G1", "買", "2030-07-25 04:00")
    _archive_bot_message(
        "old-sent-reminder",
        "@媽媽\n⏰ 提醒（明天）\n2030-07-25 04:00 買這個",
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("@咪寶 這則取消", quoted_message_id="old-sent-reminder"),
        "G1",
    )

    assert _status(shorter_id) == "pending"
    assert replies and "沒有找到時間與事項都相符" in replies[0]


def test_pasted_rendered_reminder_selects_time_when_actions_are_equal(monkeypatch):
    action = "查看251巷租金是否入郵局帳戶，催繳代書"
    four = _seed("G1", action, "2030-07-25 04:00")
    eight = _seed("G1", action, "2030-07-25 08:00")
    replies: list[str] = []

    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒（明天）\n"
            f"2030-07-25 04:00 {action}\n"
            "這一則"
        ),
        "G1",
    )

    assert _status(four) == "cancelled"
    assert _status(eight) == "pending"
    assert replies and "2030-07-25 04:00" in replies[0]


def test_ambiguous_action_only_cancel_does_not_mutate(monkeypatch):
    action = "查看租金是否入帳"
    four = _seed("G1", action, "2030-07-25 04:00")
    eight = _seed("G1", action, "2030-07-25 08:00")
    replies: list[str] = []

    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(f"取消提醒\n事項：{action}"),
        "G1",
    )

    assert _status(four) == "pending"
    assert _status(eight) == "pending"
    assert replies and "多筆" in replies[0]


def test_creation_acknowledgement_quote_can_be_cancelled(monkeypatch):
    action = "領取護照"
    reminder_id = _seed("G1", action, "2030-08-03 10:30")
    _archive_bot_message(
        "creation-ack",
        "已新增提醒\n時間：2030-08-03 10:30\n事項：領取護照",
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這個提醒不用了", quoted_message_id="creation-ack"),
        "G1",
    )

    assert _status(reminder_id) == "cancelled"
    assert replies and "已取消提醒" in replies[0]


def test_old_cancelled_and_identical_new_pending_are_ambiguous(monkeypatch):
    action = "繳房租"
    old_id = _seed("G1", action, "2030-08-03 10:30")
    assert memory.cancel_pending_reminder(
        "G1", old_id, action, _ts("2030-08-03 10:30")
    )
    new_id = _seed("G1", action, "2030-08-03 10:30")
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("取消提醒 2030-08-03 10:30 繳房租"),
        "G1",
    )

    assert _status(old_id) == "cancelled"
    assert _status(new_id) == "pending"
    assert replies and "已取消和待處理" in replies[0]


def test_scheduled_push_archives_real_line_message_id(monkeypatch):
    archived: list[tuple[str, str, str | None, str]] = []
    references: list[tuple[tuple, dict]] = []

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def push_message(self, _request):
            return SimpleNamespace(
                sent_messages=[SimpleNamespace(id="line-sent-reminder")]
            )

    monkeypatch.setattr(reminder_push, "ApiClient", FakeApiClient)
    monkeypatch.setattr(reminder_push, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(reminder_push, "_line_access_token", lambda: "token")
    monkeypatch.setattr(
        reminder_push.memory,
        "log_raw_message",
        lambda *args: archived.append(args),
    )
    monkeypatch.setattr(
        reminder_push.memory,
        "log_sent_reminder_reference",
        lambda *args, **kwargs: references.append((args, kwargs)),
    )

    assert reminder_push._push_to_group(
        "G1",
        "⏰ 提醒\n2030-01-02 04:00 繳費",
        reminder_id=77,
    )
    assert archived == [
        ("G1", "line-sent-reminder", "__bot__", "⏰ 提醒\n2030-01-02 04:00 繳費")
    ]
    assert references == [(("G1", "line-sent-reminder"), {"reminder_id": 77})]


def test_scheduled_push_archive_failure_does_not_retry_delivery(monkeypatch):
    calls = 0

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def push_message(self, _request):
            nonlocal calls
            calls += 1
            return SimpleNamespace(sent_messages=[SimpleNamespace(id="sent-once")])

    monkeypatch.setattr(reminder_push, "ApiClient", FakeApiClient)
    monkeypatch.setattr(reminder_push, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(reminder_push, "_line_access_token", lambda: "token")
    monkeypatch.setattr(
        reminder_push.memory,
        "log_raw_message",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("archive down")),
    )

    assert reminder_push._push_to_group("G1", "⏰ 提醒\n2030-01-02 04:00 繳費")
    assert calls == 1


def test_scheduled_reminder_render_uses_taipei_timezone():
    remind_at = _ts("2030-01-02 04:00")
    row = {
        "remind_at": remind_at,
        "action": "繳費",
        "mention_aliases": [],
    }

    text = reminder_push._format_push_text(row, "1d", now=_ts("2030-01-01 04:00"))

    assert "2030-01-02 04:00 繳費" in text


def test_fast_path_reminder_reply_archives_sent_id_and_plain_text(monkeypatch):
    plain = "⏰ 提醒（明天）\n2030-01-02 04:00 繳費"
    archived: list[tuple[str, str, str | None, str]] = []
    references: list[tuple[tuple, dict]] = []

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, _request):
            return SimpleNamespace(
                sent_messages=[SimpleNamespace(id="fast-path-sent")]
            )

    import calendar_db

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *_a, **_k: [])
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *_a, **_k: [
                {
                    "reminder_id": 7,
                    "stage": "1d",
                    "text": plain,
                    "message": TextMessage(text=plain),
                    "action": "繳費",
                    "remind_at": _ts("2030-01-02 04:00"),
                    "weekly_count": 0,
                }
            ],
        )
    monkeypatch.setattr(main.memory, "is_reminder_pending", lambda _gid, _rid: True)
    monkeypatch.setattr(
        main.memory,
        "claim_natural_reminder_delivery",
        lambda *_a, **_k: {
            "reminder_id": 7,
            "claim_token": "claim",
        },
    )
    monkeypatch.setattr(
        main.memory,
        "finalize_natural_reminder_delivery",
        lambda _claim: True,
    )
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda _token: None)
    monkeypatch.setattr(
        main.memory,
        "log_raw_message",
        lambda *args: archived.append(args),
    )
    monkeypatch.setattr(
        main.memory,
        "log_sent_reminder_reference",
        lambda *args, **kwargs: references.append((args, kwargs)),
    )

    assert main._try_piggyback_reminders_fast_path("reply-token", "G1")
    assert archived == [("G1", "fast-path-sent", "__bot__", plain)]
    assert references == [(("G1", "fast-path-sent"), {"reminder_id": 7})]


def test_cancelled_calendar_reminder_stays_cancelled_and_event_stays_active():
    import calendar_db

    event_date = (calendar_db._today_tw() + timedelta(days=1)).isoformat()
    event_id = calendar_db.insert_event(
        group_id="G1",
        title="明日家庭行程",
        event_date=event_date,
        event_time="04:00",
    )
    assert event_id

    candidates = memory.list_reminder_cancellation_candidates("G1")
    reminder = next(
        row
        for row in candidates
        if row["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND
        and row["source_ref"] == event_id
    )
    assert memory.cancel_pending_reminder(
        "G1",
        reminder["reminder_id"],
        reminder["action"],
        reminder["remind_at"],
    )

    calendar_db.sync_active_events_to_reminders("G1")

    persisted = memory.get_reminder(reminder["reminder_id"])
    assert persisted is not None
    assert persisted["status"] == "cancelled"
    with calendar_db._conn() as conn:
        event_status = conn.execute(
            "SELECT status FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert event_status == ("active",)
    assert all(
        event["event_id"] != event_id
        for event in calendar_db.list_due_for_reminder("G1", days_ahead=1)
    )


def test_quote_cancels_lead_time_calendar_reminder_by_exact_event_source(
    monkeypatch,
):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="全家打羽球",
        event_date="2030-07-25",
        event_time="16:00",
        participants=["黃聖雅"],
    )
    assert event_id
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND
        and row["source_ref"] == event_id
    )
    assert source_row["action"] == "黃聖雅負責預約7/25打羽球場地"
    assert source_row["remind_at"] == _ts("2030-07-18 18:00")

    _archive_bot_message(
        "lead-time-event-reminder",
        "🔔 **1 週後活動提醒**\n"
        "📅 2030-07-25 16:00\n"
        "🎯 全家打羽球\n"
        "👥 黃聖雅",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="lead-time-event-reminder"),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert replies and "已取消提醒" in replies[0]
    with calendar_db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert status == ("active",)


def test_lead_time_calendar_cancel_race_reports_idempotent_success(monkeypatch):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="親子打羽球",
        event_date="2030-08-03",
        event_time="15:00",
        participants=["爸爸"],
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND
        and row["source_ref"] == event_id
    )
    _archive_bot_message(
        "racing-event-reminder",
        "🔔 **1 週後活動提醒**\n"
        "📅 2030-08-03 15:00\n"
        "🎯 親子打羽球\n"
        "👥 爸爸",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    original_cancel = memory.cancel_reminder_for_source

    def lose_after_other_worker_wins(*args, **kwargs):
        assert original_cancel(*args, **kwargs) is not None
        return None

    replies: list[str] = []
    monkeypatch.setattr(
        main.memory,
        "cancel_reminder_for_source",
        lose_after_other_worker_wins,
    )
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="racing-event-reminder"),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert replies == ["這則提醒已經取消過了，不會再推送。"]


def test_source_cancel_retries_when_sender_moves_pending_to_done(monkeypatch):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="朋友打羽球",
        event_date="2030-08-10",
        event_time="14:00",
        participants=["爸爸"],
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    _archive_bot_message(
        "sender-race-event-reminder",
        "🔔 **1 週後活動提醒**\n"
        "📅 2030-08-10 14:00\n"
        "🎯 朋友打羽球\n"
        "👥 爸爸",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    original_cancel = memory.cancel_reminder_for_source
    calls = 0

    def sender_wins_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert memory.mark_reminder_pushed(source_row["reminder_id"], "now")
            return None
        return original_cancel(*args, **kwargs)

    replies: list[str] = []
    monkeypatch.setattr(
        main.memory,
        "cancel_reminder_for_source",
        sender_wins_first,
    )
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="sender-race-event-reminder"),
        "G1",
    )

    assert calls == 2
    assert _status(source_row["reminder_id"]) == "cancelled"
    assert replies and "已取消提醒" in replies[0]


def test_later_event_notification_can_tombstone_done_lead_time_source(
    monkeypatch,
):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭打羽球",
        event_date="2030-07-25",
        event_time="16:00",
        participants=["媽媽"],
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND
        and row["source_ref"] == event_id
    )
    assert memory.mark_reminder_pushed(source_row["reminder_id"], "now")
    assert _status(source_row["reminder_id"]) == "done"

    monkeypatch.setattr(calendar_db, "_today_tw", lambda: date(2030, 7, 22))
    assert [
        event["event_id"]
        for event in calendar_db.list_due_for_reminder("G1", days_ahead=3)
    ] == [event_id]

    _archive_bot_message(
        "later-event-reminder",
        "🔔 **3 天後活動提醒**\n"
        "📅 2030-07-25 16:00\n"
        "🎯 家庭打羽球\n"
        "👥 媽媽",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND,
        source_ref=event_id,
    )
    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="later-event-reminder"),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert replies and "已取消提醒" in replies[0]
    assert calendar_db.list_due_for_reminder("G1", days_ahead=3) == []
    with calendar_db._conn() as conn:
        status = conn.execute(
            "SELECT status FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert status == ("active",)


def test_historical_natural_message_id_pivots_to_done_calendar_source(
    monkeypatch,
):
    """A pre-migration reminder_id binding must still stop later event stages."""

    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐",
        event_date="2030-07-25",
        event_time="16:00",
        participants=["媽媽"],
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND
        and row["source_ref"] == event_id
    )
    assert memory.mark_reminder_pushed(source_row["reminder_id"], "now")
    assert _status(source_row["reminder_id"]) == "done"

    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES ('G1', 'historical-natural-reminder', '__bot__', ?, 1)",
            ("⏰ 提醒（現在 / 即將到時）\n2030-07-25 16:00 家庭聚餐",),
        )
        # Historical rows stored only reminder_id. The mapped reminder itself
        # carries the durable calendar source and must be pivoted at cancel time.
        conn.execute(
            "INSERT INTO sent_reminder_refs("
            "group_id, message_id, reminder_id, source_kind, source_ref, created_at"
            ") VALUES ('G1', 'historical-natural-reminder', ?, '', '', 1)",
            (source_row["reminder_id"],),
        )

    monkeypatch.setattr(calendar_db, "_today_tw", lambda: date(2030, 7, 22))
    assert [
        event["event_id"]
        for event in calendar_db.list_due_for_reminder("G1", days_ahead=3)
    ] == [event_id]

    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("這則取消", quoted_message_id="historical-natural-reminder"),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert replies and "已取消提醒" in replies[0]
    assert calendar_db.list_due_for_reminder("G1", days_ahead=3) == []


def test_pasted_done_natural_reminder_tombstones_calendar_source(monkeypatch):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐",
        event_date="2030-07-25",
        event_time="16:00",
        participants=["媽媽"],
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_kind"] == calendar_db.EVENT_REMINDER_SOURCE_KIND
        and row["source_ref"] == event_id
    )
    assert memory.mark_reminder_pushed(source_row["reminder_id"], "now")
    assert _status(source_row["reminder_id"]) == "done"
    monkeypatch.setattr(calendar_db, "_today_tw", lambda: date(2030, 7, 22))
    assert [
        event["event_id"]
        for event in calendar_db.list_due_for_reminder("G1", days_ahead=3)
    ] == [event_id]

    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n"
            "⏰ 提醒（現在 / 即將到時）\n"
            "2030-07-25 16:00 家庭聚餐"
        ),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert replies and "已取消提醒" in replies[0]
    assert calendar_db.list_due_for_reminder("G1", days_ahead=3) == []


@pytest.mark.parametrize("terminal_status", ["done", "expired", "cancelled"])
def test_pasted_terminal_source_and_recreated_pending_is_ambiguous(
    monkeypatch,
    terminal_status,
):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐",
        event_date="2030-07-25",
        event_time="16:00",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    with memory._conn() as conn:
        conn.execute(
            "UPDATE reminders SET status=? WHERE reminder_id=?",
            (terminal_status, source_row["reminder_id"]),
        )
    generic_id = _seed("G1", "家庭聚餐", "2030-07-25 16:00")

    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event(
            "取消提醒\n"
            "⏰ 提醒（現在 / 即將到時）\n"
            "2030-07-25 16:00 家庭聚餐"
        ),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == terminal_status
    assert _status(generic_id) == "pending"
    assert replies
    assert "已取消提醒" not in replies[0]
    assert "無法確認" in replies[0] or "多筆" in replies[0]


def test_pasted_pending_source_cancels_nfkc_equivalent_generic_peer(
    monkeypatch,
):
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id="G1",
        title="ＡＢＣ",
        event_date="2030-07-25",
        event_time="16:00",
    )
    source_row = next(
        row
        for row in memory.list_reminder_cancellation_candidates("G1")
        if row["source_ref"] == event_id
    )
    generic_id = _seed("G1", "ABC", "2030-07-25 16:00")

    replies: list[str] = []
    monkeypatch.setattr(
        main,
        "_reply",
        lambda _token, text, **_kwargs: replies.append(text),
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)

    main._handle_text_message(
        _event("取消提醒\n2030-07-25 16:00 ABC"),
        "G1",
    )

    assert _status(source_row["reminder_id"]) == "cancelled"
    assert _status(generic_id) == "cancelled"
    assert replies and "已取消提醒" in replies[0]


def test_cancellation_reply_disables_every_auxiliary_message(monkeypatch):
    captured: list[str] = []

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, request):
            captured.extend(message.text for message in request.messages)
            return SimpleNamespace(sent_messages=[])

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("cancellation reply must not inspect auxiliary queues")

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", must_not_run)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", must_not_run)
    monkeypatch.setattr(
        main.memory,
        "claim_reminder_confirmations",
        must_not_run,
    )
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda _token: None)

    main._reply(
        "reply-token",
        "已取消提醒",
        group_id="G1",
        include_auxiliary=False,
    )

    assert captured == ["已取消提醒"]


def test_normal_reply_archive_failure_is_nonfatal_after_delivery(monkeypatch):
    deliveries = 0

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, _request):
            nonlocal deliveries
            deliveries += 1
            return SimpleNamespace(
                sent_messages=[SimpleNamespace(id="normal-reply-sent")]
            )

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda _token: None)
    monkeypatch.setattr(
        main.memory,
        "log_raw_message",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("archive down")),
    )

    main._reply(
        "reply-token",
        "已取消提醒",
        group_id="G1",
        include_auxiliary=False,
    )

    assert deliveries == 1


def test_normal_reply_piggyback_archives_natural_reminder_identity(monkeypatch):
    import calendar_db

    archived: list[tuple] = []
    references: list[tuple[tuple, dict]] = []
    lifecycle_order: list[str] = []

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, _request):
            return SimpleNamespace(
                sent_messages=[
                    SimpleNamespace(id="primary-sent"),
                    SimpleNamespace(id="piggyback-reminder-sent"),
                ]
            )

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(main.memory, "claim_reminder_confirmations", lambda *_a, **_k: [])
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *_a, **_k: [])
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *_a, **_k: [
                {
                    "reminder_id": 88,
                    "stage": "1d",
                    "text": "⏰ 提醒（明天）\n2030-01-02 04:00 繳費",
                    "message": TextMessage(text="⏰ 提醒\n2030-01-02 04:00 繳費"),
                    "action": "繳費",
                    "remind_at": _ts("2030-01-02 04:00"),
                    "weekly_count": 0,
                }
            ],
        )
    monkeypatch.setattr(main.memory, "is_reminder_pending", lambda *_a, **_k: True)
    monkeypatch.setattr(
        main.memory,
        "claim_natural_reminder_delivery",
        lambda *_a, **_k: {
            "reminder_id": 88,
            "claim_token": "claim",
        },
    )
    monkeypatch.setattr(
        main.memory,
        "finalize_natural_reminder_delivery",
        lambda _claim: lifecycle_order.append("finalize") or True,
    )
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda _token: None)
    monkeypatch.setattr(
        main.memory,
        "log_raw_message",
        lambda *args: archived.append(args),
    )
    monkeypatch.setattr(
        main.memory,
        "log_sent_reminder_reference",
        lambda *args, **kwargs: (
            lifecycle_order.append("archive"),
            references.append((args, kwargs)),
        )[-1],
    )

    main._reply("reply-token", "一般回覆", group_id="G1")

    assert [row[1] for row in archived] == [
        "primary-sent",
        "piggyback-reminder-sent",
    ]
    assert references == [
        (
            ("G1", "piggyback-reminder-sent"),
            {
                "reminder_id": 88,
                "source_kind": "",
                "source_ref": "",
            },
        )
    ]
    assert lifecycle_order == ["archive", "finalize"]


def test_event_reminder_push_archives_real_line_message_id(monkeypatch):
    import event_reminder

    archived: list[tuple[str, str, str | None, str]] = []
    references: list[tuple[tuple, dict]] = []

    def fake_push_messages(_group_id, _messages, **kwargs):
        kwargs["sent_message_ids"].append("event-reminder-sent")
        return True

    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "token")
    monkeypatch.setattr(event_reminder, "push_messages", fake_push_messages)
    monkeypatch.setattr(
        memory,
        "log_raw_message",
        lambda *args: archived.append(args),
    )
    monkeypatch.setattr(
        memory,
        "log_sent_reminder_reference",
        lambda *args, **kwargs: references.append((args, kwargs)),
    )

    assert (
        event_reminder._post_result(
            [{"type": "text", "text": "🔔 活動提醒"}],
            retry_key="retry-key",
            reminder_refs=[
                {
                    "source_kind": "calendar_event",
                    "source_ref": "event-7",
                }
            ],
        )
        == event_reminder.POST_OK
    )
    assert archived == [
        ("G1", "event-reminder-sent", "__bot__", "🔔 活動提醒")
    ]
    assert references == [
        (
            ("G1", "event-reminder-sent"),
            {
                "reminder_id": None,
                "source_kind": "calendar_event",
                "source_ref": "event-7",
            },
        )
    ]


def test_scheduled_sender_skips_reminder_cancelled_after_due_snapshot(monkeypatch):
    monkeypatch.setattr(
        reminder_push.memory,
        "delete_stale_pending_reminders",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        reminder_push,
        "_due_reminder_items",
        lambda: [
            {
                "reminder_id": 7,
                "group_id": "G1",
                "stage": "1d",
                "text": "⏰ 提醒（明天）\n2030-01-02 04:00 繳費",
                "message": TextMessage(text="提醒"),
                "action": "繳費",
            }
        ],
    )
    monkeypatch.setattr(
        reminder_push.memory,
        "is_reminder_pending",
        lambda _group_id, _reminder_id: False,
    )
    monkeypatch.setattr(
        reminder_push,
        "_push_to_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled reminder must not reach LINE")
        ),
    )

    assert reminder_push.push_reminders() == 0


def test_scheduled_sender_claims_before_external_delivery(monkeypatch):
    action = "繳信用卡"
    when = "2030-01-02 04:00"
    reminder_id = _seed("G1", action, when)
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        reminder_push.memory,
        "delete_stale_pending_reminders",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        reminder_push,
        "_due_reminder_items",
        lambda: [
            {
                "reminder_id": reminder_id,
                "group_id": "G1",
                "stage": "1d",
                "text": f"⏰ 提醒（明天）\n{when} {action}",
                "message": TextMessage(text="提醒"),
                "action": action,
                "remind_at": _ts(when),
                "weekly_count": 0,
            }
        ],
    )

    def fake_line_delivery(*_args, **_kwargs):
        cancelled = memory.cancel_pending_reminder(
            "G1",
            reminder_id,
            action,
            _ts(when),
        )
        assert cancelled is not None
        observed["in_flight"] = cancelled.get("_delivery_in_flight")
        return True

    monkeypatch.setattr(reminder_push, "_push_to_group", fake_line_delivery)

    assert reminder_push.push_reminders() == 1
    assert observed["in_flight"] is True
    assert _status(reminder_id) == "cancelled"
    with memory._conn() as conn:
        pushed, claims = conn.execute(
            "SELECT pushed_1d, "
            "(SELECT COUNT(*) FROM reminder_delivery_claims) "
            "FROM reminders WHERE reminder_id=?",
            (reminder_id,),
        ).fetchone()
    assert pushed == 1
    assert claims == 0


def test_event_sender_claims_before_external_delivery(monkeypatch):
    import calendar_db
    import event_reminder

    event_date = (calendar_db._today_tw() + timedelta(days=1)).isoformat()
    event_id = calendar_db.insert_event(
        group_id="G1",
        title="家庭聚餐",
        event_date=event_date,
        event_time="18:00",
    )
    source_row = next(
        row
        for row in memory.list_reminder_source_cancellation_candidates(
            "G1",
            calendar_db.EVENT_REMINDER_SOURCE_KIND,
            event_id,
        )
    )
    observed: dict[str, object] = {}

    def fake_line_delivery(_spec, **_kwargs):
        cancelled = memory.cancel_reminder_for_source(
            "G1",
            int(source_row["reminder_id"]),
            str(source_row["action"]),
            int(source_row["remind_at"]),
            calendar_db.EVENT_REMINDER_SOURCE_KIND,
            event_id,
            str(source_row["status"]),
        )
        assert cancelled is not None
        observed["in_flight"] = cancelled.get("_delivery_in_flight")
        return event_reminder.POST_OK

    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(calendar_db, "REMINDER_OFFSETS", (1,))
    monkeypatch.setattr(
        event_reminder,
        "_send_reminder_message_spec",
        fake_line_delivery,
    )

    assert event_reminder.main() == 0
    assert observed["in_flight"] is True
    assert _status(source_row["reminder_id"]) == "cancelled"
    with calendar_db._conn() as conn:
        reminded = conn.execute(
            "SELECT reminded_1d FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()[0]
    assert reminded is not None


def test_fast_reply_claims_natural_reminder_before_line(monkeypatch):
    import calendar_db

    action = "繳電費"
    when = "2030-01-02 04:00"
    reminder_id = _seed("G1", action, when)
    observed: dict[str, object] = {}

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, _request):
            cancelled = memory.cancel_pending_reminder(
                "G1",
                reminder_id,
                action,
                _ts(when),
            )
            assert cancelled is not None
            observed["in_flight"] = cancelled.get("_delivery_in_flight")
            return SimpleNamespace(sent_messages=[])

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *_a, **_k: [])
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *_a, **_k: [
            {
                "reminder_id": reminder_id,
                "stage": "1d",
                "text": f"⏰ 提醒（明天）\n{when} {action}",
                "message": TextMessage(text="提醒"),
                "action": action,
                "remind_at": _ts(when),
                "weekly_count": 0,
            }
        ],
    )
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda _token: None)

    assert main._try_piggyback_reminders_fast_path("reply-token", "G1")
    assert observed["in_flight"] is True
    assert _status(reminder_id) == "cancelled"


def test_normal_reply_claims_event_before_line(monkeypatch):
    import calendar_db

    event_date = (calendar_db._today_tw() + timedelta(days=1)).isoformat()
    event_id = calendar_db.insert_event(
        group_id="G1",
        title="拿生日蛋糕",
        event_date=event_date,
        event_time="14:00",
    )
    source_row = next(
        row
        for row in memory.list_reminder_source_cancellation_candidates(
            "G1",
            calendar_db.EVENT_REMINDER_SOURCE_KIND,
            event_id,
        )
    )
    observed: dict[str, object] = {}

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, request):
            observed["message_count"] = len(request.messages)
            cancelled = memory.cancel_reminder_for_source(
                "G1",
                int(source_row["reminder_id"]),
                str(source_row["action"]),
                int(source_row["remind_at"]),
                calendar_db.EVENT_REMINDER_SOURCE_KIND,
                event_id,
                str(source_row["status"]),
            )
            assert cancelled is not None
            observed["in_flight"] = cancelled.get("_delivery_in_flight")
            return SimpleNamespace(sent_messages=[])

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(
        main.memory,
        "claim_reminder_confirmations",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(calendar_db, "REMINDER_OFFSETS", (1,))
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda _token: None)

    main._reply("reply-token", "一般回覆", group_id="G1")

    assert observed["message_count"] == 2
    assert observed["in_flight"] is True
    assert _status(source_row["reminder_id"]) == "cancelled"


def test_fast_path_failed_claim_blocks_cancelled_reminder(monkeypatch):
    import calendar_db

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *_a, **_k: [])
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *_a, **_k: [
            {
                "reminder_id": 7,
                "stage": "1d",
                "text": "⏰ 提醒（明天）\n2030-01-02 04:00 繳費",
                "message": TextMessage(text="提醒"),
                "action": "繳費",
                "remind_at": _ts("2030-01-02 04:00"),
                "weekly_count": 0,
            }
        ],
    )
    monkeypatch.setattr(
        main.memory,
        "is_reminder_pending",
        lambda _group_id, _reminder_id: True,
    )
    monkeypatch.setattr(
        main.memory,
        "claim_natural_reminder_delivery",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        main,
        "ApiClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled reminder must not reach LINE")
        ),
    )

    assert not main._try_piggyback_reminders_fast_path("reply-token", "G1")


def test_normal_reply_failed_claim_drops_cancelled_piggyback(monkeypatch):
    import calendar_db

    captured: list[str] = []

    class FakeApiClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, request):
            captured.extend(message.text for message in request.messages)
            return SimpleNamespace(sent_messages=[])

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(
        main.memory,
        "claim_reminder_confirmations",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *_a, **_k: [])
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *_a, **_k: [
            {
                "reminder_id": 7,
                "stage": "1d",
                "text": "⏰ 提醒（明天）\n2030-01-02 04:00 繳費",
                "message": TextMessage(text="提醒"),
                "action": "繳費",
                "remind_at": _ts("2030-01-02 04:00"),
                "weekly_count": 0,
            }
        ],
    )
    monkeypatch.setattr(
        main.memory,
        "is_reminder_pending",
        lambda _group_id, _reminder_id: True,
    )
    monkeypatch.setattr(
        main.memory,
        "claim_natural_reminder_delivery",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(main, "ApiClient", FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda _token: None)

    main._reply("reply-token", "一般回覆", group_id="G1")

    assert captured == ["一般回覆"]
