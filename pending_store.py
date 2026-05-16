"""Single source of truth for pending_explicit_reply.json.

All readers/writers MUST go through this module. fcntl.flock for cross-process
safety (subprocess workers vs uvicorn handler threads), threading.RLock for
in-process re-entrant safety, atomic os.replace for crash-safe writes.

Replaces ad-hoc json.load/dump scattered across main.py.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).parent
PENDING_PATH = BASE / "pending_explicit_reply.json"
PENDING_MEDIA_DIR = BASE / "pending_media"
LOCK_PATH = BASE / ".pending_explicit_reply.lock"

logger = logging.getLogger("pending_store")

_rlock = threading.RLock()


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _load_raw() -> dict:
    if not PENDING_PATH.exists():
        return {}
    try:
        with open(PENDING_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("load pending failed: %s", e)
        return {}


def _save_raw(data: dict) -> None:
    """Atomic write: tmp file + os.replace."""
    fd, tmp_path = tempfile.mkstemp(
        prefix=".pending_explicit_reply.", suffix=".tmp", dir=str(BASE)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, PENDING_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load() -> dict:
    """Read full pending dict. Snapshot — caller mustn't mutate."""
    with _rlock, _FileLock(LOCK_PATH):
        return _load_raw()


def list_for_group(group_id: str) -> list[dict]:
    """Snapshot of pending entries for a group."""
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        items = data.get(group_id, [])
        return list(items) if isinstance(items, list) else []


def add(group_id: str, entry: dict) -> None:
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        if group_id not in data or not isinstance(data[group_id], list):
            data[group_id] = []
        data[group_id].append(entry)
        _save_raw(data)


def remove_by_message_id(group_id: str, message_id: str) -> bool:
    """Idempotent. Removes matching entry + unlinks media file. Returns whether removed."""
    if not message_id:
        return False
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        items = data.get(group_id, [])
        if not isinstance(items, list):
            return False
        new_items = []
        removed = False
        for it in items:
            if it.get("message_id") == message_id:
                removed = True
                mp = it.get("media_path")
                if mp and os.path.exists(mp):
                    try:
                        os.remove(mp)
                    except OSError as e:
                        logger.warning("unlink media %s failed: %s", mp, e)
                continue
            new_items.append(it)
        if not removed:
            return False
        if new_items:
            data[group_id] = new_items
        else:
            data.pop(group_id, None)
        _save_raw(data)
        return True


def clear_group(group_id: str) -> None:
    """Drop all entries for a group + media files."""
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        for entry in data.get(group_id, []):
            mp = entry.get("media_path")
            if mp and os.path.exists(mp):
                try:
                    os.remove(mp)
                except OSError:
                    pass
        data.pop(group_id, None)
        _save_raw(data)


def replace_group(group_id: str, items: list[dict]) -> None:
    """Replace a group's entries wholesale (startup processor uses this after partial drain)."""
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        if items:
            data[group_id] = items
        else:
            data.pop(group_id, None)
        _save_raw(data)


def save_full(data: dict) -> None:
    """Atomically replace whole pending dict. Used by legacy callers in main.py."""
    with _rlock, _FileLock(LOCK_PATH):
        _save_raw(data)

def sweep_orphan_media(max_age_sec: int = 86400) -> int:
    """Remove media files in pending_media/ with no matching JSON entry, older than max_age_sec.

    GP2 deferred: wire into startup hook + 500MB cap.
    """
    if not PENDING_MEDIA_DIR.exists():
        return 0
    referenced = set()
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        for items in data.values():
            if not isinstance(items, list):
                continue
            for it in items:
                mp = it.get("media_path")
                if mp:
                    referenced.add(os.path.abspath(mp))
    now = time.time()
    removed = 0
    for p in PENDING_MEDIA_DIR.iterdir():
        if not p.is_file():
            continue
        if str(p.resolve()) in referenced:
            continue
        try:
            if now - p.stat().st_mtime < max_age_sec:
                continue
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed
