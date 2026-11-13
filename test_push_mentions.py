"""2026-11-12 「提到人就真 @」 (Andrew: 「以後所有事情，提到人的時候都走B方案」): a push that names a family
member @s them.  This file covers the builder (`line_mentions.user_mention_targets`) and the helper
(`line_push_client.push_text_with_mentions` / `try_push_text_with_mentions`) through the real
`push_messages` and a fake HTTP session.  The exits are tested next to their own tests.

Plan and reviews: ops/state/line_bot_reminder_alert_20261112/.  All ids and names are made up, and nothing
here reaches the network.
"""

from __future__ import annotations

import socket
import uuid
from types import SimpleNamespace

import pytest
import requests

import line_mentions
import line_push_client
import output_validator

GROUP = "G_TEST"
KEY = "retry-key-for-a-test"
PLAIN_KEY = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{KEY}:plain"))
TODO = "📋 今日 TODO 提醒\n\n🔔 今天到期：\n- 成員甲 繳學費"
FAKE_LINE_ID = "U" + "0123456789abcdef" * 2  # made at run time: the repo's privacy audit flags that shape in a file


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No connection and no name lookup leave this machine; the token is a stand-in."""

    def refuse(*_a, **_kw):
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(requests, "post", refuse)
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", "")


class FakeSession:
    """What LINE answers, one status per POST; `push_messages` uses it instead of requests."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.posts: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"key": headers.get("X-Line-Retry-Key"), "messages": json["messages"]})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(
            status_code=answer,
            text='{"message":"MARKER-秘密 ' + FAKE_LINE_ID + '"}',
            json=lambda: {},
        )


def two_members():
    return line_mentions.user_mention_targets([("U_A", "成員甲"), ("U_B", "成員乙")])


def everyone():
    return [line_mentions.MentionTarget(key="all", kind="all", label="@all")]


def push(session, text=TODO, targets=None, **kwargs):
    return line_push_client.push_text_with_mentions(
        GROUP, text, two_members() if targets is None else targets, retry_key=KEY, session=session, **kwargs
    )


# ── the builder ───────────────────────────────────────────────────────────────────────────────


def test_the_builder_uses_letter_keys_and_drops_empty_repeated_nameless_and_bot_ids():
    targets = line_mentions.user_mention_targets(
        [("U_A", "成員甲"), ("U_A", "成員甲"), ("", "成員丙"), ("__bot__", "咪寶"), ("U_B", "@成員乙"), ("U_C", " ")]
    )
    assert [(t.key, t.kind, t.user_id, t.label) for t in targets] == [
        ("pa", "user", "U_A", "@成員甲"),
        ("pb", "user", "U_B", "@成員乙"),
    ]
    assert not any(ch.isdigit() for t in targets for ch in t.key)


def test_the_builder_stops_at_twenty_people():
    targets = line_mentions.user_mention_targets([(f"U_{n}", f"成員{n}") for n in range(25)])
    assert len(targets) == 20 and len({t.key for t in targets}) == 20


# ── the helper: the @ path ────────────────────────────────────────────────────────────────────


def test_a_named_member_is_pinged_on_a_first_line_and_the_body_is_unchanged():
    session = FakeSession(200)
    assert push(session) is True
    [post] = session.posts
    assert post["key"] == KEY
    assert post["messages"] == [
        {
            "type": "textV2",
            "text": "{pa} {pb}\n" + TODO,
            "substitution": {
                "pa": {"type": "mention", "mentionee": {"type": "user", "userId": "U_A"}},
                "pb": {"type": "mention", "mentionee": {"type": "user", "userId": "U_B"}},
            },
        }
    ]


def test_the_whole_group_is_one_all_mention():
    session = FakeSession(200)
    assert push(session, "🩺 爸媽健康：爸爸 21:19 提到跌倒", everyone()) is True
    message = session.posts[0]["messages"][0]
    assert message["text"].startswith("{all}\n")
    assert message["substitution"] == {"all": {"type": "mention", "mentionee": {"type": "all"}}}


def test_a_body_with_braces_is_escaped_for_textv2_and_sent_as_is_in_the_fallback():
    session = FakeSession(400, 200)
    assert push(session, "提醒：帶 {文件}") is True
    first, second = session.posts
    assert first["messages"][0]["text"] == "{pa} {pb}\n提醒：帶 {{文件}}"
    assert second["messages"] == [{"type": "text", "text": "@成員甲 @成員乙\n提醒：帶 {文件}"}]


def test_a_digit_free_key_keeps_a_message_the_validator_lets_through():
    """`{p1}` gives 「查最新資料」 the digit the "latest data" rule wants and the TODO is replaced; `{pa}` does not."""
    body = "📋 今日 TODO 提醒\n\n🔔 今天到期：\n- 成員甲 查最新資料"
    assert output_validator.validate_outbound_text(body).ok
    assert not output_validator.validate_outbound_text("{p1}\n" + body).ok  # why the keys have no digits
    session = FakeSession(200)
    assert push(session, body, line_mentions.user_mention_targets([("U_A", "成員甲")])) is True
    assert session.posts[0]["messages"][0]["type"] == "textV2"
    assert session.posts[0]["messages"][0]["text"] == "{pa}\n" + body


# ── the helper: LINE refuses or fails ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("second", [200, 409])
def test_a_refused_textv2_goes_out_once_as_plain_text_under_its_own_key(second):
    session = FakeSession(400, second)
    assert push(session) is True
    first, fallback = session.posts
    assert first["key"] == KEY and first["messages"][0]["type"] == "textV2"
    assert fallback["key"] == PLAIN_KEY != KEY
    assert fallback["messages"] == [{"type": "text", "text": "@成員甲 @成員乙\n" + TODO}]


@pytest.mark.parametrize("status", [403, 404])
def test_any_other_refusal_falls_back_too(status):
    session = FakeSession(status, 200)
    assert push(session) is True
    assert [post["key"] for post in session.posts] == [KEY, PLAIN_KEY]


def test_409_means_line_already_took_the_key_and_nothing_is_sent_again():
    session = FakeSession(409)
    assert push(session) is True
    assert len(session.posts) == 1


@pytest.mark.parametrize("answer", [429, 500, 503, requests.ConnectionError("down"), requests.Timeout("slow")])
def test_quota_server_errors_and_lost_connections_raise_without_a_second_send(answer):
    session = FakeSession(answer)
    with pytest.raises(line_push_client.LinePushError):
        push(session)
    assert len(session.posts) == 1


def test_a_failed_fallback_raises():
    session = FakeSession(400, 429)
    with pytest.raises(line_push_client.LinePushError):
        push(session)
    assert len(session.posts) == 2


def test_logs_never_carry_line_answer_text_names_or_ids(caplog):
    caplog.set_level("DEBUG")
    session = FakeSession(400, 429)
    assert line_push_client.try_push_text_with_mentions(
        GROUP, TODO, two_members(), retry_key=KEY, session=session, source="todo"
    ) is False
    assert "status=400" in caplog.text and "status=429" in caplog.text
    for secret in ("MARKER", "秘密", FAKE_LINE_ID, "U_A", "U_B", "成員甲", "繳學費"):
        assert secret not in caplog.text, secret


def test_the_bool_wrapper_turns_any_failure_into_false():
    assert line_push_client.try_push_text_with_mentions(
        GROUP, TODO, two_members(), retry_key=KEY, session=FakeSession(500)
    ) is False
    assert line_push_client.try_push_text_with_mentions(GROUP, TODO, two_members(), retry_key="") is False


def test_a_retry_key_is_required_before_anything_is_sent():
    session = FakeSession(200)
    with pytest.raises(ValueError):
        line_push_client.push_text_with_mentions(GROUP, TODO, two_members(), retry_key="", session=session)
    assert session.posts == []


# ── the helper: anything off about the @ makes exactly today's call ─────────────────────────────


def todays_call(session, text=TODO):
    assert session.posts == [{"key": KEY, "messages": [{"type": "text", "text": text}]}]


def test_no_targets_is_todays_call(caplog):
    caplog.set_level("INFO")
    session = FakeSession(200)
    assert push(session, targets=[]) is True
    todays_call(session)
    assert "push mentions" not in caplog.text


def test_unusable_targets_are_filtered_and_none_left_is_todays_call():
    bad = [
        line_mentions.MentionTarget(key="p1", kind="user", user_id="U_A", label="@成員甲"),  # a digit key
        line_mentions.MentionTarget(key="pa", kind="user", user_id="", label="@成員甲"),  # no id
        line_mentions.MentionTarget(key="pb", kind="user", user_id="__bot__", label="@咪寶"),
        line_mentions.MentionTarget(key="pc", kind="user", user_id="U_A", label="@成員甲"),
        line_mentions.MentionTarget(key="pc", kind="user", user_id="U_B", label="@成員乙"),  # same key twice
        line_mentions.MentionTarget(key="pd", kind="user", user_id="U_A", label="@成員甲"),  # same id twice
    ]
    session = FakeSession(200)
    assert push(session, targets=bad) is True
    message = session.posts[0]["messages"][0]
    assert message["text"] == "{pc}\n" + TODO
    assert message["substitution"] == {"pc": {"type": "mention", "mentionee": {"type": "user", "userId": "U_A"}}}
    session = FakeSession(200)
    assert push(session, targets=bad[:3]) is True
    todays_call(session)


@pytest.mark.parametrize("value", ["0", "off", "False", " no ", "disable", "n", "關"])
def test_the_switch_turns_the_mention_off_and_so_does_a_value_it_does_not_know(monkeypatch, value):
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", value)
    session = FakeSession(200)
    assert push(session) is True
    todays_call(session)


@pytest.mark.parametrize("value", ["", "1", "on", "true", "yes", " ON "])
def test_the_switch_is_on_only_when_it_says_so(monkeypatch, value):
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", value)
    session = FakeSession(200)
    assert push(session) is True
    assert session.posts[0]["messages"][0]["type"] == "textV2"


def test_a_blank_text_never_becomes_a_bare_ping():
    session = FakeSession(200)
    with pytest.raises(line_push_client.LinePushError):  # today's call: push_messages refuses an empty text
        push(session, "  ")
    assert session.posts == []


def test_a_template_the_validator_blocks_is_todays_call(monkeypatch):
    real = output_validator.validate_outbound_text

    def picky(text):
        if str(text).startswith("{"):
            return SimpleNamespace(ok=False, text="", reason="test_block")
        return real(text)

    monkeypatch.setattr(output_validator, "validate_outbound_text", picky)
    session = FakeSession(200)
    assert push(session) is True
    todays_call(session)


def test_a_body_the_validator_blocks_is_not_let_through_by_the_mention_line():
    """A line in front stops the anchored short-reply rule from matching: the body decides first."""
    body = "好喔，我們會好好打卡的，" + "記得" * 14
    assert not output_validator.validate_outbound_text(body).ok
    targets = line_mentions.user_mention_targets([("U_A", "成員甲")])
    template = line_mentions.text_v2_dict(body, targets)["text"]
    plain = line_mentions.text_with_plain_mentions(body, targets)
    assert output_validator.validate_outbound_text(template).ok  # the bypass this guards against
    assert output_validator.validate_outbound_text(plain).ok
    session = FakeSession(200)
    with pytest.raises(line_push_client.LinePushError):  # today's call: the validator removes it, nothing is sent
        push(session, body, targets)
    assert session.posts == []


def test_a_mention_builder_that_raises_is_todays_call(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("broken")

    monkeypatch.setattr(line_mentions, "text_v2_dict", boom)
    session = FakeSession(200)
    assert push(session) is True
    todays_call(session)


def test_the_plain_fallback_fits_the_utf16_limit_line_counts_by():
    body = "😀" * 2600  # 5200 UTF-16 units
    session = FakeSession(400, 200)
    assert push(session, body, line_mentions.user_mention_targets([("U_A", "成員甲")])) is True
    text = session.posts[1]["messages"][0]["text"]
    assert line_mentions.utf16_len(text) <= 5000 and text.startswith("@成員甲\n")
    assert line_mentions.utf16_len(session.posts[0]["messages"][0]["text"]) <= 5000


def test_only_the_plain_copy_blocked_is_todays_call():
    """A digit in a name gives the plain copy what the "latest data" rule wants; the textV2 text has none."""
    body = "📋 今日 TODO 提醒\n\n🔔 今天到期：\n- 查最新資料"
    targets = line_mentions.user_mention_targets([("U_A", "成員2")])
    assert output_validator.validate_outbound_text(body).ok
    assert output_validator.validate_outbound_text(line_mentions.text_v2_dict(body, targets)["text"]).ok
    assert not output_validator.validate_outbound_text(line_mentions.text_with_plain_mentions(body, targets)).ok
    session = FakeSession(200)
    assert push(session, body, targets) is True
    todays_call(session, body)


def test_ids_with_spaces_are_dropped_and_no_more_than_twenty_people_are_pinged():
    spaced = [line_mentions.MentionTarget(key="pa", kind="user", user_id=" U_A", label="@成員甲")]
    session = FakeSession(200)
    assert push(session, targets=spaced) is True
    todays_call(session)
    many = [
        line_mentions.MentionTarget(key="p" + chr(97 + n // 26) + chr(97 + n % 26), kind="user", user_id=f"U_{n}", label="@成員")
        for n in range(25)
    ]
    session = FakeSession(200)
    assert push(session, targets=many) is True
    assert len(session.posts[0]["messages"][0]["substitution"]) == 20


def test_the_switch_warning_never_prints_the_value(monkeypatch, caplog):
    caplog.set_level("WARNING")
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", "Disable-ZZMARK")  # the switch reads it lowercased
    session = FakeSession(200)
    assert push(session) is True
    todays_call(session)
    assert "LINE_BOT_PUSH_MENTIONS" in caplog.text and "zzmark" not in caplog.text.lower()
