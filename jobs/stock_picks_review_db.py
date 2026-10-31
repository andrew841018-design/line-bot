"""股票推薦回測（計畫 B，2026-10-29）：review.sqlite 的 schema 與價格庫同步。

規格：ops/state/line_bot_stock_backtest_20261028/PLAN.md（v5）§3、§4.4。

review.sqlite（state/stock_picks_review/，只有這支 job 寫；WAL）
- schema 版本記在 PRAGMA user_version，之後的段只會「新增表」（MIGRATIONS 往後加）。DB 的版本比程式新（例如 C 段
  回滾）照樣跑：A 段用到的表都在就好，不因 schema 比較新就停（審查 r2#6）。
- A 段的表：prices、pool_days、pool_rows，加一張 sync_state（只放價格同步鍵 prices_sync；之後的段要記別的狀態就開
  自己的表、同步更新 PLAN，不要塞進這裡）。
- pool_days.origin：simple（job 從狀態檔推導）｜spool（bot 當下寫的 spool）。prices.src 是收盤的來源（tw｜tw_all｜us），
  pool_days.source 是 AI 名單的來源（00952｜cached｜core_only），三個是不同的東西。
- pool_days 比 PLAN 多一欄 meta_json（一版的附帶資訊），各族的鍵（picks_match、funnel_match、pick_mismatch 只是診斷：
  重算的排序、漏斗和 bot 記下的一不一致，目前沒有人讀）：
  - tw_v1（simple）：pool、listed（主快照兩份清單的筆數）、candidates（重算的候選數）、picks_match。
  - tw_ai，origin＝simple：funnel（AI 狀態的漏斗原樣）、funnel_match、main_ver_s／main_origin（配對的主快照：
    round(computed_at)、current｜archive｜import）、pick_mismatch（入選卻重算不成立的檔數）、skipped（AI 狀態 skipped
    的檔數）、universe_as_of、fetched_day。
  - tw_ai，origin＝spool：funnel（spool 的漏斗原樣）。
  - us（simple）：covered、c0_db／c0_latest（c0 取自 DB／快照 latest_closes 的檔數）、candidates、picks_match、
    universe_as_of。
- pool_days.market_json：台股 [大盤狀態, 月線乖離‰, 季線乖離‰]；美股 {"level", "b20", "b60"}（SPY）。spool 取代簡版時，
  spool 沒有就沿用簡版的。
- pool_rows 有 CHECK：status＝member 的列一定有 c0。
- C1 的表（user_version 2）：member_returns(m, family, rule, t, ver_s, derive_version, code, h, ledger_version,
  calc_version, r, b, skip, inputs_json, frozen_at)：股票池每個成員、每個 h 用 perf.evaluate 算出來的報酬
  （stock_picks_review_returns 寫），凍結後不改（ON CONFLICT DO NOTHING）。
  - PK 含 rule（和池子表的版本身分 (m, rule, t, ver_s, derive_version) 一致；family 留給 C2 查詢）、ledger_version
    （perf 帳的版本）與 calc_version（job 端算法的版本，returns.CALC_VERSION）：任一個升版，舊列留著備查、新版本重算，
    查詢一律只看目前的版本（審查 C1 r2#1、r4#3）。
  - inputs_json＝{th, c0, ce, ch, cf, be, bh, bf}（和 perf 帳列的算式輸入同名）；沒有 c0、不等 perf 凍結就直接記
    missing 的列是 NULL。
  p（股票池等權平均）不另存，出報表時才從這張表算。A 段的程式照樣跑得動這個庫（只多一張表；C1 回滾時不刪它）。
  returns 階段自己檢查這張表在不在（has_tables）：不在只讓那個階段失敗，不讓 open_db 擋住其他階段。

為什麼放內建碟、怎麼備份
- review.sqlite 每 30 分鐘寫一次（WAL），外接的 WD_BLACK 只跑 USB 2.0（約 34 MiB/s，而且和 Time Machine 共用空間），
  所以留在內建碟的 state/ 底下；內建碟由 Time Machine 備份。
- 它是超過 10 天的非 0050 成分台股收盤唯一的一份（盤勢 DB 的 tw_all 只留 10 天）；壞掉時，台股收盤可以從封存的主快照
  （archive/ 裡 stock_picks_tw 的 listed 收盤）重建，美股與 0050／TAIEX 從盤勢 DB 重抓。封存是 AI 狀態唯一的一份，
  一樣靠 Time Machine。

價格庫（prices）
- 要收的代號：池子紀錄近 420 天出現過的代號；台股另加 0050、TAIEX，美股另加 SPY、VOO、QQQ。
- 來源：盤勢 DB 的 tw、tw_all、us（provisional＝0），一律經 stock_db_ro（mode=ro、一個 SELECT 就關），路徑由呼叫端給。
- DB 保留範圍內以 DB 為準：每個（來源、代號）在 DB 的 [最早, 最晚] 之間，DB 沒有的日子刪掉、收盤不同就覆蓋；
  範圍外（DB 已經滾掉的）留著。同一天 tw 和 tw_all 都有時用 tw。整體只留 700 天。
- upsert 帶 `WHERE close IS NOT excluded.close`（沒變的列不重寫）。
- 同步鍵＝(盤勢 DB 的 mtime, 代號集合的雜湊)：和上次成功時一樣就跳過；代號集合沒變時 25 分鐘內最多同步一次。
  成功後才記鍵（DbBusy 中途失敗時下一輪照樣重來）。DbBusy／hot journal 回 busy，不算失敗。
- 比對只讀要同步的代號在保留範圍內的列（不把整張 prices 載進記憶體）；過期的列用 SQL 直接數、直接刪。

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

SCHEMA_VERSION = 2
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
            origin TEXT NOT NULL CHECK (origin IN ('simple', 'spool')),
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
        # 只放價格同步鍵（SYNC_KEY）
        "CREATE TABLE IF NOT EXISTS sync_state (k TEXT PRIMARY KEY, v TEXT NOT NULL, at REAL NOT NULL)",
    ),
    2: (  # C1：成員報酬（PLAN v5 §3）。r 有值 ⇔ 沒有 skip；有值的列一定有基準 b
        """CREATE TABLE IF NOT EXISTS member_returns (
            m TEXT NOT NULL, family TEXT NOT NULL, rule TEXT NOT NULL, t TEXT NOT NULL, ver_s INTEGER NOT NULL,
            derive_version INTEGER NOT NULL, code TEXT NOT NULL, h INTEGER NOT NULL, ledger_version INTEGER NOT NULL,
            calc_version INTEGER NOT NULL,
            r REAL, b REAL, skip TEXT, inputs_json TEXT, frozen_at REAL NOT NULL,
            CHECK ((skip IS NULL) = (r IS NOT NULL)),
            CHECK (skip IS NOT NULL OR b IS NOT NULL),
            PRIMARY KEY (m, family, rule, t, ver_s, derive_version, code, h, ledger_version, calc_version))
            WITHOUT ROWID""",
    ),
}
A_TABLES = ("prices", "pool_days", "pool_rows", "sync_state")
C1_TABLES = ("member_returns",)


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
    唯讀目錄還會直接失敗；immutable 什麼都不建。代價（審查 r1#5）：不加鎖、不讀 -wal，-wal 裡還沒 checkpoint 的
    最新幾筆看不到；job 正好在寫或 checkpoint 時，可能讀到舊資料，甚至回 malformed。只用在診斷輸出，可以接受。"""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def ensure_schema(conn: sqlite3.Connection) -> int:
    """補到目前版本（只跑還沒跑過的 MIGRATIONS）。A 段的表不在（user_version 說有、表卻不在）就丟 SchemaError、
    整個交易 rollback，不自己修；升版時也一樣先檢查再改 user_version。"""
    current = user_version(conn)
    if current < SCHEMA_VERSION:
        with transaction(conn):
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for sql in MIGRATIONS[version]:
                    conn.execute(sql)
            if not has_tables(conn, A_TABLES):
                raise SchemaError("missing tables")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return SCHEMA_VERSION
    if not has_tables(conn, A_TABLES):  # 只看 A 段的表：C1 的表由 returns 階段自己看（不在只讓那個階段失敗）
        raise SchemaError("missing tables")
    return current  # 比程式新也照跑（之後的段只加表）


def has_tables(conn: sqlite3.Connection, names) -> bool:
    present = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return set(names) <= present


def get_state(conn: Optional[sqlite3.Connection], key: str) -> Optional[tuple[dict, float]]:
    """sync_state 的一筆（目前只有 SYNC_KEY）。"""
    if conn is None:
        return None
    found = conn.execute("SELECT v, at FROM sync_state WHERE k = ?", (key,)).fetchone()
    if not found:
        return None
    try:
        value = json.loads(found[0])
    except ValueError:
        return None
    return (value, float(found[1])) if isinstance(value, dict) else None


def set_state(conn: sqlite3.Connection, key: str, value: dict, at: float) -> None:
    conn.execute(
        "INSERT INTO sync_state (k, v, at) VALUES (?, ?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v, at = excluded.at",
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


def existing_prices(conn: Optional[sqlite3.Connection], codes: dict[str, list[str]],
                    keep_from: str) -> dict[tuple, float]:
    """{(m, code, d): close}：只有要同步的代號、保留範圍內（d ≥ keep_from）的列（plan_changes 只看這些）。
    一次最多查 500 個代號（SQLite 的參數上限），不把整張 prices 載進記憶體。"""
    out: dict[tuple, float] = {}
    if conn is None:
        return out
    for market, symbols in codes.items():
        for start in range(0, len(symbols), 500):
            chunk = symbols[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for m, code, d, close in conn.execute(
                    f"SELECT m, code, d, close FROM prices WHERE m = ? AND d >= ? AND code IN ({marks})",
                    (market, keep_from, *chunk)):
                out[(m, code, d)] = close
    return out


def is_busy(exc: BaseException) -> bool:
    """DbBusy，或盤勢 job 被殺留下 hot journal（mode=ro 讀不了，等它下一輪收拾）：都不算失敗（價格、成員報酬共用）。"""
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
        if is_busy(exc):
            return {**out, "status": "busy"}
        raise
    existing = existing_prices(conn, codes, keep_from)
    upserts, deletes = plan_changes(existing, fetched, keep_from)
    expired = 0 if conn is None else conn.execute("SELECT count(*) FROM prices WHERE d < ?", (keep_from,)).fetchone()[0]
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
