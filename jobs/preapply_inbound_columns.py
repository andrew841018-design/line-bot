"""Pre-apply the 2026-10-24 inbound_events change (LINE bot fixes, batch 1).

The new code reads two columns of ``inbound_events`` that the old table does not have:

    attempts INTEGER NOT NULL DEFAULT 1   -- how many times the event has been started
    owner    TEXT    NOT NULL DEFAULT ''  -- which bot process instance started it

Both have defaults, so the code before this change keeps working against the new table (its
INSERT/UPDATE statements never name them) and a rollback needs no database restore.  The bot
also tries the same ALTERs at start-up, but only logs when they fail; run this script first so
the columns are there before the new code starts.

How to run it
  * With the bot's own interpreter: ``$BOT_DIR/.venv/bin/python <this file> --db <line_bot.db>``.
    Never through ``env python3``: the first python3 on PATH is a broken x86 3.8 build here (the
    file has no shebang on purpose).
  * Run the DEPLOY COPY, ops/state/line_bot_fixes_20261023/preapply_inbound_columns.py in the
    Data_engineer workspace.  It is byte-identical to this file (a test compares them).  The
    pre-apply runs before the merge and the rollback after the revert; at both moments this
    file (jobs/preapply_inbound_columns.py) is not in the live tree.
  * It runs inside the restart lock, so it has a total deadline (``--deadline``, 20 s).  A writer
    that never lets go ends it with exit 1 instead of minutes of silence.  Abort the deploy
    before the merge on any non-zero exit.

Deploy order (inside the restart lock):
  1. WAL-safe backup  (ops/backup_sqlite_dumps.py, see the deploy notes)
  2. this script, ``--db <line_bot.db>``, then ``--check``   (both exit 0; idempotent)
  3. merge the code, restart

Rollback order (inside the same restart lock; ops/state/line_bot_fixes_20261023/ROLLBACK_B1.md):
  1. ``--rollback-fence``   -- the new code is still running and has been fencing replies
  2. revert the code (a revert commit, not a reset)
  3. ``launchctl kickstart -k``  -- the new-code process dies; the old code is not serving yet
  4. ``--rollback-fence`` AGAIN right after the kickstart returns: the new process may have
     fenced one more reply between steps 1 and 3.  Normally ``converted_rows=0``.
  5. only then wait for /health
The old code does not know the ``reply_sending`` status and would lease-retry such a row after
30 s, i.e. send a reply a second time.  The columns stay; the database is not restored.

Usage:
  preapply_inbound_columns.py --db PATH [--check | --rollback-fence] [--deadline SECONDS]

Exit codes: 0 done / already done, 1 failed (including the deadline), 2 bad arguments or
database missing, 3 ``--check`` found a column missing.  Output is counts and column names only:
no row content.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

COLUMNS = (
    ("attempts", "attempts INTEGER NOT NULL DEFAULT 1"),
    ("owner", "owner TEXT NOT NULL DEFAULT ''"),
)
LOCK_RETRIES = 8
# Total time budget of one run (read when main() runs, so a test can patch it).
DEADLINE_SECONDS = 20.0


class DeadlineExceeded(Exception):
    """The total time budget ran out -- another writer is holding the database."""


def _left(deadline: float) -> float:
    """Seconds until the deadline (a monotonic clock value); raises when there are none."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExceeded
    return left


def _arm(conn: sqlite3.Connection, deadline: float) -> None:
    """Never let one statement wait longer than what is left of the total budget."""
    conn.execute(f"PRAGMA busy_timeout={max(1, int(_left(deadline) * 1000))}")


def _connect(db: Path, deadline: float) -> sqlite3.Connection:
    # mode=rw: never creates a database by accident.
    conn = sqlite3.connect(
        f"{db.absolute().as_uri()}?mode=rw", uri=True, timeout=_left(deadline), isolation_level=None
    )
    _arm(conn, deadline)
    return conn


def _table_columns(conn: sqlite3.Connection, deadline: float) -> set[str] | None:
    """Column names of inbound_events, or None when the table does not exist."""
    _arm(conn, deadline)
    rows = conn.execute("PRAGMA table_info(inbound_events)").fetchall()
    return {row[1] for row in rows} if rows else None


def _add_column(conn: sqlite3.Connection, name: str, ddl: str, deadline: float) -> bool:
    """True when this call added the column.  Tolerates a concurrent ALTER and a locked file."""
    for attempt in range(LOCK_RETRIES):
        _arm(conn, deadline)
        try:
            conn.execute(f"ALTER TABLE inbound_events ADD COLUMN {ddl}")
            return True
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "duplicate column" in message:
                return False
            if "database is locked" in message and attempt < LOCK_RETRIES - 1:
                time.sleep(min(0.25 * (attempt + 1), _left(deadline)))
                continue
            raise
    return False


def apply(db: Path, *, check_only: bool = False, deadline: float | None = None) -> int:
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_SECONDS
    conn = _connect(db, deadline)
    try:
        have = _table_columns(conn, deadline)
        if have is None:
            print("inbound_events table missing: nothing to apply (the new code creates it with the columns)")
            return 0
        missing = [(name, ddl) for name, ddl in COLUMNS if name not in have]
        if check_only:
            print(f"check present={sorted(have & {n for n, _ in COLUMNS})} missing={[n for n, _ in missing]}")
            return 3 if missing else 0
        added = [name for name, ddl in missing if _add_column(conn, name, ddl, deadline)]
        have_after = _table_columns(conn, deadline) or set()
        absent = [name for name, _ in COLUMNS if name not in have_after]
        if absent:
            print(f"FAILED columns still missing: {absent}")
            return 1
        _arm(conn, deadline)
        rows = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]
        print(f"preapply ok added={added} already_present={[n for n, _ in COLUMNS if n not in added]} rows={rows}")
        return 0
    finally:
        conn.close()


def rollback_fence(db: Path, *, deadline: float | None = None) -> int:
    """reply_sending -> completed_no_reply: the old code understands the latter (terminal)."""
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_SECONDS
    conn = _connect(db, deadline)
    try:
        if _table_columns(conn, deadline) is None:
            print("inbound_events table missing: nothing to convert")
            return 0
        _arm(conn, deadline)
        before = conn.total_changes
        conn.execute("UPDATE inbound_events SET status = 'completed_no_reply' WHERE status = 'reply_sending'")
        print(f"rollback-fence converted_rows={conn.total_changes - before}")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="path of line_bot.db")
    parser.add_argument(
        "--deadline", type=float, default=None, help=f"total seconds this run may take (default {DEADLINE_SECONDS:g})"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report only; exit 3 if a column is missing")
    mode.add_argument(
        "--rollback-fence",
        action="store_true",
        help="convert reply_sending rows; run it before the revert AND again right after the restart",
    )
    args = parser.parse_args(argv)
    db = Path(args.db)
    if not db.is_file():
        print("database file not found")
        return 2
    budget = args.deadline if args.deadline is not None else DEADLINE_SECONDS
    deadline = time.monotonic() + budget
    try:
        if args.rollback_fence:
            return rollback_fence(db, deadline=deadline)
        return apply(db, check_only=args.check, deadline=deadline)
    except DeadlineExceeded:
        print(f"FAILED deadline of {budget:g} s exceeded: another writer is holding the database")
        return 1
    except sqlite3.Error as exc:
        print(f"FAILED error_type={type(exc).__name__}")
        return 1
    except Exception as exc:  # any other failure must still be a non-zero exit with no row content
        print(f"FAILED error_type={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
