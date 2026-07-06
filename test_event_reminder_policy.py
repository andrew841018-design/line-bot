"""Regression tests for LINE event reminder delivery policy."""

from __future__ import annotations

import json
import uuid


def _event(event_id: str = "E1") -> dict:
    return {
        "event_id": event_id,
        "event_date": "2026-06-02",
        "event_time": "08:00",
        "title": "看診",
        "location": "",
        "participants": "[]",
    }


def test_line_id_redaction_handles_all_prefixes_and_case():
    import event_reminder

    text = (
        "bad "
        "UAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "
        "Cbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "
        "R1234567890abcdef1234567890ABCDEF"
    )

    redacted = event_reminder._redact_line_ids(text)

    assert "U***" in redacted
    assert "C***" in redacted
    assert "R***" in redacted
    assert "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in redacted
    assert "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in redacted
    assert "1234567890abcdef1234567890ABCDEF" not in redacted


def test_post_result_sends_retry_key_and_accepts_409(monkeypatch):
    import event_reminder

    captured: dict = {}
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "token")

    class _Resp:
        status_code = 409
        text = "duplicate accepted"

    def fake_post(url, headers, json, timeout):
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(event_reminder._session, "post", fake_post)

    result = event_reminder._post_result([{"type": "text", "text": "hi"}])

    assert result == event_reminder.POST_OK
    assert uuid.UUID(captured["headers"]["X-Line-Retry-Key"])
    assert captured["json"]["to"] == "G1"


def test_one_off_policy_disabled_without_private_config(tmp_path, monkeypatch):
    import event_reminder

    monkeypatch.delenv("EVENT_REMINDER_ONE_OFF_JSON", raising=False)
    monkeypatch.delenv("REMINDER_ASK_EVENT_IDS", raising=False)
    monkeypatch.setenv("EVENT_REMINDER_PRIVATE_CONFIG", str(tmp_path / "missing.json"))

    spec = event_reminder.build_reminder_message_spec(_event("E1"), 1)

    assert spec is not None
    assert spec["kind"] == "text"
    assert "活動提醒" in spec["text"]


def test_one_off_policy_suppresses_standard_and_shared_with_piggyback(
    tmp_path, monkeypatch
):
    import event_reminder

    monkeypatch.setenv("EVENT_REMINDER_PRIVATE_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("REMINDER_ASK_EVENT_IDS", "E1")
    monkeypatch.setenv("REMINDER_ASK_OFFSET", "1")
    monkeypatch.setenv("REMINDER_ASK_PLACEHOLDER", "mom")
    monkeypatch.setenv("REMINDER_ASK_TEXT", "{mom}\n請確認時間")
    monkeypatch.setenv("REMINDER_ASK_TEXT_PLAIN", "媽媽\n請確認時間")
    monkeypatch.setenv("REMINDER_ASK_USER_ID", "U" + "1" * 32)

    event = _event("E1")

    assert event_reminder.should_skip_standard_reminder(event, 3) is True
    assert event_reminder.build_reminder_message_spec(event, 3) is None
    launchd_spec = event_reminder.build_reminder_message_spec(event, 1)
    piggyback_spec = event_reminder.build_reminder_message_spec(
        event, 1, allow_mention=False
    )

    assert launchd_spec["kind"] == "mention"
    assert launchd_spec["user_id"] == "U" + "1" * 32
    assert piggyback_spec == {"kind": "text", "text": "媽媽\n請確認時間"}


def test_standard_event_all_participants_uses_all_mention(monkeypatch):
    import event_reminder

    event = _event("E_ALL")
    event["title"] = "家族聚餐"
    event["participants"] = json.dumps(["全家"], ensure_ascii=False)

    spec = event_reminder.build_reminder_message_spec(event, 1)

    assert spec["kind"] == "textV2"
    assert spec["message"]["text"].startswith("{all}\n")
    assert spec["message"]["substitution"]["all"]["mentionee"] == {"type": "all"}
    assert spec["fallback_text"].startswith("@all\n")


def test_standard_event_participant_alias_uses_user_mention(tmp_path, monkeypatch):
    import line_mentions
    import event_reminder

    alias_path = tmp_path / "aliases.json"
    user_id = "U" + "1" * 32
    alias_path.write_text(json.dumps({user_id: "媽媽"}, ensure_ascii=False))
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(alias_path))
    line_mentions.clear_alias_cache()

    event = _event("E_MOM")
    event["participants"] = json.dumps(["媽媽"], ensure_ascii=False)

    spec = event_reminder.build_reminder_message_spec(event, 1)

    assert spec["kind"] == "textV2"
    assert spec["message"]["text"].startswith("{p1}\n")
    assert spec["message"]["substitution"]["p1"]["mentionee"] == {
        "type": "user",
        "userId": user_id,
    }
    assert spec["fallback_text"].startswith("@媽媽\n")


def test_standard_event_unknown_participant_falls_back_to_plain_at(
    tmp_path, monkeypatch
):
    import line_mentions
    import event_reminder

    alias_path = tmp_path / "aliases.json"
    alias_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(alias_path))
    line_mentions.clear_alias_cache()

    event = _event("E_FRIEND")
    event["participants"] = json.dumps(["朋友"], ensure_ascii=False)

    spec = event_reminder.build_reminder_message_spec(event, 1)

    assert spec["kind"] == "text"
    assert spec["text"].startswith("@朋友\n")


def test_sdk_message_from_spec_returns_none_when_validation_suppresses(monkeypatch):
    import event_reminder

    monkeypatch.setattr(event_reminder, "validate_push_text", lambda *_a, **_kw: "")

    assert event_reminder.sdk_message_from_spec({"kind": "text", "text": "x"}) is None
