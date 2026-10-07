"""Every reminder message puts the person first (Andrew 2026-10-07).

「提醒事項，主詞放前面」 then 「都改」: pushes, receipts, the calendar push and
list, and the daily todo push read 「成員甲 繳管理費」, and quoting any of them
to reschedule or cancel still finds the reminder.  All content is made up.
"""
from __future__ import annotations

import json

import pytest

import calendar_db
import event_reminder
import main
import memory
import reminder_push
import todo
from test_quoted_reminder_reschedule import (  # noqa: F401  (fixtures)
    _archive_bot,
    _event,
    _row,
    _ts,
    fixed_now,
    replies,
)

G = "G1"


@pytest.fixture(autouse=True)
def _synthetic_aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(
        json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))


def _seed(action: str, when: str, people: list[str]) -> int:
    reminder_id = memory.add_reminder(
        G, "U_A", action, _ts(when), source_text=action, mention_aliases=people
    )
    assert reminder_id is not None
    return int(reminder_id)


def _push_text(reminder_id: int) -> str:
    return reminder_push._format_push_text(memory.get_reminder(reminder_id), "1d")


# ── what each message says ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "people", "line"),
    [
        ("繳管理費", ["成員甲"], "2099-10-01 12:00 成員甲 繳管理費"),
        ("繳管理費", ["成員甲", "成員乙"], "2099-10-01 12:00 成員甲、成員乙 繳管理費"),
        ("繳管理費", ["all"], "2099-10-01 12:00 全家 繳管理費"),
        ("成員甲回診", ["成員甲"], "2099-10-01 12:00 成員甲回診"),
        ("繳管理費", [], "2099-10-01 12:00 繳管理費"),
    ],
)
def test_a_push_puts_the_people_first(action, people, line):
    text = _push_text(_seed(action, "2099-10-01 12:00", people))

    assert text.splitlines()[1] == line
    assert "參加人" not in text


def test_a_receipt_puts_the_people_first_and_pings_them_once():
    receipt = main._format_reminder_write_confirmation(
        "created", "繳管理費", main.datetime(2099, 10, 1, 12, 0), ["成員甲"]
    )

    assert receipt == "@成員甲\n已新增提醒\n時間：2099-10-01 12:00\n事項：成員甲 繳管理費"
    text, message = main._text_message_with_mentions(receipt, explicit_only=True)
    assert text == receipt
    assert message.text == "{p1}\n已新增提醒\n時間：2099-10-01 12:00\n事項：成員甲 繳管理費"
    assert message.substitution["p1"].mentionee.user_id == "U_A"


@pytest.mark.parametrize(
    ("people", "expected"),
    [
        (["哥哥"], "已新增提醒\n時間：2099-10-01 12:00\n事項：哥哥 繳管理費"),
        (["all"], "@all\n已新增提醒\n時間：2099-10-01 12:00\n事項：全家 繳管理費"),
        (["全家"], "已新增提醒\n時間：2099-10-01 12:00\n事項：全家 繳管理費"),
    ],
)
def test_a_receipt_pings_only_whom_the_old_line_pinged(people, expected):
    """哥哥 has no LINE account here and 「@全家」 never pinged; @all did."""
    receipt = main._format_reminder_write_confirmation(
        "created", "繳管理費", main.datetime(2099, 10, 1, 12, 0), people
    )

    assert receipt == expected


def test_other_replies_keep_their_mention_line():
    text, message = main._text_message_with_mentions("記得 @成員甲 繳費", explicit_only=True)

    assert text == "記得 @成員甲 繳費"
    assert message.text == "{p1}\n記得 @成員甲 繳費"


def test_the_calendar_push_and_list_put_the_people_first():
    event = {
        "event_id": "e1", "title": "家長會", "event_date": "2099-10-01",
        "event_time": "15:00", "location": "學校", "event_type": "family_gathering",
        "participants": json.dumps(["成員甲", "成員乙"], ensure_ascii=False),
    }

    push = event_reminder._format_event(event, 1)
    listed = main._format_calendar_event(event)

    assert "🎯 成員甲、成員乙 家長會\n📍 學校" in push
    assert listed == "🍽️ 2099-10-01 15:00 成員甲、成員乙 家長會\n📍 學校"
    assert "👥" not in push + listed


def test_the_daily_todo_push_names_the_owner_first():
    text = todo._format_reminder(
        [{"task": "繳學費", "sender_user_id": "U_A"}, {"task": "買菜", "sender_user_id": "U_X"}],
        [],
    )

    assert "- 成員甲 繳學費" in text
    assert "- 買菜" in text
    assert "[" not in text  # no more user id endings


# ── quoting them still finds the reminder ───────────────────────────────────


def test_a_bound_person_first_push_can_be_rescheduled(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00", ["成員甲"])
    _archive_bot("push-1", "@成員甲\n" + _push_text(rid), reminder_id=rid)

    main._handle_text_message(_event("時間：早上 9 點", quoted="push-1"), G)

    assert _row(rid)["remind_at"] == _ts("2099-10-01 09:00")
    assert "事項：成員甲 繳管理費" in replies[0][0]


def test_someone_added_since_the_push_does_not_make_it_stale(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00", ["成員甲"])
    _archive_bot("push-2", _push_text(rid), reminder_id=rid)
    with memory._conn() as conn:
        conn.execute(
            "UPDATE reminders SET mention_aliases=? WHERE reminder_id=?",
            (json.dumps(["成員甲", "成員乙"], ensure_ascii=False), rid),
        )

    main._handle_text_message(_event("時間：早上 9 點", quoted="push-2"), G)

    assert _row(rid)["remind_at"] == _ts("2099-10-01 09:00")
    assert "事項：成員甲、成員乙 繳管理費" in replies[0][0]


def test_an_unbound_person_first_receipt_can_be_rescheduled(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00", ["成員甲"])
    receipt = main._format_reminder_write_confirmation(
        "created", "繳管理費", main.datetime(2099, 10, 1, 12, 0), ["成員甲"]
    )
    _archive_bot("ack-1", receipt)

    main._handle_text_message(_event("10月8日", quoted="ack-1"), G)

    assert _row(rid)["remind_at"] == _ts("2099-10-08 12:00")
    assert replies[0][0].startswith("已更新提醒（2099-10-01 12:00 → 2099-10-08 12:00）")


def test_words_that_are_not_people_are_not_dropped(replies, fixed_now):
    rid = _seed("牛奶", "2099-10-01 12:00", [])
    _archive_bot("ack-3", "已新增提醒\n時間：2099-10-01 12:00\n事項：買 牛奶")

    main._handle_text_message(_event("10月8日", quoted="ack-3"), G)

    assert _row(rid)["remind_at"] == _ts("2099-10-01 12:00")
    assert replies[0][0].startswith("尚未更新提醒")


def test_a_person_first_receipt_can_be_cancelled(replies, fixed_now):
    rid = _seed("繳管理費", "2099-10-01 12:00", ["成員甲"])
    main._handle_text_message(
        _event("10/8", quoted=_archive_receipt_of(rid, "ack-4")), G
    )
    moved = replies[-1][0]
    assert "事項：成員甲 繳管理費" in moved
    _archive_bot("moved-4", moved)

    main._handle_text_message(_event("這則取消", quoted="moved-4"), G)

    assert _row(rid)["status"] == "cancelled"
    assert replies[-1][0].startswith("已取消提醒")
    assert replies[-1][0].endswith("事項：成員甲 繳管理費")


def _archive_receipt_of(reminder_id: int, message_id: str) -> str:
    row = memory.get_reminder(reminder_id)
    receipt = main._format_reminder_write_confirmation(
        "created",
        row["action"],
        main.datetime.fromtimestamp(row["remind_at"], main.ZoneInfo("Asia/Taipei")),
        row["mention_aliases"],
    )
    _archive_bot(message_id, receipt)
    return message_id


def test_a_pasted_person_first_calendar_push_finds_the_event():
    event_id = calendar_db.insert_event(
        group_id=G, title="家長會", event_date="2099-10-01", event_time="15:00",
        participants=["成員甲"],
    )
    assert event_id

    found = calendar_db.find_active_events_exact(
        G, title="成員甲 家長會", event_date="2099-10-01", event_time="15:00"
    )

    assert [event["event_id"] for event in found] == [event_id]
    assert calendar_db.find_active_events_exact(
        G, title="成員乙 家長會", event_date="2099-10-01", event_time="15:00"
    ) == []
