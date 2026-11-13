"""A due reminder LINE refuses as textV2 goes out once as plain text (2026-11-13).

Rule 「提到人就真 @」 (agent_rules/line_bot.md, 2026-11-12): 「textV2 被 LINE 拒收時改送純文字一次，
不能因為加了 @ 讓訊息送不到」.  Before this, ``reminder_push._push_to_group`` sent a refused textV2
again, unchanged, up to three times and gave up, and the next 15-minute run did the same.
Synthetic data only; no string here is shaped like a LINE id.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from types import SimpleNamespace

import pytest
from linebot.v3.messaging import ApiException, TextMessageV2

import memory
import reminder_push

KEY = "123e4567-e89b-12d3-a456-426614174000"
PLAIN_KEY = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{KEY}:plain"))
OWNER = "U_TEST_OWNER"  # not shaped like a LINE id on purpose
NOW = 1_800_000_000


def _row(**overrides) -> dict:
    data = {
        "reminder_id": 7,
        "group_id": "G1",
        "user_id": OWNER,
        "action": "看醫生",
        "remind_at": NOW + 3600,
        "created_at": NOW - 86400,
        "source_text": "看醫生",
        "mention_aliases": [],
        "merged_details": [],
    }
    data.update(overrides)
    return data


class _Line:
    """A fake LINE push endpoint: answers each call from a script (a status, or "ok")."""

    def __init__(self, monkeypatch, *script):
        self.script = list(script)
        self.calls: list[tuple[str, str, str | None]] = []
        self.sleeps: list[float] = []
        self.archived: list[tuple[str, str, str, str]] = []
        self.references: list[tuple[str, str, dict]] = []
        line = self

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

            def push_message(self, request, x_line_retry_key=None):
                message = request.messages[0]
                line.calls.append((type(message).__name__, message.text, x_line_retry_key))
                outcome = line.script.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                if outcome != "ok":
                    raise ApiException(status=outcome, reason="scripted")
                return SimpleNamespace(sent_messages=[SimpleNamespace(id=f"m{len(line.calls)}")])

        monkeypatch.setattr(reminder_push, "_line_access_token", lambda: "token")
        monkeypatch.setattr(reminder_push, "ApiClient", FakeApiClient)
        monkeypatch.setattr(reminder_push, "MessagingApi", FakeMessagingApi)
        monkeypatch.setattr(reminder_push.time, "sleep", self.sleeps.append)

    def record_archive(self, monkeypatch):
        monkeypatch.setattr(
            reminder_push.memory,
            "log_raw_message",
            lambda group_id, message_id, user_id, text: self.archived.append(
                (group_id, message_id, user_id, text)
            ),
        )
        monkeypatch.setattr(
            reminder_push.memory,
            "log_sent_reminder_reference",
            lambda group_id, message_id, **kwargs: self.references.append((group_id, message_id, kwargs)),
        )
        return self

    def kinds(self) -> list[str]:
        return [kind for kind, _text, _key in self.calls]

    def keys(self) -> list[str | None]:
        return [key for _kind, _text, key in self.calls]


def _due_push(row: dict | None = None):
    text, message = reminder_push._build_push_text_and_message(row or _row(), "1hr", now=NOW)
    assert isinstance(message, TextMessageV2)  # the owner is @'d
    return text, message


# ── the plain fallback ────────────────────────────────────────────────────────────────────────


def test_a_refused_text_v2_goes_out_once_as_plain_text_under_its_own_key(monkeypatch):
    line = _Line(monkeypatch, 400, "ok").record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, reminder_id=7, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2", "TextMessage"]
    assert line.keys() == [KEY, PLAIN_KEY]
    assert PLAIN_KEY != KEY
    assert line.calls[1][1] == text  # the plain twin: @labels, no placeholder
    assert "{target}" not in text and text.startswith("@")
    assert line.sleeps == []
    # what was sent is what is archived, and the reminder reference rides on it
    assert line.archived == [("G1", "m2", "__bot__", text)]
    assert line.references == [("G1", "m2", {"reminder_id": 7})]


def test_a_refusal_on_the_last_try_still_gets_its_plain_try(monkeypatch):
    line = _Line(monkeypatch, 503, 503, 400, "ok").record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2"] * 3 + ["TextMessage"]
    assert line.keys() == [KEY, KEY, KEY, PLAIN_KEY]
    assert line.sleeps == [1, 2]


def test_the_plain_try_gets_its_own_attempt_budget(monkeypatch):
    line = _Line(monkeypatch, 503, 503, 400, 503, "ok").record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2"] * 3 + ["TextMessage"] * 2
    assert line.keys() == [KEY] * 3 + [PLAIN_KEY] * 2
    assert line.sleeps == [1, 2, 1]


class _HeaderedResponse:
    """What the SDK wraps into ApiException(http_resp=...): its headers and body end up in str(e)."""

    status = 400
    reason = "Bad Request"
    data = b'{"message":"The request body has 1 error(s)"}'

    @staticmethod
    def getheaders():
        return {"Content-Length": "429", "x-line-request-id": "req-4429"}


def test_a_refusal_whose_headers_mention_429_still_gets_its_plain_try(monkeypatch):
    refused = ApiException(http_resp=_HeaderedResponse())
    assert "429" in str(refused) and refused.status == 400
    line = _Line(monkeypatch, refused, "ok").record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2", "TextMessage"]


def test_an_error_without_a_status_keeps_the_text_v2(monkeypatch):
    # a timeout or a dropped connection: the textV2 may have gone out
    line = _Line(monkeypatch, ConnectionError("connection reset"), "ok").record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2", "TextMessageV2"]
    assert line.keys() == [KEY, KEY]
    assert line.sleeps == [1]


def test_riders_are_closed_with_the_item_when_the_plain_try_goes_out(monkeypatch):
    line = _Line(monkeypatch, 400, "ok").record_archive(monkeypatch)
    text, message = _due_push()
    item = {"reminder_id": 7, "group_id": "G1", "stage": "1hr", "text": text, "message": message}
    rider = {"reminder_id": 8, "group_id": "G1", "stage": "1d", "text": text, "message": message}
    claims = {7: {"retry_key": KEY, "id": 7}, 8: {"retry_key": "unused", "id": 8}}
    finalized: list[int] = []
    released: list[int] = []
    monkeypatch.setattr(reminder_push, "_claim_due_item", lambda due: claims[int(due["reminder_id"])])
    monkeypatch.setattr(
        reminder_push.memory, "finalize_natural_reminder_delivery", lambda claim: finalized.append(claim["id"]) or True
    )
    monkeypatch.setattr(reminder_push.memory, "release_reminder_delivery_claim", lambda claim: released.append(claim["id"]))

    assert reminder_push._push_due_item(item, [rider]) == 1

    assert line.keys() == [KEY, PLAIN_KEY]
    assert finalized == [7, 8] and released == []


def test_a_transient_failure_of_the_plain_try_is_retried_under_the_plain_key(monkeypatch):
    line = _Line(monkeypatch, 400, 503, "ok").record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2", "TextMessage", "TextMessage"]
    assert line.keys() == [KEY, PLAIN_KEY, PLAIN_KEY]
    assert line.sleeps == [1]


def test_a_refused_plain_try_ends_the_push(monkeypatch):
    line = _Line(monkeypatch, 400, 400).record_archive(monkeypatch)
    text, message = _due_push()

    assert not reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2", "TextMessage"]  # never the refused textV2 again
    assert line.sleeps == []
    assert line.archived == []


def test_a_plain_try_line_already_took_counts_as_delivered(monkeypatch):
    # a rerun after the plain went out: textV2 refused again, the plain key answers 409
    line = _Line(monkeypatch, 400, 409).record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.keys() == [KEY, PLAIN_KEY]


def test_without_a_retry_key_the_plain_try_has_none_either(monkeypatch):
    # a strict fake: push_message has no key parameter at all, so passing one (even None) would fail
    calls: list[str] = []

    class StrictMessagingApi:
        def __init__(self, _client):
            pass

        def push_message(self, request):
            message = request.messages[0]
            calls.append(type(message).__name__)
            if isinstance(message, TextMessageV2):
                raise ApiException(status=400, reason="scripted")
            return SimpleNamespace(sent_messages=[])

    line = _Line(monkeypatch)
    monkeypatch.setattr(reminder_push, "MessagingApi", StrictMessagingApi)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message)

    assert calls == ["TextMessageV2", "TextMessage"]
    assert line.sleeps == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_any_refusing_4xx_takes_the_plain_try(monkeypatch, status):
    line = _Line(monkeypatch, status, "ok").record_archive(monkeypatch)
    text, message = _due_push()

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2", "TextMessage"]
    assert line.keys() == [KEY, PLAIN_KEY]


@pytest.mark.parametrize("status", [401, 403, 404])
def test_any_refusing_4xx_ends_a_plain_reminder(monkeypatch, status):
    line = _Line(monkeypatch, status, "ok").record_archive(monkeypatch)

    assert not reminder_push._push_to_group("G1", "⏰ 提醒（1 小時後）\n2027-01-15 10:00 看醫生", retry_key=KEY)

    assert line.kinds() == ["TextMessage"]


class _SentinelResponse:
    status = 400
    reason = "Bad Request"
    data = json.dumps(
        {"message": "The request body has 1 error(s)",
         "details": [{"message": "SENTINEL-DETAIL", "property": "messages[0].text"}]}
    ).encode()

    @staticmethod
    def getheaders():
        return {"x-line-request-id": "req-1"}


class _NoStatusError(Exception):
    def __init__(self, reason: str):
        super().__init__("SENTINEL-STR")
        self.reason = reason


def test_failure_logs_carry_no_words_of_the_message_or_the_error(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="reminder_push")
    line = _Line(
        monkeypatch,
        ApiException(http_resp=_SentinelResponse()),
        ApiException(status=0, reason="SSLError\nSENTINEL-SSL"),
        _NoStatusError("X" + "z" * 40 + "Error SENTINEL-REASON"),
        "ok",
    ).record_archive(monkeypatch)
    text, message = _due_push(_row(action="看醫生 SENTINELWORDS"))

    assert reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2", "TextMessage", "TextMessage", "TextMessage"]
    logged = caplog.text
    for secret in ("SENTINELWORDS", "SENTINEL-DETAIL", "SENTINEL-SSL", "SENTINEL-STR", "SENTINEL-REASON", "zzzz"):
        assert secret not in logged, secret
    assert "status=400" in logged and "props=messages[0].text" in logged
    assert "cause=SSLError" in logged


@pytest.mark.parametrize("status", [429, 409])
def test_429_and_409_never_turn_into_a_plain_try(monkeypatch, status):
    line = _Line(monkeypatch, status).record_archive(monkeypatch)
    text, message = _due_push()

    delivered = reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert delivered is (status == 409)
    assert line.kinds() == ["TextMessageV2"]


def test_a_transient_failure_keeps_resending_the_text_v2(monkeypatch):
    # 5xx or a timeout: the textV2 may have gone out, so only its own key may be used again
    line = _Line(monkeypatch, 503, 500, 502).record_archive(monkeypatch)
    text, message = _due_push()

    assert not reminder_push._push_to_group("G1", text, message=message, retry_key=KEY)

    assert line.kinds() == ["TextMessageV2"] * 3
    assert line.keys() == [KEY] * 3
    assert line.sleeps == [1, 2]


def test_a_refused_plain_reminder_is_not_sent_again(monkeypatch):
    # nobody named: a plain text from the start; a 4xx will not change on a retry
    line = _Line(monkeypatch, 400, "ok", "ok").record_archive(monkeypatch)

    assert not reminder_push._push_to_group("G1", "⏰ 提醒（1 小時後）\n2027-01-15 10:00 看醫生", retry_key=KEY)

    assert line.kinds() == ["TextMessage"]
    assert line.sleeps == []


# ── the real 15-minute job ────────────────────────────────────────────────────────────────────


def test_the_job_delivers_and_closes_the_stage_when_line_refuses_the_text_v2(monkeypatch):
    remind_at = NOW + 3600
    clock = {"now": NOW - 86400}
    monkeypatch.setattr(time, "time", lambda: float(clock["now"]))
    reminder_id = memory.add_reminder("G1", OWNER, "看醫生", remind_at, source_text="看醫生")
    assert reminder_id is not None
    clock["now"] = NOW
    line = _Line(monkeypatch, 400, "ok")

    assert reminder_push.push_reminders() == 1

    assert line.kinds() == ["TextMessageV2", "TextMessage"]
    retry_key = line.keys()[0]
    assert line.keys()[1] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"{retry_key}:plain"))
    state = memory.get_reminder_push_state("G1", int(reminder_id))
    assert state is not None and int(state["pushed_1hr"]) == 1
    sent_text = line.calls[1][1]
    assert memory.get_raw_message("G1", "m2") == ("__bot__", sent_text)
    assert memory.get_sent_reminder_reference("G1", "m2")["reminder_id"] == int(reminder_id)

    # the next run has nothing left to send for this stage
    assert reminder_push.push_reminders() == 0
    assert len(line.calls) == 2
