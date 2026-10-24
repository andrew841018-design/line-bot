"""market_pipeline_us（/盤勢 美股 collect 任務，2026-10-17）的測試。

用真的 market_store.Store（暫存目錄）、真的 market_calendar 日曆、真的 market_sources_us 解析器；網路換成
假的 http（照 fixtures_raw 的真實格式合成小回應），帳本換成假的（另有一個用真的 StoreLedger）。時間一律注入
（ctx.now、假的 monotonic clock）。CFTC／FINRA 的檔案格式由 test_market_sources_us 測，這裡在來源函式那層
換成假的（照樣檢查帳本有傳進去）。不連網、不碰正式 state。
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import openpyxl
import pytest

import market_calc
import market_calendar
import market_http
import market_paths
import market_pipeline_us as pipe
import market_sources_us as src
import market_store
import market_tasks

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
TASKS = {task.name: task for task in pipe.TASKS}

D = date(2026, 10, 16)  # 週五
NOW = datetime(2026, 10, 16, 17, 0, tzinfo=ET).timestamp()  # 美東週五收盤後


def at(year, month, day, hour=0, minute=0, tz=ET) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


def sessions(end: date, n: int) -> list[date]:
    """≤end 的最近 n 個平日（扣 NYSE 表的休市日）；假資料用，不靠被測的日曆。"""
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5 and day not in market_calendar.NYSE_HOLIDAYS:
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


# ── 假的帳本、時鐘、http ─────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.wall = NOW

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.wall += seconds


class FakeLedger:
    """market_http.Ledger：記下預扣；可設某主機第 N 次起說額度不夠；403／429 之後冷卻。"""

    def __init__(self) -> None:
        self.reserved: list[str] = []
        self.cooling: set[str] = set()
        self.cap: dict[str, int] = {}

    def reserve(self, host: str) -> None:
        if host in self.cooling:
            raise market_http.FetchError("cooldown")
        if host in self.cap and self.reserved.count(host) >= self.cap[host]:
            raise market_http.FetchError("budget")
        self.reserved.append(host)

    def cooldown(self, host: str, seconds: float) -> None:
        self.cooling.add(host)


def nasdaq(data: Any) -> dict:
    return {"data": data, "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


def _num_text(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


class FakeHttp:
    """假的 market_http：依網址回 Nasdaq／CBOE／SSGA／MoneyDJ／財政部／Fed 的小回應。

    每次都檢查帳本就是 ctx.ledger、先預扣（和真的 market_http 一樣），記下每次請求。"""

    FetchError = market_http.FetchError

    def __init__(self, ledger: Any, clock: FakeClock) -> None:
        self.ledger = ledger
        self.clock = clock
        self.calls: list[tuple[str, str, dict]] = []  # (種類, 代號或鍵, 查詢參數)
        self.prices: dict[str, dict[date, float]] = {}
        self.hist_hook: dict[str, Callable[[date, date], dict[date, float]]] = {}
        self.hist_raw: dict[str, dict] = {}  # 代號 → 整份 historical 回應（空殼、廠商錯誤；2026-10-23）
        self.fail: dict[str, str] = {}  # 鍵（"hist:SPY"、"hist:*"、"cboe:QQQ"…）→ FetchError kind
        self.chains: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.market_info: Optional[dict] = None
        self.splits: list[dict] = []
        self.ndx: Optional[dict] = None
        self.inst: dict[str, dict] = {}
        self.fin: dict[str, dict] = {}  # 代號 → 整份 financials 回應（沒給就用 NVDA 那份）
        self.summary: dict[str, dict] = {}  # 代號 → 整份 summary 回應
        self.earnings: dict[str, dict] = {}
        self.econ: dict[str, list[dict]] = {}
        self.wednesday_mba = True  # 真實的每個週三都有 MBA 房貸申請（經濟日曆的日期對應要靠它驗）
        self.treasury: dict[str, str] = {}
        self.ssga: Optional[bytes] = None
        self.moneydj: dict[str, str] = {}
        self.fomc: Optional[str] = None
        self.seconds_per_call = 0.0

    # 測試看請求
    def requested(self, kind: str) -> list[str]:
        return [key for k, key, _ in self.calls if k == kind]

    def query(self, kind: str, key: str) -> list[dict]:
        return [q for k, name, q in self.calls if k == kind and name == key]

    def _check_fail(self, host: str, *keys: str) -> None:
        for key in keys:
            kind = self.fail.get(key)
            if kind is None:
                continue
            if kind in ("blocked", "rate_limited"):
                self.ledger.cooldown(host, 7200.0)
            raise market_http.FetchError(kind)

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        assert ledger is self.ledger, "every fetch must get ctx.ledger"
        parts = urlsplit(url)
        host = parts.hostname or ""
        assert parts.scheme == "https" and host in market_http.ALLOWED_HOSTS
        ledger.reserve(host)
        self.clock.sleep(self.seconds_per_call)
        query = {key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
        body = self._route(host, parts.path, query)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        return market_http.Response(200, body, {"content-type": "application/json"}, url)

    def _route(self, host: str, path: str, query: dict) -> Any:
        if host == "api.nasdaq.com":
            parts = path.split("/")
            if path.endswith("/historical"):
                symbol = parts[3]
                self.calls.append(("hist", symbol, query))
                self._check_fail(host, f"hist:{symbol}", "hist:*")
                if symbol in self.hist_raw:
                    return self.hist_raw[symbol]
                return self._historical(symbol, query)
            if path == "/api/market-info":
                self.calls.append(("market-info", "", query))
                self._check_fail(host, "market-info")
                return self.market_info
            if path == "/api/calendar/splits":
                self.calls.append(("splits", "", query))
                return nasdaq({"asOf": "Fri, Oct 16, 2026", "headers": {}, "rows": self.splits})
            if path == "/api/quote/list-type/nasdaq100":
                self.calls.append(("ndx", "", query))
                return self.ndx
            if path.endswith("/institutional-holdings"):
                self.calls.append(("13f", parts[3], query))
                self._check_fail(host, f"13f:{parts[3]}")
                return nasdaq(self.inst.get(parts[3], INST))
            if path.endswith("/financials"):
                self.calls.append(("fin", parts[3], query))
                return self.fin.get(parts[3]) or financials(NET_INCOME)
            if path.endswith("/summary"):
                self.calls.append(("summary", parts[3], query))
                self._check_fail(host, f"summary:{parts[3]}")
                return self.summary.get(parts[3]) or summary(parts[3], "1,000,000,000,000")
            if path.endswith("/earnings-date"):
                self.calls.append(("earnings", parts[3], query))
                return nasdaq(self.earnings.get(parts[3]) or earnings_payload(parts[3]))
            if path == "/api/calendar/economicevents":
                self.calls.append(("econ", query["date"], query))
                # 照 Nasdaq 的真實行為：date=X 回 X 前一天（美東）的事件（2026-10-18 實測）；self.econ 以事件日為鍵
                day = (date.fromisoformat(query["date"]) - timedelta(days=1)).isoformat()
                rows = self.econ.get(day)
                if rows is None and self.wednesday_mba and date.fromisoformat(day).weekday() == 2:
                    rows = [{"gmt": "07:00", "country": "United States", "eventName": "MBA Mortgage Applications"}]
                return nasdaq({"asOf": "x", "headers": {}, "rows": rows or []})
        if host == "cdn-api.cboe.com":
            name = path.rsplit("/", 1)[-1].removesuffix(".json")
            if "/options/" in path:
                self.calls.append(("cboe", name, query))
                self._check_fail(host, f"cboe:{name}")
                return self.chains[name]
            self.calls.append(("quote", name.lstrip("_"), query))
            self._check_fail(host, f"quote:{name.lstrip('_')}")
            return self.quotes[name.lstrip("_")]
        if host == "home.treasury.gov":
            ym = query["field_tdr_date_value_month"]
            self.calls.append(("treasury", ym, query))
            return self.treasury.get(ym, TREASURY_HEADER + "\n")
        if host == "www.ssga.com":
            self.calls.append(("ssga", "", query))
            return self.ssga
        if host == "www.moneydj.com":
            self.calls.append(("moneydj", query["etfid"], query))
            return self.moneydj[query["etfid"]]
        if host == "www.federalreserve.gov":
            self.calls.append(("fomc", "", query))
            return self.fomc
        raise AssertionError(f"unexpected request {host}{path}")

    def _historical(self, symbol: str, query: dict) -> dict:
        start, end = date.fromisoformat(query["fromdate"]), date.fromisoformat(query["todate"])
        hook = self.hist_hook.get(symbol)
        series = hook(start, end) if hook else self.prices.get(symbol, {})
        etf = query["assetclass"] == "etf"
        rows = [
            {"date": day.strftime("%m/%d/%Y"), "close": ("" if etf else "$") + _num_text(close), "volume": "1,000",
             "open": "1", "high": "1", "low": "1"}
            for day, close in sorted(series.items(), reverse=True)
            if start <= day <= end or hook is not None
        ]
        table = {"asOf": None, "headers": {"date": "Date", "close": "Close/Last"}, "rows": rows or None}
        return nasdaq({"symbol": symbol, "totalRecords": len(rows), "tradesTable": table})


# ── 各來源的小回應（照 fixtures_raw 的格式合成） ─────────────────────────────

INST = {
    "ownershipSummary": {"SharesOutstandingPCT": {"label": "Institutional Ownership", "value": "79.53%"}},
    "activePositions": {"rows": [
        {"positions": "Increased Positions", "holders": "3,369", "shares": "1"},
        {"positions": "Decreased Positions", "holders": "2,574", "shares": "1"},
        {"positions": "Held Positions", "holders": "348", "shares": "1"},
        {"positions": "Total Institutional Shares", "holders": "6,291", "shares": "1"},
    ]},
    "newSoldOutPositions": {"rows": [
        {"positions": "New Positions", "holders": "287", "shares": "1"},
        {"positions": "Sold Out Positions", "holders": "116", "shares": "1"},
    ]},
    "holdingsTransactions": {"table": {"rows": [
        {"ownerName": "Vanguard Group Inc", "date": "12/31/2025"},
        {"ownerName": "Blackrock, Inc.", "date": "6/30/2026"},
        {"ownerName": "Fmr Llc", "date": "6/30/2026"},
    ]}},
}
QUARTER_HEADERS = {"value1": "Quarterly Ending:", "value2": "7/26/2026", "value3": "4/26/2026", "value4": "1/25/2026",
                   "value5": "10/26/2025"}
NET_INCOME = {"value1": "Net Income Applicable to Common Shareholders", "value2": "$26,422,000",
              "value3": "$18,775,000", "value4": "$22,091,000", "value5": "$19,309,000"}
TREASURY_HEADER = 'Date,"1 Mo","3 Mo","2 Yr","10 Yr","30 Yr"'


def financials(row: dict, headers: Optional[dict] = None) -> dict:
    return nasdaq({"symbol": "X", "incomeStatementTable": {"asOf": None, "headers": headers or QUARTER_HEADERS,
                                                           "rows": [row]}})


def summary(symbol: str, market_cap: Any) -> dict:
    """照 fixtures_raw/nasdaq_summary_*.json 的形狀（裁掉大部分欄位）；BRK.B 的回應代號寫 BRK/B。"""
    data: dict[str, Any] = {
        "Exchange": {"label": "Exchange", "value": "NYSE"},
        "PreviousClose": {"label": "Previous Close", "value": "$335.53"},
        "SpecialDividendDate": {"label": "Special Dividend Date", "value": "N/A"},
    }
    if market_cap is not None:
        data["MarketCap"] = {"label": "Market Cap", "value": market_cap}
    return nasdaq({"symbol": symbol, "summaryData": data, "assetClass": "STOCKS", "additionalData": None,
                   "bidAsk": {"Bid * Size": {"label": "Bid * Size", "value": "N/A"}}})


def earnings_payload(symbol: str) -> dict:
    return {"reportText": f"{symbol} is estimated to report earnings on  11/18/2026. The upcoming earnings date is "
                          "derived from an algorithm.",
            "announcement": f"Earnings announcement* for {symbol}: Nov 18, 2026"}


def market_info(closing="Oct 16, 2026 04:00 PM ET", previous="Oct 15, 2026", following="Oct 19, 2026", business=True):
    return nasdaq({"marketClosingTime": closing, "previousTradeDate": previous, "nextTradeDate": following,
                   "isBusinessDay": business})


def option(code: str, oi: int = 100, iv: float = 0.25) -> dict:
    return {"option": code, "open_interest": float(oi), "volume": 5.0, "iv": iv, "gamma": 0.01, "delta": 0.5}


def cboe_chain(symbol: str, *, timestamp: str, last_trade: str, close: float = 717.23, options=None) -> dict:
    options = options if options is not None else [option(f"{symbol}261120C00700000"),
                                                   option(f"{symbol}261120P00700000")]
    return {"timestamp": timestamp, "symbol": symbol,
            "data": {"symbol": symbol, "current_price": close, "close": close, "volume": 24923927, "iv30": 20.8,
                     "last_trade_time": last_trade, "options": options}}


def cboe_quote(symbol: str, *, timestamp: str, last_trade: str, close: float = 19.99) -> dict:
    return {"timestamp": timestamp, "symbol": f"_{symbol}",
            "data": {"symbol": f"^{symbol}", "close": close, "price_change": 3.53, "price_change_percent": 17.6588,
                     "prev_day_close": close, "last_trade_time": last_trade}}


def cboe_etf_quote(symbol: str, *, timestamp: str, last_trade: str, close: float) -> dict:
    """ETF 的 …/quotes/{SYM}.json（照 investigations/freshness_20261023.md 附錄 A 的 QQQ 形狀；VOO 同一型）。"""
    return {"timestamp": timestamp, "symbol": symbol,
            "data": {"symbol": symbol, "current_price": close, "price_change": -8.42, "close": close,
                     "prev_day_close": close, "volume": 6123456, "last_trade_time": last_trade}}


def cusip(base8: str) -> str:
    values = []
    for index, ch in enumerate(base8):
        value = int(ch) if ch.isdigit() else ord(ch) - 55
        values.append(value * 2 if index % 2 else value)
    total = sum(v // 10 + v % 10 for v in values)
    return base8 + str((10 - total % 10) % 10)


def ssga_xlsx(named: list[tuple[str, str, float]], *, as_of: str = "As of 15-Oct-2026", fillers: int = 455) -> bytes:
    header = ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"]
    rows = [[name, ticker, cusip(f"1{i:07d}"), "-", weight, "-", 1.0, "USD"] for i, (name, ticker, weight) in
            enumerate(named)]
    rows += [[f"EXAMPLE CO {i}", f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}", cusip(f"9{i:07d}"), "-", 0.01, "-", 1.0, "USD"]
             for i in range(fillers)]
    sheet_rows = [["Fund Name:", "SPDR S&P 500 ETF Trust"], ["Ticker Symbol:", "SPY"], ["Holdings:", as_of], [], header]
    sheet_rows += rows + [[], ["Past performance is not a reliable indicator of future performance."]]
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "holdings"
    for row in sheet_rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def moneydj_html(label: str, rows: list[tuple[str, str]], as_of: str = "2026/09/30") -> str:
    body = "".join(
        f'<tr class="even"><td class="col05"><a href="/etf/ea/ETZCW.djhtm?c=1&etfid={code}&back={label}">'
        f"{name}({code})</a></td><td class=\"col06\">{weight}</td><td class=\"col07\">1,000</td></tr>"
        for code, name, weight in ((code, name, weight) for code, (name, weight) in rows)
    )
    return ('<html><body><div class="eTitle">ETF(' + label + ')-全部持股'
            f'<div id="x_sdate3" align="right">資料日期：{as_of}</div></div>'
            '<table class="datalist" id="x_stable3"><thead><tr><th scope="col">個股名稱</th>'
            '<th scope="col">投資比例(%)</th><th scope="col">持有股數 / 口數</th></tr></thead>'
            f"<tbody>{body}</tbody><tfoot></tfoot></table></body></html>")


def fomc_html() -> str:
    items = "".join(
        f'<div class="row fomc-meeting" "><div class="fomc-meeting__month col-xs-5"><strong>{month}</strong></div>'
        f'<div class="fomc-meeting__date col-xs-4">{days}</div><div class="col-xs-12"><a href="/x">PDF</a></div></div>'
        for month, days in (("January", "27-28"), ("March", "17-18*"), ("October", "27-28"), ("December", "8-9*"))
    )
    return ('<html><body><div class="panel panel-default"><div class="panel-heading"><h4><a id="1">2026 FOMC Meetings'
            f"</a></h4></div>{items}</div></body></html>")


# ── 環境 ─────────────────────────────────────────────────────────────────────


@dataclass
class Env:
    paths: market_paths.Paths
    store: market_store.Store
    clock: FakeClock
    ledger: Any
    http: FakeHttp
    extra: dict = field(default_factory=dict)


@pytest.fixture
def env(tmp_path: Path):
    paths = market_paths.Paths.under(tmp_path)
    paths.state_dir.mkdir(mode=0o700)
    clock = FakeClock()
    store = market_store.Store(paths.history_db, now_fn=lambda: clock.wall)
    ledger = FakeLedger()
    yield Env(paths, store, clock, ledger, FakeHttp(ledger, clock))
    store.close()


def ctx_at(env: Env, now: float, *, cal: Any = None) -> market_tasks.Context:
    """一輪 collect 的 Context：日曆照 job 的做法從 store 建（含 SPY 實際成交日與覆寫）。"""
    env.clock.wall = now
    return market_tasks.Context(
        store=env.store, http=env.http, ledger=env.ledger, now=now, paths=env.paths,
        deadline=env.clock() + 600.0, us_cal=cal or market_calendar.us_calendar(env.store),
        tw_cal=market_calendar.Calendar("tw"), clock=env.clock,
    )


def run(env: Env, name: str, now: float = NOW) -> str:
    """經過框架跑一個任務（含 due、例外記帳）；回框架記下的結果種類。"""
    return market_tasks.run_task(ctx_at(env, now), TASKS[name])


def seed(env: Env, symbol: str, closes: dict[date, float], *, source: str = "nasdaq", provisional: bool = False):
    env.store.upsert_closes("us", symbol, [(day, close, 1000) for day, close in closes.items()], source,
                            provisional=provisional)


def flat(days: list[date], price: float) -> dict[date, float]:
    return {day: price for day in days}


def seed_universe(env: Env, symbols: list[str]) -> None:
    rows = [[symbol, f"{symbol} Inc", None, 1e9] for symbol in symbols]
    env.store.put_obs(pipe.OBS_HOLDINGS_NDX, {"source": "Nasdaq", "as_of": "2026-10-16", "rows": rows},
                      data_date="2026-10-16")


HISTORY = sessions(date(2026, 10, 15), 30)  # 9/3 … 10/15


def etfs_ready(env: Env, *, with_d: bool = True, price: float = 100.0) -> None:
    """四檔 ETF 已有到 10/15 的歷史；假的 Nasdaq 有（或沒有）D 那一列。"""
    for symbol in pipe.ETFS:
        seed(env, symbol, flat(HISTORY, price))
        env.http.prices[symbol] = flat(HISTORY + ([D] if with_d else []), price)


def closes(env: Env, symbol: str) -> dict[date, tuple]:
    return {row.date: (row.close, row.source, row.provisional) for row in env.store.closes("us", symbol)}


def obs(env: Env, obs_id: str) -> Optional[dict]:
    return env.store.get_obs(obs_id)


# ── 任務表與登記 ─────────────────────────────────────────────────────────────


def test_tasks_are_registered_with_groups_budgets_and_order():
    names = [task.name for task in pipe.TASKS]
    assert len(names) == len(set(names))
    registered = {task.name for task in market_tasks.registered()}
    assert set(names) <= registered  # import 時登記進框架（job 用 load_modules 載入）
    for task in pipe.TASKS:
        assert task.name.startswith("us.") and task.market == "us"
        assert task.group in market_tasks.GROUPS and 0 < task.budget_s <= 95
    core = [task.name for task in pipe.TASKS if task.group == "core"]
    assert core.index("us.splits") < core.index("us.hist") and core.index("us.holdings.spy") < core.index("us.hist")
    assert TASKS["us.hist.backfill"].group == "backfill"
    assert {TASKS[n].group for n in ("us.cftc", "us.finra", "us.13f", "us.fund", "us.events.fomc")} == {"low"}


# ── D ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("now, expected", [
    (at(2026, 10, 16, 16, 29), date(2026, 10, 15)),  # 收盤＋30 分鐘之前：還是前一天
    (at(2026, 10, 16, 16, 30), date(2026, 10, 16)),
    (at(2026, 10, 17, 0, 0, tz=ZoneInfo("Asia/Taipei")), date(2026, 10, 15)),  # 台北午夜＝美東中午
    (at(2026, 10, 17, 4, 0), date(2026, 10, 16)),  # 美東週六凌晨
    (at(2026, 10, 19, 9, 0), date(2026, 10, 16)),  # 週一開盤前
    (at(2026, 11, 26, 20, 0), date(2026, 11, 25)),  # 感恩節休市
    (at(2026, 11, 27, 13, 29), date(2026, 11, 25)),  # 早收日 13:00＋30 分鐘之前
    (at(2026, 11, 27, 13, 30), date(2026, 11, 27)),
    (at(2026, 11, 2, 16, 30), date(2026, 11, 2)),  # 冬令時間（11/1 換）
    (at(2026, 3, 9, 16, 30), date(2026, 3, 9)),  # 夏令時間（3/8 換）
    (at(2027, 12, 31, 18, 0), date(2027, 12, 31)),  # 表最後一天照常開
    (at(2028, 1, 3, 18, 0), None),  # 表外：未知
])
def test_d_is_the_latest_closed_session(env, now, expected):
    assert pipe.current_d(ctx_at(env, now)) == expected


D_TASKS = ("us.splits", "us.holdings.spy", "us.holdings.ndx", "us.hist", "us.vix", "us.options", "us.ust",
           "us.hist.backfill")  # 靠 D 的任務（經濟日曆、週／月任務看美東日期，不靠 D）


def test_no_d_means_no_task_runs(env):
    ctx = ctx_at(env, at(2028, 1, 3, 18, 0))
    assert pipe.current_d(ctx) is None
    assert not any(TASKS[name].due(ctx) for name in D_TASKS)
    assert all(TASKS[name].run(ctx) is None for name in D_TASKS)
    assert env.http.calls == []


def test_a_missing_source_day_is_not_a_holiday(env):
    """只有 Nasdaq 缺列不算休市：D 照表、不寫覆寫。"""
    etfs_ready(env, with_d=False)
    assert run(env, "us.hist") == "not_yet"
    assert env.store.calendar_overrides("us") == {}
    assert pipe.current_d(ctx_at(env, NOW + 600)) == D


# ── 日線：區間、≤D、帳本 ─────────────────────────────────────────────────────


def test_hist_range_last_valid_minus_three_sessions_and_one_year_bootstrap(env):
    etfs_ready(env)
    # SPY 的 10/15 只有 CBOE 備援列：「最後一筆有效日期」不算它
    env.store.replace_symbol("us", "SPY", [(day, 100.0, 1000) for day in HISTORY[:-1]], "nasdaq")
    seed(env, "SPY", {date(2026, 10, 15): 101.0}, source="cboe", provisional=True)
    seed_universe(env, ["AAA", "NEWX"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    env.http.prices["NEWX"] = flat(sessions(D, 250), 20.0)
    assert run(env, "us.hist") == "ok"
    spy = env.http.query("hist", "SPY")[0]
    assert (spy["fromdate"], spy["todate"], spy["assetclass"]) == ("2026-10-09", "2026-10-16", "etf")  # 10/14 往前 3 個
    aaa = env.http.query("hist", "AAA")[0]
    assert (aaa["fromdate"], aaa["todate"], aaa["assetclass"]) == ("2026-10-12", "2026-10-16", "stocks")
    assert "NEWX" not in env.http.requested("hist")  # 沒有歷史的走 backfill
    assert closes(env, "SPY")[date(2026, 10, 15)] == (100.0, "nasdaq", False)  # 備援列被正式列蓋掉
    assert run(env, "us.hist.backfill") == "ok"
    newx = env.http.query("hist", "NEWX")[0]
    assert (newx["fromdate"], newx["todate"]) == ((D - timedelta(days=365)).isoformat(), "2026-10-16")
    assert max(closes(env, "NEWX")) == D and env.store.last_full_refetch("us", "NEWX") == D
    assert set(env.ledger.reserved) == {"api.nasdaq.com"} and len(env.ledger.reserved) == len(env.http.calls)
    status = obs(env, pipe.OBS_HIST_STATUS)["payload"]
    assert status["D"] == "2026-10-16" and status["total"] == 6 and status["have_d"] == 6


def test_range_start_falls_back_outside_the_calendar():
    cal = market_calendar.Calendar("us")
    assert pipe._range_start(cal, date(2026, 10, 15)) == date(2026, 10, 12)
    assert pipe._range_start(cal, date(2025, 11, 3)) == date(2025, 10, 27)  # 表外、沒有實際成交日：退 7 天


def test_rows_after_d_are_dropped(env):
    bars = [src.Bar(date(2026, 10, 15), 10.0, 5), src.Bar(D, 11.0, None), src.Bar(date(2026, 10, 19), 12.0, 7),
            src.Bar(date(2026, 10, 14), float("nan"), 1)]
    assert pipe._rows_le(bars, D) == [(date(2026, 10, 15), 10.0, 5), (D, 11.0, None)]
    # 週一盤中（D 還是週五）：來源多給的 10/19 盤中列不會進 store
    etfs_ready(env)
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.hist_hook["AAA"] = lambda start, end: flat(HISTORY[-3:] + [D, date(2026, 10, 19)], 50.0)
    monday = at(2026, 10, 19, 11, 0)
    assert pipe.current_d(ctx_at(env, monday)) == D
    run(env, "us.hist", monday)
    assert max(closes(env, "AAA")) == D


def test_every_fetch_uses_the_context_ledger_and_the_real_store_ledger_works(env):
    etfs_ready(env)
    ledger = env.store.ledger(now_fn=lambda: env.clock.wall, sleep_fn=env.clock.sleep)
    env.http.ledger = env.ledger = ledger
    assert run(env, "us.hist") == "ok"
    assert len(env.http.requested("hist")) == 4
    assert env.store.fetch_counts(now=env.clock.wall)["api.nasdaq.com"]["count"] == 4
    assert env.clock.t >= 1003.0  # 間隔 1 秒：帳本自己等


# ── 覆寫守門 ─────────────────────────────────────────────────────────────────


def test_revision_without_split_waits_for_two_consistent_fetches_then_replaces(env):
    etfs_ready(env)
    seed_universe(env, ["BBB"])
    old_row = date(2025, 1, 2)  # 整段重抓的視窗之外：整段替換時要被刪掉
    seed(env, "BBB", {old_row: 40.0, **flat(HISTORY, 50.0)})
    revised = flat(sessions(D, 260), 49.5)  # 來源回溯調整（例：配息）：全部 −1%
    env.http.prices["BBB"] = revised
    assert run(env, "us.hist") == "ok"
    pending = env.store.pending_jump("us", "BBB")
    assert pending["kind"] == "revision" and pending["dates"][0] == "2026-10-12" and pending["disagree"] == 0
    assert closes(env, "BBB")[date(2026, 10, 14)][0] == 50.0 and D not in closes(env, "BBB")  # 不覆寫、也先不寫新列
    assert env.store.quarantine("us") == {}
    assert run(env, "us.hist", NOW + 900) == "ok"  # 下一輪：整段重抓，和上次一樣 → 接受
    full = env.http.query("hist", "BBB")[-1]
    assert full["fromdate"] == (D - timedelta(days=400)).isoformat()
    stored = closes(env, "BBB")
    assert old_row not in stored and stored[D][0] == 49.5 and stored[date(2026, 10, 9)][0] == 49.5
    assert env.store.pending_jump("us", "BBB") is None and env.store.quarantine("us") == {}


def test_split_event_triggers_full_refetch_and_replacement(env):
    etfs_ready(env)
    seed_universe(env, ["CCC"])
    seed(env, "CCC", {date(2025, 1, 2): 9.0, **flat(HISTORY, 10.0)})  # 1:10 合股前的基準
    env.store.upsert_splits([("CCC", D, 1.0, 10.0)], "nasdaq")
    env.http.prices["CCC"] = {**flat(sessions(date(2026, 10, 15), 260), 100.0), D: 101.0}  # Nasdaq 已調整
    assert run(env, "us.hist") == "ok"
    assert [q["fromdate"] for q in env.http.query("hist", "CCC")] == [(D - timedelta(days=400)).isoformat()]
    stored = closes(env, "CCC")
    assert date(2025, 1, 2) not in stored and stored[date(2026, 10, 15)][0] == 100.0 and stored[D][0] == 101.0
    assert env.store.last_full_refetch("us", "CCC") == D and env.store.quarantine("us") == {}
    assert run(env, "us.hist", NOW + 900) == "ok"
    assert len(env.http.query("hist", "CCC")) == 1  # 做過了就不再重抓


def test_unadjusted_split_is_quarantined_and_retried(env):
    etfs_ready(env)
    seed_universe(env, ["CCC"])
    seed(env, "CCC", flat(HISTORY, 10.0))
    env.store.upsert_splits([("CCC", D, 1.0, 10.0)], "nasdaq")
    env.http.prices["CCC"] = {**flat(HISTORY, 10.0), D: 101.0}  # 來源還沒回溯調整：差一個分割倍數
    run(env, "us.hist")
    assert env.store.quarantine("us")["CCC"] == [(date(2026, 10, 15), D, "split_pending")]
    assert env.store.last_full_refetch("us", "CCC") is None
    run(env, "us.hist", NOW + 1800)
    assert len(env.http.query("hist", "CCC")) == 1  # 2 小時內不重抓
    env.http.prices["CCC"] = {**flat(sessions(date(2026, 10, 15), 260), 100.0), D: 101.0}
    run(env, "us.hist", NOW + 2 * 3600 + 60)
    assert env.store.quarantine("us") == {} and env.store.last_full_refetch("us", "CCC") == D


def test_guard_difference_after_a_known_split_replaces_the_whole_series(env):
    etfs_ready(env)
    seed_universe(env, ["DDD"])
    days = sessions(date(2026, 10, 15), 30)
    seed(env, "DDD", {day: (20.0 if day < date(2026, 10, 14) else 10.0) for day in days})  # 2:1 前的舊值沒調整
    env.store.upsert_splits([("DDD", date(2026, 10, 14), 2.0, 1.0)], "nasdaq")
    env.store.mark_full_refetch("us", "DDD", date(2026, 10, 14))  # 事件那條已經跑過（例如來源當時還沒調整）
    env.http.prices["DDD"] = flat(sessions(D, 260), 10.0)
    assert run(env, "us.hist") == "ok"
    starts = [q["fromdate"] for q in env.http.query("hist", "DDD")]
    assert starts == ["2026-10-12", (D - timedelta(days=400)).isoformat()]  # 守門看到差異 → 整段重抓
    assert {close for close, _, _ in closes(env, "DDD").values()} == {10.0}


def test_plus_23_percent_without_split_is_accepted_after_two_consistent_fetches(env):
    etfs_ready(env)
    seed_universe(env, ["EEE"])
    seed(env, "EEE", flat(HISTORY, 100.0))
    env.http.prices["EEE"] = {**flat(sessions(date(2026, 10, 15), 260), 100.0), D: 123.0}
    run(env, "us.hist")
    assert env.store.pending_jump("us", "EEE")["kind"] == "jump" and D not in closes(env, "EEE")
    run(env, "us.hist", NOW + 900)
    assert closes(env, "EEE")[D] == (123.0, "nasdaq", False)
    assert env.store.pending_jump("us", "EEE") is None and env.store.quarantine("us") == {}
    # 寬度照樣算得到：market_calc 用的就是這些收盤
    series = [row.close for row in env.store.closes("us", "EEE")]
    assert series[-1] / series[-2] == pytest.approx(1.23)


def test_disagreeing_refetch_is_a_genuine_conflict_until_two_fetches_agree(env):
    etfs_ready(env)
    seed_universe(env, ["EEE"])
    seed(env, "EEE", flat(HISTORY, 100.0))
    base = flat(sessions(date(2026, 10, 15), 260), 100.0)
    env.http.prices["EEE"] = {**base, D: 123.0}
    run(env, "us.hist")
    env.http.prices["EEE"] = {**base, D: 118.0}
    run(env, "us.hist", NOW + 900)
    assert env.store.quarantine("us")["EEE"] == [(D, D, "conflict")]
    pending = env.store.pending_jump("us", "EEE")
    assert pending["disagree"] == 1 and pending["closes"] == {"2026-10-16": 118.0}
    run(env, "us.hist", NOW + 1800)
    assert closes(env, "EEE")[D][0] == 118.0
    assert env.store.quarantine("us") == {} and env.store.pending_jump("us", "EEE") is None


# ── 沒有 D、ETF 先抓 ───────────────────────────────────────────────────────


def test_missing_d_is_retried_every_30_minutes_up_to_four_times(env):
    etfs_ready(env)
    seed_universe(env, ["FFF"])
    seed(env, "FFF", flat(HISTORY, 30.0))
    env.http.prices["FFF"] = flat(HISTORY, 30.0)  # 來源還沒有 D
    for step in range(12):  # 15 分鐘一輪，跑 2 小時 45 分
        run(env, "us.hist", NOW + step * 900)
    # 首抓、30、60、90、120 分鐘各一次（4 次重排）→ 之後就不再打
    assert len(env.http.query("hist", "FFF")) == 5
    assert pipe._state(ctx_at(env, NOW), "us.hist")["retry"]["FFF"]["n"] == 5
    assert obs(env, pipe.OBS_HIST_STATUS)["payload"]["exhausted"] == 1


def test_missing_d_then_published(env):
    etfs_ready(env)
    seed_universe(env, ["FFF"])
    seed(env, "FFF", flat(HISTORY, 30.0))
    env.http.prices["FFF"] = flat(HISTORY, 30.0)
    assert run(env, "us.hist") == "partial"  # ETF 都好了、FFF 還沒有 D（不算失敗）
    assert env.store.task_state("us.hist")["fail_streak"] == 0
    env.http.prices["FFF"] = flat(HISTORY + [D], 30.0)
    run(env, "us.hist", NOW + 900)
    assert len(env.http.query("hist", "FFF")) == 1  # 還在等 30 分鐘
    run(env, "us.hist", NOW + 1800)
    assert D in closes(env, "FFF") and "FFF" not in pipe._state(ctx_at(env, NOW), "us.hist")["retry"]


def test_when_no_etf_has_d_constituents_are_not_fetched(env):
    etfs_ready(env, with_d=False)
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    assert run(env, "us.hist") == "not_yet"
    assert env.http.requested("hist") == ["SPY"]  # 2026-10-23：D 公布前只探 SPY，其他 ETF 也等它
    assert obs(env, pipe.OBS_HIST_STATUS)["payload"]["waiting_for_source"] is True
    assert market_tasks.run_task(ctx_at(env, NOW + 900), TASKS["us.hist"]) == "not_yet"
    assert env.http.requested("hist") == ["SPY"]  # 30 分鐘內 SPY 也不重打
    state = env.store.task_state("us.hist")
    assert state["fail_streak"] == 0  # 還沒公布不算失敗


def test_d_is_probed_with_spy_only_then_hourly_until_published(env):
    """2026-10-23（investigations/format_20261023.md §2）：Nasdaq 公布 D 列比收盤後 2 小時晚，以前 4 檔 ETF 各探 5 次
    就整個 D 不再抓，成分股從 10/16 起沒更新過。現在 D 公布前只探 SPY：前 5 次每 30 分鐘、之後每小時；D 在 NOW＋3
    小時出現，同一輪就抓其他 ETF 與成分股。"""
    published = NOW + 3 * 3600

    def source(price: float) -> Callable[[date, date], dict[date, float]]:  # 假的 Nasdaq：NOW＋3 小時起才有 D 列
        return lambda start, end: flat(HISTORY + ([D] if env.clock.wall >= published else []), price)

    seed_universe(env, ["AAA", "BBB"])
    for symbol, price in (("SPY", 100.0), ("VOO", 100.0), ("QQQ", 100.0), ("SOXX", 100.0), ("AAA", 50.0),
                          ("BBB", 50.0)):
        seed(env, symbol, flat(HISTORY, price))
        env.http.hist_hook[symbol] = source(price)
    seen: dict[int, list[str]] = {}
    for step in range(16):  # 15 分鐘一輪，跑到 NOW＋3 小時 45 分
        before = len(env.http.calls)
        kind = run(env, "us.hist", NOW + step * 900)
        seen[step * 15] = [key for name, key, _ in env.http.calls[before:] if name == "hist"]
        assert env.store.task_state("us.hist")["fail_streak"] == 0
        assert kind == ("not_yet" if step < 12 else "ok"), step
    assert [minute for minute, keys in seen.items() if "SPY" in keys] == [0, 30, 60, 90, 120, 180]
    # D 換日（16:30 美東，NOW 是 17:00）滿 2.5 小時，VOO 也開始探（審查 r9 gp2 I3／Codex 席 I1）；公布前只有這兩檔
    assert [minute for minute, keys in seen.items() if "VOO" in keys and minute < 180] == [120, 150]
    assert all(set(keys) <= {"SPY", "VOO"} for minute, keys in seen.items() if minute < 180)
    assert seen[180] == ["SPY", "VOO", "QQQ", "SOXX", "AAA", "BBB"]
    assert all(keys == [] for minute, keys in seen.items() if minute > 180)
    assert all(D in closes(env, symbol) for symbol in ("SPY", "VOO", "QQQ", "SOXX", "AAA", "BBB"))
    status = obs(env, pipe.OBS_HIST_STATUS)["payload"]
    assert status["waiting_for_source"] is False and status["have_d"] == 6 and status["exhausted"] == 0


def test_gate_opens_through_voo_when_spy_alone_keeps_failing(env):
    """2026-10-23 審查 r9 gp2 I3：只看 SPY 的話，SPY 單獨一直回廠商錯誤（或卡在待確認）時，其他 ETF、成分股、
    宏觀代理整天都不抓。SPY 探滿 5 次後 VOO 也每輪探；VOO 有 D 就開閘門。"""
    etfs_ready(env)  # 假的 Nasdaq：其他三檔都有 D
    env.http.hist_raw["SPY"] = {"data": None, "message": None, "status": {
        "rCode": 400, "bCodeMessage": [{"code": 2001, "errorMessage": "Error while calling vendor"}],
        "developerMessage": None}}
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    kinds = [run(env, "us.hist", NOW + step * 900) for step in range(10)]
    assert D in closes(env, "VOO") and D in closes(env, "QQQ") and D in closes(env, "AAA")
    assert D not in closes(env, "SPY") and env.store.task_state("us.hist")["fail_streak"] == 0
    assert "ok" in kinds or "partial" in kinds


def test_gate_opens_through_voo_when_spy_keeps_giving_bad_data(env):
    """2026-10-23 審查 r9 Codex 席 I1：SPY 回格式錯（或卡在待確認）時不會記 miss，只看 SPY 重排次數的備援永遠不
    啟動，其他 ETF、成分股、宏觀代理就凍結好幾天。SPY 壞過一次，下一輪 VOO 就一起探；VOO 有 D 就開閘門。"""
    etfs_ready(env)
    env.http.hist_raw["SPY"] = {"data": {"symbol": "QQQ", "totalRecords": 0, "tradesTable": None}, "message": None,
                                "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    for step in range(4):
        run(env, "us.hist", NOW + step * 900)
    assert D in closes(env, "VOO") and D in closes(env, "QQQ") and D in closes(env, "AAA")
    # SPY 自己的資料壞了照樣記失敗（每日維護看得到），只是不再擋住其他檔
    assert D not in closes(env, "SPY") and env.store.task_state("us.hist")["fail_streak"] >= 1


def test_gate_opens_even_when_the_old_etf_retries_are_used_up(env):
    """2026-10-23：升級那天的狀態——舊版已經把 4 檔 ETF 這個 D 的 5 次都用完（{"d": D, "n": 5}），閘門整個 D 鎖死。
    現在 SPY 照每小時探；SPY 有 D 之後其他 ETF 的舊紀錄清掉，同一輪連成分股一起抓到 D。"""
    etfs_ready(env)  # 假的 Nasdaq 已經有 D
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    used_up = {"d": D.isoformat(), "n": 5, "at": NOW - 600}
    env.store.set_task_state("sched:us.hist", {"retry": {sym: dict(used_up) for sym in ("SPY", "VOO", "QQQ", "SOXX")}})
    assert run(env, "us.hist") == "ok"
    assert env.http.requested("hist") == ["SPY", "VOO", "QQQ", "SOXX", "AAA"]
    assert all(D in closes(env, symbol) for symbol in ("SPY", "VOO", "QQQ", "SOXX", "AAA"))
    assert pipe._state(ctx_at(env, NOW), "us.hist")["retry"] == {}


def test_backfill_member_without_nasdaq_data_is_not_a_failure(env):
    """2026-10-23（investigations/format_20261023.md §1.1）：Nasdaq 對 BF.B 回空殼。以前判成 format：只剩它一檔時整個
    backfill 失敗、退避 2 小時（fail_streak 到 44），也不記重排、每次都重打。現在當成「還沒有 D」：不算失敗、
    每個 D 最多 5 次，另一檔照樣補進來。"""
    etfs_ready(env)
    seed_universe(env, ["BF.B", "NEWX"])
    env.http.prices["NEWX"] = flat(sessions(D, 250), 20.0)
    env.http.hist_raw["BF.B"] = nasdaq({"symbol": None, "totalRecords": 0, "tradesTable": None})  # 活回應原樣
    assert run(env, "us.hist") == "ok"  # SPY 有 D：成分股（含還沒有歷史的）可以抓了
    kinds = [run(env, "us.hist.backfill", NOW + step * 900) for step in range(12)]  # 15 分鐘一輪，2 小時 45 分
    assert kinds[0] == "partial"  # BF.B 還沒有 D、NEWX 補進來
    assert set(kinds[1:]) <= {"not_yet", "not_due"} and "not_yet" in kinds[1:]
    assert env.store.task_state("us.hist.backfill")["fail_streak"] == 0
    assert max(closes(env, "NEWX")) == D
    assert len(env.http.query("hist", "BF.B")) == 5  # 首抓＋4 次重排（每 30 分鐘），之後這個 D 不再打
    assert pipe._state(ctx_at(env, NOW), "us.hist")["retry"]["BF.B"]["n"] == 5
    outcomes = obs(env, pipe.OBS_HIST_STATUS)["payload"]["outcomes"]
    assert outcomes["missing_d"] == 5 and not [kind for kind in outcomes if kind.startswith("error:")]


def test_vendor_error_is_retried_like_a_missing_d_but_counted(env):
    """2026-10-23 審查 r7b F2：Nasdaq 廠商錯誤（data: null＋rCode 400；外殼形狀是假設的，見 test_market_sources_us 的
    VENDOR_ERROR）以前被當成「還沒有 D」吞掉。現在一樣走每個 D 最多 5 次的重排（不會每輪重打、不算失敗），但結果記成
    vendor，us.hist.status 的 vendor 看得到。"""
    etfs_ready(env)
    seed_universe(env, ["AAA", "COO"])
    for symbol in ("AAA", "COO"):
        seed(env, symbol, flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    env.http.hist_raw["COO"] = {"data": None, "message": None, "status": {
        "rCode": 400, "bCodeMessage": [{"code": 2001, "errorMessage": "Error while calling vendor"}],
        "developerMessage": None}}
    kinds = [run(env, "us.hist", NOW + step * 900) for step in range(12)]
    assert kinds[0] == "partial" and set(kinds[1:]) <= {"ok", "not_yet"}
    assert env.store.task_state("us.hist")["fail_streak"] == 0
    assert len(env.http.query("hist", "COO")) == 5  # 首抓＋4 次重排，之後這個 D 不再打
    status = obs(env, pipe.OBS_HIST_STATUS)["payload"]
    assert status["outcomes"]["vendor"] == 5 and "missing_d" not in status["outcomes"]
    assert status["vendor"] == 5 and status["exhausted"] == 1 and D in closes(env, "AAA")


def test_backfill_waits_for_spy_to_have_d(env):
    """2026-10-23：還沒有歷史的成分股也等 SPY 有 D——D 公布前只探 SPY；公布前補的一年只到 D−1，公布後還得再抓一次。"""
    etfs_ready(env, with_d=False)
    seed_universe(env, ["NEWX"])
    env.http.prices["NEWX"] = flat(sessions(date(2026, 10, 15), 250), 20.0)
    assert run(env, "us.hist") == "not_yet"
    assert not TASKS["us.hist.backfill"].due(ctx_at(env, NOW + 60))
    for symbol in pipe.ETFS:
        env.http.prices[symbol] = flat(HISTORY + [D], 100.0)  # Nasdaq 公布 D 了
    assert run(env, "us.hist", NOW + 1800) == "ok"
    assert TASKS["us.hist.backfill"].due(ctx_at(env, NOW + 1860))


def test_backfill_does_not_reset_the_waiting_flag(env):
    """2026-10-23（investigations/format_20261023.md §3.2 第 3 點）：以前 backfill 跑完就用 waiting=False 覆寫整份
    us.hist.status，閘門鎖住時「等來源」的旗標在 backfill 到下一次 core 之間會翻成 false。"""
    etfs_ready(env, with_d=False)
    seed_universe(env, ["NEWX"])
    env.http.prices["NEWX"] = flat(sessions(date(2026, 10, 15), 250), 20.0)
    assert run(env, "us.hist") == "not_yet"
    assert obs(env, pipe.OBS_HIST_STATUS)["payload"]["waiting_for_source"] is True
    pipe._run_hist_backfill(ctx_at(env, NOW + 60))  # 直接跑（繞過 due）：它寫的狀態也不能把旗標翻掉
    status = obs(env, pipe.OBS_HIST_STATUS)["payload"]
    assert status["outcomes"]["missing_d"] >= 1 and status["waiting_for_source"] is True


def test_hist_status_records_the_members_typical_last_date(env):
    """2026-10-23：成分股最後正式日期的中位數（偶數檔取偏舊那個）進 us.hist.status；jobs/market_snapshot.py 拿它和
    當下的 D 算「落後幾個交易日」。還沒有歷史的不算。"""
    etfs_ready(env, with_d=False)
    seed_universe(env, ["AAA", "BBB", "CCC", "DDD", "NEWX"])
    for symbol, last in (("AAA", date(2026, 10, 9)), ("BBB", date(2026, 10, 13)), ("CCC", date(2026, 10, 14)),
                         ("DDD", date(2026, 10, 15))):
        seed(env, symbol, flat(sessions(last, 20), 10.0))
    run(env, "us.hist")
    status = obs(env, pipe.OBS_HIST_STATUS)["payload"]
    assert status["members_last"] == "2026-10-13" and status["vendor"] == 0


# ── 預算、should_stop、每輪 60 檔 ──────────────────────────────────────────


def test_at_most_60_symbols_per_run_shared_with_backfill(env):
    etfs_ready(env)
    names = [f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(70)]
    seed_universe(env, names + ["NEWX"])
    for name in names:
        seed(env, name, flat(HISTORY, 10.0))
        env.http.prices[name] = flat(HISTORY + [D], 10.0)
    ctx = ctx_at(env, NOW)
    assert market_tasks.run_task(ctx, TASKS["us.hist"]) == "ok"
    assert len(env.http.requested("hist")) == 60
    assert not TASKS["us.hist.backfill"].due(ctx)  # 同一輪：上限已經用完
    ctx = ctx_at(env, NOW + 900)
    market_tasks.run_task(ctx, TASKS["us.hist"])
    assert len(env.http.requested("hist")) == 74 and TASKS["us.hist.backfill"].due(ctx)


def test_ledger_budget_stops_the_batch_without_counting_a_failure(env):
    etfs_ready(env)
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    env.ledger.cap["api.nasdaq.com"] = 2
    assert run(env, "us.hist") == "partial"
    assert env.http.requested("hist") == ["SPY", "VOO"]
    assert env.store.task_state("us.hist")["fail_streak"] == 0
    assert pipe._state(ctx_at(env, NOW), "us.hist")["run"]["used"] == 2  # 沒放行的那次不算進 60 檔


def test_should_stop_before_any_request_is_not_a_success_or_a_failure(env):
    etfs_ready(env)
    ctx = ctx_at(env, NOW)
    ctx.task_deadline = env.clock() - 1.0
    with pytest.raises(market_http.FetchError) as caught:
        pipe._run_hist_core(ctx)
    assert caught.value.kind == "budget" and env.http.calls == []


def test_should_stop_is_checked_between_batch_items(env):
    late = "2026-10-16T16:30:00"
    for symbol in pipe.OPTION_SYMBOLS:
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade=late)
    env.http.seconds_per_call = 50.0  # 每次抓 50 秒；這個任務 95 秒
    assert run(env, "us.options") == "partial"
    assert env.http.requested("cboe") == ["QQQ", "SPY"]
    assert run(env, "us.options", NOW + 60) == "ok"  # 下一輪接著抓剩下的
    assert env.http.requested("cboe") == ["QQQ", "SPY", "SOXX"]


# ── CBOE ─────────────────────────────────────────────────────────────────────


def test_cboe_stale_file_is_rejected_as_a_failure(env):
    for symbol in pipe.OPTION_SYMBOLS:  # timestamp 比抓取時間早 13 小時
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 08:00:00", last_trade="2026-10-16T16:30:00")
    assert run(env, "us.options") == "stale"
    got = obs(env, pipe.OBS_OPTIONS["QQQ"])
    assert got["payload"] is None and got["fail_streak"] == 1 and got["error"] == "stale"
    assert env.store.task_state("us.options")["fail_streak"] == 1


def test_cboe_data_date_is_the_eastern_date_of_the_last_trade(env):
    # last_trade 美東 10/16 20:30 ＝ UTC 10/17 00:30；抓的是台北週六 16:31（timestamp 晚 7 小時多，還在 12 小時內）
    now = at(2026, 10, 17, 16, 31, tz=ZoneInfo("Asia/Taipei"))
    conflict = [option("QQQ261120C00710000", oi=50), option("QQQ261120C00710000", oi=60)]
    env.http.chains["QQQ"] = cboe_chain("QQQ", timestamp="2026-10-17 00:40:00", last_trade="2026-10-16T20:30:00",
                                        options=[option("QQQ261120C00700000"), option("QQQ261120P00700000")] + conflict)
    for symbol in ("SPY", "SOXX"):
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-17 00:40:00", last_trade="2026-10-16T19:59:00")
    assert run(env, "us.options", now) == "ok"
    got = obs(env, pipe.OBS_OPTIONS["QQQ"])
    assert got["data_date"] == "2026-10-16"
    payload = got["payload"]
    assert payload["symbol"] == "QQQ" and payload["quote_last_trade_et"] == "2026-10-16T20:30:00-04:00"
    assert payload["conflicts"] == [["QQQ261120C00710000", None]] and payload["conflict_count"] == 2
    chain = pipe.chain_from_payload(payload)
    assert chain.contracts[0].expiry == date(2026, 11, 20) and chain.timestamp_utc.tzinfo is not None
    legs_oi, _legs_gamma, stats = market_calc.option_sets(chain, D)
    assert len(legs_oi) == 2 and stats.unattributed_conflicts == 0  # 衝突列有代號：覆蓋率不是「下限」


def test_cboe_not_published_yet_is_not_a_failure(env):
    for symbol in pipe.OPTION_SYMBOLS:  # 還是 D 前一天的檔
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-15T16:15:00")
    assert run(env, "us.options") == "not_yet"
    got = obs(env, pipe.OBS_OPTIONS["SPY"])
    assert got["payload"] is None and got["fail_streak"] == 0 and got["checked_at"] is not None
    assert env.store.task_state("us.options")["fail_streak"] == 0
    assert not TASKS["us.options"].due(ctx_at(env, NOW + 600))  # 30 分鐘後再看
    assert TASKS["us.options"].due(ctx_at(env, NOW + 1800))


def test_cboe_intraday_snapshot_does_not_count_as_that_day(env):
    for symbol in pipe.OPTION_SYMBOLS:  # 12:33 美東產生的檔（timestamp 16:33 UTC）
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 16:33:11", last_trade="2026-10-16T12:18:10")
    assert run(env, "us.options", at(2026, 10, 16, 17, 0)) == "not_yet"
    assert obs(env, pipe.OBS_OPTIONS["SPY"])["payload"] is None


def test_cboe_failure_is_recorded_on_the_obs(env):
    env.http.fail["cboe:QQQ"] = "status"
    for symbol in ("SPY", "SOXX"):
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T16:30:00")
    assert run(env, "us.options") == "partial"
    assert obs(env, pipe.OBS_OPTIONS["QQQ"])["fail_streak"] == 1
    assert obs(env, pipe.OBS_OPTIONS["SPY"])["data_date"] == "2026-10-16"


def test_vix_uses_last_trade_date_and_keeps_its_own_history(env):
    for day, stamp, close in ((date(2026, 10, 15), "2026-10-15T16:15:01", 16.46), (D, "2026-10-16T16:15:01", 19.99)):
        for symbol in pipe.VIX_SYMBOLS:
            env.http.quotes[symbol] = cboe_quote(symbol, timestamp=f"{day.isoformat()} 21:37:16", last_trade=stamp,
                                                 close=close)
        assert run(env, "us.vix", datetime.combine(day, datetime.min.time(), ET).timestamp() + 18 * 3600) == "ok"
    payload = obs(env, pipe.OBS_QUOTE["VIX"])["payload"]
    assert obs(env, pipe.OBS_QUOTE["VIX"])["data_date"] == "2026-10-16"
    assert payload["history"] == [["2026-10-15", 16.46], ["2026-10-16", 19.99]] and payload["price_change"] == 3.53


def test_provisional_cboe_close_when_nasdaq_is_blocked_then_nasdaq_overwrites_without_the_guard(env):
    for symbol in pipe.ETFS:
        seed(env, symbol, flat(HISTORY, 700.0))
    env.http.fail["hist:*"] = "blocked"  # Nasdaq 403
    assert run(env, "us.hist") == "blocked"
    assert env.ledger.cooling == {"api.nasdaq.com"}
    for symbol in pipe.OPTION_SYMBOLS:
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T16:15:00")
    env.http.quotes["VOO"] = cboe_etf_quote("VOO", timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T15:59:59",
                                            close=702.3)
    assert run(env, "us.options", NOW + 600) == "ok"
    assert closes(env, "SPY")[D] == (717.23, "cboe", True)
    assert closes(env, "VOO")[D] == (702.3, "cboe", True)  # 2026-10-23：VOO 沒有期權鏈，改抓一個小報價
    # Nasdaq 回來：正式列直接蓋掉備援（差 1% 也不經守門，因為不同來源）
    env.http.fail.clear()
    env.ledger.cooling.clear()
    for symbol in pipe.ETFS:
        env.http.prices[symbol] = {**flat(HISTORY, 700.0), D: 709.80}
    later = NOW + 3 * 3600
    assert run(env, "us.hist", later) == "ok"
    assert closes(env, "SPY")[D] == (709.80, "nasdaq", False) and closes(env, "VOO")[D] == (709.80, "nasdaq", False)
    assert env.store.pending_jump("us", "SPY") is None
    assert not pipe.nasdaq_blocked(ctx_at(env, later))


def test_no_provisional_close_when_nasdaq_has_d_or_the_trade_is_well_before_the_close(env):
    """2026-10-23 起不必等 Nasdaq 被擋：只要還沒有 D 的正式列就寫；已經有就不寫。CBOE 寫 15:59:59，收盤前 60 秒內
    （含）的最後成交算收盤，更早的是盤中的檔。"""
    seed(env, "SPY", flat(HISTORY + [D], 700.0))  # Nasdaq 已經有 D
    for symbol in ("QQQ", "SOXX"):  # 備援要和最近的正式收盤差不多才寫（審查 r9 gp2 C1）
        seed(env, symbol, flat(HISTORY, 710.0))
    for symbol in pipe.OPTION_SYMBOLS:
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T15:59:59")
    assert run(env, "us.options") == "ok"
    assert closes(env, "SPY")[D] == (700.0, "nasdaq", False)
    assert closes(env, "QQQ")[D] == (717.23, "cboe", True)  # QQQ 還沒有 D：照寫備援
    ctx = ctx_at(env, NOW)
    for last_trade, written in (("2026-10-16T15:58:59", False), ("2026-10-16T15:59:00", True)):
        chain = src.parse_cboe_options(json.dumps(cboe_chain("SOXX", timestamp="2026-10-16 20:45:00",
                                                             last_trade=last_trade)).encode())
        assert pipe._maybe_provisional(ctx, D, chain, chain.quote_last_trade_et) is written, last_trade


def test_cboe_close_is_the_provisional_d_bar_while_nasdaq_is_late(env):
    """2026-10-23（investigations/freshness_20261023.md §3 第 2 點）：Nasdaq 只是晚公布（沒被擋）、CBOE 寫 15:59:59 時，
    以前這條備援從來沒觸發，ETF 列整晚停在 D−1。現在收盤後 CBOE 一到，4 檔 ETF 都有 D 的 provisional；它不算
    Nasdaq 公布了 D（成分股照樣等 SPY 的正式列），正式列到了直接蓋掉。"""
    etfs_ready(env, with_d=False, price=710.0)  # 和 CBOE 的 717.23／702.3 差不多（離最近正式收盤太遠的不寫，r9 gp2 C1）
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(HISTORY, 50.0))
    env.http.prices["AAA"] = flat(HISTORY + [D], 50.0)
    for symbol in ("QQQ", "SPY", "SOXX"):
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T15:59:59")
    env.http.quotes["VOO"] = cboe_etf_quote("VOO", timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T15:59:59",
                                            close=702.3)
    assert run(env, "us.hist") == "not_yet"  # Nasdaq 還沒有 D
    assert run(env, "us.options", NOW + 60) == "ok"
    assert {symbol: closes(env, symbol)[D] for symbol in ("SPY", "VOO", "QQQ", "SOXX")} == {
        "SPY": (717.23, "cboe", True), "VOO": (702.3, "cboe", True), "QQQ": (717.23, "cboe", True),
        "SOXX": (717.23, "cboe", True)}
    assert env.http.requested("quote") == ["VOO"]
    assert run(env, "us.hist", NOW + 1800) == "not_yet"  # 備援不算 Nasdaq 公布了 D：還是只探 SPY
    assert env.http.requested("hist") == ["SPY", "SPY"] and D not in closes(env, "AAA")
    assert obs(env, pipe.OBS_HIST_STATUS)["payload"]["waiting_for_source"] is True
    for symbol in ("SPY", "VOO", "QQQ", "SOXX"):
        env.http.prices[symbol] = flat(HISTORY + [D], 710.0)  # Nasdaq 公布了
    assert run(env, "us.hist", NOW + 3600) == "ok"
    assert all(closes(env, symbol)[D] == (710.0, "nasdaq", False) for symbol in ("SPY", "VOO", "QQQ", "SOXX"))
    assert closes(env, "AAA")[D][1:] == ("nasdaq", False)


def test_cboe_close_far_from_the_last_official_close_is_not_written(env):
    """2026-10-23 審查 r9 gp2 C1：ETF 分割日 CBOE 已是拆後價、或檔案給了離譜值，以前照寫成 D 的收盤、上卡（「7.02
    -98.94% 在月線下」，卡上看不出是備援）。和最近一筆正式收盤差到跳空門檻就不寫，等 Nasdaq；沒有正式收盤可比也不寫。"""
    seed(env, "SPY", flat(HISTORY, 700.0))
    seed(env, "SOXX", flat(HISTORY, 750.0))
    ctx = ctx_at(env, NOW)
    for symbol, close, written in (("SPY", 7.02, False), ("SPY", 70200.0, False), ("SPY", 702.3, True),
                                   ("SOXX", 250.0, False), ("QQQ", 717.23, False)):  # QQQ 沒有正式收盤可比
        chain = src.parse_cboe_options(json.dumps(cboe_chain(symbol, timestamp="2026-10-16 20:45:00",
                                                             last_trade="2026-10-16T15:59:59", close=close)).encode())
        assert pipe._maybe_provisional(ctx, D, chain, chain.quote_last_trade_et) is written, (symbol, close)
    assert D not in closes(env, "SOXX") and closes(env, "SPY")[D] == (702.3, "cboe", True)


def test_voo_close_quote_is_fetched_only_when_it_helps(env):
    """2026-10-23：VOO 的 CBOE 報價只在「已經有歷史、Nasdaq 還沒有 D 的正式列」時才抓；報價檔還沒到收盤後
    （ETF 檔常晚更新）→ 不算失敗，30 分鐘後再抓。"""
    for symbol in ("QQQ", "SPY", "SOXX"):
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T15:59:59")
    env.http.quotes["VOO"] = cboe_etf_quote("VOO", timestamp="2026-10-16 19:30:00", last_trade="2026-10-15T15:59:59",
                                            close=710.72)  # 還是 D−1 的檔
    assert run(env, "us.options") == "ok" and env.http.requested("quote") == []  # VOO 還沒有歷史：不抓
    seed(env, "VOO", flat(HISTORY, 700.0))
    assert run(env, "us.options", NOW + 60) == "not_yet" and env.http.requested("quote") == ["VOO"]
    assert D not in closes(env, "VOO") and env.store.task_state("us.options")["fail_streak"] == 0
    assert not TASKS["us.options"].due(ctx_at(env, NOW + 900))  # 30 分鐘後再看
    env.http.quotes["VOO"] = cboe_etf_quote("VOO", timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T15:59:59",
                                            close=702.3)
    assert run(env, "us.options", NOW + 1860) == "ok"
    assert closes(env, "VOO")[D] == (702.3, "cboe", True) and env.http.requested("quote") == ["VOO", "VOO"]
    assert not TASKS["us.options"].due(ctx_at(env, NOW + 3600))  # 做完了


def test_cboe_evening_evidence_marks_an_unscheduled_closure(env):
    evening = at(2026, 10, 16, 20, 30)
    for symbol in pipe.OPTION_SYMBOLS:  # 整天沒成交：最後成交還是前一交易日
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-17 00:25:00", last_trade="2026-10-15T19:59:00")
    assert run(env, "us.options", evening) == "not_yet"
    override = env.store.calendar_overrides("us")[D]
    assert override["kind"] == "closed" and override["confirmed"] is False and "cboe" in override["evidence"]
    assert pipe.current_d(ctx_at(env, evening + 60)) == date(2026, 10, 15)


def test_no_closure_evidence_before_the_evening(env):
    for symbol in pipe.OPTION_SYMBOLS:
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 21:00:00", last_trade="2026-10-15T19:59:00")
    run(env, "us.options", at(2026, 10, 16, 17, 30))
    assert env.store.calendar_overrides("us") == {}


# ── NYSE 日曆核對（market-info） ─────────────────────────────────────────────


def test_market_info_business_day_false_writes_a_provisional_override(env):
    env.http.market_info = market_info(business=False)
    assert run(env, "us.calendar", at(2026, 10, 16, 10, 0)) == "ok"
    check = obs(env, pipe.OBS_CALENDAR_CHECK)["payload"]
    assert check["T"] == "2026-10-16" and "is_business_day" in check["mismatches"]
    assert env.store.calendar_overrides("us")[D]["kind"] == "closed"
    assert check["overrides"][0]["date"] == "2026-10-16"


def test_market_info_previous_trade_date_skipping_a_day(env):
    env.http.market_info = market_info(previous="Oct 14, 2026")  # 表上 10/15 是交易日
    run(env, "us.calendar", at(2026, 10, 16, 10, 0))
    assert list(env.store.calendar_overrides("us")) == [date(2026, 10, 15)]


def test_market_info_next_trade_date_skip_writes_only_small_spans(env):
    env.http.market_info = market_info(following="Oct 21, 2026")  # 跳過 10/19、10/20（例：事先公布的國喪）
    run(env, "us.calendar", at(2026, 10, 16, 10, 0))
    assert list(env.store.calendar_overrides("us")) == [date(2026, 10, 19), date(2026, 10, 20)]


def test_market_info_far_future_next_trade_date_is_only_a_mismatch(env):
    env.http.market_info = market_info(following="Dec 15, 2026")  # 壞掉的回應：不能一口氣關掉兩個月
    assert run(env, "us.calendar", at(2026, 10, 16, 10, 0)) == "ok"
    assert env.store.calendar_overrides("us") == {}
    check = obs(env, pipe.OBS_CALENDAR_CHECK)["payload"]
    assert "next_trade_date" in check["mismatches"] and check["overrides"] == []


def test_market_info_from_yesterday_or_on_weekends_is_not_compared(env):
    env.http.market_info = market_info(closing="Oct 15, 2026 04:00 PM ET", previous="Oct 14, 2026",
                                       following="Oct 16, 2026")  # 還沒換日
    assert run(env, "us.calendar", at(2026, 10, 16, 10, 0)) == "ok"
    assert env.store.calendar_overrides("us") == {}
    assert not TASKS["us.calendar"].due(ctx_at(env, at(2026, 10, 17, 12, 0)))  # 週六
    assert not TASKS["us.calendar"].due(ctx_at(env, at(2026, 10, 19, 9, 0)))  # 開盤前
    assert not TASKS["us.calendar"].due(ctx_at(env, at(2026, 10, 16, 15, 0)))  # 今天已經核對過


# ── 分割事件表 ───────────────────────────────────────────────────────────────


def test_split_calendar_bootstrap_then_d_minus_7_and_resume_after_downtime(env):
    env.http.splits = [
        {"symbol": "KNDI", "name": "Kandi", "ratio": "1:10", "executionDate": "10/16/2026"},
        {"symbol": "ODD", "name": "Odd Co", "ratio": "see note", "executionDate": "10/15/2026"},
    ]
    assert run(env, "us.splits") == "ok"
    assert env.http.query("splits", "")[0]["date"] == (D - timedelta(days=400)).isoformat()
    assert env.store.coverage("nasdaq.splits") == [(D - timedelta(days=400), D)]
    kndi = env.store.splits_since("KNDI", date(2026, 1, 1))[0]
    assert (kndi.new, kndi.old) == (1.0, 10.0)
    odd = env.store.splits_since("ODD", date(2026, 1, 1))[0]
    assert (odd.new, odd.old) == (1.0, 1.0)  # 比例看不懂：事件照樣記（倍數未知），一樣會觸發整段重抓
    monday = at(2026, 10, 19, 17, 0)
    run(env, "us.splits", monday)
    assert env.http.query("splits", "")[-1]["date"] == "2026-10-12"  # D−7
    env.store.add_coverage("nasdaq.splits", date(2026, 9, 1), date(2026, 9, 1))  # 不影響：取最後的終點
    late = at(2026, 11, 6, 17, 0)  # 停機兩週：從上次涵蓋的終點接續
    run(env, "us.splits", late)
    assert env.http.query("splits", "")[-1]["date"] == "2026-10-19"


def test_unknown_ratio_split_still_triggers_a_full_refetch(env):
    etfs_ready(env)
    seed_universe(env, ["ODD"])
    seed(env, "ODD", flat(HISTORY, 5.0))
    env.store.upsert_splits([("ODD", D, 1.0, 1.0)], "nasdaq")
    env.http.prices["ODD"] = flat(sessions(D, 260), 5.0)
    run(env, "us.hist")
    assert [q["fromdate"] for q in env.http.query("hist", "ODD")] == [(D - timedelta(days=400)).isoformat()]
    assert env.store.last_full_refetch("us", "ODD") == D


# ── 持股清單、前十大、13F、基本面 ─────────────────────────────────────────────


SPY_NAMED = [("NVIDIA CORP", "NVDA", 7.9), ("APPLE INC", "AAPL", 6.5), ("MICROSOFT CORP", "MSFT", 6.0),
             ("AMAZON.COM INC", "AMZN", 4.0), ("META PLATFORMS", "META", 2.5), ("BROADCOM INC", "AVGO", 2.4),
             ("ALPHABET INC CL A", "GOOGL", 2.0), ("TESLA INC", "TSLA", 1.8), ("ALPHABET INC CL C", "GOOG", 1.6),
             ("BERKSHIRE HATHAWAY CL B", "BRK.B", 1.5), ("JPMORGAN CHASE", "JPM", 1.4), ("ELI LILLY", "LLY", 1.3),
             ("VISIT https://spam.example NOW", "SPAM", 0.02)]


def test_ssga_holdings_are_stored_with_cleaned_names_and_not_overwritten_by_older_lists(env):
    env.http.ssga = ssga_xlsx(SPY_NAMED)
    assert run(env, "us.holdings.spy") == "ok"  # As of 10/15 ＝ D 的前一交易日：這一天的清單到了
    got = obs(env, pipe.OBS_HOLDINGS_SPY)
    assert got["data_date"] == "2026-10-15"
    rows = {row[0]: row for row in got["payload"]["rows"]}
    assert rows["NVDA"][1:3] == ["NVIDIA CORP", 7.9] and rows["SPAM"][1] is None  # 含網址的名稱整則丟掉
    assert got["payload"]["count"] == len(rows) and "BRK.B" in rows
    env.http.ssga = ssga_xlsx(SPY_NAMED, as_of="As of 13-Oct-2026")
    monday = at(2026, 10, 19, 17, 0)
    assert run(env, "us.holdings.spy", monday) == "not_yet"  # 舊清單：不覆寫、晚點再看
    assert obs(env, pipe.OBS_HOLDINGS_SPY)["data_date"] == "2026-10-15"


def test_nasdaq100_list_keeps_market_caps(env):
    rows = [{"symbol": "AAPL", "companyName": "Apple Inc.", "marketCap": "5,027,985,434,182"},
            {"symbol": "GOOGL", "companyName": "Alphabet A", "marketCap": "2,000,000,000,000"}]
    rows += [{"symbol": f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}", "companyName": f"Example {i}", "marketCap": "1,000"}
             for i in range(98)]
    env.http.ndx = nasdaq({"totalrecords": len(rows), "limit": 101, "offset": 0, "date": "Oct 16, 2026 12:34 PM",
                           "data": {"asOf": None, "headers": {}, "rows": rows}})
    assert run(env, "us.holdings.ndx") == "ok"
    got = obs(env, pipe.OBS_HOLDINGS_NDX)
    assert got["data_date"] == "2026-10-16" and got["payload"]["rows"][0] == ["AAPL", "Apple Inc.", None, 5027985434182.0]
    ctx = ctx_at(env, NOW)
    assert pipe._market_cap(ctx, "GOOGL") == (2e12, pipe.MARKET_CAP_NDX) and pipe._market_cap(ctx, "JPM") == (None, None)


def test_moneydj_lists_weekly(env):
    rows = [(f"{s}.US", (f"{s} Corp", f"{4.0 - i * 0.1:.2f}")) for i, s in enumerate(
        ["NVDA", "AVGO", "AMD", "QCOM", "TXN", "MU", "INTC", "AMAT", "LRCX", "KLAC", "ADI", "MRVL", "NXPI", "MCHP",
         "ON", "MPWR", "TER", "ENTG", "SWKS", "QRVO", "COHR"])]
    env.http.moneydj["SOXX"] = moneydj_html("SOXX", rows)
    assert run(env, "us.holdings.soxx") == "ok"
    got = obs(env, pipe.OBS_HOLDINGS_SOXX)
    assert got["data_date"] == "2026-09-30" and got["payload"]["rows"][0][0] == "NVDA"
    assert not TASKS["us.holdings.soxx"].due(ctx_at(env, NOW + 86400))  # 同一週不再抓
    assert TASKS["us.holdings.soxx"].due(ctx_at(env, at(2026, 10, 19, 12, 0)))


def put_list(env: Env, obs_id: str, rows: list[tuple[str, float]], as_of: str = "2026-10-15") -> None:
    env.store.put_obs(obs_id, {"source": "x", "as_of": as_of,
                               "rows": [[symbol, f"{symbol} name", weight, None] for symbol, weight in rows]},
                      data_date=as_of)


def test_13f_top10_merges_share_classes_and_queries_each_company_once(env):
    put_list(env, pipe.OBS_HOLDINGS_SPY, [(t, w) for _, t, w in SPY_NAMED[:-1]] + [("BRK.A", 0.2)])
    put_list(env, pipe.OBS_HOLDINGS_QQQ, [("NVDA", 9.0), ("GOOG", 2.6), ("GOOGL", 2.8), ("AAPL", 8.0)], "2026-09-30")
    put_list(env, pipe.OBS_HOLDINGS_SOXX, [("NVDA", 9.5), ("AVGO", 8.0), ("AMD", 7.0)], "2026-09-30")
    assert run(env, "us.13f") == "ok"
    voo = obs(env, pipe.OBS_TOP10["VOO"])["payload"]
    alphabet = next(row for row in voo["rows"] if "GOOGL" in row["symbols"])
    assert alphabet["symbols"] == ["GOOG", "GOOGL"] and alphabet["weight"] == pytest.approx(3.6)
    assert alphabet["pick"] == "GOOGL"  # 權重較大的股別
    berkshire = next(row for row in voo["rows"] if "BRK.B" in row["symbols"])
    assert berkshire["symbols"] == ["BRK.A", "BRK.B"] and berkshire["pick"] == "BRK.B"
    assert len(voo["rows"]) == 10 and voo["source"] == "SPY 每日持股"
    queried = env.http.requested("13f")
    assert len(queried) == len(set(queried)) and "GOOG" not in queried and "BRK.A" not in queried
    assert {"GOOGL", "BRK.B", "NVDA", "AMD"} <= set(queried)
    got = obs(env, pipe.OBS_13F_PREFIX + "NVDA")
    assert got["period"] == "2026-06-30" and got["data_date"] is None  # 申報期間的眾數；彙總期間來源未標
    assert got["payload"]["increased"] == 3369 and got["payload"]["holder_period_mode"] == "2026-06-30"
    assert not TASKS["us.13f"].due(ctx_at(env, NOW + 3600))
    assert TASKS["us.13f"].due(ctx_at(env, at(2026, 11, 2, 12, 0)))  # 申報季：每天抓


def test_13f_partial_failure_keeps_the_rest(env):
    put_list(env, pipe.OBS_HOLDINGS_SOXX, [("NVDA", 9.5), ("AVGO", 8.0)], "2026-09-30")
    env.http.fail["13f:AVGO"] = "status"
    assert run(env, "us.13f") == "partial"
    assert obs(env, pipe.OBS_13F_PREFIX + "AVGO")["fail_streak"] == 1
    assert obs(env, pipe.OBS_13F_PREFIX + "NVDA")["fail_streak"] == 0
    assert TASKS["us.13f"].due(ctx_at(env, NOW + 60))  # 失敗的那檔這週還會再試


def test_fund_quarters_with_nasdaq_market_cap(env):
    put_list(env, pipe.OBS_HOLDINGS_SOXX, [("NVDA", 9.5)], "2026-09-30")
    env.store.put_obs(pipe.OBS_HOLDINGS_NDX, {"rows": [["NVDA", "NVIDIA", None, 4.2e12]]}, data_date="2026-10-16")
    assert run(env, "us.fund") == "ok"
    got = obs(env, pipe.OBS_FUND_PREFIX + "NVDA")
    assert got["data_date"] == "2026-07-26"
    assert got["payload"]["quarters"][0] == ["2025-10-26", 19309000.0] and got["payload"]["market_cap_usd"] == 4.2e12
    assert got["payload"]["market_cap_source"] == "nasdaq100_list"
    assert env.http.requested("summary") == []  # Nasdaq-100 名單已經有市值：不另查


# 不在 Nasdaq-100 的前十大：季度損益照 Nasdaq financials 的形狀合成（JPM、BRK.B 沒有存真實回應；數字取
# 讓近四季本益比約 14 的量級，主 Claude 10/18 實測兩家 TTM 本益比都約 14），市值照 fixtures_raw 的 summary。
JPM_QUARTERS = financials(
    {"value1": "Net Income Applicable to Common Shareholders", "value2": "$17,280,000", "value3": "$15,830,000",
     "value4": "$16,140,000", "value5": "$15,250,000"},
    {"value1": "Quarterly Ending:", "value2": "9/30/2026", "value3": "6/30/2026", "value4": "3/31/2026",
     "value5": "12/31/2025"})
BRK_QUARTERS = financials(
    {"value1": "Net Income Applicable to Common Shareholders", "value2": "$30,250,000", "value3": "$12,370,000",
     "value4": "$4,600,000", "value5": "$36,620,000"},
    {"value1": "Quarterly Ending:", "value2": "6/30/2026", "value3": "3/31/2026", "value4": "12/31/2025",
     "value5": "9/30/2025"})
TSM_NULL = {"data": None, "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


def test_fund_fetches_the_summary_market_cap_for_names_outside_the_nasdaq_100(env):
    put_list(env, pipe.OBS_HOLDINGS_SPY, [("JPM", 1.4), ("BRK.B", 1.5), ("BRK.A", 0.1), ("NVDA", 7.9)])
    put_list(env, pipe.OBS_HOLDINGS_SOXX, [("TSM", 3.0)], "2026-09-30")
    env.store.put_obs(pipe.OBS_HOLDINGS_NDX, {"rows": [["NVDA", "NVIDIA", None, 4.2e12]]}, data_date="2026-10-16")
    env.http.fin.update({"JPM": JPM_QUARTERS, "BRK.B": BRK_QUARTERS, "TSM": TSM_NULL})
    env.http.summary.update({"JPM": summary("JPM", "903,039,014,165"), "BRK.B": summary("BRK/B", "1,173,801,775,599")})
    assert run(env, "us.fund") == "ok"
    assert sorted(env.http.requested("summary")) == ["BRK.B", "JPM"]  # NVDA 用名單的；TSM 沒有季報就不查
    assert env.http.query("summary", "JPM")[0] == {"assetclass": "stocks"}
    for symbol, cap in (("JPM", 903039014165.0), ("BRK.B", 1173801775599.0)):
        payload = obs(env, pipe.OBS_FUND_PREFIX + symbol)["payload"]
        assert payload["market_cap_usd"] == cap and payload["market_cap_source"] == "nasdaq_summary"
        quarters = [(date.fromisoformat(end), value) for end, value in payload["quarters"]]
        result = market_calc.eps_metrics(quarters, payload["market_cap_usd"])  # market_publish 的算法
        assert result.status == "ok" and 13.5 < result.pe < 14.5  # 單位對得上：市值美元、淨利千美元
    tsm = obs(env, pipe.OBS_FUND_PREFIX + "TSM")
    assert tsm["payload"] is None and tsm["fail_streak"] == 0  # ADR 沒有季報：本益比暫缺，不算失敗
    assert env.store.task_state("us.fund")["fail_streak"] == 0


def test_fund_summary_without_a_market_cap_leaves_pe_missing(env):
    put_list(env, pipe.OBS_HOLDINGS_SPY, [("JPM", 1.4)])
    env.http.fin["JPM"] = JPM_QUARTERS
    env.http.summary["JPM"] = summary("JPM", "N/A")
    assert run(env, "us.fund") == "ok"
    payload = obs(env, pipe.OBS_FUND_PREFIX + "JPM")["payload"]
    assert payload["market_cap_usd"] is None and payload["market_cap_source"] is None
    quarters = [(date.fromisoformat(end), value) for end, value in payload["quarters"]]
    assert market_calc.eps_metrics(quarters, payload["market_cap_usd"]).status == "no_market_cap"


def test_fund_summary_failure_is_recorded_and_retried(env):
    put_list(env, pipe.OBS_HOLDINGS_SPY, [("JPM", 1.4), ("LLY", 1.3)])
    env.http.fin.update({"JPM": JPM_QUARTERS, "LLY": JPM_QUARTERS})
    env.http.summary.update({"JPM": summary("JPMX", "903,039,014,165"), "LLY": summary("LLY", "1,183,314,071,417")})
    assert run(env, "us.fund") == "partial"  # JPM 的回應代號對不上：那一檔失敗，其他照存
    jpm = obs(env, pipe.OBS_FUND_PREFIX + "JPM")
    assert jpm["payload"] is None and jpm["fail_streak"] == 1 and jpm["error"] == "format"
    assert obs(env, pipe.OBS_FUND_PREFIX + "LLY")["payload"]["market_cap_usd"] == 1183314071417.0
    assert TASKS["us.fund"].due(ctx_at(env, NOW + 60))  # 失敗的這週還會再試


# ── Nasdaq summary 市值（market_sources_us 新增的解析） ──────────────────────────


@pytest.mark.parametrize("payload, expect, cap", [
    (summary("JPM", "903,039,014,165"), "JPM", 903039014165.0),
    (summary("BRK/B", "1,173,801,775,599"), "BRK.B", 1173801775599.0),  # 回應用「/」分股別
    (summary("TSM", "2,140,003,788,439"), "TSM", 2140003788439.0),
    (summary("JPM", "$1,000"), "JPM", 1000.0),
    (summary("JPM", "N/A"), "JPM", None),  # 沒有就是 None，不是 0
    (summary("JPM", ""), "JPM", None),
    (summary("JPM", "0"), "JPM", None),
    (summary("JPM", None), "JPM", None),  # 沒有 MarketCap 這欄
    (summary("JPM", "903,039,014,165"), None, 903039014165.0),  # 不比代號
])
def test_parse_summary_market_cap(payload, expect, cap):
    assert src.parse_nasdaq_summary_market_cap(json.dumps(payload).encode(), expect_symbol=expect) == cap


@pytest.mark.parametrize("payload, expect, kind", [
    (summary("JPMX", "903,039,014,165"), "JPM", "format"),  # 代號對不上
    (summary("BRK/A", "1,173,801,775,599"), "BRK.B", "format"),
    (summary("JPM", "903B"), "JPM", "format"),
    (summary("JPM", "12.5%"), "JPM", "format"),
    (summary("JPM", "-5,000"), "JPM", "format"),
    (summary("JPM", "1,23,456"), "JPM", "format"),  # 千分位不對
    (summary("JPM", 903039014165), "JPM", "format"),  # 不是字串
    (summary("JPM", "999,999,999,999,999"), "JPM", "format"),  # 大到不合理
    (nasdaq({"symbol": "JPM", "summaryData": {"MarketCap": "903,039,014,165"}}), "JPM", "format"),
    (nasdaq({"symbol": "JPM", "summaryData": ["x"]}), "JPM", "format"),
    (nasdaq({"symbol": "JPM"}), "JPM", "empty"),
    (TSM_NULL, "TSM", "empty"),
    ({"status": {}}, "JPM", "format"),
])
def test_parse_summary_market_cap_errors(payload, expect, kind):
    with pytest.raises(market_calc.ParseError) as caught:
        src.parse_nasdaq_summary_market_cap(json.dumps(payload).encode(), expect_symbol=expect)
    assert caught.value.kind == kind


def test_summary_empty_shell_is_empty_not_a_symbol_mismatch():
    """2026-10-23：和日線同一個順序問題——代號、summaryData 都是 null 的空殼先判 empty（以前是 format）；
    代號是 null 卻有 summaryData 還是 format（認不出是哪一家）。"""
    def kind(payload, expect):
        with pytest.raises(market_calc.ParseError) as caught:
            src.parse_nasdaq_summary_market_cap(json.dumps(payload).encode(), expect_symbol=expect)
        return caught.value.kind

    assert kind(nasdaq({"symbol": None, "summaryData": None}), "BF.B") == "empty"
    assert kind(nasdaq({"symbol": None, "summaryData": {"MarketCap": {"value": "1,000"}}}), "JPM") == "format"
    assert kind(summary("JPMX", "903,039,014,165"), "JPM") == "format"


def test_fetch_summary_market_cap_url_endpoint_and_ledger():
    calls = []

    class Http:
        def fetch(self, url, **kwargs):
            calls.append((url, kwargs))
            return market_http.Response(200, json.dumps(summary("BRK/B", "1,173,801,775,599")).encode(), {}, url)

    ledger = object()
    assert src.fetch_nasdaq_summary_market_cap("BRK.B", http=Http(), ledger=ledger) == 1173801775599.0
    endpoint = src.ENDPOINTS["nasdaq_summary"]
    assert calls == [("https://api.nasdaq.com/api/quote/BRK.B/summary?assetclass=stocks",
                      {"max_bytes": endpoint.max_bytes, "timeout": endpoint.timeout, "accept": endpoint.accept,
                       "ledger": ledger})]
    assert endpoint.host == "api.nasdaq.com" and endpoint.per_day >= 30 and endpoint.timeout == 15.0
    with pytest.raises(market_calc.ParseError):
        src.fetch_nasdaq_summary_market_cap("brk/b", http=Http(), ledger=ledger)  # 不是 canonical：不發請求
    assert len(calls) == 1


# ── 美債、CFTC、FINRA、事件 ──────────────────────────────────────────────────


def test_treasury_rows_and_not_yet_when_d_is_missing(env):
    env.http.treasury["202610"] = TREASURY_HEADER + "\n10/15/2026,4.17,4.28,4.77,5.10,5.43\n10/14/2026,4.11,4.28,4.82,5.19,5.52\n"
    env.http.treasury["202609"] = TREASURY_HEADER + "\n09/30/2026,4.10,4.20,4.70,5.00,5.40\n"
    assert run(env, "us.ust") == "not_yet"  # 10/16 還沒公布
    got = obs(env, pipe.OBS_UST)
    assert got["data_date"] == "2026-10-15" and got["payload"]["rows"][0] == ["2026-09-30", 4.2, 4.7, 5.0, 5.4]
    assert env.store.task_state("us.ust")["fail_streak"] == 0
    env.http.treasury["202610"] = TREASURY_HEADER + "\n10/16/2026,4.12,4.26,4.74,5.09,5.43\n" + \
        env.http.treasury["202610"].split("\n", 1)[1]
    assert run(env, "us.ust", NOW + 3600) == "ok"
    assert obs(env, pipe.OBS_UST)["data_date"] == "2026-10-16"
    assert env.http.requested("treasury").count("202609") == 1  # 有前一列之後不再抓上個月


def cftc_row(code: str, report: date, lev_l: int = 100, lev_s: int = 60) -> src.CftcRow:
    return src.CftcRow(market="E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE <b>x</b>", report_date=report, oi=1000,
                       dealer_l=1, dealer_s=2, am_l=3, am_s=4, lev_l=lev_l, lev_s=lev_s, other_l=5, other_s=6,
                       code=code, chg_lev_l=10, chg_lev_s=4)


def test_cftc_stores_fixed_labels_and_waits_for_the_friday_release(env, monkeypatch):
    report = {"value": date(2026, 10, 13)}

    def fake_fetch(*, http, ledger):
        assert http is env.http and ledger is env.ledger
        return [cftc_row("13874+", report["value"]), cftc_row("20974+", report["value"], 50, 70)]

    monkeypatch.setattr(src, "fetch_cftc_tff", fake_fetch)
    saturday = at(2026, 10, 17, 9, 0)
    assert run(env, "us.cftc", saturday) == "ok"
    es = obs(env, pipe.OBS_CFTC["es"])
    assert es["data_date"] == "2026-10-13"
    assert "market" not in es["payload"] and es["payload"]["label"] == "S&P 500" and es["payload"]["code"] == "13874+"
    assert es["payload"]["basis"] == "consolidated" and es["payload"]["lev_net"] == 40
    assert obs(env, pipe.OBS_CFTC["nq"])["payload"]["lev_net_change"] == 6
    next_friday = at(2026, 10, 23, 16, 0)
    assert run(env, "us.cftc", next_friday) == "not_yet"  # 新的一週還沒出來
    report["value"] = date(2026, 10, 20)
    assert run(env, "us.cftc", next_friday + 6 * 3600) == "ok"
    assert obs(env, pipe.OBS_CFTC["es"])["data_date"] == "2026-10-20"


def test_finra_month_end_anchor_and_not_yet(env, monkeypatch):
    monkeypatch.setattr(src, "fetch_finra_margin", lambda *, http, ledger: [
        src.MarginRow(date(2025, 9, 1), 1100000), src.MarginRow(date(2026, 8, 1), 1450000)])
    assert run(env, "us.finra") == "not_yet"  # 10/16：期待 9 月
    got = obs(env, pipe.OBS_MARGIN)
    assert got["period"] == "2026-08" and got["data_date"] == "2026-08-31"
    monkeypatch.setattr(src, "fetch_finra_margin", lambda *, http, ledger: [
        src.MarginRow(date(2025, 9, 1), 1100000), src.MarginRow(date(2026, 9, 1), 1480054)])
    assert run(env, "us.finra", NOW + 13 * 3600) == "ok"
    got = obs(env, pipe.OBS_MARGIN)
    assert got["period"] == "2026-09" and got["data_date"] == "2026-09-30"
    assert got["payload"]["rows"] == [["2025-09", 1100000.0], ["2026-09", 1480054.0]]


def test_events_fomc_econ_and_earnings(env):
    env.http.fomc = fomc_html()
    assert run(env, "us.events.fomc") == "ok"
    meetings = obs(env, pipe.OBS_EVENTS_FOMC)["payload"]["meetings"]
    october = next(item for item in meetings if item["start"] == "2026-10-27")
    assert october["decision_utc"] == "2026-10-28T18:00:00+00:00" and meetings[1]["sep"] is True
    env.http.econ["2026-10-20"] = [{"gmt": "08:30", "country": "United States", "eventName": "CPI"},
                                   {"gmt": "09:00", "country": "Germany", "eventName": "German WPI"}]
    assert run(env, "us.events.econ") == "ok"  # 週三的 MBA 由假 HTTP 預設補上
    assert len(env.http.requested("econ")) == 8  # 7 天＋先驗一次週三
    days = obs(env, pipe.OBS_EVENTS_ECON)["payload"]["days"]
    assert days["2026-10-20"] == [["2026-10-20T12:30:00+00:00", "CPI"]] and days["2026-10-16"] == []
    put_list(env, pipe.OBS_HOLDINGS_SOXX, [("NVDA", 9.5)], "2026-09-30")
    assert run(env, "us.events.earnings") == "ok"
    assert obs(env, pipe.OBS_EVENTS_EARNINGS)["payload"]["symbols"] == {
        "NVDA": {"date": "2026-11-18", "estimated": True}}


def test_econ_window_is_dropped_when_the_date_mapping_changes(env):
    """2026-10-18 審查 r6b I1：Nasdaq 哪天不再位移（date=X 回 X 當天）→ 週三的請求看不到 MBA → 整批不寫、任務失敗。"""
    env.http.econ["2026-10-21"] = [{"gmt": "07:00", "country": "United States", "eventName": "MBA Mortgage Applications"}]
    env.http.econ["2026-10-20"] = [{"gmt": "08:30", "country": "United States", "eventName": "CPI"}]
    shifted = {(date.fromisoformat(k) - timedelta(days=1)).isoformat(): v for k, v in env.http.econ.items()}
    env.http.econ = shifted  # 假 HTTP 照「date=X 回 X−1」回：鍵往前一天＝模擬 Nasdaq 不再位移
    env.http.wednesday_mba = False
    assert run(env, "us.events.econ") != "ok"
    assert obs(env, pipe.OBS_EVENTS_ECON) is None  # 一天都沒寫


# ── 小工具 ───────────────────────────────────────────────────────────────────


def test_merge_top10_ignores_bad_rows_and_breaks_ties_by_symbol():
    rows = [["GOOG", "C", 1.0, None], ["GOOGL", "A", 1.0, None], ["bad!", "x", 9.0, None], ["MSFT", "M", None, None]]
    assert pipe.merge_top10(rows) == [{"name": "C", "symbols": ["GOOG", "GOOGL"], "weight": 2.0, "pick": "GOOG"}]


def test_looks_unadjusted_against_the_split_factor():
    split = (D, 3.0, 2.0)  # 3:2 分割：沒調整時價格變成 2/3
    assert pipe._looks_unadjusted(90.0, 60.5, split)
    assert not pipe._looks_unadjusted(90.0, 91.0, split)
    assert not pipe._looks_unadjusted(90.0, 60.0, (D, 1.02, 1.0))  # 2% 股票股利：分不出來，不判斷


# ── 整輪 collect（框架的 run_collect）：每個任務都跑得起來、沒有 unexpected ─────────────


def test_full_collect_round_on_a_fresh_store(env, monkeypatch):
    http = env.http
    http.market_info = market_info()
    ndx_rows = [{"symbol": "AAPL", "companyName": "Apple", "marketCap": "5,000,000"},
                {"symbol": "GOOGL", "companyName": "Alphabet A", "marketCap": "2,000,000"},
                {"symbol": "GOOG", "companyName": "Alphabet C", "marketCap": "1,900,000"}]
    ndx_rows += [{"symbol": f"N{chr(65 + i // 26)}{chr(65 + i % 26)}", "companyName": f"N {i}", "marketCap": "1,000"}
                 for i in range(97)]
    http.ndx = nasdaq({"totalrecords": 100, "limit": 101, "offset": 0, "date": "Oct 16, 2026 12:34 PM",
                       "data": {"asOf": None, "headers": {}, "rows": ndx_rows}})
    http.ssga = ssga_xlsx(SPY_NAMED)
    etfs_ready(env, price=710.0)  # 和 CBOE 期權鏈的 717.23 差不多：現價離正式收盤太遠的不存（r9b gp2 O1）
    http.prices["AAPL"] = flat(sessions(D, 250), 300.0)
    for symbol in pipe.OPTION_SYMBOLS:
        http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T16:15:00")
    for symbol in pipe.VIX_SYMBOLS:
        http.quotes[symbol] = cboe_quote(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T16:15:01")
    http.treasury["202610"] = TREASURY_HEADER + "\n10/16/2026,4.12,4.26,4.74,5.09,5.43\n10/15/2026,4.17,4.28,4.77,5.10,5.43\n"
    soxx = [(f"{s}.US", (s, "4.00")) for s in ("NVDA", "AVGO", "AMD", "QCOM", "TXN", "MU", "INTC", "AMAT", "LRCX",
                                                "KLAC", "ADI", "MRVL", "NXPI", "MCHP", "ON", "MPWR", "TER", "ENTG",
                                                "SWKS", "QRVO")]
    http.moneydj["SOXX"] = moneydj_html("SOXX", soxx)
    http.moneydj["QQQ"] = moneydj_html("QQQ", [(f"Q{chr(65 + i // 26)}{chr(65 + i % 26)}.US", (f"Q {i}", "1.00"))
                                               for i in range(95)])
    http.fomc = fomc_html()
    monkeypatch.setattr(src, "fetch_cftc_tff", lambda *, http, ledger: [cftc_row("13874+", date(2026, 10, 13)),
                                                                        cftc_row("20974+", date(2026, 10, 13))])
    monkeypatch.setattr(src, "fetch_finra_margin", lambda *, http, ledger: [src.MarginRow(date(2026, 9, 1), 1480054)])
    outcome = market_tasks.run_collect(ctx_at(env, NOW), pipe.TASKS)
    results = outcome["results"]
    assert set(results) == set(TASKS) and outcome["stopped"] is None
    assert not [name for name, kind in results.items() if kind.startswith("unexpected")], results
    assert results["us.hist"] == "ok" and results["us.options"] == "ok" and results["us.13f"] == "ok"
    assert results["us.fund"] == "ok"  # 不在名單的前十大另查 summary 市值
    assert "JPM" in http.requested("summary") and "AAPL" not in http.requested("summary")
    assert results["us.hist.backfill"] in ("ok", "partial")  # 新成分一年歷史：沒有價格的那些是「還沒有 D」
    for obs_id in (pipe.OBS_HOLDINGS_SPY, pipe.OBS_HOLDINGS_NDX, pipe.OBS_HOLDINGS_SOXX, pipe.OBS_HOLDINGS_QQQ,
                   pipe.OBS_UNIVERSE, pipe.OBS_HIST_STATUS, pipe.OBS_OPTIONS["SPY"], pipe.OBS_QUOTE["VIX"], pipe.OBS_UST,
                   pipe.OBS_CFTC["es"], pipe.OBS_MARGIN, pipe.OBS_TOP10["QQQ"], pipe.OBS_EVENTS_FOMC,
                   pipe.OBS_EVENTS_ECON, pipe.OBS_EVENTS_EARNINGS, pipe.OBS_CALENDAR_CHECK):
        assert obs(env, obs_id)["payload"] is not None, obs_id
    assert obs(env, pipe.OBS_13F_PREFIX + "NVDA")["payload"]["own_pct"] == pytest.approx(79.53)
    assert D in closes(env, "AAPL") and len(http.requested("hist")) <= pipe.HIST_MAX_SYMBOLS_PER_RUN
    assert len(env.ledger.reserved) == len(http.calls)  # 每一次請求都先在帳本預扣
    again = market_tasks.run_collect(ctx_at(env, NOW + 900), pipe.TASKS)["results"]
    assert again["us.options"] == "not_due" and again["us.13f"] == "not_due"  # 這個期間已經做完


@pytest.mark.parametrize("day, now_et", [
    (date(2026, 11, 2), (2026, 11, 2, 19, 0)),  # 11/1 起冬令：16:00 EST 收盤，19:00 EST＝D 換日後 2.5 小時
    (date(2026, 10, 30), (2026, 10, 30, 19, 0)),  # 還是夏令
    (date(2026, 11, 27), (2026, 11, 27, 16, 0)),  # 早收 13:00：D 換日 13:30
    (date(2026, 12, 24), (2026, 12, 24, 16, 0)),
    (date(2026, 3, 9), (2026, 3, 9, 19, 0)),
])
def test_since_d_change_follows_the_calendar_close(env, day, now_et):
    """2026-10-24 審查 r9b gp1 nit 4：VOO 備援探針看「D 換日（收盤＋30 分鐘）後多久」，夏令／冬令、早收日都照日曆。"""
    ctx = ctx_at(env, at(*now_et))
    assert pipe.current_d(ctx) == day
    assert pipe._since_d_change(ctx, day) == pytest.approx(2.5 * 3600, abs=1.0)


def test_since_d_change_across_the_dst_weekend_and_unknown_days(env):
    ctx = ctx_at(env, at(2026, 11, 2, 8, 0))  # 週一 08:00 EST，D 還是週五 10/30（EDT）
    assert pipe.current_d(ctx) == date(2026, 10, 30)
    assert pipe._since_d_change(ctx, date(2026, 10, 30)) == pytest.approx(
        at(2026, 11, 2, 8, 0) - at(2026, 10, 30, 16, 30), abs=1.0)
    assert pipe._since_d_change(ctx, date(2026, 11, 1)) == 0.0  # 不是交易日
    assert pipe._since_d_change(ctx, date(2030, 1, 2)) == 0.0  # 日曆表外


def test_gate_round_requests_the_backup_probe_once(env):
    """2026-10-24 審查 r9b gp1 nit 1：VOO 在備援探針裡剛 miss，同一輪 SPY 公布了 D；_open_gate 清掉 VOO 的重排後
    「其他 ETF」又排了它一次＝同一輪打兩次。"""
    etfs_ready(env, with_d=False)
    env.http.hist_raw["VOO"] = {"data": None, "message": None, "status": {
        "rCode": 400, "bCodeMessage": [{"code": 2001, "errorMessage": "Error while calling vendor"}],
        "developerMessage": None}}
    rounds = []
    for step in range(16):  # 17:00 ET 起每 15 分鐘；D 換日 16:30，第 120 分鐘起 VOO 也探
        if step == 10:  # 第 150 分鐘 SPY／QQQ／SOXX 公布 D
            for symbol in ("SPY", "QQQ", "SOXX"):
                env.http.prices[symbol] = flat(HISTORY + [D], 100.0)
        before = len(env.http.calls)
        run(env, "us.hist", NOW + step * 900)
        rounds.append([key for kind, key, _ in env.http.calls[before:] if kind == "hist"])
    gate = next(keys for keys in rounds if "QQQ" in keys)
    assert gate.count("VOO") == 1, gate
    assert D in closes(env, "QQQ") and D in closes(env, "SOXX")


def test_options_spot_far_from_the_official_close_is_not_stored(env):
    """2026-10-24 審查 r9b gp2 O1：期權鏈的 current_price 只要 >0 就收，卡上就印「現價 7.02」「現價 70,200」，牆距和
    GEX 也全跟著錯。現價和最近一筆正式收盤差到跳空門檻就不存（卡片退回上一份）、記成格式錯；收盤正常的話暫定收盤照寫。"""
    for symbol in pipe.OPTION_SYMBOLS:
        seed(env, symbol, flat(HISTORY, 700.0))
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T16:15:00",
                                             close=702.0)
    env.http.chains["QQQ"]["data"]["current_price"] = 7.02
    env.http.chains["SOXX"]["data"]["current_price"] = 70200.0
    assert run(env, "us.options", NOW + 600) == "partial"
    for symbol in ("QQQ", "SOXX"):
        got = obs(env, pipe.OBS_OPTIONS[symbol])
        assert got["payload"] is None and got["error"] == "format", symbol
        assert closes(env, symbol)[D] == (702.0, "cboe", True), symbol  # 收盤有自己的守門：照寫暫定值
    spy = pipe.chain_from_payload(obs(env, pipe.OBS_OPTIONS["SPY"])["payload"])
    assert spy.spot == 702.0


def test_options_spot_without_an_official_close_is_checked_against_the_file_close(env):
    """還沒有正式收盤可比（剛建庫）時和同一份檔的 close 比：一樣擋得住 current_price 單獨壞掉。"""
    for symbol in pipe.OPTION_SYMBOLS:
        env.http.chains[symbol] = cboe_chain(symbol, timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T16:15:00")
    env.http.chains["SPY"]["data"]["current_price"] = 1e-9
    assert run(env, "us.options") == "partial"
    assert obs(env, pipe.OBS_OPTIONS["SPY"])["payload"] is None
    assert obs(env, pipe.OBS_OPTIONS["QQQ"])["payload"] is not None


@pytest.mark.parametrize("close", [1e-9, 0.5, 7000.0])
def test_vix_out_of_range_is_not_stored(env, close):
    """2026-10-24 審查 r9b gp2 O1：VIX 收盤 1e-9 原本照存，卡上印「恐慌指數（VIX）0.00」。"""
    for symbol in pipe.VIX_SYMBOLS:
        env.http.quotes[symbol] = cboe_quote(symbol, timestamp="2026-10-16 21:37:16", last_trade="2026-10-16T16:15:01",
                                             close=close)
    run(env, "us.vix", datetime.combine(D, datetime.min.time(), ET).timestamp() + 18 * 3600)
    got = obs(env, pipe.OBS_QUOTE["VIX"])
    assert got["payload"] is None and got["error"] == "format"
