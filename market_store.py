"""市場盤勢（/盤勢，2026-10-17）job 的 SQLite 存放區：`state/market_history.sqlite`。

Andrew 2026-10-17 要一張「目前市場狀況」的盤勢卡（美股／台股的技術面、寬度、量能、期權、籌碼、槓桿、
13F、宏觀、事件）。重活在 launchd 的 jobs/market_snapshot.py：collect 子程序抓資料寫進這裡，publish
子程序只讀這裡算顯示用快照。兩個子程序先後跑、不會同時開，所以這裡只有一個寫入者。

規則（PLAN §2、§18.2、§18.6、§18.7、§18.14、§19.6；INTERFACES §9.1）：
- 每個寫入都是短交易（BEGIN IMMEDIATE … COMMIT）：collect 被 kill 時最多丟掉正在寫的那一批，
  publish 照樣讀得到前面已 commit 的資料。`transaction()` 可以把幾個寫入包在一起；巢狀時內層用
  SAVEPOINT，內層失敗只退回內層。
- 檔案 0600：先用 O_NOFOLLOW 自己建好空檔，再用 mode=rw 交給 sqlite（sqlite 的 journal 檔沿用資料庫
  的權限）。不建立、不 chmod `state/` 本身（restart_lock 要求它是自己的 0700），呼叫端先用
  market_paths.state_dir_ok 檢查。
- 帳本（fetch_log）實作 market_http.Ledger：請求前在交易內先記帳；間隔、每台北日上限、冷卻都看這裡。
  帳本不能在呼叫端開著的 `transaction()` 裡用：網路請求不能夾在交易中間，交易回滾也會吃掉預扣。
- 價格表 closes：備援列（provisional，例如 CBOE 的收盤）不蓋掉正式來源的列；正式來源一律蓋掉備援
  （PLAN §18.7：Nasdaq 恢復後一律覆蓋、不經守門）。
- 這裡只存放、不判斷：覆寫守門、跳空待確認、分割整段重抓、颱風休市的規則在各 market_pipeline_*。
- 外部來源的字串（貼文、休市表說明）原樣存；給卡片用之前由 publish／bot 清洗。

這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import numbers
import os
import re
import sqlite3
import stat
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, NamedTuple, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from market_http import FetchError

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
POSTS_KEEP_S = 30 * 86400  # 川普貼文／新聞只用 48 小時；多留一陣子給缺口判斷，之後自動清掉
FETCH_LOG_KEEP_DAYS = 10
MAX_COOLDOWN_S = 7 * 86400
_TPE = ZoneInfo("Asia/Taipei")  # 每日上限以台北日期計（job 跑在台北的 Mac 上）


@dataclass(frozen=True)
class HostRule:
    min_interval_s: float  # 同一個帳戶兩次請求（預扣時刻）之間至少隔幾秒
    daily_cap: int  # 每個台北日最多幾次；預扣就算，失敗的請求也算
    bucket: str = ""  # 共用額度的帳戶名；空字串＝用主機名


# INTERFACES §9.1；Yahoo 的 query1／query2 是同一個服務，共用間隔與次數（Andrew：Yahoo 每天 1 次為主）。
HOST_RULES: dict[str, HostRule] = {
    "api.nasdaq.com": HostRule(1.0, 900),
    "www.twse.com.tw": HostRule(10.0, 12),
    "api.finmindtrade.com": HostRule(3.0, 60),
    # BLS API v1：免註冊每天 25 次、日界線沒寫、不一定以 IP 計。每台北日 12 次（一半）：任一 24 小時至多橫跨兩個台北日，也不會超過 25；
    # 一輪最多 4 個請求隔 5 秒（任務預算 35 秒），公布日 ≤10 次，留 2 次給失敗重試（2026-10-24 審查 r9 s2、s4）
    "api.bls.gov": HostRule(5.0, 12),
    "query1.finance.yahoo.com": HostRule(5.0, 2, "finance.yahoo.com"),
    "query2.finance.yahoo.com": HostRule(5.0, 2, "finance.yahoo.com"),
    # 2026-10-24：MacroMargin 的川普壓力指數頁（個人網站）：每個資料日一次、沒更新時每 2 小時再看、每台北日最多 6 次
    # （market_pipeline_taco）；主機另外留 2 次餘裕，間隔 10 秒
    "ocmacro.com": HostRule(10.0, 8),
}
DEFAULT_HOST_RULE = HostRule(2.0, 200)

OVERRIDE_CLOSED = "closed"  # 預期交易日其實沒開（颱風、國喪）
OVERRIDE_OPEN = "open"  # 表上不是交易日但其實有開
OVERRIDE_REVOKED = "revoked"  # 撤銷暫定休市（之後看到那天的資料）；交易日曆當作沒有覆寫，紀錄留著
OVERRIDE_KINDS = frozenset({OVERRIDE_CLOSED, OVERRIDE_OPEN, OVERRIDE_REVOKED})

_MARKET_RE = re.compile(r"[a-z][a-z0-9_]{0,15}")
_SYMBOL_RE = re.compile(r"[A-Za-z0-9.^=_\-]{1,32}")  # 含 Yahoo 的 ^VIX、DX-Y.NYB、CL=F
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,95}")  # obs_id、來源名、原因、種類
_STATE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-]{0,95}")  # task_state：框架自己的名字以 _ 開頭
_TW_CODE_RE = re.compile(r"[0-9A-Z]{4,6}")
_CHART_RE = re.compile(r"[0-9a-f]{32}\.png")  # 圖檔名一律 uuid4 hex，清理時才不會刪到別的東西
_HASH_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")


class StoreError(RuntimeError):
    """資料庫檔不安全、或 schema 比程式新。"""


class CloseRow(NamedTuple):
    date: date
    close: float
    volume: Optional[int]
    source: str
    provisional: bool


class SplitRow(NamedTuple):
    symbol: str
    execution_date: date
    new: float  # ratio「a : b」＝a 新股換 b 舊股
    old: float
    source: str


class ExRightRow(NamedTuple):
    # 欄位名和 market_sources_tw.ExRight 相同：market_calc.adjust_tw_closes 可以直接吃
    date: date
    code: str
    pre_close: float
    ref_price: float
    kind: str


# ── 驗證與轉換 ────────────────────────────────────────────────────────────────


def _check(pattern: re.Pattern[str], value: Any, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


def _market(value: Any) -> str:
    return _check(_MARKET_RE, value, "market")


def _symbol(value: Any) -> str:
    return _check(_SYMBOL_RE, value, "symbol")


def _ident(value: Any, what: str) -> str:
    return _check(_ID_RE, value, what)


def _text(value: Any, what: str, max_len: int, *, allow_empty: bool = False) -> str:
    """外部來源的短字串：不能有控制字元（存檔前就擋下壞資料）。"""
    if not isinstance(value, str) or len(value) > max_len or (not value and not allow_empty):
        raise ValueError(f"invalid {what}")
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value):
        raise ValueError(f"invalid {what}")
    return value


def _day(value: Any, what: str = "date") -> date:
    # datetime 也是 date 的子類別，但沒有時區就不知道是哪一天：一律拒收
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{what} must be a datetime.date")
    return value


def _positive(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{what} must be a number")
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{what} must be finite and > 0")
    return out


def _volume(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise ValueError("volume must be an int or None")
    out = int(value)
    if out < 0:
        raise ValueError("volume must be >= 0")
    return out


def _seconds(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(float(value)):
        raise ValueError(f"{what} must be a finite number")
    return float(value)


def _epoch(value: Any, what: str) -> float:
    """aware datetime、epoch 秒或帶時區的 ISO 字串 → epoch 秒。"""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{what} must be timezone-aware")
        return value.timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"invalid {what}") from None
        return _epoch(parsed, what)
    return _seconds(value, what)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("naive datetime is not stored")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _dumps(value: Any) -> str:
    # NaN／Infinity 不是合法 JSON，快照與卡片都不能有：寫入時就擋（呼叫端會記成 unexpected:ValueError）
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default, separators=(",", ":"))


def _taipei_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _TPE).date().isoformat()


def host_rule(host: str) -> tuple[str, HostRule]:
    """主機 → (帳戶名, 規則)。"""
    name = str(host).lower()
    rule = HOST_RULES.get(name, DEFAULT_HOST_RULE)
    return (rule.bucket or name), rule


def _bucket_rule(bucket: str) -> HostRule:
    for host, rule in HOST_RULES.items():
        if (rule.bucket or host) == bucket:
            return rule
    return DEFAULT_HOST_RULE


def _close_row(row: Any) -> tuple[date, float, Optional[int]]:
    # 收 (date, close, volume) tuple，也收 market_sources 的 Bar／TwBar（有 date、close、volume 屬性）
    if hasattr(row, "date") and hasattr(row, "close"):
        day, close, volume = row.date, row.close, getattr(row, "volume", None)
    else:
        day, close, volume = row[0], row[1], (row[2] if len(row) > 2 else None)
    return _day(day), _positive(close, "close"), _volume(volume)


def _prepare_rows(rows: Iterable[Any]) -> list[tuple[date, float, Optional[int]]]:
    prepared: dict[date, tuple[date, float, Optional[int]]] = {}
    for raw in rows:
        row = _close_row(raw)
        old = prepared.get(row[0])
        if old is not None and old != row:
            raise ValueError("conflicting rows for the same date")
        prepared[row[0]] = row
    return [prepared[day] for day in sorted(prepared)]


def _prepare_private_file(path: Path) -> None:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)  # 上層目錄不存在就丟 FileNotFoundError：不替呼叫端建 state/
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise StoreError("unsafe_db_file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS closes (
        market TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL,
        close REAL NOT NULL, volume INTEGER, source TEXT NOT NULL,
        provisional INTEGER NOT NULL DEFAULT 0, fetched_at REAL NOT NULL,
        PRIMARY KEY (market, symbol, date)) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS quarantine (
        id INTEGER PRIMARY KEY, market TEXT NOT NULL, symbol TEXT NOT NULL,
        from_date TEXT NOT NULL, to_date TEXT NOT NULL, reason TEXT NOT NULL,
        since REAL NOT NULL, cleared_at REAL)""",
    "CREATE INDEX IF NOT EXISTS quarantine_market ON quarantine (market, symbol)",
    """CREATE TABLE IF NOT EXISTS pending_jumps (
        market TEXT NOT NULL, symbol TEXT NOT NULL, info TEXT NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY (market, symbol))""",
    """CREATE TABLE IF NOT EXISTS splits (
        symbol TEXT NOT NULL, execution_date TEXT NOT NULL, new REAL NOT NULL, old REAL NOT NULL,
        source TEXT NOT NULL, seen_at REAL NOT NULL, PRIMARY KEY (symbol, execution_date))""",
    """CREATE TABLE IF NOT EXISTS full_refetch (
        market TEXT NOT NULL, symbol TEXT NOT NULL, on_date TEXT NOT NULL, at REAL NOT NULL,
        PRIMARY KEY (market, symbol))""",
    """CREATE TABLE IF NOT EXISTS exrights (
        code TEXT NOT NULL, date TEXT NOT NULL, pre_close REAL NOT NULL, ref_price REAL NOT NULL,
        kind TEXT NOT NULL, seen_at REAL NOT NULL, PRIMARY KEY (code, date))""",
    """CREATE TABLE IF NOT EXISTS coverage (
        source TEXT NOT NULL, from_date TEXT NOT NULL, to_date TEXT NOT NULL, fetched_at REAL NOT NULL,
        PRIMARY KEY (source, from_date))""",
    """CREATE TABLE IF NOT EXISTS obs (
        obs_id TEXT PRIMARY KEY, json TEXT, data_date TEXT, period TEXT,
        last_success_at REAL, checked_at REAL, fail_streak INTEGER NOT NULL DEFAULT 0, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS posts (
        kind TEXT NOT NULL, post_id TEXT NOT NULL, published_at REAL NOT NULL, json TEXT NOT NULL,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY (kind, post_id))""",
    "CREATE INDEX IF NOT EXISTS posts_time ON posts (kind, published_at)",
    """CREATE TABLE IF NOT EXISTS calendar_overrides (
        market TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT NOT NULL,
        confirmed INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (market, day))""",
    """CREATE TABLE IF NOT EXISTS fetch_log (
        host TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, last_at REAL, cooldown_until REAL,
        PRIMARY KEY (host, day))""",
    """CREATE TABLE IF NOT EXISTS task_state (
        name TEXT PRIMARY KEY, json TEXT NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS chart_files (
        name TEXT PRIMARY KEY, inputs_hash TEXT NOT NULL, created_at REAL NOT NULL, retired_at REAL)""",
)


class Store:
    """market_history.sqlite 的唯一入口（INTERFACES §9.1）。一個程序開一個，用完 close()。"""

    def __init__(self, path: Path, *, now_fn: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._now_fn = now_fn
        self._depth = 0
        _prepare_private_file(self.path)
        uri = "file:" + quote(str(self.path)) + "?mode=rw"  # mode=rw：sqlite 自己不建檔（權限由上面決定）
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        try:
            self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate()
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    # ── 連線與交易 ──

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("closed")
        return self._conn

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """把幾個寫入包成一個交易。巢狀呼叫用 SAVEPOINT：內層丟例外只退回內層，外層照樣決定要不要 commit。"""
        conn = self._db()
        if self._depth:
            name = f"sp{self._depth}"
            conn.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute(f"ROLLBACK TO {name}")
                    conn.execute(f"RELEASE {name}")
                raise
            else:
                conn.execute(f"RELEASE {name}")
            finally:
                self._depth -= 1
            return
        conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield conn
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            try:
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        finally:
            self._depth = 0

    def _migrate(self) -> None:
        version = self._db().execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError("schema_newer")  # 舊程式不要去改新版的表
        with self.transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _now(self) -> float:
        return float(self._now_fn())

    # ── 價格 ──

    _UPSERT_CLOSE = (
        "INSERT INTO closes (market, symbol, date, close, volume, source, provisional, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (market, symbol, date) DO UPDATE SET "
        "close = excluded.close, volume = excluded.volume, source = excluded.source, "
        "provisional = excluded.provisional, fetched_at = excluded.fetched_at"
    )

    def upsert_closes(
        self,
        market: str,
        symbol: str,
        rows: Iterable[Any],
        source: str,
        *,
        provisional: bool = False,
    ) -> None:
        """寫入或更新 (date, close, volume|None)。provisional=True 的列只蓋掉別的備援列，不蓋正式列。"""
        market, symbol, source = _market(market), _symbol(symbol), _ident(source, "source")
        prepared = _prepare_rows(rows)
        if not prepared:
            return
        sql = self._UPSERT_CLOSE + (" WHERE closes.provisional = 1" if provisional else "")
        now = self._now()
        flag = 1 if provisional else 0
        with self.transaction() as conn:
            conn.executemany(
                sql, [(market, symbol, d.isoformat(), c, v, source, flag, now) for d, c, v in prepared]
            )

    def replace_symbol(
        self,
        market: str,
        symbol: str,
        rows: Iterable[Any],
        source: str,
        *,
        refetch_on: Optional[date] = None,
    ) -> None:
        """一個交易內先刪掉這檔全部的列再寫入（分割／來源修訂的整段替換，PLAN §18.6）。

        rows 不能是空的：整段重抓回空表比較像抓壞了，不能拿它把歷史清光。refetch_on 有給就在同一個
        交易裡記下整段重抓的日期（mark_full_refetch）。
        """
        market, symbol, source = _market(market), _symbol(symbol), _ident(source, "source")
        prepared = _prepare_rows(rows)
        if not prepared:
            raise ValueError("replace_symbol needs at least one row")
        if refetch_on is not None:
            refetch_on = _day(refetch_on, "refetch_on")
        now = self._now()
        with self.transaction() as conn:
            conn.execute("DELETE FROM closes WHERE market = ? AND symbol = ?", (market, symbol))
            conn.executemany(
                self._UPSERT_CLOSE,
                [(market, symbol, d.isoformat(), c, v, source, 0, now) for d, c, v in prepared],
            )
            if refetch_on is not None:
                self._mark_full_refetch(conn, market, symbol, refetch_on, now)

    def closes(self, market: str, symbol: str, *, since: Optional[date] = None) -> list[CloseRow]:
        """依日期由舊到新：(date, close, volume, source, provisional)。"""
        sql = "SELECT date, close, volume, source, provisional FROM closes WHERE market = ? AND symbol = ?"
        params: list[Any] = [_market(market), _symbol(symbol)]
        if since is not None:
            sql += " AND date >= ?"
            params.append(_day(since, "since").isoformat())
        rows = self._db().execute(sql + " ORDER BY date", params).fetchall()
        return [
            CloseRow(date.fromisoformat(d), float(c), None if v is None else int(v), str(s), bool(p))
            for d, c, v, s, p in rows
        ]

    def last_date(self, market: str, symbol: str, *, include_provisional: bool = True) -> Optional[date]:
        sql = "SELECT MAX(date) FROM closes WHERE market = ? AND symbol = ?"
        if not include_provisional:
            sql += " AND provisional = 0"
        value = self._db().execute(sql, (_market(market), _symbol(symbol))).fetchone()[0]
        return None if value is None else date.fromisoformat(value)

    def symbols(self, market: str) -> list[str]:
        rows = self._db().execute(
            "SELECT DISTINCT symbol FROM closes WHERE market = ? ORDER BY symbol", (_market(market),)
        ).fetchall()
        return [r[0] for r in rows]

    def prune(self, market: str, keep_sessions: int) -> None:
        """每檔只留最新的 keep_sessions 列（PLAN §18.6：保留期 ≤ Nasdaq 一次能回的列數）。"""
        if isinstance(keep_sessions, bool) or not isinstance(keep_sessions, int) or keep_sessions < 1:
            raise ValueError("keep_sessions must be a positive int")
        market = _market(market)
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM closes WHERE market = ?1 AND (symbol, date) IN ("
                " SELECT symbol, date FROM (SELECT symbol, date, ROW_NUMBER() OVER ("
                "  PARTITION BY symbol ORDER BY date DESC) AS rn FROM closes WHERE market = ?1)"
                " WHERE rn > ?2)",
                (market, keep_sessions),
            )

    # ── 隔離區間與待確認跳空（PLAN §18.2） ──

    def add_quarantine(self, market: str, symbol: str, from_date: date, to_date: date, reason: str) -> None:
        market, symbol, reason = _market(market), _symbol(symbol), _ident(reason, "reason")
        start, end = _day(from_date, "from_date"), _day(to_date, "to_date")
        if start > end:
            raise ValueError("from_date is after to_date")
        args = (market, symbol, start.isoformat(), end.isoformat(), reason)
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM quarantine WHERE market = ? AND symbol = ? AND from_date = ? AND to_date = ?"
                " AND reason = ? AND cleared_at IS NULL",
                args,
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO quarantine (market, symbol, from_date, to_date, reason, since)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (*args, self._now()),
                )

    def clear_quarantine(self, market: str, symbol: str, reason: Optional[str] = None) -> None:
        """解除這檔還有效的隔離（reason 有給就只解除那個原因的）；留下紀錄（cleared_at）。"""
        sql = "UPDATE quarantine SET cleared_at = ? WHERE market = ? AND symbol = ? AND cleared_at IS NULL"
        params: list[Any] = [self._now(), _market(market), _symbol(symbol)]
        if reason is not None:
            sql += " AND reason = ?"
            params.append(_ident(reason, "reason"))
        with self.transaction() as conn:
            conn.execute(sql, params)

    def quarantine(self, market: str) -> dict[str, list[tuple[date, date, str]]]:
        """還有效的隔離區間：{symbol: [(from_date, to_date, reason), …]}（依起日排序）。"""
        rows = self._db().execute(
            "SELECT symbol, from_date, to_date, reason FROM quarantine WHERE market = ? AND cleared_at IS NULL"
            " ORDER BY symbol, from_date, to_date",
            (_market(market),),
        ).fetchall()
        out: dict[str, list[tuple[date, date, str]]] = {}
        for symbol, start, end, reason in rows:
            out.setdefault(symbol, []).append((date.fromisoformat(start), date.fromisoformat(end), reason))
        return out

    def pending_jump(self, market: str, symbol: str) -> Optional[dict]:
        row = self._db().execute(
            "SELECT info FROM pending_jumps WHERE market = ? AND symbol = ?", (_market(market), _symbol(symbol))
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def set_pending_jump(self, market: str, symbol: str, info: Optional[dict]) -> None:
        """info=None 刪掉這檔的待確認紀錄。"""
        market, symbol = _market(market), _symbol(symbol)
        with self.transaction() as conn:
            if info is None:
                conn.execute("DELETE FROM pending_jumps WHERE market = ? AND symbol = ?", (market, symbol))
                return
            if not isinstance(info, dict):
                raise ValueError("pending jump info must be a dict")
            conn.execute(
                "INSERT INTO pending_jumps (market, symbol, info, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (market, symbol) DO UPDATE SET info = excluded.info, updated_at = excluded.updated_at",
                (market, symbol, _dumps(info), self._now()),
            )

    # ── 事件：分割、整段重抓、除權息、查過的區間 ──

    def upsert_splits(self, rows: Iterable[Any], source: str) -> None:
        """(symbol, execution_date, new, old)，或有這幾個屬性的 Split。"""
        source = _ident(source, "source")
        prepared = []
        for row in rows:
            if hasattr(row, "execution_date"):
                values = (row.symbol, row.execution_date, row.new, row.old)
            else:
                values = tuple(row[:4])
            symbol, day, new, old = values
            prepared.append(
                (_symbol(symbol), _day(day, "execution_date").isoformat(), _positive(new, "new"), _positive(old, "old"))
            )
        if not prepared:
            return
        now = self._now()
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO splits (symbol, execution_date, new, old, source, seen_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (symbol, execution_date) DO UPDATE SET new = excluded.new, old = excluded.old,"
                " source = excluded.source, seen_at = excluded.seen_at",
                [(*row, source, now) for row in prepared],
            )

    def splits_since(self, symbol: str, after: date, *, until: Optional[date] = None) -> list[SplitRow]:
        """execution_date 在 (after, until] 的分割（until 沒給就不設上限），依日期排序。"""
        sql = "SELECT symbol, execution_date, new, old, source FROM splits WHERE symbol = ? AND execution_date > ?"
        params: list[Any] = [_symbol(symbol), _day(after, "after").isoformat()]
        if until is not None:
            sql += " AND execution_date <= ?"
            params.append(_day(until, "until").isoformat())
        rows = self._db().execute(sql + " ORDER BY execution_date", params).fetchall()
        return [SplitRow(s, date.fromisoformat(d), float(n), float(o), src) for s, d, n, o, src in rows]

    def _mark_full_refetch(self, conn: sqlite3.Connection, market: str, symbol: str, on: date, now: float) -> None:
        conn.execute(
            "INSERT INTO full_refetch (market, symbol, on_date, at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (market, symbol) DO UPDATE SET on_date = excluded.on_date, at = excluded.at",
            (market, symbol, on.isoformat(), now),
        )

    def mark_full_refetch(self, market: str, symbol: str, on: date) -> None:
        market, symbol, on = _market(market), _symbol(symbol), _day(on, "on")
        with self.transaction() as conn:
            self._mark_full_refetch(conn, market, symbol, on, self._now())

    def last_full_refetch(self, market: str, symbol: str) -> Optional[date]:
        row = self._db().execute(
            "SELECT on_date FROM full_refetch WHERE market = ? AND symbol = ?", (_market(market), _symbol(symbol))
        ).fetchone()
        return None if row is None else date.fromisoformat(row[0])

    def upsert_exrights(self, rows: Iterable[Any]) -> None:
        """market_sources_tw.ExRight（date、code、pre_close、ref_price、kind），或同順序的 tuple。"""
        prepared = []
        for row in rows:
            if hasattr(row, "ref_price"):
                values = (row.date, row.code, row.pre_close, row.ref_price, row.kind)
            else:
                values = tuple(row[:5])
            day, code, pre_close, ref_price, kind = values
            prepared.append(
                (
                    _check(_TW_CODE_RE, code, "code"),
                    _day(day).isoformat(),
                    _positive(pre_close, "pre_close"),
                    _positive(ref_price, "ref_price"),
                    _text(kind, "kind", 16),
                )
            )
        if not prepared:
            return
        now = self._now()
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO exrights (code, date, pre_close, ref_price, kind, seen_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (code, date) DO UPDATE SET pre_close = excluded.pre_close,"
                " ref_price = excluded.ref_price, kind = excluded.kind, seen_at = excluded.seen_at",
                [(*row, now) for row in prepared],
            )

    def exrights(self, code: str) -> list[ExRightRow]:
        rows = self._db().execute(
            "SELECT date, code, pre_close, ref_price, kind FROM exrights WHERE code = ? ORDER BY date",
            (_check(_TW_CODE_RE, code, "code"),),
        ).fetchall()
        return [ExRightRow(date.fromisoformat(d), c, float(p), float(r), k) for d, c, p, r, k in rows]

    def add_coverage(self, source: str, from_date: date, to_date: date) -> None:
        """記下查過的日期區間；重疊或相鄰（差一天）的區間合併成一段。"""
        source = _ident(source, "source")
        start, end = _day(from_date, "from_date"), _day(to_date, "to_date")
        if start > end:
            raise ValueError("from_date is after to_date")
        now = self._now()
        with self.transaction() as conn:
            spans = [
                (date.fromisoformat(a), date.fromisoformat(b))
                for a, b in conn.execute("SELECT from_date, to_date FROM coverage WHERE source = ?", (source,))
            ]
            spans.append((start, end))
            spans.sort()
            merged: list[list[date]] = []
            for a, b in spans:
                if merged and a <= merged[-1][1] + timedelta(days=1):
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            conn.execute("DELETE FROM coverage WHERE source = ?", (source,))
            conn.executemany(
                "INSERT INTO coverage (source, from_date, to_date, fetched_at) VALUES (?, ?, ?, ?)",
                [(source, a.isoformat(), b.isoformat(), now) for a, b in merged],
            )

    def coverage(self, source: str) -> list[tuple[date, date]]:
        rows = self._db().execute(
            "SELECT from_date, to_date FROM coverage WHERE source = ? ORDER BY from_date", (_ident(source, "source"),)
        ).fetchall()
        return [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in rows]

    # ── 觀測值（非價格的來源值；publish 讀） ──

    def put_obs(self, obs_id: str, payload: dict, *, data_date: Any, period: Optional[str] = None) -> None:
        """成功：寫值、last_success_at＝checked_at＝現在、fail_streak 歸零、清掉 error。

        data_date 通常是 "YYYY-MM-DD"（也收 date）；payload 存成 JSON（key 一律變成字串）。
        """
        obs_id = _ident(obs_id, "obs_id")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        if isinstance(data_date, date) and not isinstance(data_date, datetime):
            data_date = data_date.isoformat()
        if data_date is not None:
            data_date = _text(data_date, "data_date", 40)
        if period is not None:
            period = _text(period, "period", 64)
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO obs (obs_id, json, data_date, period, last_success_at, checked_at, fail_streak, error)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, NULL) ON CONFLICT (obs_id) DO UPDATE SET json = excluded.json,"
                " data_date = excluded.data_date, period = excluded.period,"
                " last_success_at = excluded.last_success_at, checked_at = excluded.checked_at,"
                " fail_streak = 0, error = NULL",
                (obs_id, _dumps(payload), data_date, period, now, now),
            )

    def obs_checked(self, obs_id: str, *, error_kind: str) -> None:
        """失敗：只更新 checked_at、fail_streak＋1、error；舊值與 last_success_at 原樣保留。"""
        obs_id, kind = _ident(obs_id, "obs_id"), _ident(error_kind, "error_kind")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO obs (obs_id, json, data_date, period, last_success_at, checked_at, fail_streak, error)"
                " VALUES (?, NULL, NULL, NULL, NULL, ?, 1, ?) ON CONFLICT (obs_id) DO UPDATE SET"
                " checked_at = excluded.checked_at, fail_streak = obs.fail_streak + 1, error = excluded.error",
                (obs_id, self._now(), kind),
            )

    def obs_not_yet(self, obs_id: str) -> None:
        """「資料還沒公布」：只更新 checked_at，不算失敗（PLAN §5）。"""
        obs_id = _ident(obs_id, "obs_id")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO obs (obs_id, json, data_date, period, last_success_at, checked_at, fail_streak, error)"
                " VALUES (?, NULL, NULL, NULL, NULL, ?, 0, NULL) ON CONFLICT (obs_id) DO UPDATE SET"
                " checked_at = excluded.checked_at",
                (obs_id, self._now()),
            )

    def get_obs(self, obs_id: str) -> Optional[dict]:
        """{payload, data_date, period, last_success_at, checked_at, fail_streak, error}；沒有這筆回 None。

        只失敗過、從沒成功的那筆 payload 是 None。
        """
        row = self._db().execute(
            "SELECT json, data_date, period, last_success_at, checked_at, fail_streak, error FROM obs WHERE obs_id = ?",
            (_ident(obs_id, "obs_id"),),
        ).fetchone()
        if row is None:
            return None
        payload, data_date, period, last_success, checked, streak, error = row
        return {
            "payload": None if payload is None else json.loads(payload),
            "data_date": data_date,
            "period": period,
            "last_success_at": last_success,
            "checked_at": checked,
            "fail_streak": int(streak),
            "error": error,
        }

    def obs_ids(self, prefix: str) -> list[str]:
        if not isinstance(prefix, str):
            raise ValueError("prefix must be a string")
        rows = self._db().execute(
            "SELECT obs_id FROM obs WHERE substr(obs_id, 1, ?) = ? ORDER BY obs_id", (len(prefix), prefix)
        ).fetchall()
        return [r[0] for r in rows]

    # ── 貼文與新聞的累積（PLAN §18.14：依 id 累積，從累積資料算 48 小時） ──

    @staticmethod
    def _post_fields(item: Any) -> tuple[str, float, dict]:
        data = dataclasses.asdict(item) if dataclasses.is_dataclass(item) and not isinstance(item, type) else item
        if not isinstance(data, dict):
            raise ValueError("post item must be a dict")
        # 新聞用 guid、川普用 original_id（market_sources_misc 的欄位名）；也收通用的 id
        post_id = next((data[k] for k in ("id", "original_id", "guid") if data.get(k) not in (None, "")), None)
        post_id = _text(str(post_id) if isinstance(post_id, int) else post_id, "post id", 512)
        if "published_utc" not in data:
            raise ValueError("post item needs published_utc")
        return post_id, _epoch(data["published_utc"], "published_utc"), data

    def upsert_posts(self, kind: str, items: Iterable[Any]) -> None:
        """同一個 id 再看到就更新內容（例如 removed 變了），保留第一次看到的時間；30 天前的自動清掉。"""
        kind = _ident(kind, "kind")
        prepared = [self._post_fields(item) for item in items]
        now = self._now()
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO posts (kind, post_id, published_at, json, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (kind, post_id) DO UPDATE SET published_at = excluded.published_at,"
                " json = excluded.json, last_seen = excluded.last_seen",
                [(kind, pid, ts, _dumps(data), now, now) for pid, ts, data in prepared],
            )
            conn.execute("DELETE FROM posts WHERE kind = ? AND published_at < ?", (kind, now - POSTS_KEEP_S))

    def posts(self, kind: str, *, since_utc: Any) -> list[dict]:
        """published_utc ≥ since_utc 的項目，新的在前。

        每則是存進來的 dict，另外保證有 id（字串）、published_utc（aware UTC datetime）、
        first_seen_at／last_seen_at（epoch 秒）；其他 datetime 欄位讀回來是 ISO 字串。
        """
        rows = self._db().execute(
            "SELECT post_id, published_at, json, first_seen, last_seen FROM posts WHERE kind = ? AND published_at >= ?"
            " ORDER BY published_at DESC, post_id",
            (_ident(kind, "kind"), _epoch(since_utc, "since_utc")),
        ).fetchall()
        out = []
        for post_id, published, payload, first_seen, last_seen in rows:
            item = json.loads(payload)
            item.update(
                id=post_id,
                published_utc=datetime.fromtimestamp(published, timezone.utc),
                first_seen_at=first_seen,
                last_seen_at=last_seen,
            )
            out.append(item)
        return out

    # ── 交易日曆覆寫（PLAN §18.3、§18.4；證交所休市表由 market_pipeline_tw 存成 obs） ──

    def set_calendar_override(
        self, market: str, day: date, kind: str, evidence: str, *, confirmed: bool = False
    ) -> None:
        """kind：closed（預期交易日其實沒開）、open（表上不是交易日但有開）或 revoked（撤銷，留紀錄）；
        evidence 寫依據的種類。"""
        market, day = _market(market), _day(day, "day")
        if kind not in OVERRIDE_KINDS:
            raise ValueError("override kind must be 'closed', 'open' or 'revoked'")
        evidence = _text(evidence, "evidence", 200)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO calendar_overrides (market, day, kind, evidence, confirmed, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (market, day) DO UPDATE SET kind = excluded.kind,"
                " evidence = excluded.evidence, confirmed = excluded.confirmed, updated_at = excluded.updated_at",
                (market, day.isoformat(), kind, evidence, 1 if confirmed else 0, self._now()),
            )

    def clear_calendar_override(self, market: str, day: date) -> None:
        """撤銷覆寫（例如颱風判定休市之後又看到資料，PLAN §18.4）。"""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM calendar_overrides WHERE market = ? AND day = ?",
                (_market(market), _day(day, "day").isoformat()),
            )

    def calendar_overrides(self, market: str) -> dict[date, dict]:
        """{day: {kind, evidence, confirmed, updated_at}}（暫定的也在內；交易日曆一律採用）。"""
        rows = self._db().execute(
            "SELECT day, kind, evidence, confirmed, updated_at FROM calendar_overrides WHERE market = ? ORDER BY day",
            (_market(market),),
        ).fetchall()
        return {
            date.fromisoformat(day): {"kind": kind, "evidence": evidence, "confirmed": bool(conf), "updated_at": at}
            for day, kind, evidence, conf, at in rows
        }

    # ── 帳本（market_http.Ledger） ──

    def ledger(
        self,
        now_fn: Optional[Callable[[], float]] = None,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        stop_fn: Optional[Callable[[], bool]] = None,
        max_wait_s: Optional[float] = None,
    ) -> "StoreLedger":
        """間隔不夠時在交易外等（最多等到該主機的間隔，stop_fn 成立就放棄、丟 FetchError("budget")）。"""
        return StoreLedger(self, now_fn or self._now_fn, sleep_fn=sleep_fn, stop_fn=stop_fn, max_wait_s=max_wait_s)

    def fetch_counts(self, now: Optional[float] = None) -> dict[str, dict]:
        """今天（台北日）各帳戶的次數、上限與冷卻到期時間（給 outcome 用；只有主機名與數字）。"""
        now = self._now() if now is None else float(now)
        day = _taipei_day(now)
        conn = self._db()
        out: dict[str, dict] = {}
        for host, count in conn.execute("SELECT host, count FROM fetch_log WHERE day = ?", (day,)):
            out[host] = {"count": int(count), "cap": _bucket_rule(host).daily_cap, "cooldown_until": None}
        for host, until in conn.execute("SELECT host, MAX(cooldown_until) FROM fetch_log GROUP BY host"):
            if until is not None and until > now:
                entry = out.setdefault(host, {"count": 0, "cap": _bucket_rule(host).daily_cap, "cooldown_until": None})
                entry["cooldown_until"] = float(until)
        return out

    # ── 排程狀態 ──

    def task_state(self, name: str) -> dict:
        row = self._db().execute(
            "SELECT json FROM task_state WHERE name = ?", (_check(_STATE_NAME_RE, name, "task name"),)
        ).fetchone()
        return {} if row is None else json.loads(row[0])

    def set_task_state(self, name: str, state: dict) -> None:
        name = _check(_STATE_NAME_RE, name, "task name")
        if not isinstance(state, dict):
            raise ValueError("task state must be a dict")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO task_state (name, json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET json = excluded.json, updated_at = excluded.updated_at",
                (name, _dumps(state), self._now()),
            )

    def task_names(self) -> list[str]:
        return [r[0] for r in self._db().execute("SELECT name FROM task_state ORDER BY name")]

    # ── 圖檔登記（PLAN §7、§19.5：只刪自己登記、已退役超過 72 小時的檔） ──

    def register_chart(self, name: str, inputs_hash: str, created_at: float) -> None:
        name, digest = _check(_CHART_RE, name, "chart name"), _check(_HASH_RE, inputs_hash, "inputs_hash")
        created = _seconds(created_at, "created_at")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO chart_files (name, inputs_hash, created_at, retired_at) VALUES (?, ?, ?, NULL)"
                " ON CONFLICT (name) DO UPDATE SET inputs_hash = excluded.inputs_hash,"
                " created_at = excluded.created_at, retired_at = NULL",
                (name, digest, created),
            )

    def retire_charts(self, keep: Iterable[str], now: float) -> None:
        """現行快照沒引用的圖記下退役時間；又被引用的圖取消退役。"""
        names = json.dumps(sorted({_check(_CHART_RE, n, "chart name") for n in keep}))
        when = _seconds(now, "now")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE chart_files SET retired_at = ? WHERE retired_at IS NULL"
                " AND name NOT IN (SELECT value FROM json_each(?))",
                (when, names),
            )
            conn.execute(
                "UPDATE chart_files SET retired_at = NULL WHERE retired_at IS NOT NULL"
                " AND name IN (SELECT value FROM json_each(?))",
                (names,),
            )

    def charts_to_delete(self, now: float, older_than: float) -> list[str]:
        """退役超過 older_than 秒的圖檔名（呼叫端刪檔前還要 lstat 確認是一般檔，刪完再 forget_chart）。"""
        cutoff = _seconds(now, "now") - _seconds(older_than, "older_than")
        rows = self._db().execute(
            "SELECT name FROM chart_files WHERE retired_at IS NOT NULL AND retired_at <= ? ORDER BY name", (cutoff,)
        ).fetchall()
        return [r[0] for r in rows]

    def forget_chart(self, name: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM chart_files WHERE name = ?", (_check(_CHART_RE, name, "chart name"),))

    def chart_files(self) -> dict[str, dict]:
        """{name: {inputs_hash, created_at, retired_at}}：publish 判斷輸入沒變就沿用舊圖。"""
        rows = self._db().execute("SELECT name, inputs_hash, created_at, retired_at FROM chart_files ORDER BY name")
        return {n: {"inputs_hash": h, "created_at": c, "retired_at": r} for n, h, c, r in rows}


class StoreLedger:
    """market_http.Ledger 的 SQLite 版：請求前預扣、間隔、每台北日上限、429／403 後冷卻。"""

    _MAX_ROUNDS = 5  # 等過間隔還不行（另一個寫入者剛好搶先）最多再試幾次

    def __init__(
        self,
        store: Store,
        now_fn: Callable[[], float],
        *,
        sleep_fn: Callable[[float], None],
        stop_fn: Optional[Callable[[], bool]],
        max_wait_s: Optional[float],
    ) -> None:
        self._store = store
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._stop_fn = stop_fn
        self._max_wait_s = max_wait_s

    def _guard(self) -> None:
        if self._store.in_transaction:
            raise StoreError("ledger used inside a store transaction")

    def reserve(self, host: str) -> None:
        bucket, rule = host_rule(host)
        limit = rule.min_interval_s if self._max_wait_s is None else min(self._max_wait_s, rule.min_interval_s)
        for _ in range(self._MAX_ROUNDS):
            wait = self._try_reserve(bucket, rule)
            if wait <= 0:
                return
            if wait > limit + 1e-9:
                raise FetchError("budget")
            self._wait(wait)
        raise FetchError("budget")

    def _try_reserve(self, bucket: str, rule: HostRule) -> float:
        """能預扣就記帳回 0；間隔還不夠回要等的秒數（不記帳）；冷卻中或超過上限丟 FetchError。"""
        self._guard()
        with self._store.transaction() as conn:
            now = float(self._now_fn())
            day = _taipei_day(now)
            last_at, cooldown_until = conn.execute(
                "SELECT MAX(last_at), MAX(cooldown_until) FROM fetch_log WHERE host = ?", (bucket,)
            ).fetchone()
            if cooldown_until is not None and now < cooldown_until:
                raise FetchError("cooldown")
            row = conn.execute("SELECT count FROM fetch_log WHERE host = ? AND day = ?", (bucket, day)).fetchone()
            if (int(row[0]) if row else 0) >= rule.daily_cap:
                raise FetchError("budget")
            if last_at is not None:
                # 最多等一個間隔：時鐘往回跳時也不會卡住好幾個小時
                wait = min(rule.min_interval_s, float(last_at) + rule.min_interval_s - now)
                if wait > 0:
                    return wait
            conn.execute(
                "INSERT INTO fetch_log (host, day, count, last_at, cooldown_until) VALUES (?, ?, 1, ?, NULL)"
                " ON CONFLICT (host, day) DO UPDATE SET count = fetch_log.count + 1, last_at = excluded.last_at",
                (bucket, day, now),
            )
            cutoff = (date.fromisoformat(day) - timedelta(days=FETCH_LOG_KEEP_DAYS)).isoformat()
            conn.execute(
                "DELETE FROM fetch_log WHERE host = ? AND day < ? AND (cooldown_until IS NULL OR cooldown_until <= ?)",
                (bucket, cutoff, now),
            )
            return 0.0

    def _wait(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0:
            if self._stop_fn is not None and self._stop_fn():
                raise FetchError("budget")  # 這輪時間到或要停了：不再等這個主機
            step = min(0.25, remaining)
            self._sleep_fn(step)
            remaining -= step

    def cooldown(self, host: str, seconds: float) -> None:
        span = _seconds(seconds, "seconds")
        if not 0 <= span <= MAX_COOLDOWN_S:
            raise ValueError("cooldown seconds out of range")
        bucket, _rule = host_rule(host)
        self._guard()
        with self._store.transaction() as conn:
            now = float(self._now_fn())
            conn.execute(
                "INSERT INTO fetch_log (host, day, count, last_at, cooldown_until) VALUES (?, ?, 0, NULL, ?)"
                " ON CONFLICT (host, day) DO UPDATE SET"
                " cooldown_until = MAX(COALESCE(fetch_log.cooldown_until, 0), excluded.cooldown_until)",
                (bucket, _taipei_day(now), now + span),
            )

    def status(self) -> dict[str, dict]:
        return self._store.fetch_counts(now=float(self._now_fn()))
