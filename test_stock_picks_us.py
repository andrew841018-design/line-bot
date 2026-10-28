"""stock_picks_us（美股單檔查詢＋美股推薦，2026-10-28）的測試。

小的合成資料：tmp state、tmp 盤勢 DB（用盤勢 job 自己的 market_store 建同 schema）、假的 Yahoo（換掉
stock_picks._get_json）、假的成績模組（PERF 子代理同時在寫 stock_picks_perf，這裡不依賴它）。不連網。
順序照 IMPL_SPEC §3.4：代號 → 格式與純判斷 → 卡片 → DB 推薦池 → 狀態檔與 worker → 單檔即時查 → 回覆與狀態。
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import market_store  # noqa: E402
import stock_card_check  # noqa: E402
import stock_db_ro  # noqa: E402
import stock_picks as sp  # noqa: E402
import stock_picks_us as us  # noqa: E402

NY = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")
D = date(2026, 10, 27)  # 週二（美東）
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=TPE).timestamp()  # 台北 10/28 15:00 ＝ 美東 10/28 03:00


def weekdays(end: date, n: int) -> list[date]:
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return out[::-1]


CAL = weekdays(D, 260)


def rising(step: float = 0.1, n: int = 260, start: float = 100.0) -> list[float]:
    return [start + step * i for i in range(n)]


def hot(n: int = 260) -> list[float]:  # 比季線高約 9% → ⚠️ 漲多
    return [100.0 * 1.003 ** i for i in range(n)]


def falling(n: int = 260) -> list[float]:  # 在年線之下 → ⏸️
    return [200.0 - 0.2 * i for i in range(n)]


POOL = {  # 代號: (SSGA 名稱, 權重)；合計 101，GOOG 和 GOOGL 同公司
    "NVDA": ("NVIDIA CORP", 30.0),
    "MSFT": ("MICROSOFT CORP", 25.0),
    "AAPL": ("APPLE INC", 20.0),
    "AMZN": ("AMAZON.COM INC", 15.0),
    "GOOGL": ("ALPHABET INC CL A", 6.0),
    "GOOG": ("ALPHABET INC CL C", 5.0),
}
SERIES = {"NVDA": rising(0.1), "MSFT": rising(0.12), "AAPL": hot(), "AMZN": falling(), "GOOGL": rising(0.05),
          "GOOG": rising(0.05)}
MSFT_NO_D = dict(zip(CAL[:-1], rising(0.12)[:-1]))  # MSFT 少了 D 那一根（盤勢 job 還沒寫到）
QUARTERS = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]


def holdings(pool=POOL, as_of="2026-10-26") -> dict:
    return {"source": "SSGA", "as_of": as_of, "rows": [[s, n, w, None] for s, (n, w) in pool.items()],
            "count": len(pool)}


def db_path() -> Path:
    return sp.STATE_DIR / stock_db_ro.DB_NAME


def make_db(series=SERIES, *, cal=CAL, pool=POOL, as_of="2026-10-26", extra=None, path=None) -> Path:
    path = path or db_path()
    store = market_store.Store(path, now_fn=lambda: NOW)
    for i, etf in enumerate(("SPY", "VOO", "QQQ")):
        store.upsert_closes("us", etf, [(d, 500.0 + i + 0.5 * k, 1) for k, d in enumerate(cal)], "nasdaq")
    for symbol, values in {**series, **(extra or {})}.items():  # list：對齊日曆最後幾天；dict：指定日期
        rows = values.items() if isinstance(values, dict) else zip(cal[-len(values):], values)
        store.upsert_closes("us", symbol, [(d, v, 1) for d, v in rows], "nasdaq")
    if pool is not None:
        store.put_obs(us.HOLDINGS_OBS, holdings(pool, as_of), data_date=as_of)
    store.close()
    return path


def add_closes(symbol: str, rows, *, provisional: bool = False, market: str = "us") -> None:
    store = market_store.Store(db_path(), now_fn=lambda: NOW)
    store.upsert_closes(market, symbol, rows, "nasdaq", provisional=provisional)
    store.close()


def ts_payload(ysym: str, series: dict, cur="USD") -> dict:
    result = []
    for type_name, rows in series.items():
        result.append({
            "meta": {"symbol": [ysym], "type": [type_name]},
            "timestamp": [int(datetime(d.year, d.month, d.day).timestamp()) for d, _ in rows],
            type_name: [{"dataId": 1, "asOfDate": d.isoformat(), "periodType": "3M", "currencyCode": cur,
                         "reportedValue": {"raw": v, "fmt": "x"}} for d, v in rows],
        })
    return {"timeseries": {"result": result, "error": None}}


def good_fund(ysym: str, cur: str = "USD") -> dict:
    return ts_payload(ysym, {
        "quarterlyTotalRevenue": list(zip(QUARTERS, [100.0, 105.0, 110.0, 115.0, 120.0])),
        "quarterlyNetIncome": list(zip(QUARTERS, [10.0, 11.0, 12.0, 13.0, 14.0])),
        "quarterlyDilutedEPS": list(zip(QUARTERS, [1.0, 1.1, 1.2, 1.3, 1.4])),
    }, cur)


def chart_payload(ysym: str, bars: dict, *, kind="EQUITY", currency="USD", exchange="NMS",
                  long_name="NVIDIA Corporation", short_name="NVIDIA") -> dict:
    days = sorted(bars)
    meta = {"symbol": ysym, "currency": currency, "instrumentType": kind, "exchangeName": exchange,
            "longName": long_name, "shortName": short_name}
    return {"chart": {"result": [{
        "meta": meta,
        "timestamp": [int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY).timestamp()) for d in days],
        "indicators": {"quote": [{"close": [bars[d] for d in days]}]},
    }], "error": None}}


def bars_of(values, cal=CAL) -> dict:
    return dict(zip(cal[-len(values):], values))


class FakeYahoo:
    """換掉 stock_picks._get_json：依路徑回日線或財報；值是例外就丟。記下每次的主機、路徑與 timeout。"""

    def __init__(self, charts=None, funds=None, hook=None):
        self.charts, self.funds, self.hook = dict(charts or {}), dict(funds or {}), hook
        self.calls: list[tuple] = []  # (主機, 路徑, timeout, total_s)

    def __call__(self, host, path, *, params=None, max_bytes, timeout, total_s, if_modified_since=None, stop=None):
        self.calls.append((host, path, timeout, total_s))
        if self.hook is not None:
            self.hook(host, path)
        symbol = unquote(path.rsplit("/", 1)[-1])
        table = self.charts if path.startswith("/v8/finance/chart/") else self.funds
        value = table.get(symbol)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise sp.FetchError("not_found")
        return 200, value, None

    def fund_calls(self) -> list[str]:
        return [unquote(p.rsplit("/", 1)[-1]) for _, p, *_ in self.calls if "fundamentals" in p]


def _no_network(*_a, **_k):
    raise AssertionError("stock_picks must not touch the network in tests")


@pytest.fixture(autouse=True)
def fake_perf(monkeypatch):
    """假的 stock_picks_perf（介面照 IMPL_SPEC §4）。"""
    module = types.ModuleType("stock_picks_perf")
    module.US_HISTORY_NAME = "stock_picks_us_history.jsonl"
    module.calls = []
    module.result = None
    module.raise_update = False

    def update(*, ai_enabled, now, db_path=None):
        module.calls.append((ai_enabled, now))
        if module.raise_update:
            raise RuntimeError("perf broke")
        return module.result

    def perf_bubble(result):
        line = "推薦成績計算中" if result is None else "台股推薦後 5 個交易日：平均漲 1.8%，0050 同期漲 0.9%"
        return sp._bubble("股票推薦：推薦成績", None, [sp._text(line)], [sp._text(sp.TEXT_DISCLAIMER, size="sm")])

    module.update = update
    module.load_result = lambda: module.result
    module.perf_bubble = perf_bubble
    module.perf_alt_text = lambda result: "股票推薦：只供參考｜推薦成績｜計算中"
    module.summary = lambda: {"perf_date": None, "ledger_rows": 0, "ledger_full": False, "pending": 0, "skipped": 0}
    monkeypatch.setitem(sys.modules, "stock_picks_perf", module)
    return module


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(us, "_wall", lambda: NOW)
    monkeypatch.setattr(us, "FUND_SPACING_S", 0.0)


@pytest.fixture()
def small_pool(monkeypatch):
    monkeypatch.setattr(us, "POOL_N", 5)
    monkeypatch.setattr(us, "POOL_MIN_ROWS", 5)


@pytest.fixture()
def yahoo(monkeypatch):
    fake = FakeYahoo(funds={s: good_fund(s) for s in POOL})
    monkeypatch.setattr(sp, "_get_json", fake)
    return fake


def full_round(now: float = NOW) -> float:
    return us._tick(threading.Event(), now)


def set_clock(monkeypatch, value: float) -> None:
    monkeypatch.setattr(us, "_wall", lambda: value)


def read_worker_file() -> dict:
    return json.loads((sp.STATE_DIR / us.WORKER_NAME).read_text())


def history_lines() -> list[dict]:
    path = sp.STATE_DIR / us.HISTORY_NAME
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# ── 1. 代號（PLAN §5.1） ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("token, expected", [
    ("NVDA", "NVDA"), ("nvda", "NVDA"), ("NVDA嗎", "NVDA"), ("nvda？", "NVDA"), ("NVDA現在", "NVDA"),
    ("NVDA股價", "NVDA"), ("NVDA.", "NVDA"), ("NVDA(輝達)", "NVDA"), ("BRK.B", "BRK.B"), ("BRK/B", "BRK.B"),
    ("brk-b", "BRK.B"), ("ＮＶＤＡ", "NVDA"), ("NV​DA", "NVDA"), ("A", "A"), ("GOOGL", "GOOGL"),
    ("S&P", us.REJECTED), ("J&J", us.REJECTED), ("P&G", us.REJECTED), ("AT&T", us.REJECTED), ("AT+T", us.REJECTED),
    ("S＆P", us.REJECTED), ("A1", None), ("NVDA2330", None), ("ABCDEF", None), ("ß", None), ("NVDAß", None),
    ("NVDA台積電", None), ("BRK.BC", None), ("NVDA-", None), ("台積電", None), ("", None), ("2330", None),
])
def test_parse_us_token(token, expected):
    assert us.parse_us_token(token) == expected


@pytest.mark.parametrize("text, expected", [
    ("S & P 500", us.REJECTED), ("S&P 500", us.REJECTED), ("NVDA AAPL", "NVDA"), ("美國 NVDA", "NVDA"),
    ("NVDA、AAPL", "NVDA"), ("NVDA|AAPL", "NVDA"), ("我想問 nvda?", "NVDA"), ("AI", "AI"), ("hi", "HI"),
    ("tw", "TW"), ("台積電 跟 台達電", None), ("", None), (None, None),
])
def test_find_us_symbol(text, expected):
    assert us.find_us_symbol(text) == expected


def test_reserved_words_and_fixed_texts():
    assert us.RESERVED == {"AI", "ETF", "US", "USA", "OK", "HELP", "BUY", "SELL"}
    assert "HI" not in us.RESERVED and "TW" not in us.RESERVED  # Hillenbrand、Tradeweb 是真的代號
    assert us.TEXT_RESERVED == "股票評估：美股請打代號，例如 /股票 NVDA；看推薦請打 /股票"
    assert us.TEXT_US_NOT_FOUND == "股票評估：認不出這個美股代號，請確認代號，例如 /股票 NVDA"
    assert us.TEXT_PHASE3_OFF == "股票推薦：美股推薦和推薦成績暫時關閉"
    assert not _symbol_like(us.REJECTED)


def _symbol_like(value) -> bool:
    return isinstance(value, str) and bool(us._SYMBOL_RE.fullmatch(value))


def test_hooks_route_through_this_module(monkeypatch):
    """hook（stock_picks 檔尾）＋這裡的代號規則：台股優先、保留字、S&P、強制美股、台股快照 None。"""
    calls = []
    monkeypatch.setattr(us, "handle_symbol", lambda symbol, *, deadline=None: calls.append(symbol) or sp.Reply(text="ok"))
    index = sp.name_index({"AES-KY": "6781", "GIS-KY": "6456", "台積電": "2330"})
    # 衝突全集（台股名稱 ∩ DB 的美股代號，FACTS.md）：台股優先；「美股 GIS」才查美股
    assert sp.resolve_query("GIS", index) == ("code", "6456")
    assert sp.resolve_query("AES", index) == ("code", "6781")
    assert sp.resolve_query("2330 NVDA", index) == ("code", "2330")
    assert sp.resolve_query("NVDA", index) == (sp.US_SYMBOL, "NVDA")
    assert sp.resolve_query("S&P", index) == (sp.US_SYMBOL, us.REJECTED)
    assert sp._us_command("美股 GIS", None).text == "ok" and calls == ["GIS"]
    assert sp._us_command("美股AI", None).text == "ok" and calls == ["GIS", "AI"]  # 強制美股：保留字也查
    assert sp._us_command("美股 S&P", None).text == us.TEXT_US_NOT_FOUND
    assert sp._us_detail("AI", None).text == us.TEXT_RESERVED
    assert sp._us_detail(us.REJECTED, None).text == us.TEXT_US_NOT_FOUND
    # 台股快照 None：純英文台股簡稱不送美股
    assert sp._us_only_symbol("gis") is None and sp._us_only_symbol("NVDA") == "NVDA"
    assert {"aes", "gis"} <= sp._TW_ASCII_NAMES
    # 「美股 推薦」中間有空白也算；台股快照 None 時，美股指令與代號照樣能用
    assert sp._us_command("美股 推薦", None).alt_text.startswith("股票推薦：只供參考｜美股：")
    assert sp._snapshot is None
    assert sp.handle("美股").alt_text.startswith("股票推薦：只供參考｜美股：")
    assert sp.handle("nvda？", deadline=time.monotonic() + 20).text == "ok" and calls[-1] == "NVDA"
    assert sp.handle("gis").text == sp.TEXT_NOT_READY  # 台股的純英文簡稱：照舊回準備中


# ── 2. 格式與純判斷 ────────────────────────────────────────────────────────────────
def test_usd_format():
    assert us._fmt_usd(1281.905) == "1,281.91"
    assert us._fmt_usd(186.23) == "186.23"
    assert us._fmt_usd(0.5) == "0.50"
    assert us._fmt_usd(1234567.891) == "1,234,567.89"


def test_us_trend_compares_at_cents():
    trend = us.UsTrend(close=100.004, ma60=100.0, ma240=100.0049, p60=0)
    assert trend.above_year  # 兩邊都顯示 100.00
    assert not us.UsTrend(close=99.994, ma60=100.0, ma240=100.0, p60=0).above_year
    built = us.us_trend_of(rising(0.1), rising(0.1)[-60:])
    assert isinstance(built, us.UsTrend) and built.p60 == 24 and built.above_year
    assert us.us_trend_of(rising(0.1)[:200], rising(0.1)[140:200]) is None  # 年線窗不夠


def _rows(values, quarters=QUARTERS, cur="USD"):
    return tuple((q, float(v), cur) for q, v in zip(quarters, values))


def test_parse_fundamentals_validates_each_row():
    payload = good_fund("NVDA")
    rows = payload["timeseries"]["result"][0]["quarterlyTotalRevenue"]
    rows.append(dict(rows[-1], reportedValue={"raw": 999.0}))  # 同一天：取最後一筆
    rows.append({"asOfDate": "2026-03-31", "periodType": "12M", "currencyCode": "USD", "reportedValue": {"raw": 1}})
    rows.append({"asOfDate": "2026-04-30", "periodType": "3M", "currencyCode": "USD", "reportedValue": {"raw": "123"}})
    rows.append({"asOfDate": "2026-05-31", "periodType": "3M", "currencyCode": "USD", "reportedValue": {"raw": math.nan}})
    rows.append({"asOfDate": "2026-05-15", "periodType": "3M", "currencyCode": "USD", "reportedValue": {"raw": True}})
    rows.append({"asOfDate": "2026-05-16", "periodType": "3M", "reportedValue": {"raw": 5.0}})
    rows.append({"asOfDate": "bad", "periodType": "3M", "currencyCode": "USD", "reportedValue": {"raw": 5.0}})
    rows.append(None)
    parsed = us.parse_fundamentals(payload)
    assert [r[0] for r in parsed["rev"]] == QUARTERS
    assert parsed["rev"][-1][1] == 999.0
    assert len(parsed["ni"]) == 5 and len(parsed["eps"]) == 5
    assert us.parse_fundamentals({"nope": 1}) is None and us.parse_fundamentals(None) is None
    assert us.parse_fundamentals({"timeseries": {"result": []}}) == {"rev": (), "ni": (), "eps": ()}


def test_growth_rules():
    assert us.growth_of(_rows([100, 105, 110, 115, 120]), D) == us.Growth(QUARTERS[-1], 200)
    assert us.growth_of(_rows([100, 105, 110, 115, 100]), D).yoy == 0  # 0.0% 不算成長
    assert us.growth_of(_rows([100, 105, 110, 115, 100.04]), D).yoy == 0
    assert us.growth_of(_rows([100, 105, 110, 115, 120]), date(2027, 5, 1)) is None  # 最近一季太舊（> 200 天）
    assert us.growth_of(_rows([100, 105, 110, 115, 120])[1:], D) is None  # 沒有去年同季
    assert us.growth_of(_rows([0, 105, 110, 115, 120]), D) is None  # 去年同季 ≤ 0
    assert us.growth_of(_rows([100, 105, 110, 115, -5]), D) is None  # 最新一季 ≤ 0
    mixed = _rows([100, 105, 110, 115], QUARTERS[:4], "USD") + _rows([120], QUARTERS[4:], "TWD")
    assert us.growth_of(mixed, D) is None  # 幣別不同
    cost_q = [date(2025, 8, 31), date(2025, 11, 30), date(2026, 2, 28), date(2026, 5, 31), date(2026, 8, 31)]
    assert us.growth_of(_rows([86, 62, 66, 63, 94], cost_q), D).yoy == 93  # COST 的月底季：365 天


def test_profit_rules():
    assert us.profit_of(_rows([10, 11, 12, 13, 14]), D) == us.Profit(QUARTERS[-1], 0, 0)
    assert us.profit_of(_rows([10, 11, -1, 13, 14]), D) == us.Profit(QUARTERS[-1], 1, 0)
    assert us.profit_of(_rows([10, 11, 0, 13, 14]), D) == us.Profit(QUARTERS[-1], 0, 1)
    brk = _rows([10, 12, 13], [date(2025, 3, 31), date(2025, 6, 30), date(2025, 12, 31)]) + _rows([14], [date(2026, 3, 31)])
    assert us.profit_of(brk, D) is None  # BRK-B：缺 2025-09-30（不連續）
    assert us.profit_of(_rows([10, 11, 12, 13, 14]), date(2027, 6, 1)) is None
    assert us.profit_of(_rows([11, 12, 13]), D) is None


def test_pe_rules():
    eps, ni = _rows([1.0, 1.1, 1.2, 1.3, 1.4]), _rows([10, 11, 12, 13, 14])
    assert us.pe_of(eps, ni, 125.9, D) == us.Pe(us.PE_OK, 252)  # 125.9 ÷ 5.0
    assert us.pe_of(_rows([1.0, 1.1, 1.2, 1.3, 1.4], cur="TWD"), _rows([10, 11, 12, 13, 14], cur="TWD"), 300.0, D).kind == us.PE_NOT_USD
    split = _rows([1.0, 1.1, 1.2, 1.3, 0.35])  # 最後一季沒還原 4:1 分割 → 股數跳
    assert us.pe_of(split, ni, 125.9, D).kind == us.PE_NONE
    assert us.pe_of(_rows([1.0, -3.0, 1.2, -1.0, 0.5]), _rows([10, -30, 12, -10, 5]), 50.0, D).kind == us.PE_LOSS  # 合計虧損先講
    assert us.pe_of(_rows([1.0, 3.0, -1.0, 1.0, -0.5]), _rows([10, 30, -10, 10, -5]), 50.0, D).kind == us.PE_NONE  # 正的季不到 3 季
    assert us.pe_of(_rows([1.0, 1.0, 1.0, 1.0, -4.0]), _rows([10, 10, 10, 10, -40]), 50.0, D).kind == us.PE_LOSS
    assert us.pe_of(eps, ni, 6000.0, D).kind == us.PE_OVER  # 6000 ÷ 5 ＝ 1200 倍
    assert us.pe_of((), ni, 10.0, D).kind == us.PE_NONE
    # JPM 的形狀：EPS 到 9/30，淨利只到 6/30 → 股數檢查只用對得上的 3 季，照算
    assert us.pe_of(eps, ni[:4], 125.9, D) == us.Pe(us.PE_OK, 252)
    assert us.pe_of(eps, ni[:3], 125.9, D).kind == us.PE_NONE  # 只對得上 2 季
    assert us.pe_of(eps[:3], ni, 10.0, D).kind == us.PE_NONE


GOOD_GROWTH, GOOD_PROFIT, GOOD_PE = us.Growth(QUARTERS[-1], 200), us.Profit(QUARTERS[-1], 0, 0), us.Pe(us.PE_OK, 252)


def _facts(trend=None, *, kind=us.STOCK, growth=GOOD_GROWTH, profit=GOOD_PROFIT, fund=us.FUND_OK, pe=GOOD_PE,
           symbol="NVDA", name="NVIDIA CORP", weight=30.0, **kw):
    trend = trend if trend is not None else us.UsTrend(close=125.9, ma60=122.95, ma240=113.95, p60=24)
    return us.UsFacts(symbol, name, kind, D, trend, growth=growth, profit=profit, pe=pe, fund=fund, weight=weight, **kw)


def _trend(p60: int, *, below: bool = False) -> us.UsTrend:
    return us.UsTrend(close=100.0, ma60=100.0 / (1 + p60 / 1000), ma240=120.0 if below else 90.0, p60=p60)


@pytest.mark.parametrize("trend, kwargs, level, text", [
    (None, {}, sp.NO_DATA, "資料不足"),
    (_trend(0, below=True), {}, sp.BELOW_YEAR, "長期偏弱（在年線之下）"),
    (_trend(51), {}, sp.CAUTION, "漲多了（比季線高 5.1%）"),
    (_trend(-51), {}, sp.CAUTION, "比季線低 5.1%"),
    (_trend(50), {}, sp.MET, "4 條都符合"),
    (_trend(-50), {}, sp.MET, "4 條都符合"),
    (_trend(10), {"kind": us.ETF, "fund": us.FUND_NA}, sp.MET, "走勢 2 條都符合（ETF 只看走勢）"),
    (_trend(10), {"fund": us.FUND_UNAVAILABLE}, sp.CAUTION, "走勢可以，財報暫時抓不到"),
    (_trend(10), {"growth": us.Growth(QUARTERS[-1], 0)}, sp.CAUTION, "走勢可以，其他條件不足（符合 3 條，共 4 條）"),
    (_trend(10), {"profit": us.Profit(QUARTERS[-1], 1, 0), "growth": None}, sp.CAUTION,
     "走勢可以，其他條件不足（符合 2 條，共 4 條）"),
])
def test_light_branches(trend, kwargs, level, text):
    facts = _facts(trend, **kwargs) if trend is not None else us.UsFacts("NVDA", "", us.STOCK, D, None)
    light = us.us_light_of(facts)
    assert (light.level, light.text) == (level, text)


def test_year_line_equal_counts_as_above():
    facts = _facts(us.UsTrend(close=100.0, ma60=100.0, ma240=100.0, p60=0))
    assert us.us_checks_of(facts)[0].state is True and us.us_light_of(facts).level == sp.MET


def test_check_texts():
    checks = us.us_checks_of(_facts(_trend(32)))
    assert [c.text for c in checks] == [
        "在年線之上", "比季線高 3.2%（在 ±5% 以內）", "最近一季（到 9/30）營收比去年同季多 20.0%", "近四季每季都有賺錢"]
    assert us._check_line(checks[0]) == "✅ 長期向上：在年線之上"
    assert us.us_checks_of(_facts(growth=us.Growth(QUARTERS[-1], -12)))[2].text == "最近一季（到 9/30）營收比去年同季少 1.2%"
    assert us.us_checks_of(_facts(growth=us.Growth(QUARTERS[-1], 0)))[2].text == "最近一季（到 9/30）營收和去年同季一樣（0.0%）"
    assert us.us_checks_of(_facts(profit=us.Profit(QUARTERS[-1], 1, 0)))[3].text == "近四季有 1 季虧損"
    assert us.us_checks_of(_facts(profit=us.Profit(QUARTERS[-1], 0, 1)))[3].text == "近四季有 1 季沒有賺錢"
    assert [c.text for c in us.us_checks_of(_facts(growth=None, profit=None))[2:]] == ["營收資料不足", "獲利資料不足"]
    assert len(us.us_checks_of(_facts(kind=us.ETF, fund=us.FUND_NA))) == 2
    assert us._pe_text(_facts()) == "本益比 25.2 倍（參考，不算條件）"
    assert us._pe_text(_facts(pe=us.Pe(us.PE_OVER))) == "本益比超過 1000 倍（參考，不算條件）"
    assert us._pe_text(_facts(pe=us.Pe(us.PE_LOSS))) == "近四季合計虧損，本益比不適用"
    assert us._pe_text(_facts(pe=us.Pe(us.PE_NOT_USD))) == "財報不是美元，本益比不適用"
    assert us._pe_text(_facts(pe=us.Pe(us.PE_NONE))) == "本益比資料不足"


def test_pick_order_and_most_missed():
    pool = [
        _facts(_trend(30), symbol="AAA", weight=10.0), _facts(_trend(-11), symbol="BBB", weight=5.0),
        _facts(_trend(12), symbol="CCC", weight=8.0), _facts(_trend(12), symbol="DDD", weight=9.0),
        _facts(_trend(60), symbol="EEE"), _facts(_trend(5), symbol="FFF", fund=us.FUND_UNAVAILABLE),
    ]
    assert [f.symbol for f in us.choose_us_picks(pool)] == ["BBB", "DDD", "CCC"]  # |乖離| → 權重大 → 代號
    tie = [_facts(_trend(12), symbol="ZZZ", weight=9.0), _facts(_trend(12), symbol="YYY", weight=9.0)]
    assert [f.symbol for f in us.choose_us_picks(tie)] == ["YYY", "ZZZ"]
    missed_pool = [_facts(_trend(5), growth=us.Growth(QUARTERS[-1], -5)), _facts(_trend(5), growth=None),
                   _facts(_trend(80))]
    assert us.us_most_missed(missed_pool) == (us.GROWTH, 2, 3)
    assert us.us_most_missed([_facts(_trend(80)), _facts(_trend(-80))]) == (us.MID, 2, 2)
    assert us.us_most_missed([]) is None


def test_name_cleaning():
    assert us.clean_us_name("META PLATFORMS INC...", pool=True) == "META PLATFORMS INC"
    assert us.clean_us_name("JPMORGAN CHASE + CO", pool=True) == "JPMORGAN CHASE & CO"
    assert us.clean_us_name("PROCTER + GAMBLE CO/THE", pool=True) == "PROCTER & GAMBLE CO"
    assert us.clean_us_name("NVIDIA Corporation*") == "NVIDIA Corporation"
    assert us.clean_us_name("McDonald's Corp.") == "McDonald's Corp."
    assert us.clean_us_name("Bad@Name Inc") == "" and us.clean_us_name("Café Corp") == ""
    assert us.clean_us_name("x" * 61) == "" and us.clean_us_name(None) == ""


# ── 3. 卡片 ───────────────────────────────────────────────────────────────────────
def _all_texts(node) -> list[str]:
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node["text"])
        for value in node.values():
            out += _all_texts(value)
    elif isinstance(node, list):
        for item in node:
            out += _all_texts(item)
    return out


def test_pick_card_texts_and_check():
    market = sp.Market(level=sp.NEUTRAL, b20=10, b60=20)
    card = us.us_pick_bubble(_facts(_trend(24)), 1, 3, market, lead_note=True)
    texts = _all_texts(card)
    assert texts[0] == "股票推薦：美股第 1 檔（共 3 檔）" and texts[1] == "美股大盤（SPY）：中性"
    assert sp.TEXT_LEAD_NOTE in texts and us.TEXT_US_POOL_LEAD in texts
    assert "✅ NVDA（NVIDIA CORP）" in texts
    assert "4 條都符合｜收盤 100.00 美元（10/27 美國時間）" in texts
    assert "依據：收盤（Nasdaq 資料）、財報（Yahoo）｜資料到 10/27（美國時間）" in texts
    assert "美股推薦只從標普 500 市值前 50 大挑；只供參考，不是買賣指示" in texts
    assert stock_card_check.card_ok(card)
    assert sp.TEXT_LEAD_NOTE not in _all_texts(us.us_pick_bubble(_facts(), 2, 3, None))


def test_status_cards_pass_check():
    missed = (us.GROWTH, 30, 50)
    no_pick = us.us_no_pick_bubble(D, sp.Market(sp.HOT, 70, 90), missed)
    texts = _all_texts(no_pick)
    assert "這次（資料到 10/27 美國時間）美股沒有 4 條都符合的" in texts
    assert "最常差的條件：有在成長（50 檔裡有 30 檔沒過）" in texts
    assert "美股大盤（SPY）：偏熱（比月線高 7.0%），追高風險較大" in texts
    assert "最後一次的資料到 10/27（美國時間）" in _all_texts(us.us_not_updated_bubble(D))
    for card in (no_pick, us.us_not_updated_bubble(D), us.us_status_bubble(us.TEXT_US_COMPUTING),
                 us.us_status_bubble(us.TEXT_US_STUCK)):
        assert stock_card_check.card_ok(card)


@pytest.mark.parametrize("facts", [
    _facts(_trend(24)),
    _facts(_trend(0, below=True)),
    _facts(_trend(10), fund=us.FUND_UNAVAILABLE),
    _facts(_trend(10), kind=us.ETF, fund=us.FUND_NA, name="SPDR S&P 500 ETF Trust"),
    us.UsFacts("TSLA", "", us.STOCK, D, None, gray="jump", source="yahoo"),
    _facts(_trend(10), source="yahoo", verified=True),
])
def test_detail_cards_pass_check(facts):
    card = us.us_detail_bubble(facts, market=sp.Market(sp.COLD, -70, -50), market_date=D)
    alt = us.us_detail_alt_text(facts)
    assert alt.startswith("股票評估：只供參考｜") and stock_card_check.card_ok(card, alt)
    texts = _all_texts(card)
    assert texts[-1] == sp.TEXT_DISCLAIMER
    if facts.kind == us.ETF:
        assert us.TEXT_ETF_NA in texts and us.TEXT_US_DETAIL_POOL_NOTE not in texts
    if facts.trend is None:
        assert "資料到 10/27（美國時間）" in texts


def test_detail_card_score_line_and_sources():
    below = _all_texts(us.us_detail_bubble(_facts(_trend(0, below=True))))
    assert "收盤 100.00 美元（10/27 美國時間）｜符合 3 條（共 4 條）" in below
    unavailable = _all_texts(us.us_detail_bubble(_facts(_trend(0, below=True), fund=us.FUND_UNAVAILABLE)))
    assert "收盤 100.00 美元（10/27 美國時間）" in unavailable  # 財報抓不到：不補分數
    verified = _all_texts(us.us_detail_bubble(_facts(source="yahoo", verified=True)))
    assert "依據：Yahoo 收盤、Yahoo 財報｜收盤和 Nasdaq 資料核對過｜資料到 10/27（美國時間）" in verified
    etf = _all_texts(us.us_detail_bubble(_facts(kind=us.ETF, fund=us.FUND_NA, source="yahoo")))
    assert "依據：Yahoo 收盤｜資料到 10/27（美國時間）" in etf
    later = _all_texts(us.us_detail_bubble(_facts(), market=sp.Market(sp.NEUTRAL, 0, 0), market_date=D - timedelta(days=1)))
    assert "美股大盤（SPY）：中性（10/26 美國時間）" in later


def _real_perf(monkeypatch):
    """真的 stock_picks_perf（autouse 的假模組先拿掉；測試結束會換回來）；載不到回 None。"""
    monkeypatch.delitem(sys.modules, "stock_picks_perf", raising=False)
    try:
        return importlib.import_module("stock_picks_perf")
    except Exception:
        return None


def full_perf_result(perf) -> dict:
    """成績模組結果檔的形狀，兩個市場、兩個天數都有成績、有跳過、有 AI 起始日（卡片最長的情況）。"""
    h = {"n": 30, "r_pm": -123, "b_pm": 567, "wins": 18, "skipped": 3, "pending": 2}
    market = {"first_t": "2026-10-08", "from": "2026-10-08", "to": "2026-10-28", "ai_from": "2026-10-24",
              "h": {str(x): dict(h) for x in perf.HORIZONS}}
    return {"schema": perf.RESULT_SCHEMA, "computed_at": int(NOW), "ledger_rows": 120, "ledger_full": False,
            "pending": 8, "skipped": 12, "markets": {perf.TW: dict(market), perf.US: dict(market, ai_from=None)}}


def test_worst_case_size_fits(monkeypatch):
    """8 張（台股 4＋美股 3＋成績 1）、名稱都最長：≤ 25 KB；加大字版按鈕（每張約 402 B）≤ 30 KB。
    成績卡用真的 perf_bubble（完整結果，約 2.5 KB）；成績模組載不到時用同樣大小的假卡（P4 #8）。"""
    long_name = "A" * us.NAME_MAX
    tw = [sp._bubble("股票推薦：第 1 檔（共 3 檔）", "大盤：中性", [sp._text("台" * 40, size="md")] * 12, [sp._text("x")])] * 4
    usc = [us.us_pick_bubble(_facts(name=long_name, symbol="ABCDE.F"), i + 1, 3, sp.Market(sp.HOT, 70, 130),
                             lead_note=i == 0) for i in range(3)]
    perf_module = _real_perf(monkeypatch)
    if perf_module is not None:
        perf = perf_module.perf_bubble(full_perf_result(perf_module))
        assert "推薦成績計算中" not in json.dumps(perf, ensure_ascii=False)  # 真的是完整的那張
    else:
        perf = sp._bubble("股票推薦：推薦成績", None, [sp._text("推" * 40, size="md")] * 16, [sp._text("x" * 60)] * 3)
    assert us._card_bytes(perf) >= 1800
    assert all(us._card_bytes(card) <= 3500 for card in usc)
    size = us._card_bytes(sp.carousel(tw + usc + [perf]))
    assert size <= us.MAX_CARD_BYTES
    assert size + 8 * 402 <= 30 * 1024


def test_fixed_texts_pass_main_outbound_and_validator():
    import main  # noqa: PLC0415  （重：只在這個測試載入）

    for text in (us.TEXT_RESERVED, us.TEXT_US_NOT_FOUND, us.TEXT_PHASE3_OFF, us.TEXT_US_UNSUPPORTED,
                 us.TEXT_US_EXCHANGE, us.TEXT_US_NO_TRADE):
        assert main._prepare_outbound_text(text) == text
    for card in (us.us_pick_bubble(_facts(), 1, 1, None, lead_note=True), us.us_detail_bubble(_facts())):
        for text in _all_texts(card):
            assert main._prepare_outbound_text(text) == text


# ── 4. DB 推薦池 ───────────────────────────────────────────────────────────────────
def test_parse_pool_dedupes_share_classes_and_validates(small_pool, monkeypatch):
    monkeypatch.setattr(us, "POOL_N", 6)  # 6 檔的名額：GOOG 和 GOOGL 同公司，只留權重大的
    pool = us.parse_pool(holdings())
    assert [m.symbol for m in pool.members] == ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL"]
    assert pool.as_of == date(2026, 10, 26) and pool.members[0].name == "NVIDIA CORP"
    assert us.parse_pool(holdings(dict(list(POOL.items())[:4]))) is None  # 列數不夠
    assert us.parse_pool(holdings({**POOL, "XOM": ("EXXON MOBIL CORP", 30.0)})) is None  # 合計 131
    assert us.parse_pool(holdings(as_of="bad")) is None and us.parse_pool(None) is None
    bad = holdings()
    bad["rows"] += [["BAD1", "X", float("nan"), None], ["lower", "X", 1.0, None], ["ZZZ", "X", True, None]]
    assert [m.symbol for m in us.parse_pool(bad).members] == ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL"]


def test_select_d_rules():
    cal = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]
    full = {d: 50 for d in cal}
    assert us.select_d(cal, full, 50, seen_latest=None, seen_latest_at=None, now=NOW) == "2026-10-27"
    partial = {**full, "2026-10-27": 48}
    seen = date(2026, 10, 27)
    assert us.select_d(cal, partial, 50, seen_latest=seen, seen_latest_at=NOW - 9 * 3600, now=NOW) == "2026-10-26"
    assert us.select_d(cal, partial, 50, seen_latest=seen, seen_latest_at=NOW - 10 * 3600, now=NOW) == "2026-10-27"
    three_missing = {**full, "2026-10-27": 47}
    assert us.select_d(cal, three_missing, 50, seen_latest=seen, seen_latest_at=NOW - 20 * 3600, now=NOW) == "2026-10-26"
    assert us.select_d(cal, {}, 50, seen_latest=None, seen_latest_at=None, now=NOW) is None


def test_db_trend_window_quarantine_and_no_jump_gate():
    cal = [d.isoformat() for d in CAL]
    series = dict(zip(cal, rising(0.1)))
    trend, reason = us.db_trend(series, cal, cal[-1], ())
    assert trend is not None and reason == ""
    assert us.db_trend({k: v for k, v in series.items() if k != cal[-1]}, cal, cal[-1], ()) == (None, "missing")
    assert us.db_trend(series, cal, cal[-1], [("2026-01-05", "2026-01-06")]) == (None, "quarantine")
    assert us.db_trend(series, cal, cal[-1], [("2024-01-01", "2024-01-02")])[0] is not None  # 窗外的不算
    jumped = dict(series)
    jumped[cal[-30]] = series[cal[-30]] * 1.5  # DB 路徑不套 1.45：真的大漲照算
    assert us.db_trend(jumped, cal, cal[-1], ())[0] is not None
    short = {k: v for k, v in list(series.items())[-200:]}
    assert us.db_trend(short, cal, cal[-1], ()) == (None, "short")
    five_missing = {k: v for i, (k, v) in enumerate(series.items()) if i not in (30, 60, 90, 120, 150)}
    assert us.db_trend(five_missing, cal, cal[-1], ())[0] is not None  # 年線窗缺 ≤ 5 天照算
    six_missing = {k: v for i, (k, v) in enumerate(series.items()) if i not in (30, 60, 90, 120, 150, 180)}
    assert us.db_trend(six_missing, cal, cal[-1], ()) == (None, "short")
    quarter_holes = {k: v for i, (k, v) in enumerate(series.items()) if i not in (210, 220, 230)}
    assert us.db_trend(quarter_holes, cal, cal[-1], ()) == (None, "short")  # 季線窗缺 3 天


def test_round_uses_latest_complete_date_and_skips_provisional(small_pool, yahoo):
    make_db({**SERIES, "MSFT": MSFT_NO_D})
    add_closes("MSFT", [(D, 999.0, 1)], provisional=True)  # 備援列不算到齊
    full_round()
    assert us._snapshot.d == D - timedelta(days=1)  # 10/26 才到齊
    assert us._worker.seen_latest == D and us._worker.behind_since == NOW


def test_straggler_after_10_hours_goes_gray(small_pool, yahoo):
    make_db({**SERIES, "MSFT": MSFT_NO_D})
    full_round()
    assert us._snapshot.d == D - timedelta(days=1)
    later = NOW + 10 * 3600
    full_round(later)
    snap = us._snapshot
    assert snap.d == D and snap.facts_of("MSFT").trend is None and snap.facts_of("MSFT").gray == "missing"
    assert us._worker.behind_since is None and snap.covered == 4


def test_too_many_gray_keeps_old_snapshot_and_saves_requests(small_pool, yahoo):
    make_db({**SERIES, "MSFT": rising(0.12)[-150:], "AAPL": hot()[-150:]})
    full_round()
    assert us._snapshot is None and us._worker.last_fail == "too_many_gray" and us._worker.fail_count == 1
    assert yahoo.calls == []  # 走勢就過不了閘門：不打 Yahoo


def test_pool_falls_back_to_previous_pool(small_pool, yahoo):
    make_db()
    full_round()
    first = us._snapshot
    store = market_store.Store(db_path(), now_fn=lambda: NOW)
    store.put_obs(us.HOLDINGS_OBS, holdings(dict(list(POOL.items())[:3])), data_date="2026-10-27")  # 不合格
    store.close()
    nxt = D + timedelta(days=1)
    for symbol, value in {**{k: v[-1] + 0.1 for k, v in SERIES.items()}, "SPY": 700.0, "VOO": 700.0, "QQQ": 700.0}.items():
        add_closes(symbol, [(nxt, value, 1)])
    later = datetime(2026, 10, 29, 15, 0, tzinfo=TPE).timestamp()
    full_round(later)
    assert us._snapshot.d == nxt and [f.symbol for f in us._snapshot.pool] == [f.symbol for f in first.pool]


def test_missing_table_is_db_unavailable(small_pool, yahoo):
    con = sqlite3.connect(db_path())
    con.execute("CREATE TABLE unrelated (x)")
    con.commit()
    con.close()
    delay = full_round()
    assert us._worker.last_fail == "db_unavailable" and us._worker.fail_count == 1
    assert delay == pytest.approx(us.DB_RETRY_S)


# ── 5. 狀態檔與 worker ─────────────────────────────────────────────────────────────
def test_full_round_writes_snapshot_history_and_perf(small_pool, yahoo, fake_perf):
    make_db()
    assert full_round() == us.POLL_S
    snap = us._snapshot
    assert snap.d == D and snap.rule == us.RULE and snap.covered == 5
    assert snap.picks == ("GOOGL", "NVDA", "MSFT")
    assert [f.symbol for f in snap.pool] == ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL"]
    assert snap.spy is not None and snap.spy_dates[-1] == D and len(snap.spy_dates) == 260
    assert snap.latest_closes["NVDA"] == (D, pytest.approx(125.9))
    assert sorted(yahoo.fund_calls()) == ["AAPL", "AMZN", "GOOGL", "MSFT", "NVDA"]
    assert all(host == "query1.finance.yahoo.com" for host, *_ in yahoo.calls)
    lines = history_lines()
    assert len(lines) == 1 and lines[0]["t"] == "2026-10-27" and lines[0]["rule"] == "us_v1"
    assert [p["code"] for p in lines[0]["picks"]] == ["GOOGL", "NVDA", "MSFT"] and lines[0]["picks"][0]["score"] == 4
    assert fake_perf.calls == [(True, NOW)]
    worker = read_worker_file()
    assert worker["fund_count"] == 5 and worker["fund_day"] == "2026-10-28" and len(worker["fund"]) == 5
    for name in (us.SNAPSHOT_NAME, us.WORKER_NAME, us.HISTORY_NAME):
        assert (sp.STATE_DIR / name).stat().st_mode & 0o777 == 0o600
    # 同一個 D 不再算第二次（不打 Yahoo、計算時間不變），只是推薦成績照更新
    full_round(NOW + 1800)
    assert us._snapshot.computed_at == snap.computed_at and len(yahoo.calls) == 5
    assert len(history_lines()) == 1 and len(fake_perf.calls) == 2


def test_snapshot_round_trip_and_reload(small_pool, yahoo):
    make_db()
    full_round()
    snap = us._snapshot
    payload = json.loads((sp.STATE_DIR / us.SNAPSHOT_NAME).read_text())
    assert us.snapshot_from_json(payload, NOW) == snap
    us.reset_for_tests()
    us._ensure_loaded()
    assert us._snapshot == snap and us._worker.fund_count == 5


def test_rule_change_recomputes_same_d(small_pool, yahoo, monkeypatch):
    make_db()
    full_round()
    monkeypatch.setattr(us, "RULE", "us_v2")
    full_round(NOW + 1800)
    assert us._snapshot.rule == "us_v2" and us._snapshot.d == D
    assert len(history_lines()) == 1  # 紀錄保留第一次那行
    assert len(yahoo.fund_calls()) == 5  # 財報 3 天內不重抓


def test_rule_change_does_not_recompute_an_older_d(small_pool, yahoo, monkeypatch):
    make_db()
    full_round()
    snap = us._snapshot
    newer = us.replace(snap, d=D + timedelta(days=1))  # 快照比 DB 算得出來的 D 新（DB 退回）
    assert us._commit_snapshot(newer, NOW + 60, refresh_only=True) == us.COMMIT_WRITTEN
    monkeypatch.setattr(us, "RULE", "us_v2")
    computed_at = us._snapshot.computed_at
    full_round(NOW + 1800)
    assert us._snapshot.d == D + timedelta(days=1) and us._snapshot.computed_at == computed_at


def test_history_is_backfilled_every_round(small_pool, yahoo):
    make_db()
    full_round()
    (sp.STATE_DIR / us.HISTORY_NAME).unlink()
    full_round(NOW + 1800)
    assert [line["t"] for line in history_lines()] == ["2026-10-27"]


def test_db_busy_is_not_yet_within_two_seconds(small_pool, yahoo):
    make_db()
    writer = sqlite3.connect(db_path(), timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        delay = full_round()
        assert time.monotonic() - start < 2.5
    finally:
        writer.rollback()
        writer.close()
    assert us._worker.last_wait == "db_busy" and us._worker.fail_count == 0
    assert delay == pytest.approx(us.NOT_YET_RETRY_S)


def test_missing_db_fails_and_backoff_only_extends(fake_perf):
    delay = full_round()
    assert us._worker.last_fail == "db_unavailable" and delay == pytest.approx(us.DB_RETRY_S)
    assert fake_perf.calls  # 推薦成績照跑
    # 退避中：不再讀 DB
    assert full_round(NOW + 60) == pytest.approx(us.DB_RETRY_S - 60)
    # 只延長不縮短：比較晚的期限（別的程序的冷卻）不會被較短的覆蓋
    us._set_worker(us.replace(us._worker, next_try=NOW + 5000))
    assert us._fail("x", NOW, retry_s=60) == pytest.approx(5000)


def test_not_yet_does_not_count_as_failure(monkeypatch):
    us._set_worker(us.UsWorker(fail_count=2))
    us._fail("pool_incomplete", NOW, not_yet=True)
    assert us._worker.fail_count == 2 and us._worker.last_wait == "pool_incomplete"
    us._fail("boom", NOW + 10 ** 4)
    assert us._worker.fail_count == 3 and us._worker.last_wait == ""


def test_fund_requests_are_charged_before_sending(small_pool, monkeypatch):
    make_db()
    seen = []

    def check_ledger(host, path):
        if "fundamentals" in path:
            seen.append(read_worker_file()["fund_count"])

    fake = FakeYahoo(funds={s: good_fund(s) for s in POOL}, hook=check_ledger)
    monkeypatch.setattr(sp, "_get_json", fake)
    full_round()
    assert seen and all(count == 5 for count in seen)  # 第一個請求送出前，整批 5 次已經存進檔案


def test_fund_per_round_cap_and_waiting_gate(small_pool, yahoo, monkeypatch):
    make_db()
    monkeypatch.setattr(us, "FUND_PER_ROUND", 2)
    full_round()
    assert us._snapshot is None and us._worker.last_wait == "fund_waiting" and us._worker.fail_count == 0
    assert yahoo.fund_calls() == ["NVDA", "MSFT"]  # 沒抓過的照池的順序
    full_round(NOW + us.NOT_YET_RETRY_S + 1)
    snap = us._snapshot  # 只剩 1 檔還在等（≤ 兩成）：照算，那一檔寫「財報暫時抓不到」，不算進閘門
    assert snap is not None and yahoo.fund_calls() == ["NVDA", "MSFT", "AAPL", "AMZN"]
    googl = snap.facts_of("GOOGL")
    assert googl.fund == us.FUND_UNAVAILABLE and us.us_light_of(googl).text == "走勢可以，財報暫時抓不到"
    assert snap.picks == ("NVDA", "MSFT")


def test_fund_daily_cap_uses_taipei_date(small_pool, yahoo, monkeypatch):
    make_db()
    monkeypatch.setattr(us, "FUND_PER_DAY", 3)
    full_round()
    assert len(yahoo.fund_calls()) == 3 and us._worker.last_wait == "fund_waiting"
    full_round(NOW + us.NOT_YET_RETRY_S + 1)
    assert len(yahoo.fund_calls()) == 3  # 同一個台北日期：額度用完
    tomorrow = datetime(2026, 10, 29, 0, 30, tzinfo=TPE).timestamp()  # 台北換日（美東還是 10/28）
    full_round(tomorrow)
    assert len(yahoo.fund_calls()) == 5 and us._snapshot is not None
    assert us._worker.fund_day == "2026-10-29" and us._worker.fund_count == 2


def test_fund_refresh_every_three_days(small_pool, yahoo):
    make_db()
    full_round()
    fund = dict(us._worker.fund)
    fund["NVDA"] = us.replace(fund["NVDA"], at=NOW - 4 * 86400)
    us._set_worker(us.replace(us._worker, fund=fund))
    us._set_snapshot(None)  # 讓下一輪重算
    full_round(NOW + 1800)
    assert yahoo.fund_calls()[5:] == ["NVDA"]  # 只有超過 3 天的那檔重抓


def test_rate_limit_stops_round_blocks_shared_and_keeps_fetched(small_pool, monkeypatch):
    make_db()
    funds = {s: good_fund(s) for s in POOL}
    funds["MSFT"] = sp.FetchError("rate_limited")
    fake = FakeYahoo(funds=funds)
    monkeypatch.setattr(sp, "_get_json", fake)
    delay = full_round()
    assert us._worker.last_fail == "yahoo_rate_limited" and delay == pytest.approx(sp.BLOCKED_BACKOFF_S)
    assert us._worker.bg_yahoo_until == pytest.approx(NOW + sp.BLOCKED_BACKOFF_S)
    assert sp._yahoo_blocked_until > time.time()  # 計畫 C 的共用冷卻也設了
    assert fake.fund_calls() == ["NVDA", "MSFT"]
    assert set(read_worker_file()["fund"]) == {"NVDA"}  # 已抓的留在快取


def test_shared_cooldown_means_no_requests(small_pool, yahoo, monkeypatch):
    make_db()
    monkeypatch.setattr(sp, "_yahoo_blocked_until", time.time() + 600)
    full_round()
    assert yahoo.calls == [] and us._worker.last_wait == "fund_waiting"


def test_fallback_host_is_charged_too(small_pool, monkeypatch):
    make_db()
    calls = []

    def flaky(host, path, **kwargs):
        calls.append(host)
        if host == "query1.finance.yahoo.com":
            raise sp.FetchError("network")
        return 200, good_fund(unquote(path.rsplit("/", 1)[-1])), None

    monkeypatch.setattr(sp, "_get_json", flaky)
    full_round()
    assert calls.count("query2.finance.yahoo.com") == 5 and us._worker.fund_count == 10
    assert us._snapshot is not None


def test_behind_since_is_not_reset_by_newer_dates(small_pool, yahoo):
    make_db({**SERIES, "MSFT": MSFT_NO_D})
    full_round()
    assert us._snapshot.d == D - timedelta(days=1) and us._worker.behind_since == NOW
    nxt = D + timedelta(days=1)
    for etf in ("SPY", "VOO", "QQQ"):
        add_closes(etf, [(nxt, 700.0, 1)])
    full_round(NOW + 3600)
    w = us._worker
    assert w.seen_latest == nxt and w.behind_since == NOW
    assert not us._not_updated(us._snapshot, w, NOW + 11 * 3600)
    assert us._not_updated(us._snapshot, w, NOW + 12 * 3600)


def test_not_updated_rules():
    snap = us.UsSnapshot(d=D, rule=us.RULE, covered=5, computed_at=NOW, universe_as_of=None, spy=None,
                         spy_dates=(), pool=(_facts(),), picks=(), missed=None)
    assert not us._not_updated(snap, us.UsWorker(last_ok_check=NOW), NOW)
    assert us._not_updated(snap, us.UsWorker(last_ok_check=NOW + 7 * 86400), NOW + 8 * 86400)  # D 比美東今天早 > 7 天
    assert us._not_updated(snap, us.UsWorker(last_ok_check=NOW), NOW + 36 * 3600)  # 36 小時沒成功檢查


def test_flag_off_only_runs_perf(monkeypatch, fake_perf, yahoo):
    monkeypatch.setenv(us.FLAG_ENV, "0")
    us.reset_for_tests()
    assert us.ENABLED is False
    make_db()
    assert full_round() == us.POLL_S
    assert yahoo.calls == [] and us._snapshot is None and us._worker.last_ok_check is None
    assert fake_perf.calls == [(True, NOW)]
    assert us.handle_symbol("NVDA").text == sp.TEXT_US
    reply = sp.Reply(flex=sp.carousel([sp._bubble("股票推薦：x", None, [sp._text("x")], [sp._text("y")])]), alt_text="股票推薦：只供參考｜x")
    assert us.extend_picks_reply(reply) is reply
    assert us.us_reply().text == us.TEXT_PHASE3_OFF and us.perf_reply().text == us.TEXT_PHASE3_OFF


def test_perf_failure_does_not_break_round(small_pool, yahoo, fake_perf):
    make_db()
    fake_perf.raise_update = True
    assert full_round() == us.POLL_S and us._snapshot is not None


def test_perf_module_missing_is_tolerated(small_pool, yahoo, monkeypatch):
    monkeypatch.setitem(sys.modules, "stock_picks_perf", None)  # import 會失敗
    make_db()
    assert full_round() == us.POLL_S and us._snapshot is not None
    assert us.perf_reply().text == sp.TEXT_US_CARDS_FAILED
    reply = us.us_reply()
    assert reply.flex["type"] == "carousel" and len(reply.flex["contents"]) == 3  # 沒有成績卡
    assert us.status_summary()["perf"] is None


def test_worker_merge_takes_larger_ledger_later_backoff_newer_cache():
    entry_old = us.FundEntry(at=NOW - 100, rev=_rows([1, 2, 3, 4, 5]))
    entry_new = us.FundEntry(at=NOW - 10, rev=_rows([1, 2, 3, 4, 6]))
    disk = us.UsWorker(fund_day="2026-10-28", fund_count=30, next_try=NOW + 3000, bg_yahoo_until=NOW + 900,
                       fund={"NVDA": entry_new, "MSFT": entry_old}, writer="other", last_ok_check=NOW - 5)
    mem = us.UsWorker(fund_day="2026-10-28", fund_count=10, next_try=NOW + 60, fund={"NVDA": entry_old},
                      last_ok_check=NOW - 50)
    merged = us._merge_worker(mem, disk, NOW)
    assert merged.fund_count == 30 and merged.next_try == NOW + 3000 and merged.bg_yahoo_until == NOW + 900
    assert merged.fund["NVDA"] == entry_new and "MSFT" in merged.fund and merged.last_ok_check == NOW - 5
    newer_day = us._merge_worker(us.replace(mem, fund_day="2026-10-29", fund_count=1), disk, NOW)
    assert (newer_day.fund_day, newer_day.fund_count) == ("2026-10-29", 1)
    own = us._merge_worker(mem, us.replace(disk, writer=us._WRITER_ID), NOW)
    assert own.next_try == NOW + 60  # 自己上次寫的退避不併回來
    assert own.fund_count == 30  # 帳一律取大的


def test_corrupt_worker_file_backs_off_and_is_repaired(small_pool, yahoo):
    make_db()
    (sp.STATE_DIR / us.WORKER_NAME).write_text("{not json")
    delay = full_round()
    assert delay == pytest.approx(sp.STATE_CORRUPT_BACKOFF_S)
    assert us._worker.last_fail == "state_corrupt" and us._worker.fund_count == us.FUND_PER_DAY
    assert read_worker_file()["schema"] == us.WORKER_SCHEMA  # 修好了（退避一起存）
    assert yahoo.calls == []


def test_future_times_are_clamped():
    obj = us.worker_to_json(us.UsWorker(next_try=NOW + 10 ** 7, last_ok_check=NOW + 10 ** 7, behind_since=NOW + 10 ** 6,
                                        bg_yahoo_until=NOW + 10 ** 7, seen_latest=date(2030, 1, 1), seen_latest_at=NOW,
                                        fund={"NVDA": us.FundEntry(at=NOW + 10 ** 6, rev=_rows([1, 2]))}))
    w = us.worker_from_json(json.loads(json.dumps(obj)), NOW)
    assert w.next_try <= NOW + us._MAX_BACKOFF_S + sp._CLOCK_SKEW_S
    assert w.last_ok_check <= NOW + sp._CLOCK_SKEW_S and w.behind_since <= NOW
    assert w.bg_yahoo_until <= NOW + sp.BLOCKED_BACKOFF_S + sp._CLOCK_SKEW_S
    assert w.seen_latest is None and w.fund["NVDA"].at == NOW


def test_seed_worker_file_with_only_fund_loads(small_pool, yahoo):
    """部署種子檔只帶財報快取（PLAN §10.3）：其他欄位缺著也要讀得進來，第一輪就不必重抓。"""
    entry = us._entry_from_parsed(us.parse_fundamentals(good_fund("NVDA")), NOW - 3600)
    seed = {"schema": us.WORKER_SCHEMA, "fund": us.worker_to_json(us.UsWorker(fund={"NVDA": entry}))["fund"]}
    (sp.STATE_DIR / us.WORKER_NAME).write_text(json.dumps(seed))
    make_db()
    full_round()
    assert us._worker.last_fail == "" and us._snapshot is not None
    assert sorted(yahoo.fund_calls()) == ["AAPL", "AMZN", "GOOGL", "MSFT"]  # NVDA 用種子的


def test_run_loop_waits_at_most_one_round(monkeypatch):
    """美股在長退避時，背景照樣每 30 分鐘跑一輪（推薦紀錄、推薦成績不停）。"""

    class FakeStop:
        def __init__(self):
            self.waits = []

        def is_set(self):
            return len(self.waits) >= 3

        def wait(self, timeout):
            self.waits.append(timeout)
            return len(self.waits) >= 3

    monkeypatch.setattr(us, "_tick", lambda stop, now: 3600.0)
    stop = FakeStop()
    us._run(stop)
    assert stop.waits == [us.STARTUP_DELAY_S, float(us.POLL_S), float(us.POLL_S)]


def test_bad_fund_entry_is_dropped_not_the_ledger():
    obj = us.worker_to_json(us.UsWorker(fund_day="2026-10-28", fund_count=7,
                                        fund={"NVDA": us.FundEntry(at=NOW - 1, rev=_rows([1, 2]))}))
    obj["fund"]["MSFT"] = {"at": "bad"}
    w = us.worker_from_json(json.loads(json.dumps(obj)), NOW)
    assert set(w.fund) == {"NVDA"} and w.fund_count == 7


def test_snapshot_validation_rejects_bad_files(small_pool, yahoo):
    make_db()
    full_round()
    good = json.loads((sp.STATE_DIR / us.SNAPSHOT_NAME).read_text())
    for mutate in (
        lambda o: o.update(schema=99),
        lambda o: o.update(d="2030-01-01"),
        lambda o: o["pool"][0].update(s="nvda"),
        lambda o: o["pool"][0].update(n="Bad@Name"),
        lambda o: o["pool"][0].update(tr=[1e9, 1, 1, 0]),
        lambda o: o.update(picks=["ZZZZ"]),
        lambda o: o["latest_closes"].update(NVDA=["2026-10-27", -1]),
    ):
        broken = json.loads(json.dumps(good))
        mutate(broken)
        with pytest.raises((ValueError, TypeError, KeyError)):
            us.snapshot_from_json(broken, NOW)
    future = json.loads(json.dumps(good))
    future["computed_at"] = NOW + 10 ** 6
    assert us.snapshot_from_json(future, NOW).computed_at == NOW + sp._CLOCK_SKEW_S
    (sp.STATE_DIR / us.SNAPSHOT_NAME).write_text("[]")
    assert us._load_snapshot_file(NOW) is None


def test_commit_keeps_newer_disk_snapshot(small_pool, yahoo):
    make_db()
    full_round()
    snap = us._snapshot
    older = us.replace(snap, d=D - timedelta(days=1), computed_at=NOW + 1)
    assert us._commit_snapshot(older, NOW) == us.COMMIT_DISK and us._snapshot.d == D
    same = us.replace(snap, computed_at=NOW + 2, picks=())
    assert us._commit_snapshot(same, NOW) == us.COMMIT_DISK and us._snapshot.picks == snap.picks  # 每個 D 只算一次
    refreshed = us.replace(snap, latest_closes={"NVDA": (D, 1.0)})
    assert us._commit_snapshot(refreshed, NOW, refresh_only=True) == us.COMMIT_WRITTEN


def test_latest_closes_refresh_every_round(small_pool, yahoo):
    make_db()
    full_round()
    add_closes("TSLA", [(D, 250.0, 1)])
    full_round(NOW + 1800)
    assert us._snapshot.latest_closes["TSLA"] == (D, 250.0)
    disk = json.loads((sp.STATE_DIR / us.SNAPSHOT_NAME).read_text())
    assert disk["latest_closes"]["TSLA"] == ["2026-10-27", 250.0]


def test_stale_tmp_files_are_swept(small_pool, yahoo):
    old = sp.STATE_DIR / ".stock_picks_us.json.1.2.3.tmp"
    keep = sp.STATE_DIR / ".stock_picks_tw.json.1.2.3.tmp"
    for path in (old, keep):
        path.write_text("x")
        os.utime(path, (NOW - 7200, NOW - 7200))
    full_round()
    assert not old.exists() and keep.exists()


def test_background_start_stop(monkeypatch):
    us.start_background()
    assert us._thread is None  # conftest 關掉了背景
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    sp.start_background()  # 台股的 hook：美股 worker 放在台股提早 return 之前
    try:
        assert us._thread is not None and us._thread.is_alive() and us._thread.name == "stock-picks-us"
        us.start_background()  # 還活著就不再起
    finally:
        sp.stop_background(timeout=3.0)
    assert not us._thread.is_alive()


# ── 6. 單檔即時查 ───────────────────────────────────────────────────────────────────
def lookup_yahoo(monkeypatch, *, charts=None, funds=None, hook=None) -> FakeYahoo:
    fake = FakeYahoo(charts=charts, funds=funds, hook=hook)
    monkeypatch.setattr(sp, "_get_json", fake)
    return fake


def test_pool_symbol_is_served_from_snapshot_without_network(small_pool, yahoo, monkeypatch):
    make_db()
    full_round()
    monkeypatch.setattr(sp, "_get_json", _no_network)
    reply = us.handle_symbol("NVDA", deadline=time.monotonic() + 20)
    texts = _all_texts(reply.flex)
    assert texts[0] == "股票評估：NVDA（NVIDIA CORP）" and "✅ 4 條都符合" in texts
    assert "依據：收盤（Nasdaq 資料）、財報（Yahoo）｜資料到 10/27（美國時間）" in texts
    assert reply.alt_text == "股票評估：只供參考｜NVDA（NVIDIA CORP）｜✅ 4 條都符合"
    assert reply.fallback_text == sp.TEXT_EVAL_CARD_FAILED and stock_card_check.card_ok(reply.flex, reply.alt_text)


def test_yahoo_path_card_verified_against_nasdaq(small_pool, yahoo, monkeypatch):
    make_db(extra={"TSLA": rising(0.2)})
    full_round()
    fake = lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)), long_name="Tesla, Inc.")},
                        funds={"TSLA": good_fund("TSLA")})
    reply = us.handle_symbol("TSLA", deadline=time.monotonic() + 20)
    texts = _all_texts(reply.flex)
    assert texts[0] == "股票評估：TSLA（Tesla, Inc.）"
    assert "依據：Yahoo 收盤、Yahoo 財報｜收盤和 Nasdaq 資料核對過｜資料到 10/27（美國時間）" in texts
    assert [h for h, *_ in fake.calls] == ["query1.finance.yahoo.com"] * 2  # 只打 query1、依序兩個請求
    assert all(max(timeout) <= us.LOOKUP_FETCH_S for _, _, timeout, _ in fake.calls)


def test_yahoo_close_mismatch_goes_gray(small_pool, yahoo, monkeypatch):
    make_db(extra={"TSLA": rising(0.2)[:-1] + [rising(0.2)[-1] * 1.02]})
    full_round()
    lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)))}, funds={})
    reply = us.handle_symbol("TSLA", deadline=time.monotonic() + 20)
    texts = _all_texts(reply.flex)
    assert "⚪ 資料不足" in texts
    assert not any("收盤 " in t and "美元" in t for t in texts)  # 對不上的數字不上卡
    assert "⬜ 有在成長：財報暫時抓不到" in texts and "本益比：財報暫時抓不到" in texts  # 財報沒抓：不說「資料不足」
    assert not any(t in texts for t in ("⬜ 有在成長：營收資料不足", "⬜ 有在賺錢：獲利資料不足", "本益比資料不足"))


def test_gray_pool_symbol_uses_live_lookup(small_pool, yahoo, monkeypatch):
    make_db({**SERIES, "MSFT": MSFT_NO_D})
    full_round(NOW)
    full_round(NOW + 10 * 3600)  # MSFT ⚪（missing）
    set_clock(monkeypatch, NOW + 10 * 3600)
    fake = lookup_yahoo(monkeypatch, charts={"MSFT": chart_payload("MSFT", bars_of(rising(0.12)), long_name="Microsoft")},
                        funds={})
    reply = us.handle_symbol("MSFT", deadline=time.monotonic() + 20)
    assert fake.calls and "股票評估：MSFT（Microsoft）" in _all_texts(reply.flex)


def test_etf_lookup_skips_fundamentals(monkeypatch):
    fake = lookup_yahoo(monkeypatch, charts={"SOXL": chart_payload("SOXL", bars_of(rising(0.1)), kind="ETF", exchange="PCX",
                                                                  long_name="Direxion Daily Semiconductor Bull 3X Shares")})
    reply = us.handle_symbol("SOXL", deadline=time.monotonic() + 20)
    texts = _all_texts(reply.flex)
    assert "✅ 走勢 2 條都符合（ETF 只看走勢）" in texts and us.TEXT_ETF_NA in texts
    assert len(fake.calls) == 1


@pytest.mark.parametrize("payload_kw, expected", [
    ({"currency": "EUR"}, us.TEXT_US_UNSUPPORTED),
    ({"kind": "MUTUALFUND"}, us.TEXT_US_UNSUPPORTED),
    ({"exchange": "PNK"}, us.TEXT_US_EXCHANGE),
])
def test_unsupported_lookups(monkeypatch, payload_kw, expected):
    lookup_yahoo(monkeypatch, charts={"ABCD": chart_payload("ABCD", bars_of(rising(0.1)), **payload_kw)})
    assert us.handle_symbol("ABCD", deadline=time.monotonic() + 20).text == expected


def test_404_vs_other_errors(monkeypatch):
    not_found_body = {"chart": {"result": None, "error": {"code": "Not Found", "description": "x"}}}
    lookup_yahoo(monkeypatch, charts={"ZZZZ": sp.FetchError("not_found"), "YYYY": not_found_body,
                                      "XXXX": sp.FetchError("status"), "WWWW": chart_payload("OTHER", bars_of(rising()))})
    assert us.handle_symbol("ZZZZ", deadline=time.monotonic() + 20).text == us.TEXT_US_NOT_FOUND
    assert us.handle_symbol("YYYY", deadline=time.monotonic() + 20).text == us.TEXT_US_NOT_FOUND
    assert us.handle_symbol("XXXX", deadline=time.monotonic() + 20).text == sp.TEXT_LOOKUP_FAILED
    assert us.handle_symbol("WWWW", deadline=time.monotonic() + 20).text == sp.TEXT_LOOKUP_FAILED  # meta.symbol 對不上


def test_lookup_limit_and_cache(monkeypatch):
    charts = {s: sp.FetchError("not_found") for s in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE")}
    fake = lookup_yahoo(monkeypatch, charts=charts)
    for symbol in ("AAAA", "BBBB", "CCCC", "DDDD"):
        assert us.handle_symbol(symbol, deadline=time.monotonic() + 20).text == us.TEXT_US_NOT_FOUND
    assert us.handle_symbol("EEEE", deadline=time.monotonic() + 20).text == sp.TEXT_BUSY
    assert us.handle_symbol("AAAA", deadline=time.monotonic() + 20).text == us.TEXT_US_NOT_FOUND  # 快取，不算次數
    assert len(fake.calls) == 4
    set_clock(monkeypatch, NOW + us.LOOKUP_FAIL_TTL_S + 1)  # 失敗只快取 10 分鐘
    with us._lookup_lock:
        us._lookup_times.clear()
    us.handle_symbol("AAAA", deadline=time.monotonic() + 20)
    assert len(fake.calls) == 5


def test_success_cached_until_next_us_close(monkeypatch):
    fake = lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)))},
                        funds={"TSLA": good_fund("TSLA")})
    assert us.handle_symbol("TSLA", deadline=time.monotonic() + 20).flex is not None
    set_clock(monkeypatch, datetime(2026, 10, 28, 16, 59, tzinfo=NY).timestamp())
    us.handle_symbol("TSLA", deadline=time.monotonic() + 20)
    assert len(fake.calls) == 2
    set_clock(monkeypatch, datetime(2026, 10, 28, 17, 1, tzinfo=NY).timestamp())
    us.handle_symbol("TSLA", deadline=time.monotonic() + 20)
    assert len(fake.calls) == 4


def test_deadline_rules(monkeypatch):
    fake = lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)))},
                        funds={"TSLA": good_fund("TSLA")})
    assert us.handle_symbol("TSLA", deadline=time.monotonic() + 4.0).text == sp.TEXT_NO_TIME
    assert fake.calls == []
    monkeypatch.setattr(us, "LOOKUP_FUND_MIN_S", 100.0)  # 抓完日線後預算不夠抓財報
    reply = us.handle_symbol("TSLA", deadline=time.monotonic() + 5.5)
    assert "⚠️ 走勢可以，財報暫時抓不到" in _all_texts(reply.flex) and len(fake.calls) == 1
    connect, read = fake.calls[0][2]
    assert fake.calls[0][3] <= 2.5 and connect + 2 * read <= 2.5 + 1e-6  # 總預算 min(6, 5.5 − 3)，一個請求拖不過它
    assert us._lookup_cache["TSLA"][0] == pytest.approx(NOW + us.LOOKUP_FAIL_TTL_S)  # 只快取 10 分鐘


def test_lookup_uses_fresh_background_fund_cache(monkeypatch):
    us._ensure_loaded()
    parsed = us.parse_fundamentals(good_fund("TSLA"))
    us._set_worker(us.replace(us._worker, fund={"TSLA": us._entry_from_parsed(parsed, NOW - 3600)}))
    fake = lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)))})
    reply = us.handle_symbol("TSLA", deadline=time.monotonic() + 20)
    assert len(fake.calls) == 1 and "✅ 4 條都符合" in _all_texts(reply.flex)


def test_lookup_rate_limit_uses_own_cooldown_only(monkeypatch):
    fake = lookup_yahoo(monkeypatch, charts={"TSLA": sp.FetchError("rate_limited"), "AAPL": chart_payload("AAPL", bars_of(rising()))})
    assert us.handle_symbol("TSLA", deadline=time.monotonic() + 20).text == sp.TEXT_LOOKUP_FAILED
    assert us._lookup_yahoo_until == pytest.approx(NOW + us.LOOKUP_BLOCK_S)
    assert sp._yahoo_blocked_until == 0.0  # 不設共用冷卻
    assert us.handle_symbol("AAPL", deadline=time.monotonic() + 20).text == sp.TEXT_BUSY
    assert len(fake.calls) == 1


def test_lookup_ignores_shared_cooldown(monkeypatch):
    monkeypatch.setattr(sp, "_yahoo_blocked_until", time.time() + 600)
    lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)))}, funds={"TSLA": good_fund("TSLA")})
    assert us.handle_symbol("TSLA", deadline=time.monotonic() + 20).flex is not None


def test_unfinished_bar_and_dst():
    day = date(2026, 3, 9)  # 夏令時間開始後的週一
    cal = weekdays(day, 260)
    bars = dict(zip(cal, rising(0.1)))
    before = datetime(2026, 3, 9, 16, 30, tzinfo=NY).timestamp()
    after = datetime(2026, 3, 9, 17, 30, tzinfo=NY).timestamp()  # UTC 21:30：用固定 −5 小時會算成 16:30
    assert us.yahoo_trend(bars, before, ())[1] == cal[-2]
    assert us.yahoo_trend(bars, after, ())[1] == day
    stamp = datetime(2026, 3, 9, 9, 30, tzinfo=NY).timestamp()
    status, info = us.parse_us_chart(chart_payload("NVDA", {day: 100.0}), "NVDA")
    assert status == "ok" and list(info["bars"]) == [day] and datetime.fromtimestamp(stamp, NY).date() == day


def test_yahoo_trend_rules():
    bars = bars_of(rising(0.1))
    status, data_date, trend, gray = us.yahoo_trend(bars, NOW, tuple(CAL))
    assert (status, data_date, gray) == ("ok", D, "") and trend.p60 == 24
    holes = {d: v for i, (d, v) in enumerate(bars.items()) if i % 40 != 7}  # 年線窗缺 6 天、季線窗缺 1～2 天
    assert us.yahoo_trend(holes, NOW, tuple(CAL))[2] is None  # 日曆涵蓋資料日：檢查缺日
    assert us.yahoo_trend(holes, NOW, ())[2] is not None  # 沒有日曆：只看根數
    jumped = dict(bars)
    jumped[CAL[-30]] = bars[CAL[-30]] * 1.5
    assert us.yahoo_trend(jumped, NOW, tuple(CAL))[2:] == (None, "jump")
    old = bars_of(rising(0.1), weekdays(D - timedelta(days=8), 260))
    assert us.yahoo_trend(old, NOW, ())[0] == "no_trade"


def test_no_trade_and_name_fallback(monkeypatch):
    old_bars = bars_of(rising(0.1), weekdays(D - timedelta(days=8), 260))
    lookup_yahoo(monkeypatch, charts={
        "OLDX": chart_payload("OLDX", old_bars),
        "LONG": chart_payload("LONG", bars_of(rising(0.1)), long_name="L" * 61, short_name="Short Name Co", kind="ETF"),
        "BADN": chart_payload("BADN", bars_of(rising(0.1)), long_name="Bad@Name", short_name="Ünicode", kind="ETF"),
    })
    assert us.handle_symbol("OLDX", deadline=time.monotonic() + 20).text == us.TEXT_US_NO_TRADE
    assert _all_texts(us.handle_symbol("LONG", deadline=time.monotonic() + 20).flex)[0] == "股票評估：LONG（Short Name Co）"
    assert _all_texts(us.handle_symbol("BADN", deadline=time.monotonic() + 20).flex)[0] == "股票評估：BADN"


def test_brk_b_uses_yahoo_dash_symbol(monkeypatch):
    fake = lookup_yahoo(monkeypatch, charts={"BRK-B": chart_payload("BRK-B", bars_of(rising(0.1)), kind="ETF")})
    reply = us.handle_symbol("BRK.B", deadline=time.monotonic() + 20)
    assert reply.flex is not None and fake.calls[0][1] == "/v8/finance/chart/BRK-B"


def test_card_rejected_returns_fixed_text(monkeypatch):
    lookup_yahoo(monkeypatch, charts={"SOXL": chart_payload("SOXL", bars_of(rising(0.1)), kind="ETF")})
    monkeypatch.setattr(stock_card_check, "card_ok", lambda card, alt_text=None: False)
    assert us.handle_symbol("SOXL", deadline=time.monotonic() + 20).text == sp.TEXT_EVAL_CARD_FAILED
    assert us.status_summary()["cards_rejected"] == 1


def test_handle_symbol_rejects_bad_symbols():
    assert us.handle_symbol(us.REJECTED).text == us.TEXT_US_NOT_FOUND
    assert us.handle_symbol("nvda").text == us.TEXT_US_NOT_FOUND


# ── 7. 推薦卡組、/股票 美股、/股票 成績、狀態、reset ─────────────────────────────────────
def tw_reply() -> sp.Reply:
    bubble = sp._bubble("股票推薦：第 1 檔（共 1 檔）", "大盤：中性", [sp._text("✅ 台積電 2330")], [sp._text(sp.TEXT_DISCLAIMER)])
    return sp.Reply(flex=sp.carousel([bubble]), alt_text="股票推薦：只供參考｜台積電｜資料到 10/22")


def test_extend_appends_us_and_perf_cards_without_mutating(small_pool, yahoo, fake_perf):
    make_db()
    fake_perf.result = {"x": 1}
    full_round()
    original = tw_reply()
    before = json.dumps(original.flex)
    extended = us.extend_picks_reply(original)
    assert json.dumps(original.flex) == before  # 傳進來的台股卡組不改
    titles = [c["header"]["contents"][0]["text"] for c in extended.flex["contents"]]
    assert titles == ["股票推薦：第 1 檔（共 1 檔）", "股票推薦：美股第 1 檔（共 3 檔）", "股票推薦：美股第 2 檔（共 3 檔）",
                      "股票推薦：美股第 3 檔（共 3 檔）", "股票推薦：推薦成績"]
    assert extended.alt_text == "股票推薦：只供參考｜台積電｜資料到 10/22｜美股：GOOGL、NVDA、MSFT"
    assert stock_card_check.card_ok(extended.flex, extended.alt_text)


def test_extend_keeps_original_alt_when_too_long(small_pool, yahoo, fake_perf):
    make_db()
    fake_perf.result = {"x": 1}
    full_round()
    original = us.replace(tw_reply(), alt_text="股票推薦：只供參考｜" + "台" * 385)
    extended = us.extend_picks_reply(original)
    assert extended.alt_text == original.alt_text and len(extended.flex["contents"]) == 5  # 卡照接、altText 不超過 400


def test_extend_without_snapshot_leaves_the_tw_cards_alone():
    """2026-10-28 整合：還沒有美股快照、也還沒有成績結果 → 台股卡組原樣（不多一張「計算中」）；
    美股的狀態改在「/股票 美股」看。"""
    original = tw_reply()
    assert us.extend_picks_reply(original) is original
    assert us.TEXT_US_COMPUTING in _all_texts(us.us_reply().flex)
    us._set_worker(us.UsWorker(fail_count=3))
    assert us.extend_picks_reply(original) is original
    assert us.TEXT_US_STUCK in _all_texts(us.us_reply().flex)


def test_extend_not_updated_and_no_picks(small_pool, yahoo, monkeypatch):
    make_db()
    full_round()
    snap = us._snapshot
    us._set_snapshot(us.replace(snap, picks=(), missed=(us.GROWTH, 3, 5)))
    no_pick = us.extend_picks_reply(tw_reply())
    assert no_pick.alt_text.endswith("｜美股：這次沒有符合條件的")
    assert "最常差的條件：有在成長（5 檔裡有 3 檔沒過）" in _all_texts(no_pick.flex["contents"][1])
    set_clock(monkeypatch, NOW + 40 * 3600)
    stale = us.extend_picks_reply(tw_reply())
    assert stale.alt_text.endswith("｜美股：資料暫時沒有更新") and us.TEXT_US_NOT_UPDATED in _all_texts(stale.flex)


def test_extend_size_limit_drops_perf_then_everything(small_pool, yahoo, monkeypatch, fake_perf):
    make_db()
    fake_perf.result = {"x": 1}
    full_round()
    with_perf = us.extend_picks_reply(tw_reply())
    full = us._card_bytes(with_perf.flex)
    monkeypatch.setattr(us, "MAX_CARD_BYTES", full - 1)
    no_perf = us.extend_picks_reply(tw_reply())
    assert len(no_perf.flex["contents"]) == 4
    monkeypatch.setattr(us, "MAX_CARD_BYTES", 100)
    original = tw_reply()
    assert us.extend_picks_reply(original) is original


def test_extend_rejected_cards_fall_back_to_tw(small_pool, yahoo, monkeypatch):
    make_db()
    full_round()
    real = stock_card_check.card_ok
    monkeypatch.setattr(stock_card_check, "card_ok",
                        lambda card, alt_text=None: "美股第" not in json.dumps(card, ensure_ascii=False) and real(card, alt_text))
    original = tw_reply()
    assert us.extend_picks_reply(original) is original
    assert us.status_summary()["cards_rejected"] >= 1


def test_extend_ignores_non_carousel_and_errors(monkeypatch):
    bubble_reply = sp.Reply(flex=sp._bubble("股票評估：x", None, [sp._text("x")], [sp._text("y")]), alt_text="股票評估：只供參考｜x")
    assert us.extend_picks_reply(bubble_reply) is bubble_reply
    text_reply = sp.Reply(text="股票推薦：資料準備中，請過幾分鐘再按一次")
    assert us.extend_picks_reply(text_reply) is text_reply
    monkeypatch.setattr(us, "_us_section", lambda now: 1 / 0)
    original = tw_reply()
    assert us.extend_picks_reply(original) is original


def test_with_us_hook_end_to_end(small_pool, yahoo, fake_perf):
    make_db()
    fake_perf.result = {"x": 1}
    full_round()
    extended = sp._with_us(tw_reply())
    assert len(extended.flex["contents"]) == 5


def test_us_reply_and_perf_reply(small_pool, yahoo, fake_perf):
    computing = us.us_reply()
    assert computing.alt_text == "股票推薦：只供參考｜美股：計算中"
    assert len(computing.flex["contents"]) == 2 and stock_card_check.card_ok(computing.flex, computing.alt_text)
    make_db()
    full_round()
    reply = us.us_reply()
    assert reply.alt_text == "股票推薦：只供參考｜美股：GOOGL、NVDA、MSFT｜資料到 10/27（美國時間）"
    assert len(reply.flex["contents"]) == 4 and reply.fallback_text == sp.TEXT_US_CARDS_FAILED
    fake_perf.result = {"x": 1}
    full_round(NOW + 1800)  # 成績結果每輪放進記憶體
    perf = us.perf_reply()
    assert perf.alt_text == "股票推薦：只供參考｜推薦成績｜計算中"
    assert "台股推薦後 5 個交易日：平均漲 1.8%，0050 同期漲 0.9%" in _all_texts(perf.flex)
    assert sp._us_command("美股", None).alt_text.startswith("股票推薦：只供參考｜美股：")
    assert sp._us_command("成績", None).flex is not None


def test_status_summary_fields(small_pool, yahoo):
    assert us.status_summary()["status"] == "no_snapshot"
    make_db()
    full_round()
    info = us.status_summary()
    assert info["status"] == "ok" and info["data_date"] == "2026-10-27" and info["seen_date"] == "2026-10-27"
    assert info["covered"] == 5 and info["fail_count"] == 0 and info["fund_count_today"] == 5
    assert info["perf"]["ledger_rows"] == 0 and info["perf_date"] is None and info["cards_rejected"] == 0
    assert info["hours_since_check"] == 0.0 and info["backoff_minutes"] is None and info["waiting"] is None
    assert us.status_summary(NOW + 40 * 3600)["status"] == "not_updated"


def test_status_summary_reads_files_only(small_pool, yahoo, monkeypatch):
    make_db()
    full_round()
    us.reset_for_tests()  # 像每日維護另開的程序：還沒載入狀態
    monkeypatch.setattr(stock_db_ro, "rows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    before = sorted((p.name, p.stat().st_mtime_ns) for p in sp.STATE_DIR.iterdir())
    info = us.status_summary()
    assert info["status"] == "ok" and info["data_date"] == "2026-10-27"
    assert sorted((p.name, p.stat().st_mtime_ns) for p in sp.STATE_DIR.iterdir()) == before
    assert us._loaded is False


def test_status_disabled_by_env_file(monkeypatch):
    sp.ENV_FILE.write_text("LINE_BOT_STOCK_PICKS_US=0\n")
    assert us.enabled_now() is False and us.status_summary()["status"] == "disabled"
    monkeypatch.setenv(us.FLAG_ENV, "1")  # 環境變數優先（和 main 的 load_dotenv(override=False) 一致）
    assert us.enabled_now() is True


def test_reset_for_tests_rereads_flag(monkeypatch):
    monkeypatch.setenv(us.FLAG_ENV, "off")
    us.reset_for_tests()
    assert us.ENABLED is False
    monkeypatch.delenv(us.FLAG_ENV)
    us.reset_for_tests()
    assert us.ENABLED is True and us._snapshot is None and not us._lookup_cache


# ── 契約：借用的名稱、import 順序、和成績模組共用的檔名 ─────────────────────────────────────
BORROWED = (
    "_get_json", "FetchError", "RoundFailed", "Reply", "_text", "_button", "_separator", "_bubble", "carousel", "MET",
    "CAUTION", "BELOW_YEAR", "NO_DATA", "_EMOJI", "_COLOR", "Trend", "trend_of", "_permille", "_tenths", "_fmt_tenths",
    "_round_half_up", "_fmt_date", "market_of", "Market", "HOT", "COLD", "NEUTRAL", "_market_gap_words",
    "_read_json_file", "_write_json_file", "_state_file_lock", "_append_history", "_yahoo_blocked", "_block_yahoo",
    "_ai_flag", "_env_file_value", "_v_num", "_v_int", "_v_str", "_v_date", "_require", "_at_most", "_CLOCK_SKEW_S",
    "BACKOFF_S", "BLOCKED_BACKOFF_S", "STATE_CORRUPT_BACKOFF_S", "_budget_left", "_valid_retry", "_NAME_TAILS",
    "PREFIX", "EVAL_PREFIX", "TEXT_DISCLAIMER", "TEXT_LEAD_NOTE", "TEXT_NO_TIME", "TEXT_BUSY", "TEXT_LOOKUP_FAILED",
    "TEXT_EVAL_CARD_FAILED", "TEXT_US", "TEXT_US_CARDS_FAILED", "AI_ENABLED", "STATE_DIR", "_BACKGROUND_ENABLED",
    "YEAR_BARS", "QUARTER_BARS", "NEAR_PERMILLE",
)


def test_borrowed_names_exist_and_signatures_match():
    for name in BORROWED:
        assert hasattr(sp, name), name
    for name in ("_text", "_button", "_separator", "_bubble", "carousel", "trend_of", "market_of", "_append_history",
                 "_state_file_lock", "_budget_left", "_yahoo_blocked", "_block_yahoo", "_ai_flag", "_env_file_value"):
        assert callable(getattr(sp, name)), name
    # conftest 已經把 _get_json 換成擋網的 (*args, **kwargs)：參數名從原始碼讀
    import ast  # noqa: PLC0415

    tree = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
    get_json = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_get_json")
    names = {a.arg for a in get_json.args.args + get_json.args.kwonlyargs}
    assert {"host", "path", "params", "max_bytes", "timeout", "total_s", "stop"} <= names
    assert {"name", "record"} <= set(inspect.signature(sp._append_history).parameters)
    assert set(inspect.signature(sp._v_num).parameters) >= {"value", "lo", "hi", "allow_none"}
    assert {"not_yet", "blocked", "retry_s"} <= set(inspect.signature(sp.RoundFailed).parameters)
    assert {"text", "flex", "alt_text", "fallback_text"} <= set(inspect.signature(sp.Reply).parameters)
    assert set(us._question_tails()) <= set(sp._NAME_TAILS) and "嗎" in us._question_tails()


def test_hook_names_exist():
    for name in ("find_us_symbol", "REJECTED", "RESERVED", "TEXT_RESERVED", "TEXT_US_NOT_FOUND", "handle_symbol",
                 "extend_picks_reply", "us_reply", "perf_reply", "start_background", "signal_stop", "join_background",
                 "reset_for_tests", "parse_us_token", "enabled_now", "status_summary", "RULE", "FLAG_ENV", "ENABLED",
                 "SNAPSHOT_NAME", "WORKER_NAME", "HISTORY_NAME", "POOL_N", "MAX_PICKS", "MIN_SCORE", "TEXT_PHASE3_OFF"):
        assert hasattr(us, name), name
    assert (us.POOL_N, us.MAX_PICKS, us.MIN_SCORE) == (50, 3, 4)
    assert (us.SNAPSHOT_NAME, us.WORKER_NAME, us.HISTORY_NAME) == (
        "stock_picks_us.json", "stock_picks_us_worker.json", "stock_picks_us_history.jsonl")
    assert set(inspect.signature(us._tick).parameters) == {"stop", "now"}
    assert "deadline" in inspect.signature(us.handle_symbol).parameters


def test_import_without_stock_picks_first():
    """先 import stock_picks_us（不先 import stock_picks）也能用。另開一個程序，不連網、不起背景。"""
    code = ("import sys; import stock_picks_us as us; assert 'stock_picks' in sys.modules; "
            "assert us.find_us_symbol('nvda?') == 'NVDA'; assert us.parse_us_token('S&P') == us.REJECTED; print('ok')")
    env = {**os.environ, "BOT_MUTED": "true", "CLAUDE_CODE_CMD": "/nonexistent/claude"}
    done = subprocess.run([sys.executable, "-c", code], cwd=str(Path(us.__file__).parent), env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-2000:]


def test_history_name_matches_perf_module(monkeypatch):
    """單一來源：和 stock_picks_perf.US_HISTORY_NAME 一樣（成績模組還沒有的時候跳過）。"""
    monkeypatch.delitem(sys.modules, "stock_picks_perf", raising=False)
    try:
        perf = importlib.import_module("stock_picks_perf")
    except Exception as exc:  # 還沒寫好（PERF 子代理同時在做）
        pytest.skip(f"stock_picks_perf not importable yet: {type(exc).__name__}")
    assert perf.US_HISTORY_NAME == us.HISTORY_NAME


# ── 審查後的修正（P1／P2／P4，2026-10-28） ─────────────────────────────────────────────────
COST_Q = [date(2025, 5, 11), date(2025, 8, 31), date(2025, 11, 23), date(2026, 2, 15), date(2026, 5, 10), date(2026, 8, 30)]


def test_costco_16_week_quarter_passes_continuity():
    """Costco 的會計年度是 12／12／12／16 週：16 週那一季 112 天，不能被當成缺季（P1 #1）。"""
    eps = _rows([4.3, 5.87, 4.5, 4.58, 4.3, 6.75], COST_Q)
    ni = _rows([v * 443.0 for v in (4.3, 5.87, 4.5, 4.58, 4.3, 6.75)], COST_Q)
    rev = _rows([62.0, 86.0, 66.0, 69.0, 63.0, 94.0], COST_Q)
    assert us.profit_of(ni, D) == us.Profit(COST_Q[-1], 0, 0)
    assert us.pe_of(eps, ni, 900.0, D).kind == us.PE_OK
    assert us.growth_of(rev, D).yoy == 93  # 2026-08-30 對 2025-08-31（364 天）
    missing = tuple(r for r in ni if r[0] != date(2026, 5, 10))  # 少一季：差 196 天
    assert us.profit_of(missing, D) is None


def _empty_fund() -> dict:
    return {"timeseries": {"result": [], "error": None}}


def _without(payload: dict, type_name: str) -> dict:
    out = json.loads(json.dumps(payload))
    out["timeseries"]["result"] = [r for r in out["timeseries"]["result"] if type_name not in r]
    return out


def test_empty_or_partial_fund_response_keeps_cached_series(small_pool, yahoo):
    """200 但沒有能用的列＝沒抓到：舊的照留、時間不動；只缺淨利 → 淨利沿用舊的（P1 #2）。"""
    make_db()
    full_round()
    old = dict(us._worker.fund)
    stale = {s: us.replace(e, at=NOW - 4 * 86400) for s, e in old.items()}
    stale_worker = us.replace(us._worker, fund=stale)  # 檔案也寫成舊的（不然存檔時會併回比較新的那份）
    sp._write_json_file(sp.STATE_DIR / us.WORKER_NAME, us.worker_to_json(stale_worker))
    us._set_worker(stale_worker)
    yahoo.funds["NVDA"] = _empty_fund()
    yahoo.funds["MSFT"] = _without(good_fund("MSFT"), "quarterlyNetIncome")
    us._set_snapshot(None)  # 讓這一輪重算（會去抓到期的財報）
    later = NOW + 1800
    full_round(later)
    fund = us._worker.fund
    assert fund["NVDA"] == stale["NVDA"] and us._fund_due(fund["NVDA"], later)  # 沒蓋掉、下一輪還會再抓
    assert fund["MSFT"].ni == old["MSFT"].ni and fund["MSFT"].at == later and fund["MSFT"].complete
    assert read_worker_file()["fund"]["NVDA"]["at"] == NOW - 4 * 86400
    assert us._worker.last_fail == ""


def test_first_partial_fund_is_retried_sooner():
    partial = us.FundEntry(at=NOW, rev=_rows([1, 2, 3, 4, 5]), eps=_rows([1, 1, 1, 1, 1]))
    assert not partial.complete
    assert not us._fund_due(partial, NOW + 3600)
    assert us._fund_due(partial, NOW + us.FUND_PARTIAL_RETRY_S)
    full = us.replace(partial, ni=_rows([1, 1, 1, 1, 1]))
    assert not us._fund_due(full, NOW + us.FUND_PARTIAL_RETRY_S) and us._fund_due(full, NOW + us.FUND_REFRESH_S)
    assert us._entry_from_parsed({"rev": (), "ni": (), "eps": ()}, NOW, full) is None


def test_lookup_fund_fallback_rules(monkeypatch):
    """即時抓不到財報（200 但空的、失敗）：背景快取 30 天內的才用，結果只快取 10 分鐘；再舊就當抓不到（P1 #5）。"""
    us._ensure_loaded()
    good = us._entry_from_parsed(us.parse_fundamentals(good_fund("TSLA")), NOW - 10 * 86400)
    chart = chart_payload("TSLA", bars_of(rising(0.2)))

    def ask(fund_entry, fund_payload):
        with us._lookup_lock:
            us._lookup_times.clear()
            us._lookup_cache.clear()
        us._set_worker(us.replace(us._worker, fund={} if fund_entry is None else {"TSLA": fund_entry}))
        lookup_yahoo(monkeypatch, charts={"TSLA": chart}, funds={"TSLA": fund_payload})
        return _all_texts(us.handle_symbol("TSLA", deadline=time.monotonic() + 20).flex)

    assert "✅ 4 條都符合" in ask(good, _empty_fund())
    assert us._lookup_cache["TSLA"][0] == pytest.approx(NOW + us.LOOKUP_FAIL_TTL_S)
    assert "✅ 4 條都符合" in ask(good, sp.FetchError("status"))
    assert us._lookup_cache["TSLA"][0] == pytest.approx(NOW + us.LOOKUP_FAIL_TTL_S)
    assert "⚠️ 走勢可以，財報暫時抓不到" in ask(us.replace(good, at=NOW - 40 * 86400), _empty_fund())
    assert "⚠️ 走勢可以，財報暫時抓不到" in ask(None, _empty_fund())
    # 只缺淨利：用背景快取（30 天內）的淨利補；補過的只快取 10 分鐘
    assert "✅ 4 條都符合" in ask(good, _without(good_fund("TSLA"), "quarterlyNetIncome"))
    assert us._lookup_cache["TSLA"][0] == pytest.approx(NOW + us.LOOKUP_FAIL_TTL_S)
    assert "⬜ 有在賺錢：獲利資料不足" in ask(None, _without(good_fund("TSLA"), "quarterlyNetIncome"))


def test_lookup_requests_fit_the_budget(monkeypatch):
    """每個請求傳 (連線, 讀取)，連線＋兩次讀取 ≤ total_s ≤ 這次查詢剩下的預算（P2 #3）。"""
    fake = lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)))},
                        funds={"TSLA": good_fund("TSLA")})
    assert us.handle_symbol("TSLA", deadline=time.monotonic() + 9.0).flex is not None  # 預算 min(6, 9 − 3)
    assert len(fake.calls) == 2
    for _, _, timeout, total_s in fake.calls:
        connect, read = timeout
        assert connect <= us.LOOKUP_CONNECT_S and read <= us.LOOKUP_FETCH_S
        assert connect + 2 * read <= total_s + 1e-6 and total_s <= us.LOOKUP_TOTAL_S + 1e-6


def _extreme_fund(ysym: str) -> dict:
    return ts_payload(ysym, {
        "quarterlyTotalRevenue": list(zip(QUARTERS, [1e-3, 1.0, 1.0, 1.0, 1e5])),  # 年增約 1 億倍（資料錯）
        "quarterlyNetIncome": list(zip(QUARTERS, [10.0, 11.0, 12.0, 13.0, 14.0])),
        "quarterlyDilutedEPS": list(zip(QUARTERS, [1.0, 1.1, 1.2, 1.3, 1.4])),
    })


def test_one_extreme_value_only_affects_that_stock(small_pool, yahoo):
    """一檔的年增超出快照能存的範圍 → 只有那一檔「營收資料不足」，整份快照照寫（P1 #3）。"""
    yahoo.funds["AAPL"] = _extreme_fund("AAPL")
    make_db()
    full_round()
    snap = us._snapshot
    assert snap is not None and us._worker.last_fail == ""
    assert snap.facts_of("AAPL").growth is None and snap.facts_of("NVDA").growth is not None


def test_snapshot_write_read_round_trip_with_edge_values(small_pool, monkeypatch):
    """寫得進去的一定讀得回來：名稱清一次不定型、收盤低於讀回下限、年增超出範圍（P2 #2）。"""
    for raw in ("WALT DISNEY CO... /THE", "ABC INC... -", "X... &"):
        once = us.clean_us_name(raw, pool=True)
        assert once and us.clean_us_name(once) == once and us.clean_us_name(once, pool=True) == once
    assert us.clean_us_name("WALT DISNEY CO... /THE", pool=True) == "WALT DISNEY CO"
    funds = {s: good_fund(s) for s in POOL}
    funds["AAPL"] = _extreme_fund("AAPL")
    monkeypatch.setattr(sp, "_get_json", FakeYahoo(funds=funds))
    make_db(pool={**POOL, "AMZN": ("WALT DISNEY CO... /THE", 15.0)}, extra={"TINY": [1e-7] * 3})
    full_round()
    snap = us._snapshot
    assert snap is not None and us._worker.last_fail == ""
    assert snap.facts_of("AMZN").name == "WALT DISNEY CO" and "TINY" not in snap.latest_closes
    disk = us.snapshot_from_json(json.loads((sp.STATE_DIR / us.SNAPSHOT_NAME).read_text()), NOW)
    assert disk == snap


def test_card_rejections_are_persisted_for_other_processes(small_pool, yahoo, monkeypatch):
    """回覆路徑擋下的卡片數，背景每一輪併進工作狀態檔；每日維護另開的程序讀檔看得到（P2 #1）。"""
    make_db()
    full_round()
    monkeypatch.setattr(stock_card_check, "card_ok", lambda card, alt_text=None: False)
    assert us.handle_symbol("NVDA", deadline=time.monotonic() + 20).text == sp.TEXT_EVAL_CARD_FAILED
    assert us.status_summary()["cards_rejected"] == 1
    full_round(NOW + 1800)
    worker = read_worker_file()
    assert worker["cards_rejected"] == 1 and worker["cards_rejected_at"] == NOW
    full_round(NOW + 3600)  # 不會重複加
    assert read_worker_file()["cards_rejected"] == 1
    us.reset_for_tests()  # 像每日維護另開的程序
    info = us.status_summary(NOW + 7200)
    assert info["cards_rejected"] == 1 and info["cards_rejected_hours_ago"] == 2.0 and us._loaded is False


def test_fund_insufficient_over_gate_keeps_old_snapshot(small_pool, yahoo):
    """抓到了但算不出成長的超過兩成 → 整輪失敗、保留舊快照、財報快取留著；只有一檔時照算（P4 #1）。"""
    make_db()
    full_round()
    first = us._snapshot
    nxt = D + timedelta(days=1)
    for symbol, value in {**{k: v[-1] + 0.1 for k, v in SERIES.items()}, "SPY": 700.0, "VOO": 700.0, "QQQ": 700.0}.items():
        add_closes(symbol, [(nxt, value, 1)])
    good = dict(us._worker.fund)
    broken = {**good, **{s: us.replace(good[s], rev=good[s].rev[1:]) for s in ("NVDA", "MSFT")}}  # 少了去年同季
    us._set_worker(us.replace(us._worker, fund=broken))
    later = datetime(2026, 10, 29, 15, 0, tzinfo=TPE).timestamp()
    full_round(later)
    assert us._worker.last_fail == "too_many_gray" and us._snapshot == first
    assert set(read_worker_file()["fund"]) == set(good)
    us._set_worker(us.replace(us._worker, fund={**broken, "MSFT": good["MSFT"]}))
    full_round(later + us.POLL_S)
    snap = us._snapshot
    assert snap.d == nxt and snap.facts_of("NVDA").growth is None and snap.facts_of("MSFT").growth is not None


def test_history_record_matches_snapshot_and_perf_reader(small_pool, yahoo, monkeypatch):
    """寫入端的契約：紀錄的 close＝快照裡 D 那天的收盤（＝DB 的 Nasdaq 收盤），成績模組讀得到 code／close（P4 #2）。"""
    make_db()
    full_round()
    snap = us._snapshot
    line = history_lines()[-1]
    assert line["t"] == D.isoformat() and [p["code"] for p in line["picks"]] == list(snap.picks)
    for pick in line["picks"]:
        facts = snap.facts_of(pick["code"])
        assert pick["close"] == facts.trend.close == SERIES[pick["code"]][-1]
        assert pick["name"] == facts.name and pick["p60"] == facts.trend.p60
    perf = _real_perf(monkeypatch)
    if perf is None:
        pytest.skip("stock_picks_perf not importable")
    records = perf.parse_records([line], market=perf.US, default_rule=perf.US_RULE, today="2026-10-28")
    rec = records[D.isoformat()]
    assert rec.rule == "us_v1" and [(p.code, p.close) for p in rec.picks] == [(p["code"], p["close"]) for p in line["picks"]]


def test_not_updated_snapshot_uses_live_lookup(small_pool, yahoo, monkeypatch):
    """快照「沒更新」時，池裡的股票也改走即時查，不出舊的收盤（PLAN §5.2 step 2；P4 #3）。"""
    make_db()
    full_round()
    set_clock(monkeypatch, NOW + 40 * 3600)
    assert us._not_updated(us._snapshot, us._worker, NOW + 40 * 3600)
    fake = lookup_yahoo(monkeypatch, charts={"NVDA": chart_payload("NVDA", bars_of(rising(0.1)))},
                        funds={"NVDA": good_fund("NVDA")})
    texts = _all_texts(us.handle_symbol("NVDA", deadline=time.monotonic() + 20).flex)
    assert fake.calls and any(t.startswith("依據：Yahoo 收盤") for t in texts)


def test_reply_path_never_touches_sqlite(small_pool, yahoo, monkeypatch, fake_perf):
    """回覆路徑完全不碰 sqlite（v5 #10；P4 #4）：池內、Yahoo 路徑、推薦卡組、/股票 美股、/股票 成績都照常。"""
    make_db(extra={"TSLA": rising(0.2)})
    fake_perf.result = {"x": 1}
    full_round()
    us.reset_for_tests()  # 連第一次讀狀態檔也走一次
    touched = []

    def record(*args, **kwargs):
        touched.append(args[:1])
        raise AssertionError("reply path touched sqlite")

    lookup_yahoo(monkeypatch, charts={"TSLA": chart_payload("TSLA", bars_of(rising(0.2)))}, funds={"TSLA": good_fund("TSLA")})
    with monkeypatch.context() as patch:  # 在這個區塊裡換掉；離開就換回（conftest 收尾還要用 sqlite）
        patch.setattr(stock_db_ro, "rows", record)
        patch.setattr(sqlite3, "connect", record)
        assert us.handle_symbol("NVDA", deadline=time.monotonic() + 20).flex is not None  # 池內
        tsla = us.handle_symbol("TSLA", deadline=time.monotonic() + 20)  # Yahoo 路徑（核對用快照帶的收盤）
        assert "依據：Yahoo 收盤、Yahoo 財報｜收盤和 Nasdaq 資料核對過｜資料到 10/27（美國時間）" in _all_texts(tsla.flex)
        assert len(us.extend_picks_reply(tw_reply()).flex["contents"]) == 5
        assert us.us_reply().flex is not None and us.perf_reply().flex is not None
        assert us.status_summary()["status"] == "ok"
    assert touched == []


def test_perf_card_dropped_when_rejected(small_pool, yahoo, monkeypatch, fake_perf):
    """只有成績卡沒過自我檢查 → 美股卡照接、成績卡拿掉、記一次被擋（P4 #6）。"""
    make_db()
    fake_perf.result = {"x": 1}
    full_round()
    real = stock_card_check.card_ok
    monkeypatch.setattr(stock_card_check, "card_ok",
                        lambda card, alt_text=None: "推薦成績" not in json.dumps(card, ensure_ascii=False) and real(card, alt_text))
    extended = us.extend_picks_reply(tw_reply())
    titles = [c["header"]["contents"][0]["text"] for c in extended.flex["contents"]]
    assert titles == ["股票推薦：第 1 檔（共 1 檔）", "股票推薦：美股第 1 檔（共 3 檔）", "股票推薦：美股第 2 檔（共 3 檔）",
                      "股票推薦：美股第 3 檔（共 3 檔）"]
    assert us.status_summary()["cards_rejected"] >= 1
