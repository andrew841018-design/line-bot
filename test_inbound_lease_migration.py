"""Inbound-event lease, attempts cap, process-instance fast path and the schema migration
(LINE bot fixes batch 1, 2026-10-24).  Synthetic ids only.

What is pinned here:

* the lease is 150 s (media 300 s) so LINE's ~60 s redeliveries no longer re-run the models;
  an event is started at most 3 times, then closed (completed_no_reply);
* a row that a *different* process instance started (a restart orphaned it) is retried at once;
* ``attempts`` / ``owner`` have defaults, readers fall back to the old statements when the
  columns are missing, a failing ALTER never stops the import, and the code before this change
  can still write to the new schema (rollback needs no database restore);
* the deploy script ``jobs/preapply_inbound_columns.py`` is idempotent and has a deadline.
"""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
import time
from pathlib import Path

import pytest

import memory

GROUP = "GRP-LEASE"

HERE = Path(__file__).resolve().parent
PREAPPLY_SCRIPT = HERE / "jobs" / "preapply_inbound_columns.py"
DEPLOY_COPY_DIR = "ops/state/line_bot_fixes_20261023"  # the deploy copy lives outside the repo

LEGACY_TABLE = (
    "CREATE TABLE inbound_events (group_id TEXT NOT NULL, message_id TEXT NOT NULL, "
    "status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
    "PRIMARY KEY (group_id, message_id))"
)

# The statements the code before this change ran, verbatim.
OLD_SELECT = "SELECT status, updated_at FROM inbound_events WHERE group_id = ? AND message_id = ?"
OLD_INSERT = (
    "INSERT INTO inbound_events (group_id, message_id, status, created_at, updated_at) "
    "VALUES (?, ?, 'processing', ?, ?)"
)
OLD_RETRY = (
    "UPDATE inbound_events SET status = 'processing', updated_at = ? "
    "WHERE group_id = ? AND message_id = ?"
)
OLD_REPLIED = (
    "UPDATE inbound_events SET status = 'replied', updated_at = ? "
    "WHERE group_id = ? AND message_id = ?"
)


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": 1_800_000_000.0}
    monkeypatch.setattr(memory._time, "time", lambda: state["t"])
    return state


def _row(message_id: str, group: str = GROUP):
    with memory._lock, memory._conn() as c:
        return c.execute(
            "SELECT status, attempts, owner, updated_at FROM inbound_events "
            "WHERE group_id = ? AND message_id = ?",
            (group, message_id),
        ).fetchone()


def _columns() -> set[str]:
    with memory._lock, memory._conn() as c:
        return {r[1] for r in c.execute("PRAGMA table_info(inbound_events)").fetchall()}


def _become_instance(monkeypatch, name: str) -> None:
    monkeypatch.setattr(memory, "_INSTANCE_ID", name)


@pytest.fixture()
def legacy_db(monkeypatch, tmp_path):
    """A database whose inbound_events still has the old shape, and the columns cannot be
    added (the ALTER keeps failing): what the bot would see if the pre-apply was skipped and
    the start-up ALTER lost to a lock."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_TABLE)
    conn.commit()
    conn.close()
    monkeypatch.setattr(memory, "_DB_PATH", path)

    real_add_column = memory._add_column

    def refuse(c, table, column, ddl):
        if table == "inbound_events":
            raise sqlite3.OperationalError("database is locked")
        return real_add_column(c, table, column, ddl)  # the other tables migrate as usual

    monkeypatch.setattr(memory, "_add_column", refuse)
    memory._init_db()  # must not raise
    return path


# ── lease and attempts ────────────────────────────────────────────────────────────────────


def test_lease_constants():
    assert memory._INBOUND_PROCESSING_LEASE_SECONDS == 150
    assert memory._INBOUND_MEDIA_PROCESSING_LEASE_SECONDS == 300
    assert memory._INBOUND_MEDIA_PROCESSING_LEASE_SECONDS > memory._INBOUND_PROCESSING_LEASE_SECONDS
    assert memory._INBOUND_MAX_ATTEMPTS == 3
    assert memory._INBOUND_EVENT_RETENTION_SECONDS == 14 * 86400


def test_a_new_row_records_the_first_attempt_and_its_instance(monkeypatch, clock):
    _become_instance(monkeypatch, "inst-A")
    assert memory.begin_inbound_event(GROUP, "m1") == "new"

    status, attempts, owner, _updated = _row("m1")
    assert (status, attempts, owner) == ("processing", 1, "inst-A")


def test_redelivery_inside_the_lease_is_skipped_and_the_loop_is_capped(clock):
    """10/15: one message was re-run 15 times.  Now: the ~62 s redelivery is skipped, the
    third start is the last, the fourth is closed."""
    assert memory.begin_inbound_event(GROUP, "loop") == "new"
    clock["t"] += 62
    assert memory.begin_inbound_event(GROUP, "loop") == "processing"
    clock["t"] += 62
    assert memory.begin_inbound_event(GROUP, "loop") == "processing"  # 124 s: still inside
    clock["t"] += 100  # 224 s after the claim
    assert memory.begin_inbound_event(GROUP, "loop") == "retry"
    assert _row("loop")[1] == 2
    clock["t"] += 160
    assert memory.begin_inbound_event(GROUP, "loop") == "retry"
    assert _row("loop")[1] == 3
    clock["t"] += 160
    assert memory.begin_inbound_event(GROUP, "loop") == "completed_no_reply"
    assert _row("loop")[0] == "completed_no_reply"
    clock["t"] += 160
    assert memory.begin_inbound_event(GROUP, "loop") == "completed_no_reply"


def test_a_retry_restarts_the_lease(clock):
    memory.begin_inbound_event(GROUP, "m")
    clock["t"] += 151
    assert memory.begin_inbound_event(GROUP, "m") == "retry"
    clock["t"] += 100
    assert memory.begin_inbound_event(GROUP, "m") == "processing"


def test_media_rows_keep_their_longer_lease(clock):
    memory.begin_inbound_event(GROUP, "img")
    memory.mark_inbound_event_media_processing(GROUP, "img")
    clock["t"] += memory._INBOUND_PROCESSING_LEASE_SECONDS + 1
    assert memory.begin_inbound_event(GROUP, "img") == "processing"
    clock["t"] += memory._INBOUND_MEDIA_PROCESSING_LEASE_SECONDS
    assert memory.begin_inbound_event(GROUP, "img") == "retry"


def test_terminal_states_stay_terminal(clock):
    memory.begin_inbound_event(GROUP, "done")
    memory.mark_inbound_event_replied(GROUP, "done")
    memory.begin_inbound_event(GROUP, "quiet")
    memory.mark_inbound_events_completed_no_reply(GROUP, ["quiet"])
    clock["t"] += 100000
    assert memory.begin_inbound_event(GROUP, "done") == "replied"
    assert memory.begin_inbound_event(GROUP, "quiet") == "completed_no_reply"


# ── restart orphans ───────────────────────────────────────────────────────────────────────


def test_a_row_started_by_another_instance_is_retried_at_once(monkeypatch, clock):
    _become_instance(monkeypatch, "inst-old")
    assert memory.begin_inbound_event(GROUP, "orphan") == "new"
    clock["t"] += 5  # the restart happened seconds ago, far inside the lease

    _become_instance(monkeypatch, "inst-new")
    assert memory.begin_inbound_event(GROUP, "orphan") == "retry"
    assert _row("orphan")[1:3] == (2, "inst-new")
    # The new instance owns it now: the lease protects it again.
    clock["t"] += 5
    assert memory.begin_inbound_event(GROUP, "orphan") == "processing"


def test_a_media_row_of_another_instance_is_retried_at_once(monkeypatch, clock):
    _become_instance(monkeypatch, "inst-old")
    memory.begin_inbound_event(GROUP, "img")
    memory.mark_inbound_event_media_processing(GROUP, "img")
    clock["t"] += 5

    _become_instance(monkeypatch, "inst-new")
    assert memory.begin_inbound_event(GROUP, "img") == "retry"


def test_the_attempts_cap_also_stops_orphan_retries(monkeypatch, clock):
    _become_instance(monkeypatch, "inst-1")
    memory.begin_inbound_event(GROUP, "poison")
    for name in ("inst-2", "inst-3"):
        _become_instance(monkeypatch, name)
        assert memory.begin_inbound_event(GROUP, "poison") == "retry"
    _become_instance(monkeypatch, "inst-4")
    assert memory.begin_inbound_event(GROUP, "poison") == "completed_no_reply"


def test_a_fenced_row_is_never_retried_not_even_by_a_new_instance(monkeypatch, clock):
    _become_instance(monkeypatch, "inst-old")
    memory.begin_inbound_event(GROUP, "sent")
    assert memory.claim_inbound_reply(GROUP, ["sent"])[0] == "claimed"

    _become_instance(monkeypatch, "inst-new")
    assert memory.begin_inbound_event(GROUP, "sent") == "reply_sending"
    clock["t"] += 100000
    assert memory.begin_inbound_event(GROUP, "sent") == "reply_sending"


def test_rows_written_by_the_code_before_the_change_count_as_orphans(monkeypatch, clock):
    """Right after the deploy restart every row of the old process has owner '' -- its
    handler died with the old process, so retrying at once is what we want."""
    now = int(clock["t"])
    with memory._lock, memory._conn() as c:
        c.execute(OLD_INSERT, (GROUP, "legacy-row", now, now))
    assert _row("legacy-row")[1:3] == (1, "")

    clock["t"] += 3
    assert memory.begin_inbound_event(GROUP, "legacy-row") == "retry"
    assert _row("legacy-row")[1:3] == (2, memory._INSTANCE_ID)


def test_the_instance_id_is_per_process_start():
    assert isinstance(memory._INSTANCE_ID, str) and memory._INSTANCE_ID
    assert memory._INSTANCE_ID.split("-")[0] == str(__import__("os").getpid())


# ── the fence status in the other writers ─────────────────────────────────────────────────


def test_media_processing_never_overwrites_a_fence_or_a_reply(clock):
    memory.begin_inbound_event(GROUP, "a")
    memory.claim_inbound_reply(GROUP, ["a"])
    memory.mark_inbound_event_media_processing(GROUP, "a")
    assert _row("a")[0] == "reply_sending"

    memory.begin_inbound_event(GROUP, "b")
    memory.mark_inbound_event_replied(GROUP, "b")
    memory.mark_inbound_event_media_processing(GROUP, "b")
    assert _row("b")[0] == "replied"


# ── migration safety ──────────────────────────────────────────────────────────────────────


def test_init_db_adds_the_columns_to_an_old_table_and_keeps_its_rows(monkeypatch, tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_TABLE)
    conn.execute("INSERT INTO inbound_events VALUES ('G', 'keep-1', 'processing', 100, 200)")
    conn.execute("INSERT INTO inbound_events VALUES ('G', 'keep-2', 'replied', 100, 200)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(memory, "_DB_PATH", path)

    memory._init_db()
    memory._init_db()  # idempotent

    assert {"attempts", "owner"} <= _columns()
    with memory._lock, memory._conn() as c:
        rows = c.execute(
            "SELECT message_id, status, attempts, owner FROM inbound_events ORDER BY message_id"
        ).fetchall()
    assert rows == [("keep-1", "processing", 1, ""), ("keep-2", "replied", 1, "")]


def test_a_failing_alter_does_not_stop_the_import(legacy_db, caplog):
    assert "attempts" not in _columns() and "owner" not in _columns()
    caplog.set_level(logging.WARNING)
    memory._init_db()  # again, still failing: still silent apart from the log
    assert any("inbound_events columns not added" in r.getMessage() for r in caplog.records)
    # The reason is logged as a type name only.
    assert all("locked" not in r.getMessage() for r in caplog.records)


def test_legacy_shape_lease_still_works_without_the_new_columns(legacy_db, monkeypatch, clock):
    assert memory.begin_inbound_event(GROUP, "m1") == "new"
    assert "attempts" not in _columns()
    clock["t"] += 62
    assert memory.begin_inbound_event(GROUP, "m1") == "processing"
    clock["t"] += 100
    assert memory.begin_inbound_event(GROUP, "m1") == "retry"
    with memory._lock, memory._conn() as c:
        assert c.execute(OLD_SELECT, (GROUP, "m1")).fetchone()[0] == "processing"


def test_legacy_shape_has_neither_the_cap_nor_the_orphan_shortcut(legacy_db, monkeypatch, clock):
    memory.begin_inbound_event(GROUP, "m")
    _become_instance(monkeypatch, "someone-else")
    clock["t"] += 5
    assert memory.begin_inbound_event(GROUP, "m") == "processing"  # nobody knows who started it
    for _ in range(6):
        clock["t"] += 151
        assert memory.begin_inbound_event(GROUP, "m") == "retry"  # no cap to count against


def test_legacy_shape_fence_works(legacy_db, clock):
    memory.begin_inbound_event(GROUP, "m")
    assert memory.claim_inbound_reply(GROUP, ["m"]) == ("claimed", False)
    assert memory.begin_inbound_event(GROUP, "m") == "reply_sending"
    assert memory.claim_inbound_reply(GROUP, ["m"]) == ("blocked", False)
    assert memory.release_inbound_reply(GROUP, ["m"]) == 1
    assert memory.get_inbound_event_status(GROUP, "m") == "processing"
    assert memory.claim_inbound_reply(GROUP, ["m"]) == ("claimed", False)


def test_the_code_before_the_change_still_works_on_the_new_schema(clock):
    """Rollback: the old statements never name the new columns, whose defaults apply."""
    now = int(clock["t"])
    with memory._lock, memory._conn() as c:
        c.execute(OLD_INSERT, (GROUP, "old-insert", now, now))
        assert c.execute(OLD_SELECT, (GROUP, "old-insert")).fetchone() == ("processing", now)
        c.execute(OLD_RETRY, (now + 40, GROUP, "old-insert"))
        c.execute(OLD_REPLIED, (now + 50, GROUP, "old-insert"))
    assert _row("old-insert")[:3] == ("replied", 1, "")
    # ...and the new reader reads what the old writer wrote.
    assert memory.begin_inbound_event(GROUP, "old-insert") == "replied"


# ── the deploy script ─────────────────────────────────────────────────────────────────────


def _load_preapply():
    assert PREAPPLY_SCRIPT.exists(), "the pre-apply script is part of the repository"
    spec = importlib.util.spec_from_file_location("preapply_inbound_columns", PREAPPLY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_file(path: Path, rows=(("secret-message-id", "reply_sending"), ("plain-id", "processing"))):
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_TABLE)
    for message_id, status in rows:
        conn.execute("INSERT INTO inbound_events VALUES ('G', ?, ?, 100, 200)", (message_id, status))
    conn.commit()
    conn.close()


def _file_columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(inbound_events)").fetchall()}
    finally:
        conn.close()


def test_preapply_adds_both_columns_keeps_the_rows_and_is_idempotent(tmp_path, capsys):
    script = _load_preapply()
    db = tmp_path / "prod.db"
    _legacy_file(db)

    assert script.main(["--db", str(db)]) == 0
    first = capsys.readouterr().out
    assert {"attempts", "owner"} <= _file_columns(db)
    assert "added=['attempts', 'owner']" in first and "rows=2" in first

    assert script.main(["--db", str(db)]) == 0
    second = capsys.readouterr().out
    assert "added=[]" in second

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT message_id, status, attempts, owner FROM inbound_events ORDER BY message_id").fetchall()
    conn.close()
    assert rows == [("plain-id", "processing", 1, ""), ("secret-message-id", "reply_sending", 1, "")]
    assert "secret-message-id" not in first + second  # counts and column names only


def test_preapply_check_mode_reports_without_writing(tmp_path, capsys):
    script = _load_preapply()
    db = tmp_path / "prod.db"
    _legacy_file(db)

    assert script.main(["--db", str(db), "--check"]) == 3
    assert "attempts" not in _file_columns(db)
    assert script.main(["--db", str(db)]) == 0
    capsys.readouterr()
    assert script.main(["--db", str(db), "--check"]) == 0


def test_preapply_refuses_a_missing_database_and_never_creates_one(tmp_path, capsys):
    script = _load_preapply()
    db = tmp_path / "nope.db"

    assert script.main(["--db", str(db)]) == 2
    assert not db.exists()


def test_preapply_leaves_a_database_without_the_table_alone(tmp_path, capsys):
    script = _load_preapply()
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()

    assert script.main(["--db", str(db)]) == 0
    assert _file_columns(db) == set()


def test_preapply_works_on_a_wal_database_another_connection_holds_open(tmp_path, capsys):
    script = _load_preapply()
    db = tmp_path / "wal.db"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute(LEGACY_TABLE)
    holder.execute("INSERT INTO inbound_events VALUES ('G', 'live', 'processing', 100, 200)")
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("SELECT * FROM inbound_events").fetchall()
    try:
        assert script.main(["--db", str(db)]) == 0
        assert reader.execute("SELECT attempts, owner FROM inbound_events").fetchall() == [(1, "")]
    finally:
        reader.close()
        holder.close()


def test_preapply_rollback_fence_converts_only_the_fenced_rows(tmp_path, capsys):
    script = _load_preapply()
    db = tmp_path / "prod.db"
    _legacy_file(
        db,
        rows=(("a", "reply_sending"), ("b", "reply_sending"), ("c", "processing"), ("d", "replied")),
    )

    assert script.main(["--db", str(db), "--rollback-fence"]) == 0
    assert "converted_rows=2" in capsys.readouterr().out
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT message_id, status FROM inbound_events").fetchall())
    conn.close()
    assert rows == {"a": "completed_no_reply", "b": "completed_no_reply", "c": "processing", "d": "replied"}
    # Nothing left to convert the second time.
    assert script.main(["--db", str(db), "--rollback-fence"]) == 0
    assert "converted_rows=0" in capsys.readouterr().out


def test_the_new_code_reads_a_database_the_script_prepared(tmp_path, monkeypatch, capsys):
    script = _load_preapply()
    db = tmp_path / "prod.db"
    _legacy_file(db, rows=())
    assert script.main(["--db", str(db)]) == 0
    monkeypatch.setattr(memory, "_DB_PATH", db)

    real_add_column = memory._add_column

    def must_not_alter(c, table, column, ddl):
        assert table != "inbound_events", "the columns are already there"
        return real_add_column(c, table, column, ddl)

    monkeypatch.setattr(memory, "_add_column", must_not_alter)
    memory._init_db()
    assert memory.begin_inbound_event(GROUP, "m") == "new"
    assert _row("m")[1:3] == (1, memory._INSTANCE_ID)


def test_preapply_gives_up_at_its_deadline_when_a_writer_never_lets_go(tmp_path, capsys):
    """It runs inside the restart lock: a stuck writer must end it with exit 1 in seconds, not
    after minutes of silence, so the deploy aborts before the merge."""
    script = _load_preapply()
    db = tmp_path / "locked.db"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute(LEGACY_TABLE)
    holder.execute("BEGIN IMMEDIATE")  # the write lock, never released during the run
    try:
        for argv in (["--db", str(db), "--deadline", "1"], ["--db", str(db), "--deadline", "1", "--rollback-fence"]):
            started = time.monotonic()
            assert script.main(argv) == 1
            assert time.monotonic() - started < 4
            assert capsys.readouterr().out.startswith("FAILED")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "attempts" not in _file_columns(db)  # nothing half-applied


def test_preapply_deadline_defaults_to_about_twenty_seconds():
    script = _load_preapply()

    assert 15 <= script.DEADLINE_SECONDS <= 30


def test_preapply_turns_any_unexpected_failure_into_a_nonzero_exit(tmp_path, monkeypatch, capsys):
    script = _load_preapply()
    db = tmp_path / "prod.db"
    _legacy_file(db)

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(script, "_connect", boom)

    assert script.main(["--db", str(db)]) == 1
    assert capsys.readouterr().out.strip() == "FAILED error_type=OSError"


def test_the_deploy_copy_is_byte_identical_to_the_repository_file():
    """The deploy copy outside the repository is what runs before the merge and after a revert."""
    for parent in HERE.parents:
        copy = parent / DEPLOY_COPY_DIR / "preapply_inbound_columns.py"
        if copy.exists():
            assert copy.read_bytes() == PREAPPLY_SCRIPT.read_bytes()
            return
    pytest.skip("no deploy copy next to this checkout")
