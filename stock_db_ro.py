"""盤勢 DB 的唯讀存取（2026-10-28，美股推薦與推薦成績用）。

`state/market_history.sqlite` 是盤勢 job（jobs/market_snapshot.py）的內部庫，單一寫入者、rollback journal
（不是 WAL）：讀者拿著 SHARED 鎖時寫者的 COMMIT 要等，沒讀完的 cursor 也會讓它的 COMMIT 失敗（10/28 審查實測）。
所以這裡只有一種用法：開（mode=ro、query_only）→ 一個 SELECT → fetchall → 立刻關。

錯誤分兩種：
- `DbBusy`：SQLITE_BUSY／SQLITE_LOCKED（寫者正在 commit）→ 呼叫端當「還沒好」，不算失敗。
- `DbUnavailable`：DB 不見、欄位或表不見、盤勢 job 被殺留下 hot journal（SQLITE_READONLY_ROLLBACK）、
  JSON 壞掉等其他情況。

不 import main、market_*（盤勢模組改 API 不會連帶壞；這裡只用 SQL）。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import quote

import stock_picks

DB_PATH_ENV = "LINE_BOT_MARKET_DB_PATH"  # staging 用；平常不設
DB_NAME = "market_history.sqlite"
DEFAULT_TIMEOUT_S = 1.0
_BUSY_PREFIXES = ("SQLITE_BUSY", "SQLITE_LOCKED")


class DbBusy(Exception):
    """寫者正在 commit：這次先不讀，等一下再試（不算失敗）。"""


class DbUnavailable(Exception):
    """讀不到（不見、壞掉、欄位改了）；只帶種類。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def db_path() -> Path:
    """呼叫時才算：測試把 stock_picks.STATE_DIR 換成 tmp 時也跟著換。"""
    override = (os.environ.get(DB_PATH_ENV) or "").strip()
    return Path(override) if override else stock_picks.STATE_DIR / DB_NAME


def _classify(exc: sqlite3.Error) -> Exception:
    name = str(getattr(exc, "sqlite_errorname", "") or "")
    if name.startswith(_BUSY_PREFIXES):
        return DbBusy(name)
    return DbUnavailable(name.lower() or type(exc).__name__.lower())


def rows(sql: str, params: Sequence = (), *, path: Optional[Path] = None, timeout: float = DEFAULT_TIMEOUT_S) -> list[tuple]:
    """跑一個 SELECT、整批讀完才回；連線一定關掉。只收 SELECT／WITH（防呆，不是安全邊界：連線本身是唯讀）。"""
    if not sql.lstrip()[:6].upper().startswith(("SELECT", "WITH")):
        raise ValueError("stock_db_ro only runs SELECT")
    target = path if path is not None else db_path()
    if not target.is_file():
        raise DbUnavailable("missing")
    uri = f"file:{quote(str(target))}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=timeout)
    except sqlite3.Error as exc:
        raise _classify(exc) from None
    try:
        with contextlib.closing(con):
            con.execute("PRAGMA query_only = ON")
            return con.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error as exc:
        raise _classify(exc) from None


def obs_json(obs_id: str, *, path: Optional[Path] = None) -> Any:
    """obs 表一列的 json 欄位（解析後）；沒有這列回 None，JSON 壞掉丟 DbUnavailable。"""
    found = rows("SELECT json FROM obs WHERE obs_id = ?", (obs_id,), path=path)
    if not found or found[0][0] is None:
        return None
    try:
        return json.loads(found[0][0])
    except (TypeError, ValueError, RecursionError):
        raise DbUnavailable("bad_json") from None


# ── 共用查詢（美股推薦與推薦成績都用；每個都是一個短 SELECT、參數化，代號清單分批） ────────────
US_CALENDAR_SYMBOLS = ("SPY", "VOO", "QQQ")  # 美股日曆：三檔 ETF 的日期聯集（盤勢 job 10/23 起也是看任一 ETF）
TW_CALENDAR_SYMBOL = "TAIEX"
TW_CLOSED_KINDS = ("closed", "settlement_only")  # 證交所休市表裡「沒有交易」的兩種
_IN_CHUNK = 400  # SQLite 參數上限遠高於這個；分批只是讓單一查詢保持短


def _chunks(items: Sequence[str]) -> list[list[str]]:
    unique = sorted({str(i) for i in items})
    return [unique[i:i + _IN_CHUNK] for i in range(0, len(unique), _IN_CHUNK)]


def _in(n: int) -> str:
    return ",".join("?" * n)


def us_calendar(since: str, *, path: Optional[Path] = None) -> list[str]:
    """美股交易日（美東日期字串，舊到新）：SPY／VOO／QQQ 任一檔有正式收盤的日期。"""
    found = rows(
        f"SELECT DISTINCT date FROM closes WHERE market = 'us' AND provisional = 0 AND date >= ?"
        f" AND symbol IN ({_in(len(US_CALENDAR_SYMBOLS))}) ORDER BY date",
        (since, *US_CALENDAR_SYMBOLS), path=path,
    )
    return [d for (d,) in found]


def closes(market: str, symbols: Sequence[str], since: str, *, path: Optional[Path] = None) -> dict[str, dict[str, float]]:
    """{代號: {日期: 正式收盤}}（provisional=0）；沒有資料的代號不在結果裡。"""
    out: dict[str, dict[str, float]] = {}
    for chunk in _chunks(symbols):
        for symbol, day, close in rows(
            f"SELECT symbol, date, close FROM closes WHERE market = ? AND provisional = 0 AND date >= ?"
            f" AND symbol IN ({_in(len(chunk))})",
            (market, since, *chunk), path=path,
        ):
            out.setdefault(symbol, {})[day] = close
    return out


def coverage_by_date(market: str, symbols: Sequence[str], since: str, *, path: Optional[Path] = None) -> dict[str, int]:
    """{日期: 這些代號裡有正式收盤的檔數}（推薦池到齊了沒有）。"""
    counts: dict[str, int] = {}
    for chunk in _chunks(symbols):
        for day, n in rows(
            f"SELECT date, count(*) FROM closes WHERE market = ? AND provisional = 0 AND date >= ?"
            f" AND symbol IN ({_in(len(chunk))}) GROUP BY date",
            (market, since, *chunk), path=path,
        ):
            counts[day] = counts.get(day, 0) + n
    return counts


def latest_closes(market: str, *, path: Optional[Path] = None) -> dict[str, tuple[str, float]]:
    """{代號: (最新一根正式收盤的日期, 收盤)}（美股單檔核對用：存進快照，回覆路徑就不碰 DB）。"""
    found = rows(
        "SELECT c.symbol, c.date, c.close FROM closes c JOIN ("
        " SELECT symbol, max(date) AS d FROM closes WHERE market = ? AND provisional = 0 GROUP BY symbol"
        ") m ON c.symbol = m.symbol AND c.date = m.d WHERE c.market = ? AND c.provisional = 0",
        (market, market), path=path,
    )
    return {symbol: (day, close) for symbol, day, close in found}


def active_quarantine(market: str, symbols: Sequence[str], *, path: Optional[Path] = None) -> dict[str, list[tuple[str, str]]]:
    """{代號: [(from_date, to_date), …]}：盤勢 job 判定有問題、還沒解除的區間（含分割待調整）。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for chunk in _chunks(symbols):
        for symbol, start, end in rows(
            f"SELECT symbol, from_date, to_date FROM quarantine WHERE market = ? AND cleared_at IS NULL"
            f" AND symbol IN ({_in(len(chunk))})",
            (market, *chunk), path=path,
        ):
            out.setdefault(symbol, []).append((start, end))
    return out


def splits_after(symbols: Sequence[str], after: str, *, path: Optional[Path] = None) -> dict[str, list[str]]:
    """{代號: [分割生效日, …]}：生效日 > after 的美股分割（Nasdaq 分割表）。"""
    out: dict[str, list[str]] = {}
    for chunk in _chunks(symbols):
        for symbol, day in rows(
            f"SELECT symbol, execution_date FROM splits WHERE execution_date > ? AND symbol IN ({_in(len(chunk))})",
            (after, *chunk), path=path,
        ):
            out.setdefault(symbol, []).append(day)
    return out


def tw_calendar(since: str, *, path: Optional[Path] = None) -> list[str]:
    """台股交易日（舊到新）：加權指數（TAIEX）有正式收盤的日期。"""
    found = rows(
        "SELECT date FROM closes WHERE market = 'tw' AND symbol = ? AND provisional = 0 AND date >= ? ORDER BY date",
        (TW_CALENDAR_SYMBOL, since), path=path,
    )
    return [d for (d,) in found]


TW_CLOSURE_RECHECK_DAYS = 10  # = market_pipeline_tw.CLOSURE_RECHECK_DAYS：暫定休市之後這幾天內，盤勢 job 看到資料就撤銷


def tw_closed_days(years: Sequence[int], *, today: str, path: Optional[Path] = None) -> Optional[set[str]]:
    """台股確定沒有交易的平日：證交所休市表（obs tw.holidays.YYYY 的 closed／settlement_only）＋盤勢 job
    判定的臨時休市（calendar_overrides 的 closed，例如颱風）。臨時休市是「暫定」：confirmed，或已經過了
    盤勢 job 會撤銷的 10 天，才算確定；revoked、open 不算。任一年的休市表讀不到、或有看不懂的列（unknown）→ None
    （呼叫端當作還不確定）。"""
    closed: set[str] = set()
    for year in sorted(set(years)):
        obj = obs_json(f"tw.holidays.{int(year)}", path=path)
        if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
            return None
        if obj.get("unknown"):  # 盤勢 job 看不懂的列（market_calendar.TW_HOLIDAY_UNKNOWN）：這一年的表先不信
            return None
        for row in obj["rows"]:
            if isinstance(row, dict) and row.get("kind") in TW_CLOSED_KINDS and isinstance(row.get("date"), str):
                closed.add(row["date"])
    try:
        today_d = date.fromisoformat(today)
    except (TypeError, ValueError):
        raise DbUnavailable("bad_today") from None
    for day, confirmed in rows(
        "SELECT day, confirmed FROM calendar_overrides WHERE market = 'tw' AND kind = 'closed'", path=path
    ):
        try:
            age = (today_d - date.fromisoformat(day)).days
        except (TypeError, ValueError):
            raise DbUnavailable("bad_override") from None
        if confirmed or age > TW_CLOSURE_RECHECK_DAYS:
            closed.add(day)
    return closed


def exrights(codes: Sequence[str], since: str, *, path: Optional[Path] = None) -> dict[str, list[tuple[str, float, float]]]:
    """{代號: [(除權息日, 除權息前收盤, 參考價), …]}（台股 TWT49U；日期舊到新）。"""
    out: dict[str, list[tuple[str, float, float]]] = {}
    for chunk in _chunks(codes):
        for code, day, pre, ref in rows(
            f"SELECT code, date, pre_close, ref_price FROM exrights WHERE date > ? AND code IN ({_in(len(chunk))})"
            " ORDER BY date",
            (since, *chunk), path=path,
        ):
            out.setdefault(code, []).append((day, pre, ref))
    return out


def coverage_to(source: str, *, path: Optional[Path] = None) -> Optional[str]:
    """盤勢 job 記的「這個來源已經抓到哪一天」（例如 twt49u：除權息資料涵蓋到哪一天）；沒有回 None。"""
    found = rows("SELECT max(to_date) FROM coverage WHERE source = ?", (source,), path=path)
    return found[0][0] if found and found[0][0] else None
