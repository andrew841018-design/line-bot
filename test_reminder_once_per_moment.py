"""2026-10-04 Andrew：同一件事只留一筆、階段照舊；同一時刻同一提醒最多推一則，
錯過的階段不補推、不累積成連推。

Covers P4 v2/v3: the push-time fold of same-event rows, consuming the stages a
creation receipt already announced, the write-time e1/e3 loosening, calendar
mirror duplicates at write time, one message per event inside a piggyback
batch, and the "never twice at one moment" invariants.  All content is made
up; dates are relative to today so the tests never expire.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

import main
import memory
import reminder_intent as ri
import reminder_push

TZ = ZoneInfo("Asia/Taipei")
G = "G_ONCE"


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _synthetic_aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(
        json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))


def _day(offset: int):
    return (datetime.now(TZ) + timedelta(days=offset)).date()


def _at(day, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp())


def _md(day) -> str:
    return f"{day.month}/{day.day}"


def _insert(
    action: str,
    remind_at: int,
    *,
    user: str = "U_A",
    source: str | None = None,
    kind: str | None = "clock",
    source_kind: str = "",
    source_ref: str = "",
    mentions: str = "[]",
    details: str = "[]",
    **flags,
) -> int:
    """A row written before the write-time merge existed (or by another path)."""
    with memory._conn() as c:
        cur = c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases, time_kind, merged_details) "
            "VALUES (?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?, ?, ?)",
            (G, user, action, remind_at, source_kind, source_ref, source or action,
             mentions, kind, details),
        )
        rid = int(cur.lastrowid)
        for column, value in flags.items():
            c.execute(f"UPDATE reminders SET {column}=? WHERE reminder_id=?", (value, rid))
    return rid


def _row(rid: int) -> dict:
    with memory._conn() as c:
        c.row_factory = __import__("sqlite3").Row
        row = c.execute("SELECT * FROM reminders WHERE reminder_id=?", (rid,)).fetchone()
    assert row is not None
    return dict(row)


def _status(rid: int) -> str:
    return str(_row(rid)["status"])


def _details(rid: int) -> str:
    return json.dumps(json.loads(_row(rid)["merged_details"] or "[]"), ensure_ascii=False)


def _all_reminder_rows() -> list[tuple]:
    with memory._conn() as c:
        return c.execute("SELECT * FROM reminders ORDER BY reminder_id").fetchall()


def _claims() -> int:
    with memory._conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM reminder_delivery_claims").fetchone()[0])


def _set_now(monkeypatch, moment: int) -> None:
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: int(moment))


def _record_pushes(monkeypatch, outcomes=None) -> list[dict]:
    calls: list[dict] = []
    results = iter(outcomes) if outcomes is not None else None

    def fake_push(group_id, text, **kwargs):
        ok = True if results is None else next(results)
        calls.append({"group_id": group_id, "text": text, "ok": ok, **kwargs})
        return ok

    monkeypatch.setattr(reminder_push, "_push_to_group", fake_push)
    return calls


# Dinner booked three ways by the same person, all at 17:30 (made-up venue).
DINNER = (
    ("海景餐廳用餐", "明天晚上已訂星光飯店：海景餐廳，晚上5:30。"),
    ("星光：海景餐廳", "今天晚上5:30 星光： 海景餐廳"),
    ("在星光飯店用餐", "訂好了，對面就是星光飯店5:30開吃。"),
)


def _dinner_rows(at: int) -> list[int]:
    return [_insert(action, at, source=source) for action, source in DINNER]


class _FakeApiClient:
    def __init__(self, _cfg):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _fake_line(monkeypatch, *, fail: Exception | None = None) -> list[list]:
    batches: list[list] = []

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def reply_message(self, request):
            batches.append(list(request.messages))
            if fail is not None:
                raise fail
            return SimpleNamespace(sent_messages=[])

        def push_message(self, request, **_kwargs):
            raise AssertionError("no push fallback expected")

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    return batches


def _texts(batch: list) -> list[str]:
    return [str(getattr(message, "text", "") or "") for message in batch]


# ── reminder_stages: one source of truth for the 5/8 ladder (N8) ─────────────


def _legacy_decide_stage(r: dict, now: int) -> str | None:
    """Frozen copy of reminder_push._decide_stage before 2026-10-04."""
    delta = r["remind_at"] - now
    days = delta / 86400
    hours = delta / 3600
    if str(r.get("source_kind") or "") == "contextual_date_once":
        reminder_dt = datetime.fromtimestamp(int(r["remind_at"]), TZ)
        now_dt = datetime.fromtimestamp(int(now), TZ)
        if (
            not r["pushed_now"]
            and now >= int(r["remind_at"])
            and now_dt.date() == reminder_dt.date()
        ):
            return "now"
        return None
    calendar_mirror = bool(
        str(r.get("source_kind") or "") == "calendar_event"
        and str(r.get("source_ref") or "")
    )
    calendar_mirror_has_clock = bool(
        calendar_mirror
        and re.search(
            r"(?:活動)?時間：(?:[01]\d|2[0-3]):[0-5]\d(?:\b|$)",
            str(r.get("source_text") or ""),
        )
    )
    if calendar_mirror and not calendar_mirror_has_clock:
        return None
    if -0.25 <= hours <= 0.25 and not r["pushed_now"]:
        return "now"
    if 0.5 < hours <= 1.5 and not r["pushed_1hr"]:
        return "1hr"
    if 1.5 < hours <= 2.5 and not r["pushed_2hr"]:
        return "2hr"
    if 3.5 < hours <= 4.5 and not r["pushed_4hr"]:
        return "4hr"
    if calendar_mirror:
        return None
    if hours > 4.5 and 0.5 < days <= 2 and not r["pushed_1d"]:
        return "1d"
    if 2 < days <= 4 and not r["pushed_3d"]:
        return "3d"
    if 4 < days < 7:
        return None
    if 7 <= days <= 30:
        last_weekly = r["last_weekly_at"]
        days_since = (now - last_weekly) / 86400 if last_weekly else 999
        if days_since >= 6.5:
            return "weekly"
    return None


def test_open_stage_matches_the_old_ladder_everywhere():
    import reminder_stages

    now = _at(_day(3), 12, 0)
    sources = [
        ("", "", "明天看醫生"),
        ("calendar_event", "E1", "皮拉提斯；時間：11:00"),
        ("calendar_event", "E1", "家族聚餐；參加人：全家"),
        ("calendar_event", "", "沒有來源的鏡像"),
        ("contextual_date_once", "M1:0", "看牙醫（當天提醒）"),
    ]
    marks = [
        -90000, -86400, -3600, -901, -900, -899, -1, 0, 1, 899, 900, 901, 1799, 1800,
        1801, 3600, 5399, 5400, 5401, 8999, 9000, 9001, 12599, 12600, 12601, 16199,
        16200, 16201, 20000, 43199, 43200, 43201, 86400, 172799, 172800, 172801,
        345599, 345600, 345601, 500000, 604799, 604800, 604801, 2592000, 2592001,
        3000000,
    ]
    flag_sets = [
        {},
        {"pushed_now": 1, "pushed_1hr": 1, "pushed_2hr": 1, "pushed_4hr": 1,
         "pushed_1d": 1, "pushed_3d": 1},
        {"pushed_1hr": 1, "pushed_1d": 1},
    ]
    checked = 0
    for kind, ref, text in sources:
        for delta in marks:
            for flags in flag_sets:
                for last_weekly in (0, now - 6 * 86400, now - 7 * 86400):
                    row = {
                        "remind_at": now + delta, "source_kind": kind, "source_ref": ref,
                        "source_text": text, "last_weekly_at": last_weekly,
                        "pushed_3d": 0, "pushed_1d": 0, "pushed_4hr": 0,
                        "pushed_2hr": 0, "pushed_1hr": 0, "pushed_now": 0, **flags,
                    }
                    expected = _legacy_decide_stage(row, now)
                    assert reminder_stages.open_stage(row, now) == expected, (kind, delta, flags)
                    assert reminder_push._decide_stage(row, now) == expected
                    checked += 1
    assert checked > 1000


# ── consume_open_stages: the receipt already told the family ─────────────────


def test_created_1_6_hours_ahead_consumes_2hr_and_1hr_but_never_now():
    now = int(time.time())
    rid = _insert("去郵局寄包裹", now + int(1.6 * 3600))
    assert memory.consume_open_stages([rid], now=now) == 2
    row = _row(rid)
    assert (row["pushed_2hr"], row["pushed_1hr"], row["pushed_now"]) == (1, 1, 0)
    assert (row["pushed_4hr"], row["pushed_1d"], row["pushed_3d"]) == (0, 0, 0)
    assert row["status"] == "pending"
    # "now" still comes at the event time, and nothing comes before it.
    due = reminder_push._due_reminder_items(group_id=G, now=now + 60)
    assert due == []
    due = reminder_push._due_reminder_items(group_id=G, now=now + int(1.6 * 3600))
    assert [item["stage"] for item in due] == ["now"]


def test_a_receipt_for_tomorrow_evening_leaves_nothing_due_right_after():
    tomorrow = _day(1)
    rid, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "海景餐廳用餐", _at(tomorrow, 17, 30),
        source_text="明天晚上5:30海景餐廳", time_kind="clock",
    )
    assert outcome == "created"
    now = int(time.time())
    assert [i["stage"] for i in reminder_push._due_reminder_items(group_id=G, now=now)] == ["1d"]
    assert memory.consume_open_stages([rid], now=now) == 1
    assert reminder_push._due_reminder_items(group_id=G, now=now + 60) == []
    assert _row(rid)["pushed_1d"] == 1


def test_a_far_reminder_only_records_the_weekly_notice_time():
    now = int(time.time())
    rid = _insert("出國買保健品", _at(_day(20), 12), kind="none")
    assert memory.consume_open_stages([rid], now=now) == 1
    row = _row(rid)
    assert row["last_weekly_at"] == now and row["weekly_count"] == 0
    assert reminder_push._due_reminder_items(group_id=G, now=now + 15 * 60) == []


def test_consuming_is_idempotent_and_touches_pending_rows_only():
    now = int(time.time())
    rid = _insert("繳停車費", now + 30 * 3600)
    other = _insert("繳水費", now + 30 * 3600 + 120)
    gone = _insert("繳電費", now + 30 * 3600)
    with memory._conn() as c:
        c.execute("UPDATE reminders SET status='cancelled' WHERE reminder_id=?", (gone,))
    assert memory.consume_open_stages([rid, gone], now=now) == 1
    assert memory.consume_open_stages([rid, gone], now=now) == 0
    assert memory.consume_open_stages([], now=now) == 0
    assert _row(other)["pushed_1d"] == 0 and _row(gone)["pushed_1d"] == 0


@pytest.mark.parametrize(
    ("ahead_seconds", "expected"),
    [
        (int(2.01 * 86400), {"pushed_3d", "pushed_1d"}),   # 1d opens 14 minutes later
        (int(0.4 * 3600), set()),                          # only "now" is near: never consumed
        (int(0.75 * 3600), {"pushed_1hr"}),
        (int(4.6 * 3600), {"pushed_4hr"}),                 # 4hr opens 6 minutes later
        (int(1.2 * 86400), {"pushed_1d"}),
    ],
)
def test_stages_opening_within_twenty_minutes_are_consumed_too(ahead_seconds, expected):
    now = int(time.time())
    rid = _insert("看牙醫", now + ahead_seconds)
    memory.consume_open_stages([rid], now=now)
    row = _row(rid)
    marked = {c for c in ("pushed_3d", "pushed_1d", "pushed_4hr", "pushed_2hr",
                          "pushed_1hr", "pushed_now") if row[c]}
    assert marked == expected


def test_thirty_days_and_a_few_minutes_ahead_consumes_the_weekly_notice():
    now = int(time.time())
    rid = _insert("換護照", now + 30 * 86400 + 600)
    assert memory.consume_open_stages([rid], now=now) == 1
    assert _row(rid)["last_weekly_at"] == now


def test_contextual_pairs_have_nothing_to_consume():
    now = int(time.time())
    rid = _insert("成員甲 看牙醫（當天提醒）", now + 3600,
                  source_kind="contextual_date_once", source_ref="M1:same:0")
    assert memory.consume_open_stages([rid], now=now) == 0


# ── push-time fold: one row per real-world event ─────────────────────────────


def test_dinner_triplet_becomes_one_push_and_one_row(monkeypatch):
    at = _at(_day(3), 17, 30)
    rids = _dinner_rows(at)
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, at - 3600)

    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and "（1 小時後）" in calls[0]["text"]
    pending = [rid for rid in rids if _status(rid) == "pending"]
    assert len(pending) == 1
    primary = pending[0]
    assert calls[0]["reminder_id"] == primary
    assert sorted(_status(rid) for rid in rids if rid != primary) == ["cancelled", "cancelled"]
    assert _row(primary)["pushed_1hr"] == 1
    details = _details(primary)
    for action, _source in DINNER:
        if action != _row(primary)["action"]:
            assert action in details


def test_a_stage_already_sent_by_a_same_time_row_is_not_sent_again(monkeypatch):
    # Another sender delivered one row's 1-hour notice a moment ago.
    at = _at(_day(3), 17, 30)
    first, second, third = _dinner_rows(at)
    with memory._conn() as c:
        c.execute("UPDATE reminders SET pushed_1hr=1 WHERE reminder_id=?", (third,))
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, at - 3600)

    assert reminder_push.push_reminders() == 0

    assert calls == []
    remaining = [rid for rid in (first, second, third) if _status(rid) == "pending"]
    assert len(remaining) == 1 and _row(remaining[0])["pushed_1hr"] == 1
    _set_now(monkeypatch, at - 1800)
    assert reminder_push._due_reminder_items(group_id=G) == []


def test_a_default_noon_row_folds_into_the_clock_row_of_the_same_day(monkeypatch):
    d = _day(6)
    noon = _insert("打流感疫苗", _at(d, 12), kind="none", source=f"{_md(d)} 打流感疫苗")
    clock = _insert("施打流感疫苗和新冠疫苗", _at(d, 14),
                    source=f"{_md(d)} 下午兩點施打流感疫苗和新冠疫苗")
    calls = _record_pushes(monkeypatch)

    _set_now(monkeypatch, _at(d, 12, 4) - 4 * 86400)   # the noon row's 3-day window
    reminder_push.push_reminders()
    assert calls == []
    assert _status(noon) == "cancelled" and _status(clock) == "pending"
    assert "打流感疫苗" in _details(clock)

    _set_now(monkeypatch, _at(d, 14, 4) - 4 * 86400)
    reminder_push.push_reminders()
    assert len(calls) == 1 and calls[0]["reminder_id"] == clock


def test_a_vague_row_between_two_appointments_is_not_guessed(monkeypatch):
    d = _day(6)
    vague = _insert("打流感疫苗", _at(d, 12), kind="none")
    early = _insert("打流感疫苗", _at(d, 14))
    late = _insert("打流感疫苗", _at(d, 17))
    _set_now(monkeypatch, _at(d, 12, 4) - 4 * 86400)
    assert reminder_push.fold_due_duplicates() == 0
    assert {_status(r) for r in (vague, early, late)} == {"pending"}


def test_different_activities_at_the_same_place_and_time_stay_separate(monkeypatch):
    at = _at(_day(3), 14)
    first = _insert("合成路1號3樓開會", at)
    second = _insert("合成路1號3樓上課", at)
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, at - 3600)
    assert reminder_push.push_reminders() == 2
    assert len(calls) == 2 and _status(first) == _status(second) == "pending"


def test_different_people_never_fold(monkeypatch):
    at = _at(_day(3), 14)
    first = _insert("打流感疫苗", at, user="U_A")
    second = _insert("去打流感疫苗", at, user="U_B")
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, at - 3600)
    reminder_push.push_reminders()
    assert len(calls) == 2 and _status(first) == _status(second) == "pending"


def test_morning_and_evening_doses_stay_separate(monkeypatch):
    d = _day(3)
    morning = _insert("早上吃血壓藥", _at(d, 9), kind="daypart:早上")
    evening = _insert("晚上吃血壓藥", _at(d, 19), kind="daypart:晚上")
    _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 8))
    reminder_push.push_reminders()
    assert _status(morning) == _status(evening) == "pending"


def test_a_contextual_pair_is_never_folded(monkeypatch):
    d = _day(3)
    before = _insert("成員甲 看牙醫（前一天提醒）", _at(d, 9) - 86400,
                     source_kind="contextual_date_once", source_ref="M9:before:0")
    same_day = _insert("成員甲 看牙醫（當天提醒）", _at(d, 9),
                       source_kind="contextual_date_once", source_ref="M9:same:0")
    natural = _insert("成員甲看牙醫", _at(d, 10, 30))
    _set_now(monkeypatch, _at(d, 9, 5))
    assert reminder_push.fold_due_duplicates() == 0
    assert {_status(r) for r in (before, same_day, natural)} == {"pending"}
    # fixC12 (GP1 r2): both owe a stage at 09:05, but they are one appointment.
    # One message goes out (the reminder's, with its clock and stage label) and
    # it completes the 當天 row; nothing is cancelled.
    calls = _record_pushes(monkeypatch)
    assert reminder_push.push_reminders() == 1
    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert "（1 小時後）" in calls[0]["text"]
    assert _status(same_day) == "done" and _row(same_day)["pushed_now"] == 1
    assert _row(natural)["pushed_1hr"] == 1
    assert _status(before) == "pending" and _row(before)["pushed_now"] == 0
    assert _claims() == 0


def test_a_calendar_mirror_is_never_folded(monkeypatch):
    at = _at(_day(3), 14)
    mirror = _insert("打流感疫苗", at, kind=None, source_kind="calendar_event",
                     source_ref="E-flu", source="打流感疫苗；時間：14:00")
    natural = _insert("去打流感疫苗", at)
    _set_now(monkeypatch, at - 3600)
    assert reminder_push.fold_due_duplicates() == 0
    assert _status(mirror) == _status(natural) == "pending"


def test_a_peer_being_delivered_is_left_alone(monkeypatch):
    at = _at(_day(3), 17, 30)
    rids = _dinner_rows(at)
    in_flight = rids[2]
    with memory._conn() as c:
        c.execute(
            "INSERT INTO reminder_delivery_claims(group_id, delivery_kind, subject_ref, "
            "occurrence, source_kind, source_ref, transport, state, claim_token, retry_key, "
            "fallback_retry_key, claimed_at) VALUES (?, 'natural', ?, '1hr', '', '', 'reply', "
            "'sending', 'T', 'R', '', ?)",
            (G, str(in_flight), int(time.time())),
        )
    _set_now(monkeypatch, at - 3600)
    reminder_push.fold_due_duplicates()
    assert _status(in_flight) == "pending"
    assert "pending" in {_status(rids[0]), _status(rids[1])}
    assert sum(_status(r) == "cancelled" for r in rids) == 1


def test_dry_run_neither_folds_nor_writes(monkeypatch, capsys):
    at = _at(_day(3), 17, 30)
    _dinner_rows(at)
    _insert("過期的事", int(time.time()) - 2 * 3600)          # stale: deleted by a live run
    _insert("繳房租", at + 7200)
    _insert("繳房租", at + 7200 + 30)                         # exact duplicate: merged by a live run
    before = _all_reminder_rows()
    monkeypatch.setattr(
        reminder_push, "_push_to_group",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dry run must not send")),
    )
    _set_now(monkeypatch, at - 3600)

    reminder_push.push_reminders(dry_run=True)

    assert _all_reminder_rows() == before
    out = capsys.readouterr().out
    item_lines = [line for line in out.splitlines() if line.startswith("[DRY] rid=")]
    dinner_lines = [line for line in item_lines if "stage=1hr" in line]
    assert len(dinner_lines) == 1
    assert "[DRY] fold" in out


def test_the_push_log_carries_ids_not_reminder_text(monkeypatch, caplog):
    at = _at(_day(3), 14)
    _insert("秘密的私人行程", at)
    _record_pushes(monkeypatch)
    _set_now(monkeypatch, at - 3600)
    with caplog.at_level("INFO", logger="reminder_push"):
        assert reminder_push.push_reminders() == 1
    assert "秘密的私人行程" not in caplog.text
    assert "pushed rid=" in caplog.text


def _cancel_event(text: str, quoted_message_id: str) -> MessageEvent:
    message = MagicMock(spec=TextMessageContent)
    message.id = "incoming-cancel"
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted_message_id
    message.quote_token = "quote-token"
    message.type = "text"
    source = MagicMock(spec=GroupSource)
    source.group_id = G
    source.user_id = "U_A"
    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "reply-token"
    event.delivery_context = SimpleNamespace(is_redelivery=False)
    return event


def _quote_cancel(monkeypatch, quoted_message_id: str) -> list[str]:
    replies: list[str] = []

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("cancellation must stop all later routing")

    for name in ("_try_one_shot_reply", "_try_handle_calendar_correction",
                 "_auto_capture_text_if_important", "_maybe_extract_reminder"):
        monkeypatch.setattr(main, name, must_not_run)
    monkeypatch.setattr(main, "_reply", lambda _token, text, **_kw: replies.append(text))
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda _gid: None)
    main._handle_text_message(_cancel_event("這則取消", quoted_message_id), G)
    return replies


def _line_push_with_ids(monkeypatch, ids: list[str]) -> None:
    sent = iter(ids)

    class FakeMessagingApi:
        def __init__(self, _client):
            pass

        def push_message(self, _request, x_line_retry_key=None):
            return SimpleNamespace(sent_messages=[SimpleNamespace(id=next(sent))])

    monkeypatch.setattr(reminder_push, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(reminder_push, "MessagingApi", FakeMessagingApi)
    monkeypatch.setattr(reminder_push, "_line_access_token", lambda: "token")


def test_quoting_the_folded_push_cancels_the_one_remaining_reminder(monkeypatch):
    at = _at(_day(3), 17, 30)
    rids = _dinner_rows(at)
    _line_push_with_ids(monkeypatch, ["push-after-fold"])
    _set_now(monkeypatch, at - 3600)
    assert reminder_push.push_reminders() == 1

    replies = _quote_cancel(monkeypatch, "push-after-fold")

    assert len(replies) == 1 and "已取消提醒" in replies[0]
    assert {_status(rid) for rid in rids} == {"cancelled"}
    _set_now(monkeypatch, at)
    assert reminder_push._due_reminder_items(group_id=G) == []


def test_quoting_an_older_push_of_a_folded_row_cancels_the_surviving_row(monkeypatch):
    d = _day(6)
    noon = _insert("打流感疫苗", _at(d, 12), kind="none")
    clock = _insert("施打流感疫苗和新冠疫苗", _at(d, 14))
    _line_push_with_ids(monkeypatch, ["old-noon-push"])
    # An older push of the noon row went out before the two rows were folded.
    assert reminder_push._push_to_group(G, f"⏰ 提醒\n{_md(d)} 12:00 打流感疫苗", reminder_id=noon)
    _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 12, 4) - 4 * 86400)
    reminder_push.push_reminders()
    assert _status(noon) == "cancelled"

    replies = _quote_cancel(monkeypatch, "old-noon-push")

    assert len(replies) == 1 and "已取消提醒" in replies[0]
    assert _status(clock) == "cancelled"


def test_the_reply_piggyback_folds_before_collecting(monkeypatch):
    now = int(time.time()) // 60 * 60
    rids = _dinner_rows(now + 3600)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, now)

    assert main._reply("reply-token", "嗨", group_id=G)

    texts = _texts(batches[0])
    assert texts[0] == "嗨"
    assert sum("⏰" in t for t in texts) == 1
    assert sum(_status(r) == "cancelled" for r in rids) == 2


def test_the_fast_path_folds_before_collecting(monkeypatch):
    now = int(time.time()) // 60 * 60
    rids = _dinner_rows(now + 3600)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, now)

    assert main._try_piggyback_reminders_fast_path("reply-token", G)

    assert sum("⏰" in t for t in _texts(batches[0])) == 1
    assert sum(_status(r) == "cancelled" for r in rids) == 2


# ── write-time merge: the narrow e1/e3 loosening (item 2) ────────────────────


def test_the_dinner_triplet_merges_at_write_time():
    at = _at(_day(30), 17, 30)
    outcomes = [
        memory.add_reminder_with_outcome(G, "U_A", action, at, source_text=source,
                                         time_kind="clock")[1]
        for action, source in DINNER
    ]
    assert outcomes == ["created", "merged", "merged"]


def test_covid_spelled_two_ways_is_one_event_at_the_same_clock():
    at = _at(_day(30), 15)
    first, _ = memory.add_reminder_with_outcome(
        G, "U_A", "打新冠疫苗", at, source_text="下午三點打新冠疫苗", time_kind="clock")
    same, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "打COVID-19疫苗", at, source_text="15:00 打COVID-19疫苗", time_kind="clock")
    assert (same, outcome) == (first, "merged")


@pytest.mark.parametrize("change", ["author", "clock"])
def test_the_loosening_needs_the_same_author_and_the_same_clock(change):
    at = _at(_day(30), 17, 30)
    first, _ = memory.add_reminder_with_outcome(
        G, "U_A", DINNER[0][0], at, source_text=DINNER[0][1], time_kind="clock")
    other, outcome = memory.add_reminder_with_outcome(
        G, "U_B" if change == "author" else "U_A", DINNER[2][0],
        at if change == "author" else at + 1800,
        source_text=DINNER[2][1], time_kind="clock")
    assert outcome == "created" and other != first


def _apart_pairs() -> list[tuple[str, str]]:
    import test_reminder_same_event_merge as base

    pairs: list[tuple[str, str]] = []
    for name in (
        "test_matcher_keeps_different_events_apart",
        "test_different_events_on_the_same_day_are_not_merged",
        "test_reordered_names_at_the_same_clock_are_different_events",
        "test_a_different_object_condition_or_place_never_moves_the_reminder",
        "test_a_different_lane_section_or_street_is_a_different_appointment",
    ):
        mark = next(m for m in getattr(base, name).pytestmark if m.name == "parametrize")
        for first, second in mark.args[1]:
            pairs.append((
                first[0] if isinstance(first, tuple) else first,
                second[0] if isinstance(second, tuple) else second,
            ))
    pairs += [("晚餐", "晚餐後散步"), ("聚餐", "聚餐前買蛋糕"), ("早餐", "早餐前空腹抽血")]
    return pairs


def test_every_known_counterexample_stays_apart_even_in_the_worst_case():
    pairs = _apart_pairs()
    assert len(pairs) >= 70
    for new, kept in pairs:
        for incoming, stored in ((new, kept), (kept, new)):
            assert not ri.mention_matches_reminder(
                incoming, stored,
                time_kind="clock", hhmm="14:00", kept_kind="clock", kept_hhmm="14:00",
                kept_identity=[stored], kept_source=f"14:00 {stored}", same_author=True,
                names=("成員甲", "成員乙", "成員丙"),
            ), (incoming, stored)


def test_the_loosened_matcher_still_recognises_the_known_same_events():
    import test_reminder_same_event_merge as base

    mark = next(m for m in base.test_matcher_recognises_the_same_event.pytestmark
                if m.name == "parametrize")
    for new, kept in mark.args[1]:
        assert ri.mention_matches_reminder(
            new, kept, time_kind="clock", hhmm="14:00", kept_kind="clock",
            kept_hhmm="14:00", kept_identity=[kept], kept_source=kept, same_author=True,
            names=("成員甲", "成員乙", "成員丙"),
        ), (new, kept)


# ── calendar mirror duplicates at write time (item 3, GP1 I11) ───────────────


def _mirror_for(event_id: str) -> dict:
    with memory._conn() as c:
        c.row_factory = __import__("sqlite3").Row
        row = c.execute(
            "SELECT * FROM reminders WHERE group_id=? AND source_kind='calendar_event' "
            "AND source_ref=?",
            (G, event_id),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_a_new_reminder_for_a_mirrored_calendar_event_is_a_duplicate():
    import calendar_db

    d = _day(12)
    event_id = calendar_db.insert_event(
        group_id=G, title="打流感疫苗", event_date=d.isoformat(), event_time="14:00",
    )
    mirror = _mirror_for(event_id)
    count_before = len(_all_reminder_rows())

    rid, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "施打流感疫苗和新冠疫苗", _at(d, 14),
        source_text=f"{_md(d)} 14:00 施打流感疫苗和新冠疫苗", time_kind="clock",
    )

    assert (rid, outcome) == (mirror["reminder_id"], "duplicate")
    assert len(_all_reminder_rows()) == count_before
    assert _mirror_for(event_id) == mirror


def test_a_different_event_on_the_mirrored_day_is_still_created():
    import calendar_db

    d = _day(12)
    calendar_db.insert_event(
        group_id=G, title="打流感疫苗", event_date=d.isoformat(), event_time="14:00",
    )
    _, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "繳房屋稅", _at(d, 14), source_text=f"{_md(d)} 14:00 繳房屋稅",
        time_kind="clock",
    )
    assert outcome == "created"


def test_mirrors_are_checked_only_when_no_natural_reminder_matched():
    import calendar_db

    d = _day(12)
    natural, _ = memory.add_reminder_with_outcome(
        G, "U_A", "打流感疫苗", _at(d, 14), source_text=f"{_md(d)} 14:00 打流感疫苗",
        time_kind="clock",
    )
    # Worded differently, so the exact-text cleanup keeps both rows.
    event_id = calendar_db.insert_event(
        group_id=G, title="流感疫苗接種", event_date=d.isoformat(), event_time="14:00",
    )
    assert _status(natural) == "pending" and _mirror_for(event_id)["status"] == "pending"
    same, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "施打流感疫苗和新冠疫苗", _at(d, 14),
        source_text=f"{_md(d)} 14:00 施打流感疫苗和新冠疫苗", time_kind="clock",
    )
    assert same == natural and outcome in {"merged", "duplicate"}


# ── one message per event inside one piggyback batch (v3 item 6) ─────────────


def _calendar_and_reminder_for_tomorrow(title: str, action: str) -> tuple[str, int]:
    import calendar_db

    tomorrow = _day(1)
    rid = _insert(action, _at(tomorrow, 14), source=f"{_md(tomorrow)} 14:00 {action}")
    event_id = calendar_db.insert_event(
        group_id=G, title=title, event_date=tomorrow.isoformat(), event_time="14:00",
    )
    assert event_id
    return event_id, rid


def _event_offset_marked(event_id: str) -> bool:
    import calendar_db

    with calendar_db._conn() as c:
        row = c.execute("SELECT reminded_1d FROM events WHERE event_id=?", (event_id,)).fetchone()
    return row[0] is not None


def test_one_reply_carries_one_message_for_an_event_and_its_reminder(monkeypatch):
    event_id, rid = _calendar_and_reminder_for_tomorrow("打流感疫苗", "施打流感疫苗和新冠疫苗")
    batches = _fake_line(monkeypatch)

    assert main._reply("reply-token", "嗨", group_id=G)

    texts = _texts(batches[0])
    assert len(texts) == 2 and texts[0] == "嗨" and "⏰" in texts[1]
    assert not any("🔔" in t for t in texts)
    assert _event_offset_marked(event_id) and _row(rid)["pushed_1d"] == 1
    assert _claims() == 0


def test_a_failed_reply_releases_both_halves_of_the_pair(monkeypatch):
    event_id, rid = _calendar_and_reminder_for_tomorrow("打流感疫苗", "施打流感疫苗和新冠疫苗")
    _fake_line(monkeypatch, fail=RuntimeError("reply_token expired (simulated)"))

    assert not main._reply("reply-token", "嗨", group_id=G, allow_push_fallback=False)

    assert not _event_offset_marked(event_id) and _row(rid)["pushed_1d"] == 0
    assert _claims() == 0


def test_different_events_on_the_same_day_keep_both_messages(monkeypatch):
    event_id, rid = _calendar_and_reminder_for_tomorrow("家族聚餐", "打流感疫苗")
    batches = _fake_line(monkeypatch)

    assert main._reply("reply-token", "嗨", group_id=G)

    texts = _texts(batches[0])
    assert len(texts) == 3
    assert sum("🔔" in t for t in texts) == 1 and sum("⏰" in t for t in texts) == 1
    assert _event_offset_marked(event_id) and _row(rid)["pushed_1d"] == 1


def test_an_event_for_someone_else_is_not_swallowed(monkeypatch):
    import calendar_db

    tomorrow = _day(1)
    _insert("施打流感疫苗和新冠疫苗", _at(tomorrow, 14), user="U_A")
    calendar_db.insert_event(
        group_id=G, title="打流感疫苗", event_date=tomorrow.isoformat(), event_time="14:00",
        participants=["成員乙"],
    )
    batches = _fake_line(monkeypatch)
    assert main._reply("reply-token", "嗨", group_id=G)
    assert len(_texts(batches[0])) == 3


def test_the_fast_path_also_sends_one_message_for_the_pair(monkeypatch):
    event_id, rid = _calendar_and_reminder_for_tomorrow("打流感疫苗", "施打流感疫苗和新冠疫苗")
    batches = _fake_line(monkeypatch)

    assert main._try_piggyback_reminders_fast_path("reply-token", G)

    texts = _texts(batches[0])
    assert len(texts) == 1 and "⏰" in texts[0]
    assert _event_offset_marked(event_id) and _row(rid)["pushed_1d"] == 1
    assert _claims() == 0


# ── invariants: never twice at one moment, never a replayed backlog (item 7) ─


def test_failed_pushes_never_replay_the_missed_stages(monkeypatch):
    at = _at(_day(5), 17, 30)
    rid = _insert("繳停車費", at)
    calls = _record_pushes(monkeypatch, outcomes=[False, False, True, True])
    runs = [
        (at - 3 * 86400, 1),          # 3-day stage: LINE 429
        (at - 86400, 1),              # 1-day stage: LINE 429
        (at - 4 * 3600, 1),           # 4-hour stage: accepted
        (at - 4 * 3600 + 900, 0),     # next run: nothing to send
        (at - 2 * 3600, 1),           # 2-hour stage: accepted
    ]
    for moment, attempts in runs:
        _set_now(monkeypatch, moment)
        before = len(calls)
        reminder_push.push_reminders()
        assert len(calls) - before == attempts

    delivered = [c["text"] for c in calls if c["ok"]]
    assert len(delivered) == 2
    assert "（4 小時後）" in delivered[0] and "（2 小時後）" in delivered[1]
    row = _row(rid)
    assert (row["pushed_3d"], row["pushed_1d"], row["pushed_4hr"], row["pushed_2hr"]) == (0, 0, 1, 1)


def test_a_receipt_and_its_new_stage_never_go_out_together(monkeypatch):
    tomorrow = _day(1)
    first, _ = memory.add_reminder_with_outcome(
        G, "U_A", "海景餐廳用餐", _at(tomorrow, 17, 30),
        source_text="明天晚上5:30海景餐廳", time_kind="clock")
    second, _ = memory.add_reminder_with_outcome(
        G, "U_A", "繳房屋稅", _at(_day(3), 12), source_text="大後天中午繳房屋稅",
        time_kind="clock")
    batches = _fake_line(monkeypatch)

    assert main._reply(
        "receipt-token", "已新增 2 筆提醒", group_id=G, allow_push_fallback=False,
        primary_reminder_ref={"reminder_id": first, "reminder_ids": [first, second]},
    )
    assert _texts(batches[0]) == ["已新增 2 筆提醒"]

    assert memory.consume_open_stages([first, second]) == 2
    assert main._reply("next-token", "嗨", group_id=G)
    assert _texts(batches[1]) == ["嗨"]
    assert reminder_push._due_reminder_items(group_id=G) == []


def test_one_moment_one_message_for_folded_rows_across_all_senders(monkeypatch):
    now = int(time.time()) // 60 * 60
    _dinner_rows(now + 3600)
    batches = _fake_line(monkeypatch)
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, now)

    main._reply("reply-token", "嗨", group_id=G)
    reminder_push.push_reminders()
    main._try_piggyback_reminders_fast_path("other-token", G)

    natural_messages = sum("⏰" in t for batch in batches for t in _texts(batch))
    assert natural_messages + len(calls) == 1


# ── Phase 6 r2 (GP1): a receipt is the notice; a mirror rides with its reminder ─
# Andrew 2026-10-04: the same reminder / the same real-world event never yields
# two messages at one moment.  Three reviewer probes plus their negatives.


def _text_event(
    text: str, *, message_id: str, user: str = "U_A", quoted: str | None = None
) -> MessageEvent:
    message = MagicMock(spec=TextMessageContent)
    message.id = message_id
    message.text = text
    message.mention = None
    message.quoted_message_id = quoted
    message.quote_token = "quote-token"
    message.type = "text"
    source = MagicMock(spec=GroupSource)
    source.group_id = G
    source.user_id = user
    event = MagicMock(spec=MessageEvent)
    event.message = message
    event.source = source
    event.reply_token = "receipt-token"
    event.timestamp = int(time.time() * 1000)
    event.delivery_context = SimpleNamespace(is_redelivery=False)
    return event


def _route_quietly(monkeypatch) -> None:
    """Only the reminder paths answer: no model, no chat, no burst."""
    monkeypatch.setattr(main.feedback_collector, "in_feedback_window", lambda: False)
    monkeypatch.setattr(main.burst_filter, "add_to_burst", MagicMock())
    monkeypatch.setattr(main.burst_filter, "cancel_burst", MagicMock(return_value=[]))
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_k: False)
    monkeypatch.setattr(
        main, "_handle_explicit_text",
        MagicMock(side_effect=AssertionError("must not reach chat")),
    )


def _seed_raw(message_id: str, user: str, text: str, age_sec: int = 600) -> None:
    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (G, message_id, user, text, int(time.time()) - age_sec),
        )


def _reminded(event_id: str, offset: int):
    import calendar_db

    with calendar_db._conn() as c:
        return c.execute(
            f"SELECT reminded_{int(offset)}d FROM events WHERE event_id=?", (event_id,)
        ).fetchone()[0]


def _two_line_schedule() -> str:
    # Both lines default to 12:00, two and three days out: a day-level stage is open.
    return f"{_md(_day(2))}去看牙醫\n{_md(_day(3))}回診拿藥"


def _stage_flags(rid: int) -> tuple:
    row = _row(rid)
    return tuple(row[c] for c in ("pushed_3d", "pushed_1d", "pushed_4hr", "pushed_2hr",
                                  "pushed_1hr", "pushed_now", "last_weekly_at"))


# finding 1: a duplicate receipt names its reminders too


def test_a_duplicate_receipt_never_carries_its_own_open_stage(monkeypatch):
    # GP1 r2 probe: 看牙醫 tomorrow 14:00 still owes its 1-day notice (LINE push
    # quota ran out); the same request again answers 「提醒已存在」.
    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    rid, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "看牙醫", _at(_day(1), 14), source_text="明天下午兩點看牙醫",
        time_kind="clock",
    )
    assert outcome == "created"

    main._handle_text_message(_text_event("提醒我明天下午兩點看牙醫", message_id="m-dup"), G)

    assert len(batches) == 1
    texts = _texts(batches[0])
    assert len(texts) == 1 and texts[0].startswith("提醒已存在") and "看牙醫" in texts[0]
    # A plain duplicate stands in for nothing: the 1-day notice goes out later.
    assert _row(rid)["pushed_1d"] == 0
    assert [i["stage"] for i in reminder_push._due_reminder_items(group_id=G)] == ["1d"]


def test_a_duplicate_schedule_list_receipt_carries_none_of_its_items(monkeypatch):
    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    text = _two_line_schedule()
    created = main._maybe_extract_reminder(text, G, "U_A", "m-list")   # never delivered
    assert created.startswith("已新增 2 筆提醒")
    before = {rid: _stage_flags(rid) for rid in created.reminder_ids}

    main._handle_text_message(_text_event(text, message_id="m-list"), G)

    assert len(batches) == 1
    texts = _texts(batches[0])
    assert len(texts) == 1 and texts[0].startswith("2 筆提醒皆已存在")
    assert {rid: _stage_flags(rid) for rid in created.reminder_ids} == before
    assert len(reminder_push._due_reminder_items(group_id=G)) == 2


def test_a_duplicate_quoted_capture_receipt_carries_none_of_its_items(monkeypatch):
    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    text = _two_line_schedule()
    _seed_raw("m-src", "U_B", text)
    first = main._create_schedule_reminders(
        G, "U_B", "m-src", text, main._local_schedule_list_items(text, _day(0))
    )
    assert first.startswith("已新增 2 筆提醒")

    main._handle_text_message(_text_event("咪寶", message_id="m-cmd", quoted="m-src"), G)

    assert len(batches) == 1
    texts = _texts(batches[0])
    assert len(texts) == 1 and texts[0].startswith("2 筆提醒皆已存在")


def test_a_duplicate_followup_receipt_names_its_reminder_without_consuming():
    # Sent with include_auxiliary=False, but it names its reminder like the others.
    _seed_raw("m-fsrc", "U_A", "明天下午3點看牙醫", age_sec=30)
    _seed_raw("m-fcmd", "U_A", "加到提醒", age_sec=0)
    command = _text_event("加到提醒", message_id="m-fcmd", quoted="m-fsrc")
    created = main._creation_followup_reply(command, G, "加到提醒")
    assert isinstance(created, main.ReminderReceipt) and len(created.reminder_ids) == 1
    (rid,) = created.reminder_ids

    again = main._creation_followup_reply(command, G, "加到提醒")

    assert isinstance(again, main.ReminderReceipt) and again.startswith("提醒已存在")
    assert again.reminder_ids == ()                    # nothing to consume
    assert main._receipt_reply_ref(again) == {"reminder_ids": [rid]}


def test_a_duplicate_of_a_calendar_mirror_keeps_the_event_notice_out_of_the_reply(
    monkeypatch,
):
    import calendar_db

    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    tomorrow = _day(1)
    event_id = calendar_db.insert_event(
        group_id=G, title="打流感疫苗", event_date=tomorrow.isoformat(), event_time="14:00",
    )
    receipt = main._maybe_extract_reminder("提醒我明天下午兩點打流感疫苗", G, "U_A", "m-dm")
    assert receipt.startswith("提醒已存在")

    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )

    assert _texts(batches[0]) == [str(receipt)]
    assert _reminded(event_id, 1) is None              # a duplicate marks nothing
    assert _claims() == 0


# finding 2: a calendar-capture receipt is that event's notice


def test_a_calendar_capture_receipt_is_the_events_notice(monkeypatch):
    # GP1 r2 probe: the receipt and the new event's own 🔔 went out in one reply.
    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)

    main._handle_text_message(_text_event("明天晚上6點全家去餐廳聚餐", message_id="m-cal"), G)

    assert len(batches) == 1
    texts = _texts(batches[0])
    assert len(texts) == 1 and texts[0].startswith("已新增提醒") and "全家去餐廳聚餐" in texts[0]
    import calendar_db

    (event,) = calendar_db.find_active_events_by_source_message(G, "m-cal")
    assert _reminded(event["event_id"], 1) is not None    # the receipt was the notice
    assert _claims() == 0
    assert main._reply("next-token", "嗨", group_id=G)
    assert _texts(batches[1]) == ["嗨"]


def test_a_failed_calendar_capture_receipt_marks_nothing(monkeypatch):
    import calendar_db

    _route_quietly(monkeypatch)
    _fake_line(monkeypatch, fail=RuntimeError("reply_token expired (simulated)"))

    main._handle_text_message(_text_event("明天晚上6點全家去餐廳聚餐", message_id="m-cal"), G)

    (event,) = calendar_db.find_active_events_by_source_message(G, "m-cal")
    assert _reminded(event["event_id"], 1) is None
    assert _claims() == 0
    mirror = _mirror_for(event["event_id"])
    assert (mirror["pushed_1d"], mirror["pushed_4hr"], mirror["pushed_1hr"]) == (0, 0, 0)


def test_a_calendar_capture_receipt_leaves_other_events_alone(monkeypatch):
    import calendar_db

    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    other = calendar_db.insert_event(
        group_id=G, title="繳房屋稅", event_date=_day(1).isoformat(), event_time="10:00",
    )

    main._handle_text_message(_text_event("明天晚上6點全家去餐廳聚餐", message_id="m-cal"), G)

    texts = _texts(batches[0])
    assert len(texts) == 2 and texts[0].startswith("已新增提醒")
    assert "🔔" in texts[1] and "繳房屋稅" in texts[1]
    (event,) = calendar_db.find_active_events_by_source_message(G, "m-cal")
    assert _reminded(other, 1) is not None and _reminded(event["event_id"], 1) is not None
    assert _claims() == 0


def test_a_calendar_capture_receipt_keeps_its_mirror_out_of_the_reply(monkeypatch):
    import calendar_db

    tomorrow = _day(1)
    _seed_raw("m-cap", "U_A", "明天下午2點全家聚餐")
    event_id = calendar_db.insert_event(
        group_id=G, title="全家聚餐", event_date=tomorrow.isoformat(), event_time="14:00",
        source_msg_id="m-cap",
    )
    receipt = main._format_source_calendar_capture_confirmation(G, "m-cap", "明天下午2點全家聚餐")
    mirror = _mirror_for(event_id)
    assert receipt.event_ids == (event_id,)
    assert receipt.reminder_ids == (mirror["reminder_id"],)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(tomorrow, 13))      # the mirror's 1-hour stage is open

    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )

    assert _texts(batches[0]) == [str(receipt)]
    assert _claims() == 0


# finding 3 (S10 i): a natural reminder and a later calendar mirror of one event


def _natural_and_later_mirror(day, action: str = "施打流感疫苗和新冠疫苗",
                              title: str = "打流感疫苗") -> tuple[int, int]:
    import calendar_db

    rid, outcome = memory.add_reminder_with_outcome(
        G, "U_A", action, _at(day, 14), source_text=f"{_md(day)} 14:00 {action}",
        time_kind="clock",
    )
    assert outcome == "created"
    event_id = calendar_db.insert_event(
        group_id=G, title=title, event_date=day.isoformat(), event_time="14:00",
    )
    assert event_id
    return rid, int(_mirror_for(event_id)["reminder_id"])


def test_a_natural_reminder_and_a_later_calendar_mirror_push_once(monkeypatch):
    # GP1 r2 probe: two 「⏰ 提醒（1 小時後）」 for one appointment in one run.
    d = _day(3)
    natural, mirror = _natural_and_later_mirror(d)
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))

    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert "（1 小時後）" in calls[0]["text"] and "施打流感疫苗和新冠疫苗" in calls[0]["text"]
    assert _row(natural)["pushed_1hr"] == 1 and _row(mirror)["pushed_1hr"] == 1
    assert _status(mirror) == "pending"            # never cancelled, never folded
    assert _claims() == 0
    _set_now(monkeypatch, _at(d, 13, 15))
    assert reminder_push.push_reminders() == 0 and len(calls) == 1


def test_different_events_at_the_same_time_still_push_twice(monkeypatch):
    d = _day(3)
    natural, mirror = _natural_and_later_mirror(d, action="繳房屋稅")
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))

    assert reminder_push.push_reminders() == 2

    assert sorted(c["reminder_id"] for c in calls) == sorted([natural, mirror])
    assert _row(natural)["pushed_1hr"] == 1 and _row(mirror)["pushed_1hr"] == 1


def test_a_failed_push_releases_the_reminder_and_its_mirror(monkeypatch):
    d = _day(3)
    natural, mirror = _natural_and_later_mirror(d)
    calls = _record_pushes(monkeypatch, outcomes=[False, False])
    _set_now(monkeypatch, _at(d, 13))

    assert reminder_push.push_reminders() == 0

    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert _row(natural)["pushed_1hr"] == 0 and _row(mirror)["pushed_1hr"] == 0
    assert _claims() == 0
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 13, 15))
    assert reminder_push.push_reminders() == 1
    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert _row(natural)["pushed_1hr"] == 1 and _row(mirror)["pushed_1hr"] == 1


def test_the_reply_piggyback_sends_one_message_for_a_reminder_and_its_mirror(monkeypatch):
    d = _day(5)                      # no calendar offset is due five days out
    natural, mirror = _natural_and_later_mirror(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))

    assert main._reply("reply-token", "嗨", group_id=G)

    texts = _texts(batches[0])
    assert len(texts) == 2 and texts[0] == "嗨"
    assert "⏰" in texts[1] and "施打流感疫苗和新冠疫苗" in texts[1]
    assert _row(natural)["pushed_1hr"] == 1 and _row(mirror)["pushed_1hr"] == 1
    assert _status(mirror) == "pending" and _claims() == 0


def test_a_failed_piggyback_releases_the_reminder_and_its_mirror(monkeypatch):
    d = _day(5)
    natural, mirror = _natural_and_later_mirror(d)
    batches = _fake_line(monkeypatch, fail=RuntimeError("reply_token expired (simulated)"))
    _set_now(monkeypatch, _at(d, 13))

    assert not main._reply("reply-token", "嗨", group_id=G, allow_push_fallback=False)

    assert len(_texts(batches[0])) == 2
    assert _row(natural)["pushed_1hr"] == 0 and _row(mirror)["pushed_1hr"] == 0
    assert _claims() == 0


def test_the_fast_path_sends_one_message_for_a_reminder_and_its_mirror(monkeypatch):
    d = _day(5)
    natural, mirror = _natural_and_later_mirror(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))

    assert main._try_piggyback_reminders_fast_path("reply-token", G)

    texts = _texts(batches[0])
    assert len(texts) == 1 and "施打流感疫苗和新冠疫苗" in texts[0]
    assert _row(natural)["pushed_1hr"] == 1 and _row(mirror)["pushed_1hr"] == 1
    assert _claims() == 0


def test_the_fast_path_keeps_different_events_at_the_same_time_apart(monkeypatch):
    d = _day(5)
    _natural_and_later_mirror(d, action="繳房屋稅")
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))

    assert main._try_piggyback_reminders_fast_path("reply-token", G)

    assert len(_texts(batches[0])) == 2


def test_a_receipt_keeps_the_mirror_of_its_reminder_out_of_the_reply(monkeypatch):
    # The receipt names the reminder; its event's mirror is the same event.
    d = _day(5)
    natural, mirror = _natural_and_later_mirror(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))              # both owe their 1-hour notice
    receipt = main.ReminderReceipt("提醒已存在，未重複新增", (), (natural,))

    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )

    assert _texts(batches[0]) == ["提醒已存在，未重複新增"]
    assert _row(natural)["pushed_1hr"] == 0 and _row(mirror)["pushed_1hr"] == 0
    assert _claims() == 0


def test_a_receipt_keeps_the_calendar_notice_of_its_event_out_of_the_reply(monkeypatch):
    event_id, rid = _calendar_and_reminder_for_tomorrow("打流感疫苗", "施打流感疫苗和新冠疫苗")
    batches = _fake_line(monkeypatch)
    receipt = main.ReminderReceipt("提醒已存在，未重複新增", (), (rid,))

    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )

    assert _texts(batches[0]) == ["提醒已存在，未重複新增"]
    assert not _event_offset_marked(event_id) and _row(rid)["pushed_1d"] == 0
    assert _claims() == 0


# ── fixC12 (GP1 r2 important): a receipt's reply keeps its event whole, and a
# 前一天／當天 row rides with the reminder of its appointment ────────────────
# Andrew 2026-10-04: one reminder / one real-world event never yields two
# messages at one moment; missed stages are never replayed.


def _banquet_rows(day) -> tuple[int, int]:
    """An older 晚宴 row and a newer 聚會 row that the fold pairs one way only.

    The newer message also says 晚宴, so the fold reads the older row into the
    newer one (same author, same clock, same place, e3) although the older
    message never says 聚會.  Inserted directly, as rows written before the
    write-time merge read such a pair both ways.
    """
    older = _insert("海景餐廳晚宴", _at(day, 18), source=f"{_md(day)} 晚上6點海景餐廳晚宴")
    newer = _insert("海景餐廳聚會", _at(day, 18),
                    source=f"{_md(day)} 晚宴改成晚上6點海景餐廳聚會")
    return older, newer


def _send_receipt(text: str, *ids: int) -> bool:
    receipt = main.ReminderReceipt(text, ids, ids)
    return main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )


# finding: the fold in a receipt's reply cancelled the row the receipt names


def test_a_receipt_reply_never_folds_away_the_row_it_names(monkeypatch):
    # GP1 r2 probe: the receipt's new row was cancelled into the older one and
    # the older row's 1-day notice rode with the receipt.
    tomorrow = _day(1)
    older, newer = _banquet_rows(tomorrow)
    moment = _at(tomorrow, 18) - 30 * 3600            # both owe the 1-day notice
    _set_now(monkeypatch, moment)
    batches = _fake_line(monkeypatch)

    assert _send_receipt("已新增提醒：海景餐廳聚會", newer)

    assert _texts(batches[0]) == ["已新增提醒：海景餐廳聚會"]
    assert _status(newer) == _status(older) == "pending"
    assert _row(older)["pushed_1d"] == 0 and _row(newer)["pushed_1d"] == 0
    assert _claims() == 0
    # The receipt was the event's notice.  Once its stage is consumed, the
    # next moment folds the pair, the consumed stage moves over, nothing more.
    assert memory.consume_open_stages([newer], now=moment) == 1
    _set_now(monkeypatch, moment + 15 * 60)
    assert main._reply("next-token", "嗨", group_id=G)
    assert _texts(batches[1]) == ["嗨"]
    assert sorted(_status(r) for r in (older, newer)) == ["cancelled", "pending"]


def test_a_receipt_reply_holds_a_row_that_already_absorbed_the_named_one(monkeypatch):
    # Another sender folded the receipt's row away just before the receipt.
    tomorrow = _day(1)
    older, newer = _banquet_rows(tomorrow)
    _set_now(monkeypatch, _at(tomorrow, 18) - 30 * 3600)
    assert reminder_push.fold_due_duplicates(G) == 1
    assert _status(newer) == "cancelled" and "海景餐廳聚會" in _details(older)
    batches = _fake_line(monkeypatch)

    assert _send_receipt("已新增提醒：海景餐廳聚會", newer)

    assert _texts(batches[0]) == ["已新增提醒：海景餐廳聚會"]
    assert _row(older)["pushed_1d"] == 0 and _claims() == 0


def test_a_receipt_reply_still_carries_another_event(monkeypatch):
    tomorrow = _day(1)
    older, newer = _banquet_rows(tomorrow)
    other = _insert("繳房屋稅", _at(tomorrow, 10))
    _set_now(monkeypatch, _at(tomorrow, 18) - 30 * 3600)
    batches = _fake_line(monkeypatch)

    assert _send_receipt("已新增提醒：海景餐廳聚會", newer)

    texts = _texts(batches[0])
    assert len(texts) == 2 and texts[0] == "已新增提醒：海景餐廳聚會"
    assert "繳房屋稅" in texts[1] and "海景餐廳" not in texts[1]
    assert _row(other)["pushed_1d"] == 1 and _row(older)["pushed_1d"] == 0
    assert _claims() == 0


def test_same_event_ids_makes_no_writes():
    # GP2 r3 nit: it runs inside a receipt's reply and says it writes nothing,
    # but it read through the duplicate cleanup (BEGIN IMMEDIATE + DELETE).
    tomorrow = _day(1)
    older, newer = _banquet_rows(tomorrow)
    _insert("繳房屋稅", _at(tomorrow, 10))
    _insert("繳房屋稅", _at(tomorrow, 10))          # an exact duplicate the cleanup deletes
    before = _all_reminder_rows()

    assert reminder_push.same_event_ids(G, [newer]) == {older}

    assert _all_reminder_rows() == before


def test_a_pair_only_the_fold_reads_one_way_is_written_as_two_rows():
    # fixR4b (GP1 r3 important #2): the write-time reverse reading merged such
    # a mention into the older row, whose words then hid the new ones.  The
    # mention is written as its own row again; the fold pairs the two at the
    # next moment and keeps the row with the most content (the receipt's own
    # reply is covered by keep_ids and same_event_ids, see the tests above).
    tomorrow = _day(1)
    first, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "海景餐廳晚宴", _at(tomorrow, 18),
        source_text=f"{_md(tomorrow)} 晚上6點海景餐廳晚宴", time_kind="clock")
    assert outcome == "created"
    other, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "海景餐廳聚會", _at(tomorrow, 18),
        source_text=f"{_md(tomorrow)} 晚宴改成晚上6點海景餐廳聚會", time_kind="clock")
    assert outcome == "created" and other != first
    assert _details(first) == "[]" and _details(other) == "[]"

    rows = memory.list_pending_reminders_full(G, dedupe=False)
    plans = reminder_push.plan_folds(rows, _at(tomorrow, 18) - 30 * 3600)
    assert [
        sorted([int(primary["reminder_id"]), *(int(p["reminder_id"]) for p in peers)])
        for primary, peers in plans
    ] == [sorted([first, other])]


@pytest.mark.parametrize("change", ["author", "clock", "words"])
def test_the_reverse_reading_keeps_the_narrow_conditions(change):
    tomorrow = _day(1)
    first, _ = memory.add_reminder_with_outcome(
        G, "U_A", "海景餐廳晚宴", _at(tomorrow, 18),
        source_text=f"{_md(tomorrow)} 晚上6點海景餐廳晚宴", time_kind="clock")
    source = {
        "author": "晚宴改成晚上6點海景餐廳聚會",
        "clock": "晚宴改成晚上6點半海景餐廳聚會",
        "words": "晚上6點海景餐廳聚會",
    }[change]
    other, outcome = memory.add_reminder_with_outcome(
        G, "U_B" if change == "author" else "U_A", "海景餐廳聚會",
        _at(tomorrow, 18, 30 if change == "clock" else 0),
        source_text=f"{_md(tomorrow)} {source}", time_kind="clock")
    assert outcome == "created" and other != first


# finding: a 前一天／當天 row and the reminder of the same appointment


def _contextual(action: str, day, *, lead: bool = False, user: str = "U_A",
                mentions: str = '["成員甲"]') -> int:
    """One slot of a 前一天／當天 pair the user asked for (09:00)."""
    remind_day = day - timedelta(days=1) if lead else day
    return _insert(
        action.format(md=_md(day)), _at(remind_day, 9), user=user, kind=None,
        source_kind="contextual_date_once",
        source_ref=f"M-ctx:{'lead' if lead else 'same'}:0", mentions=mentions,
    )


def _same_day_pair(day, action: str = "成員甲 看牙醫（當天提醒）") -> tuple[int, int]:
    same_day = _contextual(action, day)
    natural = _insert("成員甲看牙醫", _at(day, 10, 30))
    return same_day, natural


@pytest.mark.parametrize(
    "action",
    [
        "成員甲 看牙醫（當天提醒）",
        "成員甲 {md} 10:30 看牙醫（當天提醒）",          # the batch's own label
        "成員甲 {md} 看牙醫（當天提醒）",
    ],
)
def test_a_same_day_row_and_its_reminder_push_once(monkeypatch, action):
    # GP1 r2 probe: at 09:05 both 「看牙醫（當天提醒）」 and 「看牙醫」(1 小時後)
    # went out in one launchd run.
    d = _day(3)
    same_day, natural = _same_day_pair(d, action)
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))

    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert "（1 小時後）" in calls[0]["text"] and "10:30" in calls[0]["text"]
    assert _status(same_day) == "done" and _row(same_day)["pushed_now"] == 1
    assert _row(natural)["pushed_1hr"] == 1 and _status(natural) == "pending"
    assert _claims() == 0
    _set_now(monkeypatch, _at(d, 9, 20))
    assert reminder_push.push_reminders() == 0 and len(calls) == 1


def test_a_failed_push_releases_the_reminder_and_its_same_day_row(monkeypatch):
    d = _day(3)
    same_day, natural = _same_day_pair(d)
    calls = _record_pushes(monkeypatch, outcomes=[False, False])
    _set_now(monkeypatch, _at(d, 9, 5))

    assert reminder_push.push_reminders() == 0

    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert _status(same_day) == "pending" and _row(same_day)["pushed_now"] == 0
    assert _row(natural)["pushed_1hr"] == 0 and _claims() == 0
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 20))
    assert reminder_push.push_reminders() == 1
    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert _status(same_day) == "done" and _row(natural)["pushed_1hr"] == 1


def test_the_reply_piggyback_sends_one_message_for_a_reminder_and_its_same_day_row(
    monkeypatch,
):
    d = _day(3)
    same_day, natural = _same_day_pair(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))

    assert main._reply("reply-token", "嗨", group_id=G)

    texts = _texts(batches[0])
    assert len(texts) == 2 and texts[0] == "嗨" and "（1 小時後）" in texts[1]
    assert _status(same_day) == "done" and _row(natural)["pushed_1hr"] == 1
    assert _claims() == 0


def test_a_failed_piggyback_releases_the_reminder_and_its_same_day_row(monkeypatch):
    d = _day(3)
    same_day, natural = _same_day_pair(d)
    batches = _fake_line(monkeypatch, fail=RuntimeError("reply_token expired (simulated)"))
    _set_now(monkeypatch, _at(d, 9, 5))

    assert not main._reply("reply-token", "嗨", group_id=G, allow_push_fallback=False)

    assert len(_texts(batches[0])) == 2
    assert _status(same_day) == "pending" and _row(same_day)["pushed_now"] == 0
    assert _row(natural)["pushed_1hr"] == 0 and _claims() == 0


def test_the_fast_path_sends_one_message_for_a_reminder_and_its_same_day_row(monkeypatch):
    d = _day(3)
    same_day, natural = _same_day_pair(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))

    assert main._try_piggyback_reminders_fast_path("reply-token", G)

    texts = _texts(batches[0])
    assert len(texts) == 1 and "（1 小時後）" in texts[0]
    assert _status(same_day) == "done" and _row(natural)["pushed_1hr"] == 1
    assert _claims() == 0


def test_a_lead_day_row_rides_on_the_reminders_day_notice(monkeypatch):
    # 前一天 09:00: the appointment's own 1-day notice is still owed (LINE push
    # quota ran out), so both would say "tomorrow" at the same moment.
    d = _day(3)
    lead = _contextual("成員甲 {md} 10:30 看牙醫（前一天提醒）", d, lead=True)
    natural = _insert("成員甲看牙醫", _at(d, 10, 30))
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d - timedelta(days=1), 9, 5))

    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert "（明天）" in calls[0]["text"]
    assert _status(lead) == "done" and _row(natural)["pushed_1d"] == 1


def test_a_lead_day_row_goes_out_alone_when_no_reminder_stage_is_due(monkeypatch):
    d = _day(3)
    lead = _contextual("成員甲 {md} 10:30 看牙醫（前一天提醒）", d, lead=True)
    natural = _insert("成員甲看牙醫", _at(d, 10, 30), pushed_1d=1)   # already sent
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d - timedelta(days=1), 9, 5))

    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and calls[0]["reminder_id"] == lead
    assert "前一天提醒" in calls[0]["text"]
    assert _status(lead) == "done" and _status(natural) == "pending"


@pytest.mark.parametrize(
    ("action", "user", "mentions"),
    [
        ("成員甲 繳房屋稅（當天提醒）", "U_A", '["成員甲"]'),           # another errand
        ("成員乙 看牙醫（當天提醒）", "U_B", '["成員乙"]'),              # someone else
        ("成員甲 {md} 15:00 看牙醫（當天提醒）", "U_A", '["成員甲"]'),   # another time
    ],
)
def test_a_same_day_row_for_another_appointment_still_goes_out(
    monkeypatch, action, user, mentions
):
    d = _day(3)
    same_day = _contextual(action, d, user=user, mentions=mentions)
    natural = _insert("成員甲看牙醫", _at(d, 10, 30))
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))

    assert reminder_push.push_reminders() == 2

    assert sorted(c["reminder_id"] for c in calls) == sorted([same_day, natural])
    assert _status(same_day) == "done" and _row(natural)["pushed_1hr"] == 1


def test_a_receipt_keeps_the_same_day_row_of_its_reminder_out_of_the_reply(monkeypatch):
    d = _day(3)
    same_day, natural = _same_day_pair(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))
    receipt = main.ReminderReceipt("提醒已存在，未重複新增", (), (natural,))

    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )

    assert _texts(batches[0]) == ["提醒已存在，未重複新增"]
    assert _status(same_day) == "pending" and _row(same_day)["pushed_now"] == 0
    assert _row(natural)["pushed_1hr"] == 0 and _claims() == 0


def test_a_contextual_receipt_keeps_the_reminder_of_its_appointment_out(monkeypatch):
    d = _day(3)
    same_day, natural = _same_day_pair(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))

    assert _send_receipt("已新增 4 筆提醒", same_day)

    assert _texts(batches[0]) == ["已新增 4 筆提醒"]
    assert _row(natural)["pushed_1hr"] == 0 and _status(same_day) == "pending"
    assert _claims() == 0


def test_a_same_day_row_is_not_paired_with_the_next_days_appointment(monkeypatch):
    d = _day(3)
    same_day = _contextual("成員甲 看牙醫（當天提醒）", d)
    tomorrow_visit = _insert("成員甲看牙醫", _at(d + timedelta(days=1), 10, 30))
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))          # the next visit owes its 1-day notice

    assert reminder_push.push_reminders() == 2

    assert sorted(c["reminder_id"] for c in calls) == sorted([same_day, tomorrow_visit])


def test_a_label_naming_another_day_is_not_guessed(monkeypatch):
    d = _day(3)
    other_day = d + timedelta(days=2)
    same_day = _contextual(f"成員甲 {_md(other_day)} 看牙醫（當天提醒）", d)
    natural = _insert("成員甲看牙醫", _at(d, 10, 30))
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 9, 5))

    assert reminder_push.push_reminders() == 2
    assert sorted(c["reminder_id"] for c in calls) == sorted([same_day, natural])


def _mirror_row(action: str, at: int, event_id: str) -> int:
    hhmm = datetime.fromtimestamp(at, TZ).strftime("%H:%M")
    return _insert(action, at, kind=None, source_kind="calendar_event",
                   source_ref=event_id, source=f"{action}；時間：{hhmm}")


def test_a_mirror_that_matched_the_same_day_row_follows_it_to_the_reminder(monkeypatch):
    # All three fall due at the appointment time (noon is also the pair's
    # default time); whichever order they come in, one message carries them.
    d = _day(3)
    same_day = _contextual("成員甲 看牙醫（當天提醒）", d)
    with memory._conn() as c:
        c.execute("UPDATE reminders SET remind_at=? WHERE reminder_id=?",
                  (_at(d, 12), same_day))
    mirror = _mirror_row("看牙醫", _at(d, 12), "E-dentist")
    natural = _insert("成員甲看牙醫", _at(d, 12))
    _set_now(monkeypatch, _at(d, 12, 5))
    items = reminder_push._due_reminder_items(group_id=G)
    position = {int(item["reminder_id"]): index for index, item in enumerate(items)}
    assert set(position) == {same_day, mirror, natural}
    ordered = sorted(items, key=lambda item: [same_day, mirror, natural].index(
        int(item["reminder_id"])))

    riding = reminder_push.items_riding_on_reminders(ordered)

    assert riding == {0: 2, 1: 2}
    calls = _record_pushes(monkeypatch)
    assert reminder_push.push_reminders() == 1
    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert {_status(rid) for rid in (same_day, mirror, natural)} == {"done"}
    assert _claims() == 0


# ── fixR4b / fixR5a (GP1 r3 #2, GP1 r4 #1 #2): a later, fuller mention is
# what the family sees, while the reminder keeps its own wording ─────────────
# Andrew 2026-10-04: one reminder per event, keep the most complete one.  The
# fuller words used to live only in merged_details (the list report), so the
# receipt and every push said 「繳電費」 and never mentioned the gas bill.
# fixR4b made the fuller words the reminder's wording; that broke the pairing
# with the event's calendar notice (it compares the title with the wording)
# and quoting a message sent before (cancel / reschedule match the wording).
# fixR5a: the wording never changes; the receipt and the push show the
# absorbed wordings that add words on one 「細節：…」 line.


FULLER = (
    ("繳電費", "繳電費和瓦斯費"),
    ("看牙醫", "看牙醫順便洗牙"),
    ("打流感疫苗", "施打流感疫苗和新冠疫苗"),
)


def _detail_actions(rid: int) -> list[str]:
    return [item["action"] for item in json.loads(_row(rid)["merged_details"] or "[]")]


def _write(action: str, day, hour: int, source: str, *, user: str = "U_A",
           kind: str = "clock") -> tuple[int, str]:
    return memory.add_reminder_with_outcome(
        G, user, action, _at(day, hour), source_text=f"{_md(day)} {source}", time_kind=kind)


def _receipt_for(outcome: str, rid: int, action: str, at: int) -> str:
    return main._format_persisted_reminder_confirmation(
        outcome, rid, action, datetime.fromtimestamp(at, TZ))


@pytest.mark.parametrize(("older", "fuller"), FULLER)
def test_a_fuller_mention_keeps_the_wording_and_the_receipt_shows_its_words(older, fuller):
    # GP1 r3 #2 probe: same author, same clock, a row written for the older words.
    d = _day(3)
    first, outcome = _write(older, d, 14, f"14:00 {older}")
    assert outcome == "created"

    same, outcome = _write(fuller, d, 14, f"14:00 {fuller}")

    assert (same, outcome) == (first, "merged") and len(_all_reminder_rows()) == 1
    assert _row(first)["action"] == older              # the reminder's identity
    assert fuller in _detail_actions(first)
    assert _receipt_for(outcome, same, fuller, _at(d, 14)) == (
        f"已更新既有提醒，未重複新增\n時間：{d.isoformat()} 14:00\n"
        f"事項：成員甲 {older}\n細節：{fuller}"
    )


@pytest.mark.parametrize(("older", "fuller"), FULLER)
def test_the_receipt_and_the_next_push_show_the_fuller_words(monkeypatch, older, fuller):
    batches = _fake_line(monkeypatch)
    calls = _record_pushes(monkeypatch)
    tomorrow = _day(1)
    first = main._maybe_extract_reminder(f"提醒我明天下午兩點{older}", G, "U_A", "m-older")
    assert first.startswith("已新增提醒")      # never delivered: its 1-day notice is owed
    (rid,) = first.reminder_ids

    receipt = main._maybe_extract_reminder(f"提醒我明天下午兩點{fuller}", G, "U_A", "m-fuller")

    assert receipt.startswith("已更新既有提醒")
    assert f"事項：成員甲 {older}\n細節：{fuller}" in receipt
    assert receipt.reminder_ids == (rid,) and len(_all_reminder_rows()) == 1
    assert _row(rid)["action"] == older
    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )
    assert _texts(batches[0]) == [str(receipt)]        # no stage rides along
    main._consume_receipt_open_stages(receipt, G)
    assert _row(rid)["pushed_1d"] == 1 and _claims() == 0   # the receipt was the 1-day notice
    # The next notice (4 hours before): one message, with the fuller words.
    _set_now(monkeypatch, _at(tomorrow, 10, 5))
    assert reminder_push.push_reminders() == 1
    assert len(calls) == 1 and calls[0]["reminder_id"] == rid
    assert "（4 小時後）" in calls[0]["text"]
    assert calls[0]["text"].endswith(
        f"{tomorrow.isoformat()} 14:00 成員甲 {older}\n細節：{fuller}")
    assert _row(rid)["action"] == older and _claims() == 0


def test_a_mention_that_adds_no_words_keeps_todays_receipt():
    d = _day(3)
    rid, _ = _write("看牙醫順便洗牙", d, 14, "14:00 看牙醫順便洗牙")

    same, outcome = _write("看牙醫", d, 14, "14:00 看牙醫")

    assert (same, outcome) == (rid, "merged") and "看牙醫" in _detail_actions(rid)
    assert _receipt_for(outcome, rid, "看牙醫", _at(d, 14)) == (
        f"已更新既有提醒，未重複新增\n時間：{d.isoformat()} 14:00\n事項：成員甲 看牙醫順便洗牙"
    )


def test_a_repeated_fuller_mention_still_shows_its_words():
    # 「提醒已存在」 names the same reminder: it says the same as the push.
    d = _day(3)
    rid, _ = _write("繳電費", d, 14, "14:00 繳電費")
    assert _write("繳電費和瓦斯費", d, 14, "14:00 繳電費和瓦斯費") == (rid, "merged")

    assert _write("繳電費和瓦斯費", d, 14, "14:00 繳電費和瓦斯費") == (rid, "duplicate")

    assert _receipt_for("duplicate", rid, "繳電費和瓦斯費", _at(d, 14)) == (
        f"提醒已存在，未重複新增\n時間：{d.isoformat()} 14:00\n"
        "事項：成員甲 繳電費\n細節：繳電費和瓦斯費"
    )


def _dinner_result(action: str, day) -> dict:
    """What the reminder model returns for 「明天晚上6點…」 (made-up content)."""
    return {
        "action": action, "mention_aliases": [], "year": day.year, "month": day.month,
        "day": day.day, "hour": 18, "minute": 0, "_time_was_defaulted": False,
        "_time_default_kind": None,
    }


def test_a_fuller_rewording_only_the_fold_pairs_gets_its_own_row(monkeypatch):
    # Only the reverse reading pairs these (the new message names the older
    # 晚宴, e3).  The write-time reverse branch merged the mention into the
    # older row, so the receipt said 「事項：海景餐廳晚宴」.  Now the mention
    # gets its own row, its receipt carries no stage of the older row, and the
    # next moment folds the older row into the fuller one.
    batches = _fake_line(monkeypatch)
    calls = _record_pushes(monkeypatch)
    tomorrow = _day(1)
    first = main._maybe_extract_reminder(
        "提醒我明天晚上6點海景餐廳晚宴", G, "U_A", "m-older",
        precomputed_result=_dinner_result("海景餐廳晚宴", tomorrow))
    (older,) = first.reminder_ids              # never delivered: its 1-day notice is owed

    receipt = main._maybe_extract_reminder(
        "晚宴改成明天晚上6點海景餐廳聚會和慶生", G, "U_A", "m-fuller",
        precomputed_result=_dinner_result("海景餐廳聚會和慶生", tomorrow))

    assert receipt.startswith("已新增提醒") and "事項：成員甲 海景餐廳聚會和慶生" in receipt
    (fuller,) = receipt.reminder_ids
    assert fuller != older and _status(older) == "pending"
    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )
    assert _texts(batches[0]) == [str(receipt)]        # the older row's notice waits
    assert _row(older)["pushed_1d"] == 0 and _claims() == 0
    main._consume_receipt_open_stages(receipt, G)

    # The next moment folds the pair into the fuller row.  The receipt was the
    # 1-day notice, so nothing more goes out now.
    _set_now(monkeypatch, int(time.time()) + 15 * 60)
    assert reminder_push.push_reminders() == 0 and calls == []
    assert _status(older) == "cancelled" and _status(fuller) == "pending"
    assert _row(fuller)["action"] == "海景餐廳聚會和慶生"
    assert "海景餐廳晚宴" in _detail_actions(fuller)
    # The 4-hour notice: one message, in the fuller words.
    _set_now(monkeypatch, _at(tomorrow, 14, 5))
    assert reminder_push.push_reminders() == 1
    assert len(calls) == 1 and calls[0]["reminder_id"] == fuller
    assert "18:00 成員甲 海景餐廳聚會和慶生" in calls[0]["text"] and _claims() == 0


# What a later mention never does: change the reminder's wording, whatever it
# adds, drops, labels or @mentions.


def test_replaying_either_mention_after_the_fuller_one_adds_nothing():
    d = _day(3)
    rid, _ = _write("繳電費", d, 14, "14:00 繳電費")
    assert _write("繳電費和瓦斯費", d, 14, "14:00 繳電費和瓦斯費") == (rid, "merged")
    before = _row(rid)

    assert _write("繳電費和瓦斯費", d, 14, "14:00 繳電費和瓦斯費") == (rid, "duplicate")
    assert _write("繳電費", d, 14, "14:00 繳電費") == (rid, "duplicate")

    assert _row(rid) == before and len(_all_reminder_rows()) == 1


def test_an_even_fuller_mention_is_the_one_detail_shown():
    d = _day(3)
    rid, _ = _write("繳電費", d, 14, "14:00 繳電費")
    _write("繳電費和瓦斯費", d, 14, "14:00 繳電費和瓦斯費")

    assert _write("繳電費和瓦斯費及水費", d, 14, "14:00 繳電費和瓦斯費及水費") == (rid, "merged")

    assert _row(rid)["action"] == "繳電費"
    assert {"繳電費和瓦斯費", "繳電費和瓦斯費及水費"} <= set(_detail_actions(rid))
    assert _receipt_for("merged", rid, "繳電費", _at(d, 14)).endswith(
        "事項：成員甲 繳電費\n細節：繳電費和瓦斯費及水費")


@pytest.mark.parametrize(
    ("kept", "mention"),
    [
        ("看牙醫", "看牙醫順便帶成員乙洗牙"),          # the new words name another member
        ("看牙醫", "看牙醫順便洗牙（前一天提醒）"),     # a 前一天 label, not a fuller wording
        ("台大醫院看牙醫", "看牙醫順便洗牙"),          # it would drop the place
    ],
)
def test_a_mention_that_is_not_just_fuller_keeps_the_reminders_wording(kept, mention):
    d = _day(3)
    rid, _ = _write(kept, d, 14, f"14:00 {kept}")

    assert _write(mention, d, 14, f"14:00 {mention}") == (rid, "merged")

    assert _row(rid)["action"] == kept and mention in _detail_actions(rid)


def test_a_vaguer_fuller_mention_keeps_the_precise_reminders_wording():
    d = _day(3)
    rid, _ = _write("打流感疫苗", d, 14, "14:00 打流感疫苗")

    assert _write("施打流感疫苗和新冠疫苗", d, 12, "施打流感疫苗和新冠疫苗", kind="none") == (
        rid, "merged")

    assert _row(rid)["action"] == "打流感疫苗"
    assert "施打流感疫苗和新冠疫苗" in _detail_actions(rid)


# GP1 r4 #1: a fuller mention of an event that also has a calendar entry.  The
# calendar notice pairs with the reminder by its wording, so with the wording
# unchanged the event still yields one message per moment.


def test_a_fuller_mention_beside_an_all_day_event_is_one_message(monkeypatch):
    # GP1 r4 probe: natural 看牙醫 14:00, an all-day event 看牙醫 tomorrow, then
    # 「提醒我明天下午2點看牙醫順便洗牙」.  r4 replied with the receipt and the
    # event's own 🔔 in one reply and marked the event's 1-day notice.
    import calendar_db

    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    tomorrow = _day(1)
    rid, outcome = memory.add_reminder_with_outcome(
        G, "U_A", "看牙醫", _at(tomorrow, 14), source_text="明天下午兩點看牙醫",
        time_kind="clock",
    )
    assert outcome == "created"
    event_id = calendar_db.insert_event(
        group_id=G, title="看牙醫", event_date=tomorrow.isoformat(),
    )
    assert event_id

    main._handle_text_message(
        _text_event("提醒我明天下午2點看牙醫順便洗牙", message_id="m-fuller"), G
    )

    assert len(batches) == 1
    texts = _texts(batches[0])
    assert len(texts) == 1, texts
    assert texts[0].startswith("已更新既有提醒")
    assert "事項：成員甲 看牙醫\n細節：看牙醫順便洗牙" in texts[0]
    assert not any("🔔" in t for t in texts)
    assert _row(rid)["action"] == "看牙醫"
    with memory._conn() as c:                      # the event's own mirror row aside
        assert c.execute(
            "SELECT COUNT(*) FROM reminders WHERE group_id=? AND source_kind=''", (G,)
        ).fetchone()[0] == 1
    assert _reminded(event_id, 1) is None          # its 🔔 was not sent with the receipt
    assert _claims() == 0


def _reminder_and_card_mirror(day) -> tuple[int, int]:
    """看牙醫 at 14:00, a calendar event 「看牙醫（記得帶健保卡）」 at 14:00,
    then the fuller 「看牙醫順便洗牙」 merged into the reminder.

    The calendar pairing reads the title against the reminder's wording: it
    matches 看牙醫, but shares under half of 看牙醫順便洗牙, so a reminder
    reworded that way would read as another event.
    """
    import calendar_db

    rid, outcome = _write("看牙醫", day, 14, "14:00 看牙醫")
    assert outcome == "created"
    event_id = calendar_db.insert_event(
        group_id=G, title="看牙醫（記得帶健保卡）", event_date=day.isoformat(),
        event_time="14:00",
    )
    assert event_id
    assert _write("看牙醫順便洗牙", day, 14, "14:00 看牙醫順便洗牙") == (rid, "merged")
    return rid, int(_mirror_for(event_id)["reminder_id"])


def test_a_fuller_mention_keeps_the_mirror_riding_on_the_reminders_push(monkeypatch):
    # GP1 r4 #1, mirror-rider variant: both owe their 1-hour notice.
    # r4: two 「⏰ 提醒（1 小時後）」 at one moment (看牙醫順便洗牙 / 看牙醫（記得帶健保卡）).
    d = _day(3)
    natural, mirror = _reminder_and_card_mirror(d)
    calls = _record_pushes(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))

    assert reminder_push.push_reminders() == 1

    assert len(calls) == 1 and calls[0]["reminder_id"] == natural
    assert "（1 小時後）" in calls[0]["text"]
    assert calls[0]["text"].endswith(f"{d.isoformat()} 14:00 成員甲 看牙醫\n細節：看牙醫順便洗牙")
    assert _row(natural)["pushed_1hr"] == 1 and _row(mirror)["pushed_1hr"] == 1
    assert _status(mirror) == "pending" and _claims() == 0
    assert _row(natural)["action"] == "看牙醫"


def test_a_fuller_mentions_receipt_keeps_the_mirror_out_of_the_reply(monkeypatch):
    # r4: the receipt and the mirror's own 「⏰ 提醒（1 小時後）」 in one reply.
    d = _day(5)                      # no calendar offset is due five days out
    natural, mirror = _reminder_and_card_mirror(d)
    batches = _fake_line(monkeypatch)
    _set_now(monkeypatch, _at(d, 13))              # both owe their 1-hour notice
    receipt = main.ReminderReceipt(
        _receipt_for("merged", natural, "看牙醫順便洗牙", _at(d, 14)), (natural,), (natural,))

    assert main._reply(
        "receipt-token", receipt, group_id=G, allow_push_fallback=False,
        primary_reminder_ref=main._receipt_reply_ref(receipt),
    )

    assert _texts(batches[0]) == [str(receipt)]
    assert _row(natural)["pushed_1hr"] == 0 and _row(mirror)["pushed_1hr"] == 0
    assert _claims() == 0
    assert "事項：成員甲 看牙醫\n細節：看牙醫順便洗牙" in receipt


# GP1 r3 #4: a calendar correction reply is not the corrected event's notice
# and never carries that event's own 🔔 (same rule as the quoted correction).


def test_a_calendar_correction_reply_never_carries_the_events_own_notice(monkeypatch):
    import calendar_db

    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    event_id = calendar_db.insert_event(
        group_id=G, title="全家聚餐", event_date=_day(2).isoformat(), event_time="19:00",
    )
    text = "全家聚餐改到明天晚上7點"

    assert main._try_handle_calendar_correction(
        _text_event(text, message_id="m-fix"), G, text
    )

    assert len(batches) == 1
    texts = _texts(batches[0])
    assert len(texts) == 1, texts
    assert texts[0].startswith("已更正：") and "全家聚餐" in texts[0]
    event = calendar_db.get_active_event_by_id(G, event_id)
    assert event["event_date"] == _day(1).isoformat() and event["event_time"] == "19:00"
    assert _reminded(event_id, 1) is None        # its 🔔 still goes out later
    assert _claims() == 0


# GP2 r2/r3: a receipt the outbound validator blanked was never seen.  The
# reply may still carry other piggyback items and LINE may accept it, but
# nothing the receipt names is marked as announced (stages, event offsets).


def _blank_receipts(monkeypatch) -> None:
    original = main._prepare_outbound_text

    def blank(text, *args, **kwargs):
        if str(text or "").lstrip().startswith(("已新增", "提醒已存在", "已更新既有提醒")):
            return ""
        return original(text, *args, **kwargs)

    monkeypatch.setattr(main, "_prepare_outbound_text", blank)


def _other_event_due_tomorrow() -> str:
    import calendar_db

    other = calendar_db.insert_event(
        group_id=G, title="繳房屋稅", event_date=_day(1).isoformat(), event_time="10:00",
    )
    assert other
    return other


def _fresh_flags(at: int) -> tuple:
    """Stage flags of a row written at ``at`` that no receipt consumed."""
    control, outcome = memory.add_reminder_with_outcome(
        G, "U_B", "繳電話費", at, source_text="繳電話費", time_kind="clock",
    )
    assert outcome == "created"
    return _stage_flags(control)


def _batch_without_receipt(batches: list[list]) -> list[str]:
    assert len(batches) == 1
    texts = _texts(batches[0])
    assert not any(t.startswith(("已新增", "提醒已存在")) for t in texts)   # blanked
    assert any("🔔" in t and "繳房屋稅" in t for t in texts)          # still sent
    return texts


def test_a_blanked_receipt_consumes_no_stage(monkeypatch):
    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    _blank_receipts(monkeypatch)
    other = _other_event_due_tomorrow()
    at = _at(_day(2), 14)

    main._handle_text_message(
        _text_event("提醒我後天下午兩點看牙醫", message_id="m-blank"), G
    )

    _batch_without_receipt(batches)
    with memory._conn() as c:
        (rid,) = [
            row[0]
            for row in c.execute(
                "SELECT reminder_id FROM reminders WHERE group_id=? AND action LIKE '%看牙醫%'",
                (G,),
            ).fetchall()
        ]
    assert _row(rid)["remind_at"] == at
    assert _stage_flags(rid) == _fresh_flags(at)
    assert _reminded(other, 1) is not None        # what did go out is marked
    assert _claims() == 0


def test_a_blanked_calendar_capture_receipt_marks_no_offset(monkeypatch):
    import calendar_db

    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    _blank_receipts(monkeypatch)
    other = _other_event_due_tomorrow()

    main._handle_text_message(_text_event("明天晚上6點全家去餐廳聚餐", message_id="m-cal"), G)

    _batch_without_receipt(batches)
    (event,) = calendar_db.find_active_events_by_source_message(G, "m-cal")
    assert _reminded(event["event_id"], 1) is None
    mirror = _mirror_for(event["event_id"])
    assert _stage_flags(int(mirror["reminder_id"])) == _fresh_flags(int(mirror["remind_at"]))
    assert _reminded(other, 1) is not None
    assert _claims() == 0


def test_a_blanked_quoted_capture_receipt_consumes_no_stage(monkeypatch):
    _route_quietly(monkeypatch)
    batches = _fake_line(monkeypatch)
    _blank_receipts(monkeypatch)
    other = _other_event_due_tomorrow()
    text = _two_line_schedule()
    _seed_raw("m-src", "U_B", text)

    main._handle_text_message(_text_event("咪寶", message_id="m-cmd", quoted="m-src"), G)

    _batch_without_receipt(batches)
    with memory._conn() as c:
        rows = c.execute(
            "SELECT reminder_id, remind_at FROM reminders WHERE group_id=? "
            "AND source_kind='schedule_line'",
            (G,),
        ).fetchall()
    assert len(rows) == 2
    for rid, at in rows:
        assert _stage_flags(int(rid)) == _fresh_flags(int(at))
    assert _reminded(other, 1) is not None
    assert _claims() == 0


# ── viewing the reminder list never revives or resets a calendar mirror ─────
# Pre-existing bug reported 2026-10-05: the list view re-ran the full mirror
# sync, which set status='pending' and zeroed every stage flag, so a stage
# already pushed inside its window went out again (Andrew: one reminder is
# never pushed twice).  The view now only adds mirrors that are missing.


def _set_mirror(rid: int, **values) -> None:
    with memory._conn() as c:
        for column, value in values.items():
            c.execute(f"UPDATE reminders SET {column}=? WHERE reminder_id=?", (value, rid))


def test_viewing_the_reminder_list_keeps_a_mirrors_sent_stages():
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id=G, title="全家聚餐", event_date=_day(3).isoformat(), event_time="19:00",
    )
    rid = int(_mirror_for(event_id)["reminder_id"])
    _set_mirror(rid, pushed_4hr=1, pushed_1hr=1, last_pushed_at=123)

    main._build_todo_status_reply(G, "提醒清單")

    after = _row(rid)
    assert (after["pushed_4hr"], after["pushed_1hr"], after["last_pushed_at"]) == (1, 1, 123)
    assert after["status"] == "pending"


def test_viewing_the_reminder_list_keeps_a_done_mirror_done():
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id=G, title="打羽球", event_date=_day(2).isoformat(), event_time="08:00",
    )
    rid = int(_mirror_for(event_id)["reminder_id"])
    _set_mirror(rid, status="done", pushed_now=1)

    main._build_todo_status_reply(G, "提醒清單")

    after = _row(rid)
    assert (after["status"], after["pushed_now"]) == ("done", 1)


def test_viewing_the_reminder_list_still_adds_a_missing_mirror():
    import calendar_db

    event_id = calendar_db.insert_event(
        group_id=G, title="回診", event_date=_day(5).isoformat(), event_time="10:00",
    )
    with memory._conn() as c:
        c.execute(
            "DELETE FROM reminders WHERE group_id=? AND source_kind='calendar_event' "
            "AND source_ref=?",
            (G, event_id),
        )

    main._build_todo_status_reply(G, "提醒清單")

    assert _mirror_for(event_id)["status"] == "pending"
