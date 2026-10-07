#!/usr/bin/env python3
"""Preview/apply exact generic reminder IDs using the retained complete source.

No network calls. A mode-0600 online SQLite backup is mandatory on apply.
The target keeps its schedule and complete source; only supplied obsolete IDs
are cancelled. Re-running an already reconciled plan is a no-op.
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memory  # noqa: E402
import reminder_restatement  # noqa: E402


def run(args):
    ids = [args.target, *args.cancel]
    if len(ids) != len(set(ids)):
        raise ValueError("IDs must be distinct")
    with memory._conn() as c:
        names = [r[1] for r in c.execute("PRAGMA table_info(reminders)")]
        rows = []
        for rid in ids:
            row = c.execute(
                "SELECT * FROM reminders WHERE reminder_id=?", (rid,)
            ).fetchone()
            if row is None:
                raise ValueError("missing reminder")
            rows.append(dict(zip(names, row)))
        target, *obsolete = rows
        if not target["source_text"] or target["status"] != "pending":
            raise ValueError("target needs a complete source and pending status")
        if any(
            (r["group_id"], r["user_id"]) != (target["group_id"], target["user_id"])
            for r in rows
        ):
            raise ValueError("group/sender mismatch")
        # Never silently leave an old queued acknowledgement for these sources.
        for row in rows:
            sources = c.execute(
                "SELECT message_id FROM raw_messages WHERE group_id=? AND user_id=? AND text=?",
                (row["group_id"], row["user_id"], row["source_text"]),
            ).fetchall()
            sources += c.execute(
                "SELECT 'pending_reminder:' || pending_id FROM pending_reminder_extract "
                "WHERE group_id=? AND user_id=? AND text=?",
                (row["group_id"], row["user_id"], row["source_text"]),
            ).fetchall()
            for source in sources:
                queued = c.execute(
                    "SELECT 1 FROM reminder_confirmation_outbox WHERE group_id=? "
                    "AND source_ref=? AND status IN ('pending','processing')",
                    (row["group_id"], source[0]),
                ).fetchone()
                if queued:
                    raise ValueError("associated confirmation is queued")
    action = target["source_text"]
    active_obsolete = [r for r in obsolete if r["status"] == "pending"]
    if any(
        r["status"] != "pending"
        and not (
            r["status"] == "cancelled"
            and r["source_kind"] == reminder_restatement.OLD_KIND
            and r["source_ref"] == str(args.target)
        )
        for r in obsolete
    ):
        raise ValueError("obsolete reminder has unexpected terminal state")
    if target["action"] == action and not active_obsolete:
        return {"status": "unchanged", "target": args.target, "cancelled": args.cancel}
    if not args.apply:
        return {
            "status": "preview",
            "target": args.target,
            "cancel": args.cancel,
            "action_from_complete_source": True,
            "schedule_unchanged": True,
        }
    if not args.backup:
        raise ValueError("apply requires --backup")
    backup = Path(args.backup)
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    source = sqlite3.connect(f"file:{memory._DB_PATH}?mode=ro", uri=True)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    status = reminder_restatement.reconcile(
        target,
        active_obsolete,
        action=action,
        remind_at=target["remind_at"],
        source_text=target["source_text"],
    )
    return {
        "status": status,
        "target": args.target,
        "cancelled": args.cancel,
        "backup": str(backup.resolve()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--cancel", type=int, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    result = run(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] not in {"preview", "updated", "unchanged"}:
        sys.exit(2)
