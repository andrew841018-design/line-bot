"""B7.2 (2026-10-24): the morning restart waits for messages in flight before it kickstarts.

`wait_for_inflight_messages` in morning_restart.sh is driven here against throwaway sqlite
files (never the real database).  Everything the function does must fail open: the restart
is never held back by a missing or broken database.  Since 2026-10-29 "could not tell" is
rc 2 with the reason in the log (an error type, never row data), and the count runs on the
bot's own Python instead of macOS's /usr/bin/sqlite3.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "morning_restart.sh"
# morning_restart.sh sources this file from its hard-coded BOT_DIR before the source-only guard.
RESTART_LOCK_LIB = Path("/Users/andrew/Desktop/andrew/Data_engineer/line_bot/restart_lock.sh")
# ...and counts with the bot's own Python from the same BOT_DIR.
BOT_PY = Path("/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/bin/python")
STAGED_LIVE = Path(
    "/Users/andrew/Desktop/andrew/Data_engineer/ops/state/line_bot_fixes_20261023/"
    "scripts_stage/morning_restart_line_bot.sh"
)
# What launchd runs at 08:05.  It has no source-only guard: read it, never source it.
LIVE_SCRIPT = Path("/Users/andrew/scripts/morning_restart_line_bot.sh")

pytestmark = pytest.mark.skipif(
    not RESTART_LOCK_LIB.exists() or not BOT_PY.exists(),
    reason="needs the production restart_lock.sh and the bot's venv Python",
)


def _make_db(path: Path, rows: list[tuple[str, str, int]], *, wal: bool = False) -> sqlite3.Connection:
    """rows: (message_id, status, age_seconds)."""
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE inbound_events (group_id TEXT NOT NULL, message_id TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "PRIMARY KEY (group_id, message_id))"
    )
    now = int(time.time())
    for message_id, status, age in rows:
        conn.execute(
            "INSERT INTO inbound_events VALUES ('G', ?, ?, ?, ?)",
            (message_id, status, now - age, now - age),
        )
    return conn


def _env(db: Path) -> dict[str, str]:
    """The script's `say` appends to $HOME/Library/Logs; keep test runs out of the real morning log."""
    (db.parent / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"LINE_BOT_DB": str(db), "MORNING_RESTART_SOURCE_ONLY": "1", "HOME": str(db.parent)})
    return env


def _log(db: Path) -> str:
    path = db.parent / "Library" / "Logs" / "line_bot_morning_restart.log"
    return path.read_text() if path.exists() else ""


def _wait(db: Path, max_wait: int = 2, recent: int = 90, *, timeout: int = 30):
    env = _env(db)
    started = time.monotonic()
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'source "{SCRIPT}"; wait_for_inflight_messages {max_wait} {recent}; echo "rc=$?"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rc = int(result.stdout.strip().splitlines()[-1].split("=")[1])
    return rc, time.monotonic() - started


def test_stale_rows_do_not_hold_the_restart(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, [("old", "processing", 100000), ("older", "media_processing", 5000)]).close()

    rc, elapsed = _wait(db, max_wait=10)

    assert rc == 0
    assert elapsed < 8  # it did not sit out the 10 s (a loaded machine only slows the start-up)


def test_closed_rows_do_not_hold_the_restart(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, [("a", "replied", 2), ("b", "completed_no_reply", 2)]).close()

    assert _wait(db)[0] == 0


def test_a_fresh_processing_row_that_never_ends_costs_the_whole_wait_then_proceeds(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, [("busy", "processing", 5)]).close()

    rc, elapsed = _wait(db, max_wait=2)

    assert rc == 1  # still in flight; the caller restarts anyway
    # The deadline counts whole seconds ($SECONDS), so a 2 s wait lasts between 1 and 2 s.
    assert 0.9 <= elapsed < 20


def test_a_fresh_reply_sending_row_counts_as_in_flight(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, [("sending", "reply_sending", 1)]).close()

    assert _wait(db, max_wait=1)[0] == 1


def test_a_media_processing_row_counts_as_in_flight(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, [("img", "media_processing", 10)]).close()

    assert _wait(db, max_wait=1)[0] == 1


def test_the_wait_ends_as_soon_as_the_message_is_answered(tmp_path):
    db = tmp_path / "a.db"
    conn = _make_db(db, [("busy", "processing", 5)])

    def answer():
        time.sleep(1.3)
        conn.execute("UPDATE inbound_events SET status = 'replied' WHERE message_id = 'busy'")

    worker = threading.Thread(target=answer)
    worker.start()
    try:
        rc, elapsed = _wait(db, max_wait=10)
    finally:
        worker.join()
        conn.close()

    assert rc == 0
    assert 1.0 <= elapsed < 9


def test_it_reads_a_wal_database_that_another_connection_holds_open(tmp_path):
    db = tmp_path / "a.db"
    conn = _make_db(db, [("busy", "processing", 5)], wal=True)  # row lives in the -wal file

    try:
        assert _wait(db, max_wait=1)[0] == 1
    finally:
        conn.close()


def test_it_reads_a_wal_database_that_no_connection_holds_open(tmp_path):
    # 2026-10-29: the live database's usual state.  With no connection open there are no -wal/-shm
    # files, and macOS's /usr/bin/sqlite3 -readonly could not open it, so the old check failed open
    # and the restart never waited.
    db = tmp_path / "a.db"
    conn = _make_db(db, [("busy", "processing", 5)], wal=True)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for side in ("-wal", "-shm"):
        Path(f"{db}{side}").unlink(missing_ok=True)

    assert _wait(db, max_wait=1)[0] == 1


def test_a_check_that_fails_logs_why_without_row_data(tmp_path):
    db = tmp_path / "a.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE inbound_events (group_id TEXT, message_id TEXT, status TEXT)")
    conn.execute("INSERT INTO inbound_events VALUES ('G-private', 'm-private', 'processing')")
    conn.commit()
    conn.close()

    rc, _ = _wait(db, max_wait=1)

    log = _log(db)
    assert rc == 2
    assert "inflight check failed: ERR OperationalError" in log
    assert "private" not in log and "updated_at" not in log  # no row data, no error text


def test_a_check_that_hangs_is_cut_off(tmp_path):
    db = tmp_path / "a.db"
    os.mkfifo(db)  # opening it blocks until a writer shows up, like a hung disk

    rc, elapsed = _wait(db, max_wait=10)

    assert rc == 2
    assert elapsed < 9
    assert "inflight check failed: ERR Timeout" in _log(db)


def test_it_never_writes_to_the_database(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, [("busy", "processing", 5)]).close()
    before = db.read_bytes()

    _wait(db, max_wait=1)

    assert db.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "garbage", "empty", "no_table"])
def test_every_kind_of_broken_database_fails_open_at_once(tmp_path, kind):
    db = tmp_path / "a.db"
    if kind == "garbage":
        db.write_bytes(b"this is not a database" * 50)
    elif kind == "empty":
        db.write_bytes(b"")
    elif kind == "no_table":
        sqlite3.connect(db).close()

    rc, elapsed = _wait(db, max_wait=10)

    assert rc == 2
    assert elapsed < 8
    assert "inflight check " in _log(db)


def test_garbage_arguments_fail_open(tmp_path):
    db = tmp_path / "a.db"
    _make_db(db, [("busy", "processing", 5)]).close()
    env = _env(db)

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'source "{SCRIPT}"; wait_for_inflight_messages "x;rm -rf /" 90; echo "rc=$?"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.stdout.strip().endswith("rc=2")
    assert "inflight check skipped: bad arguments" in _log(db)


def _function_text(path: Path) -> str:
    text = path.read_text()
    start = text.index("wait_for_inflight_messages() {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _first_index(text: str, needle: str) -> int:
    index = text.index(needle)
    assert index >= 0
    return index


@pytest.mark.parametrize(
    "script", [SCRIPT, STAGED_LIVE, LIVE_SCRIPT], ids=["repo", "staged-live", "live"]
)
def test_the_wait_runs_after_the_lock_and_before_the_kickstart(script):
    if not script.exists():
        pytest.skip(f"{script} not present")
    text = script.read_text()
    body = text[text.index('say "morning_restart 開始"'):]

    lock = _first_index(body, "acquire_restart_lock")
    service = _first_index(body, "ensure_uvicorn_service")
    wait = _first_index(body, "wait_for_inflight_messages 25 90")
    kick = _first_index(body, "launchctl kickstart -k")
    assert lock < service < wait < kick
    # The restart never depends on the wait's exit code.
    assert "inflight_rc=$?" in body


@pytest.mark.parametrize("copy", [STAGED_LIVE, LIVE_SCRIPT], ids=["staged-live", "live"])
def test_both_copies_carry_the_same_function(copy):
    if not copy.exists():
        pytest.skip(f"{copy} not present")
    assert _function_text(SCRIPT) == _function_text(copy)


def test_the_function_only_reads(tmp_path):
    text = _function_text(SCRIPT)
    assert "mode=ro" in text
    assert "/usr/bin/sqlite3" not in text  # macOS's CLI cannot open the live WAL file read-only
    for forbidden in ("UPDATE", "DELETE", "INSERT", "launchctl", "kill "):
        assert forbidden not in text
