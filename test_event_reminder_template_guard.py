"""A calendar reminder never goes out as the blocked sentence because of its @ line (2026-11-13).

`push_messages` validates the textV2 template itself on the way out, while
`event_reminder.build_reminder_message_spec` validated the body and the plain twin.  The two @ lines are
laid out differently (the template puts the resolved `{p1}` first and the unresolved names after it), so
a participant list can make only the template trip a rule; the family then got 「這則回覆在送出前被擋下…」
instead of the reminder.  The builder now checks the template too and sends the plain twin when only the
template is blocked: the @ goes, the reminder does not.  Rule 「提到人就真 @」 (agent_rules/line_bot.md,
2026-11-12).  Synthetic data only.
"""

from __future__ import annotations

import json
import uuid

import pytest

import event_reminder
import line_mentions
import memory
import output_validator

KEY = str(uuid.uuid5(uuid.NAMESPACE_URL, "test:calendar:main"))
FALLBACK_KEY = str(uuid.uuid5(uuid.NAMESPACE_URL, "test:calendar:fallback"))


# ── the calendar sender: the @ line never turns a reminder into the blocked sentence ─────────────

_LONG_NAME = "很長很長的家人稱呼一二三四五六七八九十甲乙"  # a configured alias of 19+ characters


def _verdict(text: str) -> tuple:
    result = output_validator.validate_outbound_text(text)
    return result.ok, result.reason, result.text == text


def _aliases(monkeypatch, tmp_path, mapping: dict) -> None:
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps(mapping, ensure_ascii=False))
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(path))
    line_mentions.clear_alias_cache()


def _calendar_event(title: str, participants: list, **overrides) -> dict:
    event = {
        "event_id": "E1",
        "event_date": "2027-01-15",
        "event_time": None,
        "title": title,
        "location": "",
        "participants": json.dumps(participants, ensure_ascii=False),
    }
    event.update(overrides)
    return event


def test_a_calendar_reminder_never_goes_out_as_the_blocked_sentence_because_of_its_at_line(monkeypatch, tmp_path):
    # The template puts the resolved {p1} first and the unresolved names after it, so its @ line can
    # read differently from the plain twin's: here 「今天 @2019年1月」 sits together only in the template.
    _aliases(monkeypatch, tmp_path, {"U_TEST_LONG": _LONG_NAME})
    monkeypatch.delenv("EVENT_REMINDER_ONE_OFF_JSON", raising=False)
    monkeypatch.delenv("REMINDER_ASK_EVENT_IDS", raising=False)
    monkeypatch.setenv("EVENT_REMINDER_PRIVATE_CONFIG", str(tmp_path / "missing.json"))
    event = _calendar_event("家長會", ["今天", _LONG_NAME, "2019年1月"])
    body = event_reminder._format_event(event, 1)
    assert _verdict(body)[0]
    # the precondition: the long alias resolves ({p1} exists) and only the template is blocked
    targets = line_mentions.event_mention_targets(event)
    labels = line_mentions.event_plain_labels(event)
    assert [target.key for target in targets] == ["p1"]
    template = line_mentions.text_v2_dict(body, targets, labels)["text"]
    plain = line_mentions.text_with_plain_mentions(body, targets, labels)
    assert not _verdict(template)[0] and _verdict(plain)[0]

    spec = event_reminder.build_reminder_message_spec(event, 1)

    # what LINE gets is validated again on the way out; it must be the reminder, not the blocked sentence
    monkeypatch.setattr(event_reminder, "GROUP_ID", "G1")
    monkeypatch.setattr(event_reminder, "_get_token", lambda: "token")
    posted: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"sentMessages": [{"id": "m1"}]}

    def fake_post(url, headers, json, timeout):
        posted.append(json)
        return _Resp()

    monkeypatch.setattr(event_reminder._session, "post", fake_post)
    monkeypatch.setattr(memory, "log_raw_message", lambda *_a, **_k: None)
    monkeypatch.setattr(memory, "log_sent_reminder_reference", lambda *_a, **_k: None)

    assert event_reminder._send_reminder_message_spec(spec, retry_key=KEY, fallback_retry_key=FALLBACK_KEY) == (
        event_reminder.POST_OK
    )
    sent_text = posted[0]["messages"][0]["text"]
    assert "家長會" in sent_text and "被擋下" not in sent_text
    assert spec["kind"] == "text"  # the @ is dropped, the reminder is not
    assert spec["text"] == line_mentions.text_with_plain_mentions(
        body, line_mentions.event_mention_targets(event), line_mentions.event_plain_labels(event)
    )


@pytest.mark.parametrize(
    "placeholder, mention_text, plain_text",
    [
        # the digit of {p1} is the one the "latest data" rule needs: the template alone is blocked
        ("p1", "{p1}\n請查最新資料", "媽媽\n請查最新資料"),
        # a line that reads like the bot talking to itself: the template alone is emptied
        ("target", "{target}\n保持沉默", "媽媽 保持沉默"),
    ],
)
def test_a_one_off_reminder_whose_at_template_alone_is_blocked_goes_out_plain(
    monkeypatch, tmp_path, placeholder, mention_text, plain_text
):
    monkeypatch.setenv("EVENT_REMINDER_PRIVATE_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.delenv("REMINDER_ASK_EVENT_IDS", raising=False)
    monkeypatch.setenv(
        "EVENT_REMINDER_ONE_OFF_JSON",
        json.dumps(
            {
                "event_ids": ["E1"],
                "offset": 1,
                "placeholder": placeholder,
                "mention_text": mention_text,
                "plain_text": plain_text,
                "user_id": "U_TEST_MOM",
            },
            ensure_ascii=False,
        ),
    )
    assert not _verdict(mention_text)[0] and _verdict(plain_text)[0]

    spec = event_reminder.build_reminder_message_spec(_calendar_event("家長會", []), 1)

    assert spec == {"kind": "text", "text": plain_text, "source_kind": "calendar_event", "source_ref": "E1"}


# words the outbound validator reacts to, alone and next to the program's own date line
_TITLES = (
    "家長會",
    "查最新資料",
    "去移民管理局辦證件",
    "官方統計",
    "我會打卡",
    "視情況而定",
    "現在是2019年",
    "保持沉默",
    "影片點不開",
    "帶{x}和{y}",
)
_SHAPES = (["媽媽"], ["媽媽", "爸爸"], ["媽媽", "朋友"], ["朋友", "媽媽"], ["全家"])


@pytest.mark.parametrize("offset", sorted(event_reminder._OFFSET_LABEL) + [5, -5])
@pytest.mark.parametrize("title", _TITLES)
def test_calendar_template_and_plain_twin_get_the_same_verdict(monkeypatch, tmp_path, offset, title):
    # Why the builder's template check rarely fires: the 📅 line always carries the date, so {p1} is never the digit a
    # rule needs.  Dropping that line from _format_event turns cases here red.
    _aliases(monkeypatch, tmp_path, {"U_TEST_MOM": "媽媽", "U_TEST_DAD": "爸爸"})
    monkeypatch.delenv("EVENT_REMINDER_ONE_OFF_JSON", raising=False)
    monkeypatch.delenv("REMINDER_ASK_EVENT_IDS", raising=False)
    monkeypatch.setenv("EVENT_REMINDER_PRIVATE_CONFIG", str(tmp_path / "missing.json"))
    for event_time in (None, "09:30"):
        for location in ("", "台大醫院"):
            for participants in _SHAPES:
                event = _calendar_event(title, participants, event_time=event_time, location=location)
                case = (offset, title, event_time, location, participants)
                body = event_reminder._format_event(event, offset)
                targets = line_mentions.event_mention_targets(event)
                labels = line_mentions.event_plain_labels(event)
                assert targets, case  # every case has someone to @
                template = line_mentions.text_v2_dict(body, targets, labels)["text"]
                plain = line_mentions.text_with_plain_mentions(body, targets, labels)
                assert "{p1}" in template or "{all}" in template
                # push_messages validates the template; the builder validates the plain twin
                assert _verdict(template) == _verdict(plain), case
                # and whatever the builder hands over as textV2 goes out unchanged
                spec = event_reminder.build_reminder_message_spec(event, offset)
                if spec is not None and spec["kind"] == "textV2":
                    assert _verdict(spec["message"]["text"]) == (True, "", True), case
