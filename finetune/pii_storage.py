"""SQLite-backed cache for PII mappings — keeps the raw → placeholder map for
each (prompt, completion) pair so re-runs are deterministic and the original
text never leaves the laptop.

The DB lives at `finetune/data/pii_mappings.db` by default. **Never sync this
file to cloud / git** — it contains the un-masked originals.

Schema::

    CREATE TABLE pii_mappings (
        record_hash TEXT PRIMARY KEY,    -- SHA256 of raw "prompt│completion"
        mapping_json TEXT NOT NULL,      -- {"[NAME_1]": "黃媽媽", ...}
        created_at INTEGER NOT NULL      -- unix timestamp (UTC)
    );

Public API:
  - record_hash(prompt, completion) -> str
  - load_mapping(db_path, record_hash) -> dict | None
  - save_mapping(db_path, record_hash, mapping) -> None
  - mask_with_cache(prompt, completion, db_path=...) -> (mp_p, mp_c, mapping)
        Idempotent: identical input → identical output (mapping fetched from DB
        if present; otherwise computed and inserted).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from finetune.pii_masker import mask

logger = logging.getLogger("pii_storage")

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "data" / "pii_mappings.db"


# ─────────────────────────────────────────────────────────────────────────────
def record_hash(prompt: str, completion: str) -> str:
    """Stable SHA256 hash over (prompt, completion). Same as
    dataset_builder._pair_hash to keep keys aligned."""
    blob = f"{prompt}│{completion}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
def _connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS pii_mappings (
            record_hash TEXT PRIMARY KEY,
            mapping_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    con.commit()
    return con


def load_mapping(
    db_path: Path | str, rec_hash: str
) -> dict[str, str] | None:
    """Lookup a previously-cached mapping. None if not present."""
    con = _connect(db_path)
    try:
        cur = con.execute(
            "SELECT mapping_json FROM pii_mappings WHERE record_hash = ?",
            (rec_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning("malformed mapping_json for %s — recomputing", rec_hash)
            return None
    finally:
        con.close()


def save_mapping(
    db_path: Path | str, rec_hash: str, mapping: dict[str, str]
) -> None:
    """Insert-or-replace mapping for `rec_hash`."""
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO pii_mappings "
            "(record_hash, mapping_json, created_at) VALUES (?, ?, ?)",
            (rec_hash, json.dumps(mapping, ensure_ascii=False), int(time.time())),
        )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
def mask_with_cache(
    prompt: str,
    completion: str,
    db_path: Path | str = DEFAULT_DB,
    use_cache: bool = True,
) -> tuple[str, str, dict[str, str]]:
    """Mask both `prompt` and `completion` together so they share placeholders.

    Pipeline:
      1. Compute record_hash(prompt, completion).
      2. Try cache lookup; if hit, re-mask using cached mapping (idempotent).
      3. On miss, compute mapping and insert.
      4. Return (masked_prompt, masked_completion, mapping).

    Note: the mapping is **shared between prompt and completion** so a name
    that appears in both gets the same placeholder. We feed `prompt` through
    `mask()` first, then re-use the resulting mapping when masking
    `completion`.
    """
    rec_hash = record_hash(prompt, completion)

    # Cache lookup — if found, re-mask deterministically using existing
    # mapping so we get the same placeholders back.
    cached: dict[str, str] | None = None
    if use_cache:
        cached = load_mapping(db_path, rec_hash)

    # Build (or reuse) the mapping over BOTH strings. We always pass the
    # mapping through both calls so the same raw → same placeholder.
    mapping: dict[str, str] = dict(cached or {})
    mp_p, mapping = mask(prompt, mapping)
    mp_c, mapping = mask(completion, mapping)

    # Persist (overwrite is fine — mapping is deterministic for the same
    # inputs, but new entries can still appear if mask() detects more
    # spans on the second string).
    if use_cache and mapping != (cached or {}):
        save_mapping(db_path, rec_hash, mapping)

    return mp_p, mp_c, mapping


# ─────────────────────────────────────────────────────────────────────────────
def cache_size(db_path: Path | str = DEFAULT_DB) -> int:
    """How many records are cached?"""
    p = Path(db_path)
    if not p.exists():
        return 0
    con = _connect(db_path)
    try:
        cur = con.execute("SELECT COUNT(*) FROM pii_mappings")
        return int(cur.fetchone()[0])
    finally:
        con.close()


__all__ = [
    "record_hash",
    "load_mapping",
    "save_mapping",
    "mask_with_cache",
    "cache_size",
    "DEFAULT_DB",
]
