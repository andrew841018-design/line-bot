"""股票推薦回測（計畫 B，2026-10-29）：review.sqlite 的 schema 與價格庫同步。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN.md（v5）§3、§4.4。

review.sqlite（state/stock_picks_review/，只有這支 job 寫；WAL）
- schema 版本記在 PRAGMA user_version，之後的段只會「新增表」（MIGRATIONS 往後加）。DB 的版本比程式新（例如 C 段
  回滾）照樣跑：A 段用到的表都在就好，不因 schema 比較新就停（審查 r2#6）。
- A 段的表：prices、pool_days、pool_rows，加一張 job 自己的小帳 job_state（價格同步鍵）。
  pool_days 比 PLAN 多一欄 meta_json（一版的附帶資訊：漏斗、配對的主快照、c0 來源…），之後不必加欄位。
- pool_rows 有 CHECK：status＝member 的列一定有 c0。

價格庫（prices）
- 要收的代號：池子紀錄近 420 天出現過的代號；台股另加 0050、TAIEX，美股另加 SPY、VOO、QQQ。
- 來源：盤勢 DB 的 tw、tw_all、us（provisional＝0），一律經 stock_db_ro（mode=ro、一個 SELECT 就關），路徑由呼叫端給。
- DB 保留範圍內以 DB 為準：每個（來源、代號）在 DB 的 [最早, 最晚] 之間，DB 沒有的日子刪掉、收盤不同就覆蓋；
  範圍外（DB 已經滾掉的）留著。同一天 tw 和 tw_all 都有時用 tw。整體只留 700 天。
- upsert 帶 `WHERE close IS NOT excluded.close`（沒變的列不重寫）。
- 同步鍵＝(盤勢 DB 的 mtime, 代號集合的雜湊)：和上次成功時一樣就跳過；代號集合沒變時 25 分鐘內最多同步一次。
  成功後才記鍵（DbBusy 中途失敗時下一輪照樣重來）。DbBusy／hot journal 回 busy，不算失敗。

這個模組 import 時不做任何 I/O；stock_db_ro 在函式裡才 import。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

SCHEMA_VERSION = 1
BUSY_TIMEOUT_S = 5.0

# 每個版本只新增表（CREATE … IF NOT EXISTS）；舊版本的表永遠不改、不刪
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS prices (
            m TEXT NOT NULL, code TEXT NOT NULL, d TEXT NOT NULL, close REAL NOT NULL,
            src TEXT NOT NULL, seen_at REAL NOT NULL,
            PRIMARY KEY (m, code, d)) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS pool_days (
            m TEXT NOT NULL, rule TEXT NOT NULL, t TEXT NOT NULL, ver_s INTEGER NOT NULL,
            derive_version INTEGER NOT NULL, computed_at REAL NOT NULL,
            src TEXT NOT NULL CHECK (src IN ('simple', 'spool')),
            bot_sha TEXT, n_members INTEGER NOT NULL, n_picks INTEGER NOT NULL, source TEXT, core_version TEXT,
            cooldown_read TEXT, market_json TEXT, meta_json TEXT, logged_at REAL NOT NULL,
            PRIMARY KEY (m, rule, t, ver_s, derive_version))""",
        """CREATE TABLE IF NOT EXISTS pool_rows (
            m TEXT NOT NULL, rule TEXT NOT NULL, t TEXT NOT NULL, ver_s INTEGER NOT NULL,
            derive_version INTEGER NOT NULL, code TEXT NOT NULL, name TEXT, c0 REAL,
            picked INTEGER NOT NULL CHECK (picked IN (0, 1)), rank INTEGER, status TEXT NOT NULL,
            trend_json TEXT, flags_json TEXT, values_json TEXT,
            CHECK (status <> 'member' OR (c0 IS NOT NULL AND c0 > 0)),
            PRIMARY KEY (m, rule, t, ver_s, derive_version, code))""",
        "CREATE INDEX IF NOT EXISTS pool_rows_by_code ON pool_rows (m, code, t)",
        "CREATE TABLE IF NOT EXISTS job_state (k TEXT PRIMARY KEY, v TEXT NOT NULL, at REAL NOT NULL)",
    ),
}
A_TABLES = ("prices", "pool_days", "pool_rows", "job_state")


class SchemaError(Exception):
    """review.sqlite 少了 A 段的表（user_version 說有、表卻不在）：不自己修，記失敗。"""


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT；任何例外（含逾時的 BaseException）都 ROLLBACK。conn 要是 autocommit 模式。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def open_db(path: Path) -> sqlite3.Connection:
    """讀寫連線（WAL）並把 schema 補到目前版本。檔案由呼叫端的 umask（077）決定權限。"""
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_readonly(path: Path) -> Optional[sqlite3.Connection]:
    """--dry-run 用：檔案不在回 None。用 immutable=1：WAL 的庫用 mode=ro 開會建出 -shm／-wal（實測），
    唯讀目錄還會直接失敗；immutable 什麼都不建（-wal 裡還沒 checkpoint 的最新幾筆看不到，dry-run 可以接受）。"""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def ensure_schema(conn: sqlite3.Connection) -> int:
    current = user_version(conn)
    if current < SCHEMA_VERSION:
        with transaction(conn):
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for sql in MIGRATIONS[version]:
                    conn.execute(sql)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return SCHEMA_VERSION
    present = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not set(A_TABLES) <= present:
        raise SchemaError("missing tables")
    return current  # 比程式新也照跑（之後的段只加表）


def get_state(conn: Optional[sqlite3.Connection], key: str) -> Optional[tuple[dict, float]]:
    if conn is None:
        return None
    found = conn.execute("SELECT v, at FROM job_state WHERE k = ?", (key,)).fetchone()
    if not found:
        return None
    try:
        value = json.loads(found[0])
    except ValueError:
        return None
    return (value, float(found[1])) if isinstance(value, dict) else None


def set_state(conn: sqlite3.Connection, key: str, value: dict, at: float) -> None:
    conn.execute(
        "INSERT INTO job_state (k, v, at) VALUES (?, ?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v, at = excluded.at",
        (key, json.dumps(value, sort_keys=True, separators=(",", ":")), float(at)),
    )


def db_sizes(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.stat(f"{path}{suffix}").st_size
        except FileNotFoundError:
            continue
    return total


# ── 價格庫 ─────────────────────────────────────────────────────────────────
PRICE_KEEP_DAYS = 700
POOL_LOOKBACK_DAYS = 420
SYNC_THROTTLE_S = 25 * 60
SYNC_KEY = "prices_sync"
TW_EXTRA = ("0050", "TAIEX")
US_EXTRA = ("SPY", "VOO", "QQQ")
SOURCES = {"tw": ("tw", "tw_all"), "us": ("us",)}  # 同一天兩個來源都有時，排前面的優先
_MAX_PRICE = 1e7
UPSERT_SQL = (
    "INSERT INTO prices (m, code, d, close, src, seen_at) VALUES (?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (m, code, d) DO UPDATE SET close = excluded.close, src = excluded.src, seen_at = excluded.seen_at"
    " WHERE close IS NOT excluded.close"
)


def wanted_codes(conn: Optional[sqlite3.Connection], today) -> dict[str, list[str]]:
    """{市場: 要收的代號（排序過）}：池子紀錄近 420 天出現過的＋固定的基準。"""
    since = (today - timedelta(days=POOL_LOOKBACK_DAYS)).isoformat()
    out = {"tw": set(TW_EXTRA), "us": set(US_EXTRA)}
    if conn is not None:
        for market, code in conn.execute("SELECT DISTINCT m, code FROM pool_rows WHERE t >= ?", (since,)):
            if market in out and isinstance(code, str):
                out[market].add(code)
    return {market: sorted(codes) for market, codes in out.items()}


def codes_hash(codes: dict[str, list[str]]) -> str:
    return hashlib.sha256(json.dumps(codes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _valid_close(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 < number < _MAX_PRICE else None


def fetch_market(market_db: Path, codes: dict[str, list[str]], since: str) -> dict[str, dict[str, dict]]:
    """{市場: {來源: {代號: {日期: 收盤}}}}；DbBusy／DbUnavailable 照樣往上丟。"""
    import stock_db_ro

    out: dict[str, dict[str, dict]] = {}
    for market, sources in SOURCES.items():
        out[market] = {src: stock_db_ro.closes(src, codes[market], since, path=market_db) for src in sources}
    return out


def plan_changes(existing: dict[tuple, float], fetched: dict[str, dict[str, dict]], keep_from: str) -> tuple[list, list]:
    """(upserts [(m, code, d, close, src)], deletes [(m, code, d)])。純函式。
    - 同一天多個來源都有：SOURCES 排前面的優先（tw 先於 tw_all）。
    - 刪：某個來源在 DB 的 [最早, 最晚] 之間、所有來源都沒有的日子（DB 為準）；DB 完全沒有這個代號就一筆都不刪。"""
    upserts: list[tuple] = []
    deletes: list[tuple] = []
    for market, by_src in fetched.items():
        merged: dict[str, dict[str, tuple[float, str]]] = {}
        windows: dict[str, list[tuple[str, str]]] = {}
        for src in SOURCES[market]:
            for code, days in (by_src.get(src) or {}).items():
                valid = {d: c for d, c in days.items() if isinstance(d, str) and _valid_close(c) is not None}
                if not valid:
                    continue
                windows.setdefault(code, []).append((min(valid), max(valid)))
                for day, close in valid.items():
                    merged.setdefault(code, {}).setdefault(day, (float(close), src))
        for code, days in merged.items():
            for day, (close, src) in days.items():
                if day >= keep_from and existing.get((market, code, day)) != close:
                    upserts.append((market, code, day, close, src))
        for (m, code, day) in existing:
            if m != market or day < keep_from or day in merged.get(code, {}):
                continue
            if any(lo <= day <= hi for lo, hi in windows.get(code, ())):
                deletes.append((m, code, day))
    return upserts, deletes


def _is_busy(exc: BaseException) -> bool:
    """DbBusy，或盤勢 job 被殺留下 hot journal（mode=ro 讀不了，等它下一輪收拾）：都不算失敗。"""
    import stock_db_ro

    if isinstance(exc, stock_db_ro.DbBusy):
        return True
    return isinstance(exc, stock_db_ro.DbUnavailable) and str(exc.kind).startswith("sqlite_readonly_rollback")


def prices_stage(ctx) -> dict:
    """價格庫同步（PLAN §3）。dry-run：照算要改幾筆，不寫、不記鍵。"""
    conn = ctx.db()
    today = ctx.today
    codes = wanted_codes(conn, today)
    key = {"codes": codes_hash(codes)}
    try:
        key["mtime_ns"] = os.stat(ctx.paths.market_db).st_mtime_ns
    except FileNotFoundError:
        import stock_db_ro

        raise stock_db_ro.DbUnavailable("missing") from None
    last = get_state(conn, SYNC_KEY)
    out: dict[str, Any] = {"codes_tw": len(codes["tw"]), "codes_us": len(codes["us"])}
    if last is not None:
        value, at = last
        if value.get("codes") == key["codes"]:
            if value.get("mtime_ns") == key["mtime_ns"]:
                return {**out, "status": "skipped:unchanged"}
            if 0 <= ctx.now - at < SYNC_THROTTLE_S:  # 未來的時間（時鐘倒退）不擋
                return {**out, "status": "skipped:throttled"}
    keep_from = (today - timedelta(days=PRICE_KEEP_DAYS)).isoformat()
    try:
        fetched = fetch_market(ctx.paths.market_db, codes, keep_from)
    except Exception as exc:
        if _is_busy(exc):
            return {**out, "status": "busy"}
        raise
    existing = {}
    if conn is not None:
        existing = {(m, code, d): close for m, code, d, close in conn.execute("SELECT m, code, d, close FROM prices")}
    upserts, deletes = plan_changes(existing, fetched, keep_from)
    expired = sum(1 for (_m, _code, d) in existing if d < keep_from)
    out.update(upserts=len(upserts), deletes=len(deletes), expired=expired)
    if ctx.dry_run:
        return {**out, "status": "dry_run"}
    with transaction(conn):
        conn.executemany(UPSERT_SQL, [(m, code, d, close, src, ctx.now) for m, code, d, close, src in upserts])
        conn.executemany("DELETE FROM prices WHERE m = ? AND code = ? AND d = ?", deletes)
        conn.execute("DELETE FROM prices WHERE d < ?", (keep_from,))
        set_state(conn, SYNC_KEY, key, ctx.now)  # 成功才記：中途失敗下一輪整段重來
    out["rows"] = conn.execute("SELECT count(*) FROM prices").fetchone()[0]
    return {**out, "status": "ok"}
