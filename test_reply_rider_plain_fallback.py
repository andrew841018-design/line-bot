"""A due reminder LINE refuses while it rides on a reply goes out once as plain text (2026-11-14).

Andrew 2026-11-12 (「提到人就真 @」): 「textV2 被 LINE 拒收時改送純文字一次，不能因為加了 @ 讓訊息送不到」.
On 2026-11-14 he chose 「改送純文字」 for the reply path too: a reminder rider that LINE refuses with
a 400 is put into the same reply's resend as one plain TextMessage, instead of being fenced as
`uncertain` (which blocked every later claim of that stage, so it never arrived in any form).
Pushes are out of quota until the month resets, so the reply is the only way a reminder arrives.

Synthetic ids and texts only; nothing here talks to LINE.
"""

from __future__ import annotations

import json
import logging

import pytest

import calendar_db
import event_reminder
import line_mentions
import main
import memory
import reminder_push
from test_support_fake_line import (
    BAD_TOKEN,
    BAD_UNNAMED,
    GROUP,
    _at,
    _day,
    api_error,
    bad,
    claims,
    due_natural,
    install_fake_line,
    kinds,
    pushed_1hr,
    raw_bot_texts,
    refused,
    texts,
)

TOKEN = "TOKEN-SYNTHETIC-PLAIN"


@pytest.fixture(autouse=True)
def _synthetic_aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))


@pytest.fixture()
def line(monkeypatch):
    return install_fake_line(monkeypatch)


@pytest.fixture()
def riding(monkeypatch):
    """Turn the due-reminder piggyback on (the fast path and steps 2 and 3 of `_reply`)."""
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)


def open_event(message_id="M1", token=TOKEN):
    assert memory.begin_inbound_event(GROUP, message_id) == "new"
    main._register_inbound_reply_token(token, GROUP, message_id)


def say(text="嗨", *, token=TOKEN, **kwargs):
    return main._reply(token, text, group_id=GROUP, **kwargs)


def plain_of_due_natural() -> str:
    [item] = reminder_push.due_reminders_for_reply(GROUP, limit=4)
    assert type(item["message"]).__name__ == "TextMessageV2"  # the precondition: it pings someone
    return item["text"]


def test_a_named_reminder_rider_goes_out_as_plain_text_in_the_resend(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    plain = plain_of_due_natural()
    open_event()
    line.script = [bad(1)]  # LINE names the reminder

    assert say("嗨") is True

    assert [kinds(batch) for batch in line.replies] == [
        ["TextMessage", "TextMessageV2"],
        ["TextMessage", "TextMessage"],
    ]
    assert texts(line.replies[1]) == ["嗨", plain]
    assert "{" not in plain
    assert pushed_1hr(rid) == 1  # the stage is delivered
    assert claims() == []  # finalized, not fenced
    assert plain in raw_bot_texts()  # the archive holds what the family saw
    assert not line.pushes


def test_a_reminder_the_fast_path_gets_refused_goes_out_as_plain_text(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    plain = plain_of_due_natural()
    open_event()
    line.script = [bad(0)]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is True
    assert memory.get_inbound_event_status(GROUP, "M1") == "replied"  # (POST_1 N1) the token's event is answered

    assert [kinds(batch) for batch in line.replies] == [["TextMessageV2"], ["TextMessage"]]
    assert texts(line.replies[1]) == [plain]
    assert pushed_1hr(rid) == 1
    assert claims() == []
    assert plain in raw_bot_texts()
    assert memory.get_sent_reminder_reference(GROUP, "sent-1")["reminder_id"] == rid  # the retry's answer is archived
    assert not line.pushes


def test_a_named_calendar_reminder_rider_goes_out_as_plain_text_in_the_resend(line, riding):
    tomorrow = _day(1).isoformat()
    event_id = calendar_db.insert_event(
        group_id=GROUP,
        title="拿生日蛋糕",
        event_date=tomorrow,
        event_time="14:00",
        location="喜來登",
        participants=["成員甲"],
        event_type="family_gathering",
    )
    assert event_id
    [event] = [e for e in calendar_db.list_due_for_reminder(GROUP, days_ahead=1) if e["event_id"] == event_id]
    spec = event_reminder.build_reminder_message_spec(event, 1, allow_mention=True)
    assert spec["kind"] == "textV2"  # the precondition: it pings someone
    open_event()
    line.script = [bad(1)]

    assert say("嗨") is True

    assert [kinds(batch) for batch in line.replies] == [
        ["TextMessage", "TextMessageV2"],
        ["TextMessage", "TextMessage"],
    ]
    assert texts(line.replies[1]) == ["嗨", spec["fallback_text"]]
    assert claims() == []
    with calendar_db._conn() as c:
        assert c.execute("SELECT reminded_1d FROM events WHERE event_id=?", (event_id,)).fetchone()[0] is not None


# ── when the one more try is not taken ──────────────────────────────────────────────────────


def status(message_id="M1"):
    return memory.get_inbound_event_status(GROUP, message_id)


def test_a_plain_resend_that_gets_no_answer_leaves_the_reminder_uncertain_and_pushes_nothing(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), TimeoutError("read timed out")]

    assert say("嗨") is False

    assert len(line.replies) == 2 and not line.pushes
    assert status() == "reply_sending"  # the fence stays: it may have been delivered
    assert claims() == [(str(rid), "uncertain")]
    assert pushed_1hr(rid) == 0


def test_a_plain_resend_that_finds_the_token_gone_fences_the_reminder(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), BAD_TOKEN]

    assert say("嗨") is True  # the primary goes by push, in its usual form

    assert len(line.replies) == 2 and len(line.pushes) == 1
    assert texts(line.pushes[0][0]) == ["嗨"]  # the push carries the primary only
    assert claims() == [(str(rid), "uncertain")]  # fenced, never released: it must not ride again
    assert pushed_1hr(rid) == 0


def test_a_named_acknowledgement_is_still_fenced_while_a_named_reminder_goes_as_plain_text(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    plain = plain_of_due_natural()
    memory.enqueue_reminder_confirmation(
        GROUP, "pending_reminder:1", "已新增提醒\n時間：2099-10-01 12:00\n事項：繳管理費\n對象：@成員甲"
    )
    open_event()
    line.script = [bad(1, 2)]  # the acknowledgement and the reminder

    assert say("好的") is True

    assert kinds(line.replies[0]) == ["TextMessage", "TextMessageV2", "TextMessageV2"]
    assert texts(line.replies[1]) == ["好的", plain]  # the acknowledgement left out, the reminder as plain text
    assert pushed_1hr(rid) == 1 and claims() == []
    with memory._conn() as c:
        rows = c.execute("SELECT source_ref, status FROM reminder_confirmation_outbox").fetchall()
    assert rows == [("pending_reminder:1", "sending")]  # fenced, as before: tried again after its claim lapses
    assert all("繳管理費" not in text for batch in line.replies[1:] for text in texts(batch))


def test_a_named_reminder_that_is_plain_already_is_fenced_as_before(line, riding):
    """A calendar reminder nobody resolves to an account is a plain TextMessage: a plain resend
    cannot help it, so it is fenced at once (as before)."""
    tomorrow = _day(1).isoformat()
    event_id = calendar_db.insert_event(
        group_id=GROUP, title="拿生日蛋糕", event_date=tomorrow, event_time="14:00", location="喜來登", event_type="family_gathering"
    )
    open_event()
    line.script = [bad(1)]

    assert say("嗨") is True

    assert kinds(line.replies[0]) == ["TextMessage", "TextMessage"]
    assert texts(line.replies[1]) == ["嗨"]
    assert claims() == [(str(event_id), "uncertain")]


ARMED = "@成員甲 嗨"


def arm_mention():
    """The primary mentions someone (a receipt's mark), so it is a textV2 too."""
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")
    main._reply_mention_targets_by_token[TOKEN] = [target]
    return line_mentions.AllowsMemberMention(ARMED)


@pytest.mark.parametrize(
    "script",
    [
        [bad(1)],
        [bad(1), bad(1)],
        [bad(1), bad(0)],
        [bad(1), bad(1), bad(0)],
        [bad(1), bad(0), bad(0)],
        [bad(1), bad(1), bad(1), bad(1)],
        [bad(1), BAD_TOKEN],
        [bad(1), BAD_UNNAMED, BAD_UNNAMED],
    ],
)
def test_a_reply_never_takes_more_than_three_calls(line, riding, monkeypatch, script):
    due_natural(monkeypatch)
    open_event()
    line.script = list(script)

    say(arm_mention())

    assert len(line.replies) <= 3
    assert len(line.pushes) <= 1


# ── the fast path ───────────────────────────────────────────────────────────────────────────


def test_a_fast_path_plain_resend_that_is_refused_again_fences_the_reminders(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    line.script = [bad(0), bad(0)]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    assert [kinds(batch) for batch in line.replies] == [["TextMessageV2"], ["TextMessage"]]
    assert claims() == [(str(rid), "uncertain")]
    assert pushed_1hr(rid) == 0 and raw_bot_texts() == []


@pytest.mark.parametrize(
    "second",
    [BAD_TOKEN, TimeoutError("read timed out"), api_error(500, {"message": "x"}, reason="Internal Server Error")],
    ids=["token-gone", "timeout", "5xx"],
)
def test_a_fast_path_plain_resend_that_fails_any_other_way_fences_and_never_releases(line, riding, monkeypatch, second):
    """Released, a reminder whose first refusal spent the token would fail every later message's
    reply the same way; fenced, it costs one."""
    rid = due_natural(monkeypatch)
    line.script = [bad(0), second]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    assert len(line.replies) == 2
    assert claims() == [(str(rid), "uncertain")]
    assert pushed_1hr(rid) == 0


def test_the_fast_path_resends_only_the_named_reminder_as_plain_text(line, riding, monkeypatch):
    first = due_natural(monkeypatch, "繳房屋稅", user="U_A")
    second = due_natural(monkeypatch, "拿乾洗衣物", user="U_B")
    line.script = [bad(1)]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is True

    assert [kinds(batch) for batch in line.replies] == [
        ["TextMessageV2", "TextMessageV2"],
        ["TextMessageV2", "TextMessage"],  # the other one keeps its @
    ]
    assert pushed_1hr(first) == 1 and pushed_1hr(second) == 1 and claims() == []


TOKEN_NAMING_A_MESSAGE = api_error(
    400, {"message": "Invalid reply token", "details": [{"message": "x", "property": "messages[0].text"}]}
)


@pytest.mark.parametrize(
    "first_error, released",
    [
        (BAD_TOKEN, True),
        (TOKEN_NAMING_A_MESSAGE, True),  # a dead token is never content, whatever else the body says
        (api_error(403, refused(0), reason="Forbidden"), False),  # only a 400 is a content refusal
        (TimeoutError("read timed out"), False),
    ],
    ids=["token", "token-with-details", "403-naming-it", "timeout"],
)
def test_the_fast_path_has_no_plain_resend_after_a_token_error_another_4xx_or_a_timeout(
    line, riding, monkeypatch, first_error, released
):
    rid = due_natural(monkeypatch)
    line.script = [first_error]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    assert len(line.replies) == 1
    assert claims() == ([] if released else [(str(rid), "uncertain")])  # as before


def test_the_fast_path_does_not_resend_a_named_message_that_is_plain_already(line, riding):
    tomorrow = _day(1).isoformat()
    event_id = calendar_db.insert_event(
        group_id=GROUP, title="拿生日蛋糕", event_date=tomorrow, event_time="14:00", location="喜來登", event_type="family_gathering"
    )
    line.script = [bad(0)]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    assert [kinds(batch) for batch in line.replies] == [["TextMessage"]]
    assert claims() == [(str(event_id), "uncertain")]


# ── logs carry ids and kinds, never the reminder's words ────────────────────────────────────


@pytest.mark.parametrize("script", [[bad(1)], [bad(1), bad(1)], [bad(1), TimeoutError("read timed out")]])
def test_no_log_line_carries_the_words_of_a_reminder_sent_again_as_plain_text(line, riding, monkeypatch, caplog, script):
    due_natural(monkeypatch, "繳哨兵稅款")
    open_event()
    line.script = list(script)
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    say("嗨")

    assert "reply rider goes once more as plain text" in caplog.text
    assert "繳哨兵稅款" not in caplog.text and "成員甲" not in caplog.text


def test_no_fast_path_log_line_carries_the_words_of_a_reminder(line, riding, monkeypatch, caplog):
    due_natural(monkeypatch, "繳哨兵稅款")
    line.script = [bad(0), bad(0)]
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    main._try_piggyback_reminders_fast_path(TOKEN, GROUP)

    assert "once more as plain text" in caplog.text
    assert "繳哨兵稅款" not in caplog.text and "成員甲" not in caplog.text


# ── which riders can go plain ───────────────────────────────────────────────────────────────


def _textv2():
    return line_mentions.sdk_message_from_text_v2_dict(
        line_mentions.text_v2_dict(
            "⏰ 提醒",
            [line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")],
            None,
        )
    )


@pytest.mark.parametrize("kind", ["confirmation", "pending", "nudge"])
def test_only_a_due_reminder_rider_is_ever_sent_again_as_plain_text(kind):
    import reply_riders

    rider = reply_riders.Rider(kind, f"{kind}:1", plain_text="@成員甲\n⏰ 提醒")
    assert type(_textv2()).__name__ == "TextMessageV2"  # the precondition

    assert main._rider_plain_message(rider, _textv2()) is None


@pytest.mark.parametrize("kind", ["event", "natural"])
def test_a_due_reminder_rider_goes_plain_only_from_a_textv2_with_a_plain_twin(kind):
    import reply_riders
    from linebot.v3.messaging import TextMessage

    with_twin = reply_riders.Rider(kind, f"{kind}:1", plain_text="@成員甲\n⏰ 提醒")
    plain = main._rider_plain_message(with_twin, _textv2())
    assert type(plain).__name__ == "TextMessage" and plain.text == "@成員甲\n⏰ 提醒"

    assert main._rider_plain_message(with_twin, TextMessage(text="⏰ 提醒")) is None  # plain already
    for twin in (None, "", "  \n"):
        assert main._rider_plain_message(reply_riders.Rider(kind, f"{kind}:1", plain_text=twin), _textv2()) is None


# ── review PRE_1 additions ──────────────────────────────────────────────────────────────────


def test_an_unnamed_fast_path_refusal_sends_every_textv2_reminder_once_as_plain_text(line, riding, monkeypatch):
    """(Q1) The fast path's request holds nothing but reminders: an unnamed 400 used to lose the whole batch."""
    first = due_natural(monkeypatch, "繳房屋稅", user="U_A")
    second = due_natural(monkeypatch, "拿乾洗衣物", user="U_B")
    line.script = [BAD_UNNAMED]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is True

    assert [kinds(batch) for batch in line.replies] == [
        ["TextMessageV2", "TextMessageV2"],
        ["TextMessage", "TextMessage"],
    ]
    assert pushed_1hr(first) == 1 and pushed_1hr(second) == 1 and claims() == []


def test_an_unnamed_fast_path_refusal_of_plain_reminders_only_has_no_resend(line, riding):
    """The same request again would be refused again: nothing to resend, fenced as before."""
    event_id = calendar_db.insert_event(
        group_id=GROUP, title="拿生日蛋糕", event_date=_day(1).isoformat(), event_time="14:00", event_type="family_gathering"
    )
    line.script = [BAD_UNNAMED]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    assert [kinds(batch) for batch in line.replies] == [["TextMessage"]]
    assert claims() == [(str(event_id), "uncertain")]


def test_a_failure_while_building_the_plain_text_leaves_the_flow_of_before(line, riding, monkeypatch, caplog):
    """(I3) Nothing has been sent since the 400: a bug while building the plain text must not turn into an
    ambiguous send that fences every rider and never resends the primary."""
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1)]

    def broken(*_args, **_kwargs):
        raise ValueError("broken build")

    monkeypatch.setattr(main, "_rider_plain_message", broken)
    caplog.set_level(logging.WARNING)

    assert say("嗨") is True

    assert [kinds(batch) for batch in line.replies] == [["TextMessage", "TextMessageV2"], ["TextMessage"]]
    assert texts(line.replies[1]) == ["嗨"]  # the primary alone, as before
    assert claims() == [(str(rid), "uncertain")]  # fenced, as before
    assert status() == "replied"
    assert "reply rider plain text not built error_type=ValueError" in caplog.text


def test_a_failure_while_building_the_fast_path_plain_text_fences_as_before_and_logs_no_words(
    line, riding, monkeypatch, caplog
):
    rid = due_natural(monkeypatch)
    line.script = [bad(0)]

    def broken(*_args, **_kwargs):
        raise ValueError("繳哨兵稅款")  # an exception whose text carries family words

    monkeypatch.setattr(main, "_plain_reminder_message", broken)
    caplog.set_level(logging.DEBUG)

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    assert len(line.replies) == 1
    assert claims() == [(str(rid), "uncertain")]
    assert "reminder fast-path plain text not built error_type=ValueError" in caplog.text
    assert "繳哨兵稅款" not in caplog.text


DEGRADED_RECEIPT = "這個圖片我這次沒分析成功，請稍後再傳一次🙏"  # a shape Andrew rejected: suppressed


def test_a_suppressed_rejected_receipt_still_lets_a_named_reminder_go_once_as_plain_text(line, riding, monkeypatch):
    """(I4) The receipt is suppressed, so the reply is the reminder alone: it goes once more as plain text."""
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    rid = due_natural(monkeypatch)
    plain = plain_of_due_natural()
    open_event()
    line.script = [bad(0)]
    delivery = {}

    assert say(DEGRADED_RECEIPT, primary_delivery=delivery) is True

    assert delivery == {"suppressed": True}  # the precondition: the receipt itself is never sent
    assert [kinds(batch) for batch in line.replies] == [["TextMessageV2"], ["TextMessage"]]
    assert texts(line.replies[1]) == [plain] and not line.pushes
    assert pushed_1hr(rid) == 1 and claims() == []
    assert all("圖片" not in text for batch in line.replies for text in texts(batch))


def test_a_suppressed_rejected_receipt_whose_reminder_is_refused_twice_ends_the_event_as_before(line, riding, monkeypatch):
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(0), bad(0)]

    assert say(DEGRADED_RECEIPT) is False

    assert len(line.replies) == 2 and not line.pushes
    assert claims() == [(str(rid), "uncertain")]
    assert status() == "completed_no_reply"  # the event ends here, as it did before


def test_a_fast_path_covered_calendar_event_is_settled_with_its_plain_carrier(line, riding, monkeypatch):
    day = _day(1)
    event_id = calendar_db.insert_event(
        group_id=GROUP, title="拿生日蛋糕", event_date=day.isoformat(), event_time="14:00", participants=["成員甲"]
    )
    assert event_id
    rid, _ = memory.add_reminder_with_outcome(
        GROUP, "U_A", "拿生日蛋糕", _at(day, 14), source_text=f"{day.month}/{day.day} 14:00 拿生日蛋糕", time_kind="clock"
    )
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: _at(day, 13))
    line.script = [bad(0)]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is True

    assert [kinds(batch) for batch in line.replies] == [["TextMessageV2"], ["TextMessage"]]  # one message for both
    assert claims() == [] and pushed_1hr(rid) == 1
    with calendar_db._conn() as c:
        assert c.execute("SELECT reminded_1d FROM events WHERE event_id=?", (event_id,)).fetchone()[0] is not None


def test_the_fast_path_index_points_at_the_message_actually_sent_after_the_keep_filter(line, riding, monkeypatch):
    """A covered calendar item is dropped before the request is built: LINE's index 1 is the second
    message sent, and only that one goes plain."""
    day = _day(1)
    calendar_db.insert_event(
        group_id=GROUP, title="拿生日蛋糕", event_date=day.isoformat(), event_time="14:00", participants=["成員甲"]
    )
    memory.add_reminder_with_outcome(
        GROUP, "U_A", "拿生日蛋糕", _at(day, 14), source_text=f"{day.month}/{day.day} 14:00 拿生日蛋糕", time_kind="clock"
    )
    memory.add_reminder_with_outcome(
        GROUP, "U_B", "拿乾洗衣物", _at(day, 14), source_text=f"{day.month}/{day.day} 14:00 拿乾洗衣物", time_kind="clock"
    )
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: _at(day, 13))
    line.script = [bad(1)]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is True

    first, second = line.replies
    assert kinds(first) == ["TextMessageV2", "TextMessageV2"]  # the covered calendar item is not in the request
    assert kinds(second) == ["TextMessageV2", "TextMessage"]
    assert texts(second)[0] == texts(first)[0]  # untouched
    named_action = "拿乾洗衣物" if "拿乾洗衣物" in texts(first)[1] else "拿生日蛋糕"
    assert named_action in texts(second)[1] and texts(second)[1].startswith("@")  # its own plain twin
    assert claims() == []


def test_a_refused_plain_rider_is_fenced_without_a_finalization_error(line, riding, monkeypatch, caplog):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), bad(1)]
    caplog.set_level(logging.DEBUG)

    assert say("嗨") is True

    assert claims() == [(str(rid), "uncertain")]
    assert "finalization failed" not in caplog.text
    assert "reply rider plain text not taken either; fenced riders=1" in caplog.text


def test_a_reminder_only_reply_whose_plain_resend_finds_the_token_gone_closes_the_event(line, riding, monkeypatch):
    """(N3) The recovery returns the resend's own error: a dead token closes the event, so no model reruns it."""
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(0), BAD_TOKEN]

    assert say("Gemini 暫時忙不過來") is False

    assert len(line.replies) == 2 and not line.pushes
    assert claims() == [(str(rid), "uncertain")]
    assert status() == "completed_no_reply"


def test_the_menu_buttons_hang_on_the_plain_reminder_of_a_reminder_only_reply(line, riding, monkeypatch):
    due_natural(monkeypatch)
    open_event()
    line.script = [bad(0)]

    assert say("Gemini 暫時忙不過來", menu_buttons=True) is True

    first, second = line.replies
    assert first[-1].quick_reply is not None
    assert kinds(second) == ["TextMessage"] and second[-1].quick_reply is not None


def test_a_riders_repr_never_carries_its_plain_text():
    """(N7) A future `%r` of a rider must not print family words."""
    import reply_riders

    rider = reply_riders.Rider("natural", "reminder:1", plain_text="繳哨兵稅款")

    assert "繳哨兵稅款" not in repr(rider)


# ── review PRE_2 additions ──────────────────────────────────────────────────────────────────


def test_a_suppressed_rejected_receipt_with_an_unnamed_refusal_releases_its_reminder(line, riding, monkeypatch):
    """(PRE_2 I3) The riders' recovery now runs for a suppressed rejected receipt: an unnamed 400 releases the
    reminder, as for every other reminder-only reply (before 2026-11-14 it was fenced); the event still ends."""
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [BAD_UNNAMED]

    assert say(DEGRADED_RECEIPT) is False

    assert len(line.replies) == 1 and not line.pushes
    assert claims() == [] and pushed_1hr(rid) == 0  # released: it rides again
    assert status() == "completed_no_reply"


def test_a_fast_path_plain_resend_that_raises_logs_no_words(line, riding, monkeypatch, caplog):
    """(PRE_2 I4) An SDK-side exception's text can carry the request: only its type may reach the log."""
    rid = due_natural(monkeypatch, "繳哨兵稅款")
    line.script = [bad(0), ValueError("繳哨兵稅款 成員甲")]
    caplog.set_level(logging.DEBUG)

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    assert claims() == [(str(rid), "uncertain")]
    assert "plain resend failed (reminders fenced): error_type=ValueError" in caplog.text
    assert "繳哨兵稅款" not in caplog.text and "成員甲" not in caplog.text


def test_a_reply_whose_plain_resend_raises_logs_no_words(line, riding, monkeypatch, caplog):
    rid = due_natural(monkeypatch, "繳哨兵稅款")
    open_event()
    line.script = [bad(1), ValueError("繳哨兵稅款 成員甲")]
    caplog.set_level(logging.DEBUG)

    assert say("嗨") is False

    assert kinds(line.replies[1]) == ["TextMessage", "TextMessage"]  # call 2 did carry the plain reminder
    assert claims() == [(str(rid), "uncertain")] and not line.pushes
    assert "繳哨兵稅款" not in caplog.text and "成員甲" not in caplog.text


@pytest.mark.parametrize("kind", ["textV2", "mention"])
def test_a_calendar_spec_that_pings_someone_carries_its_plain_twin(kind):
    """(POST_2 N1) The one-off config kind `mention` is a textV2 on the reply path too."""
    assert main._reminder_spec_plain_text({"kind": kind, "fallback_text": "⏰ 提醒"}) == "⏰ 提醒"
    assert main._reminder_spec_plain_text({"kind": kind, "fallback_text": ""}) is None


def test_a_calendar_spec_that_is_plain_text_already_has_no_plain_twin():
    assert main._reminder_spec_plain_text({"kind": "text", "text": "⏰ 提醒"}) is None


# ── review POST_1 additions ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("second", [bad(1), BAD_TOKEN], ids=["refused-again", "token-gone"])
def test_a_plain_rider_whose_index_shifted_is_fenced_when_its_resend_is_not_taken(line, riding, monkeypatch, caplog, second):
    """(POST_1 I1) The named reminder sits after a rider that is released, so in the resend it is at index 1:
    the fence must follow it there (counted again after the first removal) -- never finalized, never released."""
    first = due_natural(monkeypatch, "繳房屋稅", user="U_A")
    other = due_natural(monkeypatch, "拿乾洗衣物", user="U_B")
    plain = {int(i["reminder_id"]): i["text"] for i in reminder_push.due_reminders_for_reply(GROUP, limit=4)}
    open_event()
    line.script = [bad(2), second]  # the second reminder is named; its plain twin is not taken either
    caplog.set_level(logging.DEBUG)

    say("嗨")

    assert kinds(line.replies[0]) == ["TextMessage", "TextMessageV2", "TextMessageV2"]
    assert kinds(line.replies[1]) == ["TextMessage", "TextMessage"]
    [named] = [rid for rid in (first, other) if plain[rid] == texts(line.replies[1])[1]]
    released = ({first, other} - {named}).pop()
    assert claims() == [(str(named), "uncertain")]  # fenced where it now sits; the other one released
    assert pushed_1hr(named) == 0 and pushed_1hr(released) == 0
    assert "finalization failed" not in caplog.text


def test_the_fast_path_ignores_an_index_past_the_end(line, riding, monkeypatch):
    """(POST_1 N2) An index LINE names that is not in the request is dropped, not a reason to fence."""
    rid = due_natural(monkeypatch)
    line.script = [api_error(400, refused(0, 7))]

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is True

    assert [kinds(batch) for batch in line.replies] == [["TextMessageV2"], ["TextMessage"]]
    assert pushed_1hr(rid) == 1 and claims() == []


def test_a_suppressed_rejected_receipt_whose_resend_gets_no_answer_keeps_the_fence(line, riding, monkeypatch):
    """(POST_1 N3) The resend may have been delivered: the event is not closed over it."""
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "")
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(0), TimeoutError("read timed out")]

    assert say(DEGRADED_RECEIPT) is False

    assert len(line.replies) == 2 and not line.pushes
    assert claims() == [(str(rid), "uncertain")]
    assert status() == "reply_sending"


def test_the_counted_log_lines_reach_the_production_log_level(line, riding, monkeypatch, caplog):
    """(POST_1 N4) The rule entry counts these lines to learn whether a refusal spends the token: they must
    keep their words and stay at INFO or above (production logs at INFO)."""
    due_natural(monkeypatch)
    open_event()
    line.script = [bad(1)]
    caplog.set_level(logging.DEBUG)

    assert say("嗨") is True

    def at_info(fragment):
        return [r for r in caplog.records if fragment in r.getMessage() and r.levelno >= logging.INFO]

    assert at_info("reply rider goes once more as plain text")
    assert at_info("reply accepted with 1 refused reminder(s) as plain text")


def test_the_counted_fast_path_log_lines_reach_the_production_log_level(line, riding, monkeypatch, caplog):
    due_natural(monkeypatch)
    line.script = [bad(0), bad(0)]
    caplog.set_level(logging.DEBUG)

    assert main._try_piggyback_reminders_fast_path(TOKEN, GROUP) is False

    def at_info(fragment):
        return [r for r in caplog.records if fragment in r.getMessage() and r.levelno >= logging.INFO]

    assert at_info("once more as plain text")
    assert at_info("fast-path plain resend failed (reminders fenced)")


def test_a_plain_twin_is_clipped_to_4900_utf16_units():
    """(POST_1 N7) What LINE accepts for one text message; no real twin comes near it."""
    plain = main._plain_reminder_message(_textv2(), "😀" * 2600)

    assert len(plain.text.encode("utf-16-le")) // 2 <= 4900
