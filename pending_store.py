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
import uuid
from pathlib import Path

BASE = Path(__file__).parent
PENDING_PATH = BASE / "pending_explicit_reply.json"
PENDING_MEDIA_DIR = BASE / "pending_media"
LOCK_PATH = BASE / ".pending_explicit_reply.lock"

logger = logging.getLogger("pending_store")

_rlock = threading.RLock()


def _unlink_media(entry: dict) -> None:
    mp = entry.get("media_path")
    if mp and os.path.exists(mp):
        try:
            os.remove(mp)
        except OSError as e:
            logger.warning("unlink media %s failed: %s", mp, e)


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
        if isinstance(items, dict):
            return [items]
        return list(items) if isinstance(items, list) else []


def add(group_id: str, entry: dict) -> None:
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        if group_id not in data or not isinstance(data[group_id], list):
            data[group_id] = []
        if not entry.get("message_id"):
            entry = dict(entry)
            entry["message_id"] = f"legacy-{uuid.uuid4().hex}"
        data[group_id].append(entry)
        _save_raw(data)


def ensure_message_ids(group_id: str) -> int:
    """Give legacy entries stable synthetic ids under the store lock."""
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        items = data.get(group_id, [])
        if isinstance(items, dict):
            items = [items]
            data[group_id] = items
        if not isinstance(items, list):
            return 0
        changed = 0
        for it in items:
            if isinstance(it, dict) and not it.get("message_id"):
                it["message_id"] = f"legacy-{uuid.uuid4().hex}"
                changed += 1
        if changed:
            _save_raw(data)
        return changed


def remove_by_message_id(group_id: str, message_id: str) -> bool:
    """Idempotent. Removes matching entry + unlinks media file. Returns whether removed."""
    return remove_by_message_ids(group_id, [message_id]) > 0


def remove_by_message_ids(group_id: str, message_ids: list[str]) -> int:
    """Idempotently remove matching entries + media files. Returns removed count."""
    target_ids = {mid for mid in message_ids if mid}
    if not target_ids:
        return 0
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        items = data.get(group_id, [])
        if not isinstance(items, list):
            return 0
        new_items = []
        removed = 0
        for it in items:
            if it.get("message_id") in target_ids:
                removed += 1
                _unlink_media(it)
                continue
            new_items.append(it)
        if not removed:
            return 0
        if new_items:
            data[group_id] = new_items
        else:
            data.pop(group_id, None)
        _save_raw(data)
        return removed


def pop_stale(group_id: str, max_age_sec: int, now: float | None = None) -> list[dict]:
    """Atomically remove stale entries for one group and return the dropped snapshot."""
    now = time.time() if now is None else now
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        items = data.get(group_id, [])
        if not isinstance(items, list) or not items:
            return []
        keep: list[dict] = []
        drop: list[dict] = []
        for it in items:
            ts = it.get("timestamp")
            if ts is None or (now - ts) <= max_age_sec:
                keep.append(it)
            else:
                drop.append(it)
        if not drop:
            return []
        for it in drop:
            _unlink_media(it)
        if keep:
            data[group_id] = keep
        else:
            data.pop(group_id, None)
        _save_raw(data)
        return list(drop)


def clear_group(group_id: str) -> None:
    """Drop all entries for a group + media files."""
    with _rlock, _FileLock(LOCK_PATH):
        data = _load_raw()
        for entry in data.get(group_id, []):
            _unlink_media(entry)
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
