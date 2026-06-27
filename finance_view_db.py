"""家族財經觀點 — finance_views 表 CRUD。

extractor 在 burst flush 時呼叫 Gemini 抽家族成員提出的財經觀點：
- 標的（ticker / macro / crypto / index）
- 方向 / 時間框架 / 信心度 / 目標價
- 驗證狀態（pending → hit / miss / na）

驗證：
- ticker 類：yfinance 比對 created_at 到 expires_at 期間表現
- macro 類：暫標 na（簡化；後續可接 Gemini 質性比對）

設計參照 calendar_db.py（WAL + threading lock + init_db idempotent + uuid pk）。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from config import settings

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()
_INIT_PATHS: set[str] = set()


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else _DB_PATH


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _db_path(db_path),
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    path = _db_path(db_path)
    path_key = str(path)
    if path_key in _INIT_PATHS:
        return
    with _lock:
        if path_key in _INIT_PATHS:
            return
        with _conn(path) as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS finance_views (
                    view_id               TEXT PRIMARY KEY,
                    group_id              TEXT NOT NULL,
                    source_msg_id         TEXT,
                    user_id               TEXT NOT NULL,
                    display_name          TEXT NOT NULL,
                    raw_text              TEXT NOT NULL,
                    symbol_type           TEXT NOT NULL,
                    ticker                TEXT,
                    macro_topic           TEXT,
                    direction             TEXT,
                    time_frame            TEXT,
                    horizon_days          INTEGER,
                    target_price          REAL,
                    target_pct            REAL,
                    confidence            TEXT,
                    condition_text        TEXT,
                    validated_price_start REAL,
                    validated_price_end   REAL,
                    status                TEXT NOT NULL DEFAULT 'active',
                    created_at            INTEGER NOT NULL,
                    expires_at            TEXT,
                    last_validated_at     INTEGER,
                    validation_result     TEXT NOT NULL DEFAULT 'pending',
                    validation_detail     TEXT
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_fv_group_created "
                "ON finance_views(group_id, created_at)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_fv_group_ticker_created "
                "ON finance_views(group_id, ticker, created_at)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_fv_expires_result "
                "ON finance_views(expires_at, validation_result)"
            )
        _INIT_PATHS.add(path_key)


def insert_view(
    group_id: str,
    source_msg_id: Optional[str],
    user_id: str,
    display_name: str,
    raw_text: str,
    symbol_type: str,
    ticker: Optional[str],
    macro_topic: Optional[str],
    direction: Optional[str],
    time_frame: Optional[str],
    horizon_days: Optional[int],
    target_price: Optional[float],
    target_pct: Optional[float],
    confidence: Optional[str],
    condition_text: Optional[str],
    expires_at: Optional[str],
    db_path: Path | str | None = None,
) -> str:
    view_id = uuid.uuid4().hex
    now_ms = int(time.time() * 1000)
    init_db(db_path)
    with _lock, _conn(db_path) as c:
        c.execute(
            """
            INSERT INTO finance_views (
                view_id, group_id, source_msg_id, user_id, display_name, raw_text,
                symbol_type, ticker, macro_topic, direction, time_frame, horizon_days,
                target_price, target_pct, confidence, condition_text,
                status, created_at, expires_at, validation_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 'pending')
            """,
            (
                view_id, group_id, source_msg_id, user_id, display_name, raw_text,
                symbol_type, ticker, macro_topic, direction, time_frame, horizon_days,
                target_price, target_pct, confidence, condition_text,
                now_ms, expires_at,
            ),
        )
    return view_id


def list_recent(group_id: str, limit: int = 10) -> list[dict]:
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM finance_views WHERE group_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_by_person(group_id: str, display_name: str, limit: int = 10) -> list[dict]:
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM finance_views WHERE group_id = ? AND display_name = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (group_id, display_name, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_by_ticker(group_id: str, ticker: str, limit: int = 10) -> list[dict]:
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM finance_views WHERE group_id = ? AND ticker = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (group_id, ticker, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_pending_validation(now_iso: str) -> list[dict]:
    """回傳 expires_at <= now 且 validation_result = pending 的 active views。"""
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM finance_views WHERE status = 'active' "
            "AND validation_result = 'pending' AND expires_at IS NOT NULL "
            "AND expires_at <= ?",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_validation(
    view_id: str,
    result: str,
    detail: str,
    price_start: Optional[float] = None,
    price_end: Optional[float] = None,
) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE finance_views SET validation_result = ?, validation_detail = ?, "
            "validated_price_start = ?, validated_price_end = ?, "
            "last_validated_at = ? WHERE view_id = ?",
            (result, detail, price_start, price_end, int(time.time() * 1000), view_id),
        )


def count_by_result(group_id: str) -> dict[str, int]:
    """聚合命中率用。回 {hit: n, miss: n, pending: n, na: n}。"""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT validation_result, COUNT(*) FROM finance_views "
            "WHERE group_id = ? GROUP BY validation_result",
            (group_id,),
        ).fetchall()
    out = {"hit": 0, "miss": 0, "pending": 0, "na": 0}
    for r in rows:
        if r[0] in out:
            out[r[0]] = r[1]
    return out


# 允許 import 時自動建表（跟 memory.py / calendar_db 同模式）
init_db()
