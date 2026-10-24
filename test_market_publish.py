"""市場盤勢（/盤勢，2026-10-17）publish 步驟的測試。

用真的 market_store.Store（暫存目錄）塞合成資料：幾檔美股成分與 ETF、一小組台股、期權鏈（用 F2 自己的
chain_payload 序列化）、宏觀（Yahoo 與 ETF 代理）、事件、新聞、川普貼文、預測市場；時間一律注入 now
（週六 2026-10-17 16:31 台北），畫圖用假的模組（計數、寫 PNG 標頭）。不連網、不碰正式 state/ 與 /tmp。
"""

from __future__ import annotations

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
    for symbol, close, change in (("VIX", 19.99, 3.53), ("VIX3M", 21.5, 1.2)):
        store.put_obs(f"us.quote.{symbol}", {
            "close": close, "price_change": change, "price_change_pct": 17.66, "last_trade_et": "2026-10-16T16:15:01-04:00",
            "timestamp_utc": "2026-10-17T01:37:16+00:00",
            "history": [["2026-10-15", round(close - change, 2)], ["2026-10-16", close]]}, data_date="2026-10-16")
    store.put_obs("us.ust", {"rows": [["2026-10-14", 4.27, 3.9, 5.07, 5.3], ["2026-10-15", 4.25, 3.9, 5.05, 5.3],
                                      ["2026-10-16", 4.26, 3.91, 5.09, 5.31]]}, data_date="2026-10-16")
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


def seed_misc(store: market_store.Store) -> None:
    """market_pipeline_misc 的格式：宏觀一格一筆（BTC 也是，2026-10-23 拿掉 Coinbase）、新聞是 obs、川普是累積的貼文＋摘要。"""
    store.put_obs("macro.DXY", macro_payload(99.4, 0.3, "Yahoo", "DX-Y.NYB"), data_date="2026-10-16")
    store.put_obs("macro.WTI", macro_payload(71.5, -0.5, "ETF 代理：USO", "USO"), data_date="2026-10-16")
    store.put_obs("macro.USDTWD", macro_payload(32.338, 0.138, "代理：期交所匯率", "USD/NTD"), data_date="2026-10-16")
    store.put_obs("macro.BTC", macro_payload(81_471.0, 120.0, "Yahoo", "BTC-USD"), data_date="2026-10-16")
    published = NOW - 3600
    for key, title in (("us", "美股收高 科技股領漲"), ("tw", "台股量縮整理"), ("iran", "伊朗局勢")):
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
    # 合成資料沒有放的：宏觀裡只放了 DXY／WTI／BTC（台幣走期交所代理）
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
    assert progress == {"us_members": [len(US_MEMBERS), len(US_MEMBERS)], "us_last_complete_day": "2026-10-16",
                        "tw_last_complete_day": "2026-10-16"}


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
    for expected in ("美股（1/4）｜10/16 收盤", "台股（2/4）｜10/16 收盤", "外資賣超", "成分股站上月線", "VIX"):
        assert expected in card, expected
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


def test_events_keep_fomc_when_earnings_crowd_the_list(world):
    """2026-10-18（端對端測試發現）：財報季時前十大的財報日會把 FOMC 擠出 12 筆；FOMC 與 CPI 一定要留。"""
    names = [f"Z{letter}" for letter in "ABCDEFGHIJKLMN"]
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
    assert rows["oneliner.us"]["values"]["text"].startswith("美股收在月線")
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
