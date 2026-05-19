#!/usr/bin/env python
"""Backfill semantic embeddings for existing raw_messages.

Added 2026-05-19. Runs once to populate `embeddings` table with vectors for
the 1700+ historical raw_messages so semantic recall has signal from msg #1
of production rollout. Resumable: INSERT OR REPLACE means re-running just
re-embeds in-place (idempotent).

Usage:
    cd /Users/andrew/Desktop/andrew/Data_engineer/line_bot
    .venv/bin/python -m scripts.backfill_embeddings
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Make sibling imports work when run as a module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
import embedding_recall as er  # noqa: E402


def iter_raw_messages(db_path: str):
    """Yield (group_id, message_id, user_id, text) in chronological order."""
    c = sqlite3.connect(db_path)
    try:
        rows = c.execute(
            "SELECT group_id, message_id, user_id, text, created_at "
            "FROM raw_messages "
            "WHERE text IS NOT NULL AND text != '' "
            "ORDER BY created_at ASC"
        )
        for row in rows:
            yield row
    finally:
        c.close()


def count_existing_st_rows(db_path: str) -> int:
    c = sqlite3.connect(db_path)
    try:
        n = c.execute(
            "SELECT COUNT(*) FROM embeddings WHERE model_name = ?",
            (er.MODEL_TAG,),
        ).fetchone()[0]
        return int(n)
    finally:
        c.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill embeddings for raw_messages")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Walk the rows but skip embed/write (sanity-check the input set).",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Stop after N rows (0 = all).",
    )
    args = parser.parse_args()

    db_path = settings.sqlite_path
    print(f"DB: {db_path}")
    print(f"Existing ST rows before: {count_existing_st_rows(db_path)}")

    indexed = 0
    skipped = 0
    failed = 0
    start = time.time()

    for i, (group_id, message_id, user_id, text, created_at) in enumerate(
        iter_raw_messages(db_path), 1
    ):
        if args.limit and i > args.limit:
            break
        is_bot = (user_id == "__bot__")
        if args.dry_run:
            if er._should_index(text):
                indexed += 1
            else:
                skipped += 1
            continue
        ok = er.index_message(message_id, group_id, text, is_bot=is_bot)
        if ok:
            indexed += 1
        else:
            # could be skip (placeholder / short) OR failure; can't tell without
            # introspecting `embed` return — keep simple.
            if er._should_index(text):
                failed += 1
            else:
                skipped += 1
        if i % 100 == 0:
            elapsed = time.time() - start
            rate = i / elapsed if elapsed > 0 else 0
            print(
                f"  progress {i} rows / indexed={indexed} skipped={skipped} "
                f"failed={failed} ({rate:.1f}/s)"
            )

    elapsed = time.time() - start
    print()
    print(f"Done in {elapsed:.1f}s")
    print(f"  indexed: {indexed}")
    print(f"  skipped (too short / placeholder): {skipped}")
    print(f"  failed: {failed}")
    print(f"Existing ST rows after: {count_existing_st_rows(db_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
