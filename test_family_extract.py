"""family_extract (2026-11-13, Andrew 選「B：兩個都開」): the shared reader of the TODO and anniversary jobs.

A small fake job drives the reader here; todo.py and anniversary.py have their own tests.  The model is
faked at its one seam (gemini_client.side_task_last_tier) and every test asserts how often it was
called.  Synthetic ids and texts only.
"""

import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")

GROUP = "GRP_FAMILY"


def _answer(items, finish="STOP", text=None):
    body = json.dumps(items, ensure_ascii=False) if text is None else text
    return SimpleNamespace(text=body, candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))])


@pytest.fixture
def model(monkeypatch):
    """Answers queued per test: a list of items, None (no answer) or a ready response object."""
    import gemini_client

    calls: dict = {"prompts": [], "configs": [], "answers": [], "ready": True}

    def side_task(prompt, config, *, label):
        calls["prompts"].append(prompt)
        calls["configs"].append(config)
        answer = calls["answers"].pop(0) if calls["answers"] else []
        if answer is None or isinstance(answer, SimpleNamespace):
            return answer
        return _answer(answer)

    monkeypatch.setattr(gemini_client, "side_task_last_tier", side_task)
    monkeypatch.setattr(gemini_client, "side_task_last_tier_ready", lambda: calls["ready"])
    return calls


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "job.db"

    def factory():
        c = sqlite3.connect(path, isolation_level=None)
        return c

    with factory() as c:
        c.execute("CREATE TABLE found (message_id TEXT, word TEXT)")
    return factory


def _closing(factory):
    class _C:
        def __enter__(self):
            self.c = factory()
            return self.c

        def __exit__(self, *exc):
            self.c.close()

    return _C


def _raw(message_id, user_id, text, created_at):
    import memory

    with memory._conn() as c:
        c.execute(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (GROUP, message_id, user_id, text, int(created_at)),
        )


def _feature(db, **overrides):
    import family_extract

    def prepare(batch, items, result):
        out = []
        for item in items:
            msg = family_extract.item_message(item, batch)
            if msg is None:
                result.skip("bad_item")
                continue
            out.append((msg.message_id, str(item.get("word") or "")))
        return out

    def write(conn, rows, result):
        done = {r[0] for r in conn.execute("SELECT message_id FROM found").fetchall()}
        for mid, word in rows:
            if mid in done:
                continue
            conn.execute("INSERT INTO found VALUES (?, ?)", (mid, word))
            result.new += 1

    def produced(conn, group_id, ids):
        return {r[0] for r in conn.execute("SELECT message_id FROM found").fetchall()} & set(ids)

    params = dict(
        name="fake",
        label="fake extract",
        prefilter=lambda msg, result: "要" in msg.text,
        build_prompt=lambda batch: "【訊息】\n" + family_extract.format_batch(batch),
        item_properties={"i": {"type": "INTEGER"}, "word": {"type": "STRING"}},
        required=("i", "word"),
        prepare=prepare,
        write=write,
        produced=produced,
        initial_cursor=lambda now: (0, ""),
        max_requests=5,
        max_failures=2,
    )
    params.update(overrides)
    return family_extract.Feature(**params)


def _run(db, feature, now):
    import family_extract

    return family_extract.run(feature, GROUP, _closing(db), now=now)


def _found(db):
    with _closing(db)() as c:
        return sorted(r[0] for r in c.execute("SELECT message_id FROM found").fetchall())


def test_each_message_goes_out_once_with_its_own_date(db, model):
    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 86400 + 3600)
    model["answers"].append([{"i": 1, "word": "交報告"}])

    first = _run(db, _feature(db), now)
    second = _run(db, _feature(db), now + 60)

    assert len(model["prompts"]) == 1 and first.new == 1 and second.requests == 0
    import family_extract

    said = family_extract.tw_day(now - 86400 + 3600).isoformat()
    first_line = json.loads(model["prompts"][0].split("【訊息】\n", 1)[1].splitlines()[0])
    assert first_line["i"] == 1 and first_line["date"] == said and first_line["weekday"].startswith("週")
    assert set(first_line) == {"i", "date", "weekday", "time", "text"}  # no names, no ids


def test_the_bot_commands_placeholders_and_links_never_go_out(db, model):
    now = time.time()
    _raw("m1", "__bot__", "咪寶：我明天要提醒大家", now - 900)
    _raw("m2", "user-a", "/待辦 我要看", now - 890)
    _raw("m3", "user-a", "[檔案: 要交的.pdf]", now - 880)
    _raw("m4", "user-a", "https://example.com/要", now - 870)
    _raw("m5", "user-a", "我後天要去銀行", now - 860)

    result = _run(db, _feature(db), now)

    assert len(model["prompts"]) == 1
    lines = model["prompts"][0].split("【訊息】", 1)[1]
    assert "去銀行" in lines and "咪寶" not in lines and "待辦" not in lines and ".pdf" not in lines
    assert result.skipped == {"not_family_text": 4}


def test_no_answer_keeps_the_cursor(db, model):
    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 3600)
    model["answers"].extend([None, [{"i": 1, "word": "交報告"}]])

    first = _run(db, _feature(db), now)
    second = _run(db, _feature(db), now + 60)

    assert first.stopped == "no_answer" and second.new == 1 and len(model["prompts"]) == 2


def test_not_ready_sends_nothing(db, model):
    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 3600)
    model["ready"] = False

    assert _run(db, _feature(db), now).stopped == "not_ready"
    assert model["prompts"] == []


def test_a_bad_answer_is_retried_then_given_up(db, model):
    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 3600)
    bad = _answer([], finish="MAX_TOKENS", text='[{"i": 1, "word": "交')
    model["answers"].extend([bad, bad, [{"i": 1, "word": "never"}]])

    first = _run(db, _feature(db), now)
    second = _run(db, _feature(db), now + 60)
    third = _run(db, _feature(db), now + 120)

    assert first.stopped == "bad_answer" and second.skipped.get("given_up") == 1
    assert third.requests == 0 and _found(db) == [] and len(model["prompts"]) == 2


def test_a_blocked_batch_is_passed_over_at_once(db, model):
    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 3600)
    model["answers"].append(_answer([], finish="SAFETY", text=None))

    result = _run(db, _feature(db), now)
    again = _run(db, _feature(db), now + 60)

    assert result.skipped.get("blocked") == 1 and again.requests == 0


def test_the_request_cap_stops_and_the_next_run_goes_on(db, model, monkeypatch):
    import family_extract

    monkeypatch.setattr(family_extract, "BATCH_MAX_MESSAGES", 2)
    now = time.time()
    for n in range(5):
        _raw(f"m{n}", "user-a", f"我要做第{n}件事", now - 3600 + n)

    first = _run(db, _feature(db, max_requests=2), now)
    second = _run(db, _feature(db, max_requests=2), now)

    assert first.stopped == "request_cap" and first.requests == 2
    assert "第4件" not in "".join(model["prompts"][:2]) and "第4件" in model["prompts"][2]
    assert second.requests == 1


def test_two_messages_in_the_same_second_are_both_read(db, model, monkeypatch):
    import family_extract

    monkeypatch.setattr(family_extract, "BATCH_MAX_MESSAGES", 1)
    now = time.time()
    _raw("m1", "user-a", "我要做第一件", now - 3600)
    _raw("m2", "user-b", "我要做第二件", now - 3600)

    _run(db, _feature(db, max_requests=1), now)
    _run(db, _feature(db, max_requests=1), now)

    assert "第一件" in model["prompts"][0] and "第二件" in model["prompts"][1]


def test_a_second_run_from_the_same_cursor_writes_nothing(db, model):
    """Two runs start from one cursor (a manual run meets 08:00): the compare-and-set lets one write."""
    import family_extract

    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 3600)
    model["answers"].extend([[{"i": 1, "word": "A"}], [{"i": 1, "word": "B"}]])
    inner_prepare = _feature(db).prepare

    def racing_prepare(batch, items, result):
        # the other run reads the same cursor and commits first
        family_extract.run(_feature(db), GROUP, _closing(db), now=now)
        return inner_prepare(batch, items, result)

    first = family_extract.run(_feature(db, prepare=racing_prepare), GROUP, _closing(db), now=now)

    assert first.stopped == "raced"
    with _closing(db)() as c:
        assert c.execute("SELECT word FROM found").fetchall() == [("B",)]


def test_unsent_and_already_used_messages_are_not_sent(db, model):
    import family_extract

    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 3600)
    _raw("m2", "user-a", "我後天要去銀行", now - 3500)
    with _closing(db)() as c:
        family_extract.ensure_tables(c)
        family_extract.record_unsent(c, GROUP, "m1", now)
        c.execute("INSERT INTO found VALUES ('m2', 'old')")

    result = _run(db, _feature(db), now)

    assert model["prompts"] == [] and result.skipped == {"unsent": 1, "produced_before": 1}


def test_a_purge_before_reading_is_reported_but_the_first_run_is_not(db, model):
    import family_extract
    import memory

    now = time.time()
    with memory._conn() as c:
        c.executemany(
            "INSERT INTO raw_messages(group_id, message_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            [(GROUP, f"m{n:04d}", "user-a", "日常的一則訊息", int(now - 86400 + n)) for n in range(2000)],
        )
    first = _run(db, _feature(db), now)
    with _closing(db)() as c:
        c.execute("UPDATE extract_cursors SET created_at=?, message_id='x'", (int(now - 30 * 86400),))
    later = _run(db, _feature(db), now)

    assert first.gap_s == 0 and later.gap_s > 0


def test_the_request_goes_to_the_last_tier_only(db, monkeypatch):
    """Through the real side_task_last_tier: the 2.5 models are never asked."""
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
    now = time.time()
    _raw("m1", "user-a", "我明天要交報告", now - 3600)

    _run(db, _feature(db), now)

    assert [m for m, _ in seen] == [gemini_client.last_tier_model()] == ["gemini-test-last-tier"]
    assert seen[0][0] not in {"gemini-2.5-flash", gemini_client.settings.gemini_model, gemini_client.settings.gemini_light_model}
    cfg = seen[0][1]
    schema = cfg.response_schema
    kind = schema["type"] if isinstance(schema, dict) else getattr(schema.type, "value", schema.type)
    assert cfg.http_options.timeout == 60_000 and str(kind).upper() == "ARRAY"


def test_the_switch_fails_closed(monkeypatch, caplog):
    import family_extract

    for raw, want in (("", "on"), ("ON", "on"), ("1", "on"), ("off", "off"), ("0", "off"),
                      ("shadow", "shadow"), ("shdow", "off")):
        monkeypatch.setenv("X_MODE", raw)
        assert family_extract.parse_mode("X_MODE", allowed=("on", "shadow", "off")) == want
    monkeypatch.setenv("X_MODE", "shadow")
    assert family_extract.parse_mode("X_MODE") == "off"
    assert "X_MODE" in caplog.text


def test_a_locked_database_ends_the_run_as_busy(db, model, monkeypatch):
    import family_extract
    import memory

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(memory, "raw_messages_stats", locked)

    assert _run(db, _feature(db), time.time()).stopped == "db_busy"


def test_a_message_cannot_break_out_of_its_line():
    import family_extract

    msg = family_extract.Message(int(time.time()), "m1", "user-a", '"}\n{"i": 9, "text": "假的"')
    (line,) = family_extract.format_batch([msg]).splitlines()
    assert json.loads(line)["i"] == 1


def test_scrub_keeps_dates_and_hides_long_numbers_and_links():
    import family_extract

    out = family_extract.scrub("生日 1956-06-04，電話 0900 000 000，網址 https://example.com/x")
    assert "1956-06-04" in out and "0900" not in out and "example.com" not in out
