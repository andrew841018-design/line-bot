"""股票推薦回測 job（jobs/stock_picks_review*.py）測試共用的合成資料。這個檔沒有測試，只有建資料的函式。

repo 是公開的：不放真實快照，全部用 bot 自己的序列化函式（snapshot_to_json）組出縮小過的合成資料，
bot 的驗證函式才收得進去。比例照真實資料（PLAN v5 §9、審查 r3#1）：AI 名單 26 檔、其中 8 檔不在主池（沒有 tr）、
3 檔入選都在主池；主快照 universe 50 檔、pool 51 筆（多一筆常問股 0050）。
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import stock_picks as sp

TPE = ZoneInfo("Asia/Taipei")
T = date(2026, 10, 27)  # 週二
T_NEXT = date(2026, 10, 28)


def ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=TPE).timestamp()


MAIN_AT = ts(2026, 10, 28, 5, 20) + 0.25  # 主快照（T＝10/27）算出的時間
AI_AT = ts(2026, 10, 28, 6, 0) + 0.6  # AI（T＝10/27）
US_AT = ts(2026, 10, 28, 15, 0) + 0.4  # 美股（D＝10/27，台北 10/28 下午）
NOW = ts(2026, 10, 28, 18, 0)  # job 跑的時間：每一版都過了 10 分鐘寬限

AI_CODES = [str(2301 + i) for i in range(26)]
AI_IN_POOL = AI_CODES[:18]
AI_NOT_IN_POOL = AI_CODES[18:]  # 8 檔：主池沒有、走勢不知道
AI_PICKS = AI_CODES[:3]
OTHER_CODES = [str(1101 + i) for i in range(32)]
MAIN_UNIVERSE = AI_IN_POOL + OTHER_CODES  # 50 檔
V1_PICKS = OTHER_CODES[:3]
SEGMENT = "AI 伺服器"


def calendar(t: date) -> tuple[date, ...]:
    days = []
    day = t
    while len(days) < 240:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return tuple(reversed(days))


def close_of(code: str) -> float:
    return 50.0 + int(code) % 50 + 0.5  # 每檔不一樣，測得出 c0 取自 listed


GOOD_TREND = sp.Trend(close=100.0, ma60=100.0, ma240=90.0, p60=0)
GOOD_REVENUE = sp.Revenue(2026, 9, 50.0, 40.0)


def tw_facts(code: str, *, t: date = T, trend: Optional[sp.Trend] = GOOD_TREND, kind: str = sp.STOCK,
             cap: Optional[float] = 1e12) -> sp.Facts:
    stock = kind == sp.STOCK
    return sp.Facts(code=code, name=f"股{code}", kind=kind, data_date=t, trend=trend,
                    revenue=GOOD_REVENUE if stock else None, pe=10.0 if stock else None,
                    pe_median=20.0 if stock else None, inst_net=1000 if stock else None,
                    cap=cap if stock else None)


def listed_row(code: str, kind: str = sp.STOCK) -> sp.ListedRow:
    stock = kind == sp.STOCK
    return sp.ListedRow(name=f"股{code}", kind=kind, industry="01" if stock else "", close=close_of(code),
                        change=0.5, pe=10.0 if stock else None, revenue=GOOD_REVENUE if stock else None,
                        inst=1000 if stock else None)


def main_snapshot(t: date = T, *, computed_at: float = MAIN_AT, universe: Iterable[str] = MAIN_UNIVERSE,
                  extra_listed: Iterable[str] = AI_NOT_IN_POOL, picks: Iterable[str] = V1_PICKS) -> dict:
    universe = list(universe)
    pool = {code: tw_facts(code, t=t) for code in universe}
    pool["0050"] = tw_facts("0050", t=t, kind=sp.ETF)
    listed = {code: listed_row(code) for code in [*universe, *extra_listed]}
    listed["0050"] = listed_row("0050", sp.ETF)
    snap = sp.Snapshot(t=t, calendar=calendar(t), computed_at=computed_at, market=sp.Market(sp.NEUTRAL, -39, -4),
                       universe=tuple(universe), picks=tuple(picks), pool=pool, listed=listed,
                       medians={"01": 20.0}, otc=None, missed=None)
    return sp.snapshot_to_json(snap)


def ai_state(t: date = T, *, computed_at: float = AI_AT, codes: Iterable[str] = AI_CODES,
             picks: Iterable[str] = AI_PICKS, skipped: Optional[dict] = None, last_try: Optional[float] = None,
             rule: str = "ai_v1") -> dict:
    codes = list(codes)
    picks = list(picks)
    pick_rows = [dict(sp._facts_json(tw_facts(code, t=t, cap=None)), ai_source=SEGMENT) for code in picks]
    return {
        "schema": 1, "rule": rule, "t": t.isoformat(),
        "universe": {"as_of": "2026-10-24", "source": "core_only", "codes": codes, "etf_codes": [], "fetched_day": None},
        "picks": pick_rows,
        # 合成資料每檔條件 1–3 都過：漏斗和 job 重算的一致（funnel_match）
        "funnel": {"universe": len(codes), "growth": len(codes), "cheap": len(codes), "trend": len(picks)},
        "computed_at": computed_at, "last_try": computed_at if last_try is None else last_try, "fail_count": 0,
        "core_as_of": "2026-10-24", "core_version": "test-core-v1", "holdings": None,
        "fetch_budget": {"day": "2026-10-28", "attempts": 1, "next_try": computed_at + 7200},
        "skipped": dict(skipped or {}),
    }


US_SYMBOLS = [chr(65 + i // 26) + chr(65 + i % 26) + "X" for i in range(49)] + ["BRK.B"]  # 50 檔
US_GRAY_WITH_LATEST = US_SYMBOLS[10]  # ⚪：DB 沒有 D 收盤，快照 latest_closes 有
US_GRAY_NO_CLOSE = US_SYMBOLS[11]  # ⚪：哪裡都沒有 D 收盤
US_PICKS = US_SYMBOLS[:3]


def us_snapshot(d: date = T, *, computed_at: float = US_AT, latest_bump: float = 0.0) -> dict:
    import stock_picks_us as us

    quarter = date(2026, 9, 30)
    facts = []
    for i, symbol in enumerate(US_SYMBOLS):
        gray = symbol in (US_GRAY_WITH_LATEST, US_GRAY_NO_CLOSE)
        facts.append(us.UsFacts(
            symbol, f"CO {symbol.replace('.', ' ')}", us.STOCK, d,
            None if gray else us.UsTrend(close=100.0 + i, ma60=100.0 + i, ma240=90.0, p60=0),
            growth=us.Growth(quarter, 120), profit=us.Profit(quarter, 0, 0), pe=us.Pe(us.PE_OK, 252),
            fund=us.FUND_OK, weight=10.0 - i * 0.1, gray="missing" if gray else ""))
    latest = {symbol: (d, 100.0 + i + latest_bump) for i, symbol in enumerate(US_SYMBOLS) if symbol != US_GRAY_NO_CLOSE}
    snap = us.UsSnapshot(d=d, rule=us.RULE, covered=48, computed_at=computed_at, universe_as_of=d - timedelta(days=3),
                         spy=sp.Market(sp.NEUTRAL, 10, 5), spy_dates=(), pool=tuple(facts), picks=tuple(US_PICKS),
                         missed=None, latest_closes=latest)
    return us.snapshot_to_json(snap)


# ── 檔案 ───────────────────────────────────────────────────────────────────
def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def write_states(state_dir: Path, *, main: Optional[dict] = None, ai: Optional[dict] = None,
                 us: Optional[dict] = None) -> None:
    for name, obj in (("stock_picks_tw.json", main), ("stock_picks_tw_ai.json", ai), ("stock_picks_us.json", us)):
        if obj is not None:
            write_json(state_dir / name, obj)


def write_gz(path: Path, obj: dict) -> None:
    path.write_bytes(gzip.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8")))


MARKET_SCHEMA = (
    "CREATE TABLE closes (market TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,"
    " volume INTEGER, source TEXT NOT NULL, provisional INTEGER NOT NULL DEFAULT 0, fetched_at REAL NOT NULL,"
    " PRIMARY KEY (market, symbol, date)) WITHOUT ROWID",
    "CREATE TABLE obs (obs_id TEXT PRIMARY KEY, json TEXT, data_date TEXT, period TEXT, last_success_at REAL,"
    " checked_at REAL, fail_streak INTEGER NOT NULL DEFAULT 0, error TEXT)",
)


def make_market_db(path: Path, closes: Iterable[tuple] = (), obs: Optional[dict] = None) -> Path:
    """盤勢 DB 的縮小版：closes(market, symbol, date, close[, provisional]) 與 obs。"""
    con = sqlite3.connect(path)
    try:
        for sql in MARKET_SCHEMA:
            con.execute(sql)
        rows = []
        for row in closes:
            market, symbol, day, close = row[:4]
            provisional = row[4] if len(row) > 4 else 0
            rows.append((market, symbol, day, close, None, "test", provisional, 0.0))
        con.executemany("INSERT INTO closes VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        for obs_id, obj in (obs or {}).items():
            con.execute("INSERT INTO obs (obs_id, json) VALUES (?, ?)", (obs_id, json.dumps(obj)))
        con.commit()
    finally:
        con.close()
    return path


def weekdays(end: date, n: int) -> list[str]:
    days = []
    day = end
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return sorted(days)


def standard_market_rows(end: date = T, n: int = 30) -> list[tuple]:
    """TAIEX、0050、AI 名單與舊規則池（tw＝前 18 檔、tw_all＝全部）、美股三檔 ETF 與池（us）的收盤。"""
    rows = []
    days = weekdays(end, n)
    for day in days:
        rows.append(("tw", "TAIEX", day, 20000.0))
        rows.append(("tw", "0050", day, 100.0))
        rows.append(("tw_all", "0050", day, 100.0))
        for code in MAIN_UNIVERSE[:18]:
            rows.append(("tw", code, day, close_of(code)))
        for code in [*MAIN_UNIVERSE, *AI_NOT_IN_POOL]:
            if day >= days[-10]:  # tw_all 只留最近 10 個交易日
                rows.append(("tw_all", code, day, close_of(code)))
        for symbol in ("SPY", "VOO", "QQQ"):
            rows.append(("us", symbol, day, 500.0))
        for i, symbol in enumerate(US_SYMBOLS):
            if symbol not in (US_GRAY_WITH_LATEST, US_GRAY_NO_CLOSE):
                rows.append(("us", symbol, day, 100.0 + i))
    return rows


def listing(*roots: Path) -> list[tuple[str, int]]:
    """遞迴列出檔案（相對路徑、大小）；dry-run「什麼都不建」用。"""
    out = []
    for root in roots:
        for base, dirs, files in os.walk(root):
            for name in dirs + files:
                full = Path(base) / name
                out.append((str(full.relative_to(root.parent)), full.stat().st_size if full.is_file() else -1))
    return sorted(out)
