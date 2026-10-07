"""Quoted reminder reschedule (2026-10-03 proposal): synthetic data only."""

from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import main
import memory
import reminder_cancel
import reminder_push
import reminder_reschedule as rr


TW = rr.TAIPEI
_IDS = itertools.count(1)


def _ts(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=TW).timestamp())


def _when(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, TW).strftime("%Y-%m-%d %H:%M")


def _seed(action: str, when: str, *, group_id: str = "G1", user_id: str = "U1") -> int:
    reminder_id = memory.add_reminder(
        group_id, user_id, action, _ts(when), source_text=action
    )
    assert reminder_id is not None
    return int(reminder_id)


def _row(reminder_id: int) -> dict:
    with memory._conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _archive_bot(message_id: str, text: str, *, reminder_id: int | None = None,
                 group_id: str = "G1", source_kind: str = "", source_ref: str = "") -> None:
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, '__bot__', ?, 1)",
            (group_id, message_id, text),
        )
    if reminder_id is not None or (source_kind and source_ref):
        assert memory.log_sent_reminder_reference(
            group_id, message_id, reminder_id=reminder_id,
            source_kind=source_kind, source_ref=source_ref,
        )


def _archive_human(message_id: str, text: str, *, group_id: str = "G1") -> None:
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, 'U2', ?, 1)",
            (group_id, message_id, text),
        )


def _event(text: str, *, quoted: str | None, message_id: str | None = None,
           timestamp: int | None = None, group_id: str = "G1") -> MessageEvent:
    message = MagicMock(spec=TextMessageContent)
    message.id = message_id or f"incoming-{next(_IDS)}"
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted
    message.quote_token = "quote-token"
    message.type = "text"
    source = MagicMock(spec=GroupSource)
    source.group_id = group_id
    source.user_id = "U2"
    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "reply-token"
    event.timestamp = timestamp
    event.delivery_context = SimpleNamespace(is_redelivery=False)
    return event


@pytest.fixture
def replies(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("a handled reschedule must stop all later routing")

    for name in (
        "_try_handle_quoted_calendar_correction",
        "_try_handle_creation_followup",
        "_try_handle_calendar_correction",
        "_try_one_shot_reply",
        "_auto_capture_text_if_important",
        "_maybe_extract_reminder",
    ):
        monkeypatch.setattr(main, name, must_not_run)
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(
        main, "_reply", lambda _token, text, **kwargs: captured.append((text, kwargs))
    )
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: [])
    return captured


@pytest.fixture
def fixed_now(monkeypatch):
    """Freeze the parser's notion of now at 2099-09-27 13:48 (a Sunday)."""
    now = datetime(2099, 9, 27, 13, 48, tzinfo=TW)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(main, "datetime", _Frozen)
    return now


# ── Parser: what the current message says ──────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "好", "收到", "10/8我不行", "早上9點我在家", "改到10/8嗎？", "不是10/8",
        "10/8也要提醒我倒垃圾", "這則取消", "到時候再說", "改天再說", "到了",
        "在路上", "在忙", "到家了", "在公司", "20:00 才對", "改成去看牙醫",
        "10/8到銀行再提醒我繳費", "在永和區", "10/8 B1",
    ],
)
def test_ordinary_replies_are_not_claimed(text):
    assert rr.classify_reschedule_text(text).status == rr.NOT_RESCHEDULE


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("延後一天", "offset_unsupported"),
        ("10/8到銀行順便去郵局", "unsupported_location"),
        ("10/8 早上9點在家", "unsupported_location"),
        ("改成早上", "daypart_only"),
        ("明晚", "daypart_only"),
        ("9點", "ambiguous_hour"),
        ("3:00", "ambiguous_hour"),
        ("改成十點", "ambiguous_hour"),
        ("晚上1點", "bad_time"),
        ("傍晚11點", "bad_time"),
        ("早上15:00", "bad_time"),
        ("下午00:30", "bad_time"),
        ("晚上12點", "bad_time"),
        ("晚間12點", "bad_time"),
        ("半夜12點", "ambiguous_midnight"),
        ("10/8半夜12點", "ambiguous_midnight"),
        ("凌晨12點", "ambiguous_midnight"),
        ("9點到10點", "range"),
        ("改成9點到10點", "range"),
        ("10/8 10/9", "multiple_dates"),
    ],
)
def test_attempts_that_cannot_be_applied_are_invalid(text, reason):
    result = rr.classify_reschedule_text(text)
    assert (result.status, result.reason) == (rr.INVALID, reason)


CURRENT = "2099-10-01 12:00"


@pytest.mark.parametrize(
    ("text", "expected", "place"),
    [
        ("時間：早上 9 點", "2099-10-01 09:00", None),
        ("時間：早上0900", "2099-10-01 09:00", None),
        ("10月8日週四可到中和區測試路100號3樓", "2099-10-08 12:00", "中和區測試路100號3樓"),
        ("10/8 0900", "2099-10-08 09:00", None),
        ("時間：10/8 0900", "2099-10-08 09:00", None),
        ("10/8 8pm", "2099-10-08 20:00", None),
        ("改期到10/8", "2099-10-08 12:00", None),
        ("幫我改到10/8", "2099-10-08 12:00", None),
        ("好，改到10/8", "2099-10-08 12:00", None),
        ("@咪寶 改到10/8", "2099-10-08 12:00", None),
        ("改成十月八號", "2099-10-08 12:00", None),
        ("延到10/8，地點：中山路1號", "2099-10-08 12:00", "中山路1號"),
        ("改到10/8在中山路1號2樓", "2099-10-08 12:00", "中山路1號2樓"),
        ("10/8在永和區", "2099-10-08 12:00", "永和區"),
        ("10/8 地點：B1", "2099-10-08 12:00", "B1"),
        ("10/8在A棟", "2099-10-08 12:00", "A棟"),
        ("10/8到停車場", "2099-10-08 12:00", "停車場"),
        ("10/8到和平東路二段", "2099-10-08 12:00", "和平東路二段"),
        ("09:00", "2099-10-01 09:00", None),
        ("15:00", "2099-10-01 15:00", None),
        ("下午3點", "2099-10-01 15:00", None),
        ("中午1點", "2099-10-01 13:00", None),
        ("晚上8點", "2099-10-01 20:00", None),
        ("凌晨3點", "2099-10-01 03:00", None),
        ("早上九點零五分", "2099-10-01 09:05", None),
        ("早上九點半", "2099-10-01 09:30", None),
        ("時間改成晚上八點", "2099-10-01 20:00", None),
        ("設定成早上9點", "2099-10-01 09:00", None),
        ("應該是 20:00", "2099-10-01 20:00", None),
        ("明天", "2099-09-28 12:00", None),
        ("明晚7點", "2099-09-28 19:00", None),
        ("週四", "2099-10-01 12:00", None),
        ("下週四", "2099-10-01 12:00", None),
        ("10/8(四)", "2099-10-08 12:00", None),
        ("4/10", "2100-04-10 12:00", None),
    ],
)
def test_candidates_resolve_against_the_quoted_reminder(text, expected, place):
    now = datetime(2099, 9, 27, 13, 48, tzinfo=TW)
    request = rr.classify_reschedule_text(text)
    assert request.status == rr.CANDIDATE, (text, request)
    status, new_at = rr.resolve_new_schedule(
        request, current_remind_at=_ts(CURRENT), message_time=now, now=now
    )
    assert status == "ok"
    assert _when(new_at) == expected
    assert request.location == place


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("10/8(五)", "weekday_mismatch"),
        ("2/30", "bad_date"),
        ("改成十月三十二號", "bad_date"),
        ("時間：早上 9 點", "past"),
    ],
)
def test_resolution_refusals(text, reason):
    now = datetime(2099, 10, 1, 10, 0, tzinfo=TW)
    request = rr.classify_reschedule_text(text)
    status, new_at = rr.resolve_new_schedule(
        request, current_remind_at=_ts(CURRENT), message_time=now, now=now
    )
    assert (status, new_at) == (reason, None)


def test_weekday_far_from_today_is_ambiguous():
    now = datetime(2099, 9, 27, 13, 48, tzinfo=TW)  # Sunday
    request = rr.classify_reschedule_text("改到週四")
    status, _ = rr.resolve_new_schedule(
        request, current_remind_at=_ts("2099-10-13 12:00"), message_time=now, now=now
    )
    assert status == "ambiguous_weekday"


def test_relative_day_uses_the_message_time_not_processing_time():
    sent = datetime(2099, 9, 27, 23, 59, tzinfo=TW)
    later = datetime(2099, 9, 28, 0, 5, tzinfo=TW)
    request = rr.classify_reschedule_text("明天")
    status, new_at = rr.resolve_new_schedule(
        request, current_remind_at=_ts(CURRENT), message_time=sent, now=later
    )
    assert status == "ok" and _when(new_at) == "2099-09-28 12:00"


# ── Parser: the quoted bot message ──────────────────────────────────────────

def test_quoted_confirmation_with_default_time_note_is_read():
    quoted = rr.parse_quoted_reminder(
        "{p1}\n已新增提醒\n時間：2099-10-01 12:00（未指定時間，預設 12:00）\n事項：繳管理費"
    )
    assert (quoted.status, quoted.action, _when(quoted.remind_at)) == (
        rr.QUOTED_ONE, "繳管理費", "2099-10-01 12:00"
    )


def test_quoted_push_with_mention_line_is_read():
    quoted = rr.parse_quoted_reminder(
        "@某人\n⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費\n參加人：某人"
    )
    assert (quoted.status, quoted.action) == (rr.QUOTED_ONE, "繳管理費")


@pytest.mark.parametrize(
    "text",
    [
        "已新增 2 筆提醒\n2099-10-08 15:00 甲\n2099-10-09 09:00 乙",
        "2 筆提醒皆已存在，未重複新增\n時間：2099-10-08 15:00\n事項：甲",
    ],
)
def test_multi_reminder_receipts_are_multiple(text):
    assert rr.parse_quoted_reminder(text).status == rr.QUOTED_MULTIPLE


def test_unrelated_bot_text_with_field_lines_is_not_a_reminder():
    text = "這是說明\n時間：2099-10-01 12:00\n事項：繳管理費"
    assert rr.parse_quoted_reminder(text).status == rr.QUOTED_NONE


def test_receipts_are_quotable_by_reminder_cancel():
    receipt = rr.updated_receipt(_ts(CURRENT), _ts("2099-10-08 12:00"), "繳管理費")
    assert receipt.startswith("已更新提醒（2099-10-01 12:00 → 2099-10-08 12:00）")
    quoted = rr.parse_quoted_reminder(receipt)
    assert (quoted.action, _when(quoted.remind_at)) == ("繳管理費", "2099-10-08 12:00")
    assert reminder_cancel._CREATION_RE.search(receipt)


def test_every_refusal_says_not_updated():
    for reason in rr._REFUSALS:
        assert rr.refusal_text(reason).startswith("尚未更新提醒：")
        assert "提醒沒有變更" in rr.refusal_text(reason)


@pytest.mark.parametrize(
    ("action", "place", "expected"),
    [
        ("繳管理費", "中山路1號", ("ok", "繳管理費，地點：中山路1號")),
        ("繳管理費，地點：中山路1號", "民生路2號", ("ok", "繳管理費，地點：民生路2號")),
        ("到郵局寄包裹", "郵局", ("ok", "到郵局寄包裹")),
        ("到郵局寄包裹", "中山路1號", ("location_conflict", "到郵局寄包裹")),
        ("繳管理費", None, ("ok", "繳管理費")),
    ],
)
def test_merge_location(action, place, expected):
    assert rr.merge_location(action, place) == expected


# ── memory: atomic write ────────────────────────────────────────────────────

def _claims() -> list:
    with memory._conn() as conn:
        return conn.execute("SELECT * FROM reminder_delivery_claims ORDER BY 1,2,3,4").fetchall()


def test_reschedule_resets_flags_and_logs(monkeypatch):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    assert memory.mark_reminder_pushed(rid, "3d")
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-1", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費",
    )
    assert result["status"] == "updated"
    row = _row(rid)
    assert row["remind_at"] == _ts("2099-10-08 12:00")
    assert all(row[col] == 0 for col in memory._REMINDER_PUSH_FLAG_COLUMNS)
    log = memory.get_reminder_reschedule_log("G1", "m-1")
    assert log["old_remind_at"] == _ts("2099-10-01 12:00")
    again = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-1", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-08 12:00"),
        new_remind_at=_ts("2099-10-09 12:00"), new_action="繳管理費",
    )
    assert again["status"] == "replayed"
    assert _row(rid)["remind_at"] == _ts("2099-10-08 12:00")


def test_place_only_change_keeps_push_flags():
    rid = _seed("繳管理費", "2099-10-01 12:00")
    assert memory.mark_reminder_pushed(rid, "3d")
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-place", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-01 12:00"), new_action="繳管理費，地點：中山路1號",
    )
    assert result["status"] == "updated"
    assert _row(rid)["pushed_3d"] == 1


@pytest.mark.parametrize("state", ["sending", "uncertain"])
def test_delivery_claims_block_and_are_kept(state):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    claim = memory.claim_natural_reminder_delivery(
        "G1", rid, "1d", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"), transport="push",
    )
    assert claim is not None
    if state == "uncertain":
        assert memory.mark_reminder_delivery_claim_uncertain(claim)
    before_claims, before_row = _claims(), _row(rid)
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-claim", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費",
    )
    assert result["status"] == ("busy" if state == "sending" else "delivery_uncertain")
    assert _claims() == before_claims and _row(rid) == before_row
    assert memory.get_reminder_reschedule_log("G1", "m-claim") is None


def test_refusals_write_nothing():
    rid = _seed("繳管理費", "2099-10-01 12:00")
    other = _seed("倒垃圾", "2099-10-08 12:00")
    cases = [
        dict(expected_action="別的", expected_remind_at=_ts("2099-10-01 12:00"),
             new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費"),
        dict(expected_action="繳管理費", expected_remind_at=_ts("2099-10-01 12:00"),
             new_remind_at=_ts("2099-10-08 12:00"), new_action="倒垃圾"),
    ]
    for index, kwargs in enumerate(cases):
        before = (_row(rid), _row(other))
        result = memory.reschedule_generic_reminder(
            "G1", rid, inbound_message_id=f"m-ref-{index}", **kwargs
        )
        assert result["status"] == ("conflict", "collision")[index]
        assert (_row(rid), _row(other)) == before
        assert memory.get_reminder_reschedule_log("G1", f"m-ref-{index}") is None


def test_duplicate_rows_at_the_old_time_are_refused():
    rid = _seed("繳管理費", "2099-10-01 12:00")
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,source_text) "
            "VALUES('G1','U9','繳管理費',?,1,'pending','繳管理費')",
            (_ts("2099-10-01 12:00"),),
        )
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-dup", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費",
    )
    assert result["status"] == "duplicate"


def test_other_group_and_terminal_rows_are_refused():
    rid = _seed("繳管理費", "2099-10-01 12:00", group_id="G2")
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-g", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費",
    )
    assert result["status"] == "not_found"
    done = _seed("倒垃圾", "2099-10-01 12:00")
    assert memory.mark_reminder_pushed(done, "now")
    result = memory.reschedule_generic_reminder(
        "G1", done, inbound_message_id="m-t", expected_action="倒垃圾",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="倒垃圾",
    )
    assert result["status"] == "terminal"


def test_queued_source_extraction_is_dropped_and_processing_blocks():
    rid = _seed("繳管理費", "2099-10-01 12:00")
    with memory._conn() as conn:
        conn.execute(
            "INSERT INTO pending_reminder_extract(group_id,user_id,message_id,text,created_at,status) "
            "VALUES('G1','U1','src-1','繳管理費',1,'pending')"
        )
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-q", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費",
    )
    assert result["status"] == "updated"
    with memory._conn() as conn:
        status = conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE message_id='src-1'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE pending_reminder_extract SET status='processing' WHERE message_id='src-1'"
        )
    assert status == "dropped"
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-q2", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-08 12:00"),
        new_remind_at=_ts("2099-10-09 12:00"), new_action="繳管理費",
    )
    assert result["status"] == "busy"


# ── main: end-to-end routing ────────────────────────────────────────────────

def test_acceptance_1_bound_push_time_only(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-1", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("時間：早上 9 點", quoted="push-1"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-01 09:00")
    assert len(replies) == 1
    text, kwargs = replies[0]
    assert text.startswith("已更新提醒（2099-10-01 12:00 → 2099-10-01 09:00）")
    assert "事項：繳管理費" in text
    assert kwargs["include_auxiliary"] is False
    assert kwargs["primary_reminder_ref"] == {"reminder_id": rid}


def test_acceptance_2_unbound_confirmation_date_only(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot(
        "ack-1",
        "{p1}\n已新增提醒\n時間：2099-10-01 12:00（未指定時間，預設 12:00）\n事項：繳管理費",
    )
    main._handle_text_message(_event("10月8日", quoted="ack-1"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-08 12:00")
    assert replies[0][0].startswith("已更新提醒（2099-10-01 12:00 → 2099-10-08 12:00）")


def test_case_a_shape_appends_place(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-a", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(
        _event("10月12日週一可到中和區測試路100號3樓", quoted="push-a"), "G1"
    )
    row = _row(rid)
    assert row["remind_at"] == _ts("2099-10-12 12:00")
    assert row["action"] == "繳管理費，地點：中和區測試路100號3樓"
    assert "事項：繳管理費，地點：中和區測試路100號3樓" in replies[0][0]


@pytest.mark.parametrize(
    ("text", "setup"),
    [
        ("10月8日", "duplicate"),
        ("改成早上", "plain"),
        ("10/8(五)", "plain"),
    ],
)
def test_acceptance_3_refusals_leave_reminder_unchanged(replies, fixed_now, text, setup):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    if setup == "duplicate":
        with memory._conn() as conn:
            conn.execute(
                "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,source_text) "
                "VALUES('G1','U9','繳管理費',?,1,'pending','繳管理費')",
                (_ts("2099-10-01 12:00"),),
            )
    _archive_bot("ack-3", "已新增提醒\n時間：2099-10-01 12:00\n事項：繳管理費")
    before = _row(rid)
    main._handle_text_message(_event(text, quoted="ack-3"), "G1")
    assert _row(rid) == before
    assert replies and replies[0][0].startswith("尚未更新提醒：")
    assert replies[0][1].get("primary_reminder_ref") is None


def test_stale_bound_quote_is_refused(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-old", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    assert memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-prev", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-12 12:00"), new_action="繳管理費",
    )["status"] == "updated"
    main._handle_text_message(_event("時間：早上 9 點", quoted="push-old"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-12 12:00")
    assert replies[0][0].startswith("尚未更新提醒：這筆提醒之後已經被改過")


def test_redelivery_replays_instead_of_overwriting_a_later_edit(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("ack-r", "已新增提醒\n時間：2099-10-01 12:00\n事項：繳管理費")
    main._handle_text_message(_event("10/8", quoted="ack-r", message_id="msg-a"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-08 12:00")
    assert memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="msg-b", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-08 12:00"),
        new_remind_at=_ts("2099-10-10 12:00"), new_action="繳管理費",
    )["status"] == "updated"
    main._handle_text_message(_event("10/8", quoted="ack-r", message_id="msg-a"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-10 12:00")
    assert replies[-1][0].startswith("這則更正先前已處理（2099-10-01 12:00 → 2099-10-08 12:00）")
    assert "時間：2099-10-10 12:00" in replies[-1][0]


def test_quoting_the_receipt_then_cancel_cancels_moved_reminder(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("ack-c", "已新增提醒\n時間：2099-10-01 12:00\n事項：繳管理費")
    main._handle_text_message(_event("10/8", quoted="ack-c"), "G1")
    receipt = replies[0][0]
    _archive_bot("receipt-c", receipt, reminder_id=rid)
    main._handle_text_message(_event("這則取消", quoted="receipt-c"), "G1")
    assert _row(rid)["status"] == "cancelled"


@pytest.mark.parametrize(
    "text", ["好", "收到", "到了", "在公司", "早上9點我在家", "10/8也要提醒我倒垃圾"]
)
def test_ordinary_replies_to_a_reminder_fall_through(monkeypatch, text):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-n", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    called = []
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: called.append("reply"))
    assert main._try_handle_quoted_reminder_reschedule(
        _event(text, quoted="push-n"), "G1", text
    ) is False
    assert called == []


def test_human_quote_is_not_claimed():
    _archive_human("human-1", "已新增提醒\n時間：2099-10-01 12:00\n事項：繳管理費")
    _seed("繳管理費", "2099-10-01 12:00")
    assert main._try_handle_quoted_reminder_reschedule(
        _event("10/8", quoted="human-1"), "G1", "10/8"
    ) is False


def test_calendar_quote_with_marker_is_handed_back(monkeypatch, replies, fixed_now):
    import calendar_db

    calendar_db.init_db()
    event_id = calendar_db.insert_event(
        group_id="G1", title="全家打球", event_date="2099-10-01",
        event_time="19:00", participants=["全家"], source_msg_id="src-cal",
    )
    _archive_bot(
        "cal-push", "🔔 **明天活動提醒**\n📅 2099-10-01 19:00\n🎯 全家打球",
        source_kind=calendar_db.EVENT_REMINDER_SOURCE_KIND, source_ref=event_id,
    )
    assert main._try_handle_quoted_reminder_reschedule(
        _event("改成 20:00", quoted="cal-push"), "G1", "改成 20:00"
    ) is False
    assert main._try_handle_quoted_reminder_reschedule(
        _event("時間：晚上8點", quoted="cal-push"), "G1", "時間：晚上8點"
    ) is True
    assert replies[-1][0].startswith("尚未更新提醒：這則是行事曆活動的提醒")


def test_generic_quote_with_unsupported_change_is_refused_honestly(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-u", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("改成去看牙醫", quoted="push-u"), "G1")
    assert replies[0][0] == rr.refusal_text("unsupported_change")
    main._handle_text_message(_event("改到10/8嗎？", quoted="push-u"), "G1")
    assert replies[1][0] == rr.refusal_text("question")
    assert _row(rid)["remind_at"] == _ts("2099-10-01 12:00")


def test_receipt_hidden_by_outbound_gate_changes_nothing(replies, fixed_now):
    rid = _seed("檢查 Gemini API key", "2099-10-01 12:00")
    _archive_bot(
        "push-g", "⏰ 提醒（4 天後）\n2099-10-01 12:00 檢查 Gemini API key", reminder_id=rid
    )
    main._handle_text_message(_event("10/8", quoted="push-g"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-01 12:00")
    assert replies[0][0] == rr.refusal_text("display_unsafe")


def test_due_stage_after_move_is_pushed_normally(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-w", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("10/8", quoted="push-w"), "G1")
    row = _row(rid)
    # No suppression: the next push cycle sends the stage that is due now,
    # like right after creating a reminder (reviewers #1-#3, plan v3 C4).
    now = int(fixed_now.timestamp())
    assert reminder_push._decide_stage(row, now) == "weekly"
    due = reminder_push._due_reminder_items(group_id="G1", now=now)
    assert [(item["reminder_id"], item["stage"]) for item in due] == [(rid, "weekly")]


def test_real_reply_binds_the_receipt(monkeypatch, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-real", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    monkeypatch.setattr(main.settings, "bot_muted", False)
    sent: dict = {}

    class _Api:
        def __init__(self, _client):
            pass

        def reply_message(self, request):
            sent["texts"] = [message.text for message in request.messages]
            return SimpleNamespace(sent_messages=[SimpleNamespace(id="line-receipt-1")])

    class _Client:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(main, "ApiClient", _Client)
    monkeypatch.setattr(main, "MessagingApi", _Api)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: [])
    main._handle_text_message(_event("時間：早上 9 點", quoted="push-real"), "G1")
    assert sent["texts"][0].startswith("已更新提醒（2099-10-01 12:00 → 2099-10-01 09:00）")
    assert memory.get_sent_reminder_reference("G1", "line-receipt-1") == {
        "reminder_id": rid, "source_kind": "", "source_ref": "",
    }


# ── Reviewer #4 additions ───────────────────────────────────────────────────

def test_chat_reply_that_only_starts_like_a_receipt_is_not_a_reminder():
    text = "已新增提醒\n時間：2099-10-01 12:00\n事項：繳管理費\n另外記得帶傘喔"
    assert rr.parse_quoted_reminder(text).status == rr.QUOTED_NONE


def test_parser_failure_before_ownership_keeps_old_routing(monkeypatch):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-x", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)

    def boom(_text):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(rr, "classify_reschedule_text", boom)
    assert main._try_handle_quoted_reminder_reschedule(
        _event("10/8", quoted="push-x"), "G1", "10/8"
    ) is False
    assert _row(rid)["remind_at"] == _ts("2099-10-01 12:00")


def test_reference_to_another_groups_reminder_is_refused_without_action(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00", group_id="G2")
    _archive_bot("push-cross", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("10/8", quoted="push-cross"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-01 12:00")
    assert replies[0][0].startswith("尚未更新提醒：")
    assert "繳管理費" not in replies[0][0]


@pytest.mark.parametrize(
    "text",
    ["1" * 120, "早上" * 60, "到" * 120, "路" * 120, "改到" * 60, "10/8" * 30,
     "在" + "測試路" * 39, "十" * 120],
)
def test_adversarial_input_is_fast(text):
    import time

    start = time.perf_counter()
    rr.classify_reschedule_text(text)
    rr.parse_quoted_reminder("已新增提醒\n" + text)
    assert time.perf_counter() - start < 0.5


def test_long_input_is_not_claimed():
    assert rr.classify_reschedule_text("10/8 " + "x" * 200).status == rr.NOT_RESCHEDULE


def test_log_keeps_no_action_text_and_is_pruned():
    rid = _seed("繳管理費", "2099-10-01 12:00")
    assert memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-priv", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費，地點：中山路1號",
    )["status"] == "updated"
    with memory._conn() as conn:
        stored = " ".join(str(v) for v in conn.execute(
            "SELECT * FROM reminder_reschedule_log").fetchone())
        assert "繳管理費" not in stored and "中山路" not in stored
        conn.execute("UPDATE reminder_reschedule_log SET created_at=1")
    assert memory.get_reminder_reschedule_log("G1", "m-priv") is None
    other = _seed("倒垃圾", "2099-10-01 12:00")
    assert memory.reschedule_generic_reminder(
        "G1", other, inbound_message_id="m-next", expected_action="倒垃圾",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-09 12:00"), new_action="倒垃圾",
    )["status"] == "updated"
    with memory._conn() as conn:
        left = conn.execute(
            "SELECT message_id FROM reminder_reschedule_log ORDER BY message_id"
        ).fetchall()
    assert left == [("m-next",)]


def test_time_change_follows_update_reminder_schedule(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    with memory._conn() as conn:
        conn.execute(
            "UPDATE reminders SET time_kind='none', merged_details=? WHERE reminder_id=?",
            ('[{"key": "k1", "action": "繳管理費", "text": "另一個說法"}]', rid),
        )
    _archive_bot("push-k", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("10/8", quoted="push-k"), "G1")
    row = _row(rid)
    assert row["remind_at"] == _ts("2099-10-08 12:00")
    assert (row["time_kind"], row["merged_details"]) == ("clock", "[]")


def test_place_only_change_keeps_time_kind_and_details(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    details = '[{"key": "k1", "action": "繳管理費", "text": "另一個說法"}]'
    with memory._conn() as conn:
        conn.execute(
            "UPDATE reminders SET time_kind='none', merged_details=? WHERE reminder_id=?",
            (details, rid),
        )
    _archive_bot("push-p", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("12:00 地點：中山路1號", quoted="push-p"), "G1")
    row = _row(rid)
    assert row["action"] == "繳管理費，地點：中山路1號"
    assert (row["time_kind"], row["merged_details"]) == ("none", details)


# ── Post-implementation review (Codex) additions ────────────────────────────

@pytest.mark.parametrize("text", ["晚上8am", "今晚8:30 am", "早上8pm"])
def test_am_pm_contradicting_a_daypart_is_invalid(text):
    result = rr.classify_reschedule_text(text)
    assert (result.status, result.reason) == (rr.INVALID, "bad_time")


def test_clock_before_date_keeps_both(replies, fixed_now):
    request = rr.classify_reschedule_text("早上9點 10/8")
    assert request.status == rr.CANDIDATE
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-o", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("早上9點 10/8", quoted="push-o"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-08 09:00")


@pytest.mark.parametrize(
    ("text", "place"),
    [
        ("10/8在銀行或郵局", "銀行或郵局"),
        ("10/8在銀行以及郵局", "銀行以及郵局"),
        ("10/8在台北市和平東路二段", "台北市和平東路二段"),
    ],
)
def test_joined_place_words_are_kept_verbatim(text, place):
    result = rr.classify_reschedule_text(text)
    assert (result.status, result.location) == (rr.CANDIDATE, place)


def test_second_place_after_punctuation_is_refused():
    result = rr.classify_reschedule_text("10/8到銀行、郵局")
    assert (result.status, result.reason) == (rr.INVALID, "unsupported_location")


def test_minutes_never_take_a_spaced_date():
    now = datetime(2099, 9, 27, 13, 48, tzinfo=TW)
    request = rr.classify_reschedule_text("早上9點10 / 8")
    status, new_at = rr.resolve_new_schedule(
        request, current_remind_at=_ts(CURRENT), message_time=now, now=now
    )
    assert (status, _when(new_at)) == ("ok", "2099-10-08 09:00")


@pytest.mark.parametrize(
    ("action", "place"),
    [("去A棟領東西", "B棟"), ("到B1拿東西", "C棟"), ("去服務中心領東西", "郵局"),
     ("在門口等", "郵局")],
)
def test_existing_place_forms_conflict(action, place):
    assert rr.merge_location(action, place)[0] == "location_conflict"


def test_muted_mode_never_passes_the_receipt_to_reply(monkeypatch, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-m", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    monkeypatch.setattr(main.settings, "bot_muted", True)
    calls = []
    monkeypatch.setattr(main, "_reply", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: [])
    main._handle_text_message(_event("10/8", quoted="push-m"), "G1")
    assert calls == []
    assert _row(rid)["remind_at"] == _ts("2099-10-08 12:00")


def test_receipt_archive_failure_keeps_the_move(monkeypatch, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-af", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    monkeypatch.setattr(main.settings, "bot_muted", False)

    class _Api:
        def __init__(self, _client):
            pass

        def reply_message(self, _request):
            return SimpleNamespace(sent_messages=[SimpleNamespace(id="line-af-1")])

    class _Client:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def broken_archive(*_args, **_kwargs):
        raise RuntimeError("archive down")

    monkeypatch.setattr(main, "ApiClient", _Client)
    monkeypatch.setattr(main, "MessagingApi", _Api)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: [])
    monkeypatch.setattr(main.memory, "log_raw_message", broken_archive)
    main._handle_text_message(_event("10/8", quoted="push-af"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-08 12:00")
    assert memory.get_sent_reminder_reference("G1", "line-af-1") is None


def test_action_with_a_road_conflicts_with_a_new_place():
    assert rr.merge_location("去測試路領東西", "其他路") == (
        "location_conflict", "去測試路領東西"
    )


def test_bound_message_showing_two_reminders_is_refused(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot(
        "push-two",
        "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費\n2099-10-02 12:00 倒垃圾",
        reminder_id=rid,
    )
    main._handle_text_message(_event("10/8", quoted="push-two"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-01 12:00")
    assert replies[0][0] == rr.refusal_text("multiple")


def test_unchanged_is_logged_and_replayed_after_another_edit(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-u2", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("12:00", quoted="push-u2", message_id="msg-u"), "G1")
    assert replies[-1][0].startswith("提醒本來就是這個時間")
    assert memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="msg-other", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-05 12:00"), new_action="繳管理費",
    )["status"] == "updated"
    main._handle_text_message(_event("12:00", quoted="push-u2", message_id="msg-u"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-05 12:00")
    assert replies[-1][0].startswith("這則更正先前已處理")


def test_log_insert_failure_rolls_back_the_move(monkeypatch):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    before = _row(rid)

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(memory, "_insert_reschedule_log_conn", broken)
    result = memory.reschedule_generic_reminder(
        "G1", rid, inbound_message_id="m-rb", expected_action="繳管理費",
        expected_remind_at=_ts("2099-10-01 12:00"),
        new_remind_at=_ts("2099-10-08 12:00"), new_action="繳管理費",
    )
    assert result["status"] == "unavailable"
    assert _row(rid) == before


def _fake_line(monkeypatch, *, push_ids):
    class _ReplyTokenError(Exception):
        status = 400

    class _Api:
        def __init__(self, _client):
            pass

        def reply_message(self, _request):
            raise _ReplyTokenError("Invalid reply token")

        def push_message(self, _request):
            return SimpleNamespace(sent_messages=[SimpleNamespace(id=i) for i in push_ids])

    class _Client:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "ApiClient", _Client)
    monkeypatch.setattr(main, "MessagingApi", _Api)
    monkeypatch.setattr(main, "_get_line_config", lambda: None)
    monkeypatch.setattr(main, "_is_definite_reply_token_error", lambda _exc: True)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: [])


def test_push_fallback_binds_the_real_sent_id(monkeypatch, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-f", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    _fake_line(monkeypatch, push_ids=["line-fallback-1"])
    main._handle_text_message(_event("10/8", quoted="push-f"), "G1")
    assert _row(rid)["remind_at"] == _ts("2099-10-08 12:00")
    assert memory.get_sent_reminder_reference("G1", "line-fallback-1")["reminder_id"] == rid


def test_push_fallback_without_sent_ids_binds_nothing(monkeypatch, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-f2", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    _fake_line(monkeypatch, push_ids=[])
    main._handle_text_message(_event("10/8", quoted="push-f2"), "G1")
    with memory._conn() as conn:
        refs = conn.execute(
            "SELECT message_id FROM sent_reminder_refs WHERE reminder_id=?", (rid,)
        ).fetchall()
    assert refs == [("push-f2",)]


# ── Round-2 reviewer additions ──────────────────────────────────────────────

def test_long_run_of_at_signs_is_fast():
    import time

    start = time.perf_counter()
    assert rr.parse_quoted_reminder("已新增提醒\n" + "@" * 1000 + " x").status == rr.QUOTED_NONE
    assert time.perf_counter() - start < 0.5


def test_every_status_code_has_its_own_refusal_text():
    codes = {
        # parser / resolver
        "unparsed", "offset_unsupported", "daypart_only", "ambiguous_hour", "bad_time",
        "ambiguous_midnight", "range", "multiple_dates", "multiple_times", "bad_date",
        "weekday_mismatch", "ambiguous_weekday", "past", "unsupported_location",
        "location_conflict",
        # target resolution
        "multiple", "ambiguous", "not_found", "terminal", "stale_quote", "calendar",
        "not_generic",
        # memory CAS
        "conflict", "duplicate", "collision", "busy", "delivery_uncertain", "unavailable",
        # handler
        "display_unsafe", "question", "unsupported_change",
    }
    assert codes <= set(rr._REFUSALS)


@pytest.mark.parametrize(
    "receipt",
    [
        rr.updated_receipt(_ts(CURRENT), _ts("2099-10-08 12:00"), "繳管理費"),
        rr.unchanged_receipt(_ts(CURRENT), "繳管理費"),
        rr.updated_receipt(_ts(CURRENT), _ts("2099-10-08 12:00"), "a_b_c 繳費"),
        rr.updated_receipt(_ts(CURRENT), _ts("2099-10-08 12:00"), "檢查 Gemini API key"),
    ],
)
def test_receipt_gate_matches_the_real_outbound_gates(receipt):
    expected = (
        len(receipt) <= 4800
        and not main._is_system_status_outbound(receipt)
        and main._prepare_outbound_text(receipt) == receipt
    )
    assert main._reschedule_receipt_displayable(receipt) is expected


def test_quoted_plain_reply_never_reads_the_log(monkeypatch):
    def must_not_read(*_args, **_kwargs):
        raise AssertionError("text checks come first")

    monkeypatch.setattr(memory, "get_reminder_reschedule_log", must_not_read)
    assert main._try_handle_quoted_reminder_reschedule(
        _event("好", quoted="whatever"), "G1", "好"
    ) is False


def test_replay_reply_failure_still_says_it_was_handled(replies, fixed_now, monkeypatch):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-rf", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    main._handle_text_message(_event("10/8", quoted="push-rf", message_id="msg-rf"), "G1")

    def broken(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(memory, "get_reminder", broken)
    main._handle_text_message(_event("10/8", quoted="push-rf", message_id="msg-rf"), "G1")
    assert replies[-1][0].startswith("這則更正先前已處理（2099-10-01 12:00 → 2099-10-08 12:00）")


# ── Round-2 correctness review additions ────────────────────────────────────

@pytest.mark.parametrize(
    "text", ["我會晚10分鐘到", "提早10分鐘出門喔", "晚半小時到", "早上9點，晚5分鐘也行"]
)
def test_chat_about_being_late_is_not_claimed(text):
    assert rr.classify_reschedule_text(text).status == rr.NOT_RESCHEDULE


@pytest.mark.parametrize("text", ["延後一天", "提醒提前兩小時吧", "請延後3天"])
def test_pure_shift_requests_are_refused(text):
    result = rr.classify_reschedule_text(text)
    assert (result.status, result.reason) == (rr.INVALID, "offset_unsupported")


@pytest.mark.parametrize("text", ["我換成搭8號公車", "應該是2個人", "其實是3樓", "帶2個才是對的"])
def test_digits_alone_are_not_a_schedule_hint(text):
    assert rr.has_schedule_hint(text) is False


def test_change_verb_without_schedule_falls_through(monkeypatch):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-bus", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: pytest.fail("must not reply"))
    assert main._try_handle_quoted_reminder_reschedule(
        _event("我換成搭8號公車", quoted="push-bus"), "G1", "我換成搭8號公車"
    ) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("改成明天早上9點提醒我", "2099-09-28 09:00"),
        ("改到10/8提醒大家", "2099-10-08 12:00"),
        ("改成10/8提醒我一下", "2099-10-08 12:00"),
    ],
)
def test_trailing_remind_phrase_after_a_change_verb(text, expected):
    now = datetime(2099, 9, 27, 13, 48, tzinfo=TW)
    request = rr.classify_reschedule_text(text)
    status, new_at = rr.resolve_new_schedule(
        request, current_remind_at=_ts(CURRENT), message_time=now, now=now
    )
    assert (status, _when(new_at)) == ("ok", expected)


def test_recently_passed_month_day_is_past_not_next_year():
    now = datetime(2099, 10, 5, 10, 0, tzinfo=TW)
    request = rr.classify_reschedule_text("10/3")
    status, _ = rr.resolve_new_schedule(
        request, current_remind_at=_ts("2099-10-08 12:00"), message_time=now, now=now
    )
    assert status == "past"


@pytest.mark.parametrize(
    ("action", "place", "expected"),
    [
        ("繳管理費，地點：中山路1號2樓", "中山路1號", "繳管理費，地點：中山路1號"),
        ("到郵局寄包裹，地點：中山路1號", "郵局", "到郵局寄包裹，地點：郵局"),
        ("繳管理費，地點：中山路1號", "中山路1號", "繳管理費，地點：中山路1號"),
    ],
)
def test_managed_place_is_compared_and_replaced(action, place, expected):
    assert rr.merge_location(action, place) == ("ok", expected)


def test_labelled_unknown_place_is_refused_not_released():
    result = rr.classify_reschedule_text("10/8 地點：台北101")
    assert (result.status, result.reason) == (rr.INVALID, "unsupported_location")


def test_relative_day_follows_the_line_event_timestamp(replies, fixed_now):
    rid = _seed("繳管理費", "2099-09-27 20:00")
    _archive_bot("push-ts", "⏰ 提醒（今天）\n2099-09-27 20:00 繳管理費", reminder_id=rid)
    sent = datetime(2099, 9, 26, 23, 50, tzinfo=TW)
    event = _event("明天", quoted="push-ts", timestamp=int(sent.timestamp() * 1000))
    main._handle_text_message(event, "G1")
    assert _row(rid)["remind_at"] == _ts("2099-09-27 20:00")
    assert replies[-1][0].startswith("提醒本來就是這個時間")


# ── Round-3 review additions ────────────────────────────────────────────────

def test_chinese_numeral_date_after_a_clock_is_not_minutes():
    now = datetime(2099, 9, 27, 13, 48, tzinfo=TW)
    request = rr.classify_reschedule_text("早上9點十一月八號")
    status, new_at = rr.resolve_new_schedule(
        request, current_remind_at=_ts(CURRENT), message_time=now, now=now
    )
    assert (status, _when(new_at)) == ("ok", "2099-11-08 09:00")


def test_earlier_today_is_past_not_next_year():
    now = datetime(2099, 10, 4, 13, 0, tzinfo=TW)
    request = rr.classify_reschedule_text("10/4早上9點")
    status, _ = rr.resolve_new_schedule(
        request, current_remind_at=_ts("2099-10-08 12:00"), message_time=now, now=now
    )
    assert status == "past"


def test_leap_day_far_away_is_bad_date():
    now = datetime(2097, 10, 4, 10, 0, tzinfo=TW)  # last leap day 2096, next 2104
    request = rr.classify_reschedule_text("2/29")
    status, _ = rr.resolve_new_schedule(
        request, current_remind_at=_ts("2097-10-08 12:00"), message_time=now, now=now
    )
    assert status == "bad_date"


@pytest.mark.parametrize(
    "text",
    ["換成小一點的", "其實是好一點了", "應該是多一點", "這兩點才是重點", "換成一時之選",
     "其實是一點點", "明天地點再說", "10/8 地址我再傳給你"],
)
def test_chinese_idioms_and_chat_are_not_refused(monkeypatch, text):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot(f"push-i-{abs(hash(text))}", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費",
                 reminder_id=rid)
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: pytest.fail("must not reply"))
    assert rr.has_schedule_hint(text) is False or rr.classify_reschedule_text(text).status != rr.INVALID
    assert main._try_handle_quoted_reminder_reschedule(
        _event(text, quoted=f"push-i-{abs(hash(text))}"), "G1", text
    ) is False


def test_bare_chinese_hour_after_a_verb_is_still_a_time():
    result = rr.classify_reschedule_text("改成九點")
    assert (result.status, result.reason) == (rr.INVALID, "ambiguous_hour")
    assert rr.classify_reschedule_text("改成早上九點").status == rr.CANDIDATE


@pytest.mark.parametrize(
    ("text", "expected"),
    [("10/3", "2027-10-03 12:00"), ("10/8", "2027-10-08 12:00"), ("9/28", "2027-09-28 12:00")],
)
def test_far_future_reminder_resolves_near_its_own_date(text, expected):
    now = datetime(2026, 10, 5, 15, 0, tzinfo=TW)
    request = rr.classify_reschedule_text(text)
    status, new_at = rr.resolve_new_schedule(
        request, current_remind_at=_ts("2027-09-30 12:00"), message_time=now, now=now
    )
    assert (status, _when(new_at)) == ("ok", expected)


def test_cancel_burst_failure_still_sends_the_receipt(monkeypatch, fixed_now, replies):
    rid = _seed("繳管理費", "2099-10-01 12:00")
    _archive_bot("push-cb", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)

    def broken(_gid):
        raise RuntimeError("burst state")

    monkeypatch.setattr(main.burst_filter, "cancel_burst", broken)
    main._handle_text_message(_event("10/8", quoted="push-cb"), "G1")
    assert replies and replies[-1][0].startswith("已更新提醒（")


# ── fixR5a (GP1 r4 #2): a later, fuller mention keeps the reminder's wording,
# so a message sent before it still names the reminder; the 細節 line that the
# merge receipt and the push now carry never hides it either ───────────────


def _write_bill(action: str) -> tuple[int, str]:
    return memory.add_reminder_with_outcome(
        "G1", "U1", action, _ts(CURRENT), source_text=f"10/1 中午12點{action}",
        time_kind="clock",
    )


def _receipt_text(outcome: str, reminder_id: int, action: str) -> str:
    return main._format_persisted_reminder_confirmation(
        outcome, reminder_id, action, datetime.fromtimestamp(_ts(CURRENT), TW)
    )


def _after_command(reminder_id: int, command: str) -> None:
    row = _row(reminder_id)
    if command == "這則取消":
        assert row["status"] == "cancelled"
    else:
        assert row["status"] == "pending"
        assert row["remind_at"] == _ts("2099-10-01 15:00")
    assert row["action"] == "繳電費"


@pytest.mark.parametrize("command", ["這則取消", "改到下午3點"])
def test_quoting_the_first_receipt_after_a_fuller_mention(replies, fixed_now, command):
    # GP1 r4 probe: r4 answered 「沒有找到時間與事項都相符的待取消提醒…」 and
    # refused the move.
    rid, outcome = _write_bill("繳電費")
    _archive_bot("ack-first", _receipt_text(outcome, rid, "繳電費"))
    assert _write_bill("繳電費和瓦斯費") == (rid, "merged")

    main._handle_text_message(_event(command, quoted="ack-first"), "G1")

    _after_command(rid, command)


def test_quoting_an_earlier_bound_push_after_a_fuller_mention_moves_it(replies, fixed_now):
    # GP1 r4 probe: r4 answered 「這筆提醒之後已經被改過…」.
    rid, _ = _write_bill("繳電費")
    _archive_bot("push-early", f"⏰ 提醒（4 天後）\n{CURRENT} 繳電費", reminder_id=rid)
    assert _write_bill("繳電費和瓦斯費") == (rid, "merged")

    main._handle_text_message(_event("改到下午3點", quoted="push-early"), "G1")

    _after_command(rid, "改到下午3點")
    assert replies[0][0].startswith("已更新提醒（2099-10-01 12:00 → 2099-10-01 15:00）")


@pytest.mark.parametrize("command", ["這則取消", "改到下午3點"])
def test_the_merge_receipts_detail_line_never_hides_the_reminder(
    replies, fixed_now, command
):
    rid, _ = _write_bill("繳電費")
    same, outcome = _write_bill("繳電費和瓦斯費")
    assert (same, outcome) == (rid, "merged")
    receipt = _receipt_text(outcome, rid, "繳電費和瓦斯費")
    assert receipt.endswith("事項：繳電費\n細節：繳電費和瓦斯費")
    quoted = rr.parse_quoted_reminder(receipt)
    assert (quoted.status, quoted.action, _when(quoted.remind_at)) == (
        rr.QUOTED_ONE, "繳電費", CURRENT
    )
    _archive_bot("ack-merged", receipt)

    main._handle_text_message(_event(command, quoted="ack-merged"), "G1")

    _after_command(rid, command)


@pytest.mark.parametrize("bound", [True, False])
@pytest.mark.parametrize("command", ["這則取消", "改到下午3點"])
def test_a_push_with_a_detail_line_can_be_quoted(replies, fixed_now, bound, command):
    rid, _ = _write_bill("繳電費")
    assert _write_bill("繳電費和瓦斯費") == (rid, "merged")
    (row,) = memory.list_pending_reminders_full("G1", dedupe=False)
    push = reminder_push._format_push_text(row, "weekly")
    assert push.endswith(f"{CURRENT} 繳電費\n細節：繳電費和瓦斯費")
    quoted = rr.parse_quoted_reminder("@某人\n" + push)
    assert (quoted.status, quoted.action, _when(quoted.remind_at)) == (
        rr.QUOTED_ONE, "繳電費", CURRENT
    )
    _archive_bot("push-detail", "@某人\n" + push, reminder_id=rid if bound else None)

    main._handle_text_message(_event(command, quoted="push-detail"), "G1")

    _after_command(rid, command)


def test_a_detail_line_alone_is_still_not_a_reminder():
    assert rr.parse_quoted_reminder("已新增提醒\n細節：繳電費和瓦斯費").status == rr.QUOTED_NONE
    assert rr.parse_quoted_reminder("⏰ 提醒（明天）\n細節：繳電費和瓦斯費").status == rr.QUOTED_NONE
