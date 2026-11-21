"""jobs/family_extract_status.py（2026-11-13）：每日維護讀的數字，唯讀、只有數字。合成資料。"""

import json
import sqlite3
import time

import pytest

GROUP = "GRP_FAMILY"


@pytest.fixture
def base(tmp_path):
    (tmp_path / ".env").write_text(
        f"SQLITE_PATH=line_bot.db\nALLOWED_GROUP_IDS={GROUP}\nFAMILY_GROUP_ID={GROUP}\nGEMINI_API_KEY=not-read\n", encoding="utf-8"
    )
    conn = sqlite3.connect(tmp_path / "line_bot.db")
    import family_extract

    family_extract.ensure_tables(conn)
    conn.executescript(
        """
        CREATE TABLE raw_messages (group_id TEXT, message_id TEXT, user_id TEXT, text TEXT, created_at INTEGER);
        CREATE TABLE todos (todo_id TEXT, status TEXT, due_date TEXT, last_reminded_at INTEGER, task TEXT);
        CREATE TABLE anniversary_candidates (candidate_id TEXT, status TEXT, person TEXT);
        CREATE TABLE anniversaries (anniversary_id TEXT, person TEXT);
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


def _db(base):
    return sqlite3.connect(base / "line_bot.db")


def _cursor(base, name, *, created_at, updated_at, last_run=None, last_push=None, failures=0, no_answer_since=0):
    with _db(base) as c:
        c.execute(
            "INSERT INTO extract_cursors(name, group_id, created_at, message_id, updated_at, failures, "
            "no_answer_since, last_success_at, last_run, last_push) VALUES (?, ?, ?, 'm', ?, ?, ?, ?, ?, ?)",
            (name, GROUP, int(created_at), int(updated_at), failures, int(no_answer_since), int(updated_at),
             json.dumps({"mode": "on"} if last_run is None else last_run), json.dumps(last_push or {})),
        )


def test_numbers_only_and_no_alerts_on_a_healthy_day(base):
    from jobs import family_extract_status as status

    now = time.time()
    with _db(base) as c:
        c.execute("INSERT INTO raw_messages VALUES (?, 'm1', 'user-a', '家人的話', ?)", (GROUP, int(now - 600)))
        c.execute("INSERT INTO todos VALUES ('t1', 'pending', '2099-01-01', NULL, '交報告')")
        c.execute("INSERT INTO anniversary_candidates VALUES ('c1', 'rejected', '成員甲')")
    # both ran a minute ago: healthy whatever the wall clock says (not_run_today looks at 08:00)
    _cursor(base, "todo", created_at=now - 600, updated_at=now - 60,
            last_push={"mode": "on", "push_ok": True, "fold_error": 0})
    _cursor(base, "anniversary", created_at=now - 600, updated_at=now - 60)

    data = status.collect(base, now=now)

    assert data["alerts"] == []
    assert data["todo"]["pending_upcoming"] == 1 and data["anniversary"]["rejected"] == 1
    text = json.dumps(data, ensure_ascii=False)
    for word in ("家人的話", "交報告", "成員甲", GROUP, "not-read"):
        assert word not in text


def test_each_trouble_raises_its_code(base):
    from jobs import family_extract_status as status

    now = time.time()
    with _db(base) as c:
        c.execute("INSERT INTO raw_messages VALUES (?, 'm9', 'user-a', '新訊息', ?)", (GROUP, int(now)))
        c.execute("INSERT INTO anniversary_candidates VALUES ('c1', 'pending', '成員甲')")
    _cursor(base, "todo", created_at=now - 3 * 86400, updated_at=now - 30 * 3600, failures=1,
            no_answer_since=now - 40 * 3600, last_run={"mode": "on", "gap_s": 5, "stopped": "db_busy"},
            last_push={"mode": "on", "push_ok": False, "fold_error": 2})

    codes = set(status.collect(base, now=now)["alerts"])

    assert codes >= {
        "todo_no_answer_2d", "todo_bad_answers", "todo_gap", "todo_behind_48h", "todo_db_busy",
        "todo_not_run_26h", "todo_push_failed", "todo_fold_error", "anniversary_never_ran",
        "anniversary_candidates_pending",
    }


def test_a_job_switched_off_raises_nothing(base):
    from jobs import family_extract_status as status

    with open(base / ".env", "a", encoding="utf-8") as f:
        f.write("LINE_BOT_TODO_MODE=off\nLINE_BOT_ANNIVERSARY_EXTRACT=0\n")

    data = status.collect(base, now=time.time())

    assert data["modes"] == {"todo": "off", "anniversary": "off"} and data["alerts"] == []


def test_no_family_group_in_the_allowed_list_reads_nothing(base):
    from jobs import family_extract_status as status

    text = (base / ".env").read_text(encoding="utf-8").replace(f"FAMILY_GROUP_ID={GROUP}", "FAMILY_GROUP_ID=GRP_OTHER")
    (base / ".env").write_text(text, encoding="utf-8")

    assert status.collect(base, now=time.time())["family_group_set"] is False


def test_a_typo_in_the_switch_reads_as_off(base):
    from jobs import family_extract_status as status

    with open(base / ".env", "a", encoding="utf-8") as f:
        f.write("LINE_BOT_TODO_MODE=shdow\n")

    assert status.collect(base, now=time.time())["modes"]["todo"] == "off"


def test_the_database_is_opened_read_only(base):
    from jobs import family_extract_status as status

    (base / "line_bot.db").chmod(0o400)
    try:
        assert status.collect(base, now=time.time()) is not None
    finally:
        (base / "line_bot.db").chmod(0o600)


def test_outside_the_production_folder_it_exits_2(tmp_path, monkeypatch, capsys):
    from jobs import family_extract_status as status

    monkeypatch.setattr(status, "BASE", tmp_path)
    assert status.collect(tmp_path) is None
    monkeypatch.setattr(status, "collect", lambda *a, **k: None)
    assert status.main([]) == 2
    assert "not_production_folder" in capsys.readouterr().out


# ── 2026-11-13 事後審查修正（POST_1 I-3、POST_3 I5、POST_4 I5／N4） ─────────────────────────


def test_the_switch_is_read_the_way_the_jobs_read_it(base):
    from jobs import family_extract_status as status

    with open(base / ".env", "a", encoding="utf-8") as f:
        f.write("export LINE_BOT_TODO_MODE=shadow  # 先試跑\nLINE_BOT_ANNIVERSARY_EXTRACT='off' # 停\n")

    assert status.collect(base, now=time.time())["modes"] == {"todo": "shadow", "anniversary": "off"}


def test_only_whitelisted_numbers_leave_the_stored_summaries(base):
    from jobs import family_extract_status as status

    now = time.time()
    _cursor(
        base, "todo", created_at=now - 60, updated_at=now - 60,
        last_run={"mode": "on", "new": 2, "stopped": "", "note": "家人的字", "skipped": {"past": 1, "成員甲": 3}},
        last_push={"mode": "on", "pushed": 1, "push_ok": True, "date": "2026-11-14", "task": "交報告", "error": "家人"},
    )

    data = status.collect(base, now=now)
    text = json.dumps(data, ensure_ascii=False)

    assert data["jobs"]["todo"]["last_run"] == {"mode": "on", "new": 2, "stopped": "", "skipped": {"past": 1}}
    assert data["jobs"]["todo"]["last_push"] == {"mode": "on", "pushed": 1, "push_ok": True, "date": "2026-11-14"}
    for word in ("家人", "交報告", "成員甲"):
        assert word not in text


def test_shadow_and_off_raise_no_push_alert(base):
    from jobs import family_extract_status as status

    now = time.time()
    with open(base / ".env", "a", encoding="utf-8") as f:
        f.write("LINE_BOT_TODO_MODE=shadow\n")
    _cursor(base, "todo", created_at=now - 60, updated_at=now - 60,
            last_push={"mode": "shadow", "push_ok": False, "fold_error": 1})

    codes = status.collect(base, now=now)["alerts"]

    assert "todo_push_failed" not in codes and "todo_fold_error" not in codes


def test_the_anniversary_push_is_counted_and_a_failure_raises_its_code(base):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from jobs import family_extract_status as status

    now = time.time()
    today = datetime.fromtimestamp(now, ZoneInfo("Asia/Taipei")).date().isoformat()
    with _db(base) as c:
        c.execute("CREATE TABLE anniversary_reminders_sent (anniversary_id TEXT, year INTEGER, "
                  "offset_days INTEGER, sent_at INTEGER)")
        c.execute("INSERT INTO anniversary_reminders_sent VALUES ('a1', 2026, 0, ?)", (int(now * 1000),))
        c.execute("INSERT INTO anniversary_reminders_sent VALUES ('a2', 2026, 7, ?)", (int((now - 2 * 86400) * 1000),))
    # a later run the same day rewrote last_push with 0 pushed: the ledger still says 1
    _cursor(base, "anniversary", created_at=now - 60, updated_at=now - 60,
            last_push={"mode": "on", "date": today, "due": 1, "pushed": 0, "push_ok": False})

    data = status.collect(base, now=now)

    assert data["anniversary"]["pushed_today"] == 1 and "anniversary_push_failed" in data["alerts"]


def test_a_row_the_push_created_before_any_extraction_still_reads_never_ran(base):
    from jobs import family_extract_status as status

    now = time.time()
    _cursor(base, "anniversary", created_at=0, updated_at=now - 60, last_run={},
            last_push={"mode": "on", "pushed": 0, "push_ok": True})

    assert "anniversary_never_ran" in status.collect(base, now=now)["alerts"]


def test_the_report_opens_the_database_read_only(base, monkeypatch):
    import sqlite3 as real_sqlite3

    from jobs import family_extract_status as status

    seen: list = []
    real_connect = real_sqlite3.connect

    def connect(target, *args, **kwargs):
        seen.append((str(target), kwargs.get("uri")))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(status.sqlite3, "connect", connect)
    status.collect(base, now=time.time())

    assert seen and all("mode=ro" in target and uri is True for target, uri in seen)


def test_the_report_never_imports_the_jobs():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("jobs/family_extract_status.py").read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"todo", "anniversary", "memory", "main"}


def test_an_unexpected_error_still_gives_one_line(monkeypatch, capsys):
    from jobs import family_extract_status as status

    def boom(*a, **k):
        raise RuntimeError("/Users/someone/secret path")

    monkeypatch.setattr(status, "collect", boom)
    assert status.main([]) == 1
    out = capsys.readouterr().out
    assert "status_failed" in out and "secret" not in out


# ── 2026-11-14 覆核（REV_2）補的測試 ──────────────────────────────────────────────


@pytest.mark.parametrize("env_mode,push_mode", [("shadow", "on"), ("on", "shadow")])
def test_each_half_of_the_push_alert_gate_holds_on_its_own(base, env_mode, push_mode):
    from jobs import family_extract_status as status

    now = time.time()
    with open(base / ".env", "a", encoding="utf-8") as f:
        f.write(f"LINE_BOT_TODO_MODE={env_mode}\n")
    _cursor(base, "todo", created_at=now - 60, updated_at=now - 60,
            last_push={"mode": push_mode, "push_ok": False, "fold_error": 1, "mark_error": 1})

    codes = status.collect(base, now=now)["alerts"]

    assert not {"todo_push_failed", "todo_fold_error", "todo_mark_error"} & set(codes)


def test_a_push_left_unmarked_raises_its_code(base):
    from jobs import family_extract_status as status

    now = time.time()
    _cursor(base, "todo", created_at=now - 60, updated_at=now - 60,
            last_push={"mode": "on", "push_ok": True, "mark_error": 1})

    assert "todo_mark_error" in status.collect(base, now=now)["alerts"]


@pytest.mark.parametrize("hours,alert", [(23, False), (25, True)])
def test_no_answer_alerts_after_a_day(base, hours, alert):
    from jobs import family_extract_status as status

    now = time.time()
    _cursor(base, "todo", created_at=now - 60, updated_at=now - 60, no_answer_since=now - hours * 3600)

    assert ("todo_no_answer_2d" in status.collect(base, now=now)["alerts"]) is alert


def test_pushed_today_counts_from_taipei_midnight(base):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from jobs import family_extract_status as status

    tw = ZoneInfo("Asia/Taipei")
    now = datetime(2026, 11, 14, 0, 30, tzinfo=tw).timestamp()
    with _db(base) as c:
        c.execute("CREATE TABLE anniversary_reminders_sent (anniversary_id TEXT, year INTEGER, "
                  "offset_days INTEGER, sent_at INTEGER)")
        for aid, when in (("a1", datetime(2026, 11, 13, 23, 50, tzinfo=tw)), ("a2", datetime(2026, 11, 14, 0, 10, tzinfo=tw))):
            c.execute("INSERT INTO anniversary_reminders_sent VALUES (?, 2026, 0, ?)", (aid, int(when.timestamp() * 1000)))

    assert status.collect(base, now=now)["anniversary"]["pushed_today"] == 1


def test_the_whitelist_keeps_only_known_words():
    from jobs import family_extract_status as status

    kept = status._clean(
        {"mode": "unknown_mode", "stopped": "raced\n", "error": "OperationalError", "date": "２０２６-11-14"},
        status._PUSH_KEYS + ("stopped",),
    )

    assert kept == {"error": "OperationalError"}



@pytest.mark.parametrize("ran_at_hour,alert", [(7, True), (8.05, False)])
def test_a_job_that_left_no_mark_since_08_00_raises_not_run_today(base, ran_at_hour, alert):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from jobs import family_extract_status as status

    tw = ZoneInfo("Asia/Taipei")
    now = datetime(2026, 11, 14, 9, 40, tzinfo=tw).timestamp()  # the daily maintenance
    ran_at = datetime(2026, 11, 14, 0, 0, tzinfo=tw).timestamp() + ran_at_hour * 3600
    _cursor(base, "todo", created_at=ran_at - 60, updated_at=ran_at)

    assert ("todo_not_run_today" in status.collect(base, now=now)["alerts"]) is alert


# ── 2026-11-21 修正輪第 2 輪覆核（F1）：沒跑完、沒推，報告要看得到 ──────────────────────────


def _tw(day, hour, minute=0):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()


@pytest.mark.parametrize("finished_day,alert", [(13, True), (14, False)])
def test_not_run_today_needs_a_run_that_finished_not_a_cursor_that_moved(base, finished_day, alert):
    """F1 (c): a lock cut today's 08:00 run off after its first batch.  The batch touched the row at 08:00:30;
    the last run that got to its end is yesterday's."""
    from jobs import family_extract_status as status

    _cursor(base, "todo", created_at=_tw(14, 7, 59), updated_at=_tw(14, 8) + 30,
            last_run={"mode": "on", "finished_at": int(_tw(finished_day, 8))},
            last_push={"mode": "on", "date": "2026-11-14", "push_ok": True})

    assert ("todo_not_run_today" in status.collect(base, now=_tw(14, 9, 40))["alerts"]) is alert


@pytest.mark.parametrize("push_day,at,alert", [
    ("2026-11-13", (9, 40), True),  # the 09:40 report: today's push left no summary
    ("2026-11-14", (9, 40), False),
    ("2026-11-13", (8, 5), False),  # before 08:10 the 08:00 job may still be running
])
def test_a_push_with_no_summary_today_raises_push_not_today(base, push_day, at, alert):
    """F1 (a), (b): a lock during the push half, or while its summary is saved, leaves yesterday's summary."""
    from jobs import family_extract_status as status

    _cursor(base, "todo", created_at=_tw(14, 7, 59), updated_at=_tw(14, 8),
            last_run={"mode": "on", "finished_at": int(_tw(14, 8))},
            last_push={"mode": "on", "date": push_day, "push_ok": True})

    assert ("todo_push_not_today" in status.collect(base, now=_tw(14, *at))["alerts"]) is alert


def test_todo_switched_off_raises_no_push_alert_but_the_anniversary_push_still_does(base):
    """The anniversary push runs whatever the extraction switch says, like anniversary_push_failed."""
    from jobs import family_extract_status as status

    with open(base / ".env", "a", encoding="utf-8") as f:
        f.write("LINE_BOT_TODO_MODE=off\nLINE_BOT_ANNIVERSARY_EXTRACT=off\n")
    for name in ("todo", "anniversary"):
        _cursor(base, name, created_at=_tw(13, 7, 59), updated_at=_tw(13, 8),
                last_run={"mode": "off", "finished_at": int(_tw(13, 8))},
                last_push={"mode": "off", "date": "2026-11-13", "push_ok": True})

    alerts = status.collect(base, now=_tw(14, 9, 40))["alerts"]

    assert "todo_push_not_today" not in alerts and "anniversary_push_not_today" in alerts


def test_a_push_that_never_ran_raises_no_push_alert(base):
    """The deploy day: the extraction ran by hand and the first 08:00 push is still to come."""
    from jobs import family_extract_status as status

    _cursor(base, "todo", created_at=_tw(14, 7, 59), updated_at=_tw(14, 8),
            last_run={"mode": "on", "finished_at": int(_tw(14, 8))})

    assert "todo_push_not_today" not in status.collect(base, now=_tw(14, 9, 40))["alerts"]


def test_the_finish_time_passes_the_whitelist():
    from jobs import family_extract_status as status

    kept = status._clean({"finished_at": 1795221461, "read": 3}, status._RUN_KEYS)
    assert kept == {"finished_at": 1795221461, "read": 3}


def test_random_states_keep_the_08_10_rules():
    """Rounds 1 and 2 both found an important issue in ``alerts()``, so random states back the examples above
    (新版 §3.8): the two 08:10 rules, the 26-hour rule and the off switch, stated here without the code's own
    arithmetic.  A run's time is its finished_at, or updated_at for a row written before finished_at."""
    import random

    from jobs import family_extract_status as status

    rng = random.Random(20261121)
    day0 = _tw(14, 0)
    run_at = _tw(14, 8)
    for _ in range(4000):
        now = day0 + rng.uniform(0, 86400)
        jobs, modes, ref = {}, {}, {}
        for name in ("todo", "anniversary"):
            updated = now - rng.uniform(0, 3 * 86400)
            finished = rng.choice([None, int(now - rng.uniform(0, 3 * 86400))])
            push_date = rng.choice([None, "2026-11-12", "2026-11-13", "2026-11-14"])
            modes[name] = rng.choice(["on", "shadow", "off"] if name == "todo" else ["on", "off"])
            last_run = {"mode": modes[name], **({"finished_at": finished} if finished else {})}
            jobs[name] = {
                "ran": True, "last_run": last_run, "last_push": {"date": push_date} if push_date else {},
                "since_run_h": (now - updated) / 3600, "failures": 0, "no_answer_h": 0, "behind_h": 0,
            }
            ref[name] = (finished or updated, push_date)

        codes = set(status.alerts(jobs, {}, modes, now=now))

        after = now >= run_at + 600
        for name in ("todo", "anniversary"):
            ran_at, push_date = ref[name]
            on = modes[name] != "off"
            assert (f"{name}_not_run_today" in codes) == (on and after and ran_at < run_at - 360)
            assert (f"{name}_not_run_26h" in codes) == (on and now - ran_at > 26 * 3600)
            push_off = name == "todo" and not on  # the anniversary push runs whatever its switch says
            expect_push = after and push_date is not None and push_date != "2026-11-14" and not push_off
            assert (f"{name}_push_not_today" in codes) == expect_push
        if modes["todo"] == "off":
            assert not any(code.startswith("todo_") for code in codes)
