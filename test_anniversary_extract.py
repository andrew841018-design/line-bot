"""紀念日抽取、候選確認與推播（2026-11-13，Andrew 選「B：兩個都開」）。

依據 ops/state/line_bot_extractor_sweep_20261113/IMPL_PLAN_v2.md：PRE_3 §5.12 的紀念日項目，加上 PRE_4 的
I2（例外只記型別）、I3（管理指令）、I4（只准管理指令呼叫）、I9（決定後清欄位）、N1（只抽家族主群）。

模型只在一個接縫換掉（gemini_client.side_task_last_tier），每個抽取測試都斷言它被叫了幾次。推播走 conftest
的記錄器（line_push_client.push_messages）或換掉 anniversary._push，不會送 LINE；now 一律注入（入口測試除外，
那裡照真的時間算）。人一律寫「成員甲／乙」，id 是 user-a、mid-a1 這類字串。合成資料標記（jobs/git_privacy_audit.py
的 _SYNTHETIC_SCHEDULE_MARKER）只加在同一行同時有日期、時刻和事件字（同檔的 _PRIVATE_SCHEDULE_TOPIC_RE）的行：
這個檔沒有這種行，所以一個都不加（POST_3 N8：多加的標記會讓那一行以後永遠不被掃）。
"""

import ast
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

GROUP = "GRP_FAMILY"
OTHER = "GRP_OTHER"
TW = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent
SECRET = "成員丁的祕密暗號"
ALL_MENTION = {"all": {"type": "mention", "mentionee": {"type": "all"}}}


def at(y, m, d, hh=8, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=TW).timestamp()


NOW = at(2026, 11, 14)  # the 08:00 run
EVENING = at(2026, 11, 13, 20)  # when the family wrote

# PRE_2 P2-I1 plus PRE_3's additions, copied here on purpose: a word taken out of anniversary.py fails below.
KEYWORDS = (
    "生日", "壽", "歲", "結婚", "婚", "紀念", "忌日", "冥誕", "週年", "周年", "過世", "逝世",
    "慶生", "生快", "誕", "出生", "忌", "祭", "對年", "滿月", "往生", "去世", "嫁", "娶",
    "birthday", "bday", "hbd", "anniversary",
)
# A lunar date in the words (PRE_3 N-b), plus what POST_2 N-6 found missing: a 初X day, 冬月 and a leap month.
LUNAR_SAYINGS = ("農曆", "陰曆", "舊曆", "正月", "臘月", "冬月", "閏月", "閏四月", "十月初五", "初十")
# Solar ones that only look lunar: 閏年 is a 2/29, 月初 the start of a month, 當初／最初／起初 not a day at all.
NOT_LUNAR = (
    "成員甲生日 2/29，閏年才過", "下個月初幫成員甲慶生", "當初一起幫成員甲慶生", "最初一年的結婚紀念日 6/6",
    "起初一直忘了成員乙生日 4/6",
)
# The eleven ways PRE_3 found the old list missed (synthetic sentences).
MISSED_BEFORE = (
    "週末幫成員甲慶生", "成員甲生快！", "HBD 成員甲", "Happy Birthday 成員乙", "成員丙誕辰一百年",
    "成員甲出生那天很冷", "下週要做忌", "成員丙對年", "小孫滿月酒", "成員丙往生了", "成員丙去世三年了",
)
# 稱呼 Andrew may not type (confirm --as, add): relative words, pronouns, @-ish characters, links, format chars.
BAD_TYPED_NAMES = (
    "我阿姨", "我", "自己", "本人", "他", "大家", "", "成員@丙", "成員＠丙", "成員{丙}", "成員[丙]",
    "x" * 31, "成員‮丙", "example.com", "https://example.com/x",
)


# ── fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture
def family(monkeypatch, tmp_path):
    """The family main group is GROUP (allowed, with OTHER), two members have local aliases, the switch is
    unset and the push token is a stand-in."""
    import anniversary
    import line_push_client

    monkeypatch.setattr(anniversary.settings, "family_group_id", GROUP)
    monkeypatch.setenv("ALLOWED_GROUP_IDS", f"{GROUP},{OTHER}")
    monkeypatch.delenv(anniversary.EXTRACT_SWITCH, raising=False)
    aliases = tmp_path / "user_aliases.json"
    aliases.write_text(json.dumps({"user-a": "成員甲", "user-b": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    monkeypatch.setenv("LINE_BOT_PUSH_MENTIONS", "")
    return anniversary


@pytest.fixture
def pushes():
    import line_push_client

    return line_push_client.push_messages.calls


def _answer(items, finish="STOP", text=None):
    body = json.dumps(items, ensure_ascii=False) if text is None else text
    return SimpleNamespace(text=body, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))])


@pytest.fixture
def model(monkeypatch):
    """Answers queued per test: items, a function of the prompt, None (no answer) or a response object."""
    import gemini_client

    calls: dict = {"prompts": [], "answers": [], "ready": True}

    def side_task(prompt, config, *, label):
        calls["prompts"].append(prompt)
        answer = calls["answers"].pop(0) if calls["answers"] else []
        if callable(answer):
            answer = answer(prompt)
        if answer is None or isinstance(answer, SimpleNamespace):
            return answer
        return _answer(answer)

    monkeypatch.setattr(gemini_client, "side_task_last_tier", side_task)
    monkeypatch.setattr(gemini_client, "side_task_last_tier_ready", lambda: calls["ready"])
    return calls


def _raw(message_id, user_id, text, created_at, group=GROUP):
    import memory

    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (group, message_id, user_id, text, int(created_at)),
        )


def _lines(prompt):
    return [json.loads(line) for line in prompt.split("【訊息】\n", 1)[1].splitlines()]


def _i(prompt, needle):
    """The number the prompt gives the one message containing ``needle``."""
    [i] = [line["i"] for line in _lines(prompt) if needle in line["text"]]
    return i


def _item(i, person, month, day, kind="birthday", lunar=False):
    return {"i": i, "person": person, "anniversary_type": kind, "month": month, "day": day, "lunar": lunar}


def _says(needle, person, month, day, kind="birthday", lunar=False):
    """An answer pointing at the message with ``needle``, wherever the batch put it."""
    return lambda prompt: [_item(_i(prompt, needle), person, month, day, kind, lunar)]


def _candidates():
    import anniversary

    with anniversary._conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM anniversary_candidates ORDER BY created_at, person")]


def _anniversaries():
    import anniversary

    with anniversary._conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM anniversaries ORDER BY month, day, person")]


def _extract(anniversary, now=NOW):
    return anniversary.run_incremental(GROUP, now=now)


# ── extraction: what becomes a candidate ─────────────────────────────────────


def test_a_birthday_becomes_a_pending_candidate_read_once_and_never_pushed(family, model, pushes):
    _raw("mid-a1", "user-b", "明天成員甲生日", EVENING)
    model["answers"].append(_says("成員甲", "成員甲", 11, 14))

    assert family.main_reminder(now=NOW) == 0
    assert family.main_reminder(now=NOW + 60) == 0  # due today, but only a candidate

    assert len(model["prompts"]) == 1
    [row] = _candidates()
    assert (row["person"], row["person_raw"], row["self_substituted"], row["anniversary_type"]) == (
        "成員甲", "成員甲", 0, "birthday",
    )
    assert (row["month"], row["day"], row["status"], row["source_msg_id"]) == (11, 14, "pending", "mid-a1")
    assert (row["speaker_user_id"], row["said_at"], row["created_at"]) == ("user-b", int(EVENING), int(NOW))
    assert _anniversaries() == [] and pushes == []


def test_the_prompt_has_no_names_or_ids_and_keeps_1500_characters(family, model):
    _raw("mid-long", "user-a", "今天天氣很好" * 100 + "成員乙的生日是 3/5", EVENING)

    _extract(family)

    [prompt] = model["prompts"]
    [line] = _lines(prompt)
    assert line["text"].endswith("成員乙的生日是 3/5")  # past the 500 characters other jobs keep
    assert (line["i"], line["date"], line["time"]) == (1, "2026-11-13", "20:00")
    for word in ("user-a", "成員甲", GROUP, "mid-long"):
        assert word not in prompt


def test_one_message_with_two_birthdays_gives_two_candidates(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5，成員乙生日 4/6", EVENING)
    model["answers"].append(
        lambda p: [_item(_i(p, "成員甲"), "成員甲", 3, 5), _item(_i(p, "成員甲"), "成員乙", 4, 6)]
    )

    result = _extract(family)

    assert len(model["prompts"]) == 1 and result.new == 2
    assert sorted((r["person"], r["month"], r["day"]) for r in _candidates()) == [("成員乙", 4, 6), ("成員甲", 3, 5)]


def test_i_becomes_the_speakers_local_name_only_for_their_own_birthday(family, model):
    _raw("mid-a1", "user-a", "我生日是 6/4", EVENING)  # substituted
    _raw("mid-a2", "user-b", "我媽生日 5/1", EVENING + 1)  # 「我」 is not whose birthday it is
    _raw("mid-a3", "user-c", "我的生日是 7/7", EVENING + 2)  # no local alias
    _raw("mid-a4", "user-a", "我結婚紀念日 8/8", EVENING + 3)  # not a birthday
    _raw("mid-a5", "user-b", "我生日跟我姐同一天 9/1", EVENING + 4)  # a relative in the same sentence
    _raw("mid-a6", "user-a", "我生日那天也是他們的結婚週年 10/10", EVENING + 5)  # 「我」 as the couple
    model["answers"].append(lambda p: [
        _item(_i(p, "我生日是"), "我", 6, 4),
        _item(_i(p, "我媽"), "我", 5, 1),
        _item(_i(p, "我的生日"), "我", 7, 7),
        _item(_i(p, "我結婚"), "我", 8, 8, "wedding"),
        _item(_i(p, "我姐"), "我自己", 9, 1),
        _item(_i(p, "結婚週年"), "我", 10, 10, "wedding"),
    ])

    result = _extract(family)

    assert len(model["prompts"]) == 1
    [row] = _candidates()
    assert (row["person"], row["person_raw"], row["self_substituted"], row["month"], row["day"]) == (
        "成員甲", "我", 1, 6, 4,
    )
    assert result.skipped.get("self_unclear") == 4 and result.skipped.get("self_no_alias") == 1
    [shown] = family.review_rows()["candidates"]
    assert shown["self_substituted"] is True and shown["person_raw"] == "我" and shown["speaker"] == "成員甲"


def test_a_birthday_in_someone_elses_quoted_words_is_not_the_speakers(family, model):
    """POST_2 N-7: 「我生日」 inside quote marks is the quoted person's; only one outside them is substituted."""
    _raw("mid-a1", "user-a", "媽媽說：「我生日那天想吃火鍋」", EVENING)
    _raw("mid-a2", "user-a", "我生日 6/4，成員乙說「記得回家」", EVENING + 1)
    model["answers"].append(lambda p: [_item(_i(p, "火鍋"), "我", 11, 12), _item(_i(p, "回家"), "我", 6, 4)])

    result = _extract(family)

    assert len(model["prompts"]) == 1 and result.skipped.get("self_unclear") == 1
    [row] = _candidates()
    assert (row["person"], row["person_raw"], row["self_substituted"], row["month"], row["day"]) == (
        "成員甲", "我", 1, 6, 4,
    )


@pytest.mark.parametrize("text", [
    "成員乙說『我的生日是 6/4』", "成員乙說“我生日 6/4”", '成員乙說"我生日 6/4"', "成員乙說＂我生日 6/4＂",
    "成員乙說〝我生日 6/4〞", "成員乙說‘我生日 6/4’", "「成員乙說『我生日 6/4』才對」",
    "成員乙說：「我生日 6/4",  # never closed: the quote runs to the end
])
def test_every_kind_of_quote_mark_keeps_the_speaker_out(family, model, text):
    from family_extract import Message, RunResult

    result = RunResult()
    rows = family._prepare([Message(int(EVENING), "mid-a1", "user-a", text)], [_item(1, "我", 6, 4)], result)

    assert rows == [] and result.skipped == {"self_unclear": 1} and model["prompts"] == []


@pytest.mark.parametrize("text", ["我生日 6/4，成員乙說「記得回家」", "「生日快樂」我的生日是 6/4", "我生日 6/4 喔"])
def test_my_birthday_outside_quote_marks_is_still_the_speakers(family, model, text):
    from family_extract import Message, RunResult

    result = RunResult()
    rows = family._prepare([Message(int(EVENING), "mid-a1", "user-a", text)], [_item(1, "我", 6, 4)], result)

    assert [(r["person"], r["person_raw"], r["self_substituted"]) for r in rows] == [("成員甲", "我", 1)]
    assert result.skipped == {} and model["prompts"] == []


def test_a_name_the_message_does_not_have_is_dropped(family, model):
    _raw("mid-a1", "user-a", "他下週生日 3/5，記得", EVENING)
    model["answers"].append(lambda p: [
        _item(_i(p, "他下週"), "成員丁", 3, 5),  # not in the message
        _item(_i(p, "他下週"), "他", 3, 5),  # a pronoun
        _item(_i(p, "他下週"), "@all", 3, 5),
        _item(_i(p, "他下週"), 42, 3, 5),  # not a string
    ])

    result = _extract(family)

    assert len(model["prompts"]) == 1 and _candidates() == []
    assert result.skipped.get("provenance") == 1 and result.skipped.get("bad_person") == 3


def test_a_lunar_date_is_never_written(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 8/15", EVENING)  # the model says lunar
    _raw("mid-a2", "user-a", "成員丙生日 9/9", EVENING + 1)  # the model leaves lunar out
    for n, word in enumerate(LUNAR_SAYINGS):
        _raw(f"mid-l{n}", "user-b", f"成員乙{word}生日", EVENING + 10 + n)  # never sent
    model["answers"].append(lambda p: [
        _item(_i(p, "成員甲"), "成員甲", 8, 15, lunar=True),
        {"i": _i(p, "成員丙"), "person": "成員丙", "anniversary_type": "birthday", "month": 9, "day": 9},
    ])

    result = _extract(family)

    assert len(model["prompts"]) == 1 and "成員乙" not in model["prompts"][0]
    assert _candidates() == [] and result.skipped.get("lunar") == 2 + len(LUNAR_SAYINGS)


def test_a_lunar_day_with_no_lunar_word_is_dropped_whatever_the_model_says(family, model):
    """POST_2 N-6: 「十月初五」 has none of the old words, so only the model's flag guarded it."""
    from family_extract import Message, RunResult

    _raw("mid-a1", "user-b", "成員丙生日是十月初五，大家記得回來", EVENING)

    result = _extract(family)

    assert model["prompts"] == [] and result.skipped == {"lunar": 1}
    seen = RunResult()  # an answer pointing at such a message, lunar=false, is dropped too
    msg = Message(int(EVENING), "mid-a2", "user-b", "成員丙生日是冬月初五")
    assert family._prepare([msg], [_item(1, "成員丙", 11, 5)], seen) == [] and seen.skipped == {"lunar": 1}


def test_words_that_only_look_lunar_still_go_out(family, model):
    from family_extract import Message, RunResult

    for text in NOT_LUNAR:
        result = RunResult()
        assert family._prefilter(Message(1, "mid-a1", "user-a", text), result), text
        assert result.skipped == {}, text
    assert model["prompts"] == []


def test_impossible_dates_are_dropped_and_feb_29_is_kept(family, model):
    _raw("mid-a1", "user-a", "成員甲生日在二月底", EVENING)
    model["answers"].append([
        _item(1, "成員甲", 2, 30),
        _item(1, "成員甲", 13, 1),
        _item(1, "成員甲", "2", 1),
        _item(1, "成員甲", True, 1),
        _item(1, "成員甲", 2.0, 1),
        _item(1, "成員甲", 10**30, 1),
        _item(1, "成員甲", 2, 29),
    ])

    result = _extract(family)

    assert len(model["prompts"]) == 1 and result.skipped.get("bad_date") == 6
    assert [(r["month"], r["day"]) for r in _candidates()] == [(2, 29)]


def test_an_item_pointing_at_no_message_is_dropped(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    model["answers"].append(
        [_item(0, "成員甲", 3, 5), _item(2, "成員甲", 3, 5), _item(True, "成員甲", 3, 5), _item("1", "成員甲", 3, 5), "成員甲"]
    )

    result = _extract(family)

    assert len(model["prompts"]) == 1 and _candidates() == [] and result.skipped.get("bad_item") == 5


def test_every_keyword_still_lets_a_message_through(family):
    from family_extract import Message, RunResult

    for word in KEYWORDS + tuple(w.upper() for w in KEYWORDS if w.isascii()):
        assert family._prefilter(Message(1, "mid-a1", "user-a", f"今天{word}"), RunResult()), word
    for text in MISSED_BEFORE:
        assert family._prefilter(Message(1, "mid-a1", "user-a", text), RunResult()), text
    result = RunResult()
    assert not family._prefilter(Message(1, "mid-a1", "user-a", "今天天氣很好"), result)
    assert result.skipped == {"prefilter": 1}


def test_a_message_without_a_keyword_is_passed_over_unsent(family, model):
    _raw("mid-a1", "user-a", "晚餐吃什麼", EVENING)
    _raw("mid-a2", "user-a", "看 https://example.com/birthday", EVENING + 1)  # the word is only in a link

    result = _extract(family)

    assert model["prompts"] == [] and result.skipped == {"prefilter": 2}
    assert _extract(family, now=NOW + 60).read == 0  # the cursor passed them


def test_a_message_that_already_gave_a_candidate_is_not_sent_again(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    model["answers"].append(_says("成員甲", "成員甲", 3, 5))
    _extract(family)
    with family._conn() as c:  # a webhook resend moves the message past the cursor again
        c.execute("DELETE FROM extract_cursors WHERE name=?", (family.EXTRACT_NAME,))

    again = _extract(family, now=NOW + 60)

    assert len(model["prompts"]) == 1 and again.skipped.get("produced_before") == 1 and len(_candidates()) == 1


def test_the_first_run_reads_every_kept_message_without_a_gap(family, model):
    """The cursor starts at the oldest kept message (the backfill), not 24 hours back as the TODO job does."""
    for n, days in enumerate((15, 9, 2)):
        _raw(f"mid-a{n}", "user-a", f"成員甲第{n}次說生日", NOW - days * 86400)

    result = _extract(family)

    assert len(model["prompts"]) == 1 and result.read == 3 and result.sent == 3 and result.gap_s == 0


def test_no_answer_keeps_the_batch_and_a_blocked_one_is_passed_over(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    model["answers"].extend([None, _answer([], finish="SAFETY", text=None)])

    quiet = _extract(family)
    blocked = _extract(family, now=NOW + 60)
    after = _extract(family, now=NOW + 120)

    assert quiet.stopped == "no_answer" and blocked.skipped.get("blocked") == 1 and after.requests == 0
    assert len(model["prompts"]) == 2 and _candidates() == []


def test_a_bad_answer_is_tried_three_times_then_passed_over(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    bad = _answer([], finish="MAX_TOKENS", text='[{"i": 1')
    model["answers"].extend([bad, bad, bad])

    runs = [_extract(family, now=NOW + n * 86400) for n in range(4)]

    assert [run.stopped for run in runs[:2]] == ["bad_answer", "bad_answer"]
    assert runs[2].skipped.get("given_up") == 1 and runs[3].requests == 0
    assert len(model["prompts"]) == 3 and _candidates() == []


def test_three_requests_a_run_at_most(family, model, monkeypatch):
    import family_extract

    monkeypatch.setattr(family_extract, "BATCH_MAX_MESSAGES", 1)
    for n in range(4):
        _raw(f"mid-a{n}", "user-a", f"成員甲第{n}個生日", EVENING + n)

    first = _extract(family)
    second = _extract(family, now=NOW + 86400)

    assert first.stopped == "request_cap" and first.requests == 3 and second.requests == 1
    assert len(model["prompts"]) == 4 and "第3個" in model["prompts"][3]


def test_the_request_goes_to_the_last_tier_with_the_anniversary_schema(family, monkeypatch):
    """Through the real side_task_last_tier: never gemini-2.5-flash, as the old sweep used."""
    import config
    import gemini_client

    seen: list = []

    class Models:
        def generate_content(self, *, model, contents, config):
            seen.append((model, config))
            return _answer([])

    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=Models()))
    monkeypatch.setattr(config.settings, "gemini_last_tier_model", "gemini-test-last-tier", raising=False)
    monkeypatch.setattr(gemini_client.settings, "gemini_last_tier_model", "gemini-test-last-tier", raising=False)
    monkeypatch.delenv("GEMINI_LAST_TIER_SIDE_TASK_RESERVE", raising=False)
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)

    _extract(family)

    assert [m for m, _ in seen] == ["gemini-test-last-tier"]
    assert seen[0][0] not in {"gemini-2.5-flash", gemini_client.settings.gemini_model, gemini_client.settings.gemini_light_model}
    cfg = seen[0][1]

    def part(obj, name):
        return obj[name] if isinstance(obj, dict) else getattr(obj, name)

    schema = cfg.response_schema
    kind = part(schema, "type")
    assert str(getattr(kind, "value", kind)).upper() == "ARRAY" and cfg.http_options.timeout == 60_000
    assert set(part(part(schema, "items"), "required")) == {"i", "person", "anniversary_type", "month", "day", "lunar"}


# ── only the family main group, and the switch ───────────────────────────────


def test_only_the_family_main_group_is_read(family, model, monkeypatch):
    _raw("mid-o1", "user-a", "成員甲生日 3/5", EVENING, group=OTHER)

    assert family.main_reminder(now=NOW) == 0
    assert family.run_incremental(OTHER, now=NOW).stopped == "not_family_group"
    assert model["prompts"] == []

    _raw("mid-a1", "user-a", "成員乙生日 4/6", EVENING)
    monkeypatch.setenv("ALLOWED_GROUP_IDS", OTHER)  # the family group set, but not an allowed group
    assert family.main_reminder(now=NOW + 60) == 0
    assert family.main_extractor(now=NOW + 60) == 1
    assert model["prompts"] == [] and _candidates() == []


@pytest.mark.parametrize("value, sent", [("", 1), ("on", 1), ("off", 0), ("0", 0), ("of", 0)])
def test_the_switch_stops_extraction_only(family, model, pushes, monkeypatch, caplog, value, sent):
    monkeypatch.setenv(family.EXTRACT_SWITCH, value)
    family.add("成員甲", "birthday", 11, 14, now=NOW - 86400)
    _raw("mid-a1", "user-b", "成員乙生日 4/6", EVENING)
    model["answers"].append(_says("成員乙", "成員乙", 4, 6))
    caplog.set_level(logging.WARNING)

    assert family.main_reminder(now=NOW) == 0
    assert family.main_extractor(now=NOW + 60) == 0

    assert len(pushes) == 1 and len(model["prompts"]) == sent and len(_candidates()) == sent
    if value == "of":
        assert family.EXTRACT_SWITCH in caplog.text


# ── Andrew's decisions ────────────────────────────────────────────────────────


def test_a_confirmed_candidate_is_pushed_a_week_before_the_day_before_and_on_the_day(family, model, pushes):
    _raw("mid-a1", "user-b", "成員甲生日 11/25", EVENING)
    model["answers"].append(_says("成員甲", "成員甲", 11, 25))
    family.main_reminder(now=NOW)
    assert len(model["prompts"]) == 1
    [row] = _candidates()

    out = family.confirm(row["candidate_id"][:8], now=NOW + 3600)  # 09:00, after today's run

    assert (out["next_push"]["date"], out["next_push"]["offset"]) == ("2026-11-18", 7)
    [ann] = _anniversaries()
    assert (ann["person"], ann["month"], ann["day"], ann["source_msg_id"], ann["source_text"]) == (
        "成員甲", 11, 25, "mid-a1", None,
    )
    assert ann["anniversary_id"] == out["anniversary_id"] and out["existed"] is False
    [row] = _candidates()
    assert row["status"] == "confirmed" and (row["person_raw"], row["speaker_user_id"], row["said_at"]) == (None, None, None)
    by_day = {}
    for d in range(15, 27):
        before = len(pushes)
        family.push_today_reminders(GROUP, now=at(2026, 11, d))
        by_day[d] = len(pushes) - before
    assert {d: n for d, n in by_day.items() if n} == {18: 1, 24: 1, 25: 1}
    messages = [call["messages"][0] for call in pushes]
    assert [m["type"] for m in messages] == ["textV2"] * 3 and all(m["substitution"] == ALL_MENTION for m in messages)
    assert [m["text"].split("\n")[1] for m in messages] == [
        "🔔 1 週後是 成員甲 的生日 🎂", "🔔 明天是 成員甲 的生日 🎂", "🔔 今天是 成員甲 的生日 🎂",
    ]
    assert {call["to"] for call in pushes} == {GROUP}


def test_a_relative_name_waits_for_as(family, model):
    _raw("mid-a1", "user-b", "我媽生日 5/1", EVENING)
    model["answers"].append(_says("我媽", "我媽", 5, 1))
    _extract(family)
    assert len(model["prompts"]) == 1
    [row] = _candidates()
    assert row["person"] == "我媽" and row["self_substituted"] == 0
    assert family.review_rows()["candidates"][0]["needs_as"] is True

    with pytest.raises(family.Refused):
        family.confirm(row["candidate_id"][:8], now=NOW)
    for bad in BAD_TYPED_NAMES:
        with pytest.raises(family.Refused):
            family.confirm(row["candidate_id"][:8], as_person=bad, now=NOW)
    assert _anniversaries() == [] and _candidates()[0]["status"] == "pending"

    out = family.confirm(row["candidate_id"][:8], as_person="成員丙", now=NOW)

    [ann] = _anniversaries()
    assert (ann["person"], ann["anniversary_id"]) == ("成員丙", out["anniversary_id"])
    [row] = _candidates()
    assert (row["person"], row["status"]) == ("我媽", "confirmed")  # the candidate keeps its own word


def test_reject_leaves_a_tombstone_the_next_mention_cannot_pass(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    model["answers"].append(_says("成員甲", "成員甲", 3, 5))
    _extract(family)
    [row] = _candidates()

    assert family.reject(row["candidate_id"][:8], now=NOW) == row["candidate_id"]
    with pytest.raises(family.Refused):  # decided already: only pending ones
        family.confirm(row["candidate_id"][:8], now=NOW)

    _raw("mid-a2", "user-b", "成員甲生日是 3/5 喔", NOW + 3600)
    model["answers"].append(_says("成員甲", "成員甲", 3, 5))
    result = _extract(family, now=NOW + 86400)

    assert len(model["prompts"]) == 2 and result.skipped.get("duplicate") == 1
    [row] = _candidates()
    assert row["status"] == "rejected" and (row["person_raw"], row["speaker_user_id"], row["said_at"]) == (None, None, None)
    assert family.review_rows()["candidates"] == [] and _anniversaries() == []


def test_add_turns_a_pending_or_rejected_candidate_into_confirmed(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    _raw("mid-a2", "user-a", "成員乙生日 4/6", EVENING + 1)
    model["answers"].append(lambda p: [_item(_i(p, "成員甲"), "成員甲", 3, 5), _item(_i(p, "成員乙"), "成員乙", 4, 6)])
    _extract(family)
    assert len(model["prompts"]) == 1
    [rejected] = [r for r in _candidates() if r["person"] == "成員乙"]
    family.reject(rejected["candidate_id"][:8], now=NOW)

    first = family.add("成員甲", "birthday", 3, 5, now=NOW)
    second = family.add(" 成員乙 ", "birthday", 4, 6, now=NOW)
    again = family.add("成員甲", "birthday", 3, 5, now=NOW)

    assert (first["existed"], first["promoted"], second["existed"], second["promoted"]) == (False, 1, False, 1)
    assert (again["existed"], again["promoted"], again["anniversary_id"]) == (True, 0, first["anniversary_id"])
    assert sorted((r["person"], r["status"]) for r in _candidates()) == [("成員乙", "confirmed"), ("成員甲", "confirmed")]
    assert all((r["person_raw"], r["speaker_user_id"], r["said_at"]) == (None, None, None) for r in _candidates())
    assert sorted((a["person"], a["month"], a["day"], a["source_text"]) for a in _anniversaries()) == [
        ("成員乙", 4, 6, None), ("成員甲", 3, 5, None),
    ]


def test_add_checks_names_and_dates_as_confirm_as_does(family):
    for bad in BAD_TYPED_NAMES:
        with pytest.raises(family.Refused):
            family.add(bad, "birthday", 6, 4, now=NOW)
    with pytest.raises(family.Refused):
        family.add("成員丙", "birthdy", 6, 4, now=NOW)
    for month, day in ((2, 30), (13, 1), (0, 1), (6, 31), (True, 1)):
        with pytest.raises(family.Refused):
            family.add("成員丙", "birthday", month, day, now=NOW)
    assert _anniversaries() == []
    assert family.add("成員丙", "birthday", 2, 29, now=NOW)["existed"] is False


def test_add_needs_the_family_main_group(family, monkeypatch):
    monkeypatch.setenv("ALLOWED_GROUP_IDS", OTHER)
    with pytest.raises(family.Refused):
        family.add("成員丙", "birthday", 6, 4, now=NOW)
    assert _anniversaries() == []


def test_remove_stops_the_push_and_leaves_a_tombstone(family, model, pushes):
    aid = family.add("成員甲", "birthday", 11, 14, now=NOW - 86400)["anniversary_id"]

    assert family.remove(aid[:8], now=NOW - 3600) == aid
    _raw("mid-a1", "user-b", "明天成員甲生日", EVENING)
    model["answers"].append(_says("成員甲", "成員甲", 11, 14))
    assert family.main_reminder(now=NOW) == 0

    assert pushes == [] and _anniversaries() == [] and len(model["prompts"]) == 1
    [row] = _candidates()
    assert (row["person"], row["status"], row["month"], row["day"], row["person_raw"]) == ("成員甲", "rejected", 11, 14, None)
    with pytest.raises(family.Refused):
        family.remove(aid[:8], now=NOW)


def test_an_id_must_point_at_exactly_one_row(family):
    for bad in ("", "abc", "%", "_" * 8, "all", "1-9", "a" * 33, "ＡＢＣＤＥＦ"):
        with pytest.raises(family.Refused):
            family.reject(bad, now=NOW)
    with pytest.raises(family.Refused):
        family.remove("abcdef", now=NOW)


@pytest.mark.parametrize("now, month, day, want", [
    (at(2026, 11, 14, 7), 11, 21, ("2026-11-14", 7, "")),  # before today's 08:00 run
    (at(2026, 11, 14, 9), 11, 21, ("2026-11-20", 1, "這次一週前那則已經過了，只剩前一天和當天")),
    (at(2026, 11, 20, 9), 11, 21, ("2026-11-21", 0, "這次只剩當天")),
    (at(2026, 11, 21, 9), 11, 21, ("2027-11-14", 7, "這次（2026-11-21）的三則都已經過了，不推")),
    # POST_2 N-8: a push past New Year still belongs to this time (it used to say 「今年不推」)
    (at(2026, 12, 30, 9), 1, 2, ("2027-01-01", 1, "這次一週前那則已經過了，只剩前一天和當天")),
    (at(2026, 12, 30, 9), 1, 4, ("2027-01-03", 1, "這次一週前那則已經過了，只剩前一天和當天")),
    (at(2026, 12, 30, 9), 1, 1, ("2026-12-31", 1, "這次一週前那則已經過了，只剩前一天和當天")),
    (at(2026, 12, 31, 9), 1, 1, ("2027-01-01", 0, "這次只剩當天")),
    (at(2027, 1, 1, 9), 1, 1, ("2027-12-25", 7, "這次（2027-01-01）的三則都已經過了，不推")),  # it said nothing
    (at(2027, 2, 1, 9), 2, 29, ("2027-02-21", 7, "")),
    (at(2027, 2, 28, 9), 2, 29, ("2028-02-22", 7, "這次（2027-02-28）的三則都已經過了，不推")),
    (at(2027, 3, 1, 9), 2, 29, ("2028-02-22", 7, "")),
])
def test_confirm_says_when_the_next_push_is(family, now, month, day, want):
    info = family.next_push(month, day, now=now)
    assert (info["date"], info["offset"], info["note"]) == want


# ── the push ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("day, label", [
    ((2027, 2, 21), "1 週後"), ((2027, 2, 27), "明天"), ((2027, 2, 28), "今天"),
    ((2028, 2, 22), "1 週後"), ((2028, 2, 28), "明天"), ((2028, 2, 29), "今天"),
])
def test_feb_29_is_pushed_on_these_six_days_once_each(family, monkeypatch, day, label):
    family.add("成員甲", "birthday", 2, 29, now=NOW)
    sent: list[str] = []
    monkeypatch.setattr(family, "_push", lambda g, text, targets=(), *, retry_key: sent.append(text) or True)

    assert family.push_today_reminders(GROUP, now=at(*day)) == 1

    assert len(sent) == 1 and sent[0].startswith(f"🔔 {label}是 成員甲 的生日")


@pytest.mark.parametrize("day", [(2027, 2, 20), (2027, 2, 26), (2027, 3, 1), (2028, 2, 21), (2028, 2, 27), (2028, 3, 1)])
def test_feb_29_is_not_pushed_on_other_days(family, monkeypatch, day):
    family.add("成員甲", "birthday", 2, 29, now=NOW)
    sent: list[str] = []
    monkeypatch.setattr(family, "_push", lambda g, text, targets=(), *, retry_key: sent.append(text) or True)

    assert family.push_today_reminders(GROUP, now=at(*day)) == 0 and sent == []


def test_the_08_00_job_pushes_before_it_reads(family, model, monkeypatch):
    family.add("成員甲", "birthday", 11, 14, now=NOW - 86400)
    _raw("mid-a1", "user-b", "成員乙生日 4/6", EVENING)
    order: list[str] = []
    monkeypatch.setattr(family, "_push", lambda g, text, targets=(), *, retry_key: order.append("push") or True)
    model["answers"].append(lambda p: order.append("model") or [])

    assert family.main_reminder(now=NOW) == 0

    assert order == ["push", "model"]


def _push_row(group=GROUP):
    """(created_at, message_id, updated_at, last_push) of the anniversary cursor row, or None."""
    import anniversary

    with anniversary._conn() as c:
        try:
            row = c.execute(
                "SELECT created_at, message_id, updated_at, last_push FROM extract_cursors WHERE name=? AND group_id=?",
                (anniversary.EXTRACT_NAME, group),
            ).fetchone()
        except sqlite3.OperationalError:  # no table yet
            return None
    return None if row is None else (row[0], row[1], row[2], json.loads(row[3]))


def test_the_08_00_push_counts_go_into_the_cursor_row_and_the_backfill_still_reads_the_oldest(family, model, pushes):
    """POST_2 N-10: jobs/family_extract_status.py reads these counts.  The push makes the cursor row when there is
    none, where the extraction's own first run starts it: that same run still reads a 15-day-old message."""
    family.add("成員甲", "birthday", 11, 14, now=NOW - 86400)
    _raw("mid-a1", "user-b", "成員乙生日 4/6", NOW - 15 * 86400)
    model["answers"].append(_says("成員乙", "成員乙", 4, 6))
    assert _push_row() is None

    assert family.main_reminder(now=NOW) == 0

    assert len(pushes) == 1 and len(model["prompts"]) == 1
    assert [(r["person"], r["month"], r["day"]) for r in _candidates()] == [("成員乙", 4, 6)]
    created_at, message_id, _updated, push = _push_row()
    assert push == {"mode": "on", "date": "2026-11-14", "due": 1, "pushed": 1, "push_ok": True}
    assert (created_at, message_id) == (int(NOW - 15 * 86400), "mid-a1")  # the extraction moved on from there


def test_with_the_extraction_off_the_push_row_starts_where_the_extraction_will(family, model, monkeypatch):
    monkeypatch.setenv(family.EXTRACT_SWITCH, "off")
    _raw("mid-a1", "user-b", "成員乙生日 4/6", NOW - 15 * 86400)

    assert family.main_reminder(now=NOW) == 0

    created_at, message_id, updated_at, push = _push_row()
    assert (created_at, message_id) == family._feature(GROUP, NOW).initial_cursor(NOW) == (0, "")
    assert updated_at == int(NOW)  # not 0: the status job reads an extraction that never runs as 26 hours old
    assert push == {"mode": "off", "date": "2026-11-14", "due": 0, "pushed": 0, "push_ok": True}
    assert _push_row(OTHER) is None  # only the family main group's row: the one the status job reads
    assert model["prompts"] == []

    monkeypatch.setenv(family.EXTRACT_SWITCH, "on")
    model["answers"].append(_says("成員乙", "成員乙", 4, 6))
    assert _extract(family, now=NOW + 60).read == 1 and len(model["prompts"]) == 1


def test_a_push_that_fails_is_counted_and_a_later_run_that_day_sends_it(family, monkeypatch):
    family.add("成員甲", "birthday", 11, 14, now=NOW - 86400)
    family.add("成員乙", "birthday", 11, 15, now=NOW - 86400)
    answers = [False, True, True]  # the day before (成員乙) fails once, today's (成員甲) goes
    sent: list[str] = []
    monkeypatch.setattr(family, "_push", lambda g, text, targets=(), *, retry_key: sent.append(text) or answers.pop(0))

    assert family.push_today_reminders(GROUP, now=NOW) == 1
    assert _push_row()[3] == {"mode": "on", "date": "2026-11-14", "due": 2, "pushed": 1, "push_ok": False}

    assert family.push_today_reminders(GROUP, now=NOW + 600) == 1  # run again that morning
    assert _push_row()[3] == {"mode": "on", "date": "2026-11-14", "due": 1, "pushed": 1, "push_ok": True}
    assert len(sent) == 3 and answers == []


def test_an_error_in_the_push_leaves_only_its_type_in_the_cursor_row(family, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(*_a, **_k):
        raise ValueError(SECRET)

    monkeypatch.setattr(family, "falling_on", boom)

    with pytest.raises(ValueError):  # main() turns it into exit 1 and the type (test below)
        family.push_today_reminders(GROUP, now=NOW)

    push = _push_row()[3]
    assert push == {"mode": "on", "date": "2026-11-14", "due": 0, "pushed": 0, "push_ok": False, "error": "ValueError"}
    assert SECRET not in caplog.text


def test_counts_that_cannot_be_saved_never_stop_the_push(family, monkeypatch, pushes, caplog):
    import family_extract

    caplog.set_level(logging.DEBUG)
    family.add("成員甲", "birthday", 11, 14, now=NOW - 86400)

    def boom(*_a, **_k):
        raise sqlite3.OperationalError(SECRET)

    monkeypatch.setattr(family_extract, "save_last_push", boom)

    assert family.push_today_reminders(GROUP, now=NOW) == 1

    assert len(pushes) == 1 and "anniversary push counts not saved error_type=OperationalError" in caplog.text
    assert SECRET not in caplog.text


# ── unsend ───────────────────────────────────────────────────────────────────


def test_a_message_unsent_before_08_00_never_goes_out(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)

    assert family.forget_message(GROUP, "mid-a1", now=NOW - 3600) == 0
    result = _extract(family)

    assert model["prompts"] == [] and result.skipped.get("unsent") == 1 and _candidates() == []


def test_a_message_unsent_while_the_model_answers_writes_nothing(family, model):
    """The batch transaction reads extract_unsent again."""
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)

    def answer(prompt):
        family.forget_message(GROUP, "mid-a1", now=NOW)
        return [_item(_i(prompt, "成員甲"), "成員甲", 3, 5)]

    model["answers"].append(answer)
    result = _extract(family)

    assert len(model["prompts"]) == 1 and _candidates() == [] and result.skipped.get("unsent") == 1


def test_forget_deletes_the_pending_candidate_and_keeps_the_confirmed_one(family, model):
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    _raw("mid-a2", "user-a", "成員乙生日 4/6", EVENING + 1)
    model["answers"].append(lambda p: [_item(_i(p, "成員甲"), "成員甲", 3, 5), _item(_i(p, "成員乙"), "成員乙", 4, 6)])
    _extract(family)
    assert len(model["prompts"]) == 1
    [kept] = [r for r in _candidates() if r["person"] == "成員乙"]
    family.confirm(kept["candidate_id"][:8], now=NOW)

    assert family.forget_message(GROUP, "mid-a1", now=NOW) == 1
    assert family.forget_message(GROUP, "mid-a2", now=NOW) == 0

    assert [(r["person"], r["status"]) for r in _candidates()] == [("成員乙", "confirmed")]
    assert [a["person"] for a in _anniversaries()] == ["成員乙"]


def test_the_bots_unsend_cleanup_waits_5_seconds_and_the_08_00_job_30(family, model, monkeypatch):
    """POST_1 N-9, POST_4 N1: main's unsend hook runs in the webhook thread, one LINE event at a time, so a
    locked DB may hold it 5 seconds, not 30.  The 08:00 job keeps its 30 (PRE_4 N4)."""
    waits: list = []
    real = family.connect_private_sqlite

    def spy(path, *args, **kwargs):
        waits.append(kwargs.get("timeout"))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(family, "connect_private_sqlite", spy)

    assert family.forget_message(GROUP, "mid-a1", now=NOW) == 0
    assert waits == [5]

    waits.clear()
    family.add("成員甲", "birthday", 11, 14, now=NOW - 86400)
    _raw("mid-a2", "user-b", "成員乙生日 4/6", EVENING)
    assert family.main_reminder(now=NOW) == 0 and len(model["prompts"]) == 1
    assert len(waits) > 5 and set(waits) == {30}


# ── logs and errors ──────────────────────────────────────────────────────────


def test_logs_carry_no_family_words(family, model, pushes, caplog):
    caplog.set_level(logging.DEBUG)
    _raw("mid-x9", "user-b", "成員甲生日 11/21 記得買蛋糕", EVENING)
    model["answers"].append(_says("成員甲", "成員甲", 11, 21))

    family.main_reminder(now=NOW)
    [row] = _candidates()
    family.confirm(row["candidate_id"][:8], now=NOW + 3600)
    family.main_reminder(now=at(2026, 11, 20))

    assert len(pushes) == 1 and len(model["prompts"]) == 1
    assert "anniversary reminder sent id=" in caplog.text
    for word in ("成員甲", "成員乙", "蛋糕", "user-b", "mid-x9", GROUP):
        assert word not in caplog.text, word


def test_odd_answers_and_exceptions_leave_only_types_behind(family, model, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    _raw("mid-a1", "user-a", "成員甲生日 3/5", EVENING)
    model["answers"].append([
        {"i": SECRET, "person": SECRET, "anniversary_type": SECRET, "month": SECRET, "day": SECRET, "lunar": False},
        {"i": 1, "person": SECRET, "anniversary_type": SECRET, "month": SECRET, "day": 5, "lunar": False},
        {"i": 1, "person": SECRET, "anniversary_type": SECRET, "month": 3, "day": 5, "lunar": False},
    ])
    result = _extract(family)
    assert _candidates() == [] and result.skipped == {"bad_item": 1, "bad_date": 1, "provenance": 1}

    def boom(*_a, **_k):
        raise ValueError(SECRET)

    _raw("mid-a2", "user-a", "成員乙生日 4/6", EVENING + 1)
    monkeypatch.setattr(family, "_prepare", boom)
    assert family.main_reminder(now=NOW + 60) == 0  # the extraction failed, the job did not
    assert "anniversary extract failed error_type=ValueError" in caplog.text
    monkeypatch.setattr(family, "push_today_reminders", boom)
    assert family.main([]) == 1
    assert "anniversary job failed error_type=ValueError" in caplog.text

    out, err = capsys.readouterr()
    assert SECRET not in caplog.text + out + err and len(model["prompts"]) == 2


# ── the launchd entry ────────────────────────────────────────────────────────


def test_a_mistyped_or_retired_command_neither_pushes_nor_asks_the_model(family, model, pushes, capsys):
    today = datetime.now(TW).date()
    family.add("成員甲", "birthday", today.month, today.day)
    _raw("mid-a1", "user-b", "成員乙生日 4/6", time.time() - 3600)
    model["answers"].append(_says("成員乙", "成員乙", 4, 6))

    assert family.main(["confrim"]) == 2
    assert family.main(["review"]) == 2
    assert family.main(["extract", "now"]) == 2
    assert family.main(["extract-historical"]) == 0
    assert "退休" in capsys.readouterr().out
    assert pushes == [] and model["prompts"] == []

    assert family.main(["extract"]) == 0  # reads, never pushes
    assert pushes == [] and len(model["prompts"]) == 1
    assert family.main([]) == 0  # the 08:00 run pushes today's
    assert len(pushes) == 1 and len(model["prompts"]) == 1


# ── jobs/anniversary_review.py ───────────────────────────────────────────────


def _cli():
    from jobs import anniversary_review

    return anniversary_review


@pytest.fixture
def production(family, tmp_path, monkeypatch):
    """A folder the review command accepts: .env, the family group among the allowed ones, the DB file."""
    base = tmp_path / "production"
    base.mkdir()
    env = {"FAMILY_GROUP_ID": GROUP, "ALLOWED_GROUP_IDS": f"{GROUP},{OTHER}", "SQLITE_PATH": str(family._DB_PATH)}
    (base / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    for key, value in env.items():  # set first, so load_dotenv adds nothing this test would leave behind
        monkeypatch.setenv(key, value)
    return base


def test_the_review_command_refuses_outside_the_production_folder(family, tmp_path, monkeypatch, capsys):
    cli = _cli()
    monkeypatch.setattr(family, "review_rows", lambda **_k: pytest.fail("read the DB outside production"))
    monkeypatch.setenv("FAMILY_GROUP_ID", GROUP)

    assert cli.main(["review"], base=tmp_path) == 2  # no .env: a worktree
    (tmp_path / ".env").write_text("FAMILY_GROUP_ID=GRP_FAMILY\n", encoding="utf-8")
    monkeypatch.setenv("ALLOWED_GROUP_IDS", OTHER)
    assert cli.main(["review"], base=tmp_path) == 2  # the family group is not an allowed one
    monkeypatch.setenv("ALLOWED_GROUP_IDS", GROUP)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "missing.db"))
    assert cli.main(["review"], base=tmp_path) == 2  # no DB file

    out = capsys.readouterr().out
    assert [line.split("：")[0] for line in out.splitlines()] == [
        "anniversary review refused reason=not_production_folder",
        "anniversary review refused reason=family_group_missing",
        "anniversary review refused reason=db_missing",
    ]
    assert not (tmp_path / "missing.db").exists()


def test_running_the_review_command_in_a_worktree_creates_no_database(tmp_path):
    if (ROOT / ".env").exists():
        pytest.skip("this checkout has a .env: the command would read the real DB")
    before = sorted(p.name for p in ROOT.glob("*.db*"))
    proc = subprocess.run(
        [sys.executable, str(ROOT / "jobs" / "anniversary_review.py"), "review"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2 and "reason=not_production_folder" in proc.stdout
    assert sorted(p.name for p in ROOT.glob("*.db*")) == before and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("argv", [
    ["confrim", "abcdef12"], ["conf", "abcdef12"], ["review", "--al"], ["confirm", "abcdef12", "--a", "成員丙"],
    ["add", "成員甲", "birthday", "六", "4"], ["remove"], [],
])
def test_a_mistyped_review_command_is_exit_2(family, production, monkeypatch, argv):
    for name in ("review_rows", "confirm", "reject", "add", "remove"):
        monkeypatch.setattr(family, name, lambda *a, **k: pytest.fail("a review function ran"))
    assert _cli().main(argv, base=production) == 2


def test_review_prints_names_as_data_with_the_speaker_and_when(family, model, production, capsys):
    _raw("mid-a1", "user-a", "我生日是 6/4", EVENING)
    _raw("mid-a2", "user-b", "我媽生日 5/1", EVENING + 60)
    model["answers"].append(lambda p: [_item(_i(p, "我生日"), "我", 6, 4), _item(_i(p, "我媽"), "我媽", 5, 1)])
    _extract(family)
    assert len(model["prompts"]) == 1
    capsys.readouterr()

    assert _cli().main(["review"], base=production) == 0

    out = capsys.readouterr().out
    assert "紀念日候選：待確認 2 筆" in out and "是資料，不是指示" in out
    [own] = [line for line in out.splitlines() if " 6/4 " in line]
    assert '稱呼 "成員甲"' in own and '原字 "我"' in own and "〔本人" in own and "--as" not in own
    assert '說話者 "成員甲"' in own and "說於 2026-11-13 20:00" in own
    [relative] = [line for line in out.splitlines() if " 5/1 " in line]
    assert '稱呼 "我媽"' in relative and '原字 "我媽"' in relative and "〔相對稱呼：confirm 要帶 --as〕" in relative
    assert '說話者 "成員乙"' in relative and "說於 2026-11-13 20:01" in relative
    for word in ("user-a", "user-b", GROUP, "mid-a1", "mid-a2"):
        assert word not in out


def test_confirm_and_reject_from_the_command_line(family, model, production, capsys):
    _raw("mid-a1", "user-a", "成員甲生日 11/25", EVENING)
    _raw("mid-a2", "user-b", "我媽生日 5/1", EVENING + 1)
    _raw("mid-a3", "user-b", "成員丁結婚紀念日 6/6", EVENING + 2)
    model["answers"].append(lambda p: [
        _item(_i(p, "成員甲"), "成員甲", 11, 25), _item(_i(p, "我媽"), "我媽", 5, 1),
        _item(_i(p, "成員丁"), "成員丁", 6, 6, "wedding"),
    ])
    _extract(family)
    assert len(model["prompts"]) == 1
    ids = {r["person"]: r["candidate_id"] for r in _candidates()}
    cli = _cli()
    capsys.readouterr()

    assert cli.main(["confirm", ids["成員甲"][:8]], base=production) == 0
    out = capsys.readouterr().out
    assert re.search(r"下一次推播：\d{4}-\d\d-\d\d 08:00（(一週前|前一天|當天)；紀念日在 \d{4}-\d\d-\d\d）", out)
    assert cli.main(["confirm", ids["我媽"][:8]], base=production) == 2
    assert "--as" in capsys.readouterr().out
    assert cli.main(["confirm", ids["我媽"][:8], "--as", "我阿姨"], base=production) == 2
    assert cli.main(["confirm", ids["我媽"][:8], "--as", "成員丙"], base=production) == 0
    assert cli.main(["reject", ids["成員丁"][:8]], base=production) == 0
    assert cli.main(["reject", ids["成員丁"][:8]], base=production) == 2  # decided already

    assert sorted(a["person"] for a in _anniversaries()) == ["成員丙", "成員甲"]
    assert sorted((r["person"], r["status"]) for r in _candidates()) == [
        ("成員丁", "rejected"), ("成員甲", "confirmed"), ("我媽", "confirmed"),
    ]


def test_add_review_all_and_remove_from_the_command_line(family, production, capsys):
    cli = _cli()

    assert cli.main(["add", "成員丙", "birthday", "2", "29"], base=production) == 0
    assert cli.main(["add", "成員丙", "birthday", "2", "29"], base=production) == 0
    assert cli.main(["add", "我媽", "birthday", "5", "1"], base=production) == 2
    assert cli.main(["add", "成員丙", "birthday", "2", "30"], base=production) == 2
    out = capsys.readouterr().out
    assert "已加入：" in out and "已存在：" in out and "下一次推播：" in out

    [aid] = [a["anniversary_id"] for a in _anniversaries()]
    assert cli.main(["review", "--all"], base=production) == 0
    out = capsys.readouterr().out
    assert "正在推的紀念日 1 筆" in out and f'- {aid[:8]} 生日 2/29 稱呼 "成員丙"' in out

    assert cli.main(["remove", aid[:8]], base=production) == 0
    assert cli.main(["remove", aid[:8]], base=production) == 2
    assert _anniversaries() == [] and [(r["person"], r["status"]) for r in _candidates()] == [("成員丙", "rejected")]


def test_an_unexpected_error_in_the_review_command_prints_its_type_only(family, production, monkeypatch, capsys):
    def boom(**_k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(family, "review_rows", boom)

    assert _cli().main(["review"], base=production) == 1

    out, err = capsys.readouterr()
    assert "error_type=RuntimeError" in out and SECRET not in out + err


# ── only jobs/anniversary_review.py calls the review functions (PRE_4 I4) ─────────

_REVIEW_FUNCTIONS = frozenset({"review_rows", "confirm", "reject", "add", "remove", "insert_anniversary"})
_REVIEW_COMMAND = "jobs/anniversary_review.py"


def _review_function_uses(root: Path) -> list[tuple[str, str]]:
    """Every ``anniversary.<review function>`` (by any name the module is imported as), ``from anniversary
    import`` of one, ``getattr(anniversary, ...)`` of one, and a bare use inside anniversary.py itself.  It
    stops a new caller written the usual way, not someone set on getting around it."""
    found: list[tuple[str, str]] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith((".", "__"))]  # worktrees and venvs
        for name in files:
            if not name.endswith(".py") or name.startswith("test_") or name == "conftest.py":
                continue
            path = Path(folder) / name
            where = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases |= {a.asname or a.name for a in node.names if a.name == "anniversary"}
                elif isinstance(node, ast.ImportFrom) and node.module == "anniversary":
                    found += [
                        (where, f"from anniversary import {a.name}")
                        for a in node.names
                        if a.name in _REVIEW_FUNCTIONS or a.name == "*"
                    ]
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute) and node.attr in _REVIEW_FUNCTIONS
                    and isinstance(node.value, ast.Name) and node.value.id in aliases
                ):
                    found.append((where, f"{node.value.id}.{node.attr}"))
                elif (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                    and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in _REVIEW_FUNCTIONS
                ):
                    found.append((where, f"getattr {node.args[1].value}"))
                elif where == "anniversary.py" and isinstance(node, ast.Name) and node.id in _REVIEW_FUNCTIONS:
                    found.append((where, node.id))
    return found


def test_only_the_review_command_calls_the_review_functions():
    import anniversary

    uses = _review_function_uses(Path(anniversary.__file__).resolve().parent)

    assert sorted({use for use in uses if use[0] != _REVIEW_COMMAND}) == []
    assert {what for where, what in uses if where == _REVIEW_COMMAND} == {
        "anniversary.review_rows", "anniversary.confirm", "anniversary.reject", "anniversary.add", "anniversary.remove",
    }


def test_the_review_command_imports_anniversary_only_after_its_check():
    """No module-level import: importing anniversary creates its tables (PRE_4 I3)."""
    tree = ast.parse((ROOT / "jobs" / "anniversary_review.py").read_text(encoding="utf-8"))
    top = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = {a.name for node in top for a in node.names} | {node.module for node in top if isinstance(node, ast.ImportFrom)}
    assert "anniversary" not in names and "config" not in names
