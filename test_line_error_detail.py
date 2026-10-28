"""LINE error logging (LINE bot fixes batch 1, 2026-10-24).  Synthetic data only.

`str(ApiException)` is "(400)\\nReason: ...\\nHTTP response headers: ...\\nHTTP response body: ..."
so the old `str(e)[:300]` cut the body -- the only part that says why LINE refused -- off.
`line_errors.error_detail` logs the status, reason, request id, LINE's message and the refused
property (`messages[2].text`), masks LINE ids and URLs, and never contains the reply token or
chat text.  Anything that is not an HTTP answer is reduced to its type name.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from linebot.v3.messaging.exceptions import ApiException

import line_errors
import main

TOKEN = "REPLYTOKEN-SYNTHETIC-0123456789abcdef"
USER_ID = "U" + "a1" * 16
GROUP_ID = "C" + "b2" * 16


class _Resp:
    def __init__(self, status, reason, data, headers):
        self.status, self.reason, self.data, self._headers = status, reason, data, headers

    def getheaders(self):
        return self._headers


def api_error(status, body, reason="Bad Request", headers=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    return ApiException(
        http_resp=_Resp(status, reason, body, {"x-line-request-id": "req-123"} if headers is None else headers)
    )


def test_status_reason_request_id_message_and_property_are_logged():
    text = line_errors.error_detail(
        api_error(
            400,
            {
                "message": "The request body has 2 error(s)",
                "details": [
                    {"message": "May not be empty", "property": "messages[0].text"},
                    {"message": "Length must be <= 5000", "property": "messages[2].text"},
                ],
            },
        )
    )
    assert text == (
        "status=400 reason=Bad Request request_id=req-123 "
        'message="The request body has 2 error(s)" props=messages[0].text,messages[2].text'
    )


def test_the_detail_messages_are_not_logged():
    """A detail message can echo what was refused (a URI, a snippet): only the path is kept."""
    text = line_errors.error_detail(
        api_error(400, {"message": "bad", "details": [{"message": "Invalid URI: https://x.test/card/abc", "property": "messages[1].originalContentUrl"}]})
    )
    assert "x.test" not in text and "Invalid URI" not in text
    assert "messages[1].originalContentUrl" in text


def test_line_ids_urls_and_long_runs_are_masked():
    body = {
        "message": f"user {USER_ID} in {GROUP_ID} see https://example.test/card/SecretSecretSecretSecret1234 and {'z' * 40}",
        "details": [{"property": f"messages[0].{USER_ID}"}],
    }
    text = line_errors.error_detail(api_error(400, body))
    for leaked in (USER_ID, GROUP_ID, "example.test", "SecretSecret", "z" * 30):
        assert leaked not in text
    assert "<id>" in text and "<url>" in text and "<tok>" in text


def test_the_reply_token_never_appears_even_when_the_server_echoes_it():
    text = line_errors.error_detail(
        api_error(400, {"message": f"Invalid reply token {TOKEN}"}, reason=f"bad {TOKEN}"),
        secrets=(TOKEN,),
    )
    assert TOKEN not in text
    assert "<token>" in text


def test_a_body_that_is_not_lines_json_is_not_logged():
    text = line_errors.error_detail(api_error(502, "<html>Bad gateway: 媽媽的行程 https://x.test/p</html>", "Bad Gateway"))
    assert text == "status=502 reason=Bad Gateway request_id=req-123 body_len=%d" % len(
        "<html>Bad gateway: 媽媽的行程 https://x.test/p</html>"
    )
    assert "媽媽" not in text and "x.test" not in text


def test_a_json_body_without_message_or_details_is_fine():
    assert line_errors.error_detail(api_error(429, {"x": 1}, "Too Many Requests")) == (
        "status=429 reason=Too Many Requests request_id=req-123"
    )
    assert line_errors.error_detail(api_error(400, ["a", "b"])).endswith("body_len=10")


def test_a_hostile_request_id_header_is_dropped():
    text = line_errors.error_detail(
        api_error(400, {"message": "m"}, headers={"x-line-request-id": "id\nINJECT user=" + USER_ID})
    )
    assert "request_id" not in text and "INJECT" not in text


def test_control_characters_cannot_break_the_log_line():
    text = line_errors.error_detail(api_error(400, {"message": "line1\nFAKE LOG LINE\r\x00end"}))
    assert "\n" not in text and "\r" not in text and "\x00" not in text


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(f"read timed out talking about {TOKEN} and 媽媽今天的行程"),
        ConnectionError("reset by peer"),
        ValueError(f"reply_token {TOKEN} is invalid"),
        KeyError("媽媽"),
    ],
)
def test_anything_that_is_not_an_http_answer_logs_the_type_name_only(exc):
    text = line_errors.error_detail(exc, secrets=(TOKEN,))
    assert text == f"error_type={type(exc).__name__}"


def test_status_helpers():
    assert line_errors.http_status(api_error(409, {})) == 409
    assert line_errors.http_status(SimpleNamespace(status_code=404)) == 404
    assert line_errors.http_status(TimeoutError()) is None
    assert line_errors.http_status(SimpleNamespace(status="oops")) is None
    assert line_errors.is_client_error(api_error(400, {})) and line_errors.is_client_error(api_error(429, {}))
    assert not line_errors.is_client_error(api_error(500, {}))
    assert not line_errors.is_client_error(TimeoutError())
    assert not line_errors.is_client_error(api_error(302, {}))


def test_hostile_input_stays_linear():
    """H5: every pattern here is a single class with a plain quantifier."""
    shapes = {
        "ids": ("U" + "a" * 31 + "x") * 3000,
        "long run": "a" * 100000,
        "near runs": ("a" * 23 + "-") * 4000,
        "urls": "https://" * 12500,
        "hex then junk": ("U" + "0" * 32 + "g") * 2500,
        "controls": "\x00\n" * 50000,
    }
    for name, shape in shapes.items():
        started = time.perf_counter()
        line_errors.error_detail(api_error(400, {"message": shape, "details": [{"property": shape}] * 20}))
        line_errors.error_detail(api_error(400, shape))
        assert time.perf_counter() - started < 1.0, name


def test_the_bot_wrapper_uses_the_same_function():
    exc = api_error(400, {"message": "m"})
    assert main._line_error_detail(exc) == line_errors.error_detail(exc)
    assert TOKEN not in main._line_error_detail(api_error(400, {"message": TOKEN}), secrets=(TOKEN,))


# ── the log lines the bot writes ──────────────────────────────────────────────────────────


@pytest.fixture()
def line(monkeypatch):
    api = MagicMock()
    api.__enter__ = MagicMock(return_value=api)
    api.__exit__ = MagicMock(return_value=False)
    messaging = MagicMock()
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)
    with (
        patch("main.ApiClient", return_value=api),
        patch("main.MessagingApi", return_value=messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        yield messaging


def test_reply_failure_log_names_the_refused_property_and_nothing_private(line, caplog):
    caplog.set_level(logging.INFO)
    line.reply_message.side_effect = api_error(
        400,
        {
            "message": f"The request body has 1 error(s) for {USER_ID}",
            "details": [{"message": "May not be empty", "property": "messages[2].text"}],
        },
    )

    # (batch 2, B6.3: a refused text reply is sent again once as plain text and, where a push is
    # allowed, pushed; a reply-only flow ends False after the plain-text resend is refused too.)
    assert (
        main._reply(TOKEN, "合成回覆內容", group_id=GROUP_ID, include_auxiliary=False, allow_push_fallback=False)
        is False
    )

    (record,) = [r for r in caplog.records if r.getMessage().startswith("reply failed:")]
    message = record.getMessage()
    assert "status=400" in message and "request_id=req-123" in message and "messages[2].text" in message
    assert TOKEN not in message and USER_ID not in message and "合成回覆內容" not in message
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in everything and "合成回覆內容" not in everything


def test_push_failure_log_has_the_same_shape(line, caplog):
    caplog.set_level(logging.INFO)
    line.reply_message.side_effect = api_error(400, {"message": "Invalid reply token"})
    line.push_message.side_effect = api_error(429, {"message": "You have reached your monthly limit."}, "Too Many Requests")

    assert main._reply(TOKEN, "合成回覆內容", group_id=GROUP_ID, include_auxiliary=False) is False

    (record,) = [r for r in caplog.records if r.getMessage().startswith("fallback push also failed:")]
    assert "status=429" in record.getMessage() and "monthly limit" in record.getMessage()
    assert TOKEN not in "\n".join(r.getMessage() for r in caplog.records)


def test_a_failure_that_is_not_an_http_answer_logs_only_its_type(line, caplog):
    caplog.set_level(logging.INFO)
    line.reply_message.side_effect = TimeoutError(f"timed out for {TOKEN}: 媽媽今天的行程")

    main._reply(TOKEN, "合成回覆內容", group_id=GROUP_ID, include_auxiliary=False)

    (record,) = [r for r in caplog.records if r.getMessage().startswith("reply failed:")]
    assert record.getMessage() == "reply failed: error_type=TimeoutError"
    assert TOKEN not in "\n".join(r.getMessage() for r in caplog.records)


def test_the_fence_logs_count_and_group_only(line, caplog):
    caplog.set_level(logging.INFO)
    main._register_inbound_reply_token(TOKEN, GROUP_ID, "MSG-1")
    import memory

    memory.begin_inbound_event(GROUP_ID, "MSG-1")
    memory.claim_inbound_reply(GROUP_ID, ["MSG-1"])  # an earlier run is already sending

    assert main._reply(TOKEN, "合成回覆內容", group_id=GROUP_ID, include_auxiliary=False) is False

    blocked = [r.getMessage() for r in caplog.records if "already being answered" in r.getMessage()]
    assert blocked and all("合成回覆內容" not in m and TOKEN not in m and "MSG-1" not in m for m in blocked)
