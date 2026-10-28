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
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from linebot.v3.messaging.exceptions import ApiException

import line_errors
import line_mentions
import main
import memory
import reminder_push

GROUP = "GRP-400"
TOKEN = "TOKEN-SYNTHETIC-0400"
TZ = ZoneInfo("Asia/Taipei")


# ── fake LINE ───────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status, reason, data, headers):
        self.status, self.reason, self.data, self._headers = status, reason, data, headers

    def getheaders(self):
        return self._headers


def api_error(status, body=None, reason="Bad Request"):
    payload = json.dumps(body if body is not None else {"message": "x"}).encode()
    return ApiException(http_resp=_Resp(status, reason, payload, {"x-line-request-id": "req-400"}))


def refused(*indexes, prop="text"):
    """LINE's 400 body naming the refused message(s)."""
    return {
        "message": f"The request body has {len(indexes)} error(s)",
        "details": [{"message": "Invalid", "property": f"messages[{i}].{prop}"} for i in indexes],
    }


def bad(*indexes, prop="text"):
    return api_error(400, refused(*indexes, prop=prop))


BAD_UNNAMED = api_error(400, {"message": "The request body could not be understood"})
BAD_TOKEN = api_error(400, {"message": "Invalid reply token"})
MONTHLY = {"message": "You have reached your monthly limit."}


class FakeLine:
    """reply_message / push_message of a scripted LINE.  ``script`` holds the outcome of each
    reply_message call in turn (an exception to raise; None accepts); ``push_script`` the same for
    push_message.  Every call is recorded."""

    def __init__(self):
        self.script: list = []
        self.push_script: list = []
        self.replies: list[list] = []
        self.pushes: list[tuple[list, str | None]] = []
        self._ids = iter(range(1, 10**6))

    def reply_message(self, request):
        self.replies.append(list(request.messages))
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome
        return SimpleNamespace(sent_messages=[SimpleNamespace(id=f"sent-{next(self._ids)}") for _ in request.messages])

    def push_message(self, request, x_line_retry_key=None):
        self.pushes.append((list(request.messages), x_line_retry_key))
        outcome = self.push_script.pop(0) if self.push_script else None
        if outcome is not None:
            raise outcome
        return SimpleNamespace(sent_messages=[SimpleNamespace(id=f"push-{next(self._ids)}") for _ in request.messages])


class _FakeApiClient:
    def __init__(self, _config):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _synthetic_aliases(tmp_path, monkeypatch):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"U_A": "成員甲", "U_B": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_path / "no_roles.json"))


@pytest.fixture()
def line(monkeypatch):
    fake = FakeLine()
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)
    monkeypatch.setattr(main, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(main, "MessagingApi", lambda _client: fake)
    monkeypatch.setattr(main, "_get_line_config", lambda: object())
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    return fake


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


def kinds(batch):
    return [type(message).__name__ for message in batch]


def texts(batch):
    return [str(getattr(message, "text", "") or "") for message in batch]


# ── reminders, acknowledgements, claims ────────────────────────────────────────────────────


def _day(offset):
    return (datetime.now(TZ) + timedelta(days=offset)).date()


def _at(day, hour, minute=0):
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).timestamp())


def due_natural(monkeypatch, action="繳房屋稅", *, user="U_A", day_offset=3, group=GROUP):
    """A natural reminder due for its 1 hour notice right now.  Returns its id."""
    day = _day(day_offset)
    rid, outcome = memory.add_reminder_with_outcome(
        group, user, action, _at(day, 14), source_text=f"{day.month}/{day.day} 14:00 {action}", time_kind="clock"
    )
    assert outcome == "created"
    monkeypatch.setattr(reminder_push, "_now_ts", lambda: _at(day, 13))
    return rid


def claims():
    """(subject_ref, state) of every delivery claim row."""
    with memory._conn() as c:
        return sorted(c.execute("SELECT subject_ref, state FROM reminder_delivery_claims").fetchall())


def pushed_1hr(rid):
    with memory._conn() as c:
        return int(c.execute("SELECT pushed_1hr FROM reminders WHERE reminder_id=?", (rid,)).fetchone()[0])


def confirmation_rows():
    with memory._conn() as c:
        return c.execute("SELECT source_ref, status FROM reminder_confirmation_outbox ORDER BY confirmation_id").fetchall()


def raw_bot_texts():
    with memory._conn() as c:
        return [row[0] for row in c.execute("SELECT text FROM raw_messages WHERE user_id='__bot__' ORDER BY rowid").fetchall()]


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


def test_a_refusal_that_names_no_message_releases_every_rider(line, riding, monkeypatch):
    first = due_natural(monkeypatch, "繳房屋稅", user="U_A")
    second = due_natural(monkeypatch, "拿乾洗衣物", user="U_B")
    open_event()
    line.script = [BAD_UNNAMED]

    assert say("嗨") is True

    assert kinds(line.replies[1]) == ["TextMessage"] and texts(line.replies[1]) == ["嗨"]
    assert claims() == []  # released, not fenced
    assert pushed_1hr(first) == 0 and pushed_1hr(second) == 0  # both ride again


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
    rid = due_natural(monkeypatch, "繳房屋稅", user="U_STRANGER")  # no alias for this creator
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
