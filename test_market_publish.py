"""市場盤勢（/盤勢，2026-10-17）publish 步驟的測試。

用真的 market_store.Store（暫存目錄）塞合成資料：幾檔美股成分與 ETF、一小組台股、期權鏈（用 F2 自己的
chain_payload 序列化）、宏觀（Yahoo 與 ETF 代理）、事件、新聞、川普貼文、預測市場；時間一律注入 now
（週六 2026-10-17 16:31 台北），畫圖用假的模組（計數、寫 PNG 標頭）。不連網、不碰正式 state/ 與 /tmp。
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import market_calc
import market_calendar
import market_paths
import market_publish
import market_store
from jobs.market_snapshot import sanitize as snapshot_job_sanitize

TPE = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
WORK = Path(__file__).resolve().parent
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def at_tpe(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> float:
    return datetime(y, m, d, hh, mm, ss, tzinfo=TPE).timestamp()


def at_et(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


NOW = at_tpe(2026, 10, 17, 16, 31)  # 週六 16:31（台北）
D = date(2026, 10, 16)  # 週五：美股 D、台股 T
SESSIONS = 290

US_2025_CLOSED = {date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25)}
TW_2025_CLOSED = {date(2025, 10, 6), date(2025, 10, 10), date(2025, 12, 25)}
TW_2026_HOLIDAYS = [
    ("2026-01-01", "closed"), ("2026-02-12", "settlement_only"), ("2026-02-13", "settlement_only"),
    ("2026-02-16", "closed"), ("2026-02-17", "closed"), ("2026-02-18", "closed"), ("2026-02-19", "closed"),
    ("2026-02-20", "closed"), ("2026-02-27", "closed"), ("2026-04-03", "closed"), ("2026-04-06", "closed"),
    ("2026-05-01", "closed"), ("2026-06-19", "closed"), ("2026-09-25", "closed"), ("2026-09-28", "closed"),
    ("2026-10-09", "closed"), ("2026-10-26", "closed"), ("2026-12-25", "closed"),
]
_TW_CLOSED = {date.fromisoformat(day) for day, _ in TW_2026_HOLIDAYS} | TW_2025_CLOSED

VOO_MEMBERS = ("AAA", "BBB", "CCC", "DDD", "EEE")
NDX_MEMBERS = ("AAA", "BBB", "FFF", "GGG", "HHH")
SOXX_MEMBERS = ("CCC", "GGG", "HHH", "III", "JJJ")
US_MEMBERS = tuple(sorted(set(VOO_MEMBERS) | set(NDX_MEMBERS) | set(SOXX_MEMBERS)))
TW_MEMBERS = ("2330", "2317", "2454", "2308", "2382")
US_ETF_BASE = {"SPY": 600.0, "VOO": 550.0, "QQQ": 700.0, "SOXX": 250.0}


def sessions(market: str, end: date, count: int) -> list[date]:
    out: list[date] = []
    day = end
    while len(out) < count:
        closed = day in market_calendar.NYSE_HOLIDAYS or day in US_2025_CLOSED if market == "us" else day in _TW_CLOSED
        if day.weekday() < 5 and not closed:
            out.append(day)
        day -= timedelta(days=1)
    return out[::-1]


US_DAYS = sessions("us", D, SESSIONS)
TW_DAYS = sessions("tw", D, SESSIONS)


def price_rows(days: list[date], base: float, *, phase: float = 0.0, volume: int = 1_000_000) -> list[tuple]:
    rows = []
    for i, day in enumerate(days):
        close = base * (1 + 0.0006 * i) * (1 + 0.03 * math.sin(i / 9.0 + phase))
        rows.append((day, round(close, 4), volume + 1000 * i))
    return rows


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeCharts:
    """假的 market_charts：記下每次呼叫，寫一個 PNG 標頭；fail＝那一種圖丟 TypeError。"""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    def _draw(self, kind: str, out_path: Path, kwargs: dict) -> None:
        self.calls.append((kind, kwargs))
        if self.fail == kind:
            raise TypeError("boom")
        Path(out_path).write_bytes(PNG)

    def render_oi_chart(self, out_path: Path, **kwargs) -> None:
        self._draw("oi", out_path, kwargs)

    def render_gex_chart(self, out_path: Path, **kwargs) -> None:
        self._draw("gex", out_path, kwargs)


# ── 合成資料 ──────────────────────────────────────────────────────────────────


def option_chain(symbol: str, spot: float, *, scale: float = 1.0):
    """三個到期（週選、11/20 月選、12 月）、現價 ±20% 每 1% 一檔；買權在 +5%、賣權在 −5% 堆高 OI。"""
    import market_sources_us as src

    contracts = []
    for expiry in (date(2026, 10, 23), date(2026, 11, 20), date(2026, 12, 18)):
        years = (expiry - D).days / 365.0
        for step in range(-20, 21):
            strike = round(spot * (1 + step / 100.0), 2)
            for kind in ("C", "P"):
                oi = 1000 + 40 * (20 - abs(step))
                if kind == "C" and step == 5:
                    oi = 9000
                if kind == "P" and step == -5:
                    oi = 8000
                gamma = float(market_calc.bs_gamma(spot, strike, years, 0.2, 0.04))
                code = f"{symbol}{expiry:%y%m%d}{kind}{int(round(strike * 1000)):08d}"
                contracts.append(src.OptionContract(code, expiry, kind, strike, int(oi * scale), 25, 0.2, gamma, 0.5))
    return src.OptionChain(
        timestamp_utc=datetime(2026, 10, 16, 20, 33, 11, tzinfo=UTC),
        spot=spot,
        iv30=20.8,
        contracts=contracts,
        raw_count=len(contracts),
        bad_count=0,
        conflict_count=0,
        conflict_oi_known=True,
        quote_last_trade_et=datetime(2026, 10, 16, 16, 15, 1, tzinfo=ET),
        symbol=symbol,
        close=spot,
        volume=24_000_000,
    )


def cpi_series() -> dict:
    """合成的 CPI 四個序列 2025-01…2026-09（每月 +0.2%；2025-10 缺值，和 BLS 停擺那個月一樣）。"""
    out = {}
    for series_id, base in zip(market_calc.CPI_SERIES, (300.0, 301.0, 305.0, 306.0)):
        points = {}
        for index in range(21):
            year, month = 2025 + index // 12, index % 12 + 1
            if (year, month) != (2025, 10):
                points[f"{year}-{month:02d}"] = round(base * 1.002 ** index, 3)
        out[series_id] = points
    return out


def seed_us(store: market_store.Store, *, missing_on_d: tuple[str, ...] = ()) -> None:
    for i, (symbol, base) in enumerate(US_ETF_BASE.items()):
        rows = price_rows(US_DAYS, base, phase=i)
        last = rows[-1]
        rows[-1] = (last[0], last[1], int(last[2] * 1.6))  # D 放量
        store.upsert_closes("us", symbol, rows, "nasdaq")
    for i, symbol in enumerate(US_MEMBERS):
        rows = price_rows(US_DAYS, 50.0 + 10 * i, phase=0.7 * i)
        if symbol in missing_on_d:
            rows = rows[:-1]
        store.upsert_closes("us", symbol, rows, "nasdaq")
    store.put_obs("us.holdings.SPY", {"source": "SSGA", "as_of": "2026-10-15", "count": 5,
                                      "rows": [[s, f"{s} Corp", 10.0 - i] for i, s in enumerate(VOO_MEMBERS)]},
                  data_date="2026-10-15")
    store.put_obs("us.holdings.NDX", {"source": "Nasdaq", "as_of": "2026-10-16", "count": 5,
                                      "rows": [[s, f"{s} Inc", 9.0 - i] for i, s in enumerate(NDX_MEMBERS)],
                                      "market_caps": {s: 2e11 for s in NDX_MEMBERS}},
                  data_date="2026-10-16")
    store.put_obs("us.holdings.SOXX", {"source": "MoneyDJ", "as_of": "2026-09-30", "count": 5, "dropped_count": 1,
                                       "unmapped_count": 2, "unmapped_weight": 6.06,
                                       "rows": [[s, f"{s} Semi", 8.0 - i] for i, s in enumerate(SOXX_MEMBERS)]},
                  data_date="2026-09-30")
    for etf, members, list_obs, as_of in (("VOO", VOO_MEMBERS, "us.holdings.SPY", "2026-10-15"),
                                          ("QQQ", NDX_MEMBERS, "us.holdings.QQQ", "2026-09-30"),
                                          ("SOXX", SOXX_MEMBERS, "us.holdings.SOXX", "2026-09-30")):
        rows = [{"name": f"{s} Corp", "symbols": [s], "weight": 9.0 - i, "pick": s} for i, s in enumerate(members)]
        store.put_obs(f"us.top10.{etf}", {"source": "x", "list_obs": list_obs, "list_as_of": as_of, "rows": rows},
                      data_date=as_of)
    for i, symbol in enumerate(US_MEMBERS):
        store.put_obs(f"us.13f.sym.{symbol}", {
            "own_pct": 60.0 + i, "increased": 1500 + 10 * i, "decreased": 1400 + 20 * i, "held": 300, "new": 80,
            "soldout": 60, "holder_periods": ["2026-06-30"] * 7 + ["2025-12-31"] * 3, "total_holders": 4000,
            "holder_period_mode": "2026-06-30"}, data_date=None, period="2026-06-30")
        store.put_obs(f"us.fund.sym.{symbol}", {
            "quarters": [["2025-09-30", 900.0], ["2025-12-31", 1000.0], ["2026-03-31", 1100.0], ["2026-06-30", 1200.0]],
            "market_cap_usd": 1e8 * (i + 1), "market_cap_source": "Nasdaq-100 名單"}, data_date="2026-06-30")
    import market_pipeline_us as pipe

    for symbol, spot in (("QQQ", 700.0), ("SPY", 600.0), ("SOXX", 250.0)):
        store.put_obs(f"us.options.{symbol}", pipe.chain_payload(option_chain(symbol, spot)), data_date="2026-10-16")
    # VXTLT（2026-10-24 第 9 輪 A12）和 VIX、VIX3M 同一個形狀（us.vix 那一批一起抓）：約 15、日變化 −4.67%
    for symbol, close, change, pct in (("VIX", 19.99, 3.53, 17.66), ("VIX3M", 21.5, 1.2, 17.66), ("VXTLT", 15.09, -0.74, -4.67)):
        store.put_obs(f"us.quote.{symbol}", {
            "close": close, "price_change": change, "price_change_pct": pct, "last_trade_et": "2026-10-16T16:15:01-04:00",
            "timestamp_utc": "2026-10-17T01:37:16+00:00",
            "history": [["2026-10-15", round(close - change, 2)], ["2026-10-16", close]]}, data_date="2026-10-16")
    store.put_obs("us.ust", {"rows": [["2026-10-14", 4.27, 3.9, 5.07, 5.3], ["2026-10-15", 4.25, 3.9, 5.05, 5.3],
                                      ["2026-10-16", 4.26, 3.91, 5.09, 5.31]]}, data_date="2026-10-16")
    # 損益兩平（2026-10-24）：名目與實質各 6 個共同日期（10/12 債市休市、沒有那天）
    store.put_obs("us.infl.be", {"rows": [
        {"date": day, "n5": n5, "n10": n10, "r5": r5, "r10": r10} for day, n5, n10, r5, r10 in (
            ("2026-10-08", 4.99, 5.22, 2.62, 2.87), ("2026-10-09", 5.02, 5.24, 2.68, 2.91),
            ("2026-10-13", 5.00, 5.21, 2.67, 2.88), ("2026-10-14", 5.00, 5.19, 2.64, 2.84),
            ("2026-10-15", 4.91, 5.10, 2.58, 2.78), ("2026-10-16", 4.89, 5.09, 2.52, 2.74))]}, data_date="2026-10-16")
    # BLS CPI 與核心 CPI（2026-10-24）：取得日 10/16（台北）
    store.put_obs("us.infl.cpi", {"series": cpi_series(), "retrieved": "2026-10-16"}, data_date="2026-09-30", period="2026-09")
    for key, lev in (("es", (300_000, 500_000)), ("nq", (90_000, 70_000))):
        store.put_obs(f"us.cftc.{key}", {
            "market": "S&P 500 CONSOLIDATED", "report_date": "2026-10-13", "oi": 2_000_000, "dealer_l": 1, "dealer_s": 1,
            "am_l": 900_000, "am_s": 200_000, "lev_l": lev[0], "lev_s": lev[1], "other_l": 1, "other_s": 1,
            "chg_lev_l": 5_000, "chg_lev_s": 2_000, "chg_am_l": 1_000, "chg_am_s": 3_000, "basis": "consolidated"},
            data_date="2026-10-13")
    store.put_obs("us.margin", {"rows": [["2025-09", 1_200_000.0], ["2026-08", 1_450_000.0], ["2026-09", 1_480_054.0]]},
                  data_date="2026-09-30", period="2026-09")
    store.put_obs("us.events.fomc", {"meetings": [
        {"start": "2026-09-15", "end": "2026-09-16", "decision_utc": "2026-09-16T18:00:00+00:00"},
        {"start": "2026-10-27", "end": "2026-10-28", "decision_utc": "2026-10-28T18:00:00+00:00"}]},
        data_date="2026-10-17")
    store.put_obs("us.events.econ", {"days": {
        "2026-10-19": [["2026-10-19T12:30:00+00:00", "Empire State Manufacturing Survey"]],
        "2026-10-21": [["2026-10-21T12:30:00+00:00", "Consumer Price Index (MoM)"],
                       ["2026-10-21T12:30:00+00:00", "Core CPI (YoY)"], [None, "Untimed Thing"]]}},
        data_date="2026-10-17")
    store.put_obs("us.events.earnings", {"symbols": {"AAA": {"date": "2026-10-29", "estimated": True}}},
                  data_date="2026-10-17")


def seed_tw(store: market_store.Store) -> None:
    store.put_obs("tw.holidays.2026", {"year": 2026, "unknown": [], "rows": [
        {"date": day, "name": "休市", "note": "依規定放假", "kind": kind} for day, kind in TW_2026_HOLIDAYS]},
        data_date=None, period="2026")
    taiex = price_rows(TW_DAYS, 45_000.0)
    store.upsert_closes("tw", "TAIEX", [(d, c, 500_000_000_000 + 1_000_000_000 * i) for i, (d, c, _) in enumerate(taiex)],
                        "twse")
    store.upsert_closes("tw", "0050", price_rows(TW_DAYS, 190.0, phase=1.0, volume=30_000_000), "twse")
    for i, code in enumerate(TW_MEMBERS):
        store.upsert_closes("tw", code, price_rows(TW_DAYS, 100.0 + 200 * i, phase=0.5 * i), "twse")
    ex_day = TW_DAYS[-30]
    store.upsert_exrights([(ex_day, "2330", 1000.0, 995.0, "息")])
    store.add_coverage("twt49u", TW_DAYS[0], D)
    store.put_obs("tw.holdings.0050", {"source": "moneydj", "as_of": "2026-10-16", "count": 5, "dropped_count": 0,
                                       "unmapped_weight": 0.0, "rows": [[c, f"台股{c}", 10.0] for c in TW_MEMBERS]},
                  data_date="2026-10-16")
    allbut = {d.isoformat(): {"taiex_close": 45000.0, "taiex_change": 120.0, "turnover_twd": 5e11, "adv": 684,
                              "dec": 200, "flat": 80, "adv_limit": 19, "dec_limit": 3, "no_compare": 5}
              for d in TW_DAYS[-3:]}
    store.put_obs("tw.allbut", {"days": allbut, "latest": D.isoformat()}, data_date=D.isoformat())
    inst = {d.isoformat(): {"foreign": -3_000_000_000, "trust": 1_000_000_000, "dealer_self": 100_000_000,
                            "dealer_hedge": -50_000_000} for d in TW_DAYS[-5:]}
    store.put_obs("tw.inst3", {"days": inst, "latest": D.isoformat()}, data_date=D.isoformat())
    store.put_obs("tw.margin", {"date": D.isoformat(), "margin_balance_k": 350_000_000, "margin_prev_k": 349_000_000,
                                "short_units": 1000, "short_prev_units": 900}, data_date=D.isoformat())
    store.put_obs("tw.futures", {"date": D.isoformat(), "foreign": {
        "TX": {"oi_long": 10, "oi_short": 97_592, "oi_net": -97_582, "trade_net": 100},
        "MTX": {"oi_long": 1, "oi_short": 3_929, "oi_net": -3_928, "trade_net": 1},
        "TMF": {"oi_long": 1, "oi_short": 12_416, "oi_net": -12_415, "trade_net": 1}}}, data_date=D.isoformat())
    store.put_obs("tw.options_inst", {"date": D.isoformat(),
                                      "foreign_call": {"oi_long": 5, "oi_short": 3, "oi_net": 2, "oi_value_net_k": 1},
                                      "foreign_put": {"oi_long": 4, "oi_short": 6, "oi_net": -2, "oi_value_net_k": -1}},
                  data_date=D.isoformat())
    store.put_obs("tw.pcr", {"date": D.isoformat(), "rows": [
        {"date": D.isoformat(), "put_volume": 1, "call_volume": 1, "volume_ratio_pct": 90.0, "put_oi": 1, "call_oi": 1,
         "oi_ratio_pct": 60.84}]}, data_date=D.isoformat())
    spot = taiex[-1][1]
    txo = []
    for step in range(-60, 61):
        strike = float(round(spot / 100) * 100 + 100 * step)
        for kind in ("C", "P"):
            oi = 3000 if (kind == "C" and step == 20) or (kind == "P" and step == -20) else 500
            txo.append(["202610", strike, kind, oi, 10.0])
    store.put_obs("tw.txo", {"date": D.isoformat(), "fields": ["month", "strike", "kind", "oi", "settle"], "rows": txo},
                  data_date=D.isoformat())
    store.put_obs("tw.fx", {"date": D.isoformat(), "usd_ntd": 32.338, "prev_date": "2026-10-15", "prev_usd_ntd": 32.2},
                  data_date=D.isoformat())
    store.put_obs("tw.pe", {"date": D.isoformat(), "pe": {"2330": 25.1, "2317": 14.0, "2454": 20.0, "2308": 30.0,
                                                           "2382": None}}, data_date=D.isoformat())
    store.put_obs("tw.revenue", {"period": "2026-09", "rows": {
        "2330": [2026, 9, 30.5], "2317": [2026, 9, -2.0], "2454": [2026, 9, 10.0], "2308": [2026, 9, 5.0],
        "2382": [2026, 8, 1.0]}}, data_date="2026-09-30", period="2026-09")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def macro_payload(value: float, chg: float | None, source: str, symbol: str, day: str = "2026-10-16") -> dict:
    return {"value": value, "chg": chg, "chg_pct": chg / (value - chg) * 100 if chg is not None else None, "date": day,
            "prev_date": "2026-10-15" if chg is not None else None, "source": source, "symbol": symbol,
            "bars": [["2026-10-15", value - (chg or 0.0)], [day, value]]}


def election_quote(platform: str, bid: float, ask: float, *, fetched: float, result: str | None = None) -> dict:
    return {"platform": platform, "market_id": "x", "outcome": "Democratic Party", "bid": bid, "ask": ask,
            "mid": round((bid + ask) / 2, 6), "last": ask, "updated_utc": "2026-10-17T00:00:00+00:00", "active": True,
            "closed": result is not None, "result": result, "fetched_utc": iso(fetched)}


def taco_obs(last: date = D, *, days: int = 120, value: float = 9.0005) -> dict:
    """market_pipeline_taco 存的格式（川普壓力指數，2026-10-24；合成：到 last 為止的美股交易日各一筆）。"""
    rows = [[day.isoformat(), round(6.0 + 5.0 * math.sin(i / 7.0), 4)] for i, day in enumerate(sessions("us", last, days))]
    rows[-1][1] = value
    return {"as_of": last.isoformat(), "value": value, "change20d": -3.6, "status_key": "high",
            "method": "persistent-pressure-new4-v1", "history": rows}


def seed_misc(store: market_store.Store) -> None:
    """market_pipeline_misc 的格式：宏觀一格一筆（BTC 也是，2026-10-23 拿掉 Coinbase）、新聞是 obs、川普是累積的貼文＋摘要。"""
    store.put_obs("macro.DXY", macro_payload(99.4, 0.3, "Yahoo", "DX-Y.NYB"), data_date="2026-10-16")
    store.put_obs("macro.WTI", macro_payload(71.5, -0.5, "ETF 代理：USO", "USO"), data_date="2026-10-16")
    store.put_obs("macro.USDTWD", macro_payload(32.338, 0.138, "代理：期交所匯率", "USD/NTD"), data_date="2026-10-16")
    store.put_obs("macro.BTC", macro_payload(81_471.0, 120.0, "Yahoo", "BTC-USD"), data_date="2026-10-16")
    store.put_obs("macro.TACO", taco_obs(), data_date="2026-10-16")  # 川普壓力指數（market_pipeline_taco）
    published = NOW - 3600
    for key, title in (("us", "美股收高 科技股領漲"), ("tw", "台股量縮整理"), ("iran", "伊朗局勢"), ("semi", "費半收低 晶片股回檔")):
        items = [{"title": title, "source": "某報", "published_utc": iso(published)},
                 {"title": "很久以前的新聞", "source": "某報", "published_utc": iso(published - 3 * 86400)}]
        store.put_obs(f"news.{key}", {"items": items, "fetched_utc": iso(NOW - 300)}, data_date="2026-10-17")
    when = datetime.fromtimestamp(published, UTC)
    store.upsert_posts("trump", [
        {"original_id": "1001", "published_utc": when - timedelta(minutes=1), "own_text": "New tariffs on imports.",
         "quoted_author": None,
         "post_type": "post", "removed": False, "topics": ["tariff"]},
        {"original_id": "1002", "published_utc": when, "own_text": "Iran must stop.", "quoted_author": None,
         "post_type": "post", "removed": False, "topics": ["iran"]},
        {"original_id": "1003", "published_utc": when, "own_text": None, "quoted_author": None,
         "post_type": "post", "removed": True, "topics": []},
        {"original_id": "1004", "published_utc": when, "own_text": "Nice weather today.", "quoted_author": None,
         "post_type": "post", "removed": False, "topics": []},
    ])
    store.put_obs("trump.summary", {"window_h": 48.0, "topics": {"iran": 1, "tariff": 1},
                                    "labels": {"tariff": "關稅", "iran": "伊朗"}, "posts": 3, "matched": 2,  # 照 TRUMP_TOPIC_LABELS 的順序
                                    "latest_utc": iso(published), "at_least": False}, data_date="2026-10-17")
    for chamber, bids in (("house", (0.90, 0.91, 0.901, 0.902)), ("senate", (0.30, 0.32, 0.31, 0.33))):
        store.put_obs(f"election.polymarket.{chamber}", election_quote("polymarket", *bids[:2], fetched=NOW - 600),
                      data_date="2026-10-17")
        store.put_obs(f"election.kalshi.{chamber}", election_quote("kalshi", *bids[2:], fetched=NOW - 600),
                      data_date="2026-10-17")


class World:
    def __init__(self, root: Path, *, missing_on_d: tuple[str, ...] = (), misc: bool = True) -> None:
        self.root = root
        state = root / "state"
        state.mkdir(mode=0o700)
        os.chmod(state, 0o700)
        self.paths = market_paths.Paths.under(root)
        (root / "market_seasonality.json").write_bytes((WORK / "market_seasonality.json").read_bytes())
        self.clock = Clock(NOW)
        self.store = market_store.Store(self.paths.history_db, now_fn=self.clock)
        seed_us(self.store, missing_on_d=missing_on_d)
        seed_tw(self.store)
        if misc:
            seed_misc(self.store)
        self.charts = FakeCharts()

    def publish(self, now: float = NOW, charts: FakeCharts | None = None) -> dict:
        self.clock.now = now
        return market_publish.publish(self.store, self.paths, now=now, charts=charts or self.charts)

    def snapshot(self) -> dict:
        return json.loads(self.paths.snapshot.read_bytes().decode("utf-8"))

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """預測市場在正式環境是封鎖的（market_sources_misc.PREDICTION_MARKETS_BLOCKED）；這裡預設關掉，才測得到報價怎麼組，
    封鎖的情形在 test_election_row_says_blocked… 另外測（端對端測試用正式的預設）。"""
    import market_sources_misc

    monkeypatch.setattr(market_sources_misc, "PREDICTION_MARKETS_BLOCKED", False)
    w = World(tmp_path)
    yield w
    w.close()


# ── 整份快照 ──────────────────────────────────────────────────────────────────


def test_publish_writes_all_rows_with_valid_schema(world):
    outcome = world.publish()
    assert outcome["kind"] == "ok", outcome
    assert "row_errors" not in outcome and "shared_errors" not in outcome, outcome
    assert snapshot_job_sanitize(outcome) == outcome  # F1 的 job 只留列舉值：outcome 要原封不動通過
    info = os.stat(world.paths.snapshot)
    assert info.st_mode & 0o777 == 0o600
    raw = world.paths.snapshot.read_bytes()
    assert len(raw) < market_publish.SNAPSHOT_MAX_BYTES  # bot 讀取上限 2 MB，這裡還要留一半
    raw.decode("utf-8")
    snap = json.loads(raw)
    assert snap["schema"] == market_calc.SNAPSHOT_SCHEMA and snap["generated_at"] == NOW
    assert set(snap["rows"]) == set(market_calc.ROW_IDS)
    for row_id, row in snap["rows"].items():
        assert market_calc.row_ok(row_id, row), row_id
    states = {row_id: row["state"] for row_id, row in snap["rows"].items()}
    never = sorted(row_id for row_id, state in states.items() if state == "never")
    # 合成資料沒有放的：宏觀裡只放了 DXY／WTI／BTC（台幣走期交所代理）；VXTLT 有 us.quote.VXTLT（和 VIX 同一批），所以不在裡面
    assert never == sorted(f"macro.{k}" for k in ("BRENT", "GOLD", "SILVER", "RUT", "DJI", "IXIC", "MOVE"))
    assert all(states[f"us.13f.{etf}"] == "unknown_period" for etf in ("VOO", "QQQ", "SOXX"))
    assert outcome["rows"] == {"never": 7, "ok": len(states) - 10, "unknown_period": 3}
    values = snap["rows"]["us.etf.QQQ"]["values"]
    assert values["vol_label"] == "放量" and values["close"] > 0 and snap["rows"]["us.etf.QQQ"]["data_date"] == "2026-10-16"
    assert snap["rows"]["macro.DXY"]["data_date"] == "2026-10-16"  # 晚於 D 的列丟掉（§18.8）
    assert snap["rows"]["macro.DXY"]["values"]["value"] == 99.4
    assert snap["rows"]["macro.WTI"]["values"]["source"] == "ETF 代理：USO"
    assert snap["rows"]["macro.USDTWD"]["values"]["source"] == "代理：期交所匯率"
    assert snap["rows"]["macro.USDTWD"]["values"]["chg"] == pytest.approx(0.138)
    assert set(snap["charts"]) == {"QQQ", "SPY", "SOXX", "TXO"}
    assert len(snap["charts"]["QQQ"]["files"]) == 2 and len(snap["charts"]["TXO"]["files"]) == 1


def test_vxtlt_row_is_built_from_its_own_quote_like_vix(world):
    """2026-10-24 第 9 輪 A12（B 軌，取代骨架的佔位測試）：macro.VXTLT 和 macro.VIX／VIX3M 是同一個 handler，讀
    us.quote.VXTLT；資料日是 last_trade_time 的美東日期，過期規則照 VIX（KIND_US_DAILY）。macro.MOVE 不動。"""
    assert market_publish._builder_for("macro.VXTLT") is market_publish._builder_for("macro.VIX")
    assert market_publish._builder_for("macro.VIX3M") is market_publish._builder_for("macro.VIX")
    outcome = world.publish()
    assert outcome["kind"] == "ok" and "row_errors" not in outcome, outcome
    snap = world.snapshot()
    rows = snap["rows"]
    row = rows["macro.VXTLT"]
    assert row["state"] == "ok" and row["data_date"] == "2026-10-16" and row["source"] == "CBOE"
    assert row["values"] == {"value": 15.09, "chg": pytest.approx(-0.74), "chg_pct": -4.67, "source": "CBOE"}
    assert row["stale_after"] == rows["macro.VIX"]["stale_after"] == rows["macro.VIX3M"]["stale_after"]  # 過期規則照 VIX
    assert market_publish.validate_snapshot(snap) == []
    assert rows["macro.MOVE"]["state"] == "never" and rows["macro.MOVE"]["values"] == {}  # MOVE 沒被 VXTLT 頂替
    assert rows["macro.VIX"]["values"]["value"] == 19.99 and rows["macro.VIX3M"]["values"]["value"] == 21.5


def test_vxtlt_is_never_a_fact_for_the_one_liners_and_leaves_its_own_row_missing_without_a_quote(world, monkeypatch):
    seen: list[str] = []
    real = market_publish._Ctx.record_facts

    def spy(self, facts, data_date, stale_after):
        seen.extend(facts)
        return real(self, facts, data_date, stale_after)

    monkeypatch.setattr(market_publish._Ctx, "record_facts", spy)
    world.publish()
    assert "VIX" in seen and "VIX3M" in seen and "VXTLT" not in seen  # 一句話與矛盾只看 VIX 與 VIX3M
    with_quote = world.snapshot()["rows"]
    world.store._db().execute("DELETE FROM obs WHERE obs_id = 'us.quote.VXTLT'")
    world.publish()
    snap = world.snapshot()
    assert snap["rows"]["macro.VXTLT"]["state"] == "never" and market_publish.validate_snapshot(snap) == []
    for row_id in ("oneliner.us", "contra.us", "oneliner.tw", "contra.tw", "macro.VIX", "macro.VIX3M", "macro.MOVE"):
        assert snap["rows"][row_id] == with_quote[row_id], row_id


# ── 牆距的基準（2026-10-24 第 9 輪批 1 B 軌，審查 r9 seat1 I1；Andrew 對 Q2 照預設＝收盤價） ──────────────────────────


def _put_chain(world, symbol, *, spot, close):
    import market_pipeline_us as pipe

    chain = dataclasses.replace(option_chain(symbol, spot), close=close)
    world.store.put_obs(f"us.options.{symbol}", pipe.chain_payload(chain), data_date="2026-10-16")


def _set_close(world, symbol, close, *, provisional=False):
    """D 當天的收盤列：None＝沒有這一列；provisional＝CBOE 的暫定值（Nasdaq 正式列還沒到）。"""
    world.store._db().execute("DELETE FROM closes WHERE market = 'us' AND symbol = ? AND date = ?",
                              (symbol, D.isoformat()))
    if close is not None:
        world.store.upsert_closes("us", symbol, [(D, close, 1_000_000)], "cboe" if provisional else "nasdaq",
                                  provisional=provisional)


def _options_values(world, symbol="QQQ") -> dict:
    outcome = world.publish()
    assert outcome["kind"] == "ok" and "row_errors" not in outcome, outcome
    row = world.snapshot()["rows"][f"us.options.{symbol}"]
    assert row["state"] == "ok" and market_calc.row_ok(f"us.options.{symbol}", row)
    return row["values"]


def test_wall_distances_use_the_chains_own_close_when_it_agrees_with_the_official_close(world):
    _put_chain(world, "QQQ", spot=704.0, close=702.8)  # 盤後的最新價比收盤高一點
    _set_close(world, "QQQ", 702.8)
    values = _options_values(world)
    assert values["spot"] == 704.0 and values["dist_basis"] == "close" and values["ref_close"] == 702.8
    assert values["call_wall_dist_pct"] == pytest.approx((values["call_wall"] / 702.8 - 1) * 100)
    assert values["put_wall_dist_pct"] == pytest.approx((values["put_wall"] / 702.8 - 1) * 100)
    assert values["call_wall_dist_pct"] != pytest.approx((values["call_wall"] / 704.0 - 1) * 100)  # 不是現價的距離


@pytest.mark.parametrize(
    "official, provisional, close, expected",
    [
        (702.8, False, 702.8, "close"),  # 一致
        (701.0, False, 702.8, "close"),  # 差 0.26%
        (699.4, False, 702.8, "close"),  # 差 0.49%
        (699.3, False, 702.8, "spot"),  # 差 0.5005%：剛好超過
        (832.0, False, 700.0, "spot"),  # 差很多（這個合成世界的預設：ETF 約 832、鏈的 close 700）
        (200.0, False, 201.0, "close"),  # 剛好 0.5% 算一致
        (200.0, False, 201.02, "spot"),
        (650.0, True, 702.8, "close"),  # 同一天只有備援列：它就是用這份鏈的 close 寫的，沒有正式列可比
        (None, False, 702.8, "close"),  # 同一天沒有任何列
        (702.8, False, None, "spot"),  # 鏈沒有 close
        (702.8, False, 0.0, "spot"),
        (702.8, False, -1.0, "spot"),
    ],
)
def test_wall_distance_basis_rules(world, official, provisional, close, expected):
    spot = close + 1.0 if close and close > 100 else 704.0
    _put_chain(world, "QQQ", spot=spot, close=close)
    _set_close(world, "QQQ", official, provisional=provisional)
    values = _options_values(world)
    assert values["dist_basis"] == expected, values
    if expected == "close":
        assert values["ref_close"] == close
        assert values["call_wall_dist_pct"] == pytest.approx((values["call_wall"] / close - 1) * 100)
    else:
        assert values["ref_close"] is None
        assert values["call_wall_dist_pct"] == pytest.approx((values["call_wall"] / spot - 1) * 100)


def test_only_the_same_days_official_row_is_compared(world):
    """前一天的正式列差再多也不相干（比的是資料日當天）；資料日當天的正式列差太多才退回現價。"""
    _put_chain(world, "QQQ", spot=704.0, close=702.8)
    _set_close(world, "QQQ", None)
    world.store.upsert_closes("us", "QQQ", [(D - timedelta(days=1), 400.0, 1_000_000)], "nasdaq")
    assert _options_values(world)["dist_basis"] == "close"


def test_each_symbol_picks_its_own_basis(world):
    _put_chain(world, "QQQ", spot=704.0, close=702.8)
    _set_close(world, "QQQ", 702.8)
    world.publish()
    rows = world.snapshot()["rows"]
    assert [rows[f"us.options.{s}"]["values"]["dist_basis"] for s in ("QQQ", "SPY", "SOXX")] == ["close", "spot", "spot"]
    assert rows["us.options.SPY"]["values"]["ref_close"] is None


def test_the_untouched_world_falls_back_to_the_spot_and_says_so(world):
    values = _options_values(world)  # 合成 ETF 收盤約 832、鏈的 close 是 700：差 15% 以上，不能用
    assert values["dist_basis"] == "spot" and values["ref_close"] is None
    assert values["call_wall_dist_pct"] == pytest.approx((values["call_wall"] / 700.0 - 1) * 100)


def test_walls_and_gamma_do_not_depend_on_the_distance_basis(world):
    _put_chain(world, "QQQ", spot=704.0, close=702.8)
    _set_close(world, "QQQ", 702.8)
    on_close = _options_values(world)
    _set_close(world, "QQQ", 600.0)  # 正式收盤差很多 → 同一份鏈退回現價
    on_spot = _options_values(world)
    assert (on_close["dist_basis"], on_spot["dist_basis"]) == ("close", "spot")
    assert {key for key in on_close if on_close[key] != on_spot[key]} == {
        "call_wall_dist_pct", "put_wall_dist_pct", "ref_close", "dist_basis"}


def test_a_put_wall_above_the_close_keeps_its_plus_sign(world):
    _put_chain(world, "QQQ", spot=740.0, close=700.0)
    _set_close(world, "QQQ", 700.0)
    values = _options_values(world)
    assert values["dist_basis"] == "close" and values["put_wall"] > 700.0
    assert values["put_wall_dist_pct"] > 0
    assert values["put_wall_dist_pct"] == pytest.approx((values["put_wall"] / 700.0 - 1) * 100)


def test_when_the_official_closes_cannot_be_read_the_distances_stay_on_the_spot(world, monkeypatch):
    """QQQ 的收盤讀不到（共用資料壞了）：核對不了，寧可退回現價；期權列自己照常（它不靠 us.etf 那一列）。
    （整個 store.closes 壞掉的話日曆也建不起來、所有列都暫缺，所以只讓 QQQ 這一檔的收盤失敗。）"""
    _put_chain(world, "QQQ", spot=704.0, close=702.8)
    _set_close(world, "QQQ", 702.8)
    real = market_publish._Ctx.closes

    def flaky(self, market, symbol):
        if (market, symbol) == ("us", "QQQ"):
            raise market_publish._Unavailable("shared")
        return real(self, market, symbol)

    monkeypatch.setattr(market_publish._Ctx, "closes", flaky)
    outcome = world.publish()
    assert outcome["kind"] == "ok"
    rows = world.snapshot()["rows"]
    assert rows["us.etf.QQQ"]["state"] == "never"  # 它自己要用收盤
    values = rows["us.options.QQQ"]["values"]
    assert rows["us.options.QQQ"]["state"] == "ok" and values["dist_basis"] == "spot" and values["ref_close"] is None


def test_validate_snapshot_checks_the_option_basis_keys(world):
    _put_chain(world, "QQQ", spot=704.0, close=702.8)
    _set_close(world, "QQQ", 702.8)
    world.publish()
    snap = world.snapshot()
    assert snap["rows"]["us.options.QQQ"]["values"]["dist_basis"] == "close"
    assert market_publish.validate_snapshot(snap) == []

    def problems(**change) -> list[str]:
        copy = json.loads(json.dumps(snap))
        copy["rows"]["us.options.QQQ"]["values"].update(change)
        return market_publish.validate_snapshot(copy)

    assert problems(dist_basis="spot", ref_close=None) == []
    assert problems(dist_basis="weird") == ["options_basis:us.options.QQQ"]  # 型別對、但不是 job 寫得出來的詞
    assert problems(dist_basis="close", ref_close=None) == ["options_basis:us.options.QQQ"]  # 說是收盤卻沒有收盤
    assert problems(dist_basis="close", ref_close=0.0) == ["options_basis:us.options.QQQ"]
    assert problems(dist_basis="spot", ref_close=702.8) == ["options_basis:us.options.QQQ"]  # 說是現價卻帶著收盤
    assert problems(ref_close="702.8") == ["row:us.options.QQQ"]  # 型別錯：整列不合格
    assert problems(dist_basis=5) == ["row:us.options.QQQ"]


def test_an_options_row_whose_basis_keys_disagree_only_breaks_itself(world, monkeypatch):
    """欄位互相對不上（說是收盤卻沒有收盤價）在組那一列時就擋下：那三列暫缺、記 bad:options_basis，其他列與整份快照照發。"""
    real = market_calc.options_values

    def wrong(*args, **kwargs):
        values = real(*args, **kwargs)
        values["dist_basis"], values["ref_close"] = "close", None
        return values

    monkeypatch.setattr(market_calc, "options_values", wrong)
    outcome = world.publish()
    assert outcome["kind"] == "ok"
    assert outcome["row_errors"] == {f"us.options.{symbol}": "bad:options_basis" for symbol in ("QQQ", "SPY", "SOXX")}
    assert snapshot_job_sanitize(outcome) == outcome  # job 的 outcome 白名單認得這個種類
    rows = world.snapshot()["rows"]
    assert rows["us.options.QQQ"]["state"] == "never" and rows["us.etf.QQQ"]["state"] == "ok"
    assert market_publish.validate_snapshot(world.snapshot()) == []


def test_futures_day_change_needs_the_previous_session(world):
    """外資台指期淨口數的日變化（2026-10-18）：前一筆是前一個交易日才算；中間缺一天就空著。"""
    def futures(days: dict) -> dict:
        tx = lambda net: {"oi_long": 10, "oi_short": 10 - net, "oi_net": net, "trade_net": 0}  # noqa: E731
        return {"date": D.isoformat(), "foreign": {"TX": tx(-97_582)},
                "days": {k: {"TX": tx(v)} for k, v in days.items()}, "latest": D.isoformat()}

    prev, older = TW_DAYS[-2].isoformat(), TW_DAYS[-3].isoformat()
    world.store.put_obs("tw.futures", futures({D.isoformat(): -97_582, prev: -96_382, older: -90_000}),
                        data_date=D.isoformat())
    world.publish()
    values = world.snapshot()["rows"]["tw.futures"]["values"]
    assert values["foreign_net_big"] == -97_582 and values["foreign_net_big_chg"] == -1_200
    world.store.put_obs("tw.futures", futures({D.isoformat(): -97_582, older: -90_000}), data_date=D.isoformat())
    world.publish()
    assert world.snapshot()["rows"]["tw.futures"]["values"]["foreign_net_big_chg"] is None


def test_values_follow_the_definitions(world):
    world.publish()
    rows = world.snapshot()["rows"]
    options = rows["us.options.QQQ"]["values"]
    assert options["spot"] == 700.0 and options["call_wall"] == 735.0 and options["put_wall"] == 665.0
    assert options["monthly"]["expiry"] == "2026-11-20" and options["gex"]["status"] == "ok"
    assert options["gex"]["rate_assumed"] is False  # 3M 公債有值
    assert rows["us.options.QQQ"]["data_date"] == "2026-10-16"
    tw_options = rows["tw.options"]["values"]
    assert tw_options["month"] == "202610" and tw_options["pcr_oi_pct"] == 60.84
    assert tw_options["foreign_call_net"] == 2 and tw_options["call_wall"] > tw_options["spot"] > tw_options["put_wall"]
    inst = rows["tw.inst3"]["values"]
    assert inst["foreign_yi"] == pytest.approx(-30.0) and inst["dealer_yi"] == pytest.approx(0.5)
    assert inst["days_5d"] == 5 and inst["foreign_5d_yi"] == pytest.approx(-150.0)
    assert rows["tw.margin"]["values"] == {"balance_yi": pytest.approx(3500.0), "change_yi": pytest.approx(10.0)}
    assert rows["tw.futures"]["values"]["foreign_net_big"] == -97_582
    assert rows["tw.turnover"]["values"]["value_twd"] > 5e11
    fund = rows["tw.fund.0050"]["values"]
    assert fund["pe_valid"] == 4 and fund["rev_month"] == "2026-09" and (fund["rev_pos"], fund["rev_valid"]) == (3, 4)
    cftc = rows["us.cftc.es"]["values"]
    assert cftc["lev_net"] == -200_000 and cftc["lev_net_chg"] == 3_000 and cftc["contract"] == "S&P 500 Consolidated"
    assert rows["us.margin"]["values"]["yoy_pct"] == pytest.approx((1_480_054 / 1_200_000 - 1) * 100)
    f13 = rows["us.13f.QQQ"]["values"]
    assert f13["summary"]["total"] == 5 and f13["holder_period_note"] == market_calc.TEXT_13F_PERIOD.format(md="6/30")
    assert f13["weight_basis"] == "依 QQQ 持股權重（MoneyDJ 9/30）"
    assert rows["us.13f.VOO"]["values"]["weight_basis"] == market_calc.TEXT_SPY_WEIGHT.format(md="10/15")
    assert rows["us.fund.QQQ"]["values"]["pe_valid"] == 5
    breadth = rows["us.breadth.SOXX"]["values"]
    assert breadth["coverage_note"] == market_calc.TEXT_NO_US_SYMBOL.format(n=2, pct="6.1")
    assert breadth["universe_label"] == "SOXX 持股（MoneyDJ 9/30）"
    trump = rows["trump.summary"]["values"]
    assert trump["topics"] == {"伊朗": 1, "關稅": 1} and trump["at_least"] is False  # 移除的那則不算
    assert [item["excerpt"] for item in rows["trump.posts"]["values"]["items"]] == ["Iran must stop.", "New tariffs on imports."]
    assert [item["title"] for item in rows["news.us"]["values"]["items"]] == ["美股收高 科技股領漲"]  # 36 小時外不列
    election = rows["election"]["values"]
    assert election["state"] == "before" and election["days_left"] == 17  # 美東 10/17 → 11/03
    assert [(q["chamber"], q["platform"]) for q in election["quotes"]] == [
        ("house", "polymarket"), ("house", "kalshi"), ("senate", "polymarket"), ("senate", "kalshi")]
    assert [q["mid"] for q in election["quotes"]] == pytest.approx([0.905, 0.9015, 0.31, 0.32])
    season = rows["season.now"]["values"]
    assert season["month"] == 10 and season["midterm_year"] is True and season["sp500"]["n"] > 0


def test_season_row_carries_the_semiconductor_sections_and_survives_a_bad_table(world):
    """第 9 輪 A14：season.now 多了 SOXX 自身與 SOX 長歷史；靜態表的半導體段壞掉或沒有（舊表）只讓那段 pending。"""
    assert world.publish()["kind"] == "ok"
    row = world.snapshot()["rows"]["season.now"]
    assert row["state"] == "ok" and row["source"] == "datahub（Shiller）、Nasdaq、FinMind"
    values = row["values"]
    assert values["soxx"] == {"up_pct": 66.7, "median": 2.43, "mean": 1.49, "n": 9} and values["soxx_pending"] is False
    assert values["soxx_midterm"] is None and values["soxx_range"] == {"first": "2016-11", "last": "2025-12"}
    assert values["sox"]["n"] == 32 and values["sox_midterm"]["n"] == 8 and values["sox_range"]["first"] == "1994-06"
    table_path = world.root / "market_seasonality.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    table["sox"]["months_valid"] = 10  # 殘缺的重建：有效月份遠少於應有的
    del table["soxx"]
    table_path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    assert world.publish()["kind"] == "ok"
    broken = world.snapshot()["rows"]["season.now"]
    assert broken["state"] == "ok" and broken["values"]["sp500"]["n"] == 76
    assert broken["values"]["sox_pending"] is True and broken["values"]["sox"] is None
    assert broken["values"]["soxx_pending"] is True and broken["values"]["soxx_range"] is None
    assert market_publish.validate_snapshot(world.snapshot()) == []


def test_semiconductor_news_is_its_own_row_with_the_same_36_hour_rule(world):
    """第 9 輪 A17：news.semi 走和其他新聞主題同一個 @_rows("news.")；沒有 obs 時是暫缺，不影響別的新聞。"""
    assert world.publish()["kind"] == "ok"
    row = world.snapshot()["rows"]["news.semi"]
    assert row["state"] == "ok" and [item["title"] for item in row["values"]["items"]] == ["費半收低 晶片股回檔"]
    assert row["source"] == "Google News" and row["stale_after"] == NOW - 300 + market_calc.NEWS_TTL.total_seconds()
    world.store._db().execute("DELETE FROM obs WHERE obs_id = 'news.semi'")
    world.store._db().commit()
    assert world.publish()["kind"] == "ok"
    rows = world.snapshot()["rows"]
    assert rows["news.semi"]["state"] == "never" and rows["news.us"]["state"] == "ok"


def test_gex_rate_falls_back_to_assumed_when_the_3m_yield_is_stale(world):
    world.store.put_obs("us.ust", {"rows": [["2026-10-01", 4.20, 3.9, 5.0, 5.2]]}, data_date="2026-10-01")
    world.publish()
    gex = world.snapshot()["rows"]["us.options.QQQ"]["values"]["gex"]
    assert gex["rate_assumed"] is True and gex["status"] == "ok"


def test_daily_rows_expire_after_four_sessions(world):
    world.publish()
    rows = world.snapshot()["rows"]
    # 週五的資料：之後第 4 個交易日（10/22 週四）收盤過期；台股 10/26 補假不影響這段
    assert rows["us.etf.QQQ"]["stale_after"] == at_et(2026, 10, 22, 16, 0)
    assert rows["us.breadth.VOO"]["stale_after"] == at_et(2026, 10, 22, 16, 0)
    assert rows["macro.VIX"]["stale_after"] == at_et(2026, 10, 22, 16, 0)
    assert rows["tw.taiex"]["stale_after"] == at_tpe(2026, 10, 22, 13, 30)
    assert rows["tw.inst3"]["stale_after"] == at_tpe(2026, 10, 22, 13, 30)
    assert rows["us.cftc.nq"]["stale_after"] == at_tpe(2026, 10, 29)  # 報告日 10/13＋16 天
    assert rows["us.margin"]["stale_after"] == at_tpe(2026, 12, 1)  # 9/30（月底）＋62 天
    assert rows["us.13f.QQQ"]["stale_after"] == at_tpe(2026, 12, 2)  # 申報期間眾數 6/30＋155 天
    assert rows["macro.BTC"]["stale_after"] == at_et(2026, 10, 22, 16, 0)  # 2026-10-23 起和其他宏觀日線一樣（原本 Coinbase 抓取＋6 小時）
    assert rows["season.now"]["stale_after"] == at_tpe(2026, 11, 1)  # 這個月的統計：到月底
    # 倒數到美東午夜才變；報價抓取＋6 小時比較早，取小的
    assert rows["election"]["stale_after"] == min(datetime(2026, 10, 18, tzinfo=ET).timestamp(), NOW - 600 + 6 * 3600)


def test_options_friday_data_fresh_on_saturday_and_monday(world):
    world.publish()
    row = world.snapshot()["rows"]["us.options.QQQ"]
    # 下一個 NYSE 交易日（週一 10/19）收盤＋3 小時＝台北週二 07:00（§18.1）
    expected = at_tpe(2026, 10, 20, 7, 0)
    assert row["stale_after"] == expected
    assert world.snapshot()["charts"]["QQQ"]["stale_after"] == expected
    for now in (at_tpe(2026, 10, 17, 16, 31), at_tpe(2026, 10, 19, 20, 0)):
        assert now < expected
        assert world.publish(now=now)["kind"] == "ok"
        again = world.snapshot()["rows"]["us.options.QQQ"]
        assert again["state"] == "ok" and again["data_date"] == "2026-10-16" and again["stale_after"] == expected
    assert at_tpe(2026, 10, 20, 7, 0, 1) > expected


def test_breadth_falls_back_to_previous_complete_day(tmp_path):
    w = World(tmp_path, missing_on_d=("AAA", "BBB"))
    try:
        assert w.publish()["kind"] == "ok"
        snap = w.snapshot()
        voo = snap["rows"]["us.breadth.VOO"]
        assert voo["data_date"] == "2026-10-15"  # D 只有 3/5 檔有收盤（<80%）：顯示前一個完整交易日
        assert voo["stale_after"] == at_et(2026, 10, 21, 16, 0)
        assert snap["rows"]["us.breadth.SOXX"]["data_date"] == "2026-10-16"  # SOXX 的成分 D 都有
        assert snap["rows"]["us.etf.VOO"]["data_date"] == "2026-10-16"  # ETF 自己照常
        # 進度看三份成分的聯集：10 檔都有歷史（「歷史資料補到」已補完）；D 有 8/10＝80%，仍算完整日
        assert snap["progress"]["us_members"] == [len(US_MEMBERS), len(US_MEMBERS)]
        assert snap["progress"]["us_last_complete_day"] == "2026-10-16"
    finally:
        w.close()


def test_progress_counts_members_and_last_complete_days(world):
    world.publish()
    progress = world.snapshot()["progress"]
    # 2026-10-24 第 9 輪 A19、A20：多了落後／休市標示用的欄位（只增不改；細節見 test_market_lag_rsi）
    assert progress == {"us_members": [len(US_MEMBERS), len(US_MEMBERS)], "us_last_complete_day": "2026-10-16",
                        "tw_last_complete_day": "2026-10-16",
                        "us_day": "2026-10-16", "us_expected_day": "2026-10-16", "us_waiting_for_source": False,
                        "tw_day": "2026-10-16", "tw_expected_day": "2026-10-16", "tw_waiting_for_source": False,
                        "tw_closed_today": False, "tw_closed_confirmed": None, "tw_exrights_pending": False}


def test_quarantine_only_invalidates_the_touched_windows(world):
    # 隔離區間在 D 往前第 40～35 個交易日：碰到 60／120／240 日線的窗口，碰不到 20 日線（N+5＝25 天）
    world.store.add_quarantine("us", "BBB", US_DAYS[-40], US_DAYS[-35], "pending_jump")
    world.publish()
    per_n = world.snapshot()["rows"]["us.breadth.VOO"]["values"]["per_n"]
    assert per_n["20"]["valid"] == 5 and per_n["20"]["total"] == 5
    assert per_n["60"]["valid"] == 4 and per_n["120"]["valid"] == 4 and per_n["240"]["valid"] == 4


def test_etf_rows_skip_windows_that_touch_their_own_quarantine(world):
    """2026-10-23 審查 r7 #5：0050 被隔離（分割沒對上），自己的技術面原本照樣發布 -50%、月線距離 -48%。"""
    world.publish()
    before = world.snapshot()["rows"]
    assert before["tw.etf.0050"]["values"]["chg_pct"] is not None
    assert before["us.etf.VOO"]["values"]["ma_dist"]["60"] is not None
    world.store.add_quarantine("tw", "0050", TW_DAYS[-2], TW_DAYS[-1], "jump")
    world.store.add_quarantine("us", "VOO", US_DAYS[-40], US_DAYS[-35], "split")
    world.publish()
    rows = world.snapshot()["rows"]
    tw_values = rows["tw.etf.0050"]["values"]
    assert tw_values["close"] is not None and tw_values["chg_pct"] is None and tw_values["above_ma20"] is None
    assert all(value is None for value in tw_values["ma_dist"].values())
    assert tw_values["vol_ratio"] is None and tw_values["rsi14"] is None
    us_values = rows["us.etf.VOO"]["values"]
    assert us_values["chg_pct"] is not None and us_values["ma_dist"]["20"] is not None
    assert us_values["ma_dist"]["60"] is None and us_values["ma_dist"]["240"] is None


def test_spy_drawdown_row_honors_the_spy_quarantine(world):
    """2026-10-23 審查 r8 gp2 C1：us.etf.SPY 已經不算碰到隔離的窗口，macro.SPY_DD 卻照樣發布 240 日回檔。"""
    world.publish()
    assert world.snapshot()["rows"]["macro.SPY_DD"]["state"] == "ok"
    world.store.add_quarantine("us", "SPY", US_DAYS[-40], US_DAYS[-35], "split")
    world.publish()
    assert world.snapshot()["rows"]["macro.SPY_DD"]["state"] != "ok"


def test_tw_breadth_waits_for_exright_coverage(tmp_path):
    w = World(tmp_path)
    try:
        # 除權息資料只涵蓋到 T 的前一天：0050 與成分寬度都顯示前一天（§18.11），加權指數照常
        w.store._db().execute("DELETE FROM coverage")
        w.store.add_coverage("twt49u", TW_DAYS[0], TW_DAYS[-2])
        w.publish()
        rows = w.snapshot()["rows"]
        assert rows["tw.breadth.0050"]["data_date"] == TW_DAYS[-2].isoformat()
        assert rows["tw.etf.0050"]["data_date"] == TW_DAYS[-2].isoformat()
        assert rows["tw.taiex"]["data_date"] == D.isoformat()
    finally:
        w.close()


# ── 隔離：一列、一張圖壞掉不影響其他 ─────────────────────────────────────────────


def test_a_failing_calculation_only_breaks_its_rows(world, monkeypatch):
    def boom(*_args, **_kwargs):
        raise TypeError("bad input")

    monkeypatch.setattr(market_calc, "breadth", boom)
    monkeypatch.setattr(market_calc, "options_values", boom)
    monkeypatch.setattr(world.store, "exrights", boom)  # 共用資料壞掉：只記一次，用到它的列暫缺
    outcome = world.publish()
    assert outcome["kind"] == "ok"
    # 每份共用資料只記一次（0050 自己、成分寬度載入時第一檔就失敗），其他列直接暫缺
    assert outcome["shared_errors"] == {"exrights:0050": "unexpected:TypeError", "exrights:2308": "unexpected:TypeError"}
    assert snapshot_job_sanitize(outcome) == outcome
    rows = world.snapshot()["rows"]
    for row_id in ("us.breadth.VOO", "us.breadth.QQQ", "us.breadth.SOXX", "us.options.QQQ"):
        assert rows[row_id]["state"] == "never"
        assert outcome["row_errors"][row_id] == "unexpected:TypeError"
    assert rows["tw.etf.0050"]["state"] == "never" and rows["tw.breadth.0050"]["state"] == "never"
    assert rows["us.etf.QQQ"]["state"] == "ok" and rows["tw.taiex"]["state"] == "ok"
    # 期權列壞了：它的圖也不畫（不發布比卡片還新的圖）；台指的圖照常
    assert set(world.snapshot()["charts"]) == {"TXO"}


def test_a_row_failing_the_schema_becomes_never(world, monkeypatch):
    real = market_calc.etf_values

    def wrong(tech, volume):
        values = real(tech, volume)
        values["close"] = "not a number"
        return values

    monkeypatch.setattr(market_calc, "etf_values", wrong)
    outcome = world.publish()
    assert outcome["kind"] == "ok"
    assert outcome["row_errors"]["us.etf.QQQ"] == "bad:row_ids"
    assert world.snapshot()["rows"]["us.etf.QQQ"]["state"] == "never"
    assert world.snapshot()["rows"]["tw.taiex"]["state"] == "ok"


def test_external_strings_are_cleaned_and_snapshot_always_encodes(world):
    titles = ["x @all y", "翻轉\u202e字", "零寬\u200b字", "孤立\ud800代理", "看這裡 https://example.com/a"]
    items = [{"title": t, "source": "某報", "published_utc": iso(NOW - 600)} for t in titles]
    raw_json = json.dumps({"items": items, "fetched_utc": iso(NOW - 300)})  # ensure_ascii：孤立代理字元寫成 \ud800
    world.store._db().execute("UPDATE obs SET json = ? WHERE obs_id = 'news.us'", (raw_json,))
    assert world.publish()["kind"] == "ok"
    text = world.paths.snapshot.read_bytes().decode("utf-8")  # 解得開＝沒有孤立代理字元
    assert "\u202e" not in text and "\u200b" not in text and "@" not in text and "example.com" not in text
    kept = [item["title"] for item in json.loads(text)["rows"]["news.us"]["values"]["items"]]
    assert sorted(kept) == sorted(["x \u3000all y", "翻轉字", "零寬字", "孤立代理"])  # @ 換成全形空白


def test_sanitize_drops_non_finite_numbers_and_rejects_odd_types():
    counter = [0]
    out = market_publish._sanitize({"a": float("nan"), "b": [float("inf"), 1.5], 3: date(2026, 10, 16)}, counter)
    assert out == {"a": None, "b": [None, 1.5], "3": "2026-10-16"} and counter == [2]
    with pytest.raises(market_publish._BadValue):
        market_publish._sanitize({"x": object()}, [0])
    with pytest.raises(market_publish._BadValue):
        market_publish._sanitize("孤立\ud800", [0])
    with pytest.raises(market_publish._BadValue):
        market_publish._sanitize(datetime(2026, 10, 16), [0])  # 沒有時區的時間不收


def test_validate_snapshot_catches_broken_shapes(world):
    world.publish()
    snap = world.snapshot()
    assert market_publish.validate_snapshot(snap) == []
    broken = json.loads(json.dumps(snap))
    broken["rows"]["us.etf.QQQ"]["stale_after"] = "soon"
    broken["charts"]["QQQ"]["files"] = ["../../etc/passwd"]
    del broken["rows"]["tw.ad"]
    problems = market_publish.validate_snapshot(broken)
    assert "row:us.etf.QQQ" in problems and "chart_files:QQQ" in problems and "row_ids" in problems


# ── 圖 ──────────────────────────────────────────────────────────────────────


def test_charts_are_rendered_only_when_their_inputs_change(world):
    import market_pipeline_us as pipe

    world.publish()
    first = world.snapshot()["charts"]
    kinds = sorted(kind for kind, _ in world.charts.calls)
    assert kinds == ["gex", "gex", "gex", "oi", "oi", "oi", "oi"]
    gex_calls = [kwargs for kind, kwargs in world.charts.calls if kind == "gex"]
    # 圖的方向字和卡片「變號點」用 market_calc 同一份表（價格由低往高穿過時）
    assert all(direction in market_calc.GEX_DIRECTION_TEXT.values()
               for kwargs in gex_calls for _, direction in kwargs["crossings"])
    assert not hasattr(market_publish, "_GEX_DIRECTION")
    for entry in first.values():
        for name in entry["files"]:
            assert (world.paths.chart_dir / name).read_bytes() == PNG
    assert os.stat(world.paths.chart_dir).st_mode & 0o777 == 0o700

    world.charts.calls.clear()
    outcome = world.publish(now=NOW + 900)
    assert world.charts.calls == [] and outcome["charts"]["reused"] == 4
    assert world.snapshot()["charts"] == first

    # QQQ 的期權鏈變了（同一天的修訂）：只重畫 QQQ 兩張
    world.store.put_obs("us.options.QQQ", pipe.chain_payload(option_chain("QQQ", 700.0, scale=1.3)),
                        data_date="2026-10-16")
    world.publish(now=NOW + 1800)
    assert sorted(kind for kind, _ in world.charts.calls) == ["gex", "oi"]
    second = world.snapshot()["charts"]
    assert second["QQQ"]["files"] != first["QQQ"]["files"]
    assert {k: v for k, v in second.items() if k != "QQQ"} == {k: v for k, v in first.items() if k != "QQQ"}

    # /tmp 被清空：檔案不在就重畫，不沿用登記
    for name in second["SPY"]["files"]:
        (world.paths.chart_dir / name).unlink()
    world.charts.calls.clear()
    world.publish(now=NOW + 2700)
    assert sorted(kind for kind, _ in world.charts.calls) == ["gex", "oi"]
    assert all((world.paths.chart_dir / n).exists() for n in world.snapshot()["charts"]["SPY"]["files"])


def test_the_real_chart_module_accepts_the_inputs(world):
    outcome = market_publish.publish(world.store, world.paths, now=NOW)  # charts 沒給：用真的 market_charts
    assert outcome["kind"] == "ok" and outcome["charts"]["rendered"] == 4 and outcome["charts"]["failed"] == 0
    for entry in world.snapshot()["charts"].values():
        for name in entry["files"]:
            data = (world.paths.chart_dir / name).read_bytes()
            assert data.startswith(PNG[:8]) and 0 < len(data) < 1_000_000


@pytest.mark.parametrize("problem", ["mode", "symlink"])
def test_unsafe_chart_dir_means_no_charts_but_snapshot_is_written(world, problem):
    chart_dir = world.paths.chart_dir
    if problem == "mode":
        chart_dir.mkdir(mode=0o755)
        os.chmod(chart_dir, 0o755)  # 權限太寬（別人能讀寫）：不碰、不 chmod
    else:
        target = world.root / "elsewhere"
        target.mkdir(mode=0o700)
        chart_dir.symlink_to(target)
    outcome = world.publish()
    assert outcome["kind"] == "ok" and outcome["charts"]["dir"] == "chart_dir"
    snap = world.snapshot()
    assert snap["charts"] == {} and snap["rows"]["us.options.QQQ"]["state"] == "ok"
    assert world.charts.calls == []
    if problem == "mode":
        assert os.stat(chart_dir).st_mode & 0o777 == 0o755 and list(chart_dir.iterdir()) == []
    else:
        assert list((world.root / "elsewhere").iterdir()) == []


def test_retired_charts_are_deleted_only_after_72_hours(world):
    import market_pipeline_us as pipe

    world.publish()
    old = world.snapshot()["charts"]["QQQ"]["files"]
    stranger = world.paths.chart_dir / ("f" * 32 + ".png")
    stranger.write_bytes(PNG)  # 不是自己登記的圖（例如圖片生成功能的）：永遠不刪
    world.store.put_obs("us.options.QQQ", pipe.chain_payload(option_chain("QQQ", 700.0, scale=2.0)),
                        data_date="2026-10-16")
    retired_at = NOW + 600
    world.publish(now=retired_at)
    new = world.snapshot()["charts"]["QQQ"]["files"]
    assert new != old
    tmp_old = world.paths.chart_dir / ".market-abc123.tmp"
    tmp_new = world.paths.chart_dir / ".market-def456.tmp"
    for path, mtime in ((tmp_old, retired_at), (tmp_new, retired_at + 71 * 3600 - 600)):
        path.write_bytes(b"partial")
        os.utime(path, (mtime, mtime))

    world.publish(now=retired_at + 71 * 3600)
    assert all((world.paths.chart_dir / name).exists() for name in old)  # 退役未滿 72 小時
    assert not tmp_old.exists() and tmp_new.exists()  # 被 kill 留下、超過 1 小時的暫存檔才清
    outcome = world.publish(now=retired_at + 72 * 3600 + 1)
    assert not any((world.paths.chart_dir / name).exists() for name in old)
    assert outcome["charts"]["deleted"] == len(old)
    assert all((world.paths.chart_dir / name).exists() for name in new) and stranger.exists()
    assert not set(old) & set(world.store.chart_files())


def test_cleanup_never_follows_a_symlink_put_in_place_of_a_chart(world):
    import market_pipeline_us as pipe

    world.publish()
    old = world.snapshot()["charts"]["QQQ"]["files"]
    world.store.put_obs("us.options.QQQ", pipe.chain_payload(option_chain("QQQ", 700.0, scale=2.0)),
                        data_date="2026-10-16")
    world.publish(now=NOW + 600)
    victim = world.root / "victim.txt"
    victim.write_text("keep me")
    (world.paths.chart_dir / old[0]).unlink()
    (world.paths.chart_dir / old[0]).symlink_to(victim)
    outcome = world.publish(now=NOW + 600 + 73 * 3600)
    assert victim.read_text() == "keep me" and (world.paths.chart_dir / old[0]).is_symlink()
    assert outcome["charts"]["skipped"] == 1 and old[0] not in world.store.chart_files()


def test_a_failing_chart_drops_only_that_chart_key(world):
    charts = FakeCharts(fail="gex")
    outcome = world.publish(charts=charts)
    assert outcome["kind"] == "ok"
    snap = world.snapshot()
    assert set(snap["charts"]) == {"TXO"}  # QQQ／SPY／SOXX 的 GEX 圖壞了：整組不發布
    assert outcome["charts"]["errors"] == {"QQQ": "unexpected:TypeError", "SOXX": "unexpected:TypeError",
                                           "SPY": "unexpected:TypeError"}
    left = sorted(p.name for p in world.paths.chart_dir.iterdir())
    assert left == snap["charts"]["TXO"]["files"]  # 半組圖已經刪掉
    assert set(world.store.chart_files()) == set(snap["charts"]["TXO"]["files"])


def test_write_failure_keeps_the_old_snapshot_and_its_charts(world, monkeypatch):
    world.publish()
    before = world.paths.snapshot.read_bytes()
    live = {n for entry in world.snapshot()["charts"].values() for n in entry["files"]}

    def fail(_path, _data):
        raise OSError("disk full")

    monkeypatch.setattr(market_paths, "write_private_atomic", fail)
    import market_pipeline_us as pipe

    world.store.put_obs("us.options.QQQ", pipe.chain_payload(option_chain("QQQ", 700.0, scale=3.0)),
                        data_date="2026-10-16")
    outcome = world.publish(now=NOW + 600)
    assert outcome["kind"] == "write_failed:OSError"
    assert world.paths.snapshot.read_bytes() == before
    files = world.store.chart_files()
    assert all(files[name]["retired_at"] is None for name in live)  # 舊快照還引用著：不退役


@pytest.mark.parametrize("problem", ["mode", "symlink", "missing"])
def test_state_dir_not_ok_writes_nothing(tmp_path, problem):
    w = World(tmp_path)
    try:
        state = w.paths.state_dir
        if problem == "mode":
            os.chmod(state, 0o755)
        else:
            moved = tmp_path / "real_state"
            state.rename(moved)
            if problem == "symlink":
                state.symlink_to(moved)
        outcome = w.publish()
        assert outcome == {"kind": "state_dir", "generated_at": NOW}
        assert not (tmp_path / "real_state" / "market_dashboard.json").exists()
        assert not w.paths.chart_dir.exists() and w.charts.calls == []
        if problem == "mode":
            assert not w.paths.snapshot.exists()
            assert os.stat(state).st_mode & 0o777 == 0o755  # 不 chmod state/
        if problem == "missing":
            assert not state.exists()  # 不建立 state/
    finally:
        w.close()


# ── bot 端讀得懂 ────────────────────────────────────────────────────────────


def test_snapshot_round_trips_through_the_dashboard(world):
    import market_dashboard

    assert world.publish()["kind"] == "ok"
    market_dashboard.clear_cache()
    reply = market_dashboard.handle("", now=NOW, paths=world.paths)
    assert reply.flex is not None and reply.alt_text.startswith("盤勢：")
    card = json.dumps(reply.flex, ensure_ascii=False)
    assert "資料準備中" not in card and "資料準備中" not in (reply.text or "")
    for expected in ("美股（2/5）｜10/16 收盤", "台股（3/5）｜10/16 收盤", "外資賣超", "成分股站上月線", "VIX"):
        assert expected in card, expected
    # 輪播第一張（重點摘要）讀真的 publisher 寫的快照：6 列依序是美股、台股、風險、利率、事件、川普，不超寬、沒有被切掉
    rows = [node["text"] for node in reply.flex["contents"][0]["body"]["contents"]]
    assert [row[:2] for row in rows] == ["美股", "台股", "風險", "利率", "事件", "川普"], rows
    assert all(market_dashboard.row_width(row) <= 24 and "…" not in row for row in rows), rows
    assert reply.render_errors == ()
    for body in ("QQQ", "0050", "期權", "新聞", "季節性", "選舉", "13F QQQ"):
        detail = market_dashboard.handle(body, now=NOW, paths=world.paths)
        assert detail.flex is not None, body
        assert "資料準備中" not in json.dumps(detail.flex, ensure_ascii=False), body
    charts = world.snapshot()["charts"]
    for body, key in (("QQQ 圖", "QQQ"), ("台指 圖", "TXO")):
        image = market_dashboard.handle(body, now=NOW, paths=world.paths)
        assert image.images == charts[key]["files"], body
    summary = market_dashboard.status_summary(now=NOW, paths=world.paths)
    assert summary["external_dropped"] == 0 and "render_error" not in summary  # 註記、標籤都過得了 bot 的檢查
    assert summary["rows_missing"] == sorted(f"macro.{k}" for k in ("BRENT", "DJI", "GOLD", "IXIC", "MOVE", "RUT", "SILVER"))
    assert summary["rows_stale"] == [] and summary["us_members"] is None  # 進度齊了不顯示
    # 週二 07:00 之後週五的期權就過期：圖回固定句、不送舊圖
    late = market_dashboard.handle("QQQ 圖", now=at_tpe(2026, 10, 20, 7, 0, 1), paths=world.paths)
    assert late.images is None and late.text


def test_election_result_is_shown_for_seven_days_then_ended(world):
    first = at_tpe(2026, 11, 10, 12, 0)
    for platform in ("polymarket", "kalshi"):
        world.store.put_obs(f"election.{platform}.house",
                            election_quote(platform, 0.99, 0.995, fetched=first - 3600, result="Yes"),
                            data_date="2026-11-10")
    world.publish(now=first)
    values = world.snapshot()["rows"]["election"]["values"]
    assert values["state"] == "result" and values["days_left"] is None
    # 眾院已結算（不受 6 小時限制）；參院還沒結算、報價已超過 6 小時 → 不列
    assert [(q["chamber"], q["result"]) for q in values["quotes"]] == [("house", "yes"), ("house", "yes")]
    # 結算後 collect 一天才看一次：結果不受 6 小時限制；第一次看到結果後 7 天才算結束
    world.publish(now=first + 6 * 86400)
    assert world.snapshot()["rows"]["election"]["values"]["state"] == "result"
    world.publish(now=first + 7 * 86400 + 60)
    assert world.snapshot()["rows"]["election"]["values"]["state"] == "ended"


def test_missing_senate_quotes_still_publish_the_house(world):
    world.store._db().execute("DELETE FROM obs WHERE obs_id LIKE 'election.%.senate'")
    assert world.publish()["kind"] == "ok"
    row = world.snapshot()["rows"]["election"]
    assert row["state"] == "ok"
    assert [(q["chamber"], q["platform"]) for q in row["values"]["quotes"]] == [("house", "polymarket"), ("house", "kalshi")]


def test_stale_prediction_quotes_are_left_out(world):
    world.publish(now=NOW + 7 * 3600)  # 還沒結算的報價抓取已超過 6 小時
    values = world.snapshot()["rows"]["election"]["values"]
    assert values["quotes"] == [] and values["state"] == "before"


def test_btc_is_a_daily_macro_row_from_yahoo_or_the_ibit_proxy(world):
    """2026-10-23：Coinbase 拿掉，BTC 和其他宏觀格一樣——Yahoo 的 BTC-USD，沒有就是 IBIT 代理（source 寫「ETF 代理：IBIT」）。"""
    world.publish()
    row = world.snapshot()["rows"]["macro.BTC"]
    assert row["values"]["source"] == "Yahoo" and row["values"]["value"] == 81_471.0 and row["basis"] == "日收盤"
    assert row["data_date"] == "2026-10-16" and row["stale_after"] == at_et(2026, 10, 22, 16, 0)
    world.store.put_obs("macro.BTC", macro_payload(46.06, -1.78, "ETF 代理：IBIT", "IBIT"), data_date="2026-10-16")
    world.publish()
    row = world.snapshot()["rows"]["macro.BTC"]
    assert row["state"] == "ok" and row["values"]["source"] == "ETF 代理：IBIT" and row["source"] == "ETF 代理：IBIT"
    assert row["values"]["chg_pct"] == pytest.approx(-1.78 / 47.84 * 100)
    import market_dashboard  # bot 端：代理只寫單日漲跌幅，不把 46.06 印成 BTC 價格

    card = market_dashboard._macro_part(market_dashboard._Ctx(world.snapshot(), NOW), "BTC")
    assert card[0] == "比特幣（IBIT 代理）" and card[2] == "單日 -3.72%"


def test_an_old_coinbase_btc_payload_is_not_shown(world):
    """舊的 Coinbase 那筆（沒有日線的 date）留在 obs 裡：不拿來頂替，BTC 暫缺，等新的宏觀任務寫進 macro.BTC。"""
    world.store.put_obs("macro.BTC", {"value": 83_449.385, "chg": None, "chg_pct": None, "source": "Coinbase",
                                      "fetched_utc": iso(NOW - 900)}, data_date="2026-10-17")
    world.publish()
    assert world.snapshot()["rows"]["macro.BTC"]["state"] == "never"


def test_election_row_says_blocked_and_lists_no_quotes_while_the_markets_are_blocked(world, monkeypatch):
    """2026-10-23：Polymarket、Kalshi 在台灣被封鎖。選舉列寫 blocked＝True、不放報價（也不拿封鎖前最後一次的報價頂替），
    bot 才分得出「被封鎖」和「資料暫缺」；倒數照算。解除封鎖後恢復列報價。"""
    import market_sources_misc

    monkeypatch.setattr(market_sources_misc, "PREDICTION_MARKETS_BLOCKED", True)
    world.publish()  # seed 裡四個市場 10 分鐘前才抓過、都還新鮮
    row = world.snapshot()["rows"]["election"]
    values = row["values"]
    assert row["state"] == "ok" and market_calc.row_ok("election", row)
    assert values["blocked"] is True and values["quotes"] == [] and values["state"] == "before" and values["days_left"] == 17
    assert row["stale_after"] == datetime(2026, 10, 18, tzinfo=ET).timestamp()  # 倒數到美東午夜才變
    monkeypatch.setattr(market_sources_misc, "PREDICTION_MARKETS_BLOCKED", False)
    world.publish()
    values = world.snapshot()["rows"]["election"]["values"]
    assert values["blocked"] is False and len(values["quotes"]) == 4


def test_a_settled_quote_from_before_the_block_is_not_shown_while_blocked(world, monkeypatch):
    import market_sources_misc

    monkeypatch.setattr(market_sources_misc, "PREDICTION_MARKETS_BLOCKED", True)
    first = at_tpe(2026, 11, 10, 12, 0)
    world.store.put_obs("election.polymarket.house", election_quote("polymarket", 0.99, 0.995, fetched=first - 3600, result="Yes"),
                        data_date="2026-11-10")
    world.publish(now=first)
    values = world.snapshot()["rows"]["election"]["values"]
    assert values["blocked"] is True and values["quotes"] == [] and values["state"] == "waiting"


def _crowd_names(count: int) -> list[str]:
    return [f"Z{chr(65 + n // 26)}{chr(65 + n % 26)}" for n in range(count)]  # ZAA、ZAB…（三個字母，合法的美股代號）


def test_events_keep_24_items_so_the_etf_cards_can_list_every_top_holding(world):
    """2026-10-24 第 9 輪 A16（B 軌）：EVENTS_KEEP 12→24。三檔 ETF 的前十大聯集最多約 25 檔，財報季集中在同一兩週，
    只留 12 筆時後面的財報日（財報行要列的）整批消失；FOMC 與 CPI 照樣一定留。"""
    assert market_publish.EVENTS_KEEP == 24
    names = _crowd_names(30)
    world.store.put_obs("us.events.earnings", {"symbols": {
        name: {"date": f"2026-10-{20 + n % 7}", "estimated": n % 2 == 0} for n, name in enumerate(names)}},
        data_date="2026-10-17")
    world.publish()
    items = world.snapshot()["rows"]["events.next"]["values"]["items"]
    kinds = [item["kind"] for item in items]
    assert len(items) == 24 and kinds.count("fomc") == 1 and "cpi" in kinds
    assert kinds.count("earnings") >= 20  # 以前最多 12 − 必留的幾筆
    assert [item["at"] for item in items] == sorted(item["at"] for item in items)
    assert all(isinstance(item.get("estimated"), bool) for item in items if item["kind"] == "earnings")
    assert market_publish.validate_snapshot(world.snapshot()) == []


def test_events_keep_fomc_when_earnings_crowd_the_list(world):
    """2026-10-18（端對端測試發現）：財報季時前十大的財報日會把 FOMC 擠出清單；FOMC 與 CPI 一定要留。
    2026-10-24：清單上限 12→24，所以要 30 檔財報日才擠得到。"""
    names = _crowd_names(30)
    world.store.put_obs("us.events.earnings", {"symbols": {
        name: {"date": f"2026-10-{20 + n % 7}", "estimated": True} for n, name in enumerate(names)}},
        data_date="2026-10-17")
    world.publish()
    items = world.snapshot()["rows"]["events.next"]["values"]["items"]
    kinds = [item["kind"] for item in items]
    assert len(items) == market_publish.EVENTS_KEEP and kinds.count("fomc") == 1 and "cpi" in kinds
    assert [item["at"] for item in items] == sorted(item["at"] for item in items)


def test_events_at_the_same_moment_list_fomc_cpi_and_nfp_before_other_kinds(world):
    """2026-10-18 r6d：非農和失業率同時公布，「下一個事件」要先寫非農（以前照種類名排，econ 排在 nfp 前面）。"""
    moment = "2026-10-21T12:30:00+00:00"
    world.store.put_obs("us.events.econ", {"days": {"2026-10-21": [
        [moment, "Unemployment Rate"], [moment, "Nonfarm Payrolls"], [moment, "Retail Sales"]]}}, data_date="2026-10-17")
    world.publish()
    items = world.snapshot()["rows"]["events.next"]["values"]["items"]
    at = datetime(2026, 10, 21, 12, 30, tzinfo=UTC).timestamp()
    assert [(item["kind"], item["name"]) for item in items if item["at"] == at] == [
        ("nfp", "Nonfarm Payrolls"), ("econ", "Retail Sales"), ("econ", "Unemployment Rate")]  # 其他種類照名稱排


def test_events_use_the_bot_kinds_and_expire_with_the_first_event(world):
    world.publish()
    row = world.snapshot()["rows"]["events.next"]
    items = row["values"]["items"]
    assert [(item["kind"], item["name"]) for item in items] == [
        ("econ", "Empire State Manufacturing Survey"),
        ("cpi", "Consumer Price Index (MoM)"),  # 同一時刻的核心 CPI 併掉；沒時間的不列
        ("fomc", "FOMC"),
        ("earnings", "AAA"),
    ]
    first_at = datetime(2026, 10, 19, 12, 30, tzinfo=UTC).timestamp()
    assert items[0]["at"] == first_at
    assert row["stale_after"] == min(NOW + 36 * 3600, first_at + 2 * 3600)


def test_one_liner_and_contradictions_use_same_day_inputs(world):
    world.publish()
    rows = world.snapshot()["rows"]
    tw = rows["contra.tw"]
    assert "foreign_sell_trust_buy" in tw["values"]["codes"]  # 外資 5 日賣超、投信 5 日買超
    assert tw["values"]["text"] in {market_calc.contra_sentence(market_calc.Contra(c.split(":")[0],
                                    c.split(":")[1] if ":" in c else None)) for c in tw["values"]["codes"]}
    assert tw["data_date"] == "2026-10-16"
    assert rows["oneliner.tw"]["values"]["text"].startswith("台股收在月線")
    inputs = [rows[r]["stale_after"] for r in ("tw.taiex", "tw.inst3", "tw.turnover", "tw.margin", "tw.futures",
                                                "tw.etf.0050", "tw.breadth.0050")]
    assert tw["stale_after"] == min(inputs)
    assert rows["oneliner.us"]["values"]["text"].startswith("標普收在月線")
    assert rows["oneliner.us"]["stale_after"] <= rows["us.etf.VOO"]["stale_after"]


# ── 介面漂移、import ──────────────────────────────────────────────────────────


def test_obs_keys_match_the_pipelines():
    import market_pipeline_tw as tw
    import market_pipeline_us as us

    assert market_publish.OBS_US_HOLDINGS == {"VOO": us.OBS_HOLDINGS_SPY, "QQQ": us.OBS_HOLDINGS_NDX,
                                              "SOXX": us.OBS_HOLDINGS_SOXX}
    assert {etf: market_publish.OBS_US_TOP10.format(etf) for etf in us.OBS_TOP10} == us.OBS_TOP10
    assert market_publish.OBS_US_13F.format("") == us.OBS_13F_PREFIX
    assert market_publish.OBS_US_FUND.format("") == us.OBS_FUND_PREFIX
    assert {s: market_publish.OBS_US_OPTIONS.format(s) for s in us.OBS_OPTIONS} == us.OBS_OPTIONS
    assert {s: market_publish.OBS_US_QUOTE.format(s) for s in us.OBS_QUOTE} == us.OBS_QUOTE
    assert {k: market_publish.OBS_US_CFTC.format(k) for k in us.OBS_CFTC} == us.OBS_CFTC
    assert (market_publish.OBS_US_UST, market_publish.OBS_US_MARGIN) == (us.OBS_UST, us.OBS_MARGIN)
    assert (market_publish.OBS_US_FOMC, market_publish.OBS_US_ECON, market_publish.OBS_US_EARNINGS) == (
        us.OBS_EVENTS_FOMC, us.OBS_EVENTS_ECON, us.OBS_EVENTS_EARNINGS)
    pairs = {
        "OBS_TW_ALLBUT": "OBS_ALLBUT", "OBS_TW_INST3": "OBS_INST3", "OBS_TW_MARGIN": "OBS_MARGIN", "OBS_TW_PE": "OBS_PE",
        "OBS_TW_REVENUE": "OBS_REVENUE", "OBS_TW_FUTURES": "OBS_FUTURES", "OBS_TW_OPTIONS_INST": "OBS_OPTIONS_INST",
        "OBS_TW_PCR": "OBS_PCR", "OBS_TW_TXO": "OBS_TXO", "OBS_TW_FX": "OBS_FX", "OBS_TW_HOLDINGS": "OBS_HOLDINGS",
        "COVERAGE_TWT49U": "COVERAGE_TWT49U",
    }
    for mine, theirs in pairs.items():
        assert getattr(market_publish, mine) == getattr(tw, theirs), mine
    assert (us.MARKET, tw.MARKET, tw.SYM_TAIEX) == (market_publish.US, market_publish.TW, market_publish.TAIEX)
    assert market_calendar.TW_HOLIDAY_OBS_PREFIX == tw.OBS_HOLIDAYS_PREFIX
    assert market_publish.CHART_KEYS == ("QQQ", "SPY", "SOXX", "TXO")


def test_misc_keys_match_the_misc_pipeline():
    import market_pipeline_misc as misc
    import market_sources_misc as sources

    assert market_publish.OBS_MACRO.format("") == misc.OBS_MACRO_PREFIX
    assert market_publish.OBS_MACRO.format("BTC") == misc._macro_obs("BTC")  # 2026-10-23：BTC 和其他宏觀格同一條路
    assert not hasattr(market_publish, "OBS_BTC") and not hasattr(misc, "OBS_BTC")
    assert market_publish.OBS_NEWS.format("") == misc.OBS_NEWS_PREFIX
    assert {f"news.{key}" for key in misc.NEWS_TOPICS} == {r for r in market_calc.ROW_IDS if r.startswith("news.")}
    assert (market_publish.OBS_TRUMP_SUMMARY, market_publish.POSTS_TRUMP) == (misc.OBS_TRUMP_SUMMARY, misc.POSTS_TRUMP)
    expected = {misc.OBS_ELECTION_PREFIX + market.key for market in sources.PREDICTION_MARKETS}
    assert {obs for ids in market_publish.OBS_ELECTION.values() for obs in ids} == expected
    assert misc.SOURCE_FX == "代理：期交所匯率"


def test_importing_the_module_loads_no_plotting_or_numeric_libraries():
    code = ("import sys, market_publish; "
            "print(sorted(m for m in ('matplotlib', 'numpy', 'pandas', 'market_charts', 'market_pipeline_us') "
            "if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=WORK, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
