"""2026-11-13: extract_reminder reads only the six keys its prompt asks for, and checks their values.

``gemini_client.extract_reminder`` used to keep every key of the model's JSON.  A message pasted into
the group can ask the model for any of these, and each one did something:

- ``"mention_aliases": ["all"]`` (or 全家, 大家…) made every push of that reminder ping the whole group,
  and with ``all`` the 「已新增提醒」 receipt too;
- ``"_time_default_kind": "daypart:<any words>"`` with a clock in the text was stored as the time kind
  and printed into the receipt: an @all or an @member there pinged, again on every later duplicate;
- ``_trusted_direct_request`` turned off the chat checks main runs after the model;
- ``_time_was_defaulted`` / ``_date_default_kind`` put a wrong 「未指定…」 note on the receipt.

Every other key is the program's to set, or nobody's, so the whitelist drops them before the program
adds its own.  action must be words, and the five numbers whole numbers from 0 to 9999: an Infinity
raised OverflowError, which main's date check did not catch, so the drain put the row back and
asked the model again on every drain until the row aged out.  A null year is this year, like a
missing one.

Not covered here: the words the model writes into ``action``.  An @all, an @member or a line break
there still reaches the receipt, and every push pings a member it names (the next stage).

These tests drive the real ``extract_reminder`` through a fake client (``tier`` from
test_gemini_last_tier): a test that replaced ``extract_reminder`` itself would never reach the
whitelist.  Every text is synthetic; 測試成員甲 is conftest's synthetic member.

Plan, reviews and Andrew's decisions: ops/state/line_bot_reminder_extract_mention_20261113/.
"""

# TODO(2026-11-13, once the passive-reminder gate (passive_reminder.not_a_reminder_reason) is on main):
# a model answer carrying "_trusted_direct_request": true must not skip that gate, live or drained.
# Deferred in ops/state/line_bot_reminder_extract_mention_20261113/PHASE4_DECISIONS_STAGE1.md.

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import gemini_client
import line_mentions
import main
import memory
import reminder_intent
from test_gemini_last_tier import LITE, TIER, perday, resp, tier  # noqa: F401  tier is a fixture

_TW = ZoneInfo("Asia/Taipei")
_SIX = {"action", "year", "month", "day", "hour", "minute"}
# What a pasted message could ask the model to add.
_EXTRA = {
    "mention_aliases": ["all"],
    "_trusted_direct_request": True,
    "_time_was_defaulted": True,
    "_time_default_kind": "morning",
    "_date_default_kind": "month_start",
    "participants": ["all"],
    "anything_else": "x",
}
_PINGS = [
    pytest.param({"mention_aliases": ["all"]}, id="aliases-all"),
    pytest.param({"mention_aliases": ["全家"]}, id="aliases-family"),
    pytest.param({"_time_default_kind": "daypart:@all"}, id="daypart-all"),
    pytest.param({"_time_default_kind": "daypart:@測試成員甲"}, id="daypart-member"),
    pytest.param(_EXTRA, id="everything"),
]
GROUP, USER = "G_MODEL_KEYS", "U_MODEL_KEYS"


def _today_iso() -> str:
    return datetime.now(_TW).strftime("%Y-%m-%d %A")


def _message(*, clock: bool, days: int = 10) -> tuple[str, dict]:
    """A dated line from chat: no 咪寶, no 提醒我, nothing medical, so only the model reads it."""
    day = (datetime.now(_TW) + timedelta(days=days)).date()
    when = "下午3點" if clock else ""
    text = f"{day.month}月{day.day}日{when}去郵局寄包裹"
    six = {
        "action": "去郵局寄包裹",
        "year": day.year,
        "month": day.month,
        "day": day.day,
        "hour": 15 if clock else 12,
        "minute": 0,
    }
    return text, six


def _answer(six: dict, **extra) -> str:
    return json.dumps({**six, **extra}, ensure_ascii=False)


def _stored_row() -> dict:
    with memory._conn() as c:
        rows = c.execute(
            "SELECT user_id, action, mention_aliases, time_kind FROM reminders WHERE group_id=?",
            (GROUP,),
        ).fetchall()
    assert len(rows) == 1, f"expected one reminder, found {len(rows)}"
    user_id, action, aliases, time_kind = rows[0]
    return {
        "user_id": user_id,
        "action": action,
        "mention_aliases": json.loads(aliases or "[]"),
        "time_kind": time_kind,
    }


def _push_pings_everyone(row: dict) -> bool:
    targets = line_mentions.reminder_actor_targets(row["user_id"], row["mention_aliases"], row["action"])
    return any(target.kind == "all" for target in targets)


def _sent_as_reply(text: str):
    """The LINE message for this text, built with the mention arguments ``main._reply`` passes."""
    _text, message = main._text_message_with_mentions(
        text,
        prepared=True,
        explicit_only=True,
        allow_all_mention=isinstance(text, line_mentions.AllowsAllMention),
    )
    return message


def _mentions(message) -> dict:
    return dict(getattr(message, "substitution", None) or {})


# ── extract_reminder itself ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("extra", _PINGS)
def test_keys_the_prompt_never_asked_for_are_dropped(tier, caplog, extra):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer(six, **extra))})

    with caplog.at_level(logging.INFO, logger="gemini_client"):
        assert gemini_client.extract_reminder(text, _today_iso()) == six

    lines = [r.getMessage() for r in caplog.records if r.name == "gemini_client"]
    assert lines == [f"extract_reminder dropped {len(extra)} key(s) the prompt never asked for"]


def test_without_a_clock_only_the_program_writes_the_default_time_keys(tier):
    text, six = _message(clock=False)
    tier(generate={LITE: resp(_answer(six, **_EXTRA))})

    result = gemini_client.extract_reminder(text, _today_iso())

    assert result is not None
    assert set(result) == _SIX | {"_time_was_defaulted", "_time_default_kind"}
    hour, minute, kind = reminder_intent.resolve_reminder_default_time(text)
    assert (result["hour"], result["minute"], result["_time_default_kind"]) == (hour, minute, kind)


def test_the_last_tier_answer_goes_through_the_same_whitelist(tier):
    text, six = _message(clock=True)
    fake = tier(generate={LITE: perday(LITE), TIER: resp(_answer(six, **_EXTRA))})

    result = gemini_client.extract_reminder(text, _today_iso())

    assert fake.generated == [LITE, TIER]
    assert result == six


def test_an_answer_with_only_the_six_keys_is_read_as_before(tier):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer(six))})

    assert gemini_client.extract_reminder(text, _today_iso()) == six


@pytest.mark.parametrize("year", [pytest.param(None, id="null"), pytest.param("drop", id="missing")])
def test_a_missing_or_null_year_is_this_year(tier, year):
    # A null year used to stay None: main's date check failed on int(None) and made no reminder.
    # Now it is this year, like a missing one.
    text, six = _message(clock=True)
    answer = {k: v for k, v in six.items() if k != "year"}
    if year is None:
        answer["year"] = None
    tier(generate={LITE: resp(_answer(answer))})

    assert gemini_client.extract_reminder(text, _today_iso()) == {**six, "year": datetime.now().year}


@pytest.mark.parametrize("key", ["action", "month", "day", "hour", "minute"])
def test_a_missing_required_key_is_still_no_reminder(tier, key):
    # As before: an answer without one of these keys makes no reminder.  (Only the next test needs
    # the required check itself; here a missing key ends in None anyway.)
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({k: v for k, v in six.items() if k != key}))})

    assert gemini_client.extract_reminder(text, _today_iso()) is None


def test_a_missing_hour_is_no_reminder_even_when_the_program_would_set_one(tier):
    # The required keys stay required as before, even where the program replaces the value: a
    # whitelist that copied a missing key as None would let this one through.
    text, six = _message(clock=False)
    tier(generate={LITE: resp(_answer({k: v for k, v in six.items() if k != "hour"}))})

    assert gemini_client.extract_reminder(text, _today_iso()) is None


@pytest.mark.parametrize(
    "action",
    [pytest.param(None, id="null"), pytest.param(0, id="number"), pytest.param(["去郵局"], id="list"),
     pytest.param("", id="empty"), pytest.param("   ", id="spaces"),
     pytest.param(" " * 50 + "去郵局寄包裹", id="blank-first-50")],
)
def test_an_action_that_is_not_words_is_no_reminder(tier, caplog, action):
    # str(None) made a reminder whose 事項 was 「None」.
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, "action": action}))})

    with caplog.at_level(logging.INFO, logger="gemini_client"):
        assert gemini_client.extract_reminder(text, _today_iso()) is None

    lines = [r.getMessage() for r in caplog.records if r.name == "gemini_client"]
    assert lines == [f"extract_reminder unreadable action type={type(action).__name__}"]


@pytest.mark.parametrize(
    "hour",
    [
        pytest.param(True, id="bool"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(15.5, id="fraction"),
        pytest.param(10**30, id="huge"),
        pytest.param(-1, id="negative"),
        pytest.param("15點", id="words"),
        pytest.param("十五", id="chinese-numeral"),
        pytest.param("x" * 300, id="long-text"),
        pytest.param(None, id="null"),
        pytest.param([15], id="list"),
    ],
)
def test_an_unreadable_hour_is_no_reminder(tier, caplog, hour):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, "hour": hour}))})

    with caplog.at_level(logging.INFO, logger="gemini_client"):
        assert gemini_client.extract_reminder(text, _today_iso()) is None

    # The check said no, rather than an exception the outer except caught.
    lines = [r.getMessage() for r in caplog.records if r.name == "gemini_client"]
    assert lines == [f"extract_reminder unreadable hour type={type(hour).__name__}"]


@pytest.mark.parametrize("key", ["year", "month", "day", "hour", "minute"])
@pytest.mark.parametrize(
    "bad",
    [pytest.param(float("inf"), id="infinity"), pytest.param(10**30, id="huge"),
     pytest.param("明年", id="words"), pytest.param(10000, id="over-9999"), pytest.param("00015", id="padded")],
)
def test_every_number_key_is_checked(tier, key, bad):
    # A key left out of the check let its Infinity through to the drain's OverflowError again.
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, key: bad}))})

    assert gemini_client.extract_reminder(text, _today_iso()) is None


def test_a_year_of_0_is_not_taken_for_this_year(tier):
    # Only a missing or null year becomes this year.  0 is passed on as 0, and main's date check
    # turns it down (ValueError), as before.
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, "year": 0}))})

    assert gemini_client.extract_reminder(text, _today_iso()) == {**six, "year": 0}


def test_the_largest_number_is_still_read(tier):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, "year": 9999}))})

    assert gemini_client.extract_reminder(text, _today_iso()) == {**six, "year": 9999}


@pytest.mark.parametrize(
    "hour",
    [pytest.param("15", id="text"), pytest.param(" 15 ", id="spaced"), pytest.param(15.0, id="float"),
     pytest.param("１５", id="fullwidth"), pytest.param("15\n", id="newline"), pytest.param("\t15", id="tab"),
     pytest.param("１５\u3000", id="ideographic-space")],
)
def test_a_whole_number_written_another_way_is_read_as_that_number(tier, hour):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, "hour": hour, "year": str(six["year"])}))})

    result = gemini_client.extract_reminder(text, _today_iso())

    assert result == six
    assert type(result["hour"]) is int and type(result["year"]) is int


def test_a_one_digit_number_written_as_text_is_read(tier):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, "minute": "0"}))})

    assert gemini_client.extract_reminder(text, _today_iso()) == six


def test_numbers_the_program_replaces_are_not_the_models_to_spoil(tier):
    # No clock in the text: the program's default time replaces the model's hour and minute, so a
    # null or an Infinity there is never read, and the reminder is still made.
    text, six = _message(clock=False)
    tier(generate={LITE: resp(_answer({**six, "hour": None, "minute": float("inf")}))})

    result = gemini_client.extract_reminder(text, _today_iso())

    hour, minute, _kind = reminder_intent.resolve_reminder_default_time(text)
    assert result is not None and (result["hour"], result["minute"]) == (hour, minute)


def test_a_weekday_date_the_program_works_out_is_not_the_models_to_spoil(tier):
    # One weekday and no date: complete_result works the date out and replaces the model's.
    text = "下週三晚上要去看電影"
    tier(generate={LITE: resp(json.dumps(
        {"action": "看電影", "year": None, "month": None, "day": "下週三", "hour": 19, "minute": 0},
        ensure_ascii=False,
    ))})

    result = gemini_client.extract_reminder(text, _today_iso())

    assert result is not None
    when = date(result["year"], result["month"], result["day"])
    today = datetime.now(_TW).date()
    assert when.weekday() == 2 and today < when <= today + timedelta(days=14)


def test_the_prompt_asks_for_the_keys_the_whitelist_keeps(tier, monkeypatch):
    text, six = _message(clock=True)
    fake = tier(generate={LITE: resp(_answer(six))})
    prompts: list[str] = []
    generate = fake.models.generate_content

    def keep_prompt(*, model, contents=None, config=None):
        prompts.append(str(contents))
        return generate(model=model, contents=contents, config=config)

    monkeypatch.setattr(fake.models, "generate_content", keep_prompt)

    gemini_client.extract_reminder(text, _today_iso())

    schema = prompts[0].split("回 JSON，schema：", 1)[1].split("判斷規則", 1)[0]
    # Both ways: a key the prompt asks for but the whitelist drops would be lost on every answer, and
    # a key the whitelist keeps but the prompt never asks for (mention_aliases, say) is one a pasted
    # message could set again.
    assert set(re.findall(r'"(\w+)"\s*:', schema)) == set(gemini_client._REMINDER_MODEL_KEYS)


def test_the_log_counts_the_dropped_keys_without_naming_them(tier, caplog):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer(six, **_EXTRA))})

    with caplog.at_level(logging.INFO, logger="gemini_client"):
        gemini_client.extract_reminder(text, _today_iso())

    lines = [r.getMessage() for r in caplog.records if r.name == "gemini_client"]
    assert lines == [f"extract_reminder dropped {len(_EXTRA)} key(s) the prompt never asked for"]


def test_the_log_names_an_unreadable_number_by_key_and_type_only(tier, caplog):
    text, six = _message(clock=True)
    tier(generate={LITE: resp(_answer({**six, "hour": "十五點整"}))})

    with caplog.at_level(logging.INFO, logger="gemini_client"):
        assert gemini_client.extract_reminder(text, _today_iso()) is None

    lines = [r.getMessage() for r in caplog.records if r.name == "gemini_client"]
    assert lines == ["extract_reminder unreadable hour type=str"]


# ── through the bot: the receipt, the stored row, the push ────────────────────────────────────


@pytest.mark.parametrize("extra", _PINGS)
def test_a_reminder_from_chat_keeps_nothing_else_the_model_wrote(tier, extra):
    text, six = _message(clock=True)
    fake = tier(generate={LITE: [resp(_answer(six)), resp(_answer(six, **extra))]})

    plain = main._maybe_extract_reminder(text, "G_MODEL_KEYS_PLAIN", USER, message_id="M_PLAIN")
    receipt = main._maybe_extract_reminder(text, GROUP, USER, message_id="M_LIVE")

    assert fake.generated == [LITE, LITE]  # the model read both: no local parser wrote them
    assert plain and receipt == plain  # the receipt a six-key answer makes: no @ line, no default note
    assert "@" not in receipt
    assert _mentions(_sent_as_reply(receipt)) == {}  # nobody is pinged
    row = _stored_row()
    assert row["mention_aliases"] == []
    assert row["time_kind"] == "clock"
    assert not _push_pings_everyone(row)


@pytest.mark.parametrize("extra", _PINGS)
def test_a_queued_reminder_keeps_nothing_else_the_model_wrote(tier, monkeypatch, extra):
    text, six = _message(clock=True)
    fake = tier(generate={LITE: resp(_answer(six, **extra))})
    assert memory.enqueue_pending_reminder(GROUP, USER, text, "M_QUEUED")
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders(GROUP)

    assert fake.generated == [LITE]
    row = _stored_row()
    assert row["mention_aliases"] == []
    assert row["time_kind"] == "clock"
    assert not _push_pings_everyone(row)


@pytest.mark.parametrize("key", ["year", "hour"])
def test_a_queued_row_with_an_unreadable_number_is_not_asked_about_again(tier, monkeypatch, key):
    # An Infinity raised OverflowError in the drain: the row went back to the queue and the model was
    # asked again on every drain until the row aged out.
    text, six = _message(clock=True)
    fake = tier(generate={LITE: resp(_answer({**six, key: float("inf")}))})
    pending_id = memory.enqueue_pending_reminder(GROUP, USER, text, "M_QUEUED_INF")
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders(GROUP)
    main._drain_pending_reminders(GROUP)

    assert fake.generated == [LITE]
    with memory._conn() as c:
        closed = c.execute(
            "SELECT status, drop_reason FROM pending_reminder_extract WHERE pending_id=?", (pending_id,)
        ).fetchone()
    assert tuple(closed) == ("dropped", "model_null")  # what the daily audit shows


def test_a_queued_row_whose_action_cannot_be_stored_is_not_asked_about_again(tier, monkeypatch):
    # A lone surrogate ("\ud800" in the JSON text) passed the action check and raised UnicodeEncodeError
    # when the row was written, outside the drain's inner try: the row went back to the queue, the model
    # was asked again on every drain, and the rows behind it waited.
    text, six = _message(clock=True)
    answer = json.dumps({**six, "action": "\ud800" + six["action"]})  # ASCII escapes, as a model writes them
    assert "\\ud800" in answer
    fake = tier(generate={LITE: resp(answer)})
    pending_id = memory.enqueue_pending_reminder(GROUP, USER, text, "M_QUEUED_SURROGATE")
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_has_enough_quota_for_retry", lambda: True)

    main._drain_pending_reminders(GROUP)
    main._drain_pending_reminders(GROUP)

    assert fake.generated == [LITE]
    with memory._conn() as c:
        closed = c.execute(
            "SELECT status, drop_reason FROM pending_reminder_extract WHERE pending_id=?", (pending_id,)
        ).fetchone()
    assert tuple(closed) == ("dropped", "model_null")
