#!/usr/bin/env python
"""Build the group-local canonical projection for retained organic corrections.

The command is dry-run by default.  It never calls a model or sends a LINE /
Discord message.  Dry-run makes no logical database/schema changes (SQLite may
create normal WAL reader sidecars); ``--apply`` only adds projection rows and
links them to the existing immutable ``persona_notes`` audit rows.

Usage::

    ./.venv/bin/python scripts/backfill_correction_memory.py
    ./.venv/bin/python scripts/backfill_correction_memory.py --apply
    ./.venv/bin/python scripts/backfill_correction_memory.py --group-id C123
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def _positive_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("limit must be at least 1")
    return parsed


def _dry_run(
    db_path: str,
    group_id: str,
    limit: int | None,
) -> dict[str, int | None]:
    """Count eligible rows through a strictly read-only SQLite connection."""

    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        persona_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_notes'"
        ).fetchone()
        if persona_exists is None:
            return {"eligible": 0, "linked": 0, "unresolved": None}
        observations_exist = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='correction_observations'"
        ).fetchone()
        if observations_exist:
            sql = (
                "SELECT p.note_id FROM persona_notes p "
                "LEFT JOIN correction_observations o ON o.note_id=p.note_id "
                "WHERE p.kind='correction' AND p.source='organic' "
                "AND o.note_id IS NULL"
            )
        else:
            sql = (
                "SELECT note_id FROM persona_notes "
                "WHERE kind='correction' AND source='organic'"
            )
        params: list[object] = []
        if group_id:
            sql += " AND group_id=?" if not observations_exist else " AND p.group_id=?"
            params.append(group_id)
        sql += " ORDER BY p.note_id" if observations_exist else " ORDER BY note_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        eligible = sum(1 for _row in conn.execute(sql, tuple(params)))
    return {"eligible": eligible, "linked": 0, "unresolved": None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill canonical organic-correction memory (default: dry-run)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write projection rows. Without this flag the database is unchanged.",
    )
    parser.add_argument(
        "--group-id",
        default="",
        help="Restrict processing to one exact LINE group id.",
    )
    parser.add_argument(
        "--limit",
        type=_positive_limit,
        default=None,
        help="Process at most N eligible audit rows.",
    )
    args = parser.parse_args(argv)

    from config import settings

    group_id = args.group_id.strip()
    if args.apply:
        import memory

        result = memory.backfill_organic_corrections(
            group_id=group_id or None,
            dry_run=False,
            limit=args.limit,
        )
    else:
        result = _dry_run(settings.sqlite_path, group_id, args.limit)
    output = {
        "mode": "apply" if args.apply else "dry-run",
        "scope": "one-group" if group_id else "all-groups",
        **result,
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
