"""B6.3 / B6.5 (LINE bot fixes batch 2, 2026-10-24).  Synthetic ids and texts only.

LINE answers a *text* reply with HTTP 400 when one of its messages is invalid and names the message
in `details[].property` (`messages[2].text`).  Before: every rider's claim was fenced as `uncertain`
(a reminder that never arrives again) and the answer itself was lost, because a content 400 is not a
token error and so never reached the push fallback.  Now `_reply` finds the refused message, leaves
it out, and settles each rider's claim exactly once:

* only riders are named (a reminder, an acknowledgement, the nudge): they are fenced, every other
  rider is released unsent, and the primary goes alone as it was built (refused after all: once as
  plain text);
* the primary is named, or LINE names nothing: every rider is released (not fenced) and the primary
  goes alone as one plain TextMessage;
* a rider is never sent again as plain text: its plain version starts with the label of the person
  it pings, the literal 「@當事人」 when the creator has no local alias;
* ambiguous at any step -> the fence stays, nothing is pushed; refused everywhere -> a push, only
  where batch 1's fence rules allow one.

Menu / large-menu / Flex command cards never fall back to plain text (see test_inbound_send_fence).
B6.5: literal braces in a textV2 body, UTF-16 lengths, the bot's own user id as a mention target,
and the archive of a textV2 message.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

import pytest
from linebot.v3.messaging.exceptions import ApiException

import line_errors
import line_mentions
import main
import memory
import reminder_push
from test_support_fake_line import (
    BAD_TOKEN,
    BAD_UNNAMED,
    GROUP,
    MONTHLY,
    _at,
    _day,
    _Resp,
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

TOKEN = "TOKEN-SYNTHETIC-0400"


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
    """Turn the due-reminder piggyback on (steps 2 and 3 of `_reply`)."""
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)


def open_event(message_id="M1", token=TOKEN, group=GROUP, *, expect="new"):
    assert memory.begin_inbound_event(group, message_id) == expect
    main._register_inbound_reply_token(token, group, message_id)
    return message_id


def status(message_id="M1", group=GROUP):
    return memory.get_inbound_event_status(group, message_id)


def say(text="好的", *, token=TOKEN, group=GROUP, **kwargs):
    return main._reply(token, text, group_id=group, **kwargs)


# ── reminders, acknowledgements, claims ────────────────────────────────────────────────────


def confirmation_rows():
    with memory._conn() as c:
        return c.execute("SELECT source_ref, status FROM reminder_confirmation_outbox ORDER BY confirmation_id").fetchall()


# ═══ line_errors: which message did LINE refuse? ═════════════════════════════════════════════


def test_the_refused_message_indexes_come_from_the_details():
    exc = api_error(400, refused(2, 0, 2))
    assert line_errors.rejected_message_indexes(exc) == [0, 2]
    exc = api_error(400, {"message": "x", "details": [{"property": "messages[1].substitution.p1.mentionee.userId"}]})
    assert line_errors.rejected_message_indexes(exc) == [1]


@pytest.mark.parametrize(
    "body",
    [
        {"message": "no details"},
        {"message": "x", "details": []},
        {"message": "x", "details": "messages[0]"},
        {"message": "x", "details": [{"property": "replyToken"}, {"property": None}, "messages[1]", 7]},
        {"message": "x", "details": [{"property": "xmessages[1].text"}]},
        ["messages[0].text"],
    ],
)
def test_a_body_that_names_no_message_gives_no_index(body):
    assert line_errors.rejected_message_indexes(api_error(400, body)) == []


def test_non_json_bodies_and_other_exceptions_give_no_index():
    assert line_errors.rejected_message_indexes(TimeoutError("read timed out")) == []
    html = ApiException(http_resp=_Resp(400, "Bad Request", b"<html>messages[0].text</html>", {}))
    assert line_errors.rejected_message_indexes(html) == []
    assert line_errors.is_bad_request(html) and not line_errors.is_bad_request(api_error(429))


def test_hostile_or_oversized_bodies_are_read_quickly_and_name_no_index():
    # (H5) one anchored match with a bounded quantifier per property, at most 100 properties
    hostile = {"message": "x", "details": [{"property": "messages[" * 40}] * 20}
    huge = {"message": "x", "details": [{"property": "messages[3].text"}] + [{"property": "messages[" * 4000}] * 100}
    for body in (hostile, huge):
        started = time.perf_counter()
        indexes = line_errors.rejected_message_indexes(api_error(400, body))
        assert time.perf_counter() - started < 0.5
        assert indexes == []  # (the huge body is cut at 8000 characters, so it is not even valid JSON)
    assert line_errors.rejected_message_indexes(api_error(400, {"message": "x", "details": [{"property": "messages[3].text"}] * 100})) == [3]


# ═══ the primary message is refused ══════════════════════════════════════════════════════════


def test_a_refused_primary_is_sent_again_once_as_one_plain_text_message(line):
    open_event()
    line.script = [bad(0)]

    assert say("原本的回答") is True

    assert len(line.replies) == 2 and not line.pushes
    assert kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == ["原本的回答"]
    assert getattr(line.replies[1][0], "quote_token", None) in (None, "")
    assert status() == "replied"
    assert raw_bot_texts() == ["原本的回答"]


def test_the_plain_resend_drops_the_mention_and_the_placeholder(line):
    open_event()
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")
    main._reply_mention_targets_by_token[TOKEN] = [target]
    line.script = [bad(0, prop="substitution")]

    assert say("@成員甲 這是回答") is True

    assert kinds(line.replies[0]) == ["TextMessageV2"] and texts(line.replies[0])[0].startswith("{p1}")
    assert kinds(line.replies[1]) == ["TextMessage"]
    assert "{p1}" not in texts(line.replies[1])[0] and "這是回答" in texts(line.replies[1])[0]


def test_a_refusal_that_names_no_message_is_handled_like_a_refused_primary(line):
    open_event()
    line.script = [BAD_UNNAMED]

    assert say("沒說是哪一則") is True

    assert kinds(line.replies[1]) == ["TextMessage"] and len(line.replies) == 2
    assert status() == "replied"


def test_the_plain_resend_is_clipped_to_4900_utf16_units(line):
    open_event()
    line.script = [bad(0)]
    long_text = "😀" * 2600  # 5200 UTF-16 units

    assert say(long_text) is True

    sent = texts(line.replies[1])[0]
    assert line_mentions.utf16_len(sent) <= 4900 and sent == "😀" * 2450


def test_a_refused_primary_whose_resend_is_refused_too_is_pushed_as_plain_text(line):
    open_event()
    line.script = [bad(0), bad(0)]

    assert say("慢慢來的回答") is True

    assert len(line.replies) == 2 and len(line.pushes) == 1
    messages, key = line.pushes[0]
    assert kinds(messages) == ["TextMessage"] and texts(messages) == ["慢慢來的回答"]
    assert key == main._reply_push_retry_key(GROUP, main._InboundSendFence(GROUP, ("M1",), False), TOKEN)
    assert status() == "replied"


def test_a_resend_that_finds_the_token_gone_is_pushed_as_plain_text(line):
    """LINE may spend the token on the refusal; then the push is all that is left."""
    open_event()
    line.script = [bad(0), BAD_TOKEN]

    assert say("token 被燒掉了") is True

    assert len(line.pushes) == 1 and kinds(line.pushes[0][0]) == ["TextMessage"]
    assert status() == "replied"


def test_a_refused_push_ends_the_event(line):
    open_event()
    line.script = [bad(0), bad(0)]
    line.push_script = [api_error(429, MONTHLY, "Too Many Requests")]

    assert say() is False

    assert len(line.pushes) == 1
    assert status() == "completed_no_reply"  # push 4xx is final (batch 1)


def test_no_push_allowed_lifts_the_fence_and_keeps_the_event_alive(line):
    open_event()
    line.script = [bad(0), bad(0)]

    assert say(allow_push_fallback=False) is False

    assert len(line.replies) == 2 and not line.pushes
    assert status() == "processing"  # a redelivery may answer it with a fresh token


def test_a_market_quote_is_never_pushed(line):
    open_event()
    line.script = [bad(0), bad(0)]

    assert say("【市場報價】合成行情") is False

    assert not line.pushes and status() == "processing"


def test_a_lease_retry_run_never_pushes_and_does_not_close_the_event(line, monkeypatch):
    state = {"t": 1_800_000_000.0}
    monkeypatch.setattr(memory._time, "time", lambda: state["t"])
    open_event()
    state["t"] += 200
    open_event(expect="retry")
    line.script = [bad(0), bad(0)]

    assert say() is False

    assert not line.pushes and status() == "processing"


def test_an_ambiguous_resend_keeps_the_fence_and_never_pushes(line):
    open_event()
    line.script = [bad(0), TimeoutError("read timed out")]

    assert say() is False

    assert len(line.replies) == 2 and not line.pushes
    assert status() == "reply_sending"
    assert memory.claim_inbound_reply(GROUP, ["M1"]) == ("blocked", False)


@pytest.mark.parametrize("failure", [api_error(500, {"message": "oops"}, "Internal Server Error"), ConnectionError("reset")])
def test_other_ambiguous_resends_also_keep_the_fence(line, failure):
    open_event()
    line.script = [bad(0), failure]

    assert say() is False

    assert not line.pushes and status() == "reply_sending"


def test_an_ambiguous_first_attempt_is_not_recovered_at_all(line):
    open_event()
    line.script = [TimeoutError("read timed out")]

    assert say() is False

    assert len(line.replies) == 1 and not line.pushes and status() == "reply_sending"


@pytest.mark.parametrize("code", [401, 403, 404, 413, 429])
def test_a_4xx_that_is_not_a_400_is_not_recovered(line, code):
    open_event()
    line.script = [api_error(code, {"message": "no"}, "No")]

    assert say() is False

    assert len(line.replies) == 1 and not line.pushes and status() == "processing"


def test_a_dead_token_on_the_first_attempt_is_not_a_content_refusal(line):
    open_event()
    line.script = [BAD_TOKEN]

    assert say("token 一開始就壞了") is True

    assert len(line.replies) == 1 and len(line.pushes) == 1
    # the token was refused, not the words: the push carries the usual mention-aware message
    assert texts(line.pushes[0][0]) == ["token 一開始就壞了"]


def test_a_rejected_degraded_receipt_is_not_recovered(line, monkeypatch):
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "\n— 小提醒")
    degraded = "這個圖片我這次沒分析成功，請稍後再傳一次🙏"
    open_event()
    line.script = [bad(0)]

    assert say(degraded) is False

    assert len(line.replies) == 1 and not line.pushes
    assert status() == "completed_no_reply"


@pytest.mark.parametrize("card", [{"menu_card": True}, {"large_menu_card": True}])
def test_menu_cards_are_never_recovered_as_plain_text(line, card):
    open_event()
    line.script = [bad(0)]

    assert say("選單", **card) is False

    assert len(line.replies) == 1 and not line.pushes and status() == "processing"


# ═══ a rider is refused ══════════════════════════════════════════════════════════════════════
#
# A rider is fenced when LINE names it and then left out; the other riders are released unsent; the
# primary goes alone, as it was built.  A rider is never sent again as plain text (below).


def arm_mention():
    """The primary mentions someone, so it is a textV2 message (and a plain resend would be visible)."""
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")
    main._reply_mention_targets_by_token[TOKEN] = [target]
    return "@成員甲 嗨"


def test_a_named_reminder_is_fenced_and_the_primary_goes_alone_as_it_was_built(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1)]

    assert say(arm_mention()) is True

    assert len(line.replies) == 2 and not line.pushes
    first, second = line.replies
    assert kinds(first) == ["TextMessageV2", "TextMessageV2"]
    assert kinds(second) == ["TextMessageV2"] and texts(second) == texts(first)[:1]  # same message, mention and all
    assert pushed_1hr(rid) == 0
    assert claims() == [(str(rid), "uncertain")]  # fenced: it cannot ride again
    assert status() == "replied"
    assert len(raw_bot_texts()) == 1  # only what was sent is archived


def test_a_fenced_reminder_does_not_ride_on_the_next_reply(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1)]
    assert say("嗨") is True
    line.replies.clear()

    open_event("M2", token="TOKEN-SECOND")
    assert say("再一次", token="TOKEN-SECOND") is True

    assert len(line.replies) == 1 and texts(line.replies[0]) == ["再一次"]  # no rider, no extra call
    assert claims() == [(str(rid), "uncertain")]


def test_the_other_riders_are_released_when_one_is_fenced(line, riding, monkeypatch):
    bad_one = due_natural(monkeypatch, "繳房屋稅", user="U_A")
    good_one = due_natural(monkeypatch, "拿乾洗衣物", user="U_B")
    open_event()
    line.script = [bad(1)]  # the first reminder in the batch

    assert say("嗨") is True

    assert kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == ["嗨"]
    fenced = [subject for subject, state in claims() if state == "uncertain"]
    assert len(fenced) == 1 and len(claims()) == 1  # the other claim was released, not fenced
    released = ({str(bad_one), str(good_one)} - set(fenced)).pop()
    assert pushed_1hr(int(released)) == 0  # still due: it rides on a later reply


def test_a_named_reminder_and_a_refused_primary_send_the_primary_as_plain_text(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), bad(0)]  # LINE did not name the primary the first time

    assert say(arm_mention()) is True

    assert [kinds(batch) for batch in line.replies] == [
        ["TextMessageV2", "TextMessageV2"], ["TextMessageV2"], ["TextMessage"],
    ]
    assert "{p1}" not in texts(line.replies[2])[0]
    assert claims() == [(str(rid), "uncertain")] and not line.pushes
    assert status() == "replied"


def test_everything_refused_is_pushed_as_plain_text_once(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), bad(0), bad(0)]

    assert say(arm_mention()) is True

    assert len(line.replies) == 3 and len(line.pushes) == 1
    assert kinds(line.pushes[0][0]) == ["TextMessage"]
    assert claims() == [(str(rid), "uncertain")]
    assert status() == "replied"


def test_a_dead_token_after_a_named_rider_pushes_the_primary_in_its_usual_form(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), BAD_TOKEN]

    assert say(arm_mention()) is True

    assert len(line.pushes) == 1 and kinds(line.pushes[0][0]) == ["TextMessageV2"]  # the primary was never accused
    assert claims() == [(str(rid), "uncertain")]
    assert status() == "replied"


def test_an_ambiguous_resend_keeps_the_fence_and_the_fenced_rider_stays_fenced(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), TimeoutError("read timed out")]

    assert say("嗨") is False

    assert not line.pushes and status() == "reply_sending"
    assert claims() == [(str(rid), "uncertain")]


def test_a_bug_in_the_recovery_is_an_ambiguous_send_and_never_a_second_copy(line, riding, monkeypatch):
    """(R2 GP1 #1) `_recover_rejected_reply` raises: whatever it already sent is unknown, so the reply
    is handled as ambiguous -- the fence stays, nothing is pushed, the riders are fenced.  Flipping
    that outcome to "rejected" would release the claims and push after a possibly-delivered call."""
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1)]

    def broken(**_kwargs):
        raise RuntimeError("a bug in the recovery")

    monkeypatch.setattr(main, "_recover_rejected_reply", broken)

    assert say(arm_mention()) is False

    assert len(line.replies) == 1 and not line.pushes  # no resend, no push
    assert status() == "reply_sending"  # the fence stays
    assert memory.claim_inbound_reply(GROUP, ["M1"]) == ("blocked", False)
    assert claims() == [(str(rid), "uncertain")]  # fenced, not released: it must not ride again
    assert pushed_1hr(rid) == 0


def test_a_bug_halfway_through_the_recovery_is_never_followed_by_a_push(line, riding, monkeypatch):
    """The same promise one level down: the first resend was refused, the next step raises -- what
    LINE did with the calls so far is not known to the caller, so still no push."""
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), bad(0)]

    def boom(_text):
        raise RuntimeError("a bug after the first resend")

    monkeypatch.setattr(main, "_sanitized_plain_text_message", boom)

    assert say(arm_mention()) is False

    assert len(line.replies) == 2 and not line.pushes and status() == "reply_sending"
    assert claims() == [(str(rid), "uncertain")]


def test_a_resend_that_cannot_even_be_built_sent_nothing_and_ends_in_a_plain_push(line, riding, monkeypatch):
    """(R2 GP1 #1, `_reply_attempt`) a request that cannot be built never reached LINE, so the step
    counts as refused -- not as ambiguous: the usual end of "refused everywhere" follows, one plain
    push where batch 1's fence rules allow it.  Flipping it to "ambiguous" would drop the reply."""
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1)]
    real_request = main.ReplyMessageRequest
    builds = []

    def build(**kwargs):
        builds.append(1)
        if len(builds) > 1:  # the first build is `_reply`'s own; every resend fails
            raise ValueError("cannot build the resend")
        return real_request(**kwargs)

    monkeypatch.setattr(main, "ReplyMessageRequest", build)

    assert say(arm_mention()) is True

    assert len(line.replies) == 1  # the resends never left
    assert len(line.pushes) == 1 and kinds(line.pushes[0][0]) == ["TextMessage"]
    assert claims() == [(str(rid), "uncertain")] and status() == "replied"


def test_a_refusal_that_names_no_message_releases_every_rider(line, riding, monkeypatch):
    first = due_natural(monkeypatch, "繳房屋稅", user="U_A")
    second = due_natural(monkeypatch, "拿乾洗衣物", user="U_B")
    open_event()
    line.script = [BAD_UNNAMED]

    assert say("嗨") is True

    assert kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == ["嗨"]
    assert claims() == []  # released, not fenced
    assert pushed_1hr(first) == 0 and pushed_1hr(second) == 0  # both ride again


def _refused_unnamed(line, n, text="嗨"):
    """One more reply on which LINE refuses everything it is given without naming a message; the
    resend of the primary alone goes through."""
    open_event(f"M{n}", token=f"TOKEN-{n}")
    line.script = [BAD_UNNAMED]
    assert say(text, token=f"TOKEN-{n}") is True


def test_a_rider_refused_without_a_name_on_three_replies_in_a_row_is_fenced(line, riding, monkeypatch):
    """(R2 GP1 #3, decided by the session owner) an unnamed refusal releases the riders, but one that
    is in such a refusal on three replies in a row is the likely culprit: fenced, so it cannot cost
    an extra failed call and a lost mention on every later reply for ever."""
    rid = due_natural(monkeypatch)

    for n in (1, 2):
        _refused_unnamed(line, n)
        assert claims() == [] and pushed_1hr(rid) == 0  # released: it rides again
        assert len(line.replies[-2]) == 2  # (the refused call carried it)
    _refused_unnamed(line, 3)

    assert claims() == [(str(rid), "uncertain")]  # the third time: fenced
    line.replies.clear()
    open_event("M4", token="TOKEN-4")
    assert say("再一次", token="TOKEN-4") is True
    assert len(line.replies) == 1 and texts(line.replies[0]) == ["再一次"]  # it does not ride any more


def test_the_run_of_unnamed_refusals_is_counted_for_each_rider_on_its_own(line, riding, monkeypatch):
    first = due_natural(monkeypatch, "繳房屋稅", user="U_A")
    _refused_unnamed(line, 1)  # first: 1
    second = due_natural(monkeypatch, "拿乾洗衣物", user="U_B")
    _refused_unnamed(line, 2)  # first: 2, second: 1
    _refused_unnamed(line, 3)  # first: 3 -> fenced, second: 2 -> released

    assert claims() == [(str(first), "uncertain")]
    assert pushed_1hr(second) == 0


def test_a_delivered_ride_ends_the_run_of_unnamed_refusals(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    _refused_unnamed(line, 1)
    _refused_unnamed(line, 2)
    open_event("M3", token="TOKEN-3")
    # this time LINE accepts the batch with the reminder in it
    assert say("嗨", token="TOKEN-3") is True
    assert pushed_1hr(rid) == 1  # delivered, not fenced
    import reply_riders

    assert reply_riders._unnamed_streak == {}


@pytest.mark.parametrize("breaker", ["named_with_the_primary", "named_alone"])
def test_a_refusal_that_is_not_unnamed_ends_the_run(line, riding, monkeypatch, breaker):
    rid = due_natural(monkeypatch)
    _refused_unnamed(line, 1)
    _refused_unnamed(line, 2)
    open_event("M3", token="TOKEN-3")
    line.script = [bad(0, 1)] if breaker == "named_with_the_primary" else [bad(0)]
    assert say("嗨", token="TOKEN-3") is True  # LINE had its say and did not blame the rider alone
    assert claims() == []

    _refused_unnamed(line, 4)  # a new run: 1, not 3

    assert claims() == [] and pushed_1hr(rid) == 0


def test_the_third_unnamed_refusal_of_a_reminder_only_reply_fences_it_too(line, riding, monkeypatch):
    """A reply of riders only (the primary is suppressed) has nothing else LINE could be refusing."""
    rid = due_natural(monkeypatch)
    for n in (1, 2, 3):
        open_event(f"M{n}", token=f"TOKEN-{n}")
        line.script = [BAD_UNNAMED]
        assert say("Gemini 暫時忙不過來", token=f"TOKEN-{n}") is False
    assert claims() == [(str(rid), "uncertain")]


def test_the_streak_counter_is_bounded_and_a_fenced_rider_starts_again():
    import reply_riders

    reply_riders.reset_counters()
    for n in range(reply_riders._COUNTER_MAX + 50):
        assert reply_riders.note_unnamed_refusal(GROUP, reply_riders.Rider("natural", f"reminder:{n}")) is False
    assert len(reply_riders._unnamed_streak) == reply_riders._COUNTER_MAX  # the oldest entries went first

    rider = reply_riders.Rider("natural", "reminder:7")
    assert [reply_riders.note_unnamed_refusal(GROUP, rider) for _ in range(3)] == [False, False, True]
    assert [reply_riders.note_unnamed_refusal(GROUP, rider) for _ in range(3)] == [False, False, True]  # a run is over once it fences
    reply_riders.clear_unnamed_refusals(GROUP, [None, rider])  # (None is the primary message)
    assert reply_riders.note_unnamed_refusal(GROUP, rider) is False


def test_an_index_that_does_not_exist_is_treated_as_not_named(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(9)]

    assert say("嗨") is True

    assert claims() == [] and pushed_1hr(rid) == 0


def test_a_refused_primary_releases_the_riders_that_were_named_with_it(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(0, 1)]

    assert say("嗨") is True

    assert kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == ["嗨"]
    assert claims() == [] and pushed_1hr(rid) == 0  # released: it rides again


def test_a_reminder_only_reply_fences_the_named_rider_and_has_nothing_left_to_send(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(0)]

    assert say("Gemini 暫時忙不過來") is False  # the primary is suppressed: the reminder rides alone

    assert len(line.replies) == 1 and not line.pushes
    assert claims() == [(str(rid), "uncertain")]
    assert status() == "processing"


def test_a_reminder_only_reply_with_an_unnamed_refusal_releases_and_sends_nothing_more(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [BAD_UNNAMED]

    assert say("Gemini 暫時忙不過來") is False

    assert len(line.replies) == 1 and not line.pushes
    assert claims() == [] and pushed_1hr(rid) == 0


def test_the_references_of_a_primary_sent_alone_are_not_shifted(line, riding, monkeypatch):
    rid = due_natural(monkeypatch)
    open_event()
    line.script = [bad(1)]

    assert say("嗨", primary_reminder_ref={"reminder_id": 4242}) is True

    assert memory.get_sent_reminder_reference(GROUP, "sent-1")["reminder_id"] == 4242  # still bound to the primary
    assert memory.get_sent_reminder_reference(GROUP, "sent-2") is None
    assert claims() == [(str(rid), "uncertain")]


def test_the_menu_buttons_follow_the_last_message_that_is_finally_sent(line, riding, monkeypatch):
    """LINE shows only the last message's Quick Reply; after a recovery that is no longer the rider
    the buttons were first hung on."""
    due_natural(monkeypatch)
    open_event()
    line.script = [bad(1), bad(0)]

    assert say("嗨", menu_buttons=True) is True

    first, second, third = line.replies
    assert first[-1].quick_reply is not None and first[0].quick_reply is None  # on the rider, as always
    assert second[-1].quick_reply is not None and len(second) == 1  # the primary alone, as built
    assert third[-1].quick_reply is not None and kinds(third) == ["TextMessage"]  # the plain text


def test_a_named_calendar_reminder_is_fenced_like_any_other_rider(line, riding):
    import calendar_db

    tomorrow = (_day(1)).isoformat()
    event_id = calendar_db.insert_event(
        group_id=GROUP, title="拿生日蛋糕", event_date=tomorrow, event_time="14:00", location="喜來登", event_type="family_gathering"
    )
    assert event_id
    open_event()
    line.script = [bad(1)]

    assert say("嗨") is True

    assert len(line.replies[0]) == 2 and texts(line.replies[1]) == ["嗨"]
    assert claims() == [(str(event_id), "uncertain")]
    with calendar_db._conn() as c:
        assert c.execute("SELECT reminded_1d FROM events WHERE event_id=?", (event_id,)).fetchone()[0] is None


# ── a rider is never sent again as plain text ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "script",
    [
        [bad(1)],                       # the rider is named: left out, fenced
        [bad(1), bad(0)],               # ... and the primary is refused after all: plain text
        [bad(1), bad(0), bad(0)],       # ... and plain text is refused too: pushed
        [bad(1), BAD_TOKEN],            # ... the token is gone: pushed
        [bad(0, 1)],                    # the primary is named with it
        [BAD_UNNAMED],                  # LINE names nothing
        [BAD_UNNAMED, bad(0)],
    ],
    ids=["named", "named+primary", "named+primary+plain", "named+token", "primary+rider", "unnamed", "unnamed+plain"],
)
def test_no_resend_path_shows_the_plain_text_of_a_reminder(line, riding, monkeypatch, script):
    """The plain version of a reminder starts with the label of the person it pings -- the literal
    「@當事人」 when its creator has no local alias.  textV2 shows a real @ there; as plain text the
    family would read 「@當事人」 in the group."""
    due_natural(monkeypatch, "繳房屋稅", user="U_STRANGER")  # no alias for this creator
    open_event()
    line.script = list(script)

    say(arm_mention())

    first = texts(line.replies[0])
    assert "繳房屋稅" in first[1] and first[1].startswith("{target}")  # the placeholder form, as always
    seen = [text for batch in line.replies[1:] for text in texts(batch)]
    seen += [text for messages, _key in line.pushes for text in texts(messages)] + raw_bot_texts()
    assert seen, "something must have been sent after the refusal"
    assert all("@當事人" not in text and "繳房屋稅" not in text and "提醒（" not in text for text in seen), seen


def test_the_plain_version_of_this_reminder_really_does_start_with_the_unknown_person_label(line, riding, monkeypatch):
    """(The control for the test above: the text it keeps off the group exists.)"""
    due_natural(monkeypatch, "繳房屋稅", user="U_STRANGER")
    item = reminder_push.due_reminders_for_reply(GROUP, limit=4)[0]
    assert item["text"].startswith("@當事人\n")


# ── acknowledgements of new reminders ───────────────────────────────────────────────────────


def _ack(n=1, text="已新增提醒\n時間：2099-10-01 12:00\n事項：繳管理費\n對象：@成員甲"):
    memory.enqueue_reminder_confirmation(GROUP, f"pending_reminder:{n}", text)


def test_a_named_acknowledgement_is_left_out_and_stays_claimed(line):
    _ack()
    open_event()
    line.script = [bad(1)]

    assert say("好的") is True

    assert kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == ["好的"]
    assert confirmation_rows() == [("pending_reminder:1", "sending")]  # fenced: tried again after its claim lapses


def test_a_fenced_acknowledgement_comes_back_after_its_claim_lapses(line, monkeypatch):
    _ack()
    open_event()
    line.script = [bad(1)]
    assert say("好的") is True
    assert memory.claim_reminder_confirmations(GROUP, limit=4) == []  # still claimed

    now = time.time()
    monkeypatch.setattr(memory._time, "time", lambda: now + 601)

    assert [row["source_ref"] for row in memory.claim_reminder_confirmations(GROUP, limit=4)] == ["pending_reminder:1"]


def test_only_the_named_acknowledgement_is_fenced_the_other_goes_back_to_pending(line):
    _ack(1)
    _ack(2, "已新增提醒\n時間：2099-10-02 12:00\n事項：拿包裹\n對象：@成員乙")
    open_event()
    line.script = [bad(1)]  # the first acknowledgement

    assert say("好的") is True

    assert sorted(confirmation_rows()) == [("pending_reminder:1", "sending"), ("pending_reminder:2", "pending")]


def test_a_refused_primary_releases_every_acknowledgement(line):
    _ack(1)
    _ack(2, "已新增提醒\n時間：2099-10-02 12:00\n事項：拿包裹\n對象：@成員乙")
    open_event()
    line.script = [bad(0, 1)]  # the primary and the first acknowledgement are named

    assert say("好的") is True

    assert kinds(line.replies[1]) == ["TextMessage"]
    assert sorted(confirmation_rows()) == [("pending_reminder:1", "pending"), ("pending_reminder:2", "pending")]


def test_an_acknowledgement_is_never_sent_again_as_plain_text(line):
    _ack()
    open_event()
    line.script = [bad(1), bad(0)]

    assert say("好的") is True

    seen = [text for batch in line.replies[1:] for text in texts(batch)] + raw_bot_texts()
    assert all("繳管理費" not in text for text in seen), seen


def test_a_sent_acknowledgement_is_still_deleted_when_nothing_is_refused(line):
    _ack()
    open_event()

    assert say("好的") is True

    assert len(line.replies) == 1 and confirmation_rows() == []


def test_an_acknowledgement_that_keeps_being_refused_by_name_is_given_up_the_third_time(line, caplog, monkeypatch):
    """(R2 GP1 #6) a fenced acknowledgement stays claimed and is claimed again after 600 s, so one
    that LINE refuses every time cost an extra failed call per ten minutes of chat for ever.  The
    third fenced refusal ends it (status `failed`: never claimed again, never queued again)."""
    _ack()
    clock = {"t": time.time()}
    monkeypatch.setattr(memory._time, "time", lambda: clock["t"])
    caplog.set_level(logging.INFO)

    for n in (1, 2, 3):
        open_event(f"M{n}", token=f"TOKEN-{n}")
        line.script = [bad(1)]
        assert say("好的", token=f"TOKEN-{n}") is True
        assert len(line.replies[-2]) == 2  # the acknowledgement rode on this one, and was refused
        if n < 3:
            assert confirmation_rows() == [("pending_reminder:1", "sending")]  # fenced: tried again later
            clock["t"] += 601  # its claim lapses

    assert confirmation_rows() == [("pending_reminder:1", "failed")]
    with memory._conn() as c:
        confirmation_id = c.execute("SELECT confirmation_id FROM reminder_confirmation_outbox").fetchone()[0]
    given_up = [r.getMessage() for r in caplog.records if "given up" in r.getMessage()]
    assert given_up == [f"reply rider given up after 3 refusals kind=confirmation id=confirmation:{confirmation_id}"]
    assert all("繳管理費" not in r.getMessage() for r in caplog.records)  # the id, never the text

    clock["t"] += 601
    line.replies.clear()
    open_event("M9", token="TOKEN-9")
    assert say("再一次", token="TOKEN-9") is True
    assert len(line.replies) == 1 and texts(line.replies[0]) == ["再一次"]  # nothing rides on it any more
    assert memory.enqueue_reminder_confirmation(GROUP, "pending_reminder:1", "已新增提醒\n事項：繳管理費") == confirmation_id
    assert confirmation_rows() == [("pending_reminder:1", "failed")]  # the same acknowledgement is not queued again


def test_an_acknowledgement_that_is_delivered_in_between_is_not_given_up(line, monkeypatch):
    _ack()
    clock = {"t": time.time()}
    monkeypatch.setattr(memory._time, "time", lambda: clock["t"])
    open_event("M1", token="TOKEN-1")
    line.script = [bad(1)]
    assert say("好的", token="TOKEN-1") is True
    clock["t"] += 601
    open_event("M2", token="TOKEN-2")

    assert say("好的", token="TOKEN-2") is True  # LINE accepts it this time

    assert confirmation_rows() == []  # delivered and deleted


def test_a_legacy_pending_rider_is_not_reported_as_fenced(caplog):
    """(R2 GP1 #6) it holds a commit id and nothing else: leaving the batch leaves it pending, so
    the log must say released, whatever the caller asked for."""
    import reply_riders

    rider = reply_riders.Rider("pending", "pending:m1", commit_id="m1")
    batch = reply_riders.Batch(
        group_id=GROUP, messages=["primary", "pending"], refs=[None, None], riders=[None, rider],
        event_claims=[], natural_claims=[], confirmation_claims=[], commit_ids=["m1"],
    )
    caplog.set_level(logging.INFO)

    batch.remove_riders({1}, fence={1})

    assert batch.messages == ["primary"] and batch.commit_ids == []
    assert "reply rider released kind=pending id=pending:m1" in caplog.text
    assert "fenced" not in caplog.text


# ── a reminder folded into another one ─────────────────────────────────────────────────────


def _natural_and_later_mirror(monkeypatch, action="施打流感疫苗和新冠疫苗", title="打流感疫苗"):
    import calendar_db

    day = _day(5)  # no calendar offset is due five days out
    rid, outcome = memory.add_reminder_with_outcome(
        GROUP, "U_A", action, _at(day, 14), source_text=f"{day.month}/{day.day} 14:00 {action}", time_kind="clock"
    )
    assert outcome == "created"
    event_id = calendar_db.insert_event(group_id=GROUP, title=title, event_date=day.isoformat(), event_time="14:00")
    assert event_id
    with memory._conn() as c:
        mirror = int(
            c.execute(
                "SELECT reminder_id FROM reminders WHERE group_id=? AND source_kind='calendar_event' AND source_ref=?",
                (GROUP, event_id),
            ).fetchone()[0]
        )
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: _at(day, 13))
    return rid, mirror


def test_the_claim_of_a_folded_reminder_is_fenced_with_its_carrier(line, riding, monkeypatch):
    natural, mirror = _natural_and_later_mirror(monkeypatch)
    open_event()
    line.script = [bad(1)]

    assert say("嗨") is True

    assert len(line.replies[0]) == 2  # one message for the natural reminder and its mirror
    assert claims() == sorted([(str(natural), "uncertain"), (str(mirror), "uncertain")])
    assert pushed_1hr(natural) == 0 and pushed_1hr(mirror) == 0


def test_the_claim_of_a_folded_reminder_is_released_with_its_carrier(line, riding, monkeypatch):
    natural, mirror = _natural_and_later_mirror(monkeypatch)
    open_event()
    line.script = [BAD_UNNAMED]

    assert say("嗨") is True

    assert claims() == [] and pushed_1hr(natural) == 0 and pushed_1hr(mirror) == 0


@pytest.mark.parametrize(
    "script, fenced",
    [([bad(1)], 1), ([BAD_UNNAMED], 0), ([bad(0)], 0)],
    ids=["carrier-named", "unnamed", "primary-named"],
)
def test_a_claim_no_rider_owns_is_released_when_the_reply_is_recovered(line, riding, monkeypatch, script, fenced):
    """(R2 GP1 #1) a reminder folded into another one keeps its claim with the carrier; when the fold
    itself failed ("rider claim folding skipped") the claim belongs to nobody in the batch.  The
    recovery releases it: left in the batch it would be finalised as delivered once the primary
    went through alone, and a reminder nobody saw would never be sent."""
    import reply_riders

    natural, mirror = _natural_and_later_mirror(monkeypatch)

    def failing_fold(*_args, **_kwargs):
        raise RuntimeError("fold failed")

    monkeypatch.setattr(reply_riders, "fold_claims", failing_fold)
    open_event()
    line.script = list(script)

    assert say("嗨") is True

    assert len(line.replies[0]) == 2  # one message for the pair, as before
    held = claims()
    # only the carrier's own claim can be fenced (when LINE names it); the orphan's is released
    assert len(held) == fenced and all(state == "uncertain" for _subject, state in held)
    assert pushed_1hr(natural) == 0 and pushed_1hr(mirror) == 0  # neither was marked as delivered
    assert texts(line.replies[-1]) == ["嗨"]


def test_the_claim_of_a_calendar_event_covered_by_a_reminder_is_fenced_with_its_carrier(line, riding, monkeypatch):
    import calendar_db

    day = _day(1)
    event_id = calendar_db.insert_event(
        group_id=GROUP, title="拿生日蛋糕", event_date=day.isoformat(), event_time="14:00", participants=["成員甲"]
    )
    assert event_id
    rid, _ = memory.add_reminder_with_outcome(
        GROUP, "U_A", "拿生日蛋糕", _at(day, 14), source_text=f"{day.month}/{day.day} 14:00 拿生日蛋糕", time_kind="clock"
    )
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: _at(day, 13))
    open_event()
    line.script = [bad(1)]

    assert say("嗨") is True

    held = claims()
    assert len(held) == 2 and all(state == "uncertain" for _subject, state in held)  # the event's and the reminder's
    assert len(line.replies[0]) == 2  # the event and its reminder go out as one message
    assert texts(line.replies[-1]) == ["嗨"]


# ── the classification itself ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "named, case, hits",
    [
        ([], "unknown", []),
        ([9], "unknown", []),
        ([0], "primary", []),
        ([0, 2], "primary", [2]),
        ([2], "riders", [2]),
        ([1, 2], "riders", [1, 2]),
    ],
)
def test_the_refusal_is_classified_by_the_messages_it_names(named, case, hits):
    import reply_riders

    riders = [None, reply_riders.Rider("natural", "a"), reply_riders.Rider("natural", "b")]
    assert reply_riders.classify_rejection(named, riders, True) == (case, hits)


def test_without_a_primary_index_zero_is_a_rider():
    import reply_riders

    riders = [reply_riders.Rider("natural", "a")]
    assert reply_riders.classify_rejection([0], riders, False) == ("riders", [0])


# ═══ B6.5: textV2 ═══════════════════════════════════════════════════════════════════════════


def test_literal_braces_in_a_textv2_body_are_escaped():
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")

    data = line_mentions.text_v2_dict('設定 {"a": 1} 與 {x}', [target])

    assert data["text"] == '{p1}\n設定 {{"a": 1}} 與 {{x}}'
    assert set(data["substitution"]) == {"p1"}


def test_plain_labels_are_escaped_too():
    data = line_mentions.text_v2_dict("body", [], ["@a{b}"])
    assert data["text"].startswith("@a{{b}}")


def test_escape_and_unescape_round_trip():
    for body in ["", "沒有大括號", "{", "}", "{{", "}}", "{}{", "}{ {x} }", "a{{b}}c"]:
        assert line_mentions.unescape_text_v2(line_mentions.escape_text_v2(body)) == body


def test_the_template_is_clipped_after_escaping_and_never_inside_a_pair():
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")
    head = len("{p1}\n")

    for budget in range(head, head + 12):
        data = line_mentions.text_v2_dict("{}" * 20, [target], limit=budget)
        body = data["text"][head:]
        assert line_mentions.utf16_len(data["text"]) <= budget
        assert len(body) == 2 * ((budget - head) // 2)  # every source brace costs two units: whole pairs only
        assert line_mentions.unescape_text_v2(body) == ("{}" * 20)[: len(body) // 2]


def test_utf16_length_and_clip():
    assert line_mentions.utf16_len("a😀b") == 4
    assert line_mentions.clip_utf16("a😀b", 3) == "a😀"
    assert line_mentions.clip_utf16("a😀b", 2) == "a"  # never half an emoji
    assert line_mentions.clip_utf16("abc", 0) == "" and line_mentions.clip_utf16("abc", 10) == "abc"


def test_an_emoji_costs_two_units_when_the_template_is_clipped():
    """(R2 GP1 #1) `_escape_and_clip` counts UTF-16 units: an emoji is 2, a brace is 2 once escaped."""
    clip = line_mentions._escape_and_clip
    assert clip("😀😀😀", 5) == "😀😀" and clip("😀😀😀", 6) == "😀😀😀" and clip("😀😀😀", 3) == "😀"
    assert clip("😀{😀", 5) == "😀{{" and clip("😀{😀", 6) == "😀{{😀"
    assert clip("}}", 3) == "}}" and clip("}}", 4) == "}}}}" and clip("😀", 1) == ""


def test_a_textv2_full_of_emoji_and_braces_stays_inside_5000_utf16_units():
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")
    source = "😀{字}" * 1700  # 6 UTF-16 units per repeat once the braces are doubled: far over the limit

    data = line_mentions.text_v2_dict(source, [target], limit=5000)

    assert 4990 <= line_mentions.utf16_len(data["text"]) <= 5000
    body = data["text"][len("{p1}\n"):]
    assert source.startswith(line_mentions.unescape_text_v2(body))  # a clean prefix: no pair split, no emoji halved
    reply = main._text_message_with_mentions(
        "@成員甲\n" + source, prepared=True, limit=5000, explicit_only=True, reply_targets=[target]
    )[1]
    assert type(reply).__name__ == "TextMessageV2" and line_mentions.utf16_len(reply.text) <= 5000


def test_the_archive_of_an_event_reminder_push_keeps_the_words_not_the_escapes(monkeypatch):
    """(R2 GP1 #1, event_reminder `_post_result`) a textV2 template escapes literal braces; the
    archive is read back by the model as the bot's own words.  A plain text message is stored as is."""
    import event_reminder

    archived = []
    sent = iter(["sent-a", "sent-b"])
    monkeypatch.setattr(event_reminder, "GROUP_ID", GROUP)
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "token")
    monkeypatch.setattr(
        event_reminder, "push_messages",
        lambda *_args, sent_message_ids=None, **_kwargs: sent_message_ids.extend([next(sent), next(sent)]),
    )
    monkeypatch.setattr(memory, "log_raw_message", lambda group, message_id, user, text, **kw: archived.append((message_id, text)))

    result = event_reminder._post_result(
        [
            {"type": "textV2", "text": '{p1}\n活動 {{"a": 1}} 與 {{x}}', "substitution": {}},
            {"type": "text", "text": "純文字 {{留著}}"},
        ]
    )

    assert result == event_reminder.POST_OK
    assert archived == [("sent-a", '{p1}\n活動 {"a": 1} 與 {x}'), ("sent-b", "純文字 {{留著}}")]


def test_a_reply_with_braces_and_a_mention_is_one_valid_textv2_message(line):
    open_event()
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")
    main._reply_mention_targets_by_token[TOKEN] = [target]

    assert say('@成員甲 結果是 {"ok": true}') is True

    sent = line.replies[0][0]
    assert type(sent).__name__ == "TextMessageV2"
    assert sent.text == '{p1}\n@成員甲 結果是 {{"ok": true}}'  # the braces are escaped, {p1} is the placeholder
    assert list(sent.substitution) == ["p1"]


def test_the_archive_keeps_the_words_not_the_escapes(line):
    open_event()
    target = line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲")
    main._reply_mention_targets_by_token[TOKEN] = [target]

    assert say('@成員甲 結果是 {"ok": true}') is True

    archived = raw_bot_texts()
    assert len(archived) == 1 and '{"ok": true}' in archived[0] and "{{" not in archived[0]
    assert archived[0].startswith("{p1}\n")  # the placeholder stays, as it always did


def test_a_brace_free_archive_is_byte_for_byte_what_it_was(line):
    open_event()
    assert say("一般的回答，沒有大括號") is True
    assert raw_bot_texts() == ["一般的回答，沒有大括號"]


def test_the_primary_is_clipped_in_utf16_units_before_the_footer(line, monkeypatch):
    monkeypatch.setattr(main, "_get_quota_footer", lambda: "\n— 😀")
    open_event()

    assert say("字" * 6000) is True

    sent = texts(line.replies[0])[0]
    assert sent.endswith("\n— 😀") and line_mentions.utf16_len(sent) <= 4900


def test_the_bot_itself_is_not_a_mention_target():
    me = SimpleNamespace(user_id="U_BOT", index=0, length=3, is_self=True)
    someone = SimpleNamespace(user_id="U_A", index=4, length=4, is_self=False)
    unknown = SimpleNamespace(user_id="U_B", index=9, length=4)  # no is_self attribute
    message = SimpleNamespace(text="@咪寶 @成員甲 @成員乙", mention=SimpleNamespace(mentionees=[me, someone, unknown]))

    targets = main._extract_reply_payload_targets(message)

    assert [target.user_id for target in targets] == ["U_A", "U_B"]
    as_dicts = SimpleNamespace(
        text="@咪寶 @成員甲",
        mention=SimpleNamespace(mentionees=[{"userId": "U_BOT", "isSelf": True, "index": 0, "length": 3}, {"userId": "U_A", "index": 4, "length": 4}]),
    )
    assert [target.user_id for target in main._extract_reply_payload_targets(as_dicts)] == ["U_A"]
