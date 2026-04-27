"""家族行事曆 — events 表 CRUD。

events 表：burst flush 時 Gemini 抽出的家族活動（聚餐、出遊、就醫…）。
- status='active'：未取消，會被 7 天前提醒掃到
- status='cancelled'：被使用者口頭取消（"不去了 / 改期"）
- reminded_at：已推 7 天前提醒的時間（NULL = 還沒推），避免重推
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

from config import settings

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id       TEXT PRIMARY KEY,
                group_id       TEXT NOT NULL,
                title          TEXT NOT NULL,
                event_date     TEXT NOT NULL,
                event_time     TEXT,
                location       TEXT,
                participants   TEXT,
                source_msg_id  TEXT,
                status         TEXT NOT NULL DEFAULT 'active',
                created_at     INTEGER NOT NULL,
                reminded_at    INTEGER
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_active "
            "ON events(group_id, status, event_date)"
        )


def insert_event(
    group_id: str,
    title: str,
    event_date: str,
    event_time: str | None = None,
    location: str | None = None,
    participants: list[str] | None = None,
    source_msg_id: str | None = None,
) -> str:
    event_id = uuid.uuid4().hex
    parts_json = json.dumps(participants or [], ensure_ascii=False)
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO events (event_id, group_id, title, event_date, event_time, "
            "location, participants, source_msg_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                event_id,
                group_id,
                title,
                event_date,
                event_time,
                location,
                parts_json,
                source_msg_id,
                int(time.time() * 1000),
            ),
        )
    return event_id


def find_active_event(
    group_id: str, keyword: str | None = None, near_date: str | None = None
) -> dict | None:
    """找最近的一筆 active event 用來取消／更新。
    優先順序：keyword 命中 title or location → near_date 一致 → 最近建立的。
    """
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
            "AND event_date >= date('now', '-1 day') ORDER BY created_at DESC",
            (group_id,),
        ).fetchall()
    if not rows:
        return None
    if keyword:
        for r in rows:
            if keyword in (r["title"] or "") or keyword in (r["location"] or ""):
                return dict(r)
    if near_date:
        for r in rows:
            if r["event_date"] == near_date:
                return dict(r)
    return dict(rows[0])


def cancel_event(event_id: str) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE events SET status = 'cancelled' WHERE event_id = ? AND status = 'active'",
            (event_id,),
        )
        return cur.rowcount > 0


def update_event_date(event_id: str, new_date: str, new_time: str | None = None) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE events SET event_date = ?, event_time = COALESCE(?, event_time), "
            "reminded_at = NULL WHERE event_id = ?",
            (new_date, new_time, event_id),
        )
        return cur.rowcount > 0


def list_upcoming(group_id: str, days: int = 30) -> list[dict]:
    today = date.today().isoformat()
    until = (date.today() + timedelta(days=days)).isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
            "AND event_date BETWEEN ? AND ? ORDER BY event_date, event_time",
            (group_id, today, until),
        ).fetchall()
    return [dict(r) for r in rows]


def list_due_for_reminder(days_ahead: int = 7) -> list[dict]:
    """回傳所有 event_date = today + days_ahead 且尚未推過提醒的 active events。"""
    target = (date.today() + timedelta(days=days_ahead)).isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE status = 'active' AND event_date = ? "
            "AND reminded_at IS NULL",
            (target,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_reminded(event_id: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE events SET reminded_at = ? WHERE event_id = ?",
            (int(time.time() * 1000), event_id),
        )


# 允許 import 時自動建表（跟 memory.py 同模式）
init_db()
