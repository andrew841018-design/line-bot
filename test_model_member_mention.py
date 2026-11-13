"""2026-11-13 (Andrew): a reply the model wrote pings nobody, not even one member.

He picked four things, one question at a time (the first three around midnight on 11/12-13, the last
at 18:4x on 11/13):
- 「聊天回覆一律不 @」: an 「@成員甲」 the model writes stays in the words and rings no phone.  A page,
  a video's text or an article can tell the model to write every member's name after an @.
- 「也拿掉」: a chat reply no longer pings the people the asker @'d in their own message.
- 「一起改成不響」: the to-do/reminder list and the calendar query ping nobody, even when an item's
  words hold an 「@成員甲」.
- 「回條照舊跟著 @」: a reminder receipt keeps pinging as it did, the asker's @s included.
Receipts are `line_mentions.AllowsMemberMention` where they are built (`ReminderReceipt` and polls are
`AllowsAllMention`, under it); due reminders and the exercise nudge ping whom they name as before.  The
whole-group half is test_model_all_mention.py.  All text is made up, and nothing here reaches the network.
"""

from __future__ import annotations

import ast
import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import calendar_db
import event_reminder
import family_poll
import line_mentions
import main
import memory
import pending_store
import reminder_push
import reminder_push_limit as rpl
import reminder_restatement
import test_quoted_reminder_reschedule as reschedule_tests
import test_reminder_cancel_integration as cancel_tests
import test_reminder_push_limit as push_limit_tests
from test_model_all_mention import (  # noqa: F401
    REAL_REPLY,
    _no_extraction_model,
    _no_network,
    poll,
    say,
    through_the_text_handler,
)
from test_poll_flow import G, _install_noop_side_modules, _make_text_event, poll_db  # noqa: F401
from test_research_verify_claims import ANSWER, ROW, research_env  # noqa: F401
from test_support_fake_line import BAD_TOKEN, GROUP, install_fake_line

MEMBERS = {"U_A": "成員甲", "U_B": "成員乙", "U_C": "成員丙"}

# What a model can be told to write.  Each one pings a member only when the program wrote it:
# test_each_spelling_really_pings_when_the_program_wrote_it proves that.
MODEL_WORDS = [
    "@成員甲 @成員乙 @成員丙 記得吃飯",
    "@成員甲 @成員乙 @成員丙\n記得吃飯",
    "記得跟 @成員乙 說晚餐不用煮",
    "＠成員甲 記得吃飯",
    "**@成員甲** 記得吃飯",
]
MODEL_WORD_IDS = ["one-line", "own-at-line", "inline", "full-width", "bold"]


@pytest.fixture(autouse=True)
def _synthetic_aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps(MEMBERS, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))


@pytest.fixture()
def line(monkeypatch):
    return install_fake_line(monkeypatch)


def pinged(message) -> set[str]:
    """The user ids a sent message mentions (a plain v1 TextMessage mentions nobody)."""
    found = set()
    for item in (getattr(message, "substitution", None) or {}).values():
        user_id = getattr(getattr(item, "mentionee", None), "user_id", None)
        if user_id:
            found.add(user_id)
    return found


def asker_mentioned(*user_ids: str, token: str = "TOKEN-ALL") -> None:
    """What the webhook (`_handle_event`) registers for the reply token when the asker's own message
    @s someone with LINE's mention."""
    text = " ".join(f"@{MEMBERS[uid]}" for uid in user_ids) + " 咪寶你看這個"
    mentionees, at = [], 0
    for uid in user_ids:
        length = len(MEMBERS[uid]) + 1
        mentionees.append({"userId": uid, "index": at, "length": length})
        at += length + 1
    message = SimpleNamespace(text=text, mention=SimpleNamespace(mentionees=mentionees))
    main._register_reply_mention_targets(token, message)


def test_the_synthetic_members_resolve():
    # Without this every "pings nobody" below could pass for nothing.
    assert [line_mentions.user_id_for_alias(name) for name in MEMBERS.values()] == list(MEMBERS)


# ── a reply the model wrote ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", MODEL_WORDS, ids=MODEL_WORD_IDS)
def test_a_model_reply_pings_no_member_and_keeps_its_words(line, text):
    assert say(text)

    [message] = line.replies[0]
    assert pinged(message) == set()
    assert message.text == main._prepare_outbound_text(text, source="reply")


@pytest.mark.parametrize("text", MODEL_WORDS, ids=MODEL_WORD_IDS)
def test_a_model_reply_sent_by_push_pings_no_member(line, text):
    line.script = [BAD_TOKEN]

    assert say(text)

    [(batch, _key)] = line.pushes
    assert pinged(batch[0]) == set()


def test_a_chat_reply_does_not_ping_again_whom_the_asker_mentioned(line):
    asker_mentioned("U_B")

    assert say("好的，我看看")

    [message] = line.replies[0]
    assert pinged(message) == set()
    assert "TOKEN-ALL" not in main._reply_mention_targets_by_token  # taken out, not left for later


def test_a_chat_reply_sent_by_push_does_not_ping_whom_the_asker_mentioned(line):
    asker_mentioned("U_B")
    line.script = [BAD_TOKEN]

    assert say("好的，我看看")

    [(batch, _key)] = line.pushes
    assert pinged(batch[0]) == set()


# ── the list and the calendar query ──────────────────────────────────────────────────────────


def test_the_todo_list_pings_no_member_named_in_an_item(line, monkeypatch):
    monkeypatch.setattr(main, "_build_todo_status_reply", lambda *_args: "目前待辦/提醒：\n1. @成員甲 回診")

    main._handle_todo_query(_make_text_event("/提醒清單"), GROUP, "/提醒清單")

    [message] = line.replies[0]
    assert pinged(message) == set()
    assert "@成員甲 回診" in message.text


def test_the_calendar_query_pings_no_member_named_in_an_event(line, monkeypatch):
    event = {
        "event_id": "e1",
        "group_id": GROUP,
        "title": "@成員甲 打壁球",
        "event_date": "2099-10-01",
        "event_time": "19:00",
        "location": "",
        "participants": "[]",
        "status": "active",
    }
    monkeypatch.setattr(calendar_db, "list_past", lambda *_a, **_k: [])
    monkeypatch.setattr(calendar_db, "list_upcoming", lambda *_a, **_k: [event])
    monkeypatch.setattr(event_reminder, "_format_event", lambda ev, offset: f"🎯 {ev['title']}")
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main.settings, "bot_muted", False, raising=False)

    main._handle_calendar_query(_make_text_event("最近有什麼安排"), GROUP, "最近有什麼安排")

    [message] = line.replies[0]
    assert "@成員甲 打壁球" in message.text
    assert pinged(message) == set()


# ── what the program wrote still pings ───────────────────────────────────────────────────────


def _receipt_words() -> str:
    return main._format_reminder_write_confirmation(
        "created", "回診", main.datetime(2099, 10, 1, 9, 0), ["成員甲"]
    )


def test_a_receipt_pings_the_member_it_names_and_the_same_words_as_a_plain_str_do_not(line):
    words = _receipt_words()
    assert words.startswith("@成員甲\n")

    assert say(main.ReminderReceipt(words))
    assert say(str(words))

    assert pinged(line.replies[0][0]) == {"U_A"}
    assert pinged(line.replies[1][0]) == set()


def test_a_receipt_still_pings_whom_the_asker_mentioned(line):
    # Andrew 2026-11-13 「回條照舊跟著 @」: a receipt keeps pinging whom the asker @'d.
    asker_mentioned("U_B")

    assert say(main.ReminderReceipt(_receipt_words()))

    assert pinged(line.replies[0][0]) == {"U_A", "U_B"}


def test_a_receipt_sent_by_push_still_pings(line):
    line.script = [BAD_TOKEN]

    assert say(main.ReminderReceipt(_receipt_words()))

    [(batch, _key)] = line.pushes
    assert pinged(batch[0]) == {"U_A"}


def test_a_reminder_asked_for_through_the_text_handler_pings_the_member(line, poll_db, monkeypatch):  # noqa: F811
    # This receipt has no @ line of its own: 成員甲 is pinged from the 事項 it reads back.  Whether a
    # 事項's @member should ping is the reminder-path stage 2's question for Andrew (review seat 2 N6).
    _install_noop_side_modules(monkeypatch)

    through_the_text_handler("提醒我明天10:00開會 @成員甲", "M-member-receipt")

    assert "U_A" in pinged(line.replies[0][0])


def test_a_hand_written_one_shot_still_pings_a_name(line, monkeypatch):
    monkeypatch.setattr(main, "_load_one_shot_replies", lambda: {GROUP: "成員甲 明天停水"})
    monkeypatch.setattr(main, "_save_one_shot_replies", lambda _data: None)

    assert main._try_one_shot_reply(_make_text_event("早安"), GROUP)

    assert pinged(line.replies[0][0]) == {"U_A"}


def test_the_exercise_nudge_still_pings_its_member():
    nudge = SimpleNamespace(text="@成員甲\n今天走路了嗎", user_id="U_A", label="@成員甲")

    assert pinged(main._exercise_nudge_message(nudge)) == {"U_A"}


# ── riders on a model reply: their mention is their own ──────────────────────────────────────


def _arm_a_due_reminder_for_a_member(monkeypatch, *, built: bool) -> None:
    row = {"action": "回診", "remind_at": 1_800_000_000, "user_id": "U_A", "mention_aliases": ["成員甲"]}
    text, message = reminder_push._build_push_text_and_message(row, "1d", now=1_799_900_000)
    item = {
        "reminder_id": 42,
        "group_id": GROUP,
        "stage": "1d",
        "text": text,
        "action": row["action"],
        "remind_at": row["remind_at"],
        "weekly_count": 0,
    }
    if built:
        item["message"] = message
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(reminder_push, "due_reminders_for_reply", lambda *_a, **_k: [item])
    monkeypatch.setattr(calendar_db, "list_due_for_reminder", lambda *_a, **_k: [])
    monkeypatch.setattr(memory, "is_reminder_pending", lambda *_a, **_k: True)
    monkeypatch.setattr(
        memory,
        "claim_natural_reminder_delivery",
        lambda *_a, **_k: {"reminder_id": 42, "claim_token": "c42"},
    )
    monkeypatch.setattr(memory, "finalize_natural_reminder_delivery", lambda _claim: True)


@pytest.mark.parametrize("built", [True, False], ids=["built", "read-back-as-text"])
def test_a_due_reminder_riding_on_a_model_reply_still_pings_its_member(line, monkeypatch, built):
    _arm_a_due_reminder_for_a_member(monkeypatch, built=built)

    assert say("@成員乙 好的")

    model_reply, reminder = line.replies[0]
    assert pinged(model_reply) == set()
    assert pinged(reminder) == {"U_A"}


def test_a_confirmation_riding_on_a_model_reply_still_pings_its_member(line):
    memory.enqueue_reminder_confirmation(GROUP, "pending_reminder:1", "@成員甲\n已新增提醒\n事項：回診")

    assert say("@成員乙 好的")

    model_reply, confirmation = line.replies[0]
    assert pinged(model_reply) == set()
    assert pinged(confirmation) == {"U_A"}


# ── what lets a member be pinged: the mark, nothing else ─────────────────────────────────────

MODEL_WORD_PINGS = [{"U_A", "U_B", "U_C"}, {"U_A", "U_B", "U_C"}, {"U_B"}, {"U_A"}, {"U_A"}]


@pytest.mark.parametrize(("text", "expected"), list(zip(MODEL_WORDS, MODEL_WORD_PINGS)), ids=MODEL_WORD_IDS)
def test_each_spelling_really_pings_when_the_program_wrote_it(line, text, expected):
    # Without this the 「pings nobody」 tests above could pass for nothing.
    assert say(line_mentions.AllowsMemberMention(text))

    assert pinged(line.replies[0][0]) == expected


@pytest.mark.parametrize("by_push", [False, True], ids=["reply", "push"])
def test_a_member_mark_pings_its_members_and_the_askers_but_never_the_group(line, by_push):
    asker_mentioned("U_B")
    if by_push:
        line.script = [BAD_TOKEN]

    assert say(line_mentions.AllowsMemberMention("@成員甲 @all\n已新增提醒\n事項：開會"))

    message = line.pushes[0][0][0] if by_push else line.replies[0][0]
    assert pinged(message) == {"U_A", "U_B"}
    assert "all" not in (getattr(message, "substitution", None) or {})


@pytest.mark.parametrize(
    ("text", "explicit_only", "expected"),
    [("@成員甲 開會", True, {"U_A"}), ("@成員甲 開會", False, {"U_A"}), ("成員甲 明天停水", False, {"U_A"})],
    ids=["explicit", "names-at", "names-bare"],
)
def test_the_builder_pings_no_member_unless_it_is_told(text, explicit_only, expected):
    assert pinged(main._text_message_with_mentions(text, explicit_only=explicit_only)[1]) == set()
    _words, told = main._text_message_with_mentions(text, explicit_only=explicit_only, allow_member_mention=True)
    assert pinged(told) == expected


def test_the_builder_takes_the_targets_it_is_handed_whatever_the_flag():
    # The exercise nudge pings its member this way, and is dropped when no mention comes out.
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_C", label="@成員丙")

    _words, message = main._text_message_with_mentions(
        "@成員丙\n今天走路了嗎 @成員甲", explicit_only=True, reply_targets=[target]
    )

    assert pinged(message) == {"U_C"}


@pytest.mark.parametrize("words", ["好的，我看看", "好的 @成員乙 我看看"], ids=["plain", "names-the-same-member"])
def test_a_chat_reply_pings_nobody_whom_the_asker_mentioned(line, words):
    asker_mentioned("U_B")

    assert say(words)

    assert pinged(line.replies[0][0]) == set()


def test_a_receipt_sent_by_push_still_pings_whom_the_asker_mentioned(line):
    # A unit-level check of `_reply`: a ReminderReceipt's own send sites never fall back to push.  Several
    # of the receipts marked where they are built can (review seat 4 N1): the [push] case of
    # test_a_member_mark_pings_its_members_and_the_askers_but_never_the_group covers those.
    asker_mentioned("U_B")
    line.script = [BAD_TOKEN]

    assert say(main.ReminderReceipt(_receipt_words()))

    [(batch, _key)] = line.pushes
    assert pinged(batch[0]) == {"U_A", "U_B"}


@pytest.mark.parametrize("by_push", [False, True], ids=["reply", "push"])
def test_a_poll_still_pings_whom_the_asker_mentioned(line, poll, by_push):  # noqa: F811
    # A poll really can go out by push (review seat 3 N3); since 2026-11-13 so can several of the receipts
    # marked where they are built (review seat 4 N1).
    asker_mentioned("U_B")
    if by_push:
        line.script = [BAD_TOKEN]

    assert say(family_poll.format_created_reply(poll))

    message = line.pushes[0][0][0] if by_push else line.replies[0][0]
    assert pinged(message) == {"U_B"}
    assert "all" in message.substitution


def test_the_exercise_nudge_pings_only_its_own_member():
    nudge = SimpleNamespace(text="@成員甲\n今天走路了嗎 @成員乙", user_id="U_A", label="@成員甲")

    assert pinged(main._exercise_nudge_message(nudge)) == {"U_A"}


# ── the receipts marked where they are built (each send helper also sends refusals) ────────


def test_a_corrected_event_with_details_reaches_reply_with_its_mark(line, monkeypatch):
    # Marked after its last change: the 「、」.join(details) line would have dropped an earlier mark.
    monkeypatch.setattr(main, "_parse_calendar_correction", lambda _text: {"keywords": ["開會"], "new_time": "10:00"})
    monkeypatch.setattr(
        main,
        "_find_calendar_correction_event",
        lambda *_a: {"event_id": "e1", "title": "@成員甲 開會", "event_date": "2099-10-01", "location": ""},
    )
    monkeypatch.setattr(main, "_calendar_correction_new_title", lambda *_a: None)
    monkeypatch.setattr(calendar_db, "correct_event_and_reminder_by_id", lambda *_a, **_k: {"status": "updated"})
    monkeypatch.setattr(main, "_labelled_turn", lambda _gid, text, **_k: text)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda *_a, **_k: [])
    asker_mentioned("U_B", token="TOKEN001")

    assert main._try_handle_calendar_correction(_make_text_event("改成10點"), GROUP, "改成10點")

    [message] = line.replies[0]
    assert "行事曆已更新" in message.text
    assert pinged(message) == {"U_A", "U_B"}


def _an_event_to_cancel(monkeypatch, *, cancelled=True):
    monkeypatch.setattr(
        calendar_db,
        "find_active_event",
        lambda *_a, **_k: {"event_id": "e1", "event_date": "2099-10-01", "title": "@成員甲 開會"},
    )
    monkeypatch.setattr(calendar_db, "cancel_event", lambda _event_id: cancelled)


def test_cancelling_an_event_by_command_marks_only_the_receipt(monkeypatch):
    # The same `_reply` call sends every command's answer, /檢討 (a model's words) among them (review seat 4 I2).
    _an_event_to_cancel(monkeypatch)
    assert isinstance(main._handle_command(GROUP, "/取消活動 開會", "U_A", "M-cancel"), line_mentions.AllowsMemberMention)

    _an_event_to_cancel(monkeypatch, cancelled=False)
    assert not isinstance(main._cancel_calendar_event(GROUP, "開會"), line_mentions.AllowsMemberMention)
    monkeypatch.setattr(calendar_db, "find_active_event", lambda *_a, **_k: None)
    assert not isinstance(main._cancel_calendar_event(GROUP, "@成員甲"), line_mentions.AllowsMemberMention)


def test_cancelling_an_event_through_the_text_handler_pings_as_it_did(line, poll_db, monkeypatch):  # noqa: F811
    _install_noop_side_modules(monkeypatch)
    _an_event_to_cancel(monkeypatch)
    asker_mentioned("U_B", token="TOKEN001")

    through_the_text_handler("/取消活動 開會", "M-cancel-event")

    assert pinged(line.replies[0][0]) == {"U_A", "U_B"}


def test_a_command_answering_in_the_models_words_pings_no_member(line, monkeypatch):
    _install_noop_side_modules(monkeypatch)
    asker_mentioned("U_C", token="TOKEN001")

    with patch("main.review.run_weekly_review", return_value=("@成員甲 @成員乙 這週要檢討", [])):
        through_the_text_handler("/檢討", "M-model-command")

    assert "@成員甲" in line.replies[0][0].text
    assert pinged(line.replies[0][0]) == set()


def test_the_list_command_pings_no_member_named_in_an_item(line, monkeypatch):
    # /提醒清單 reaches `_reply` through `_handle_command` (review seat 3 N7).
    _install_noop_side_modules(monkeypatch)
    monkeypatch.setattr(main, "_build_todo_status_reply", lambda *_args: "目前待辦/提醒：\n1. @成員甲 回診")
    asker_mentioned("U_B", token="TOKEN001")

    through_the_text_handler("/提醒清單", "M-list-command")

    assert "@成員甲 回診" in line.replies[0][0].text
    assert pinged(line.replies[0][0]) == set()


def test_a_refusal_from_a_receipts_send_helper_pings_nobody(line, monkeypatch):
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda *_a, **_k: [])
    asker_mentioned("U_B", token="TOKEN001")

    main._send_reschedule_reply(
        _make_text_event("改到 8 點"), GROUP, "MSG001", "目前只能改日期或時間 @成員甲", None, "unsupported"
    )

    assert pinged(line.replies[0][0]) == set()


# ── one `_reply` sends a receipt and the refusals next to it (post-review seat 2 I2) ─────────────
# Each receipt is marked on the line that builds it.  Marked on the shared `_reply` instead, the pin
# table would count the same, and a refusal would ping whom the asker @'d.  The receipt twin shows the
# asker's @ is there to be pinged, so the refusal's 「nobody」 is not for nothing.


def _a_calendar_correction(monkeypatch, *, found: bool) -> None:
    event = {"event_id": "e1", "title": "@成員甲 開會", "event_date": "2099-10-01", "location": ""}
    monkeypatch.setattr(main, "_parse_calendar_correction", lambda _text: {"keywords": ["@成員甲 開會"], "new_time": "10:00"})
    monkeypatch.setattr(main, "_find_calendar_correction_event", lambda *_a: event if found else None)
    monkeypatch.setattr(main, "_calendar_correction_new_title", lambda *_a: None)
    monkeypatch.setattr(calendar_db, "correct_event_and_reminder_by_id", lambda *_a, **_k: {"status": "updated"})
    monkeypatch.setattr(main, "_update_calendar_correction_reminders", lambda **_k: 0)
    monkeypatch.setattr(main, "_labelled_turn", lambda _gid, text, **_k: text)
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda *_a, **_k: [])


@pytest.mark.parametrize(
    ("found", "words", "expected"),
    [(True, "已更正：", {"U_A", "U_B"}), (False, "有收到更正，但找不到", set())],
    ids=["receipt", "not-found"],
)
def test_the_correction_send_pings_for_its_receipt_only(line, monkeypatch, found, words, expected):
    _a_calendar_correction(monkeypatch, found=found)
    asker_mentioned("U_B", token="TOKEN001")

    assert main._try_handle_calendar_correction(_make_text_event("改成10點"), GROUP, "改成10點")

    [message] = line.replies[0]
    assert words in message.text and "@成員甲 開會" in message.text  # a receipt puts the asker's @s in front
    assert pinged(message) == expected


@pytest.mark.parametrize(
    ("result", "words", "expected"),
    [
        (
            {"status": "updated", "event": {"event_date": "2099-10-01", "event_time": "10:00", "title": "@成員甲 開會"}},
            "已更正行程與提醒",
            {"U_A", "U_B"},
        ),
        ({"status": "not_found"}, "在這個群組找不到引用訊息對應的行程", set()),
    ],
    ids=["receipt", "not-found"],
)
def test_the_quoted_correction_send_pings_for_its_receipt_only(line, monkeypatch, result, words, expected):
    monkeypatch.setattr(main, "_parse_quoted_calendar_correction", lambda _text: {"status": "ok", "new_time": "10:00"})
    monkeypatch.setattr(calendar_db, "correct_quoted_event_and_reminder", lambda *_a, **_k: result)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda *_a, **_k: [])
    event = _make_text_event("改成10點")
    event.message.quoted_message_id = "M-quoted-event"
    asker_mentioned("U_B", token="TOKEN001")

    assert main._try_handle_quoted_calendar_correction(event, GROUP, "改成10點")

    [message] = line.replies[0]
    assert words in message.text
    assert pinged(message) == expected


def _the_text_handler_reaches_the_restatement(monkeypatch, outcome: dict) -> None:
    """Post-review seat 1's P1: every route before the restatement steps aside, and it answers `outcome`."""
    _install_noop_side_modules(monkeypatch)
    monkeypatch.setattr(reminder_restatement, "correction", lambda *_a, **_k: outcome)
    monkeypatch.setattr(
        memory,
        "get_reminder",
        lambda _rid: {"reminder_id": 7, "remind_at": 4_070_000_000, "action": "@成員甲 回診", "user_id": "U_C"},
    )
    for name in (
        "_maybe_reply_parent_red_flag",
        "_quoted_bot_health_card",
        "_try_handle_reminder_cancellation",
        "_try_handle_reminder_push_limit",
        "_try_handle_quoted_reminder_reschedule",
    ):
        monkeypatch.setattr(main, name, lambda *_a, **_k: False)
    monkeypatch.setattr(main, "_quoted_bot_investment_kind", lambda *_a, **_k: None)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda *_a, **_k: [])


@pytest.mark.parametrize(
    ("outcome", "words", "expected"),
    [
        ({"status": "updated", "reminder_id": 7}, "已更新提醒", {"U_A", "U_B"}),
        ({"status": "unavailable"}, "尚未更新提醒：", set()),
    ],
    ids=["receipt", "not-updated"],
)
def test_the_restatement_send_pings_for_its_receipt_only(line, monkeypatch, outcome, words, expected):
    # 「已更新提醒」 had no behavior test at all (post-review seat 1 I1).
    _the_text_handler_reaches_the_restatement(monkeypatch, outcome)
    asker_mentioned("U_B", token="TOKEN001")

    main._handle_text_message(_make_text_event("改成明天", msg_id="M-restated"), G)

    [message] = line.replies[0]
    assert words in message.text
    assert pinged(message) == expected


# ── receipt branches nothing else reaches (post-review seats 2 and 3, I1) ───────────────────────


def _record_replies(monkeypatch) -> list:
    """`_reply` swapped for a recorder of what a handler hands it, mark and all (not muted, so the send
    helpers do call it)."""
    sent: list = []
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reply", lambda _token, text, **kwargs: sent.append((text, kwargs)) or True)
    monkeypatch.setattr(main.burst_filter, "cancel_burst", lambda *_a, **_k: [])
    return sent


def test_a_date_only_cancel_of_a_reminder_being_sent_reaches_reply_with_its_mark(monkeypatch):
    # 「已取消後續提醒」 on the date-only path: the reminder's push is on its way.
    cancel_tests._freeze_date_cancel_clock(monkeypatch)
    rid = cancel_tests._seed("G1", "繳管理費", "2030-08-08 09:00")
    assert memory.claim_natural_reminder_delivery(
        "G1", rid, "1d", expected_action="繳管理費", expected_remind_at=cancel_tests._ts("2030-08-08 09:00"), transport="push"
    )
    sent = _record_replies(monkeypatch)
    text = "咪寶8/8沒有活動 取消"

    assert main._try_handle_reminder_cancellation(cancel_tests._event(text), "G1", text)

    [(reply, _kwargs)] = sent
    assert reply.startswith("已取消後續提醒\n這一則已開始傳送，仍可能送達。")
    assert isinstance(reply, line_mentions.AllowsMemberMention)  # a receipt keeps its @s (2026-11-13)


def _freeze_reschedule_clock(monkeypatch) -> None:
    """As `fixed_now` in test_quoted_reminder_reschedule.py: the parser's now is 2099-09-27 13:48."""
    now = datetime(2099, 9, 27, 13, 48, tzinfo=reschedule_tests.TW)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(main, "datetime", _Frozen)


@pytest.mark.parametrize(
    ("words", "heading"),
    [("12:00", "提醒本來就是這個時間"), ("10/8", "已更新提醒（2099-10-01 12:00 → 2099-10-08 12:00）")],
    ids=["unchanged", "updated"],
)
def test_a_reschedule_delivered_again_replays_its_receipt_with_its_mark(monkeypatch, words, heading):
    # LINE delivers the same message again and nothing changed in between: answered from the log.
    _freeze_reschedule_clock(monkeypatch)
    rid = reschedule_tests._seed("繳管理費", "2099-10-01 12:00")
    reschedule_tests._archive_bot("push-again", "⏰ 提醒（4 天後）\n2099-10-01 12:00 繳管理費", reminder_id=rid)
    sent = _record_replies(monkeypatch)

    def deliver():
        event = reschedule_tests._event(words, quoted="push-again", message_id="msg-again")
        assert main._try_handle_quoted_reminder_reschedule(event, "G1", words)

    deliver()
    assert memory.get_reminder_reschedule_log("G1", "msg-again") is not None  # so the second one is a replay
    deliver()

    [_first, (again, kwargs)] = sent
    assert again.startswith(heading) and "先前已處理" not in again
    assert isinstance(again, line_mentions.AllowsMemberMention)  # a receipt keeps its @s (2026-11-13)
    assert kwargs["primary_reminder_ref"] == {"reminder_id": rid}


def _a_bound_push_to_keep_one_stage_of(monkeypatch) -> tuple[int, list]:
    rid = push_limit_tests._seed("看牙醫", push_limit_tests.FAR)
    push_limit_tests._archive("push-keep", push_limit_tests._push_text("看牙醫"), reminder_id=rid)
    return rid, _record_replies(monkeypatch)


def _keep_one_hour_before() -> bool:
    text = "只要一小時前提醒"
    return main._try_handle_reminder_push_limit(push_limit_tests._event(text, quoted="push-keep"), "G1", text)


def test_a_push_limit_receipt_that_fails_after_the_write_sends_the_planned_one_with_its_mark(monkeypatch):
    # The stage is kept, then the receipt built after the write fails: the one planned before it goes out.
    rid, sent = _a_bound_push_to_keep_one_stage_of(monkeypatch)
    real_receipt, raised = rpl.receipt, []

    def receipt(*args, **kwargs):
        if "calendar_title" in kwargs:  # only the receipt built after the write takes a calendar line
            raised.append(True)
            raise RuntimeError("receipt failed")
        return real_receipt(*args, **kwargs)

    monkeypatch.setattr(rpl, "receipt", receipt)

    assert _keep_one_hour_before()

    assert raised == [True]
    assert push_limit_tests._state(rid)["push_keep_stage"] == "1hr"
    [(reply, kwargs)] = sent
    assert reply.splitlines()[0] == "已改成只在 1 小時前提醒"
    assert isinstance(reply, line_mentions.AllowsMemberMention)  # a receipt keeps its @s (2026-11-13)
    assert kwargs["primary_reminder_ref"] == {"reminder_id": rid}


def test_a_push_limit_receipt_whose_calendar_line_cannot_be_shown_goes_without_it_with_its_mark(monkeypatch):
    # The outbound gates would change the calendar line (a title with markdown, say): built again without it.
    _rid, sent = _a_bound_push_to_keep_one_stage_of(monkeypatch)
    monkeypatch.setattr(main, "_push_limit_calendar_title", lambda *_a: "看牙醫")
    real_displayable, checked = main._reschedule_receipt_displayable, []

    def displayable(receipt):
        checked.append(receipt)
        return "行事曆的" not in receipt and real_displayable(receipt)

    monkeypatch.setattr(main, "_reschedule_receipt_displayable", displayable)

    assert _keep_one_hour_before()

    assert any("行事曆的「看牙醫」" in receipt for receipt in checked)  # offered, and turned down
    [(reply, _kwargs)] = sent
    assert reply.splitlines()[0] == "已改成只在 1 小時前提醒" and "行事曆的" not in reply
    assert isinstance(reply, line_mentions.AllowsMemberMention)  # a receipt keeps its @s (2026-11-13)


# ── a model's words on the other ways out ────────────────────────────────────────────────────────


def test_an_article_the_research_path_read_cannot_ping_a_member(line, research_env, monkeypatch):  # noqa: F811
    # The case the docstring above names: pages nobody here picked, and a model told to copy them
    # (post-review seat 2 I3).  The asker's own @ is not pinged again either.
    monkeypatch.setattr(main, "_reply", REAL_REPLY)
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [dict(ROW)])
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: f"{ANSWER}\n@成員甲 @成員乙 記得看")
    asker_mentioned("U_C", token=research_env.event.reply_token)

    main._handle_web_research_question(research_env.event, GROUP, "輝達暴跌10 %多", addressed=False)

    [message] = line.replies[0]
    assert message.text == f"{ANSWER}\n@成員甲 @成員乙 記得看"
    assert pinged(message) == set()


def test_a_queued_answer_pushed_by_the_legacy_drain_pings_no_member(line, monkeypatch, tmp_path):
    # The legacy pending queue is off; a rollback turns it on.  What its drain pushes is a model's answer
    # (before this test, only the member pin table watched this flag).
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: True)
    monkeypatch.setattr(main, "_DRAIN_LOCK_DIR", str(tmp_path / "drain_locks"))
    pending_store.save_full(
        {GROUP: [{"message_id": "M-queued", "type": "text", "text": "早安", "user_id": "U_A", "timestamp": time.time()}]}
    )
    monkeypatch.setattr(main, "_quota_exhausted", lambda *_a, **_k: False)
    monkeypatch.setattr(main, "_gemini_group_messages", lambda items: [{"idxs": [0], "reply_to": 0}])
    monkeypatch.setattr(main, "_build_group_parts", lambda *_a: ["prompt"])
    monkeypatch.setattr(main, "_llm_chat", lambda *_a, **_k: MODEL_WORDS[0])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *_a, **_k: [])

    assert main._drain_pending_for_group(GROUP, source="test")

    [(batch, _key)] = line.pushes
    assert batch[0].text == main._prepare_outbound_text(MODEL_WORDS[0], source="pending_push")
    assert pinged(batch[0]) == set()


# ── every place that can ping a member, as one list ─────────────────────────────────────────

_MEMBER_ROOT = "AllowsMemberMention"
# what turns names, words or a LINE payload into a member's mention: every caller is listed
_MEMBER_SOURCES = {
    "reminder_actor_targets",
    "event_mention_targets",
    "aliases_mentioned_in_text",
    "mention_names",
    "_extract_reply_payload_targets",
    "_register_reply_mention_targets",
    "_consume_reply_mention_targets",
    "text_v2_dict",
    "sdk_message_from_text_v2_dict",
    "user_mention_targets",  # 「提到人就真 @」, not in main yet: whoever lands later adds its lines
    "push_text_with_mentions",
    "try_push_text_with_mentions",  # 「提到人就真 @」's wrapper of the one above: its callers come on the list too
}


def _called(node) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


def _program_modules(root: Path):
    for folder, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith((".", "__"))]  # worktrees and venvs live under dot folders
        for name in files:
            if name.endswith(".py") and not name.startswith("test_") and name != "conftest.py":
                path = Path(folder) / name
                yield path.relative_to(root).with_suffix("").as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _member_marks(modules) -> set[str]:
    """`AllowsMemberMention` and every class under it, found by name in every module: when a class moves
    under it (the reminder-path stage 2 moves `ReminderReceipt`), its call sites come on the list by
    themselves (review seat 4 I1)."""
    marks = {_MEMBER_ROOT}
    while True:
        grown = {
            node.name
            for _module, tree in modules
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and any(_called(base) in marks for base in node.bases)
        }
        if grown <= marks:
            return marks
        marks |= grown


class _MemberSites(ast.NodeVisitor):
    def __init__(self, module: str, marks: set[str]):
        self.module, self.marks, self.scope, self.found = module, marks, [], Counter()

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    def visit_ClassDef(self, node):
        if any(_called(base) in self.marks for base in node.bases):
            self.found[f"{self.module}.{node.name} is a member mark"] += 1
        self._scoped(node)

    def visit_Call(self, node):
        name, where = _called(node.func), ".".join([self.module, *self.scope])
        keywords = {kw.arg: kw.value for kw in node.keywords}
        if name in self.marks:
            self.found[f"{where} marks a text for members ({name})"] += 1
        elif name == "isinstance" and len(node.args) == 2 and any(
            _called(part) in self.marks for part in ast.walk(node.args[1])
        ):
            self.found[f"{where} reads a member mark"] += 1
        elif name == "_text_message_with_mentions":
            if None in keywords:  # **kwargs could carry the flags: show what is spread in
                self.found[f"{where} builds with **{ast.unparse(keywords[None])}"] += 1
            else:
                flag = ast.unparse(keywords["allow_member_mention"]) if "allow_member_mention" in keywords else "nothing"
                explicit = ast.unparse(keywords["explicit_only"]) if "explicit_only" in keywords else "False"
                self.found[f"{where} builds members with {flag}, explicit_only={explicit}"] += 1
            if "reply_targets" in keywords:
                self.found[f"{where} builds with reply_targets={ast.unparse(keywords['reply_targets'])}"] += 1
        elif name in _MEMBER_SOURCES:
            self.found[f"{where} calls {name}"] += 1
        elif name == "MentionTarget" and (
            None in keywords or ast.unparse(keywords.get("kind", ast.Constant(None))) == "'user'"
        ):
            self.found[f"{where} makes a user target"] += 1
        elif name == "UserMentionTarget":
            self.found[f"{where} makes an SDK user target"] += 1
        elif name == "dict" and ast.unparse(keywords.get("type", ast.Constant(None))) == "'user'":
            self.found[f"{where} writes a type-user mention"] += 1  # as MAY_PING reads dict(type="all")
        elif name == "__class__" or (isinstance(node.func, ast.Call) and _called(node.func.func) == "type"):
            self.found[f"{where} stamps a text with its own type again"] += 1  # the one way a mark survives a change
        self.generic_visit(node)

    def visit_Dict(self, node):
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant) and key.value == "type"
                and isinstance(value, ast.Constant) and value.value == "user"
            ):
                self.found[f"{'.'.join([self.module, *self.scope])} writes a type-user mention"] += 1
        self.generic_visit(node)


def _member_sites(root: Path) -> dict:
    modules = list(_program_modules(root))
    marks = _member_marks(modules)
    found: Counter = Counter()
    for module, tree in modules:
        visitor = _MemberSites(module, marks)
        visitor.visit(tree)
        found += visitor.found
    return dict(found)


# 2026-11-13.  Before a line here is added or changed: a 「marks a text for members」 line may only be a
# receipt formatter building its own words, after its last change to them.  Never a sending helper (it also
# sends refusals, and `_handle_command`'s `_reply` sends the model's /檢討) and never a model's reply.  The words
# a receipt reads back (a stored 事項 or title) ping whom they @: TODO for the reminder-path stage 2.  Whoever
# lands later adds the lines of 「提到人就真 @」 (`user_mention_targets`, `push_text_with_mentions`,
# `try_push_text_with_mentions`).
MAY_PING_A_MEMBER = {
    # the marks (a class under AllowsMemberMention is one too) and the one place they are read
    'line_mentions.AllowsAllMention is a member mark': 1,
    'main.ReminderReceipt is a member mark': 1,
    'main._reply reads a member mark': 2,  # the member flag and the @all flag
    # polls and receipts the program put together
    'family_poll.format_created_reply marks a text for members (AllowsAllMention)': 1,
    'family_poll.format_nudge_reply marks a text for members (AllowsAllMention)': 1,
    'main._create_schedule_reminders marks a text for members (ReminderReceipt)': 1,
    'main._creation_followup_reply marks a text for members (ReminderReceipt)': 1,
    'main._format_schedule_receipt marks a text for members (ReminderReceipt)': 2,
    'main._format_source_calendar_capture_confirmation marks a text for members (ReminderReceipt)': 2,
    'main._maybe_extract_reminder marks a text for members (ReminderReceipt)': 1,
    # receipts marked on the line that builds them (2026-11-13): their send helpers also send refusals
    'main._apply_quoted_reschedule marks a text for members (AllowsMemberMention)': 1,  # 改時間
    'main._reschedule_replay_reply marks a text for members (AllowsMemberMention)': 4,  # 改時間, sent again
    'main._try_handle_reminder_push_limit marks a text for members (AllowsMemberMention)': 3,  # 只推一次
    'main._try_handle_reminder_cancellation marks a text for members (AllowsMemberMention)': 4,  # 已取消, two paths
    'main._try_handle_calendar_correction marks a text for members (AllowsMemberMention)': 1,  # 已更正
    'main._try_handle_quoted_calendar_correction marks a text for members (AllowsMemberMention)': 1,  # 已更正行程與提醒
    'main._try_handle_missed_reminder_repair marks a text for members (AllowsMemberMention)': 1,  # 已補上提醒
    'main._handle_text_message marks a text for members (AllowsMemberMention)': 1,  # 已更新提醒
    'main._format_contextual_date_reminder_confirmation marks a text for members (AllowsMemberMention)': 1,  # 已新增 4 筆提醒
    'main._cancel_calendar_event marks a text for members (AllowsMemberMention)': 1,  # /取消活動: never the command's _reply
    # every builder call: who may be pinged there
    'main._reply builds members with allow_member_mention, explicit_only=True': 2,  # the reply and its push fallback: by the text's type
    'main._reply builds with reply_targets=reply_targets': 2,  # emptied unless the text is marked
    'main._reply calls _consume_reply_mention_targets': 1,
    'main._reply builds members with True, explicit_only=True': 2,  # riders the program wrote: a stored confirmation, a due reminder
    'main._try_one_shot_reply builds members with True, explicit_only=False': 1,  # a file a person writes: a bare name pings
    'main._reply builds members with False, explicit_only=True': 1,  # a queued answer riding along: a model wrote it
    'main._drain_pending_for_group builds members with False, explicit_only=True': 1,  # a queued answer: a model wrote it
    'main._handle_todo_query builds members with nothing, explicit_only=True': 1,  # lists ping nobody (2026-11-13)
    'main._handle_calendar_query builds members with nothing, explicit_only=True': 1,
    'main._exercise_nudge_message builds members with nothing, explicit_only=True': 1,
    'main._exercise_nudge_message builds with reply_targets=[target]': 1,  # its own member, chosen by the program
    'main._exercise_nudge_message makes a user target': 1,
    # the asker's own @s (LINE's mention payload)
    'main._handle_event calls _register_reply_mention_targets': 1,
    'main._register_reply_mention_targets calls _extract_reply_payload_targets': 1,
    'main._extract_reply_payload_targets makes a user target': 1,
    # inside the builder
    'main._text_message_with_mentions calls aliases_mentioned_in_text': 1,
    'main._text_message_with_mentions calls mention_names': 1,
    'main._text_message_with_mentions calls sdk_message_from_text_v2_dict': 1,
    'main._text_message_with_mentions calls text_v2_dict': 1,
    'main._text_message_with_mentions makes a user target': 2,
    # mentions made from the people a reminder or an event names, not from a reply's words
    'calendar_db._raw_text_reminder_aliases calls aliases_mentioned_in_text': 1,
    'calendar_db._raw_text_reminder_aliases calls mention_names': 1,
    'event_reminder._push_mention_result writes a type-user mention': 1,
    'event_reminder.build_reminder_message_spec calls event_mention_targets': 1,
    'event_reminder.build_reminder_message_spec calls text_v2_dict': 1,
    'event_reminder.sdk_message_from_spec calls sdk_message_from_text_v2_dict': 2,  # a stored push, sent again
    'event_reminder.sdk_message_from_spec writes a type-user mention': 1,
    'line_mentions._append_alias_target makes a user target': 1,
    'line_mentions.event_mention_targets makes a user target': 1,
    'line_mentions.reminder_actor_targets makes a user target': 1,  # also for a name in the 事項, no @ needed
    'line_mentions.sdk_message_from_text_v2_dict makes an SDK user target': 1,
    'line_mentions.text_v2_dict writes a type-user mention': 1,
    'reminder_push._build_push_text_and_message calls reminder_actor_targets': 1,
    'reminder_push._build_push_text_and_message calls sdk_message_from_text_v2_dict': 1,
    'reminder_push._build_push_text_and_message calls text_v2_dict': 1,
    'reminder_push._event_audience_covered calls event_mention_targets': 1,  # who it pings, to fold duplicates
    'reminder_push._reminder_audience calls reminder_actor_targets': 1,
    # 「提到人就真 @」 (2026-11-12): whom a push names, taken from structured fields (a parent's role, a
    # TODO's owner), sent by push: neither `_reply` nor the builder
    'anniversary._push calls try_push_text_with_mentions': 1,
    'line_mentions.user_mention_targets makes a user target': 1,
    'line_push_client.push_text_with_mentions calls text_v2_dict': 1,
    'line_push_client.try_push_text_with_mentions calls push_text_with_mentions': 1,
    'parent_health._named_parents calls user_mention_targets': 1,
    'parent_health._push_group calls push_text_with_mentions': 1,
    'todo._owner_targets calls user_mention_targets': 1,
    'todo._push calls try_push_text_with_mentions': 1,
}


def test_every_place_that_can_ping_a_member_is_on_the_list():
    # It stops a copied or tidied-up line, not someone set on getting around it (an alias of the class).
    assert _member_sites(Path(main.__file__).resolve().parent) == MAY_PING_A_MEMBER
