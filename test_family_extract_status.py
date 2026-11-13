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
             json.dumps(last_run or {"mode": "on"}), json.dumps(last_push or {})),
        )


def test_numbers_only_and_no_alerts_on_a_healthy_day(base):
    from jobs import family_extract_status as status

    now = time.time()
    with _db(base) as c:
        c.execute("INSERT INTO raw_messages VALUES (?, 'm1', 'user-a', '家人的話', ?)", (GROUP, int(now - 600)))
        c.execute("INSERT INTO todos VALUES ('t1', 'pending', '2099-01-01', NULL, '交報告')")
        c.execute("INSERT INTO anniversary_candidates VALUES ('c1', 'rejected', '成員甲')")
    _cursor(base, "todo", created_at=now - 600, updated_at=now - 3600,
            last_push={"mode": "on", "push_ok": True, "fold_error": 0})
    _cursor(base, "anniversary", created_at=now - 600, updated_at=now - 3600)

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
        "todo_no_answer_36h", "todo_bad_answers", "todo_gap", "todo_behind_48h", "todo_db_busy",
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
