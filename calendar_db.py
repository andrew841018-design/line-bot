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
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings

_TW = ZoneInfo("Asia/Taipei")


def _today_tw() -> date:
    return datetime.now(_TW).date()

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()

# 集中 source of truth — 加新 type 只動這一行（GP2 反饋：防 enum 漂移）
EVENT_TYPES: tuple[str, ...] = ("family_gathering", "personal_trip", "medical")
_DEFAULT_EVENT_TYPE = "family_gathering"

# Reminder offsets — multi-tier 提醒 (Andrew 2026-05-25 directive 加 30/7-day)
# 30 = 1 個月前 / 7 = 1 週前 / 3-2-1 = 倒數三天每天推
# 加新 offset 只動這一行 + schema migration 自動補對應 reminded_Xd 欄位
REMINDER_OFFSETS: tuple[int, ...] = (30, 7, 3, 2, 1)


def _reminded_column(offset: int) -> str:
    """offset → reminded_Xd column name。Whitelist 驗 offset 防 SQL injection (column 不能 bind param)。"""
    if offset not in REMINDER_OFFSETS:
        raise ValueError(f"invalid offset {offset}; must be in {REMINDER_OFFSETS}")
    return f"reminded_{offset}d"


def _validate_event_type(et: str | None) -> str:
    """白名單驗證 — invalid → default。"""
    return et if et in EVENT_TYPES else _DEFAULT_EVENT_TYPE


# LIKE wildcard escape — escape 順序：先 \ 再 _ / %（順序錯會 double-escape）
def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")


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
        # PRAGMA pre-check + conditional ALTER（GP2 反饋：跨 process race window 收窄）
        # event_reminder.py 在 launchd job 跑時也 import calendar_db → init_db，
        # 跟 uvicorn worker 並行。PRAGMA + ALTER 在 WAL 下窗口小，加上 BUSY 容忍。
        cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
        if "event_type" not in cols:
            try:
                c.execute(
                    "ALTER TABLE events ADD COLUMN event_type "
                    "TEXT NOT NULL DEFAULT 'family_gathering'"
                )
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                # duplicate column = 另一 process 剛剛 ALTER 過，OK
                # database is locked = 暫時 contention，下一輪 import 會補
                if "duplicate column" not in msg and "database is locked" not in msg:
                    raise
            # 重新 verify（如果是 lock 沒做成功，下一輪 init_db 會處理）
            cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
        assert "event_type" in cols, "event_type migration failed"

        # Reminder offsets migration — T-3/T-2/T-1 三個 timestamp 欄位（NULL=未推）
        # 同 event_type PRAGMA pre-check + duplicate-column tolerance (codex critical)
        for off in REMINDER_OFFSETS:
            col_name = f"reminded_{off}d"
            if col_name not in cols:
                try:
                    c.execute(f"ALTER TABLE events ADD COLUMN {col_name} INTEGER")
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "duplicate column" not in msg and "database is locked" not in msg:
                        raise
                cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
            assert col_name in cols, f"{col_name} migration failed"
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_active "
            "ON events(group_id, status, event_date)"
        )
        # Dedup: 同 group 同 title 同日 active event 唯一
        # (group_id, title, event_date) WHERE status='active' — partial unique index
        # 防 TOCTOU race（GP1 反饋）：應用層 check-then-insert 不夠，SQL 層擋住
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup "
            "ON events(group_id, title, event_date) WHERE status='active'"
        )


def insert_event(
    group_id: str,
    title: str,
    event_date: str,
    event_time: str | None = None,
    location: str | None = None,
    participants: list[str] | None = None,
    source_msg_id: str | None = None,
    event_type: str = 'family_gathering',
) -> str:
    event_id = uuid.uuid4().hex
    parts_json = json.dumps(participants or [], ensure_ascii=False)
    et = _validate_event_type(event_type)
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO events (event_id, group_id, title, event_date, event_time, "
            "location, participants, source_msg_id, status, created_at, event_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
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
                et,
            ),
        )
        if cur.rowcount == 0:
            # dedup hit — 同 group+title+event_date 的 active event 已存在
            # GP1+gemini 反饋：跨 type 同 title+date 也會被 silent drop（by design）
            return ""
    return event_id


def find_active_event(
    group_id: str,
    keyword: str | None = None,
    near_date: str | None = None,
    event_type: str | None = None,
) -> dict | None:
    """找最近的一筆 active event 用來取消／更新。
    優先順序：event_type 過濾 → keyword 命中 title or location → near_date 一致 → 最近建立的。

    TODO (GP1+GP2 Phase 6): event_type filter 已加但 caller `/取消活動 <keyword>`
    (main.py:_cancel_calendar_event) 目前**沒**傳入。Cross-type collision 風險 medium：
    user 同時有 medical + family_gathering 同含「胃鏡」keyword → 誤取消最新一筆。
    後續若實際遇到衝突，可在 cancel command parser 推斷 type 並傳入。
    """
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        if event_type is not None:
            rows = c.execute(
                "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
                "AND event_date >= date('now', '-1 day') AND event_type = ? "
                "ORDER BY created_at DESC",
                (group_id, event_type),
            ).fetchall()
        else:
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
    """Reschedule event；reset 所有 reminded 欄位（codex critical：rescheduled event
    必須重推 T-3/T-2/T-1，否則之前推過的 flag 會壓住新提醒）。
    """
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE events SET event_date = ?, "
            "event_time = COALESCE(?, event_time), "
            "reminded_at = NULL, "
            "reminded_30d = NULL, reminded_7d = NULL, "
            "reminded_3d = NULL, reminded_2d = NULL, reminded_1d = NULL "
            "WHERE event_id = ?",
            (new_date, new_time, event_id),
        )
        return cur.rowcount > 0


def list_upcoming(group_id: str, days: int = 30) -> list[dict]:
    # 用 TW timezone today（避免 UTC host 跨日誤判 — codex/GP1 反饋）
    today_d = _today_tw()
    today = today_d.isoformat()
    until = (today_d + timedelta(days=days)).isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
            "AND event_date BETWEEN ? AND ? ORDER BY event_date, event_time",
            (group_id, today, until),
        ).fetchall()
    return [dict(r) for r in rows]


def list_past(group_id: str, days: int = 30) -> list[dict]:
    """過去 N 天的 active events（含 cancelled 的不在裡面）。最新日期優先。"""
    today_d = _today_tw()
    since = (today_d - timedelta(days=days)).isoformat()
    today_iso = today_d.isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
            "AND event_date >= ? AND event_date < ? "
            "ORDER BY event_date DESC, event_time DESC",
            (group_id, since, today_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def search_by_keyword(
    group_id: str, keywords: list[str], limit: int = 5
) -> list[dict]:
    """模糊比對 title / location / participants。bound params + LIKE escape。

    回傳排序：未來事件（接近今天優先）+ 過去事件（最新優先），用 Python union 兩段。
    GP1 反饋：純 DESC 會把遠未來放最前，user 問「什麼時候 X」通常想知道下一次。
    """
    if not keywords:
        return []
    today_iso = _today_tw().isoformat()
    # 每個 keyword 對 3 個欄位各跑一次 LIKE，OR 連起來
    conditions = []
    params: list = [group_id]
    for kw in keywords:
        pat = f"%{_escape_like(kw)}%"
        conditions.append(
            "(title LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' "
            "OR participants LIKE ? ESCAPE '\\')"
        )
        params.extend([pat, pat, pat])
    where = " OR ".join(conditions)
    sql_future = (
        "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date >= ? AND ({where}) "
        "ORDER BY event_date ASC, event_time ASC LIMIT ?"
    )
    sql_past = (
        "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date < ? AND ({where}) "
        "ORDER BY event_date DESC, event_time DESC LIMIT ?"
    )
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        future_rows = c.execute(
            sql_future, [group_id, today_iso, *params[1:], limit]
        ).fetchall()
        past_rows = c.execute(
            sql_past, [group_id, today_iso, *params[1:], limit]
        ).fetchall()
    combined = [dict(r) for r in future_rows] + [dict(r) for r in past_rows]
    return combined[:limit]


def search_by_title_phrase(
    group_id: str, verb_noun_pairs: list[tuple[str, str]], limit: int = 5
) -> list[dict]:
    """Verb+noun phrase search on title column with `%verb%noun%` LIKE pattern.

    Example: pairs=[("拿","蛋糕")] → title LIKE '%拿%蛋糕%'
    Matches: '拿蛋糕' / '拿爸爸生日蛋糕' / '拿一個蛋糕'
    Skips: '蛋糕拿來' (wrong order), '蛋糕' (no verb)

    Same ordering rule as search_by_keyword: future ASC + past DESC.
    """
    if not verb_noun_pairs:
        return []
    today_iso = _today_tw().isoformat()
    conditions = []
    pattern_params: list = []
    for v, n in verb_noun_pairs:
        conditions.append("title LIKE ? ESCAPE '\\'")
        pattern_params.append(f"%{_escape_like(v)}%{_escape_like(n)}%")
    where = " OR ".join(conditions)
    sql_future = (
        f"SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date >= ? AND ({where}) "
        "ORDER BY event_date ASC, event_time ASC LIMIT ?"
    )
    sql_past = (
        f"SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date < ? AND ({where}) "
        "ORDER BY event_date DESC, event_time DESC LIMIT ?"
    )
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        future_rows = c.execute(
            sql_future, [group_id, today_iso, *pattern_params, limit]
        ).fetchall()
        past_rows = c.execute(
            sql_past, [group_id, today_iso, *pattern_params, limit]
        ).fetchall()
    combined = [dict(r) for r in future_rows] + [dict(r) for r in past_rows]
    return combined[:limit]


def list_due_for_reminder(group_id: str, days_ahead: int = 7) -> list[dict]:
    """回傳指定 group_id 中 event_date = today + days_ahead 且該 offset 的 reminded 欄位
    IS NULL 的 active events。

    days_ahead in REMINDER_OFFSETS (30/7/3/2/1) → 查對應 reminded_Xd column
    days_ahead = 其他值（如 legacy）→ 走舊 `reminded_at` graveyard column（向後相容）

    group_id 從 2026-05-27 multi-group 起 required —— 兩位 reviewer 都警告若漏 filter，
    multi-group 下 piggyback drain 路徑會把家族群 events 推到 mom 群 reply_token（isolation breach）。
    """
    target = (_today_tw() + timedelta(days=days_ahead)).isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        if days_ahead in REMINDER_OFFSETS:
            col = _reminded_column(days_ahead)
            # column name 已被 _reminded_column whitelist 驗過 (SQL injection 防護)
            rows = c.execute(
                f"SELECT * FROM events WHERE group_id = ? AND status = 'active' "
                f"AND event_date = ? AND {col} IS NULL",
                (group_id, target),
            ).fetchall()
        else:
            # legacy path（保留 reminded_at 欄位向後相容） — group_id filter 同樣加上
            rows = c.execute(
                "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
                "AND event_date = ? AND reminded_at IS NULL",
                (group_id, target),
            ).fetchall()
    return [dict(r) for r in rows]


def mark_reminded(event_id: str, days_ahead: int, group_id: str) -> None:
    """標記 offset 的 reminded 欄位為 now timestamp。group_id 從 2026-05-27 起 required
    作為防禦深度（event_id 是 PK 全表唯一，理論上單 WHERE 不會誤判，但 reviewer 兩位都建議
    強制 group_id 防 caller 漏傳）。

    days_ahead in REMINDER_OFFSETS → 寫 reminded_Xd
    其他值 → 寫 legacy reminded_at（向後相容）
    """
    ts = int(time.time() * 1000)
    with _lock, _conn() as c:
        if days_ahead in REMINDER_OFFSETS:
            col = _reminded_column(days_ahead)
            c.execute(
                f"UPDATE events SET {col} = ? WHERE event_id = ? AND group_id = ?",
                (ts, event_id, group_id),
            )
        else:
            c.execute(
                "UPDATE events SET reminded_at = ? WHERE event_id = ? AND group_id = ?",
                (ts, event_id, group_id),
            )


# 允許 import 時自動建表（跟 memory.py 同模式）
init_db()
