"""機構動向第一期 F 軌（資金流＋傳輸，2026-10-31）的測試：market_sources_inst、market_pipeline_inst、
market_pipeline_us._filing_season、market_http 白名單、market_store 的主機規則、job 的 PIPELINES。

用真的 market_store.Store（暫存目錄）、真的 market_calendar；網路換成假的 http，回應是 .py 裡合成的最小結構
（iShares、元大只留解析需要的標籤；SSGA 的 xlsx 用 openpyxl 現場產），數字與日期都是假的。帳本換成假的。
時間一律注入（ctx.now、假的 monotonic clock）。不連網、不碰正式 state。
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import openpyxl
import pytest

import market_calc
import market_calendar
import market_http
import market_paths
import market_pipeline_inst as pipe
import market_pipeline_us
import market_sources_inst as src
import market_store
import market_tasks

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")
TASKS = {task.name: task for task in pipe.TASKS}

D = date(2026, 10, 27)  # 週二
NOW = datetime(2026, 10, 27, 20, 0, tzinfo=ET).timestamp()  # 美東收盤＋30 分鐘之後：D＝10/27
TW_DAY = date(2026, 10, 28)  # 週三
TW_NOW = datetime(2026, 10, 28, 18, 0, tzinfo=TPE).timestamp()  # 台北 17:00 之後：目標日＝10/28

CONTRACT_KEYS = {
    "v", "symbol", "source", "source_url", "unit", "shares_out", "shares_out_as_of", "prev_shares_out", "prev_as_of",
    "change", "official_prev_session", "nav", "nav_as_of", "flow", "method", "derived", "flag", "change_5d", "days",
}
DAY_KEYS = {"as_of", "shares_out", "nav", "flag"}  # 2026-10-31 Phase 6 I-1：days 每一筆加自己的 flag（只增欄位）
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def at(year, month, day, hour=0, minute=0, tz=ET) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


def evening(day: date) -> float:
    """那個美股交易日的美東 20:00（D＝那天）。"""
    return at(day.year, day.month, day.day, 20, 0)


def mon_d_y(day: date) -> str:
    return f"{_MONTHS[day.month - 1]} {day.day}, {day.year}"


def dmy(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def us_sessions(end: date, n: int) -> list[date]:
    """≤end 的最近 n 個平日（扣 NYSE 表的休市日），新到舊；假資料用，不靠被測的日曆。"""
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5 and day not in market_calendar.NYSE_HOLIDAYS:
            out.append(day)
        day -= timedelta(days=1)
    return out


# ── 合成的回應（只留解析需要的結構；數字、日期都是假的） ─────────────────────────


def nav_card(nav: str, day: date) -> str:
    """iShares 的 NAV 卡：標籤「NAV as of」、日期、兩個 $（一個給螢幕閱讀器）、數字。"""
    return (
        '<div data-id="fundHeader-row-navAmount"><dt><span>NAV as of</span>'
        f'<span>{mon_d_y(day)}</span></dt><dd><span><span class="sr-only">$</span>'
        f'<span aria-hidden="true">$</span>{nav}</span><span>52 WK: 100.00 - 600.00</span></dd></div>'
    )


def ishares_html(shares: Optional[str] = "90,000,000", shares_day: date = D, nav: Optional[str] = "500.00",
                 nav_day: Optional[date] = None, *, copies: int = 2, title: str = "iShares Semiconductor ETF | SOXX",
                 extra: str = "") -> bytes:
    """iShares 產品頁的骨架：NAV 卡（真的頁面有兩份一樣的）、「1 Day NAV Change」（不能被當成 NAV）、Key Facts 的
    Shares Outstanding（數字和 as of 分在不同標籤、中間夾著 <!-- -->）、頁面上的 JSON 標籤字串、script 裡的假值。"""
    nav_day = shares_day if nav_day is None else nav_day
    card = nav_card(nav, nav_day) if nav is not None else ""
    change_card = (f'<div><dt><span>1 Day NAV Change as of</span><span>{mon_d_y(nav_day)}</span></dt>'
                   '<dd><span>$</span>7.25</dd></div>')
    shares_row = ""
    if shares is not None:
        shares_row = (
            '<div role="listitem" data-id="keyFundFacts-row-sharesOutstanding"><div><span>Shares Outstanding</span>'
            f'</div><div><span>{shares}</span></div><div>as of<!-- --> <!-- -->{mon_d_y(shares_day)}</div></div>'
        )
    body = "".join([
        "<!DOCTYPE html><html><head>",
        f"<title>{title}</title>",
        '<script type="application/ld+json">{"name":"NAV as of","value":"999.99",'
        '"text":"Shares Outstanding 1 as of Jan 2, 2026"}</script>',
        "<style>.x{content:'NAV as of Jan 2, 2026 $ 1.00'}</style>",
        "</head><body>",
        (card + change_card) * copies,
        '<div><span>Bloomberg Index Ticker</span><span>EXAMPLE</span></div>',
        shares_row,
        '<div hidden>{"label.navdata.sharesoutstanding":[{"metadata":null,"text":"Shares Outstanding"}]}</div>',
        extra,
        "</body></html>",
    ])
    return body.encode("utf-8")


def yuanta_html(units: Optional[str] = "23,000,000,000", diff: Optional[str] = "20,000,000",
                nav: Optional[str] = "NTD 100.00", nav_day: Optional[str] = "2026/10/28",
                trade_day: Optional[str] = "2026/10/28", *,
                title: str = "(0050)元大台灣卓越50證券投資信託基金 - 元大申購買回清單", extra: str = "") -> bytes:
    """元大申購買回清單頁的骨架：一排 div.td（h5 日期、h4 欄名、p 值），股票實物申贖那段的「交易日期」，
    下面一張用 div 排的成分股表（td 裡沒有 h4）。"""
    cells = []
    if units is not None:
        cells.append(f'<div class="td"><h4>已發行受益權單位總數</h4> <p>{units}</p></div>')
    if diff is not None:
        cells.append(f'<div class="td"><h4>與前日已發行單位差異數</h4> <p>{diff}</p></div>')
    if nav is not None:
        cells.append(f'<div class="td"><h5>{nav_day or ""}</h5> <h4>\n          每受益權單位淨資產價值(元)\n        </h4>'
                     f' <p>{nav}</p></div>')
    cells.append('<div class="td"><h4>每實物申購單位之受益權單位數</h4> <p>500,000</p></div>')
    trade = ""
    if trade_day is not None:
        trade = (f'<div class="trandate"><div>交易日期: <br class="d-lg-none">{trade_day}</div></div>')
    body = "".join([
        f"<html><head><meta charset=\"utf-8\"><title>{title}</title></head><body>",
        '<div class="dateBox"><p>上傳時間：<span>2026-10-28 16:30:00</span></p></div>',
        '<div class="each_table"><div class="totalNum"><h4>基金淨資產價值</h4> <p>NTD 2,300,000,000,000</p></div>',
        '<p class="ann-date">公告日期：<span>2026/10/29</span></p>',
        '<div class="tdList">', "".join(cells), "</div></div>",
        '<div><div class="tt"><h3>股票實物申贖</h3>', trade, "</div>",
        '<div class="table"><div class="tr"><div class="td">股票代碼</div><div class="td">股數</div></div>'
        '<div class="tr"><div class="td">1234</div><div class="td">1000</div></div></div></div>',
        extra,
        "<script>window.__NUXT__={osunit:1,issuesdiff:2}</script>",
        "</body></html>",
    ])
    return body.encode("utf-8")


SPY_DAYS = us_sessions(D, 12)  # 10/27、10/26、10/23 … 10/12（新到舊）
SPY_ROWS = list(zip(SPY_DAYS, (
    (700.50, 1_000_000_000), (698.25, 1_000_000_000), (695.00, 1_003_000_000), (697.10, 1_001_000_000),
    (701.00, 1_002_000_000), (699.80, 1_005_000_000), (698.00, 1_004_000_000), (696.40, 1_000_500_000),
    (694.90, 999_500_000), (693.30, 999_000_000), (690.00, 998_500_000), (688.00, 998_000_000),
)))


def spy_rows(end: date, n: int = 12) -> list:
    """到 end 為止的 SSGA 列（新到舊）；NAV 每天不同（不會被當成「發行商還沒更新」）。"""
    return [(day, 600.0 + (day.toordinal() % 50) * 1.5, 1_000_000_000 + (day.toordinal() % 7) * 100_000)
            for day in us_sessions(end, n)]


def ssga_xlsx(rows: Optional[list] = None, *, ticker: str = "SPY",
              header: tuple = ("Date", "NAV", "Shares Outstanding", "Total Net Assets"), date_text=dmy) -> bytes:
    """SSGA navhist 的骨架：基金名稱、代號、空列、表頭、新到舊的日資料、空列、免責聲明。
    rows＝[(日期, NAV, 在外股數)]；格子可以直接給字串（"-" 之類）。"""
    rows = [(day, nav, shares) for day, (nav, shares) in SPY_ROWS] if rows is None else rows
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "navhist"
    sheet.append(["Fund Name:", "Example S&P 500 Fund"])
    sheet.append(["Ticker Symbol:", ticker])
    sheet.append([None])
    sheet.append(list(header))
    for day, nav, shares in rows:
        total = nav * shares if isinstance(nav, float) and isinstance(shares, int) else "-"
        sheet.append([date_text(day) if isinstance(day, date) else day, nav, shares, total])
    sheet.append([None])
    sheet.append(["Past performance is not a reliable indicator of future performance."])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ── 假的帳本、時鐘、http ─────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.wall = NOW

    def __call__(self) -> float:
        return self.t


class FakeLedger:
    """market_http.Ledger：記下預扣；可設某主機第 N 次起說額度不夠；403／429 之後冷卻。"""

    def __init__(self) -> None:
        self.reserved: list[str] = []
        self.cooling: dict[str, float] = {}
        self.cap: dict[str, int] = {}

    def reserve(self, host: str) -> None:
        if host in self.cooling:
            raise market_http.FetchError("cooldown")
        if host in self.cap and self.reserved.count(host) >= self.cap[host]:
            raise market_http.FetchError("budget")
        self.reserved.append(host)

    def cooldown(self, host: str, seconds: float) -> None:
        self.cooling[host] = seconds


CONTENT_TYPES = {
    "www.ishares.com": "text/html;charset=UTF-8",
    "www.yuantaetfs.com": "text/html; charset=utf-8",
    "www.ssga.com": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class FakeHttp:
    """假的 market_http：網址要完全等於白名單允許的那一條（host_of 過得去），先預扣帳本（和真的一樣），
    每次都檢查帳本就是 ctx.ledger；依網址回合成的回應或丟 FetchError。"""

    FetchError = market_http.FetchError

    def __init__(self, ledger: Any) -> None:
        self.ledger = ledger
        self.calls: list[dict] = []
        self.bodies: dict[str, bytes] = {}
        self.fail: dict[str, str] = {}

    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        assert ledger is self.ledger, "every fetch must get ctx.ledger"
        host = market_http.host_of(url)
        assert host is not None, url
        ledger.reserve(host)
        self.calls.append({"url": url, "max_bytes": max_bytes, "timeout": timeout, "accept": accept})
        kind = self.fail.get(url)
        if kind:
            if kind in ("blocked", "rate_limited"):
                ledger.cooldown(host, 7200.0)
            raise market_http.FetchError(kind)
        body = self.bodies.get(url)
        if body is None:
            raise market_http.FetchError("status")
        assert len(body) <= max_bytes
        return market_http.Response(200, body, {"content-type": CONTENT_TYPES[host]}, url)


@dataclass
class Env:
    paths: market_paths.Paths
    store: market_store.Store
    clock: FakeClock
    ledger: FakeLedger
    http: FakeHttp


@pytest.fixture
def env(tmp_path: Path):
    paths = market_paths.Paths.under(tmp_path)
    paths.state_dir.mkdir(mode=0o700)
    clock = FakeClock()
    store = market_store.Store(paths.history_db, now_fn=lambda: clock.wall)
    ledger = FakeLedger()
    yield Env(paths, store, clock, ledger, FakeHttp(ledger))
    store.close()


def ctx_at(env: Env, now: float, *, tw_cal: Any = None) -> market_tasks.Context:
    env.clock.wall = now
    return market_tasks.Context(
        store=env.store, http=env.http, ledger=env.ledger, now=now, paths=env.paths,
        deadline=env.clock() + 600.0, us_cal=market_calendar.us_calendar(env.store),
        tw_cal=tw_cal if tw_cal is not None else market_calendar.Calendar("tw"), clock=env.clock,
    )


def run(env: Env, name: str, now: float = NOW, **kwargs: Any) -> str:
    """經過框架跑一個任務（含 due、例外記帳）；回框架記下的結果種類。"""
    return market_tasks.run_task(ctx_at(env, now, **kwargs), TASKS[name])


def due(env: Env, name: str, now: float = NOW, **kwargs: Any) -> bool:
    return bool(TASKS[name].due(ctx_at(env, now, **kwargs)))


def obs(env: Env, symbol: str) -> Optional[dict]:
    return env.store.get_obs(pipe.OBS_FLOWS[symbol])


def payload(env: Env, symbol: str) -> Optional[dict]:
    got = obs(env, symbol)
    return got["payload"] if got else None


def sched(env: Env, key: str) -> dict:
    return env.store.task_state(market_pipeline_us.STATE_PREFIX + key)


def feed_soxx(env: Env, **kwargs: Any) -> None:
    env.http.bodies[src.ISHARES_URL] = ishares_html(**kwargs)


def feed_spy(env: Env, rows: Optional[list] = None, **kwargs: Any) -> None:
    env.http.bodies[src.SSGA_NAVHIST_URL] = ssga_xlsx(rows, **kwargs)


def feed_0050(env: Env, **kwargs: Any) -> None:
    env.http.bodies[src.YUANTA_URL] = yuanta_html(**kwargs)


def store_soxx(env: Env, day: date, shares: str, nav: str = "500.00") -> None:
    """先跑一次 us.flows 存下那天的 SOXX（SPY 一起給、當天就完成）。"""
    feed_soxx(env, shares=shares, shares_day=day, nav=nav)
    feed_spy(env, spy_rows(day))
    assert run(env, "us.flows", evening(day)) == "ok"
    assert payload(env, "SOXX")["shares_out_as_of"] == day.isoformat()


# ── 解析：iShares ────────────────────────────────────────────────────────────


def test_ishares_page_gives_shares_and_the_same_day_nav():
    page = src.parse_ishares(ishares_html())
    assert page.symbol == "SOXX" and page.official_change is None
    assert page.days == (src.FlowDay(D, 90_000_000, 500.0),)
    assert isinstance(page.days[0].shares_out, int)


def test_ishares_ignores_scripts_styles_json_labels_and_the_nav_change_card():
    """script（JSON-LD）、style 裡的假值、頁面上的 JSON 標籤字串、「1 Day NAV Change」都不能被當成資料。"""
    page = src.parse_ishares(ishares_html(nav="512.34", copies=1))
    assert page.days[0].nav == 512.34 and page.days[0].shares_out == 90_000_000


@pytest.mark.parametrize("extra", [
    "",  # 0 個（下面把 shares 拿掉）
    '<div><span>Shares Outstanding</span><span>1,000</span><div>as of Oct 27, 2026</div></div>',  # 第 2 個
    '<div><span>Shares Outstanding</span><span>90,000,000</span><div>as of Oct 27, 2026</div></div>',  # 一樣的也不行
])
def test_ishares_shares_outstanding_must_match_exactly_once(extra):
    html = ishares_html(shares=None) if not extra else ishares_html(extra=extra)
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_ishares(html)
    assert err.value.kind == "format"


def test_ishares_nav_from_another_date_is_null():
    page = src.parse_ishares(ishares_html(nav_day=date(2026, 10, 26)))
    assert page.days == (src.FlowDay(D, 90_000_000, None),)


def test_ishares_nav_copies_that_disagree_or_a_missing_nav_give_null():
    assert src.parse_ishares(ishares_html(copies=1, extra=nav_card("501.00", D))).days[0].nav is None
    assert src.parse_ishares(ishares_html(copies=1, extra=nav_card("500.00", D))).days[0].nav == 500.0  # 一樣的兩份
    assert src.parse_ishares(ishares_html(nav=None)).days[0].nav is None


@pytest.mark.parametrize("shares", ["-90,000,000", "90,000,000.5", "0"])
def test_ishares_shares_must_be_a_positive_whole_number(shares):
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_ishares(ishares_html(shares=shares))
    assert err.value.kind == "format"


def test_ishares_other_fund_page_is_a_format_error():
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_ishares(ishares_html(title="iShares Core S&amp;P 500 ETF | IVV"))
    assert err.value.kind == "format"


def test_ishares_bad_date_is_a_date_error():
    html = ishares_html().replace(b"as of<!-- --> <!-- -->Oct 27, 2026", b"as of<!-- --> <!-- -->Oct 32, 2026")
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_ishares(html)
    assert err.value.kind == "date"


# ── 解析：SSGA navhist ───────────────────────────────────────────────────────


def test_ssga_navhist_gives_the_latest_row_and_nine_before_it():
    page = src.parse_ssga_navhist(ssga_xlsx())
    assert page.symbol == "SPY" and page.official_change is None
    assert len(page.days) == 10
    assert page.days[0] == src.FlowDay(D, 1_000_000_000, 700.5)
    assert [day.as_of for day in page.days] == SPY_DAYS[:10]
    assert all(isinstance(day.shares_out, int) for day in page.days)


def test_ssga_accepts_real_dates_and_text_numbers():
    rows = [(datetime(2026, 10, 27), "700.50", "1,000,000,000"), (date(2026, 10, 26), 698.25, 999_000_000.0)]
    page = src.parse_ssga_navhist(ssga_xlsx(rows, date_text=lambda day: day))
    assert page.days == (src.FlowDay(D, 1_000_000_000, 700.5), src.FlowDay(date(2026, 10, 26), 999_000_000, 698.25))


@pytest.mark.parametrize("kwargs", [
    {"ticker": "VOO"},
    {"header": ("Date", "NAV", "Shares", "Total Net Assets")},
    {"header": ("Date", "NAV", "Shares Outstanding")},
])
def test_ssga_needs_the_spy_ticker_and_the_four_column_header(kwargs):
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_ssga_navhist(ssga_xlsx(**kwargs))
    assert err.value.kind == "format"


def test_ssga_dates_must_go_back_in_time():
    rows = [(date(2026, 10, 26), 698.25, 999_000_000), (D, 700.5, 1_000_000_000)]
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_ssga_navhist(ssga_xlsx(rows))
    assert err.value.kind == "conflict"


@pytest.mark.parametrize("first", [(D, "-", "-"), (D, 700.5, "-"), ("27/10/2026", 700.5, 1_000_000_000),
                                   (D, -1.0, 1_000_000_000), (D, 700.5, 1_000_000_000.5)])
def test_ssga_the_latest_row_must_be_complete(first):
    with pytest.raises(market_calc.ParseError):
        src.parse_ssga_navhist(ssga_xlsx([first, (date(2026, 10, 26), 698.25, 999_000_000)]))


def test_ssga_stops_at_the_first_incomplete_older_row():
    rows = [(D, 700.5, 1_000_000_000), (date(2026, 10, 26), 698.25, 999_000_000), (date(2026, 10, 23), 695.0, "-"),
            (date(2026, 10, 22), 697.1, 998_000_000)]
    page = src.parse_ssga_navhist(ssga_xlsx(rows))
    assert [day.as_of for day in page.days] == [D, date(2026, 10, 26)]


def test_ssga_not_a_workbook_is_a_format_error():
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_ssga_navhist(b"<html>Moved Permanently</html>")
    assert err.value.kind == "format"


# ── 解析：元大 0050 ──────────────────────────────────────────────────────────


def test_yuanta_page_gives_units_the_official_change_and_the_base_date():
    page = src.parse_yuanta_pcf(yuanta_html())
    assert page.symbol == "0050"
    assert page.days == (src.FlowDay(TW_DAY, 23_000_000_000, 100.0),)
    assert page.official_change == 20_000_000 and isinstance(page.official_change, int)


@pytest.mark.parametrize("text, value", [("-20,000,000", -20_000_000), ("−20,000,000", -20_000_000),
                                         ("－20,000,000", -20_000_000), ("(20,000,000)", -20_000_000),
                                         ("+20,000,000", 20_000_000), ("0", 0)])
def test_yuanta_net_redemption_is_a_negative_official_change(text, value):
    assert src.parse_yuanta_pcf(yuanta_html(diff=text)).official_change == value


@pytest.mark.parametrize("kwargs, kind", [
    ({"units": None}, "format"),
    ({"diff": None}, "format"),
    ({"extra": '<div class="td"><h4>已發行受益權單位總數</h4> <p>1,000</p></div>'}, "format"),  # 欄名出現兩次
    ({"units": "-23,000,000,000"}, "format"),
    ({"units": "23,000,000,000.5"}, "format"),
    ({"diff": "N/A"}, "format"),
    ({"title": "(0056)元大高股息證券投資信託基金 - 元大申購買回清單"}, "format"),
    ({"trade_day": None, "nav_day": None}, "date"),
    # 兩個不同的交易日期：不知道單位數是哪天的（淨值日期和交易日期不同只丟 NAV，見下一個測試；Phase 6 審查 R1-6）
    ({"extra": '<div class="trandate"><div>交易日期: 2026/10/27</div></div>'}, "conflict"),
    ({"trade_day": "2026/13/01"}, "date"),
])
def test_yuanta_strict_rules(kwargs, kind):
    with pytest.raises(market_calc.ParseError) as err:
        src.parse_yuanta_pcf(yuanta_html(**kwargs))
    assert err.value.kind == kind


def test_yuanta_nav_is_optional_and_needs_its_own_date():
    assert src.parse_yuanta_pcf(yuanta_html(nav=None)).days == (src.FlowDay(TW_DAY, 23_000_000_000, None),)
    assert src.parse_yuanta_pcf(yuanta_html(nav_day=None)).days[0].nav is None  # 沒印日期：不能確定是同一天
    # 交易日期缺了，用淨值那格的日期當基準日
    assert src.parse_yuanta_pcf(yuanta_html(trade_day=None)).days == (src.FlowDay(TW_DAY, 23_000_000_000, 100.0),)
    assert src.parse_yuanta_pcf(yuanta_html(nav="NTD --")).days[0].nav is None
    # 淨值日期和交易日期不同：以交易日期為準、只丟 NAV（和 iShares 一致；以前整頁 conflict，Phase 6 審查 R1-6、R3-9）
    page = src.parse_yuanta_pcf(yuanta_html(trade_day="2026/10/27"))
    assert page.days == (src.FlowDay(date(2026, 10, 27), 23_000_000_000, None),) and page.official_change == 20_000_000


# ── 抓取 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("symbol, body", [("SOXX", ishares_html()), ("SPY", ssga_xlsx()), ("0050", yuanta_html())])
def test_fetch_uses_the_fixed_url_the_ledger_and_the_endpoint_limits(symbol, body):
    ledger = FakeLedger()
    http = FakeHttp(ledger)
    endpoint = src.ENDPOINTS[symbol]
    http.bodies[endpoint.url] = body
    page = src.fetch_flow(symbol, http=http, ledger=ledger)
    assert page.symbol == symbol
    assert http.calls == [{"url": endpoint.url, "max_bytes": endpoint.max_bytes, "timeout": endpoint.timeout,
                           "accept": endpoint.accept}]
    assert ledger.reserved == [endpoint.host]


def test_endpoint_table_matches_the_spec():
    assert src.ISHARES_URL == "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf"
    assert src.YUANTA_URL == "https://www.yuantaetfs.com/tradeInfo/pcf/0050"
    assert src.SSGA_NAVHIST_URL == "https://www.ssga.com/library-content/products/fund-data/etfs/us/navhist-us-en-spy.xlsx"
    assert src.ENDPOINTS["0050"].max_bytes == 2 * 1024 * 1024
    assert {symbol: (e.source, e.unit) for symbol, e in src.ENDPOINTS.items()} == {
        "SOXX": ("iShares", "USD"), "SPY": ("SSGA", "USD"), "0050": ("元大", "TWD")}
    for endpoint in src.ENDPOINTS.values():
        assert market_http.host_of(endpoint.url) == endpoint.host
        assert 0 < endpoint.timeout <= 30.0


class _OddTransport:
    def __init__(self, body: Any) -> None:
        self.body = body

    def fetch(self, url, **kwargs):
        return market_http.Response(200, self.body, {}, url)


@pytest.mark.parametrize("body", [None, "text", b"x" * (2 * 1024 * 1024 + 1)])
def test_a_transport_that_breaks_the_rules_cannot_slip_a_body_through(body):
    with pytest.raises(market_calc.ParseError) as err:
        src.fetch_flow("0050", http=_OddTransport(body), ledger=None)
    assert err.value.kind == "format"


def imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    return names


@pytest.mark.parametrize("name", ["market_sources_inst.py", "market_pipeline_inst.py"])
def test_new_modules_import_only_the_standard_library_and_market_modules(name):
    names = imported_modules(HERE / name)
    outside = {mod for mod in names if mod not in sys.stdlib_module_names and mod != "__future__"
               and not mod.startswith("market_")}
    assert not outside, outside
    assert {"main", "stock_picks", "card_pages", "market_dashboard", "market_publish"}.isdisjoint(names)


# ── 傳輸層白名單、帳本規則、job 的 PIPELINES ─────────────────────────────────


def test_the_two_new_hosts_are_whitelisted_with_one_exact_path_each():
    assert {"www.ishares.com", "www.yuantaetfs.com"} <= market_http.ALLOWED_HOSTS
    assert market_http.ALLOWED_PATHS["www.ishares.com"] == ("/us/products/239705/ishares-phlx-semiconductor-etf",)
    assert market_http.ALLOWED_PATHS["www.yuantaetfs.com"] == ("/tradeInfo/pcf/0050",)
    assert market_http.ALLOWED_PATHS["ocmacro.com"] == ("/dashboard/trump",)  # 原本那條不動
    assert "www.ssga.com" not in market_http.ALLOWED_PATHS  # SSGA 維持不限路徑
    assert set(market_http.ALLOWED_PATHS) <= market_http.ALLOWED_HOSTS
    assert market_http.host_of(src.ISHARES_URL) == "www.ishares.com"
    assert market_http.host_of(src.YUANTA_URL) == "www.yuantaetfs.com"
    assert market_http.host_of(src.SSGA_NAVHIST_URL) == "www.ssga.com"
    assert market_http.host_of(market_pipeline_us.src.SSGA_SPY_URL) == "www.ssga.com"


@pytest.mark.parametrize("url", [
    "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf/1467271812596.ajax",
    "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf?fileType=csv",
    "https://www.ishares.com/us/products/239705/ishares-semiconductor-etf",
    "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf",
    "https://www.yuantaetfs.com/tradeInfo/pcf/0056",
    "https://www.yuantaetfs.com/tradeInfo/pcf/0050?date=20261027",
    "https://www.yuantaetfs.com/tradeInfo/pcf/0050/",
    "http://www.yuantaetfs.com/tradeInfo/pcf/0050",
])
def test_other_paths_on_the_new_hosts_are_refused_without_a_request(url):
    assert market_http.host_of(url) is None
    ledger = FakeLedger()
    with pytest.raises(market_http.FetchError) as err:
        market_http.fetch(url, max_bytes=1024, ledger=ledger)
    assert err.value.kind == "host" and ledger.reserved == []


def test_the_new_hosts_have_their_own_ledger_rules():
    assert market_store.HOST_RULES["www.ishares.com"] == market_store.HostRule(10.0, 8)
    assert market_store.HOST_RULES["www.yuantaetfs.com"] == market_store.HostRule(10.0, 8)
    assert market_store.HOST_RULES["ocmacro.com"] == market_store.HostRule(10.0, 8)  # 原本的不動


def test_the_tasks_are_registered_in_the_low_group_of_their_market():
    names = [task.name for task in market_tasks.registered()]
    assert "us.flows" in names and "tw.flows" in names
    assert names.index("us.13f") < names.index("us.flows")  # market_pipeline_us 先登記
    assert (TASKS["us.flows"].group, TASKS["us.flows"].market) == ("low", "us")
    assert (TASKS["tw.flows"].group, TASKS["tw.flows"].market) == ("low", "tw")
    assert all(0 < task.budget_s <= 95 for task in pipe.TASKS)
    assert pipe.US_ITEMS == ("SOXX", "SPY") and pipe.TW_ITEMS == ("0050",)
    assert pipe.OBS_FLOWS == {"SOXX": "us.flows.etf.SOXX", "SPY": "us.flows.etf.SPY", "0050": "tw.flows.etf.0050"}
    assert pipe.CHECK_THRESHOLD == {"SOXX": 0.20, "SPY": 0.20, "0050": 0.20}
    assert (pipe.MAX_NOT_YET, pipe.NOT_YET_RETRY_S) == (6, 7200.0)


def test_the_job_loads_the_inst_pipeline_and_a_broken_one_stops_only_its_own_tasks():
    from jobs import market_snapshot as job

    assert "market_pipeline_inst" in job.PIPELINES
    assert job.PIPELINES.index("market_pipeline_us") < job.PIPELINES.index("market_pipeline_inst")
    assert job.PIPELINES[-1] == "market_pipeline_infl"  # 通膨還是排最後（test_market_pipeline_infl 靠這個）
    assert "market_pipeline_inst.py" in {path.name for path in job.code_files()}
    code = (
        "import sys, json; sys.modules['market_sources_inst'] = None\n"
        "import market_tasks\n"
        "status = market_tasks.load_modules(['market_pipeline_us', 'market_pipeline_inst'])\n"
        "names = [t.name for t in market_tasks.registered()]\n"
        "print(json.dumps({'status': status, 'hist': 'us.hist' in names, 'flows': 'us.flows' in names}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["status"]["market_pipeline_us"] == "ok"
    assert got["status"]["market_pipeline_inst"].startswith("import_error")
    assert got["hist"] is True and got["flows"] is False


# ── 13F 申報期窗口 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("day, expected", [
    (date(2026, 12, 31), False), (date(2027, 1, 1), True), (date(2027, 2, 20), True), (date(2027, 2, 21), False),
    (date(2028, 2, 29), False), (date(2028, 2, 20), True), (date(2026, 3, 31), False), (date(2026, 4, 1), True),
    (date(2026, 5, 20), True), (date(2026, 5, 21), False), (date(2026, 6, 30), False), (date(2026, 7, 1), True),
    (date(2026, 8, 20), True), (date(2026, 8, 21), False), (date(2026, 9, 30), False), (date(2026, 10, 1), True),
    (date(2026, 10, 31), True), (date(2026, 11, 20), True), (date(2026, 11, 21), False), (date(2026, 12, 1), False),
])
def test_filing_season_runs_from_the_day_after_quarter_end_to_the_twentieth(day, expected):
    assert market_pipeline_us._filing_season(day) is expected


def test_inside_the_new_window_13f_is_a_daily_period():
    ctx = market_tasks.Context(store=None, http=None, ledger=None, now=at(2026, 10, 9, 12, 0),
                               paths=market_paths.Paths.under(Path("/nonexistent")), deadline=0.0,
                               us_cal=market_calendar.Calendar("us"), tw_cal=market_calendar.Calendar("tw"))
    assert market_pipeline_us._13f_period(ctx) == "2026-10-09"  # 以前 10 月初是 ISO 週
    ctx.now = at(2026, 12, 9, 12, 0)
    assert market_pipeline_us._13f_period(ctx) == "2026-W50"  # 窗口外照舊每週一次


# ── us.flows：SOXX（iShares）、SPY（SSGA） ───────────────────────────────────


def test_a_first_soxx_snapshot_has_no_previous_change_or_flow(env):
    feed_soxx(env)
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    got = obs(env, "SOXX")
    assert (got["data_date"], got["period"], got["fail_streak"]) == ("2026-10-27", None, 0)
    assert got["payload"] == {
        "v": 1, "symbol": "SOXX", "source": "iShares", "source_url": src.ISHARES_URL, "unit": "USD",
        "shares_out": 90_000_000, "shares_out_as_of": "2026-10-27", "prev_shares_out": None, "prev_as_of": None,
        "change": None, "official_prev_session": None, "nav": 500.0, "nav_as_of": "2026-10-27", "flow": None,
        "method": "Δshares×NAV", "derived": True, "flag": None, "change_5d": None,
        "days": [{"as_of": "2026-10-27", "shares_out": 90_000_000, "nav": 500.0, "flag": None}],
    }
    assert env.http.urls() == [src.ISHARES_URL, src.SSGA_NAVHIST_URL]  # 卡片要的 SOXX 先抓
    assert not due(env, "us.flows", NOW + 3600)  # 這個 D 兩項都做完了


def test_spy_takes_previous_day_change_flow_and_five_day_change_from_the_file(env):
    feed_soxx(env)
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    got = obs(env, "SPY")
    assert got["data_date"] == "2026-10-27"
    p = got["payload"]
    assert (p["source"], p["unit"], p["source_url"]) == ("SSGA", "USD", src.SSGA_NAVHIST_URL)
    assert (p["shares_out"], p["prev_shares_out"], p["prev_as_of"]) == (1_000_000_000, 1_000_000_000, "2026-10-26")
    assert p["change"] == 0 and p["flow"] == 0.0 and p["flag"] is None  # 股數沒變是真的 0，不是缺值
    assert (p["nav"], p["nav_as_of"]) == (700.5, "2026-10-27")
    assert p["change_5d"] == {"from_as_of": "2026-10-20", "to_as_of": "2026-10-27", "change": -5_000_000}
    assert [day["as_of"] for day in p["days"]] == [day.isoformat() for day in SPY_DAYS[:10]]
    assert p["days"][2] == {"as_of": "2026-10-23", "shares_out": 1_003_000_000, "nav": 695.0, "flag": None}


def test_the_next_day_gives_the_change_and_the_estimated_flow(env):
    store_soxx(env, date(2026, 10, 26), "89,500,000", "495.50")
    feed_soxx(env, shares="90,000,000", nav="500.00")
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    p = payload(env, "SOXX")
    assert (p["prev_shares_out"], p["prev_as_of"], p["change"]) == (89_500_000, "2026-10-26", 500_000)
    assert p["flow"] == 250_000_000.0 and p["flag"] is None
    assert p["days"] == [{"as_of": "2026-10-27", "shares_out": 90_000_000, "nav": 500.0, "flag": None},
                         {"as_of": "2026-10-26", "shares_out": 89_500_000, "nav": 495.5, "flag": None}]


def test_a_net_redemption_day_is_negative(env):
    store_soxx(env, date(2026, 10, 26), "90,000,000")
    feed_soxx(env, shares="89,000,000", nav="500.00")
    feed_spy(env)
    run(env, "us.flows")
    p = payload(env, "SOXX")
    assert p["change"] == -1_000_000 and p["flow"] == -500_000_000.0 and p["flag"] is None


def test_a_missed_day_keeps_both_dates(env):
    store_soxx(env, date(2026, 10, 23), "89,000,000")  # 週五；10/26 沒抓到
    feed_soxx(env, shares="90,000,000")
    feed_spy(env)
    run(env, "us.flows")
    p = payload(env, "SOXX")
    assert (p["prev_as_of"], p["shares_out_as_of"], p["change"]) == ("2026-10-23", "2026-10-27", 1_000_000)


def test_older_than_d_is_stored_if_newer_then_retried_every_two_hours_at_most_six_times(env):
    feed_spy(env)
    feed_soxx(env, shares="89,500,000", shares_day=date(2026, 10, 26))  # 發行商還沒換到 D
    assert run(env, "us.flows") == "partial"  # SPY 做完、SOXX 還沒
    first = obs(env, "SOXX")
    assert first["data_date"] == "2026-10-26" and first["payload"]["shares_out"] == 89_500_000
    assert sched(env, "us.flows")["wait"]["SOXX"] == [1, NOW + 7200.0]
    assert not due(env, "us.flows", NOW + 7199)
    assert due(env, "us.flows", NOW + 7200)
    for n in range(2, 7):  # 同一份頁面：不重寫（只更新 checked_at）、不算失敗
        now = NOW + 7200.0 * (n - 1)
        assert run(env, "us.flows", now) == "not_yet"
        got = obs(env, "SOXX")
        assert got["payload"] == first["payload"] and got["last_success_at"] == first["last_success_at"]
        assert got["checked_at"] == now and got["fail_streak"] == 0
    assert sched(env, "us.flows")["wait"]["SOXX"][0] == 6
    assert not due(env, "us.flows", NOW + 7200.0 * 7)  # 這個 D 用完 6 次
    assert len([url for url in env.http.urls() if url == src.ISHARES_URL]) == 6
    feed_soxx(env)  # 下一個交易日：新的期間
    assert due(env, "us.flows", evening(date(2026, 10, 28)))


def test_the_same_page_again_writes_nothing(env):
    feed_soxx(env)
    feed_spy(env)
    run(env, "us.flows")
    first = obs(env, "SOXX")
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})  # 期間狀態不見了也不會重寫
    assert run(env, "us.flows", NOW + 60) == "ok"
    again = obs(env, "SOXX")
    assert again["payload"] == first["payload"] and again["last_success_at"] == first["last_success_at"]
    assert again["checked_at"] == NOW + 60


def test_newer_date_with_identical_shares_and_nav_is_not_updated_and_nothing_is_written(env):
    store_soxx(env, date(2026, 10, 26), "89,500,000", "495.50")
    before = obs(env, "SOXX")
    feed_soxx(env, shares="89,500,000", nav="495.50")  # 日期換成 10/27，數字一模一樣：發行商還沒更新
    feed_spy(env)
    assert run(env, "us.flows") == "partial"
    got = obs(env, "SOXX")
    assert got["payload"] == before["payload"] and got["data_date"] == "2026-10-26"
    assert got["checked_at"] == NOW and got["last_success_at"] == before["last_success_at"]
    assert sched(env, "us.flows")["wait"]["SOXX"][0] == 1  # 2 小時後再看
    feed_soxx(env, shares="89,500,000", nav="501.00")  # 股數沒變、NAV 變了：真的「股數不變」的一天
    assert run(env, "us.flows", NOW + 7200) == "ok"
    p = payload(env, "SOXX")
    assert (p["shares_out_as_of"], p["change"], p["flow"]) == ("2026-10-27", 0, 0.0)


@pytest.mark.parametrize("shares, flag", [("108,000,001", "check"), ("108,000,000", None), ("71,999,999", "check"),
                                          ("72,000,000", None)])
def test_a_change_over_the_threshold_is_stored_with_check(env, shares, flag):
    store_soxx(env, date(2026, 10, 26), "90,000,000")
    feed_soxx(env, shares=shares)
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    p = payload(env, "SOXX")
    assert p["flag"] == flag
    assert p["shares_out"] == int(shares.replace(",", "")) and p["change"] is not None  # 待核對也照存數字


def test_nav_from_another_day_gives_no_flow_but_keeps_the_change(env):
    store_soxx(env, date(2026, 10, 26), "89,500,000")
    feed_soxx(env, shares="90,000,000", nav="495.50", nav_day=date(2026, 10, 26))
    feed_spy(env)
    assert run(env, "us.flows") == "partial"  # 股數到了、同一天的 NAV 還沒到：寫股數、SOXX 回 not_yet 等 NAV（Phase 6 I-3）
    p = payload(env, "SOXX")
    assert p["change"] == 500_000
    assert (p["nav"], p["nav_as_of"], p["flow"]) == (None, None, None)
    assert p["days"][0] == {"as_of": "2026-10-27", "shares_out": 90_000_000, "nav": None, "flag": None}
    assert sched(env, "us.flows")["wait"]["SOXX"] == [1, NOW + 7200.0]


def test_days_are_new_to_old_at_most_ten_and_one_per_date(env):
    days = list(reversed(us_sessions(D, 12)))  # 舊到新
    for index, day in enumerate(days):
        feed_soxx(env, shares=f"{90_000_000 + index * 100_000:,}", shares_day=day, nav=f"{500 + index}.00")
        feed_spy(env, spy_rows(day))
        assert run(env, "us.flows", evening(day)) == "ok", day
    p = payload(env, "SOXX")
    assert [item["as_of"] for item in p["days"]] == [day.isoformat() for day in us_sessions(D, 10)]
    assert p["days"][0] == {"as_of": "2026-10-27", "shares_out": 91_100_000, "nav": 511.0, "flag": None}
    # 下一個 D 時發行商改了 10/27 的數字（還是 10/27）：比 D 舊但內容變了 → 寫、同日期只留一筆、回 not_yet
    feed_soxx(env, shares="91,200,000", shares_day=D, nav="511.00")
    feed_spy(env, spy_rows(date(2026, 10, 28)))
    assert run(env, "us.flows", evening(date(2026, 10, 28))) == "partial"
    p = payload(env, "SOXX")
    assert len(p["days"]) == 10 and [item["as_of"] for item in p["days"]].count("2026-10-27") == 1
    assert p["days"][0]["shares_out"] == 91_200_000 and p["change"] == 91_200_000 - 91_000_000


def test_the_five_day_change_needs_five_data_days_back(env):
    days = list(reversed(us_sessions(D, 6)))
    for index, day in enumerate(days):
        feed_soxx(env, shares=f"{90_000_000 + index * 100_000:,}", shares_day=day)
        feed_spy(env, spy_rows(day))
        assert run(env, "us.flows", evening(day)) == "ok", day
        p = payload(env, "SOXX")
        if index < 5:
            assert p["change_5d"] is None, day
    assert p["change_5d"] == {"from_as_of": days[0].isoformat(), "to_as_of": "2026-10-27", "change": 500_000}


def test_a_future_date_is_refused_and_counted_as_a_failure(env):
    feed_soxx(env, shares_day=date(2026, 10, 29))
    feed_spy(env)
    assert run(env, "us.flows") == "partial"
    got = obs(env, "SOXX")
    assert got["payload"] is None and got["error"] == "date" and got["fail_streak"] == 1


def test_a_parse_failure_is_recorded_and_tried_three_times_per_period(env):
    feed_soxx(env, shares=None)
    feed_spy(env)
    assert run(env, "us.flows", NOW) == "partial"  # SPY 做完、SOXX 格式錯
    assert run(env, "us.flows", NOW + 60) == "format"  # 只剩 SOXX：整個任務失敗（框架退避 15 分鐘）
    assert run(env, "us.flows", NOW + 120) == "backoff"
    assert run(env, "us.flows", NOW + 960) == "format"
    got = obs(env, "SOXX")
    assert (got["payload"], got["error"], got["fail_streak"]) == (None, "format", 3)
    assert sched(env, "us.flows")["fails"] == {"SOXX": 3}
    assert not due(env, "us.flows", NOW + 4000)  # 這個 D 試滿 3 次就不再試


def test_soxx_goes_first_so_an_ssga_block_cannot_hold_back_the_card(env):
    feed_soxx(env)
    env.http.fail[src.SSGA_NAVHIST_URL] = "rate_limited"
    assert run(env, "us.flows") == "partial"
    assert payload(env, "SOXX")["shares_out_as_of"] == "2026-10-27"
    assert obs(env, "SPY")["error"] == "rate_limited" and "www.ssga.com" in env.ledger.cooling


def test_a_host_off_the_whitelist_skips_only_its_item(env, monkeypatch):
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", market_http.ALLOWED_HOSTS - {"www.ishares.com"})
    feed_soxx(env)
    feed_spy(env)
    assert due(env, "us.flows")
    assert run(env, "us.flows") == "ok"
    assert env.http.urls() == [src.SSGA_NAVHIST_URL] and obs(env, "SOXX") is None
    assert not due(env, "us.flows", NOW + 60)


def test_both_us_hosts_off_the_whitelist_means_never_due(env, monkeypatch):
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS",
                        market_http.ALLOWED_HOSTS - {"www.ishares.com", "www.ssga.com"})
    feed_soxx(env)
    feed_spy(env)
    assert not due(env, "us.flows")
    assert TASKS["us.flows"].run(ctx_at(env, NOW)) == market_tasks.NOT_YET
    assert env.http.calls == [] and env.ledger.reserved == []


def test_the_disabled_flag_file_stops_both_tasks_without_a_request(env):
    feed_soxx(env)
    feed_spy(env)
    feed_0050(env)
    (env.paths.state_dir / "market_inst.disabled").write_text("", encoding="utf-8")
    assert pipe.DISABLED_FLAG == "market_inst.disabled"
    assert not due(env, "us.flows") and not due(env, "tw.flows", TW_NOW)
    assert TASKS["us.flows"].run(ctx_at(env, NOW)) == market_tasks.NOT_YET
    assert TASKS["tw.flows"].run(ctx_at(env, TW_NOW)) == market_tasks.NOT_YET
    assert env.http.calls == []
    (env.paths.state_dir / "market_inst.disabled").unlink()
    assert due(env, "us.flows") and due(env, "tw.flows", TW_NOW)


def test_a_ledger_budget_is_not_a_failure(env):
    feed_soxx(env)
    feed_spy(env)
    env.ledger.cap["www.ishares.com"] = 0
    assert run(env, "us.flows") == "budget"  # 帳本說不行：沒送請求、不算失敗（這批停下）
    assert obs(env, "SOXX") is None and sched(env, "us.flows")["fails"] == {}
    assert env.store.task_state("us.flows")["fail_streak"] == 0 and env.http.calls == []


def test_every_payload_follows_the_contract(env):
    feed_soxx(env)
    feed_spy(env)
    feed_0050(env)
    run(env, "us.flows")
    run(env, "tw.flows", TW_NOW)
    for symbol in ("SOXX", "SPY", "0050"):
        p = payload(env, symbol)
        assert set(p) == CONTRACT_KEYS, symbol
        assert p["v"] == 1 and p["symbol"] == symbol and p["method"] == "Δshares×NAV" and p["derived"] is True
        for key in ("shares_out", "prev_shares_out", "change"):
            assert p[key] is None or (isinstance(p[key], int) and not isinstance(p[key], bool)), (symbol, key)
        for key in ("nav", "flow"):
            assert p[key] is None or isinstance(p[key], float), (symbol, key)
        assert p["flag"] in (None, "check")
        assert 1 <= len(p["days"]) <= 10 and all(set(day) == DAY_KEYS for day in p["days"])
        assert all(day["flag"] in (None, "check") for day in p["days"]) and p["days"][0]["flag"] == p["flag"]
        assert p["official_prev_session"] is None if symbol != "0050" else p["official_prev_session"] == "2026-10-27"
        assert json.loads(json.dumps(p)) == p


# ── tw.flows：0050（元大） ───────────────────────────────────────────────────


def test_0050_uses_the_official_change_and_estimates_the_flow_in_twd(env):
    feed_0050(env)
    assert run(env, "tw.flows", TW_NOW) == "ok"
    got = obs(env, "0050")
    assert got["data_date"] == "2026-10-28"
    assert got["payload"] == {
        "v": 1, "symbol": "0050", "source": "元大", "source_url": src.YUANTA_URL, "unit": "TWD",
        "shares_out": 23_000_000_000, "shares_out_as_of": "2026-10-28", "prev_shares_out": None, "prev_as_of": None,
        "change": 20_000_000, "official_prev_session": "2026-10-27", "nav": 100.0, "nav_as_of": "2026-10-28",
        "flow": 2_000_000_000.0, "method": "Δshares×NAV", "derived": True, "flag": None, "change_5d": None,
        "days": [{"as_of": "2026-10-28", "shares_out": 23_000_000_000, "nav": 100.0, "flag": None}],
    }
    assert not due(env, "tw.flows", TW_NOW + 3600)


def _store_0050(env: Env, day: str, units: str, now: float) -> None:
    feed_0050(env, units=units, diff="0", nav_day=day, trade_day=day)
    assert run(env, "tw.flows", now) == "ok"


@pytest.mark.parametrize("stored_units, flag", [("22,980,000,000", None), ("22,970,000,000", "check")])
def test_0050_own_difference_must_match_the_official_change(env, stored_units, flag):
    _store_0050(env, "2026/10/27", stored_units, at(2026, 10, 27, 18, 0, tz=TPE))
    feed_0050(env)  # 10/28：23,000,000,000，官方差異 +20,000,000
    assert run(env, "tw.flows", TW_NOW) == "ok"
    p = payload(env, "0050")
    assert p["flag"] == flag and p["change"] == 20_000_000  # 對不上也照存官方的數字
    assert (p["prev_shares_out"], p["prev_as_of"]) == (int(stored_units.replace(",", "")), "2026-10-27")


def test_0050_a_split_sized_official_change_is_flagged_even_on_the_first_day(env):
    feed_0050(env, units="23,000,000,000", diff="12,000,000,000")  # 前一日只有 11,000,000,000：+109%
    run(env, "tw.flows", TW_NOW)
    p = payload(env, "0050")
    assert p["flag"] == "check" and p["change"] == 12_000_000_000


def test_0050_net_redemption(env):
    feed_0050(env, diff="-20,000,000")
    run(env, "tw.flows", TW_NOW)
    p = payload(env, "0050")
    assert (p["change"], p["flow"], p["flag"]) == (-20_000_000, -2_000_000_000.0, None)


def test_0050_period_is_the_taipei_session_after_the_publish_time(env):
    feed_0050(env, nav_day="2026/10/27", trade_day="2026/10/27")
    before = at(2026, 10, 28, 16, 0, tz=TPE)  # 10/28 還沒到 17:00：目標日是 10/27
    assert run(env, "tw.flows", before) == "ok"
    assert payload(env, "0050")["shares_out_as_of"] == "2026-10-27"
    assert not due(env, "tw.flows", before + 1800)
    assert due(env, "tw.flows", TW_NOW)  # 17:00 之後：目標日換成 10/28
    assert run(env, "tw.flows", TW_NOW) == "not_yet"  # 頁面還是 10/27
    holiday = market_calendar.Calendar("tw", holidays={TW_DAY: "假日"}, supported_until=date(2026, 12, 31),
                                       supported_from=date(2026, 1, 1))
    assert pipe.tw_target(ctx_at(env, TW_NOW, tw_cal=holiday)) == date(2026, 10, 27)


def test_tw_target_matches_market_pipeline_tw_target_session(env):
    import market_pipeline_tw

    holiday = market_calendar.Calendar("tw", holidays={date(2026, 10, 9): "假日", TW_DAY: "假日"},
                                       supported_until=date(2026, 12, 31), supported_from=date(2026, 1, 1))
    start = at(2026, 10, 5, 0, 0, tz=TPE)
    for cal in (market_calendar.Calendar("tw"), holiday):
        for step in range(0, 24 * 21, 3):
            now = start + step * 3600.0
            now_tpe = datetime.fromtimestamp(now, TPE)
            expected = market_pipeline_tw.target_session(cal, now_tpe, pipe.TW_EARLIEST)
            assert pipe.tw_target(ctx_at(env, now, tw_cal=cal)) == expected, now_tpe
    assert pipe.TW_EARLIEST == time(17, 0)


def test_tw_host_off_the_whitelist_means_never_due(env, monkeypatch):
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", market_http.ALLOWED_HOSTS - {"www.yuantaetfs.com"})
    feed_0050(env)
    assert not due(env, "tw.flows", TW_NOW)
    assert TASKS["tw.flows"].run(ctx_at(env, TW_NOW)) == market_tasks.NOT_YET
    assert env.http.calls == []


# ── 接縫：publish（P 軌）讀的 obs 鍵與欄位＝collect（F 軌、T 軌）寫的 ─────────────────────────


def _publish_reads(receivers: set[str], *, t86: bool) -> set[str]:
    """market_publish 裡所有 `_inst*` 函式（t86＝名字有沒有 "t86"）對 `<receivers>.get("鍵")` 讀的固定鍵；P 軌把讀法拆成
    幾個小函式也照樣找得到。receivers 含 "day" 時，另外收 `(某 dict.get(…) or {}).get("鍵")` 這種讀 days 每一筆的寫法。"""
    import market_publish

    keys: set[str] = set()
    for function in ast.parse(inspect.getsource(market_publish)).body:
        if not (isinstance(function, ast.FunctionDef) and function.name.startswith("_inst")
                and ("t86" in function.name) == t86):
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                continue
            target = node.func.value
            if (isinstance(target, ast.Name) and target.id in receivers) or ("day" in receivers and isinstance(target, ast.BoolOp)):
                keys.add(node.args[0].value)
    return keys


class _T86Result:
    rows = {"2330": (1_234_000, -56_000, 1_000, -2_000), "2317": (-10_000, 20_000, 0, 3_000)}


def test_publish_reads_the_obs_ids_and_fields_the_collectors_write(env):
    """2026-10-31 整合：market_publish 自己再寫一次 obs 鍵（publish 不 import collect 模組），這裡鎖住兩邊一致：
    obs id、上卡的代號、幣別、旗標、門檻與 5 日的天數，以及 publish 讀的每一個欄位都在 F 軌（market_pipeline_inst）與 T 軌
    （market_pipeline_tw.store_t86）真的寫出來的 payload 裡。"""
    import market_pipeline_tw
    import market_publish

    assert market_publish.OBS_US_FLOWS.format("SOXX") == pipe.OBS_FLOWS["SOXX"]
    assert market_publish.OBS_US_FLOWS.format("SPY") == pipe.OBS_FLOWS["SPY"]
    assert market_publish.OBS_TW_FLOWS == pipe.OBS_FLOWS[market_publish.TW_ETF]
    assert market_publish.OBS_TW_T86 == market_pipeline_tw.OBS_T86
    assert set(market_publish.INST_FLOW_SYMBOLS) <= set(pipe.US_ITEMS) and market_publish.TW_ETF in pipe.TW_ITEMS
    assert market_publish._INST_UNITS == {symbol: src.ENDPOINTS[symbol].unit for symbol in market_publish._INST_UNITS}
    assert "check" in market_calc.INST_FLOW_FLAGS  # F 軌寫的旗標只有 null 與 "check"
    # 「近 5 日」兩邊各擋一次（Phase 6 I-1）：門檻與天數要一樣
    assert market_calc.INST_FLOW_CHECK_THRESHOLD == {
        symbol: pipe.CHECK_THRESHOLD[symbol] for symbol in market_calc.INST_FLOW_CHECK_THRESHOLD}
    assert market_publish.INST_SUM_DAYS == pipe.CHANGE_5D_BACK

    days = list(reversed(us_sessions(D, 6)))
    for index, day in enumerate(days):  # SOXX 連續 6 個交易日：change_5d、days 每一筆都有值
        feed_soxx(env, shares=f"{90_000_000 + index * 100_000:,}", shares_day=day)
        feed_spy(env, spy_rows(day))
        run(env, "us.flows", evening(day))
    feed_0050(env)
    run(env, "tw.flows", TW_NOW)
    flows = {symbol: obs(env, symbol) for symbol in ("SOXX", "SPY", "0050")}
    assert flows["SOXX"]["payload"]["change_5d"] and flows["SPY"]["payload"]["change_5d"]
    payload_keys = _publish_reads({"payload"}, t86=False)
    assert {"v", "symbol", "shares_out_as_of"} <= payload_keys  # 抽得到東西（AST 的寫法沒失效）
    for symbol, found in flows.items():
        assert payload_keys <= set(found["payload"]), (symbol, payload_keys - set(found["payload"]))
        assert _publish_reads({"found"}, t86=False) <= set(found), symbol
        assert found["payload"]["v"] == 1 and found["payload"]["symbol"] == symbol
        assert _publish_reads({"item", "first", "day"}, t86=False) <= DAY_KEYS
        assert all(set(item) == DAY_KEYS for item in found["payload"]["days"])
    five_keys = _publish_reads({"five"}, t86=False)
    assert five_keys and five_keys <= set(flows["SOXX"]["payload"]["change_5d"])

    market_pipeline_tw.store_t86(ctx_at(env, TW_NOW), TW_DAY, _T86Result(), ["2330", "2317"], "2026-10-27")
    t86 = env.store.get_obs(market_pipeline_tw.OBS_T86)
    assert t86["data_date"] == TW_DAY.isoformat()
    t86_keys = _publish_reads({"payload"}, t86=True)
    assert {"v", "unit", "trade_date", "rows"} <= t86_keys and t86_keys <= set(t86["payload"])
    assert _publish_reads({"found"}, t86=True) <= set(t86)
    assert t86["payload"]["v"] == 1 and t86["payload"]["unit"] == "股"
    row_keys = _publish_reads({"row"}, t86=True) | set(market_publish._INST_T86_KEYS)
    assert len(row_keys) == 5
    for row in t86["payload"]["rows"]:
        assert row_keys <= set(row), row_keys - set(row)
    entry = t86["payload"]["days"][TW_DAY.isoformat()]
    assert set(entry) == {"2330", "2317"} and entry["2330"] == [1_234_000, -56_000, 1_000, -2_000]  # 順序＝_INST_T86_KEYS


def test_ready_times_flag_file_and_t86_days_match_across_the_modules(env):
    """2026-10-31 整合輪：收集端、publish、bot 不互相 import，就緒時間、旗標檔名、T86 的 5 日天數各自寫了一份，這裡鎖住一致。
    門檻與資金流的 5 日天數在上一個測試，job 的旗標檔名在 test_the_job_status_table_matches_the_collectors。"""
    import market_dashboard
    import market_pipeline_tw
    import market_sources_tw

    # 就緒時間（鍵＝任務名）＝任務開始把當天當目標日的時間：不一樣的話，還沒開始抓就印「最近一筆」，或落後了卻不報
    assert market_calc.INST_READY[pipe.TW_KEY] == ("twse", pipe.TW_EARLIEST)
    t86_endpoint = market_pipeline_tw.T86_ENDPOINT
    t86_earliest = market_sources_tw.SPARE_ENDPOINTS[t86_endpoint].earliest_tpe
    assert market_calc.INST_READY[market_pipeline_tw.TASK_T86] == ("twse", t86_earliest)
    assert market_pipeline_tw._spare_rule(t86_endpoint)[0] == market_pipeline_tw._SPARE_RULES[t86_endpoint][0] == t86_earliest
    # us.flows 沒有自己的時間，換日跟 current_d（收盤＋30 分鐘）走、本來就比就緒時間早：只鎖就緒時間到的時候任務已經換到當天
    calendar_name, ready = market_calc.INST_READY[pipe.US_KEY]
    assert calendar_name == "nyse"
    assert market_pipeline_us.current_d(ctx_at(env, at(D.year, D.month, D.day, ready.hour, ready.minute))) == D

    assert pipe.DISABLED_FLAG == market_pipeline_tw.INST_DISABLED_FLAG == market_dashboard.INST_DISABLED_FLAG
    assert market_pipeline_tw.T86_DAYS == market_calc.INST_SUM_DAYS  # store_t86 留幾天＝publish 的 5 日合計要幾天


# ── Phase 6 審查的修正（2026-10-31，reviews/p6_triage.md I-1～I-4 與 F 軌的 nit） ───────────────────────


def soxx_run(env: Env, day: date, shares: int, nav: float) -> dict:
    """那個美股交易日晚上跑一次 us.flows（SPY 一起給、當天就完成），回 SOXX 的 payload。"""
    feed_soxx(env, shares=f"{shares:,}", shares_day=day, nav=f"{nav:.2f}")
    feed_spy(env, spy_rows(day))
    assert run(env, "us.flows", evening(day)) == "ok", day
    return payload(env, "SOXX")


@pytest.mark.parametrize("factor", [3.0, 0.25])  # 1 拆 3（股數 ×3、NAV ÷3）；4 合 1 反分割（股數 ÷4、NAV ×4）
def test_change_5d_is_null_while_a_split_is_inside_the_window(env, factor):
    """I-1：分割那天標 check；之後 4 個資料日最新一天正常，但窗口還跨著分割 → change_5d 是 null，不印假的申贖；分割那天
    變成 days[5] 之後才又算（兩邊都是分割後的股數）。"""
    days = list(reversed(us_sessions(D, 12)))  # 舊到新：10/12 … 10/27
    split = 6
    results = []
    for index, day in enumerate(days):
        scale = factor if index >= split else 1.0
        shares = int((30_000_000 + index * 100_000) * scale)
        results.append(soxx_run(env, day, shares, (300 + index) / scale))
    assert results[5]["change_5d"] == {"from_as_of": days[0].isoformat(), "to_as_of": days[5].isoformat(),
                                       "change": 500_000}  # 分割前照算
    assert results[split]["flag"] == "check" and results[split]["nav"] is None  # NAV 跳超過 50% 也不當 NAV
    for index in range(split, split + 5):
        assert results[index]["change_5d"] is None, days[index]
    assert all(results[index]["flag"] is None for index in range(split + 1, len(days)))
    last = results[-1]
    assert last["change_5d"] == {"from_as_of": days[split].isoformat(), "to_as_of": days[-1].isoformat(),
                                 "change": last["shares_out"] - int((30_000_000 + split * 100_000) * factor)}
    assert abs(last["change_5d"]["change"]) < 0.2 * last["shares_out"]
    split_entry = next(item for item in last["days"] if item["as_of"] == days[split].isoformat())
    assert split_entry["flag"] == "check"  # 每一筆帶自己的旗標：分割那天一直是 check
    assert results[split + 1]["nav"] is not None  # 分割隔天：前一份沒有 NAV，不比，照收


def test_change_5d_is_null_around_one_wrong_day(env):
    """I-1：某天把股數讀錯（9,000 萬讀成 90 萬）：那天和隔天都是 check；那天在 days[0..5] 裡的期間 change_5d 都是 null
    （當成 days[5] 的那天也是：分母是錯的數字）；再過一天才又算。"""
    days = list(reversed(us_sessions(date(2026, 10, 28), 13)))  # 舊到新：10/12 … 10/28
    wrong = 6
    results = []
    for index, day in enumerate(days):
        shares = 900_000 if index == wrong else 90_000_000 + index * 100_000
        results.append(soxx_run(env, day, shares, 500.0 + index))
    assert results[5]["change_5d"] is not None
    assert results[wrong]["flag"] == "check" and results[wrong + 1]["flag"] == "check"
    assert [results[index]["change_5d"] for index in range(wrong, wrong + 6)] == [None] * 6
    assert results[wrong + 6]["change_5d"] == {"from_as_of": days[wrong + 1].isoformat(),
                                               "to_as_of": days[wrong + 6].isoformat(), "change": 500_000}
    flags = {item["as_of"]: item["flag"] for item in results[-1]["days"]}
    assert flags[days[wrong].isoformat()] == "check" and flags[days[wrong + 1].isoformat()] == "check"
    assert [flag for day, flag in flags.items() if day not in (days[wrong].isoformat(), days[wrong + 1].isoformat())] \
        == [None] * 8


@pytest.mark.parametrize("step, wrong_shares", [
    (100_000, 124_000_000),  # 複審 R2-1-5 的例子：回程 -19.1%、5 日 -18.8%（分母是讀錯的 1.24 億），都過得了舊門檻
    (800_000, 125_984_000),  # 每天真的 +0.8%：終點追上來，5 日只差 19.3%（用較小的終點當分母也沒超過），要靠相鄰那段
])
def test_change_5d_is_null_when_a_day_read_twenty_odd_percent_high_is_the_start(env, step, wrong_shares):
    """複審 R2-1-5：讀錯那天當起點時它自己的 flag 不看（分割要靠這點），所以門檻的分母要用較小的那筆，否則卡上印假的「近 5 日」。"""
    days = list(reversed(us_sessions(date(2026, 11, 12), 9)))  # 舊到新：11/2 … 11/12
    wrong = 2
    results = []
    for index, day in enumerate(days):
        shares = wrong_shares if index == wrong else 100_000_000 + index * step
        results.append(soxx_run(env, day, shares, 500.0 + index))
    assert results[wrong]["flag"] == "check" and results[wrong + 1]["flag"] is None  # 卡上的 flag 沒有動
    assert results[wrong + 5]["change_5d"] is None
    assert results[wrong + 6]["change_5d"] == {"from_as_of": days[wrong + 1].isoformat(),
                                               "to_as_of": days[wrong + 6].isoformat(), "change": 5 * step}


def test_change_5d_needs_days_5_to_be_exactly_five_sessions_back(env):
    """I-1：漏抓了好幾天（10/22、10/23）：days[5] 不是 5 個交易日前 → null；補滿連續 6 個交易日才又算。"""
    seen = [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20),
            date(2026, 10, 21), date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29),
            date(2026, 10, 30), date(2026, 11, 2)]
    results = {day: soxx_run(env, day, 90_000_000 + index * 100_000, 500.0 + index) for index, day in enumerate(seen)}
    assert results[date(2026, 10, 21)]["change_5d"] is not None  # 10/14→10/21：剛好 5 個交易日
    for day in (date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)):
        assert results[day]["change_5d"] is None, day  # 窗口裡缺 10/22、10/23
    assert results[date(2026, 11, 2)]["change_5d"] == {"from_as_of": "2026-10-26", "to_as_of": "2026-11-02",
                                                        "change": 500_000}
    assert results[date(2026, 10, 26)]["prev_as_of"] == "2026-10-21"  # 當日變化照實印兩個日期（不變）


def test_spy_rows_from_the_file_get_their_own_flags_and_a_jump_blocks_change_5d(env):
    """I-1：SPY 的列來自檔案、從來沒當過「最新」，每一列照它和前一列的變化標 flag；窗口裡有跳動 → change_5d 是 null。"""
    rows = [(day, nav, shares * (3 if day <= date(2026, 10, 23) else 1)) for day, nav, shares in spy_rows(D)]
    feed_soxx(env)
    feed_spy(env, rows)
    run(env, "us.flows")
    p = payload(env, "SPY")
    flags = {item["as_of"]: item["flag"] for item in p["days"]}
    assert flags["2026-10-26"] == "check" and p["flag"] is None  # 10/23→10/26 掉到三分之一
    assert [flag for day, flag in flags.items() if day != "2026-10-26"] == [None] * 9
    assert p["change_5d"] is None


def test_change_5d_of_0050_counts_taiwan_sessions(env):
    """I-1：0050 用台股日曆數 5 個交易日；10/26 是台股假日（美股照開）。台股日曆知道這天休市時算得出來，當平日的日曆
    （休市表還沒抓到）就對不上、給 null。official_prev_session 也跳過假日。"""
    holiday = market_calendar.Calendar("tw", holidays={date(2026, 10, 26): "假日"}, supported_until=date(2026, 12, 31),
                                       supported_from=date(2026, 1, 1))
    days = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 27),
            date(2026, 10, 28)]
    units = {day: 23_000_000_000 + index * 5_000_000 for index, day in enumerate(days)}

    results = {}
    for index, day in enumerate(days):
        prev = units[days[index - 1]] if index else units[day] - 5_000_000
        shown = f"{day:%Y/%m/%d}"
        feed_0050(env, units=f"{units[day]:,}", diff=f"{units[day] - prev:,}", nav_day=shown, trade_day=shown)
        assert run(env, "tw.flows", at(day.year, day.month, day.day, 18, 0, tz=TPE), tw_cal=holiday) == "ok", day
        results[day] = payload(env, "0050")
    p = results[days[-1]]
    assert p["change_5d"] == {"from_as_of": "2026-10-20", "to_as_of": "2026-10-28", "change": 25_000_000}
    assert p["official_prev_session"] == "2026-10-27" and p["flag"] is None
    after = results[date(2026, 10, 27)]  # 10/26 休市、10/24–25 週末：起點是 10/23，已存的前一份也是 10/23 → 照常比對
    assert (after["official_prev_session"], after["prev_as_of"], after["flag"]) == ("2026-10-23", "2026-10-23", None)


def test_0050_change_5d_is_null_when_the_weekday_calendar_counts_the_holiday(env):
    days = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 27),
            date(2026, 10, 28)]
    for index, day in enumerate(days):
        shown = f"{day:%Y/%m/%d}"
        feed_0050(env, units=f"{23_000_000_000 + index * 5_000_000:,}", diff="5,000,000", nav_day=shown, trade_day=shown)
        run(env, "tw.flows", at(day.year, day.month, day.day, 18, 0, tz=TPE))
    p = payload(env, "0050")
    assert p["change_5d"] is None  # 平日日曆把 10/26 當交易日：10/20 不是 5 個交易日前


def test_0050_a_missed_day_is_not_a_check(env):
    """I-2：已存的是 10/26，10/27 沒抓到，10/28 的官方差異數只說 10/27→10/28：兩個日期中間不只一天，不能拿來比、不標 check。"""
    _store_0050(env, "2026/10/26", "22,950,000,000", at(2026, 10, 26, 18, 0, tz=TPE))
    feed_0050(env)  # 10/28：23,000,000,000，官方 +20,000,000（自算 10/26→10/28 是 +50,000,000）
    assert run(env, "tw.flows", TW_NOW) == "ok"
    p = payload(env, "0050")
    assert (p["flag"], p["change"], p["prev_as_of"], p["official_prev_session"]) == (
        None, 20_000_000, "2026-10-26", "2026-10-27")


def test_0050_first_day_has_the_official_starting_session(env):
    """I-2：第一天沒有前一份，但官方差異數的起點已知：official_prev_session＝前一個台股交易日（卡片第一天就用它）。"""
    feed_0050(env)
    run(env, "tw.flows", TW_NOW)
    p = payload(env, "0050")
    assert (p["prev_as_of"], p["official_prev_session"], p["change"]) == (None, "2026-10-27", 20_000_000)


def test_a_same_day_refetch_without_nav_keeps_the_stored_nav_and_flow(env):
    """I-3：同一天重抓、頁面這次沒有同一天的 NAV → 沿用已存的 NAV 與金額，不蓋成 null；股數改了照寫，金額用已存的 NAV 重算。"""
    store_soxx(env, date(2026, 10, 26), "89,500,000", "495.50")
    feed_soxx(env, shares="90,000,000", nav="500.00")
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    first = obs(env, "SOXX")
    assert first["payload"]["flow"] == 250_000_000.0
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})
    feed_soxx(env, shares="90,000,000", nav="501.00", nav_day=date(2026, 10, 28))  # NAV 卡已經換到別天
    assert run(env, "us.flows", NOW + 60) == "ok"
    again = obs(env, "SOXX")
    assert again["payload"] == first["payload"] and again["last_success_at"] == first["last_success_at"]
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})
    feed_soxx(env, shares="90,100,000", nav=None)  # 發行商改了同一天的股數、這次沒有 NAV
    assert run(env, "us.flows", NOW + 120) == "ok"
    p = payload(env, "SOXX")
    assert (p["shares_out"], p["nav"], p["change"], p["flow"]) == (90_100_000, 500.0, 600_000, 300_000_000.0)
    assert p["days"][0] == {"as_of": "2026-10-27", "shares_out": 90_100_000, "nav": 500.0, "flag": None}


def test_a_same_day_refetch_with_a_jumped_nav_keeps_the_stored_nav_and_flow(env, caplog):
    """複審 R2-1-2：同一天重抓、頁面這次的 NAV 被跳動檢查擋掉 → 和頁面沒給 NAV 一樣沿用已存的 NAV 與金額，不蓋成 null；
    股數改了照寫，金額用已存的 NAV 重算。"""
    store_soxx(env, date(2026, 10, 26), "89,500,000", "495.50")
    feed_soxx(env, shares="90,000,000", nav="500.00")
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    first = obs(env, "SOXX")
    assert first["payload"]["flow"] == 250_000_000.0
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})
    feed_soxx(env, shares="90,000,000", nav="12.50")  # 版面改了，取到別的數字
    with caplog.at_level("WARNING", logger="market_pipeline_inst"):
        assert run(env, "us.flows", NOW + 60) == "ok"
    again = obs(env, "SOXX")
    assert again["payload"] == first["payload"] and again["last_success_at"] == first["last_success_at"]
    assert "market inst nav jump symbol=SOXX" in caplog.text and "12.5" not in caplog.text
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})
    feed_soxx(env, shares="90,100,000", nav="12.50")  # 發行商改了同一天的股數，NAV 還是壞的
    assert run(env, "us.flows", NOW + 120) == "ok"
    p = payload(env, "SOXX")
    assert (p["shares_out"], p["nav"], p["nav_as_of"], p["change"], p["flow"]) == (90_100_000, 500.0, "2026-10-27", 600_000,
                                                                                   300_000_000.0)


def test_a_same_day_refetch_with_a_jumped_nav_keeps_the_stored_nav_when_there_is_no_previous_day(env, caplog):
    """審查 R3-1：前一份沒有 NAV 或沒有前一份時，跳動也要和同一天已存的 NAV 比，壞值不能蓋掉好值。"""
    feed_soxx(env, shares="90,000,000", nav="500.00")
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    assert payload(env, "SOXX")["nav"] == 500.0
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})
    feed_soxx(env, shares="90,000,000", nav="12.50")
    with caplog.at_level("WARNING", logger="market_pipeline_inst"):
        assert run(env, "us.flows", NOW + 60) == "ok"
    assert payload(env, "SOXX")["nav"] == 500.0
    assert "market inst nav jump symbol=SOXX" in caplog.text


def test_a_same_day_refetch_with_a_jumped_nav_stays_null_when_nothing_good_is_stored(env):
    """對照：已存的同一天本來就沒有 NAV（分割那天被擋掉的）→ 還是 null、不算「還沒到」。"""
    store_soxx(env, date(2026, 10, 26), "89,500,000", "495.50")
    feed_soxx(env, shares="90,000,000", nav="12.50")
    feed_spy(env)
    assert run(env, "us.flows") == "ok"
    first = obs(env, "SOXX")
    assert (first["payload"]["nav"], first["payload"]["flow"]) == (None, None)
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})
    assert run(env, "us.flows", NOW + 60) == "ok"
    assert obs(env, "SOXX")["payload"] == first["payload"]


def test_a_stored_same_day_nav_that_itself_jumps_from_the_new_previous_day_is_not_kept():
    """SPY 的檔案列優先：前一天的 NAV 可能跟著改。已存的同一天 NAV 和新的前一天差超過門檻時也不沿用（不能繞過跳動檢查）。"""
    day, before = date(2026, 10, 27), date(2026, 10, 26)
    stored = {"days": [{"as_of": day.isoformat(), "shares_out": 1_000_000, "nav": 500.0, "flag": None},
                       {"as_of": before.isoformat(), "shares_out": 1_000_000, "nav": 495.0, "flag": None}]}
    for nav in (None, 12.5):
        page = src.FlowPage("SPY", (src.FlowDay(day, 1_000_000, nav), src.FlowDay(before, 1_000_000, 100.0)))
        got = pipe.flow_payload("SPY", page, stored).payload
        assert (got["nav"], got["flow"], got["days"][0]["nav"]) == (None, None, None), nav


def test_shares_before_nav_waits_for_the_nav_within_the_not_yet_budget(env):
    """I-3：股數先換到 D、NAV 還是前一天 → 寫股數、回 not_yet（不是 done）；2 小時後 NAV 到了 → 補上金額、done。"""
    store_soxx(env, date(2026, 10, 26), "89,500,000", "495.50")
    feed_soxx(env, shares="90,000,000", nav="495.50", nav_day=date(2026, 10, 26))
    feed_spy(env)
    assert run(env, "us.flows") == "partial"
    assert payload(env, "SOXX")["shares_out_as_of"] == "2026-10-27" and payload(env, "SOXX")["flow"] is None
    assert sched(env, "us.flows")["wait"]["SOXX"][0] == 1
    feed_soxx(env, shares="90,000,000", nav="500.00")
    assert run(env, "us.flows", NOW + 7200) == "ok"
    p = payload(env, "SOXX")
    assert (p["nav"], p["flow"]) == (500.0, 250_000_000.0)
    assert "SOXX" in sched(env, "us.flows")["done"]


def test_a_nav_that_never_arrives_stops_after_six_tries_with_the_shares_kept(env):
    store_soxx(env, date(2026, 10, 26), "89,500,000", "495.50")
    feed_soxx(env, shares="90,000,000", nav=None)
    feed_spy(env)
    for attempt in range(pipe.MAX_NOT_YET):
        run(env, "us.flows", NOW + 7200.0 * attempt)
    assert sched(env, "us.flows")["wait"]["SOXX"][0] == pipe.MAX_NOT_YET
    assert not due(env, "us.flows", NOW + 7200.0 * pipe.MAX_NOT_YET)  # 6 次用完：接受這天沒有金額
    p = payload(env, "SOXX")
    assert (p["shares_out"], p["change"], p["nav"], p["flow"]) == (90_000_000, 500_000, None, None)


@pytest.mark.parametrize("block", [
    '<div><span>NAV as of</span><span>Oct 27, 2026</span><span>1 Day NAV Change</span><span>$2.35</span></div>',
    '<div><span>NAV as of</span><span>Oct 27, 2026</span><span>12.5% YTD</span></div>',
])
def test_ishares_nav_needs_a_dollar_sign_before_the_number(block):
    """nit（R2-2）：「NAV as of <日期>」後面先接到別的數字時不能當 NAV（以前會取到 1.0、12.5）。"""
    page = src.parse_ishares(ishares_html(nav=None, extra=block))
    assert page.days == (src.FlowDay(D, 90_000_000, None),)


def test_a_nav_jump_over_half_is_dropped_and_logged(env, caplog):
    """nit（R2-2）：和前一份 NAV 差超過 50% → 不當 NAV（金額 null）、寫一行 log（只有代號）；不算「NAV 還沒到」，不再等。"""
    store_soxx(env, date(2026, 10, 26), "89,500,000", "500.00")
    feed_soxx(env, shares="90,000,000", nav="812.34")
    feed_spy(env)
    with caplog.at_level("WARNING", logger="market_pipeline_inst"):
        assert run(env, "us.flows") == "ok"
    p = payload(env, "SOXX")
    assert (p["change"], p["nav"], p["nav_as_of"], p["flow"]) == (500_000, None, None, None)
    assert "market inst nav jump symbol=SOXX" in caplog.text and "812" not in caplog.text
    assert not due(env, "us.flows", NOW + 7200)


def test_the_job_status_table_matches_the_collectors():
    """I-4 的接縫：監督程序不 import pipeline，obs、主機、旗標檔名在 jobs/market_snapshot 另寫一份，這裡鎖住三份一致。"""
    from jobs import market_snapshot as job

    assert job.INST_FLOWS == tuple((symbol, pipe.OBS_FLOWS[symbol], src.ENDPOINTS[symbol].host)
                                   for symbol in (*pipe.US_ITEMS, *pipe.TW_ITEMS))
    assert job.INST_DISABLED_FLAG == pipe.DISABLED_FLAG
    # 複審 R2-3-1、R2-3-2：T86 一項；每個來源用哪一條就緒時間；bot 的白名單（status_summary）是同一份來源
    import market_dashboard
    import market_pipeline_tw
    import market_sources_tw

    t86_host = market_sources_tw.SPARE_ENDPOINTS[market_pipeline_tw.T86_ENDPOINT].host
    assert job.INST_T86 == ("T86", market_pipeline_tw.OBS_T86, t86_host)
    assert market_dashboard.INST_STATUS_SOURCES == (*(item[0] for item in job.INST_FLOWS), job.INST_T86[0])
    assert job.INST_READY_KEYS == {**{symbol: pipe.US_KEY for symbol in pipe.US_ITEMS},
                                   **{symbol: pipe.TW_KEY for symbol in pipe.TW_ITEMS},
                                   job.INST_T86[0]: market_pipeline_tw.TASK_T86}
    assert set(job.INST_READY_KEYS.values()) == set(market_calc.INST_READY)


def test_inst_status_reports_each_source_for_the_daily_maintenance(env, monkeypatch):
    """I-4：每個來源的資料日、落後幾個交易日、最近一筆 NAV 的日期、那份 obs 自己的 fail_streak（任務 partial 時任務的
    fail_streak 會歸零）、待核對、最後成功時間、有沒有啟用；T86 另外一項（複審 R2-3-1、R2-3-2、R2-1-4）。"""
    from jobs import market_snapshot as job

    store_soxx(env, date(2026, 10, 26), "90,000,000")
    feed_soxx(env, shares="120,000,000")  # +33%：待核對
    feed_spy(env)
    run(env, "us.flows")
    env.http.fail[src.SSGA_NAVHIST_URL] = "status"
    env.store.set_task_state(market_pipeline_us.STATE_PREFIX + "us.flows", {})
    run(env, "us.flows", NOW + 60)
    status = job._inst_status(env.store, env.paths, NOW)["inst_flows"]
    soxx, spy = obs(env, "SOXX"), obs(env, "SPY")
    assert status["SOXX"] == {"data_date": "2026-10-27", "lag_sessions": 0, "nav_as_of": "2026-10-27", "fail_streak": 0,
                              "flag": "check", "last_success_at": soxx["last_success_at"], "enabled": True}
    assert status["SPY"] == {"data_date": "2026-10-27", "lag_sessions": 0, "nav_as_of": "2026-10-27", "fail_streak": 1,
                             "flag": None, "last_success_at": spy["last_success_at"], "enabled": True}
    # 從沒跑過：lag_sessions 是 null，不是 0（不能看起來正常）
    assert status["0050"] == {"data_date": None, "lag_sessions": None, "nav_as_of": None, "fail_streak": 0, "flag": None,
                              "last_success_at": None, "enabled": True}
    assert status["T86"] == {"data_date": None, "lag_sessions": None, "fail_streak": 0, "last_success_at": None,
                             "enabled": True}
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", market_http.ALLOWED_HOSTS - {"www.yuantaetfs.com", "www.twse.com.tw"})
    flows = job._inst_status(env.store, env.paths, NOW)["inst_flows"]
    assert (flows["0050"]["enabled"], flows["T86"]["enabled"], flows["SOXX"]["enabled"]) == (False, False, True)
    (env.paths.state_dir / "market_inst.disabled").write_text("", encoding="utf-8")
    assert {item["enabled"] for item in job._inst_status(env.store, env.paths, NOW)["inst_flows"].values()} == {False}


def tw_table(env: Env) -> None:
    """2026 的台股休市表（沒有休市日的最小版本）：沒有這一份時台股日曆說不知道，0050、T86 的 lag_sessions 是 null。"""
    env.store.put_obs("tw.holidays.2026", {"rows": []}, data_date=None)


@pytest.mark.parametrize("now, lag", [
    (at(2026, 10, 27, 17, 0), 0),  # 10/27 還沒到就緒時間（美東 18:00）：該有的是 10/26
    (at(2026, 10, 27, 18, 0), 1),
    (at(2026, 10, 28, 8, 0), 1),
    (at(2026, 10, 29, 19, 0), 3),
    (at(2026, 10, 31, 12, 0), 4),  # 週六：該有的是週五 10/30
    (at(2026, 11, 2, 19, 0), 5),
])
def test_lag_sessions_counts_the_sessions_after_the_data_date(env, now, lag):
    from jobs import market_snapshot as job

    store_soxx(env, date(2026, 10, 26), "90,000,000")
    assert job._inst_status(env.store, env.paths, now)["inst_flows"]["SOXX"]["lag_sessions"] == lag


def test_lag_sessions_of_the_tw_sources_use_their_own_ready_times(env):
    """複審 R2-3-1、R2-3-2：0050 看元大（17:00）、T86 看證交所（16:30）；台股日曆不知道時兩個都是 null，不是 0。"""
    from jobs import market_snapshot as job

    feed_0050(env, nav_day="2026/10/27", trade_day="2026/10/27")
    assert run(env, "tw.flows", at(2026, 10, 27, 18, 0, tz=TPE)) == "ok"
    env.store.put_obs("tw.inst.t86.0050", {"v": 1, "trade_date": "2026-10-27"}, data_date="2026-10-27")
    env.store.obs_checked("tw.inst.t86.0050", error_kind="format")

    def status(now: float) -> dict:
        return job._inst_status(env.store, env.paths, now)["inst_flows"]

    assert status(at(2026, 10, 28, 18, 0, tz=TPE))["T86"]["lag_sessions"] is None  # 還沒有休市表：說不出來
    assert status(at(2026, 10, 28, 18, 0, tz=TPE))["0050"]["lag_sessions"] is None
    tw_table(env)
    t86 = status(at(2026, 10, 28, 16, 45, tz=TPE))
    assert (t86["0050"]["lag_sessions"], t86["T86"]["lag_sessions"]) == (0, 1)  # 16:30 過了、17:00 還沒
    later = status(at(2026, 10, 30, 18, 0, tz=TPE))
    assert (later["0050"]["lag_sessions"], later["T86"]["lag_sessions"]) == (3, 3)
    found = env.store.get_obs("tw.inst.t86.0050")
    assert later["T86"] == {"data_date": "2026-10-27", "lag_sessions": 3, "fail_streak": 1,
                            "last_success_at": found["last_success_at"], "enabled": True}


def test_lag_sessions_is_null_when_the_calendars_cannot_be_read(env, monkeypatch):
    """日曆壞掉：lag_sessions 全是 null（每日維護的「或是 null」會報），其他欄位照給，不讓整個 inst_flows 變 null。"""
    from jobs import market_snapshot as job

    store_soxx(env, date(2026, 10, 26), "90,000,000")

    def broken(_store: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(market_calendar, "trading_calendars", broken)
    soxx = job._inst_status(env.store, env.paths, NOW)["inst_flows"]["SOXX"]
    assert (soxx["data_date"], soxx["lag_sessions"], soxx["enabled"]) == ("2026-10-26", None, True)


def test_nav_as_of_shows_how_long_the_nav_has_been_gone(env):
    """複審 R2-1-4：NAV 卡改版、股數照常更新時，data_date、fail_streak、flag 全部正常；nav_as_of 留在最後一筆有 NAV 的日期
    （payload 的 nav_as_of 只會等於資料日或 null，看不出停了幾天）。"""
    from jobs import market_snapshot as job

    days = list(reversed(us_sessions(D, 3)))  # 10/23、10/26、10/27
    store_soxx(env, days[0], "90,000,000")
    for index, day in enumerate(days[1:], start=1):
        feed_soxx(env, shares=f"{90_000_000 + index * 100_000:,}", shares_day=day, nav=None)
        feed_spy(env, spy_rows(day))
        for attempt in range(pipe.MAX_NOT_YET):
            run(env, "us.flows", evening(day) + 7200.0 * attempt)
    soxx = job._inst_status(env.store, env.paths, NOW)["inst_flows"]["SOXX"]
    assert (soxx["data_date"], soxx["lag_sessions"], soxx["fail_streak"], soxx["flag"]) == ("2026-10-27", 0, 0, None)
    assert soxx["nav_as_of"] == "2026-10-23" and payload(env, "SOXX")["nav_as_of"] is None


def test_a_failing_source_reaches_the_daily_maintenance_through_status_summary(env):
    """複審 R2-3-1：iShares 改版、SSGA 正常。任務每個期間第一輪都是 partial，任務的 fail_streak 到不了 3，outcome.failing 看不到；
    每日維護只讀 status_summary，要走完 obs → job 的 outcome → _outcome_summary 的白名單，每個來源的狀態才算數。"""
    import time as time_module

    import market_dashboard
    from jobs import market_snapshot as job

    store_soxx(env, date(2026, 10, 20), "90,000,000")
    env.http.bodies[src.ISHARES_URL] = ishares_html(shares=None)  # 改版：頁面上找不到在外股數
    streaks = []
    for day in reversed(us_sessions(D, 5)):  # 10/21 … 10/27
        feed_spy(env, spy_rows(day))
        for offset in (0.0, 60.0, 960.0):
            run(env, "us.flows", evening(day) + offset)
            streaks.append(env.store.task_state("us.flows").get("fail_streak", 0))
    final = evening(D) + 960.0
    assert max(streaks) < 3  # 任務層看不到（這就是 I-4）
    supervisor = job.Supervisor(env.paths, job.parse_args(["--root", str(env.paths.state_dir.parent)]), -1,
                                wall=lambda: final)
    outcome = supervisor.compose_outcome(code="ok", collect="ok", publish="ok", started_at=final,
                                         locked_at=time_module.monotonic())
    market_paths.write_private_atomic(env.paths.outcome, json.dumps(outcome).encode())
    summary = market_dashboard.status_summary(now=final, paths=env.paths)["outcome"]
    assert summary["state"] == "ok" and summary["inst_status_error"] is None
    soxx, spy = summary["inst_flows"]["SOXX"], summary["inst_flows"]["SPY"]
    assert (soxx["fail_streak"], soxx["data_date"], soxx["lag_sessions"], soxx["enabled"]) == (15, "2026-10-20", 5, True)
    assert (spy["fail_streak"], spy["data_date"], spy["lag_sessions"]) == (0, "2026-10-27", 0)
    never = summary["inst_flows"]["0050"]  # 從沒跑過：是 null，不是看起來正常的 0
    assert (never["data_date"], never["lag_sessions"], never["nav_as_of"]) == (None, None, None)
    assert summary["inst_flows"]["T86"]["data_date"] is None and summary["inst_flows"]["T86"]["lag_sessions"] is None


def test_the_outcome_carries_inst_flows_and_survives_a_broken_status(env, monkeypatch):
    import time as time_module

    from jobs import market_snapshot as job

    store_soxx(env, date(2026, 10, 26), "90,000,000")
    root = env.paths.state_dir.parent
    supervisor = job.Supervisor(env.paths, job.parse_args(["--root", str(root)]), -1, wall=lambda: NOW)
    outcome = supervisor.compose_outcome(code="ok", collect="ok", publish="ok", started_at=NOW,
                                         locked_at=time_module.monotonic())
    assert outcome["inst_flows"]["SOXX"]["data_date"] == "2026-10-26"
    assert outcome["inst_flows"]["SOXX"]["flag"] is None and outcome["inst_flows"]["0050"]["data_date"] is None
    assert outcome["inst_flows"]["SOXX"]["lag_sessions"] == 1 and outcome["inst_flows"]["T86"]["lag_sessions"] is None
    assert job._clean(outcome) == outcome  # 只有列舉值、日期、數字：過得了 outcome 的清洗

    def broken(*_args: Any) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(job, "_inst_status", broken)
    outcome = supervisor.compose_outcome(code="ok", collect="ok", publish="ok", started_at=NOW,
                                         locked_at=time_module.monotonic())
    assert outcome["inst_flows"] is None and outcome["inst_status_error"] == "unexpected:RuntimeError"


def test_change_5d_is_null_when_five_small_steps_add_up_past_the_threshold(env):
    """I-1 (b)：每天 +4.6% 都沒到 20%，5 天加起來 +25% 超過門檻 → change_5d 是 null（每一天自己都不是 check）。"""
    days = list(reversed(us_sessions(D, 6)))
    results = [soxx_run(env, day, int(90_000_000 * 1.046 ** index), 500.0 + index) for index, day in enumerate(days)]
    assert all(result["flag"] is None for result in results)
    assert results[-1]["change_5d"] is None and len(results[-1]["days"]) == 6


def test_change_5d_is_null_when_five_small_steps_down_add_up_past_the_threshold(env):
    """複審 R2-1-5：門檻的分母用起點與終點較小的那筆，往下也一樣：每天 -3.7%，5 天 -17.2%（起點當分母沒到 20%，終點當分母 20.7%）。"""
    days = list(reversed(us_sessions(D, 6)))
    results = [soxx_run(env, day, int(90_000_000 * 0.963 ** index), 500.0 + index) for index, day in enumerate(days)]
    assert all(result["flag"] is None for result in results)
    assert results[-1]["change_5d"] is None and len(results[-1]["days"]) == 6


def test_a_day_flagged_for_an_official_mismatch_keeps_its_flag_and_blocks_change_5d(env):
    """I-1：0050 某天自算差額和官方差異數對不上（數字不大、相鄰比沒超過門檻）→ 那天 check；那一筆的 flag 之後一直留著，
    它還在窗口裡時 change_5d 是 null（光看相鄰兩筆看不出來，靠的是每一筆自己帶的 flag）。"""
    days = [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26),
            date(2026, 10, 27)]
    results = {}
    for index, day in enumerate(days):
        official = "6,000,000" if day == date(2026, 10, 22) else "5,000,000"  # 那天實際只多 5,000,000
        shown = f"{day:%Y/%m/%d}"
        feed_0050(env, units=f"{23_000_000_000 + index * 5_000_000:,}", diff=official, nav_day=shown, trade_day=shown)
        assert run(env, "tw.flows", at(day.year, day.month, day.day, 18, 0, tz=TPE)) == "ok", day
        results[day] = payload(env, "0050")
    assert results[date(2026, 10, 22)]["flag"] == "check" and results[date(2026, 10, 27)]["flag"] is None
    last = results[date(2026, 10, 27)]
    assert {item["as_of"]: item["flag"] for item in last["days"]}["2026-10-22"] == "check"
    assert last["change_5d"] is None
