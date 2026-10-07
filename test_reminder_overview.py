"""The reminder list shows each event once (Andrew 2026-10-07).

「透過命令叫出的提醒事項，我不要重複，你可能把同一件事提醒三次當成三筆，
不對，我只要一筆，我只要知道有哪些代辦事項，這樣就好，多餘的不要」.
All content is made up; dates are relative to today so the tests never expire.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import calendar_db
import main
import memory
import reminder_cancel as rc
import reminder_intent as ri
import reminder_overview as ro

TZ = ZoneInfo("Asia/Taipei")
G = "G_OVERVIEW"


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


_next_id = iter(range(1, 10_000))


def _row(
    action: str,
    remind_at: int,
    *,
    user: str = "U_A",
    mentions: tuple[str, ...] = (),
    kind: str | None = "clock",
    source_kind: str = "",
    source_ref: str = "",
    source_text: str = "",
) -> dict:
    """A row as memory.list_pending_reminders returns it."""
    return {
        "reminder_id": next(_next_id),
        "group_id": G,
        "user_id": user,
        "action": action,
        "remind_at": remind_at,
        "created_at": 0,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_text": source_text,
        "mention_aliases": list(mentions),
        "time_kind": kind,
        "merged_details": [],
    }


def _contextual_pair(day, title: str, clock: str = "15:00", actor: str = "成員甲") -> list[dict]:
    """The 前一天／當天 pair main._contextual_date_reminder_plan writes."""
    base = f"{actor} {_md(day)} {clock} {title}"
    return [
        _row(
            f"{base}（前一天提醒）",
            _at(day - timedelta(days=1), 9),
            mentions=(actor,),
            kind="daypart:早上",
            source_kind="contextual_date_once",
            source_ref="M1:lead:0",
        ),
        _row(
            f"{base}（當天提醒）",
            _at(day, 9),
            mentions=(actor,),
            kind="daypart:早上",
            source_kind="contextual_date_once",
            source_ref="M1:same:0",
        ),
    ]


def _mirror(day, title: str, clock: str = "15:00", people: tuple[str, ...] = ("成員甲",)) -> dict:
    hour, minute = (int(part) for part in clock.split(":"))
    return _row(
        title,
        _at(day, hour, minute),
        mentions=people,
        kind=None,
        source_kind="calendar_event",
        source_ref="E1",
        source_text=f"{title}；時間：{clock}",
    )


# ── offset labels ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action, days", [
    ("繳學費（前一天提醒）", 1),
    ("繳學費(當天提醒)", 0),
    ("繳學費（前3天提醒）", 3),
    ("繳學費（前三天提醒）", 3),
    ("繳學費（提前提醒）", None),
    ("繳學費", None),
])
def test_offset_days_come_from_the_label(action, days):
    assert ri.reminder_offset_days(action) == days


def test_strip_offset_marker_keeps_the_event_words():
    assert ri.strip_reminder_offset_marker("繳學費（前一天提醒）") == "繳學費"
    assert ri.strip_reminder_offset_marker("成員甲 9/9 15:00 家長會（當天提醒）") == (
        "成員甲 9/9 15:00 家長會"
    )
    assert ri.strip_reminder_offset_marker("繳學費") == "繳學費"


# ── grouping ────────────────────────────────────────────────────────────────


def test_contextual_pair_and_calendar_mirror_are_one_entry():
    day = _day(5)
    entries = ro.build_entries([*_contextual_pair(day, "家長會"), _mirror(day, "家長會")])

    assert len(entries) == 1
    entry = entries[0]
    assert (entry.event_date, entry.clock, entry.action, entry.people) == (
        day, "15:00", "家長會", ("成員甲",)
    )
    assert entry.rows[0]["source_kind"] == "calendar_event"  # the row asked for as such
    assert len(entry.rows) == 3


def test_labelled_rows_a_plain_reminder_and_a_todo_are_one_entry():
    day = _day(6)
    rows = [
        _row("繳學費（前一天提醒）", _at(day - timedelta(days=1), 20)),
        _row("繳學費（當天提醒）", _at(day, 8)),
        _row("繳學費", _at(day, 9), kind="none"),
    ]
    todos = [ro.todo_item("繳學費", day.isoformat(), owner="成員甲", user_id="U_A")]

    entries = ro.build_entries(rows, todos)

    assert len(entries) == 1
    assert entries[0].action == "繳學費"
    assert entries[0].clock is None  # nobody gave a time
    assert (len(entries[0].rows), len(entries[0].todos)) == (3, 1)


def test_contextual_pair_alone_shows_the_event_day_and_clock_from_its_words():
    day = _day(7)
    entries = ro.build_entries(_contextual_pair(day, "家長會", clock="10:30"))

    assert [(e.event_date, e.clock, e.action, e.people) for e in entries] == [
        (day, "10:30", "家長會", ("成員甲",))
    ]


def test_everyone_event_and_a_plain_reminder_at_its_time_are_one_entry():
    day = _day(8)
    entries = ro.build_entries([
        _mirror(day, "家族烤肉", clock="18:00", people=("全家",)),
        _row("烤肉", _at(day, 18), user="U_B"),
    ])

    assert len(entries) == 1
    assert entries[0].action == "家族烤肉"


def test_a_task_before_the_event_stays_its_own_entry():
    day = _day(8)
    entries = ro.build_entries([
        _row("買菜", _at(day, 9)),
        _row("買菜前先列清單", _at(day, 9)),
    ])

    assert len(entries) == 2


def test_two_occurrences_the_same_day_stay_two():
    day = _day(3)
    entries = ro.build_entries([
        _row("吃藥", _at(day, 9), mentions=("成員甲",)),
        _row("吃藥", _at(day, 21), mentions=("成員甲",)),
    ])

    assert [entry.clock for entry in entries] == ["09:00", "21:00"]


def test_an_untimed_mention_that_fits_two_occurrences_joins_neither():
    day = _day(3)
    entries = ro.build_entries([
        _row("吃藥", _at(day, 9), mentions=("成員甲",)),
        _row("吃藥", _at(day, 21), mentions=("成員甲",)),
        _row("吃藥", _at(day, 9), mentions=("成員甲",), kind="none"),
    ])

    # guessing could hide a dose; the untimed one stays on its own
    assert [entry.clock for entry in entries] == [None, "09:00", "21:00"]


def test_an_untimed_day_before_pair_joins_the_one_timed_event():
    day = _day(6)
    pair = [
        _row(f"成員甲 {_md(day)} 家長會（前一天提醒）", _at(day - timedelta(days=1), 9),
             mentions=("成員甲",), kind="daypart:早上",
             source_kind="contextual_date_once", source_ref="M2:lead:0"),
        _row(f"成員甲 {_md(day)} 家長會（當天提醒）", _at(day, 9),
             mentions=("成員甲",), kind="daypart:早上",
             source_kind="contextual_date_once", source_ref="M2:same:0"),
    ]
    entries = ro.build_entries([*pair, _mirror(day, "家長會", clock="19:00")])

    assert [(e.clock, e.action, len(e.rows)) for e in entries] == [("19:00", "家長會", 3)]


def test_the_same_words_for_different_people_stay_two():
    day = _day(3)
    entries = ro.build_entries([
        _mirror(day, "家長會", people=("成員甲",)),
        _row("家長會", _at(day, 15), user="U_B", mentions=("成員乙",)),
    ])

    assert len(entries) == 2


def test_two_contextual_appointments_on_two_days_stay_two():
    first, second = _day(4), _day(9)
    entries = ro.build_entries([
        *_contextual_pair(first, "家長會"),
        *_contextual_pair(second, "家長會"),
    ])

    assert [entry.event_date for entry in entries] == [first, second]


def test_two_contextual_appointments_the_same_day_at_two_times_stay_two():
    day = _day(4)
    entries = ro.build_entries([
        *_contextual_pair(day, "家長會", clock="09:00"),
        *_contextual_pair(day, "家長會", clock="15:00"),
    ])

    assert [entry.clock for entry in entries] == ["09:00", "15:00"]


# ── the list reply ───────────────────────────────────────────────────────────


def _insert(row: dict) -> int:
    with memory._conn() as c:
        cur = c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases, time_kind) "
            "VALUES (?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?, ?)",
            (
                G,
                row["user_id"],
                row["action"],
                row["remind_at"],
                row["source_kind"],
                row["source_ref"],
                row["source_text"] or row["action"],
                json.dumps(row["mention_aliases"], ensure_ascii=False),
                row["time_kind"],
            ),
        )
        return int(cur.lastrowid)


def _event_reminded_three_ways(day) -> None:
    event_id = calendar_db.insert_event(
        group_id=G, title="家長會", event_date=day.isoformat(), event_time="15:00",
        participants=["成員甲"],
    )
    assert event_id
    calendar_db.ensure_active_event_reminder_mirrors(G)
    for row in _contextual_pair(day, "家長會"):
        _insert(row)
    with memory._conn() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM reminders WHERE group_id=? AND status='pending'", (G,)
        ).fetchone()[0]
    assert count == 3


@pytest.mark.parametrize("command", ["/提醒清單", "/待辦", "/提醒事項", "提醒清單"])
def test_reminder_list_command_lists_an_event_once(command):
    day = _day(5)
    _event_reminded_three_ways(day)

    reply = main._build_todo_status_reply(G, command)

    weekday = "一二三四五六日"[day.weekday()]
    # 主詞放前面（2026-10-07）
    assert reply == f"目前待辦/提醒：\n1. {_md(day)}（{weekday}）15:00 成員甲 家長會"


def test_detail_query_still_shows_details_but_one_item_per_event():
    day = _day(5)
    _event_reminded_three_ways(day)

    reply = main._build_todo_status_reply(G, "提醒清單細節")

    assert "提醒事項：" in reply
    assert reply.count("事項：成員甲 家長會") == 1
    assert "參加人" not in reply and "@" not in reply
    assert "\n2. " not in reply
    assert "前一天提醒" not in reply and "當天提醒" not in reply


def test_reminder_list_for_a_day_includes_the_day_before_reminder_as_the_event():
    day = _day(5)
    _event_reminded_three_ways(day)

    reply = main._build_todo_status_reply(G, "提醒清單")
    assert reply.count("家長會") == 1


def test_todo_query_reply_pings_nobody_named_in_the_list(monkeypatch):
    sent = []

    class FakeApi:
        def __init__(self, _client):
            pass

        def reply_message(self, request):
            sent.extend(request.messages)

    class FakeClient:
        def __init__(self, _cfg):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        main, "_build_todo_status_reply",
        lambda *_a: "目前待辦/提醒：\n1. 9/9（三）18:00 家族烤肉（全家）",
    )
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)
    monkeypatch.setattr(main, "MessagingApi", FakeApi)
    monkeypatch.setattr(main, "ApiClient", FakeClient)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_mark_inbound_reply_succeeded", lambda *_a: None)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a, **_k: None)

    main._handle_todo_query(type("Event", (), {"reply_token": "T"})(), G, "提醒清單")

    assert len(sent) == 1
    assert sent[0].to_dict()["type"] == "text"  # a textV2 message would carry mentions


# ── 主詞放前面（Andrew 2026-10-07） ─────────────────────────────────────────


@pytest.mark.parametrize("text, people, expected", [
    ("家長會", ["成員甲"], "成員甲 家長會"),
    ("家長會", ["成員甲", "成員乙"], "成員甲、成員乙 家長會"),
    ("成員甲回診", ["成員甲"], "成員甲回診"),            # 已經寫在事項裡
    ("家族烤肉", ["全家"], "全家 家族烤肉"),
    ("全家打球", ["全家"], "全家打球"),
    ("家族烤肉", ["成員甲", "all"], "全家 家族烤肉"),      # 全家包含所有人
    ("繳學費", [], "繳學費"),
])
def test_subject_first(text, people, expected):
    assert ro.subject_first(text, people) == expected


def test_a_companion_already_in_the_wording_is_not_moved_up():
    day = _day(4)
    row = _row("成員甲回診（成員乙陪同）", _at(day, 14), mentions=("成員甲", "成員乙"))

    line = main._format_todo_overview_line(ro.build_entries([row])[0], 1)

    assert line.endswith("14:00 成員甲回診")


def _weekday(day) -> str:
    return "一二三四五六日"[day.weekday()]


@pytest.mark.parametrize("mentions", [("成員甲",), ("成員甲", "成員乙"), ("all",), ()])
def test_quoting_the_detail_list_still_cancels_the_reminder(mentions):
    day = _day(5)
    reminder_id = _insert(_row("家長會", _at(day, 15), mentions=mentions))
    detail = main._build_todo_status_reply(G, "提醒清單細節")

    request = rc.parse_cancel_request("取消", quoted_text=detail)
    resolution = rc.resolve_cancel_request(
        request, memory.list_reminder_cancellation_candidates(G)
    )

    assert resolution.status is rc.CancelResolutionStatus.MATCHED
    assert resolution.reminder_id == reminder_id


def test_the_name_in_front_does_not_bring_back_the_original_message():
    day = _day(5)
    _insert(_row("家長會", _at(day, 15), mentions=("成員甲",), source_text="下週五下午三點家長會"))

    detail = main._build_todo_status_reply(G, "提醒清單細節")

    assert "事項：成員甲 家長會" in detail
    assert "細節：" not in detail


def _resolve_quoted(day, shown: str, candidates: list[dict]):
    quoted = f"1. {_md(day)}（{_weekday(day)}）15:00\n事項：{shown}"
    return rc.resolve_cancel_request(rc.parse_cancel_request("取消", quoted_text=quoted), candidates)


def _candidate(reminder_id: int, action: str, at: int, mentions: list[str]) -> dict:
    return {"reminder_id": reminder_id, "action": action, "remind_at": at, "mention_aliases": mentions}


def test_the_name_in_front_tells_two_same_time_reminders_apart():
    day = _day(5)
    at = _at(day, 15)
    candidates = [
        _candidate(1, "家長會", at, ["成員甲"]),
        _candidate(2, "家長會", at, ["成員乙"]),
    ]

    resolution = _resolve_quoted(day, "成員乙 家長會", candidates)

    assert resolution.status is rc.CancelResolutionStatus.MATCHED
    assert resolution.reminder_id == 2


def test_one_list_item_for_two_reminders_still_asks_which():
    day = _day(5)
    at = _at(day, 15)
    candidates = [
        _candidate(1, "家長會", at, ["成員甲"]),
        _candidate(2, "家長會", at, ["成員甲", "成員乙"]),
    ]

    resolution = _resolve_quoted(day, "成員甲、成員乙 家長會", candidates)

    assert resolution.status is rc.CancelResolutionStatus.AMBIGUOUS


def test_words_in_front_that_are_not_people_are_not_dropped():
    day = _day(5)
    at = _at(day, 15)
    candidates = [_candidate(1, "牛奶", at, []), _candidate(2, "繳費", at, ["成員甲"])]

    assert _resolve_quoted(day, "買 牛奶", candidates).status is rc.CancelResolutionStatus.NOT_FOUND
    assert _resolve_quoted(day, "成員乙 繳費", candidates).status is rc.CancelResolutionStatus.NOT_FOUND
