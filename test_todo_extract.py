"""TODO extraction, push folds, switch and unsend (2026-11-13, Andrew 選「B：兩個都開」).

Design: ops/state/line_bot_extractor_sweep_20261113/IMPL_PLAN_v2.md (PRE_3 §5 with PRE_4's fixes), and
FIX_LIST.md items 1–12 after the four post-implementation reviews (POST_1–POST_4).
The model is faked at its one seam (gemini_client.side_task_last_tier), and every test that runs the
extraction asserts how often it was called.  Pushes go to conftest's recorder, never LINE.  People are
成員甲／成員乙 (user-a／user-b), texts and ids are synthetic, and now is always injected.
"""

import ast
import itertools
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

TW = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent
GROUP = "grp-family"
OTHER = "grp-other"
TODAY = date(2026, 11, 13)  # a Friday
YESTERDAY = TODAY - timedelta(days=1)


def _ts(day: date, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TW).timestamp())


NOW = _ts(TODAY, 8)  # the 08:00 job
LAST_NIGHT = _ts(YESTERDAY, 20)


def _iso(day: date) -> str:
    return day.isoformat()


# ── fixtures ────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def todo_env(monkeypatch, tmp_path, reset_main_globals, block_external_side_effects):
    """Mode on, the family group allowed, user-a／user-b named, a stand-in token, and no real sleep."""
    import config
    import family_extract
    import line_push_client

    monkeypatch.setenv("LINE_BOT_TODO_MODE", "on")
    monkeypatch.setenv("ALLOWED_GROUP_IDS", f"{GROUP},{OTHER}")
    # every settings object the jobs may hold: test_main_calendar_query reloads config, so todo.settings
    # can differ from config.settings (suite-order dependence found 11/14)
    import anniversary
    import todo

    for holder in {id(s): s for s in (config.settings, todo.settings, anniversary.settings)}.values():
        monkeypatch.setattr(holder, "family_group_id", GROUP)
    aliases = tmp_path / "user_aliases.json"
    aliases.write_text(json.dumps({"user-a": "成員甲", "user-b": "成員乙"}, ensure_ascii=False), encoding="utf-8")
    roles = tmp_path / "family_roles.local.json"
    roles.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(aliases))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(roles))
    monkeypatch.setattr(line_push_client, "line_access_token", lambda fallback_token=None: "token")
    monkeypatch.delenv("LINE_BOT_PUSH_MENTIONS", raising=False)
    sleeps: list = []
    # Only family_extract's own clock: the process-wide time.sleep stays real for conftest's retries.
    monkeypatch.setattr(family_extract, "time", SimpleNamespace(time=time.time, sleep=sleeps.append))
    return SimpleNamespace(sleeps=sleeps)


def _answer(items, finish="STOP", text=None):
    body = json.dumps(items, ensure_ascii=False) if text is None else text
    return SimpleNamespace(text=body, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))])


@pytest.fixture(autouse=True)
def model(monkeypatch):
    """Answers queued per test: a list of items, None (no answer) or a ready response.  ``during`` holds
    what happens elsewhere while the model answers (another process, the bot).  Every test in this file
    has the model faked, so nothing can reach the real one."""
    import gemini_client

    calls: dict = {
        "prompts": [], "labels": [], "answers": [], "during": [], "ready": True,
        "real": (gemini_client.side_task_last_tier, gemini_client.side_task_last_tier_ready),
    }

    def side_task(prompt, config, *, label):
        calls["prompts"].append(prompt)
        calls["labels"].append(label)
        if calls["during"]:
            calls["during"].pop(0)()
        answer = calls["answers"].pop(0) if calls["answers"] else []
        if answer is None or isinstance(answer, SimpleNamespace):
            return answer
        return _answer(answer)

    monkeypatch.setattr(gemini_client, "side_task_last_tier", side_task)
    monkeypatch.setattr(gemini_client, "side_task_last_tier_ready", lambda: calls["ready"])
    return calls


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────


def _raw(message_id, user_id, text, created_at, group=GROUP):
    import memory

    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (group, message_id, user_id, text, int(created_at)),
        )


def _todos(group=GROUP):
    import todo

    with todo._conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM todos WHERE group_id=? ORDER BY created_at, rowid", (group,))]


def _todo(todo_id):
    import todo

    with todo._conn() as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM todos WHERE todo_id=?", (todo_id,)).fetchone())


def _seed_todo(task, owner="user-a", due=TODAY, *, mid=None, source=None, status="pending", group=GROUP,
               said=LAST_NIGHT, todo_id=None):
    """A todo as the extraction writes it: ``created_at`` is when its message was said (milliseconds)."""
    import todo

    tid = todo_id or uuid.uuid4().hex
    with todo._conn() as c:
        c.execute(
            "INSERT INTO todos (todo_id, group_id, sender_user_id, task, due_date, status, source_msg_id, "
            "source_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, group, owner, task, _iso(due), status, mid, source, int(said) * 1000),
        )
    return tid


def _reminder(action, remind_at, *, user="user-a", status="pending", aliases=(), source_ref="",
              source_text=None, source_kind="", time_kind="clock", group=GROUP, created_at=None):
    """``created_at`` (seconds) is a day before ``remind_at`` unless given: the text match only counts a
    reminder made around its message (POST_1 I-2), so the tests that rely on it say when it was made."""
    import memory

    made = int(remind_at) - 86400 if created_at is None else int(created_at)
    with memory._conn() as c:
        cur = c.execute(
            "INSERT INTO reminders (group_id, user_id, action, remind_at, created_at, status, source_kind, "
            "source_ref, source_text, mention_aliases, time_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (group, user, action, int(remind_at), made, status, source_kind, source_ref,
             source_text, json.dumps(list(aliases), ensure_ascii=False), time_kind),
        )
        return cur.lastrowid


def _event(title, day, *, participants=(), mid=None, status="active", event_time=None, group=GROUP):
    import calendar_db

    event_id = uuid.uuid4().hex
    with calendar_db._conn() as c:
        c.execute(
            "INSERT INTO events (event_id, group_id, title, event_date, event_time, participants, source_msg_id, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, group, title, _iso(day), event_time, json.dumps(list(participants), ensure_ascii=False),
             mid, status, int(time.time() * 1000)),
        )
    return event_id


def _queued(message_id, status="pending", group=GROUP):
    import memory

    with memory._conn() as c:
        c.execute(
            "INSERT INTO pending_reminder_extract (group_id, user_id, message_id, text, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group, "user-a", message_id, "合成的一則訊息", int(time.time()), status),
        )


def _seed_cursor(group=GROUP):
    """The cursor row the push's summary is written into (the extraction makes it in real runs)."""
    import family_extract
    import todo

    with todo._conn() as c:
        family_extract.ensure_tables(c)
        c.execute(
            "INSERT OR IGNORE INTO extract_cursors(name, group_id, created_at, message_id, updated_at) "
            "VALUES ('todo', ?, ?, '', ?)",
            (group, NOW - 86400, NOW),
        )


def _last_push(group=GROUP) -> dict:
    import todo

    with todo._conn() as c:
        row = c.execute("SELECT last_push FROM extract_cursors WHERE name='todo' AND group_id=?", (group,)).fetchone()
    return json.loads(row[0]) if row else {}


def _last_run(group=GROUP) -> dict:
    import todo

    with todo._conn() as c:
        row = c.execute("SELECT last_run FROM extract_cursors WHERE name='todo' AND group_id=?", (group,)).fetchone()
    return json.loads(row[0]) if row else {}


def _push(now=NOW, group=GROUP) -> int:
    import todo

    _seed_cursor(group)
    return todo.push_daily_reminders(group, now=now)


def _pushes():
    import line_push_client

    return line_push_client.push_messages.calls


def _pushed_text(call) -> str:
    return call["messages"][0]["text"]


def _run(now=NOW, group=GROUP):
    """The 08:00 job's extraction: its push follows, so a todo due today is made (main_reminder)."""
    import todo

    return todo.run_incremental(group, now=now, push_follows=True)


def _item(i, task, due, **extra):
    return {"i": i, "task": task, "due_date": _iso(due) if isinstance(due, date) else due, **extra}


# ── entry points ────────────────────────────────────────────────────────────────────────────────────


def test_a_message_said_last_night_is_read_once_and_pushed_this_morning(model):
    import todo

    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    assert todo.main_reminder(now=NOW) == 0
    assert todo.main_reminder(now=NOW + 60) == 0

    assert len(model["prompts"]) == 1
    line = json.loads(model["prompts"][0].split("【訊息】\n", 1)[1].splitlines()[0])
    assert line == {"i": 1, "date": _iso(YESTERDAY), "weekday": "週四", "time": "20:00", "text": "我明天要交報告"}
    assert "成員甲" not in model["prompts"][0] and "user-a" not in model["prompts"][0] and GROUP not in model["prompts"][0]
    [call] = _pushes()
    assert call["to"] == GROUP and "- 成員甲 交報告" in _pushed_text(call)
    [row] = _todos()
    assert row["status"] == "pending" and row["due_date"] == _iso(TODAY) and row["last_reminded_at"]
    assert row["source_text"] is None  # pushed: its words are dropped


def test_extract_only_reads_and_never_pushes(model):
    import todo

    _raw("msg-1", "user-a", "我後天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY + timedelta(days=1))])

    assert todo.main_extractor_sweep(now=NOW) == 0

    assert len(model["prompts"]) == 1 and _pushes() == []
    assert [r["task"] for r in _todos()] == ["交報告"] and _todos()[0]["last_reminded_at"] is None


def test_an_extract_no_push_follows_makes_no_todo_due_today(model):
    """POST_1 N-3: only the 08:00 job pushes, right after it extracts.  The Sunday 19:00 extract (or a manual
    one after 08:00) would make a row due today that can only expire, so it makes none; an extract before
    08:00 may, since the day's push still follows it (REV_1 I-2)."""
    import todo

    sunday = date(2026, 11, 15)
    _raw("msg-1", "user-a", "我今天晚上要繳費", _ts(sunday, 10))
    _raw("msg-2", "user-a", "我明天要交報告", _ts(sunday, 11))
    model["answers"].append([_item(1, "繳費", sunday), _item(2, "交報告", sunday + timedelta(days=1))])

    assert todo.main_extractor_sweep(now=_ts(sunday, 19)) == 0

    assert len(model["prompts"]) == 1 and _pushes() == []
    assert [r["task"] for r in _todos()] == ["交報告"]
    run = _last_run()
    assert (run["new"], run["skipped"]) == (1, {"due_today_no_push": 1})


def test_run_incremental_alone_is_an_extract_no_push_follows(model):
    import todo

    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    result = todo.run_incremental(GROUP, now=NOW)

    assert len(model["prompts"]) == 1 and result.skipped == {"due_today_no_push": 1} and _todos() == []


def test_only_no_argument_or_extract_does_anything(monkeypatch, model):
    import todo

    ran: list = []
    monkeypatch.setattr(todo, "main_reminder", lambda **k: ran.append("reminder") or 0)
    monkeypatch.setattr(todo, "main_extractor_sweep", lambda **k: ran.append("extract") or 0)

    assert todo.main([]) == 0 and todo.main(["extract"]) == 0
    for wrong in (["bogus"], ["reminder"], ["extract", "now"], ["extarct"]):
        assert todo.main(wrong) == 2
    assert ran == ["reminder", "extract"]
    assert model["prompts"] == [] and _pushes() == []


def test_a_crash_in_the_job_is_logged_by_type_only(monkeypatch, caplog, model):
    import todo

    def boom(**kwargs):
        raise RuntimeError("合成的祕密字 msg-secret")

    monkeypatch.setattr(todo, "main_reminder", boom)
    caplog.set_level(logging.INFO)

    assert todo.main([]) == 1
    assert "error_type=RuntimeError" in caplog.text and "祕密" not in caplog.text and "msg-secret" not in caplog.text


def test_only_the_family_group_is_read(monkeypatch, model):
    import todo

    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT, group=OTHER)
    assert _run(group=OTHER).stopped == "not_family_group"

    monkeypatch.setenv("ALLOWED_GROUP_IDS", OTHER)  # the family group set but not allowed
    _raw("msg-2", "user-a", "我明天要交報告", LAST_NIGHT)
    assert todo.main_reminder(now=NOW) == 0
    assert todo.main_extractor_sweep(now=NOW) == 1

    assert model["prompts"] == []


# ── how much is sent ────────────────────────────────────────────────────────────────────────────────


def test_no_answer_waits_61_seconds_and_tries_once_more_then_waits_for_the_next_run(model, todo_env):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].extend([None, None, [_item(1, "交報告", TODAY)]])

    first = _run()
    second = _run(NOW + 600)

    assert first.stopped == "no_answer" and first.requests == 2 and todo_env.sleeps == [61.0]
    assert second.new == 1 and len(model["prompts"]) == 3


def test_a_bad_answer_is_retried_once_then_passed_over(model):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    bad = _answer([], finish="MAX_TOKENS", text='[{"i": 1, "task": "交')
    model["answers"].extend([bad, bad, [_item(1, "交報告", TODAY)]])

    first = _run()
    second = _run(NOW + 60)
    third = _run(NOW + 120)

    assert first.stopped == "bad_answer" and second.skipped.get("given_up") == 1
    assert third.requests == 0 and _todos() == [] and len(model["prompts"]) == 2


def test_a_blocked_batch_is_passed_over_at_once(model):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append(_answer([], finish="SAFETY"))

    first = _run()
    second = _run(NOW + 60)

    assert first.skipped.get("blocked") == 1 and second.requests == 0 and len(model["prompts"]) == 1


def test_at_most_two_requests_a_run_and_the_rest_waits(monkeypatch, model):
    import family_extract

    monkeypatch.setattr(family_extract, "BATCH_MAX_MESSAGES", 1)
    for n in range(3):
        _raw(f"msg-{n}", "user-a", f"我明天要做第{n}件事", LAST_NIGHT + n)

    first = _run()
    second = _run(NOW + 60)

    assert first.stopped == "request_cap" and first.requests == 2
    assert second.requests == 1 and len(model["prompts"]) == 3
    assert "第2件" not in "".join(model["prompts"][:2]) and "第2件" in model["prompts"][2]


def test_the_todo_request_goes_to_the_last_tier_only(monkeypatch, model):
    """Through the real side task: never the 2.5 models (gemini-2.5-flash is the replies' free quota)."""
    import config
    import gemini_client

    seen: list = []

    class Models:
        def generate_content(self, *, model, contents, config):
            seen.append((model, config))
            return _answer([])

    monkeypatch.setattr(gemini_client, "side_task_last_tier", model["real"][0])
    monkeypatch.setattr(gemini_client, "side_task_last_tier_ready", model["real"][1])
    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=Models()))
    monkeypatch.setattr(config.settings, "gemini_last_tier_model", "gemini-test-last-tier", raising=False)
    monkeypatch.setattr(gemini_client.settings, "gemini_last_tier_model", "gemini-test-last-tier", raising=False)
    monkeypatch.delenv("GEMINI_LAST_TIER_SIDE_TASK_RESERVE", raising=False)
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)

    _run()

    assert [m for m, _ in seen] == [gemini_client.last_tier_model()] == ["gemini-test-last-tier"]
    assert seen[0][0] not in {"gemini-2.5-flash", gemini_client.settings.gemini_model, gemini_client.settings.gemini_light_model}
    schema = seen[0][1].response_schema
    kind = schema["type"] if isinstance(schema, dict) else getattr(schema.type, "value", schema.type)
    assert seen[0][1].http_options.timeout == 60_000 and str(kind).upper() == "ARRAY"
    assert model["prompts"] == []  # the fake was not used


# ── which messages are sent ─────────────────────────────────────────────────────────────────────────


def test_only_messages_with_a_date_word_are_sent(model):
    _raw("msg-1", "user-a", "我要交報告", LAST_NIGHT)
    _raw("msg-2", "user-a", "我明天要交報告", LAST_NIGHT + 1)

    result = _run()

    assert len(model["prompts"]) == 1
    sent = model["prompts"][0].split("【訊息】\n", 1)[1]
    assert "我明天要交報告" in sent and "我要交報告" not in sent.replace("我明天要交報告", "")
    assert result.skipped.get("prefilter") == 1


def test_reminder_requests_and_words_to_the_bot_are_left_to_the_reminder_feature(model):
    for n, text in enumerate(("提醒我明天交報告", "咪寶，我明天要交報告", "@AI 我明天要交報告", "＠ai 明天要交報告")):
        _raw(f"msg-{n}", "user-a", text, LAST_NIGHT + n)

    result = _run()

    assert model["prompts"] == [] and result.skipped.get("excluded") == 4


def test_placeholders_commands_links_bot_rows_and_unsent_messages_never_go_out(model):
    import todo

    _raw("msg-1", "__bot__", "咪寶：我明天要提醒大家", LAST_NIGHT)
    _raw("msg-2", "user-a", "/待辦 明天", LAST_NIGHT + 1)
    _raw("msg-3", "user-a", "[圖片]", LAST_NIGHT + 2)
    _raw("msg-4", "user-a", "https://example.com/明天", LAST_NIGHT + 3)
    _raw("msg-5", "user-a", "我明天要交報告", LAST_NIGHT + 4)
    todo.forget_message(GROUP, "msg-5", now=LAST_NIGHT + 60)  # 收回 before the 08:00 run

    result = _run()

    assert model["prompts"] == []
    assert result.skipped == {"not_family_text": 4, "unsent": 1}


def test_the_five_known_false_rejections_are_not_sent_but_a_plain_plan_is(model):
    """Known limit (PRE_3 P2-I2): both deterministic gates block these real commitments.  TODO has no
    confirmation step, so a missed one is preferred to a wrong @."""
    blocked = (
        "我下週要出差，你們要什麼伴手禮？",
        "我下禮拜要回診，誰可以載我？",
        "我月底要搬家，有人要來幫忙嗎",
        "我週末要回老家，要帶什麼嗎？",
        "我明天要去看牙醫呢",
    )
    for n, text in enumerate(blocked):
        _raw(f"msg-{n}", "user-a", text, LAST_NIGHT + n)
    _raw("msg-9", "user-a", "我明天要交報告", LAST_NIGHT + 9)

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("gate_rejected") == 5
    sent = model["prompts"][0].split("【訊息】\n", 1)[1]
    assert "我明天要交報告" in sent and not any(text in sent for text in blocked)


def test_tomorrow_said_after_midnight_is_not_sent(model):
    _raw("msg-1", "user-a", "我明天要交報告", _ts(TODAY, 0, 30))

    result = _run()

    assert model["prompts"] == [] and result.skipped.get("late_night_ambiguous") == 1


@pytest.mark.parametrize(
    "text",
    [
        "我今天要交報告", "我今晚要交報告", "我明天要交報告", "我明早要交報告", "我明晚要交報告", "我明日要交報告",
        "我後天要交報告", "我大後天要交報告", "我下週三要交報告", "我這週日要回老家", "我週三要交報告",
        "我禮拜三要交報告", "我星期三要交報告", "我下個月要搬家", "我月底要繳房租",
        "我週末要回老家", "我晚點要去買菜", "我5號要繳費", "我明天11/20要交報告",
    ],
)
def test_a_date_counted_from_today_said_after_midnight_is_not_sent(model, text):
    """F2 widened (POST_2 I-2): at 00:30 on a Monday, 「下週三」 still means the speaker's Sunday-night next
    week.  Between 00:00 and 04:59 only absolute dates are sent."""
    _raw("msg-1", "user-a", text, _ts(TODAY, 0, 30))

    result = _run()

    assert model["prompts"] == [] and result.skipped == {"late_night_ambiguous": 1}


@pytest.mark.parametrize(
    "text", ["我11/20要交報告", "我11月20日要交報告", "我十一月二十日要交報告", "我2026-11-20要交報告", "我11/20 14:30要交報告"]
)
def test_an_absolute_date_said_after_midnight_is_sent(model, text):
    _raw("msg-1", "user-a", text, _ts(TODAY, 0, 30))

    result = _run()

    assert len(model["prompts"]) == 1 and text in model["prompts"][0] and result.skipped == {}


@pytest.mark.parametrize("hour,minute,sent", [(4, 59, False), (5, 0, True)])
def test_the_late_night_rule_ends_at_five(model, hour, minute, sent):
    _raw("msg-1", "user-a", "我明天要交報告", _ts(TODAY, hour, minute))

    result = _run()

    assert len(model["prompts"]) == (1 if sent else 0)
    assert result.skipped.get("late_night_ambiguous", 0) == (0 if sent else 1)


def test_someone_elses_plan_with_no_i_in_it_is_not_sent(model):
    """POST_2 I-3: the owner is always the speaker, so a message about another person with no 我 in it is
    never sent (before this, the prompt's rule 1 was the only guard and a wrong item @ed the speaker)."""
    for n, text in enumerate(("爸爸下週三要去健檢", "成員乙明天要交報告", "你明天記得要繳房租喔", "外公明天要去銀行")):
        _raw(f"msg-{n}", "user-a", text, LAST_NIGHT + n)

    result = _run()

    assert model["prompts"] == [] and result.skipped == {"other_subject": 4}


def test_a_message_with_i_in_it_or_no_other_person_is_sent(model):
    texts = (
        "媽媽明天回診，我載她去", "明天要交報告", "明天要去公司開會", "明天要買牛奶", "明天要買其他東西",
        "成員甲明天要繳房租",  # the speaker's own name
    )
    for n, text in enumerate(texts):
        _raw(f"msg-{n}", "user-a", text, LAST_NIGHT + n)
    model["answers"].append([_item(1, "載媽媽回診", TODAY), _item(2, "交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and "other_subject" not in result.skipped and result.new == 2
    sent = model["prompts"][0].split("【訊息】\n", 1)[1]
    assert all(text in sent for text in texts)


def test_known_limits_of_the_subject_check(model):
    """Known limits (FIX_LIST 4: only a message with no 我 is checked, against a fixed list of words).
    Someone else's plan with 我 in it, or with a person word not on the list, is still sent and only the
    prompt's rule 1 guards it; the speaker's own plan with a family word and no 我 is not sent."""
    missed = ("我媽明天要回診", "阿姨明天要來家裡", "大家明天要早起")
    for n, text in enumerate(missed):
        _raw(f"msg-{n}", "user-a", text, LAST_NIGHT + n)
    _raw("msg-9", "user-a", "明天要載媽媽去回診", LAST_NIGHT + 9)

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped == {"other_subject": 1}
    sent = model["prompts"][0].split("【訊息】\n", 1)[1]
    assert all(text in sent for text in missed) and "載媽媽" not in sent


def test_a_standing_routine_is_not_a_todo(model):
    """POST_2 N-1: 「每週三」 is not one day; a message about a routine is not sent at all."""
    texts = ("我每週三要倒垃圾", "我每個禮拜三要去運動", "我從明天開始每天都要早起", "我每個月5號要繳房租", "我每年過年都要回老家")
    for n, text in enumerate(texts):
        _raw(f"msg-{n}", "user-a", text, LAST_NIGHT + n)

    result = _run()

    assert model["prompts"] == [] and result.skipped == {"recurring": 5}


# ── what each answer item must pass ─────────────────────────────────────────────────────────────────


def test_a_task_said_last_night_for_tomorrow_is_due_today(model):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.new == 1
    [row] = _todos()
    assert (row["task"], row["due_date"], row["sender_user_id"], row["source_msg_id"]) == (
        "交報告", _iso(TODAY), "user-a", "msg-1"
    )
    assert row["source_text"] == "我明天要交報告" and row["status"] == "pending"


def test_bad_item_numbers_are_dropped(model):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(i, "交報告", TODAY) for i in (0, 2, True, "1", 1.0)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("bad_item") == 5 and _todos() == []


def test_the_owner_must_be_a_named_family_member(model):
    _raw("msg-1", "user-z", "我明天要交報告", LAST_NIGHT)  # no local family name
    model["answers"].append([_item(1, "交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("owner_no_alias") == 1 and _todos() == []


def test_tasks_that_could_ping_or_break_the_push_are_dropped(model):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    tasks = ("交報告@成員乙", "交報告＠全家", "交{pa}報告", "交報告\n明天", "交報告 https://example.com/x", "", "交" * 101, 7)
    model["answers"].append([_item(1, task, TODAY) for task in tasks])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("bad_task") == len(tasks) and _todos() == []


def test_a_question_task_is_dropped(model):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "要不要交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("task_rejected") == 1 and _todos() == []


def test_a_task_must_come_from_its_own_message(model):
    """PRE_4 I1: at least half of the task's bigrams in message i.  The four synthetic counterexamples
    (pre4/provenance_probe.py); three of them would pass the old one-shared-bigram rule."""
    import todo

    cases = (
        ("我明天要去銀行", "去銀行轉帳給乙"),
        ("我明天要交報告", "交報告並道歉"),
        ("我下週要去台北開會", "去台北接成員乙"),
        ("我明天要繳費", "繳交罰款五萬"),
    )
    for n, (source, _task) in enumerate(cases, 1):
        _raw(f"msg-{n}", "user-a", source, LAST_NIGHT + n)
    _raw("msg-9", "user-a", "我明天要交期末報告", LAST_NIGHT + 9)
    model["answers"].append(
        [_item(n, task, TODAY + timedelta(days=4 if n == 3 else 0)) for n, (_s, task) in enumerate(cases, 1)]
        + [_item(5, "交報告", TODAY)]  # exactly 0.50: kept (the bound is inclusive)
    )

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("provenance") == 4 and result.new == 1
    assert [(r["task"], r["source_msg_id"]) for r in _todos()] == [("交報告", "msg-9")]
    old_rule = [bool(todo._bigrams(task) & todo._bigrams(source)) for source, task in cases]
    assert old_rule == [True, True, True, False]
    assert all(todo.provenance_coverage(task, source) < 0.5 for source, task in cases)
    assert todo.provenance_coverage("交報告", "我明天要交期末報告") == 0.5
    assert todo.provenance_coverage("跑", "我明天要跑") == 0.0  # no bigram left: never a match


def test_dates_the_message_itself_rules_out_are_dropped(model):
    _raw("msg-1", "user-a", "我下週三要交報告", LAST_NIGHT)  # said Thursday: next week's Wednesday
    _raw("msg-2", "user-a", "我明天要繳房租", LAST_NIGHT + 1)
    model["answers"].append([
        _item(1, "交報告", date(2026, 11, 19)),  # a Thursday: the wrong weekday
        _item(2, "繳房租", TODAY + timedelta(days=1)),  # 「明天」 counted twice
    ])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("date_mismatch") == 2 and _todos() == []


def test_malformed_far_and_past_due_dates_are_dropped(model):
    _raw("msg-1", "user-a", "我月底要繳房租", LAST_NIGHT)  # no day the message itself fixes: bounds only
    model["answers"].append([
        _item(1, "繳房租", "20261130"),
        _item(1, "繳房租", "2026-W47-5"),
        _item(1, "繳房租", "2027-02-29"),  # not a day in a common year
        _item(1, "繳房租", "２０２６-１１-３０"),
        _item(1, "繳房租", None),
        _item(1, "繳房租", YESTERDAY + timedelta(days=181)),  # past the message day + 180
        _item(1, "繳房租", YESTERDAY),  # already over
        _item(1, "繳房租", date(2026, 11, 30)),
    ])

    result = _run()

    assert len(model["prompts"]) == 1
    assert result.skipped.get("bad_date") == 5 and result.skipped.get("date_bounds") == 2 and result.new == 1
    assert [r["due_date"] for r in _todos()] == ["2026-11-30"]


def test_expected_dates_reads_the_message_own_words():
    import todo

    said = TODAY  # a Friday
    cases = {
        "我明天要交": {date(2026, 11, 14)},
        "我後天要交": {date(2026, 11, 15)},
        "我大後天要交": {date(2026, 11, 16)},
        "今晚要交": {TODAY},
        "我下週三要交": {date(2026, 11, 18)},
        "我下個禮拜三要交": {date(2026, 11, 18)},
        "我下下週三要交": {date(2026, 11, 25)},
        "我週三要交": {date(2026, 11, 18)},  # this week's is over
        "我週六要交": {date(2026, 11, 14)},
        "我禮拜天要交": {date(2026, 11, 15)},  # 天 and 日 are Sunday
        "我這週日要交": {date(2026, 11, 15)},
        "我11/20要交": {date(2026, 11, 20)},
        "我1/5要交": {date(2027, 1, 5)},
        "我12月1號要交": {date(2026, 12, 1)},
        "我2026年12月1日要交": {date(2026, 12, 1)},
        "我5號要繳費": {date(2026, 12, 5)},  # this month's 5th is over (POST_2 I-1: one month, not two)
        "我2/30要交": set(),
        "我2/29要交": set(),
        "我月底要繳": set(),
        "才1/3的價格": set(),
    }
    for text, want in cases.items():
        assert todo.expected_dates(text, said) == want, text
    assert todo.expected_dates("我2/29要交", date(2027, 11, 13)) == {date(2028, 2, 29)}


@pytest.mark.parametrize(
    "text,said,want",
    [
        # POST_2 I-1: a bare day number is the first such day not before the day it was said
        ("我5號要繳費", date(2026, 11, 13), {date(2026, 12, 5)}),
        ("我13號要繳費", date(2026, 11, 13), {date(2026, 11, 13)}),
        ("我25號要繳學費", date(2026, 11, 20), {date(2026, 11, 25)}),
        ("我20日要繳費", date(2026, 11, 13), {date(2026, 11, 20)}),
        ("我31號要繳", date(2026, 11, 20), {date(2026, 12, 31)}),  # November has no 31st
        ("我30號要交", date(2027, 1, 31), {date(2027, 3, 30)}),  # nor February a 30th
        # …and a month word says which month
        ("我下個月5號要繳房租", date(2026, 11, 3), {date(2026, 12, 5)}),
        ("我下月25號要繳", date(2026, 11, 3), {date(2026, 12, 25)}),
        ("我下個月的5號要繳", date(2026, 12, 3), {date(2027, 1, 5)}),
        ("我下下個月5號要繳", date(2026, 11, 3), {date(2027, 1, 5)}),
        ("我這個月20號要繳", date(2026, 11, 13), {date(2026, 11, 20)}),
        ("我本月5號要繳", date(2026, 11, 13), {date(2026, 11, 5)}),  # over: the bounds drop any answer
        ("我下個月30號要繳", date(2027, 1, 13), set()),  # no such day
        # POST_2 N-1／POST_1 N-4: Chinese numerals (一 to 三十一, 廿)
        ("我十五號要交報告", date(2026, 11, 13), {date(2026, 11, 15)}),
        ("我三號要繳", date(2026, 11, 13), {date(2026, 12, 3)}),
        ("我廿五號要繳", date(2026, 11, 13), {date(2026, 11, 25)}),
        ("我二十號要繳", date(2026, 11, 13), {date(2026, 11, 20)}),
        ("我三十一號要繳", date(2026, 11, 13), {date(2026, 12, 31)}),
        ("我十一月二十號要出差", date(2026, 11, 13), {date(2026, 11, 20)}),
        ("我三月五日要出發", date(2026, 11, 13), {date(2027, 3, 5)}),
        ("我下個月五號要繳", date(2026, 11, 13), {date(2026, 12, 5)}),
        ("今天主管說我十一月二十號要出差", date(2026, 11, 13), {date(2026, 11, 13), date(2026, 11, 20)}),
        # …and words that look like a weekday or a day number but are not one
        ("這週天氣好冷，我下週二要去看牙醫", date(2026, 11, 13), {date(2026, 11, 17)}),
        ("下週日本出差要準備", date(2026, 11, 12), set()),
        ("上週三說好的，我週五要交報告", date(2026, 11, 16), {date(2026, 11, 20)}),
        ("我每週三要倒垃圾", date(2026, 11, 13), set()),
        ("我3日後要交", date(2026, 11, 13), set()),
        ("我明天要去1日遊", date(2026, 11, 13), {date(2026, 11, 14)}),
    ],
)
def test_day_numbers_and_week_words_read_as_a_calendar_would(text, said, want):
    import todo

    assert todo.expected_dates(text, said) == want


def test_a_day_number_admits_one_month_only(model):
    """POST_2 I-1: 「下個月5號」 is next month's 5th and a bare 「25號」 this month's; a model answer a month
    off is dropped, not pushed a month early or late."""
    said = date(2026, 11, 3)
    _raw("msg-1", "user-a", "我下個月5號要繳房租", _ts(said, 21))
    _raw("msg-2", "user-a", "我25號要繳學費", _ts(said, 21) + 1)
    model["answers"].append([
        _item(1, "繳房租", date(2026, 11, 5)),  # a month early
        _item(2, "繳學費", date(2026, 12, 25)),  # a month late
        _item(1, "繳房租", date(2026, 12, 5)),
        _item(2, "繳學費", date(2026, 11, 25)),
    ])

    result = _run(_ts(said + timedelta(days=1), 8))

    assert len(model["prompts"]) == 1 and result.skipped.get("date_mismatch") == 2 and result.new == 2
    assert sorted(r["due_date"] for r in _todos()) == ["2026-11-25", "2026-12-05"]


def test_date_words_are_taken_out_of_the_task(model):
    """POST_2 N-9: pushed on its day, a task must not still say 「明天」.  A task that was only a date is
    dropped; 一日遊 and 過年 are not dates here."""
    import todo

    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    _raw("msg-2", "user-a", "我下週三要去銀行", LAST_NIGHT + 1)
    _raw("msg-3", "user-a", "我11/20要繳房租", LAST_NIGHT + 2)
    _raw("msg-4", "user-a", "我明天要去一日遊", LAST_NIGHT + 3)
    model["answers"].append([
        _item(1, "明天交報告", TODAY),
        _item(2, "下週三去銀行", date(2026, 11, 18)),
        _item(3, "11/20 繳房租", date(2026, 11, 20)),
        _item(4, "去一日遊", TODAY),
        _item(1, "明天", TODAY),
    ])

    result = _run()

    assert len(model["prompts"]) == 1 and result.new == 4 and result.skipped == {"bad_task": 1}
    assert sorted(r["task"] for r in _todos()) == sorted(["交報告", "去銀行", "繳房租", "去一日遊"])
    assert todo._clean_task("準備過年的年菜") == "準備過年的年菜"
    assert todo._clean_task("下週三的家長會") == "家長會"
    assert todo._clean_task("預約下週三看牙醫") == "預約看牙醫"  # no gap left between the words
    assert todo._clean_task("準備明天的簡報") == "準備簡報"  # a date goes with its 的
    assert todo._clean_task("交上週的報告") == "交報告"
    assert todo._clean_task("加1/3杯水") == "加1/3杯水"  # a fraction is no date


def test_a_message_that_already_made_a_reminder_or_event_makes_no_todo(model):
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    _raw("msg-2", "user-a", "我明天要繳房租", LAST_NIGHT + 1)
    _raw("msg-3", "user-a", "我明天要去銀行", LAST_NIGHT + 2)
    _reminder("交期末報告", _ts(TODAY, 9), source_ref="msg-1:0", source_kind="schedule_line", status="done")
    _event("繳房租", TODAY, mid="msg-2", status="cancelled")
    _reminder("去銀行辦事", _ts(TODAY, 10), user="user-b", source_text="我明天要去銀行", created_at=LAST_NIGHT + 62)
    model["answers"].append([_item(1, "交報告", TODAY), _item(2, "繳房租", TODAY), _item(3, "去銀行", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("dup_structural") == 3 and _todos() == []


def test_the_same_words_said_again_the_next_night_make_their_own_todo(model):
    """POST_1 I-2: two nights ago 「我明天要加班」 made a reminder (for yesterday); last night the same words
    made none.  Its todo is made (the old reminder was made before this message) and pushed today."""
    _reminder("加班", _ts(YESTERDAY, 9), user="user-a", status="done", source_text="我明天要加班",
              created_at=LAST_NIGHT - 86400 + 60)
    _raw("msg-1", "user-a", "我明天要加班", LAST_NIGHT)
    model["answers"].append([_item(1, "加班", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.new == 1 and result.skipped == {}
    assert _push() == 1 and _last_push()["fold_structural"] == 0


def test_two_todos_from_one_message_are_both_kept_but_one_task_is_never_stored_twice(model):
    _raw("msg-1", "user-a", "我明天要交報告和繳房租", LAST_NIGHT)
    _raw("msg-2", "user-a", "我明天要交報告", LAST_NIGHT + 1)
    model["answers"].append([_item(1, "交報告", TODAY), _item(1, "繳房租", TODAY), _item(2, "交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.new == 2 and result.skipped.get("duplicate") == 1
    assert sorted(r["task"] for r in _todos()) == ["交報告", "繳房租"]


# ── the switch ──────────────────────────────────────────────────────────────────────────────────────


def test_off_neither_reads_nor_pushes(monkeypatch, model):
    import todo

    monkeypatch.setenv("LINE_BOT_TODO_MODE", "off")
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    today = _seed_todo("繳房租")
    overdue = _seed_todo("倒垃圾", due=YESTERDAY)

    assert todo.main_reminder(now=NOW) == 0

    assert model["prompts"] == [] and _pushes() == []
    assert _todo(today)["last_reminded_at"] is None and _todo(overdue)["status"] == "pending"  # not even expired


def test_shadow_stores_rows_nobody_sees_and_only_counts_them(monkeypatch, model):
    import todo

    monkeypatch.setenv("LINE_BOT_TODO_MODE", "shadow")
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    _run()
    [row] = _todos()
    assert row["status"] == "shadow" and todo.list_pending(GROUP, now=NOW) == []

    assert todo.push_daily_reminders(GROUP, now=NOW) == 0
    first = _last_push()
    assert todo.push_daily_reminders(GROUP, now=NOW + 60) == 0
    second = _last_push()

    assert len(model["prompts"]) == 1 and _pushes() == []
    assert (first["mode"], first["would_push"], first["pushed"]) == ("shadow", 1, 0)
    assert second["would_push"] == 0  # marked: a second run the same day does not count it again
    assert _todo(row["todo_id"])["source_text"] is None and _todo(row["todo_id"])["status"] == "shadow"


def test_shadow_counts_folds_instead_of_marking_them_by_kind(monkeypatch):
    monkeypatch.setenv("LINE_BOT_TODO_MODE", "shadow")
    _seed_todo("交報告", status="shadow")
    _reminder("交報告", _ts(TODAY, 9))

    _push()

    push = _last_push()
    assert (push["would_fold"], push["fold_semantic"], push["would_push"]) == (1, 0, 0) and _pushes() == []


def test_shadow_rows_never_expire(monkeypatch):
    monkeypatch.setenv("LINE_BOT_TODO_MODE", "shadow")
    old = _seed_todo("交報告", due=YESTERDAY, status="shadow")

    _push()

    assert _todo(old)["status"] == "shadow" and _last_push()["expired"] == 0


@pytest.mark.parametrize("mode", ["on", "off", "shadow"])
def test_a_shadow_row_loses_its_words_once_its_day_is_over_whatever_the_mode(monkeypatch, mode):
    """PRE_4 I9 (POST_1 N-5, POST_2 N-5): a shadow row is never pushed, so the first push step after its
    day drops its words, even after the switch went back to on (or off).  It stays shadow."""
    monkeypatch.setenv("LINE_BOT_TODO_MODE", mode)
    past = _seed_todo("去銀行", due=YESTERDAY, mid="msg-1", source="我下週三要去銀行", status="shadow")
    later = _seed_todo("交報告", due=TODAY + timedelta(days=1), mid="msg-2", source="我後天要交報告", status="shadow")

    _push()

    assert (_todo(past)["status"], _todo(past)["source_text"]) == ("shadow", None)
    assert (_todo(later)["status"], _todo(later)["source_text"]) == ("shadow", "我後天要交報告")


def test_switching_back_on_after_days_off_raises_no_push_alarm():
    """Rows that fell due while the job was off just expire; push_ok only says whether today's went out."""
    first = _seed_todo("交報告", due=YESTERDAY)
    second = _seed_todo("繳房租", due=YESTERDAY - timedelta(days=1))

    assert _push() == 0

    push = _last_push()
    assert (push["mode"], push["expired"], push["due"], push["push_ok"]) == ("on", 2, 0, True)
    assert _todo(first)["status"] == _todo(second)["status"] == "expired" and _pushes() == []


def test_a_typo_in_the_switch_is_off_with_a_warning(monkeypatch, caplog, model):
    import todo

    monkeypatch.setenv("LINE_BOT_TODO_MODE", "shdow")
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    caplog.set_level(logging.WARNING)

    assert todo.current_mode() == "off"
    assert _run().stopped == "off"
    assert "LINE_BOT_TODO_MODE" in caplog.text and model["prompts"] == []


def test_an_unset_switch_is_the_default_mode_written_in_the_program(monkeypatch):
    import todo

    monkeypatch.delenv("LINE_BOT_TODO_MODE", raising=False)
    monkeypatch.setattr(todo, "DEFAULT_MODE", "on")
    assert todo.current_mode() == "on"
    monkeypatch.setattr(todo, "DEFAULT_MODE", "shadow")
    assert todo.current_mode() == "shadow"
    monkeypatch.setenv("LINE_BOT_TODO_MODE", " ")
    assert todo.current_mode() == "shadow"
    monkeypatch.setenv("LINE_BOT_TODO_MODE", "on")
    assert todo.current_mode() == "on"


# ── the push: once, and never what another reminder already says ────────────────────────────────────


def test_a_second_run_the_same_day_pushes_nothing():
    _seed_todo("交報告", source="我明天要交報告")

    assert _push() == 1
    assert _push(NOW + 60) == 0

    assert len(_pushes()) == 1 and _last_push()["due"] == 0


def test_the_retry_key_carries_the_sorted_ids():
    ids = [_seed_todo("交報告"), _seed_todo("繳房租", owner="user-b")]

    assert _push() == 2

    [call] = _pushes()
    key = f"todo_daily:{GROUP}:{_iso(TODAY)}:{','.join(sorted(ids))}"
    assert call["retry_key"] == str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    text = _pushed_text(call)
    assert "- 成員甲 交報告" in text and "- 成員乙 繳房租" in text
    users = sorted(sub["mentionee"]["userId"] for sub in call["messages"][0]["substitution"].values())
    assert users == ["user-a", "user-b"]


@pytest.mark.parametrize("order", [("todo-b", "todo-a"), ("todo-a", "todo-b")], ids=["b_first", "a_first"])
def test_the_retry_key_is_the_same_whichever_row_comes_first(order):
    """POST_3 N5: two fixed ids in both orders (the push reads them by message time), so an unsorted key
    fails every time, not by chance."""
    owners = {"todo-a": ("user-a", "交報告"), "todo-b": ("user-b", "繳房租")}
    for n, tid in enumerate(order):
        _seed_todo(owners[tid][1], owner=owners[tid][0], todo_id=tid, said=LAST_NIGHT + n)

    assert _push() == 2

    [call] = _pushes()
    key = f"todo_daily:{GROUP}:{_iso(TODAY)}:todo-a,todo-b"
    assert call["retry_key"] == str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def test_a_push_that_went_out_is_ok_even_when_marking_it_fails(monkeypatch):
    """POST_1 N-2: LINE took the message; only the marking failed.  push_ok stays true (no false
    todo_push_failed alarm) and mark_error counts the rows left unmarked."""
    import todo

    _seed_todo("交報告", source="我明天要交報告")

    def locked(ids, now, **kwargs):
        if ids:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(todo, "_mark_done", locked)

    assert _push() == 1

    push = _last_push()
    assert len(_pushes()) == 1 and (push["push_ok"], push["mark_error"], push["pushed"]) == (True, 1, 1)


def test_a_fold_that_cannot_be_marked_holds_back_nothing_else(monkeypatch):
    import todo

    folded = _seed_todo("交報告", mid="msg-1", source="我明天要交報告")
    _reminder("交報告", _ts(TODAY, 9), user="user-a")  # the same event, and it pings the owner
    pushed = _seed_todo("繳房租", owner="user-b", mid="msg-2", source="我明天要繳房租")
    real = todo._mark_done

    def flaky(ids, now, **kwargs):
        if folded in ids:
            raise sqlite3.OperationalError("database is locked")
        return real(ids, now, **kwargs)

    monkeypatch.setattr(todo, "_mark_done", flaky)

    assert _push() == 1

    push = _last_push()
    assert (push["fold_semantic"], push["mark_error"], push["pushed"], push["push_ok"]) == (1, 1, 1, True)
    assert _todo(pushed)["last_reminded_at"] and _todo(folded)["last_reminded_at"] is None


def test_the_push_waits_30_seconds_for_the_database_and_an_unsend_5(monkeypatch):
    """PRE_4 N4, POST_1 N-2: the 08:00 push waits as long as the job's other connections.  forget_message
    runs in the bot's one webhook thread, so it waits 5 seconds at most (POST_1 N-9, POST_4 N1)."""
    import todo

    _seed_todo("交報告", mid="msg-1", source="我明天要交報告")
    _seed_cursor()
    seen: list = []
    real = todo._conn

    def spy(timeout=5.0):
        seen.append(timeout)
        return real(timeout=timeout)

    monkeypatch.setattr(todo, "_conn", spy)

    assert todo.push_daily_reminders(GROUP, now=NOW) == 1
    assert seen and set(seen) == {30.0}

    seen.clear()
    todo.forget_message(GROUP, "msg-2", now=NOW)
    assert seen == [5.0]


def test_a_failed_push_is_not_marked_and_expires_tomorrow(monkeypatch):
    import line_push_client

    tid = _seed_todo("交報告", source="我明天要交報告")

    def refuse(to, messages, **kwargs):
        raise line_push_client.LinePushError("LINE push HTTP 500", status_code=500)

    monkeypatch.setattr(line_push_client, "push_messages", refuse)
    assert _push() == 0
    assert _last_push()["push_ok"] is False and _todo(tid)["last_reminded_at"] is None

    assert _push(NOW + 86400) == 0
    assert _todo(tid)["status"] == "expired" and _todo(tid)["source_text"] is None
    tomorrow = _last_push()
    assert (tomorrow["expired"], tomorrow["push_ok"]) == (1, True)


def test_overdue_todos_are_not_pushed():
    old = _seed_todo("交報告", due=YESTERDAY, source="我昨天要交報告")

    assert _push() == 0

    assert _pushes() == [] and _todo(old)["status"] == "expired" and _todo(old)["source_text"] is None


def test_the_same_task_twice_for_one_owner_is_one_line():
    first = _seed_todo("交報告", mid="msg-1")
    second = _seed_todo("交 報告", mid="msg-2")

    assert _push() == 2

    [call] = _pushes()
    assert [line for line in _pushed_text(call).splitlines() if line.startswith("- ")] == ["- 成員甲 交報告"]
    assert _todo(first)["last_reminded_at"] and _todo(second)["last_reminded_at"]
    assert (_last_push()["pushed"], _last_push()["lines"]) == (2, 1)


def test_the_reminder_format_keeps_its_two_arguments():
    import todo

    rows = [{"task": "交報告", "sender_user_id": "user-a"}]
    assert todo._format_reminder(rows, []) == "📋 今日 TODO 提醒\n\n🔔 今天到期：\n- 成員甲 交報告"
    overdue = [{"task": "繳房租", "sender_user_id": "user-b", "due_date": _iso(YESTERDAY)}]
    assert "- 成員乙 繳房租 (due " in todo._format_reminder([], overdue)


@pytest.mark.parametrize(
    "made",
    [
        lambda: _reminder("繳交期末報告", _ts(TODAY + timedelta(days=3), 9), user="user-b",
                          source_text="我明天要交期末報告", created_at=LAST_NIGHT + 62),
        lambda: _reminder("繳交期末報告", _ts(TODAY + timedelta(days=3), 9), user="user-b",
                          source_text="別人的一則\n---\n我明天要交期末報告", created_at=LAST_NIGHT + 62),
        lambda: _reminder("期末報告", _ts(TODAY + timedelta(days=3), 9), user="user-b",
                          source_kind="schedule_line", source_ref="msg-1:2", status="cancelled"),
        lambda: _event("期末報告", TODAY + timedelta(days=3), mid="msg-1", status="cancelled"),
    ],
    ids=["same_source_text", "in_merged_sources", "schedule_source_ref", "calendar_source_msg"],
)
def test_a_reminder_or_event_made_from_the_same_message_folds_the_todo(made):
    tid = _seed_todo("交報告", mid="msg-1", source="我明天要交期末報告")
    made()

    assert _push() == 0

    push = _last_push()
    assert (push["fold_structural"], push["fold_queue"], push["fold_semantic"]) == (1, 0, 0) and _pushes() == []
    assert _todo(tid)["last_reminded_at"] and _todo(tid)["source_text"] is None


@pytest.mark.parametrize(
    "made_at,folded",
    [
        (LAST_NIGHT - 300, True),
        (LAST_NIGHT + 7 * 86400, True),
        (LAST_NIGHT - 301, False),
        (LAST_NIGHT + 7 * 86400 + 1, False),
        (LAST_NIGHT - 35 * 86400, False),
    ],
    ids=["300s_before", "7_days_after", "301s_before", "past_7_days", "five_weeks_before"],
)
def test_the_same_words_fold_a_todo_only_from_a_reminder_made_around_its_message(made_at, folded):
    """POST_1 I-2: equal words are the same message only for a reminder made from 300 seconds before it was
    said to 7 days after (the reminder queue).  The id keys (source_ref, the event's message) hold always."""
    _seed_todo("加班", mid="msg-1", source="我明天要加班")
    _reminder("加班", _ts(TODAY + timedelta(days=10), 9), user="user-b", source_text="我明天要加班",
              created_at=made_at)

    assert _push() == (0 if folded else 1)

    assert _last_push()["fold_structural"] == (1 if folded else 0)


def test_the_same_words_from_a_reminder_weeks_ago_leave_the_todo_to_be_pushed(model):
    """POST_1 I-2 (P4): five weeks ago the same sentence made a passive reminder; last night's made none.
    That old reminder is another message: the todo is made and pushed."""
    import todo

    old = _ts(TODAY - timedelta(days=34), 9)
    _reminder("加班", old, user="user-a", status="done", source_text="我明天要加班", created_at=old - 86400)
    _raw("msg-1", "user-a", "我明天要加班", LAST_NIGHT)
    model["answers"].append([_item(1, "加班", TODAY)])

    assert todo.main_reminder(now=NOW) == 0

    assert len(model["prompts"]) == 1 and len(_pushes()) == 1
    assert (_last_push()["fold_structural"], _last_push()["pushed"]) == (0, 1)


def test_the_push_matches_the_words_against_when_the_message_was_said(monkeypatch, model):
    """POST_1 I-2: todos.created_at is when the message was said, not when the job ran.  Here the
    extraction's own look failed (the row is kept), so only the push can fold it into the reminder made
    from that message an hour after it was said, hours before the extraction ran."""
    import memory

    _raw("msg-1", "user-a", "我後天要交報告", LAST_NIGHT)
    _reminder("交報告", _ts(TODAY + timedelta(days=1), 9), user="user-b", source_text="我後天要交報告",
              created_at=LAST_NIGHT + 3600)
    model["answers"].append([_item(1, "交報告", TODAY + timedelta(days=1))])

    def down(*args, **kwargs):
        raise RuntimeError("the reminder table is unreadable")

    with monkeypatch.context() as m:
        m.setattr(memory, "reminders_between", down)
        result = _run()
    assert len(model["prompts"]) == 1 and result.new == 1 and result.skipped == {"structural_unchecked": 1}

    assert _push(NOW + 86400) == 0

    assert _last_push()["fold_structural"] == 1 and _pushes() == []


def test_a_long_message_folds_on_its_first_500_characters():
    """POST_1 I-2: todos.source_text keeps 500 characters and a reminder the whole message; both are cut
    the same way before they are compared."""
    long = "我明天要加班，" + "合成的說明文字" * 80
    _seed_todo("加班", mid="msg-1", source=long[:500])
    _reminder("加班", _ts(TODAY + timedelta(days=10), 9), user="user-b", source_text=long, created_at=LAST_NIGHT + 62)

    assert _push() == 0

    assert _last_push()["fold_structural"] == 1


@pytest.mark.parametrize("status,folded", [("pending", True), ("processing", True), ("done", False), ("dropped", False)])
def test_a_message_still_in_the_reminder_queue_folds_the_todo(status, folded):
    _seed_todo("交報告", mid="msg-1", source="我明天要交報告")
    _queued("msg-1", status)

    assert _push() == (0 if folded else 1)

    assert _last_push()["fold_queue"] == (1 if folded else 0) and len(_pushes()) == (0 if folded else 1)


@pytest.mark.parametrize(
    "made,folded",
    [
        (lambda: _reminder("交報告", _ts(TODAY, 9), user="user-a"), True),
        (lambda: _reminder("交報告", _ts(TODAY, 9), user="user-b"), False),
        (lambda: _reminder("交報告", _ts(TODAY, 9), user="user-b", aliases=["全家"]), True),
        (lambda: _reminder("交報告（前一天提醒）", _ts(YESTERDAY, 20), user="user-a", status="done",
                           time_kind="none"), True),
        (lambda: _reminder("交報告", _ts(TODAY, 7), user="user-a", status="done"), True),
        (lambda: _reminder("交報告", _ts(TODAY, 7), user="user-a", status="cancelled"), True),
        (lambda: _event("交報告", TODAY, participants=["成員甲"]), True),
        (lambda: _event("交報告", TODAY, participants=["成員乙"]), False),
    ],
    ids=["own_reminder", "someone_elses_reminder", "whole_family", "labelled_day_before", "sent_today",
         "cancelled_today", "calendar_names_owner", "calendar_names_someone_else"],
)
def test_a_same_event_reminder_that_pings_the_owner_folds_the_todo(made, folded):
    tid = _seed_todo("交報告", mid="msg-1", source="我明天要交報告")
    made()

    assert _push() == (0 if folded else 1)

    push = _last_push()
    assert push["fold_semantic"] == (1 if folded else 0) and push["fold_structural"] == push["fold_queue"] == 0
    assert len(_pushes()) == (0 if folded else 1) and _todo(tid)["last_reminded_at"]


@pytest.mark.parametrize(
    "task,made",
    [
        ("牙醫回診", lambda: _reminder("看牙醫", _ts(TODAY, 10), user="user-a")),  # said another way
        ("看牙醫", lambda: _reminder("看牙醫", _ts(YESTERDAY, 20), user="user-a")),  # the night before, unlabelled
    ],
    ids=["paraphrase", "unlabelled_night_before"],
)
def test_known_limits_are_still_pushed(task, made):
    """Known limits (IMPL_PLAN_v2 deferred): a reminder worded differently, or set for the evening before
    without a label, is not the same event to the fold; both are pushed."""
    _seed_todo(task, mid="msg-1", source="我明天要去看牙醫")
    made()

    assert _push() == 1 and _last_push()["fold_semantic"] == 0


def test_a_fold_check_that_fails_holds_the_todo_back(monkeypatch, caplog):
    import reminder_push

    tid = _seed_todo("交報告", mid="msg-1", source="我明天要交報告")
    _reminder("交報告", _ts(TODAY, 9), user="user-a")
    calls: list = []

    def broken(row):
        calls.append(row)
        raise RuntimeError("合成的祕密字")

    monkeypatch.setattr(reminder_push, "reminder_audience", broken)
    caplog.set_level(logging.INFO)

    assert _push() == 0

    push = _last_push()
    assert calls and (push["fold_error"], push["pushed"]) == (1, 0) and _pushes() == []
    assert _todo(tid)["last_reminded_at"] is None  # not marked either
    assert "error_type=RuntimeError" in caplog.text and "祕密" not in caplog.text


def test_an_unsend_seen_only_at_push_time_cancels_the_todo():
    """PRE_4 I8: forget_message failed (or ran before the row existed); the push looks again."""
    import family_extract
    import todo

    tid = _seed_todo("交報告", mid="msg-1", source="我明天要交報告")
    with todo._conn() as c:
        family_extract.record_unsent(c, GROUP, "msg-1", NOW - 60)

    assert _push() == 0

    assert _pushes() == [] and _last_push()["unsent"] == 1
    assert _todo(tid)["status"] == "cancelled" and _todo(tid)["source_text"] is None


# ── unsend ──────────────────────────────────────────────────────────────────────────────────────────


def test_an_unsend_after_extraction_cancels_its_todos_and_drops_their_words(monkeypatch, model):
    import todo

    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])
    _run(NOW - 3600)
    shadow = _seed_todo("繳房租", mid="msg-1", source="我明天要交報告", status="shadow")
    other = _seed_todo("倒垃圾", mid="msg-2", source="我明天要倒垃圾")

    assert todo.forget_message(GROUP, "msg-1", now=NOW - 60) == 2
    assert _push() == 1  # only the other message's todo

    gone = [r for r in _todos() if r["source_msg_id"] == "msg-1"]
    assert {r["status"] for r in gone} == {"cancelled"} and all(r["source_text"] is None for r in gone)
    assert _todo(shadow)["status"] == "cancelled" and _todo(other)["last_reminded_at"]
    assert len(model["prompts"]) == 1 and "倒垃圾" in _pushed_text(_pushes()[0])


def test_an_unsend_gives_up_on_a_locked_database_and_logs_the_type_only(monkeypatch, caplog):
    """POST_1 N-9, POST_4 N1: the bot's webhook thread never waits long; a lock makes the unsend give up
    (logged by type), the row is left as it was."""
    import todo

    tid = _seed_todo("交報告", mid="msg-secret-1", source="我明天要交報告")
    monkeypatch.setattr(todo, "FORGET_TIMEOUT_S", 0.05, raising=False)
    caplog.set_level(logging.INFO)
    holder = sqlite3.connect(todo._DB_PATH, isolation_level=None, timeout=1)
    try:
        holder.execute("BEGIN IMMEDIATE")  # another writer holds the database
        assert todo.forget_message(GROUP, "msg-secret-1", now=NOW) == 0
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert (_todo(tid)["status"], _todo(tid)["source_text"]) == ("pending", "我明天要交報告")
    assert "error_type=OperationalError" in caplog.text
    for word in ("msg-secret-1", GROUP, "交報告"):
        assert word not in caplog.text, word


# ── the batch transaction looks again (PRE_3 5.3 step 6.5.1) ───────────────────────────────────────────


def test_a_message_unsent_while_the_model_answers_makes_no_todo(model):
    """The unsend table was read before the request; the batch transaction reads it again."""
    import todo

    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])
    model["during"].append(lambda: todo.forget_message(GROUP, "msg-1", now=NOW - 30))

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped == {"unsent": 1} and result.new == 0
    assert _todos() == []


def test_a_message_that_made_a_todo_while_the_model_answered_makes_no_second_one(model):
    """What a message already made was read before the request; the batch transaction reads it again.  The
    row made meanwhile (another run) has another task and day and is no longer pending, so neither the
    duplicate check nor the unique index could stop a second one."""
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])
    model["during"].append(
        lambda: _seed_todo("交期末報告", due=TODAY + timedelta(days=1), mid="msg-1", status="cancelled")
    )

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped == {"produced_before": 1} and result.new == 0
    assert [r["task"] for r in _todos()] == ["交期末報告"]


def test_a_failed_reminder_lookup_keeps_the_todo_and_counts_it(monkeypatch, caplog, model):
    """The extraction's structural look is a first pass only: the push looks again before anything goes
    out (test_a_reminder_or_event_made_from_the_same_message_folds_the_todo)."""
    import memory

    def broken(*args, **kwargs):
        raise RuntimeError("合成的祕密字")

    monkeypatch.setattr(memory, "reminders_between", broken)
    caplog.set_level(logging.INFO)
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.new == 1 and result.skipped == {"structural_unchecked": 1}
    assert "error_type=RuntimeError" in caplog.text and "祕密" not in caplog.text


def test_a_message_with_no_sender_is_nobodys_todo(model):
    _raw("msg-1", None, "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped == {"bad_owner": 1} and _todos() == []


# ── the list ────────────────────────────────────────────────────────────────────────────────────────


def test_the_list_never_shows_yesterday_before_the_morning_job():
    import todo

    _seed_todo("交報告", due=YESTERDAY)
    _seed_todo("繳房租", due=TODAY)
    small_hours = _ts(TODAY, 3)

    assert [r["task"] for r in todo.list_pending(GROUP, now=small_hours)] == ["繳房租"]
    assert todo.list_pending(GROUP, due_date=_iso(YESTERDAY), now=small_hours) == []
    assert [r["task"] for r in todo.list_pending(GROUP, today=YESTERDAY)] == ["交報告", "繳房租"]


# ── logs ────────────────────────────────────────────────────────────────────────────────────────────


def test_logs_and_output_carry_no_family_words_or_ids(monkeypatch, caplog, capsys, model):
    import todo

    caplog.set_level(logging.DEBUG)
    _raw("msg-secret-1", "user-a", "我明天要交星河報告", LAST_NIGHT)
    model["answers"].append([
        _item(1, "交星河報告", TODAY),
        {"i": "成員甲祕密", "task": "祕密事項", "due_date": "祕密日期"},
        _item(1, "祕密事項一二", TODAY),
        _item(1, "交星河報告", "祕密日期字"),
    ])

    _run()
    _push()
    todo.forget_message(GROUP, "msg-secret-1", now=NOW + 60)

    assert len(model["prompts"]) == 1 and len(_pushes()) == 1
    out = capsys.readouterr()
    logged = caplog.text + out.out + out.err
    for word in ("星河", "祕密", "成員甲", "user-a", GROUP, "msg-secret-1"):
        assert word not in logged, word


def test_an_exception_while_checking_an_item_is_logged_by_type_only(monkeypatch, caplog, model):
    import todo

    def boom(item, batch, result, *, today):
        raise ValueError("合成的祕密字 成員甲")

    monkeypatch.setattr(todo, "_check_item", boom)
    caplog.set_level(logging.INFO)
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    result = _run()

    assert len(model["prompts"]) == 1 and result.skipped.get("item_error") == 1 and _todos() == []
    assert "error_type=ValueError" in caplog.text and "祕密" not in caplog.text and "成員甲" not in caplog.text


def test_a_failed_extraction_still_lets_the_push_run_and_logs_the_type_only(monkeypatch, caplog, model):
    import todo

    def boom(group_id, *, now=None, push_follows=False):
        raise RuntimeError("合成的祕密字 msg-secret")

    monkeypatch.setattr(todo, "run_incremental", boom)
    caplog.set_level(logging.INFO)
    _seed_todo("交報告")

    assert todo.main_reminder(now=NOW) == 0

    assert len(_pushes()) == 1
    assert "todo extract failed error_type=RuntimeError" in caplog.text and "祕密" not in caplog.text


# ── drift: what was copied must keep matching where it came from ────────────────────────────────────


def _assigned(path: Path, name: str) -> ast.AST:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} is not assigned in {path.name}")


def _flags(node: ast.AST) -> int:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re":
        return int(getattr(re, node.attr))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flags(node.left) | _flags(node.right)
    raise AssertionError(f"unexpected flags {ast.unparse(node)}")


def _compiled(path: Path, name: str) -> re.Pattern:
    call = _assigned(path, name)
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == "re.compile", name
    flags = 0
    for extra in call.args[1:]:
        flags |= _flags(extra)
    return re.compile(ast.literal_eval(call.args[0]), flags)


_MAIN_DATE_SAMPLES = (
    "我11月20要交報告", "我 11 月 20 要交", "我11/20要交報告", "我20號要繳費", "我20日要繳費",
    "我今天要交報告", "今晚要去接人", "我明天要交報告", "明日要出發", "明晚要聚會", "後天要搬家", "大後天要搬家",
    "星期三要交", "週五要交", "周日要回老家", "禮拜天要去爬山",
    "下週要出差", "下 周要出差", "下星期要出差", "下月要搬家", "這週要交", "這 周要交", "這星期要交",
)
_CALENDAR_DATE_SAMPLES = (
    "這週末要回老家", "下週末要回老家", "下週要出差", "本週要交", "下禮拜要出差", "下星期要出差", "下個月要搬家",
    "月底要繳房租", "月初要繳費", "月中要繳費", "星期三要交", "週日要回老家", "11月20日要交", "11月20號要交",
    "2026-11-20要交", "11/20要交", "20 號要繳", "14:30要交報告",
)
_ADDED_DATE_SAMPLES = (
    "週末要回老家", "周末要回老家", "這禮拜要交", "這個禮拜要交", "年底要搬家", "明早要出發", "過年要回老家",
    "晚點要去接人", "五號要繳費", "十五號要繳費", "三月五日要出發", "下個禮拜要出差", "下個星期要出差",
)


def test_every_date_word_main_sends_on_is_a_todo_date_word():
    """N5: main._REMINDER_DATE_HINT read by AST; whatever it matches (after the same fraction mask), the
    TODO prefilter matches too.  A word main adds later fails here until todo.py has it."""
    import todo

    main_hint = _compiled(ROOT / "main.py", "_REMINDER_DATE_HINT")
    for text in _MAIN_DATE_SAMPLES:
        assert main_hint.search(todo._mask_non_date_slash_tokens(text)), text  # the sample covers main
        assert todo.todo_date_hint(text), text


def _pattern_text(path: Path, name: str) -> str:
    call = _assigned(path, name)
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == "re.compile", name
    return ast.literal_eval(call.args[0])


def _written_out(pattern: str) -> list[str]:
    """Every spelling of every alternative of ``pattern``, by re's own parser: a literal as it is, a
    character class as each of its characters, \\d as 1, \\s as a space, a repeat at its least count.  It
    fails on anything else, so a new kind of word in main.py cannot slip past this reader unread."""
    from re import _constants as sre
    from re import _parser

    def seq(items) -> list[str]:
        out = [""]
        for op, av in items:
            out = [a + b for a in out for b in one(op, av)]
            assert len(out) <= 1000, "too many spellings"
        return out

    def one(op, av) -> list[str]:
        if op is sre.LITERAL:
            return [chr(av)]
        if op is sre.IN:
            chars = []
            for kind, value in av:
                if kind is sre.LITERAL:
                    chars.append(chr(value))
                elif kind is sre.RANGE:
                    chars.append(chr(value[0]))
                elif kind is sre.CATEGORY and value is sre.CATEGORY_DIGIT:
                    chars.append("1")
                elif kind is sre.CATEGORY and value is sre.CATEGORY_SPACE:
                    chars.append(" ")
                else:
                    raise AssertionError(f"cannot write out {kind} {value}")
            return chars
        if op is sre.BRANCH:
            return [s for branch in av[1] for s in seq(branch)]
        if op is sre.SUBPATTERN:
            return seq(av[-1])
        if op in (sre.MAX_REPEAT, sre.MIN_REPEAT):
            low, _high, item = av
            return ["".join(p) for p in itertools.product(seq(item), repeat=low)] if low else [""]
        raise AssertionError(f"cannot write out {op}")

    return seq(_parser.parse(pattern))


def test_every_alternative_of_main_date_words_is_a_todo_date_word():
    """POST_3 I4: main._REMINDER_DATE_HINT is read by AST and each of its alternatives written out by re's
    own parser, not taken from a fixed list of sample sentences: a word main adds later (「改天」) fails
    here until todo.py has it too."""
    import todo

    main_hint = _compiled(ROOT / "main.py", "_REMINDER_DATE_HINT")
    words = _written_out(_pattern_text(ROOT / "main.py", "_REMINDER_DATE_HINT"))
    assert {"今天", "今晚", "明天", "明日", "明晚", "後天", "大後天", "下週", "星期三", "1/1", "1月1"} <= set(words)
    assert {"改天", "1號"} <= set(_written_out(r"(今天|改天|\d+號)"))  # the reader sees an added word
    for word in words:
        assert main_hint.search(todo._mask_non_date_slash_tokens(word)), word  # the reader spells main's words
        assert todo.todo_date_hint(word), word


def test_every_calendar_date_word_and_the_added_ones_are_todo_date_words():
    import todo

    calendar_hint = _compiled(ROOT / "calendar_extractor.py", "_MODEL_DATE_HINT_RE")
    for text in _CALENDAR_DATE_SAMPLES:
        assert calendar_hint.search(text), text
        assert todo.todo_date_hint(text), text
    for text in _ADDED_DATE_SAMPLES:
        assert todo.todo_date_hint(text), text
    for text in ("我要交報告", "才1/3的價格", "只要2/3杯"):
        assert not todo.todo_date_hint(text), text


def test_the_copied_mask_and_bigram_rules_match_their_sources():
    import todo

    for name in ("_SLASH_DATE_TOKEN_RE", "_FRACTION_PREFIX_RE", "_FRACTION_SUFFIX_RE"):
        source = _compiled(ROOT / "main.py", name)
        mine = getattr(todo, name)
        assert (mine.pattern, mine.flags) == (source.pattern, source.flags), name
    daily = ROOT / "jobs" / "daily_memory.py"
    for name in ("_CJK_RUN_RE", "_ALNUM_RUN_RE"):
        source = _compiled(daily, name)
        assert (getattr(todo, name).pattern, getattr(todo, name).flags) == (source.pattern, source.flags), name
    fillers = _assigned(daily, "_FILLER_BIGRAMS")
    assert isinstance(fillers, ast.Call) and ast.unparse(fillers.func) == "frozenset"
    assert todo._FILLER_BIGRAMS == frozenset(ast.literal_eval(fillers.args[0]))


# ── the helpers in other modules ────────────────────────────────────────────────────────────────────


def test_reminders_between_reads_every_status_and_deletes_nothing():
    import memory

    soon = int(time.time()) + 3600
    ids = [
        _reminder("交報告", soon),
        _reminder("交報告", soon),  # an exact duplicate: list_pending_reminders_full would delete one
        _reminder("繳房租", soon + 60, status="done"),
        _reminder("倒垃圾", soon + 120, status="cancelled"),
    ]
    _reminder("買菜", soon + 7200)  # outside
    _reminder("交報告", soon, group=OTHER)

    rows = memory.reminders_between(GROUP, soon, soon + 3600)

    assert [r["reminder_id"] for r in rows] == ids
    assert [r["status"] for r in rows] == ["pending", "pending", "done", "cancelled"]
    assert isinstance(rows[0]["mention_aliases"], list) and isinstance(rows[0]["merged_details"], list)
    full = memory.list_pending_reminders_full(GROUP, dedupe=False)
    assert set(rows[0]) == set(full[0]) | {"status"}
    with memory._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 6


def test_events_on_reads_every_status_of_one_day():
    import calendar_db

    wanted = {_event("交報告", TODAY), _event("繳房租", TODAY, status="cancelled")}
    _event("倒垃圾", TODAY + timedelta(days=1))
    _event("買菜", TODAY, group=OTHER)

    assert {e["event_id"] for e in calendar_db.events_on(GROUP, _iso(TODAY))} == wanted
    assert calendar_db.events_on(GROUP, "") == []


def test_todo_same_event_is_pure(monkeypatch):
    import calendar_db
    import memory
    import reminder_overview

    def no_db(*args, **kwargs):
        raise AssertionError("todo_same_event never reads a database")

    todo_row = reminder_overview.todo_item("交報告", _iso(TODAY), owner="成員甲", user_id="user-a")
    row = {"action": "交報告", "remind_at": _ts(TODAY, 9), "user_id": "user-a", "time_kind": "clock",
           "mention_aliases": [], "status": "done"}

    with monkeypatch.context() as m:  # undone before conftest's teardown opens the test DBs again
        m.setattr(memory, "_conn", no_db)
        m.setattr(calendar_db, "_conn", no_db)
        assert reminder_overview.todo_same_event(todo_row, row) is True
        assert reminder_overview.todo_same_event(todo_row, {**row, "remind_at": _ts(TODAY + timedelta(days=1), 9)}) is False
        assert reminder_overview.todo_same_event(todo_row, {**row, "mention_aliases": ["成員乙"]}) is False


def test_reminder_audience_is_the_push_own_rule():
    import reminder_push

    assert reminder_push.reminder_audience is reminder_push._reminder_audience
    assert reminder_push.reminder_audience({"user_id": "user-b", "mention_aliases": ["全家"], "action": "交報告"}) == ("all:",)


# ── jobs/todo_shadow_list.py ────────────────────────────────────────────────────────────────────────


def _bot_dir(tmp_path, *, env: str | None, with_db: bool, monkeypatch) -> Path:
    import todo

    base = tmp_path / "bot"
    base.mkdir(parents=True)
    if env is not None:
        (base / ".env").write_text(env, encoding="utf-8")
    if with_db:
        monkeypatch.setattr(todo, "_DB_PATH", base / "line_bot.db")
        todo.init_db()
    return base


def test_the_shadow_list_refuses_to_run_outside_the_production_folder(tmp_path, monkeypatch, capsys):
    from jobs import todo_shadow_list as job

    family = f"FAMILY_GROUP_ID={GROUP}\nALLOWED_GROUP_IDS={GROUP}\n"
    no_env = _bot_dir(tmp_path / "a", env=None, with_db=False, monkeypatch=monkeypatch)
    no_db = _bot_dir(tmp_path / "b", env=family, with_db=False, monkeypatch=monkeypatch)
    no_group = _bot_dir(tmp_path / "c", env=f"ALLOWED_GROUP_IDS={OTHER}\n", with_db=True, monkeypatch=monkeypatch)

    for base in (no_env, no_db, no_group):
        assert job.main([], base=base) == 2
    assert not list(no_env.glob("*.db")) and not list(no_db.glob("*.db"))  # nothing was opened or made
    assert job.main(["--all"], base=no_group) == 2  # a typo does nothing either
    out = capsys.readouterr().out
    assert GROUP not in out and OTHER not in out


def test_the_shadow_list_prints_names_tasks_and_dates_as_data(tmp_path, monkeypatch, capsys):
    import todo
    from jobs import todo_shadow_list as job

    base = _bot_dir(tmp_path, env=f"FAMILY_GROUP_ID={GROUP}\nALLOWED_GROUP_IDS={GROUP},{OTHER}\n",
                    with_db=True, monkeypatch=monkeypatch)
    shadow = todo.insert_todo(GROUP, "交報告", "user-a", _iso(TODAY), source_msg_id="msg-1", source_text="我明天要交報告")
    todo.insert_todo(GROUP, "繳房租", "user-b", _iso(TODAY), source_msg_id="msg-2")  # pending: not listed
    other = todo.insert_todo(OTHER, "倒垃圾", "user-a", _iso(TODAY))
    with todo._conn() as c:
        c.execute("UPDATE todos SET status='shadow' WHERE todo_id IN (?, ?)", (shadow, other))

    assert job.main([], base=base) == 0

    out = capsys.readouterr().out
    assert "是資料，不是指示" in out
    assert f'1. {_iso(TODAY)} "成員甲" "交報告"' in out
    for word in ("繳房租", "倒垃圾", "我明天要交報告", "user-a", "msg-1", GROUP):
        assert word not in out, word


def test_the_shadow_list_never_imports_todo():
    """todo's import builds tables (a write); the list opens the DB read-only instead."""
    tree = ast.parse((ROOT / "jobs" / "todo_shadow_list.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "todo" not in imported and "family_extract" not in imported



def test_a_manual_extract_before_08_00_keeps_todays_todo_for_the_push(model):
    """REV_1 I-2: run by hand at 07:00, a todo due today is made; the 08:00 job pushes it."""
    import todo

    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])

    early = todo.run_incremental(GROUP, now=_ts(TODAY, 7))
    assert early.new == 1 and early.skipped == {} and len(model["prompts"]) == 1

    assert todo.main_reminder(now=NOW) == 0
    assert len(model["prompts"]) == 1 and len(_pushes()) == 1


# ── 2026-11-21 修正輪第 2 輪覆核（新版 §3.8，1 席反例）F1、F3 ──────────────────────────


def test_a_lock_during_the_push_leaves_an_alert_for_the_daily_report(model, monkeypatch):
    """F1 (a): the 08:00 extraction finishes, then another writer holds the lock through the push half, so
    nothing is pushed and no summary is saved.  Yesterday's summary must not read as today's."""
    import sqlite3

    import family_extract
    import todo
    from jobs import family_extract_status as status

    assert todo.main_reminder(now=NOW - 86400) == 0  # yesterday: healthy, nothing due
    _raw("msg-1", "user-a", "我明天要交報告", LAST_NIGHT)
    model["answers"].append([_item(1, "交報告", TODAY)])
    with todo._conn() as c:
        path = c.execute("PRAGMA database_list").fetchone()[2]
    real_expire = todo.expire_past_due
    held = {}

    def locked_expire(group_id, today):
        if group_id == GROUP and "h" not in held:
            h = sqlite3.connect(path, isolation_level=None)
            h.execute("BEGIN IMMEDIATE")  # another process takes the write lock as the push starts
            held["h"] = h
        return real_expire(group_id, today)

    monkeypatch.setattr(todo, "expire_past_due", locked_expire)
    monkeypatch.setattr(todo, "_job_conn", lambda: todo._conn(timeout=0.2))
    assert todo.main_reminder(now=NOW) == 0
    held["h"].execute("ROLLBACK")
    held["h"].close()

    with todo._conn() as c:
        job = family_extract.status(c, "todo", GROUP, now=NOW + 6000, newest_raw=None, unread=None)
    job["last_run"] = status._clean(job["last_run"], status._RUN_KEYS)
    job["last_push"] = status._clean(job["last_push"], status._PUSH_KEYS)
    codes = status.alerts({"todo": job}, {}, {"todo": "on", "anniversary": "off"}, now=NOW + 6000)
    assert _pushes() == [] and job["last_push"]["date"] == _iso(YESTERDAY)
    assert "todo_push_not_today" in codes


@pytest.mark.parametrize("prefix", ["下下", "下", "這", "本", "下個月", "下下個", "每", "上", "每月"])
def test_a_long_run_of_spaces_after_a_date_word_stays_fast(prefix):
    """F3: 「\\s*個?\\s*」 and 「\\s*的?\\s*」 tried every split of a run of spaces (5000 spaces: 0.42 s, and one
    LINE message can hold them).  One optional group per gap keeps the search linear."""
    import time

    import todo

    text = prefix + " " * 20000 + "x"
    started = time.perf_counter()
    todo._EXPECTED_DATE_RE.search(text)
    assert time.perf_counter() - started < 1.0


def test_the_faster_date_pattern_reads_every_text_the_way_the_old_one_did():
    """F3, differential (新版 §3.8): only the way a gap of spaces is written changed, so on random texts made
    of the words the pattern knows, the old form and the new form find the same dates in the same places."""
    import random
    import re

    import todo

    new = todo._EXPECTED_DATE_RE
    old = re.compile(new.pattern.replace(r"\s*(?:個\s*)?", r"\s*個?\s*").replace(r"\s*(?:的\s*)?", r"\s*的?\s*"))
    assert old.pattern != new.pattern
    pieces = ["下", "下", "這", "本", "上", "每", "個", "一", "月", "的", "週", "星期", "禮拜", "三", "日", "天",
              "號", "5", "12", "/", " ", "  ", "明天", "後天", "年", "2026", "-", "十", "二十", "x"]
    rng = random.Random(20261121)
    for _ in range(20000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 10)))
        found_old = [(m.span(), m.groupdict()) for m in old.finditer(text)]
        found_new = [(m.span(), m.groupdict()) for m in new.finditer(text)]
        assert found_old == found_new, text
