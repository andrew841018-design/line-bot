#!/usr/bin/env python3
"""Dry-run or apply one exact contextual date-reminder pending row.

This command never calls LINE or Discord.  Apply mode creates a private SQLite
online backup before claiming and atomically reconciling the four reminder
slots through the same production primitive used by the webhook handler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import memory  # noqa: E402


def _short_hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]


def _load_plan(pending_id: int) -> tuple[dict, dict, dict | None]:
    row = memory.get_pending_reminder_extract(pending_id)
    if row is None:
        raise RuntimeError("pending row not found")
    if row["status"] not in {"pending", "done"}:
        raise RuntimeError(f"pending row is not repairable: {row['status']}")
    plan = main._contextual_date_reminder_plan(
        row["text"],
        row["group_id"],
        row["user_id"],
        row["message_id"],
    )
    if plan is None:
        raise RuntimeError("pending row is not a safe contextual reminder command")
    legacy = memory.get_contextual_legacy_reminder(
        row["group_id"],
        row["user_id"],
        plan["source_text"],
    )
    if legacy is not None:
        plan = dict(plan)
        plan["legacy_expected"] = legacy
    return row, plan, legacy


def _backup_database(destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("backup destination already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, 0o600)
    os.close(fd)
    os.chmod(destination, 0o600)
    source = sqlite3.connect(memory._DB_PATH)
    target = sqlite3.connect(destination)
    complete = False
    try:
        source.backup(target)
        target.commit()
        complete = True
    finally:
        target.close()
        source.close()
        if not complete:
            destination.unlink(missing_ok=True)
    os.chmod(destination, 0o600)


def _require_private_backup(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("backup must be a regular mode-0600 file")
    return resolved


def _write_manifest(
    backup: Path,
    *,
    row: dict,
    plan: dict,
    reminder_ids: list[int],
    legacy_reminder_id: int | None,
) -> Path:
    path = backup.with_suffix(backup.suffix + ".manifest.json")
    payload = {
        "version": 1,
        "pending_id": int(row["pending_id"]),
        "legacy_reminder_id": legacy_reminder_id,
        "created_reminder_ids": sorted(int(value) for value in reminder_ids),
        "command_hash": _short_hash(row["text"]),
        "source_hash": _short_hash(plan["source_text"]),
        "source_refs": sorted(str(spec["source_ref"]) for spec in plan["reminders"]),
    }
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


_REMINDER_COLUMNS = (
    "reminder_id", "group_id", "user_id", "action", "remind_at", "created_at",
    "status", "source_kind", "source_ref", "source_text", "mention_aliases",
    "last_pushed_at", "weekly_count", "last_weekly_at", "pushed_3d",
    "pushed_1d", "pushed_4hr", "pushed_2hr", "pushed_1hr", "pushed_now",
)


def _rollback_database(backup: Path, row: dict, plan: dict) -> list[int]:
    """CAS-undo only this repair when all four rows remain untouched."""

    backup = _require_private_backup(backup)
    refs = [str(spec["source_ref"]) for spec in plan["reminders"]]
    expected = {str(spec["source_ref"]): spec for spec in plan["reminders"]}
    backup_conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        pending_before = backup_conn.execute(
            "SELECT pending_id,group_id,user_id,message_id,text,created_at,retries,"
            "claimed_at,claim_token,status FROM pending_reminder_extract WHERE pending_id=?",
            (int(row["pending_id"]),),
        ).fetchone()
        reminder_before = backup_conn.execute(
            f"SELECT {','.join(_REMINDER_COLUMNS)} FROM reminders "
            "WHERE group_id=? AND user_id=? AND status='pending' "
            "AND source_kind='' AND source_ref='' AND source_text=?",
            (row["group_id"], row["user_id"], plan["source_text"]),
        ).fetchall()
    finally:
        backup_conn.close()
    if pending_before is None or len(reminder_before) != 1:
        raise RuntimeError("backup does not contain the exact repair preimage")
    legacy_before = reminder_before[0]
    legacy_id = int(legacy_before[0])
    if str(pending_before[9]) != "pending":
        raise RuntimeError("backup pending preimage is not pending")

    with memory._lock, memory._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        pending_live = conn.execute(
            "SELECT pending_id,group_id,user_id,message_id,text,created_at,retries,"
            "claimed_at,claim_token,status FROM pending_reminder_extract WHERE pending_id=?",
            (int(row["pending_id"]),),
        ).fetchone()
        if (
            pending_live is None
            or tuple(pending_live[:7]) != tuple(pending_before[:7])
            or int(pending_live[7] or 0) != 0
            or str(pending_live[8] or "")
            or str(pending_live[9]) != "done"
        ):
            raise RuntimeError("live pending row drifted; rollback refused")
        placeholders = ",".join("?" for _ in refs)
        live_rows = conn.execute(
            f"SELECT {','.join(_REMINDER_COLUMNS)} FROM reminders WHERE group_id=? "
            f"AND source_kind='contextual_date_once' AND source_ref IN ({placeholders})",
            (row["group_id"], *refs),
        ).fetchall()
        if len(live_rows) != 4:
            raise RuntimeError("live contextual batch is incomplete; rollback refused")
        ids: list[int] = []
        for live in live_rows:
            live_map = dict(zip(_REMINDER_COLUMNS, live))
            spec = expected.get(str(live_map["source_ref"] or ""))
            if (
                spec is None
                or str(live_map["user_id"] or "") != str(row["user_id"] or "")
                or str(live_map["action"]) != str(spec["action"])
                or int(live_map["remind_at"]) != int(spec["remind_at"])
                or str(live_map["status"]) != "pending"
                or str(live_map["source_text"]) != str(plan["command_text"])
                or memory._load_mention_aliases(live_map["mention_aliases"])
                != list(spec["mention_aliases"])
                or any(int(live_map[key] or 0) for key in memory._REMINDER_PUSH_FLAG_COLUMNS)
            ):
                raise RuntimeError("live contextual payload drifted; rollback refused")
            ids.append(int(live_map["reminder_id"]))
        if legacy_id not in ids:
            raise RuntimeError("legacy identity is missing; rollback refused")
        id_placeholders = ",".join("?" for _ in ids)
        history = conn.execute(
            f"SELECT 1 FROM sent_reminder_refs WHERE reminder_id IN ({id_placeholders}) "
            "UNION ALL SELECT 1 FROM reminder_delivery_claims WHERE group_id=? "
            "AND delivery_kind='natural' "
            f"AND subject_ref IN ({id_placeholders}) LIMIT 1",
            (*ids, row["group_id"], *[str(value) for value in ids]),
        ).fetchone()
        if history is not None:
            raise RuntimeError("contextual batch has delivery history; rollback refused")

        for reminder_id in ids:
            if reminder_id != legacy_id:
                deleted = conn.execute(
                    "DELETE FROM reminders WHERE reminder_id=? AND status='pending'",
                    (reminder_id,),
                )
                if deleted.rowcount != 1:
                    raise RuntimeError("contextual rollback delete drift")
        assignments = ",".join(f"{column}=?" for column in _REMINDER_COLUMNS[1:])
        restored = conn.execute(
            f"UPDATE reminders SET {assignments} WHERE reminder_id=?",
            (*legacy_before[1:], legacy_id),
        )
        if restored.rowcount != 1:
            raise RuntimeError("legacy rollback restore drift")
        pending_restored = conn.execute(
            "UPDATE pending_reminder_extract SET retries=?,claimed_at=0,claim_token='',"
            "status='dropped' "
            "WHERE pending_id=? AND status='done'",
            (
                int(pending_before[6]), int(row["pending_id"]),
            ),
        )
        if pending_restored.rowcount != 1:
            raise RuntimeError("pending rollback restore drift")
    return sorted(ids)


def _verify(row: dict, plan: dict) -> None:
    refs = [str(spec["source_ref"]) for spec in plan["reminders"]]
    with memory._conn() as conn:
        placeholders = ",".join("?" for _ in refs)
        count = conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE group_id=? AND status='pending' "
            f"AND source_kind='contextual_date_once' AND source_ref IN ({placeholders})",
            (row["group_id"], *refs),
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status FROM pending_reminder_extract WHERE pending_id=?",
            (int(row["pending_id"]),),
        ).fetchone()[0]
        outbox = conn.execute(
            "SELECT COUNT(*) FROM reminder_confirmation_outbox "
            "WHERE group_id=? AND source_ref=?",
            (row["group_id"], f"pending_reminder:{int(row['pending_id'])}"),
        ).fetchone()[0]
    if count != 4 or status != "done" or outbox != 0:
        raise RuntimeError("post-repair verification failed")


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pending-id", type=int, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()

    row, plan, legacy = _load_plan(args.pending_id)
    print(f"pending_id={row['pending_id']} status={row['status']}")
    print(f"command_hash={_short_hash(row['text'])}")
    print(f"source_hash={_short_hash(plan['source_text'])}")
    print(f"legacy_reminder_id={legacy['reminder_id'] if legacy else 'none'}")
    print("slots=" + ",".join(str(spec["source_ref"]).rsplit(":", 2)[-2] + ":" + str(spec["source_ref"]).rsplit(":", 1)[-1] for spec in plan["reminders"]))
    print(
        "schedule="
        + ",".join(
            datetime.fromtimestamp(
                int(spec["remind_at"]), ZoneInfo("Asia/Taipei")
            ).strftime("%Y-%m-%dT%H:%M:%S")
            for spec in plan["reminders"]
        )
    )
    if args.rollback:
        if args.backup is None:
            raise RuntimeError("--rollback requires --backup")
        restored_ids = _rollback_database(args.backup, row, plan)
        print(f"rollback=true reminders={len(restored_ids)}")
        print("external_messages_sent=0")
        return 0
    if not args.apply:
        print("dry_run=true; no database mutation")
        return 0
    if row["status"] == "done":
        _verify(row, plan)
        print("already_applied=true")
        return 0

    backup = args.backup or Path(
        f"/private/tmp/line_bot_contextual_reminder_{args.pending_id}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
    )
    _backup_database(backup)
    print(f"backup={backup}")
    claim = memory.claim_pending_reminder(int(row["pending_id"]))
    if not claim:
        raise RuntimeError("pending claim failed")
    try:
        result = memory.complete_contextual_date_reminder_batch(
            group_id=row["group_id"],
            user_id=row["user_id"],
            plan=plan,
            pending_id=int(row["pending_id"]),
            pending_claim_token=claim,
            legacy_reminder_id=(int(legacy["reminder_id"]) if legacy else None),
        )
    except Exception:
        memory.release_pending_reminder(int(row["pending_id"]), claim)
        raise
    manifest = _write_manifest(
        backup,
        row=row,
        plan=plan,
        reminder_ids=list(result["reminder_ids"]),
        legacy_reminder_id=(int(legacy["reminder_id"]) if legacy else None),
    )
    _verify(row, plan)
    print(f"outcome={result['outcome']} reminders=4")
    print(f"manifest={manifest}")
    print("external_messages_sent=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
