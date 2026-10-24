"""market_pipeline_misc（/盤勢 宏觀、BTC、新聞、川普、選舉的 collect 任務，2026-10-17）的測試：假的 http、
暫存的 Store、固定的日曆、注入的時間。測的是：Yahoo 每個 D 一次、失敗改代理且每格標來源（BTC 是 IBIT）、≤D、代理沒有 D 那根
30 分鐘後重抓、新聞 36 小時與 503 冷卻、川普依 originalId 累積與「（至少）」、預測市場的間隔（封鎖中永遠不到期）。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest

import market_calendar
import market_http
import market_pipeline_us
import market_paths
import market_pipeline_misc as misc
import market_pipeline_tw as tw
import market_store
import market_tasks

TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
YAHOO = {
    "DXY": "DX-Y.NYB", "WTI": "CL=F", "BRENT": "BZ=F", "GOLD": "GC=F", "SILVER": "SI=F", "BTC": "BTC-USD",
    "USDTWD": "TWD=X", "RUT": "^RUT", "DJI": "^DJI", "IXIC": "^IXIC", "MOVE": "^MOVE",
}
PROXIES = {
    "DXY": "UUP", "WTI": "USO", "BRENT": "BNO", "GOLD": "GLD", "SILVER": "SLV", "BTC": "IBIT", "RUT": "IWM", "DJI": "DIA",
    "IXIC": "ONEQ",
}
WEEK = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)]
D = date(2026, 10, 15)  # 週四；台北週五 07:00＝美東週四 19:00，週四收盤＋30 分鐘已過
NOW_AFTER_D = datetime(2026, 10, 16, 7, 0, tzinfo=TPE).timestamp()


# ── 小工具 ──────────────────────────────────────────────────────────────────────
def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=TPE).timestamp()


class Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeHttp:
    """照 market_http.fetch 的樣子：先在帳本預扣，再依網址片段回假的內容（dict／list→JSON、str→UTF-8）。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.calls: list[str] = []

    def route(self, needle: str, handler) -> None:
        self.routes.insert(0, (needle, handler))

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        if ledger is not None:
            ledger.reserve(urlsplit(url).hostname)
        self.calls.append(url)
        for needle, handler in self.routes:
            if needle in url:
                result = handler(url) if callable(handler) else handler
                if isinstance(result, BaseException):
                    raise result
                if isinstance(result, str):
                    body = result.encode("utf-8")
                elif isinstance(result, bytes):
                    body = result
                else:
                    body = json.dumps(result, ensure_ascii=False).encode()
                return market_http.Response(200, body, {"content-type": "application/json"}, url)
        raise market_http.FetchError("status")

    def count(self, needle: str) -> int:
        return sum(needle in url for url in self.calls)


@pytest.fixture
def env(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    clock = Clock(at(2026, 10, 1))
    store = market_store.Store(state / "market_history.sqlite", now_fn=clock)
    store.put_obs(
        "tw.holidays.2026",
        {"year": 2026, "rows": [{"date": "2026-10-09", "name": "國慶日", "note": "補假。", "kind": "closed"}], "unknown": []},
        data_date=None,
    )
    yield SimpleNamespace(store=store, http=FakeHttp(), clock=clock, paths=market_paths.Paths.under(tmp_path))
    store.close()


def make_ctx(env: SimpleNamespace, now: float, *, http=None) -> market_tasks.Context:
    env.clock.value = max(env.clock.value, now)
    return market_tasks.Context(
        store=env.store,
        http=env.http if http is None else http,
        ledger=env.store.ledger(now_fn=env.clock, sleep_fn=env.clock.sleep),
        now=now,
        paths=env.paths,
        deadline=1e9,
        us_cal=market_calendar.us_calendar(env.store),
        tw_cal=market_calendar.tw_calendar(env.store),
        clock=lambda: 0.0,
    )


TASKS = {task.name: task for task in misc.tasks()}


def run(ctx: market_tasks.Context, name: str) -> str:
    return market_tasks.run_task(ctx, TASKS[name])


def payload(env: SimpleNamespace, obs_id: str) -> dict:
    return (env.store.get_obs(obs_id) or {}).get("payload")


# Yahoo spark：交易所當地 09:30 的時間戳（C 用交易所時區換成日期）
def spark_entry(symbol: str, bars: list[tuple[date, float]], tz: str = "America/New_York") -> dict:
    zone = ZoneInfo(tz)
    stamps = [int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=zone).timestamp()) for d, _ in bars]
    meta = {"symbol": symbol, "currency": "USD", "exchangeTimezoneName": tz}
    return {"symbol": symbol, "response": [{"meta": meta, "timestamp": stamps, "indicators": {"quote": [{"close": [c for _, c in bars]}]}}]}


def spark(entries: dict[str, list[tuple[date, float]]]) -> dict:
    return {"spark": {"result": [spark_entry(symbol, bars) for symbol, bars in entries.items()], "error": None}}


def full_week(base: float) -> list[tuple[date, float]]:
    return [(day, round(base + index * 0.5, 4)) for index, day in enumerate(WEEK)]


def nasdaq_hist(symbol: str, bars: list[tuple[date, float]]) -> dict:
    rows = [
        {"date": d.strftime("%m/%d/%Y"), "close": f"{c:.2f}", "volume": "1,000", "open": f"{c:.2f}",
         "high": f"{c:.2f}", "low": f"{c:.2f}"}
        for d, c in sorted(bars, reverse=True)
    ]
    return {"data": {"symbol": symbol, "totalRecords": len(rows), "tradesTable": {"asOf": None, "rows": rows}},
            "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


def route_proxies(env: SimpleNamespace, bars_for) -> None:
    for etf in PROXIES.values():
        env.http.route(f"/api/quote/{etf}/historical", lambda url, etf=etf: nasdaq_hist(etf, bars_for(etf)))


def nasdaq_published_d(env: SimpleNamespace, *, day: date = D, provisional: bool = False) -> None:
    """Nasdaq 日線端點已公布 D：SPY（market_pipeline_us 一直在追的那檔）在 closes 表有 D 的正式列。
    宏觀的 ETF 代理要等到這個才去抓（2026-10-23）；provisional＝CBOE 的備援列，不算公布。"""
    env.store.upsert_closes("us", "SPY", [(day, 600.0, 1_000)], "cboe" if provisional else "nasdaq", provisional=provisional)


def rfc(moment: datetime) -> str:
    return format_datetime(moment.astimezone(UTC), usegmt=True)


def news_rss(items: list[tuple[str, datetime, str]]) -> str:
    body = "".join(
        f"<item><title>{title}</title><link>https://news.google.com/rss/articles/a{i}</link>"
        f'<guid isPermaLink="false">g{i}</guid><pubDate>{rfc(when)}</pubDate><description>d</description>'
        f'<source url="https://example.com">{source}</source></item>'
        for i, (title, when, source) in enumerate(items)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'


TRUMP_NS = (
    'xmlns:truth="https://truthsocial.com/ns" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:archive="https://www.trumpstruth.org/rss-extensions"'
)


def trump_rss(posts: list[tuple[str, datetime, str, bool]]) -> str:
    items = "".join(
        "<item><title>Post</title><link>https://www.trumpstruth.org/statuses/1</link>"
        f"<description><![CDATA[<p>{text}</p>]]></description><dc:creator>Donald J. Trump</dc:creator>"
        f"<pubDate>{rfc(when)}</pubDate><truth:originalUrl>https://truthsocial.com/@realDonaldTrump/{oid}</truth:originalUrl>"
        f"<truth:originalId>{oid}</truth:originalId><archive:postType>original</archive:postType>"
        f"<archive:removed>{'true' if removed else 'false'}</archive:removed></item>"
        for oid, when, text, removed in posts
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" {TRUMP_NS}><channel><title>t</title>{items}</channel></rss>'


def polymarket_event(*, closed: bool = False, prices: str = '["0.905", "0.095"]') -> list:
    market = {
        "id": "562802", "question": "Will the Democratic Party control the House after the 2026 Midterm elections?",
        "groupItemTitle": "Democratic Party", "outcomes": '["Yes", "No"]', "outcomePrices": prices,
        "bestBid": 0.9, "bestAsk": 0.91, "lastTradePrice": 0.91, "active": not closed, "closed": closed,
        "updatedAt": "2026-10-17T02:37:14.573417Z",
    }
    return [{"id": "32225", "slug": "which-party-will-win-the-house-in-2026", "markets": [market]}]


# ── 登錄 ──────────────────────────────────────────────────────────────────────────
def test_tasks_and_groups():
    with market_tasks.isolated_registry():
        misc.register_all()
        groups = {task.name: task.group for task in market_tasks.registered()}
    import market_sources_misc

    elections = {f"election.{market.key}": "medium" for market in market_sources_misc.PREDICTION_MARKETS}
    assert {"election.polymarket.house", "election.kalshi.house"} <= set(elections)  # 每個固定市場一個任務
    assert groups == {
        "news.us": "fast", "news.tw": "fast", "news.iran": "fast", "trump.feed": "fast",
        "macro.yahoo": "core", **elections,
    }
    assert "macro.btc" not in groups  # 2026-10-23：Coinbase 那條拿掉，BTC 走宏觀（Yahoo 或 IBIT 代理）


# ── 宏觀：Yahoo 為主 ─────────────────────────────────────────────────────────────────
def test_yahoo_once_per_d_keeps_rows_up_to_d_with_yahoo_labels(env):
    entries = {symbol: full_week(10.0 + i) for i, symbol in enumerate(YAHOO.values())}
    entries["CL=F"] = full_week(80.0) + [(date(2026, 10, 16), 99.0)]  # 期貨 24 小時交易：D 之後那根要丟
    entries["^RUT"] = [(date(2026, 10, 13), 2400.0), (D, 2450.0)]  # 缺 10/14：前一筆不是前一個交易日
    env.http.route("finance.yahoo.com", spark(entries))
    ctx = make_ctx(env, NOW_AFTER_D)
    assert misc.us_session_d(ctx) == D
    assert run(ctx, "macro.yahoo") == "ok"
    assert env.http.count("finance.yahoo.com") == 1 and env.http.count("api.nasdaq.com") == 0
    dxy = payload(env, "macro.DXY")
    assert dxy["source"] == "Yahoo" and dxy["symbol"] == "DX-Y.NYB"
    assert dxy["value"] == 11.5 and dxy["date"] == "2026-10-15" and dxy["prev_date"] == "2026-10-14"
    assert dxy["chg"] == pytest.approx(0.5) and dxy["chg_pct"] == pytest.approx(0.5 / 11.0 * 100)
    wti = payload(env, "macro.WTI")
    assert wti["value"] == 81.5 and wti["date"] == "2026-10-15" and all(day <= "2026-10-15" for day, _ in wti["bars"])
    rut = payload(env, "macro.RUT")
    assert rut["value"] == 2450.0 and rut["chg"] is None and rut["prev_date"] is None
    btc = payload(env, "macro.BTC")  # 2026-10-23：BTC 和其他格一樣，Yahoo 的 BTC-USD 就是主格
    assert btc["source"] == "Yahoo" and btc["symbol"] == "BTC-USD" and btc["value"] == 16.5
    assert env.store.get_obs("macro.BTC.yahoo") is None  # 舊的備援鍵不再寫
    assert env.store.get_obs("macro.DXY")["data_date"] == "2026-10-15"
    # 同一個 D 不再打；下一個 D（週五收盤後）才再打一次
    assert run(make_ctx(env, NOW_AFTER_D + 3600), "macro.yahoo") == "not_due"
    assert misc.macro_plan(make_ctx(env, at(2026, 10, 17, 7, 0)))["yahoo"] is True


def test_yahoo_failure_switches_to_proxies_and_labels_each_value(env):
    env.http.route("finance.yahoo.com", market_http.FetchError("rate_limited"))
    route_proxies(env, lambda etf: full_week(20.0) + [(date(2026, 10, 16), 30.0)])  # D 之後那根也要丟
    nasdaq_published_d(env)
    env.store.put_obs(
        tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.338, "prev_date": "2026-10-14", "prev_usd_ntd": 32.3},
        data_date="2026-10-15",
    )
    ctx = make_ctx(env, NOW_AFTER_D)
    assert run(ctx, "macro.yahoo") == "partial"  # MOVE 沒有代理
    assert env.http.count("api.nasdaq.com") == len(PROXIES) == 9  # 8 個 ETF 代理＋BTC 的 IBIT
    assert env.http.count("coinbase") == 0 and env.http.count("taifex") == 0  # Coinbase 拿掉了、台幣讀 tw.fx
    for key, etf in PROXIES.items():
        item = payload(env, f"macro.{key}")
        assert item["source"] == f"ETF 代理：{etf}" and item["symbol"] == etf
        assert item["date"] == "2026-10-15" and item["value"] == 21.5
    usd = payload(env, "macro.USDTWD")
    assert usd["source"] == "代理：期交所匯率" and usd["value"] == 32.338  # Yahoo 的 TWD=X 拿不到才用：標成代理
    import market_dashboard

    assert market_dashboard._macro_label("USDTWD", usd) == "台幣匯率（期交所代理）"
    assert market_dashboard._macro_label("DXY", payload(env, "macro.DXY")) == "美元（UUP 代理）"
    assert usd["chg"] == pytest.approx(0.038)  # 10/14 是 10/15 的前一個台股交易日
    move = env.store.get_obs("macro.MOVE")
    assert move["payload"] is None and move["error"] == "rate_limited" and move["fail_streak"] == 1
    # 全部代理都拿到 D：同一個 D 不再排
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 3 * 3600)) is None


def test_partial_yahoo_response_sends_only_the_missing_symbols_to_proxies(env):
    entries = {symbol: full_week(10.0) for symbol in YAHOO.values() if symbol != "^RUT"}
    env.http.route("finance.yahoo.com", spark(entries))
    route_proxies(env, lambda etf: full_week(200.0))
    nasdaq_published_d(env)
    assert run(make_ctx(env, NOW_AFTER_D), "macro.yahoo") == "ok"
    assert [urlsplit(url).path for url in env.http.calls if "nasdaq" in url] == ["/api/quote/IWM/historical"]
    assert payload(env, "macro.RUT")["source"] == "ETF 代理：IWM"
    assert payload(env, "macro.DJI")["source"] == "Yahoo"


def test_proxies_wait_until_spy_has_the_official_d_row(env):
    """2026-10-23：Nasdaq 日線端點要收盤後好幾個小時才公布 D 那列。ETF 代理原本 D 換日（04:43）就開打、30 分鐘後放棄，
    8 列整天停在前一個交易日。現在等 SPY 在 closes 表有 D 的正式列才去抓：之前一個請求都不送，之後每個代理抓一次。"""
    env.http.route("finance.yahoo.com", market_http.FetchError("rate_limited"))
    route_proxies(env, lambda etf: full_week(20.0))
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.3}, data_date="2026-10-15")
    assert run(make_ctx(env, NOW_AFTER_D), "macro.yahoo") == "partial"
    assert env.http.count("api.nasdaq.com") == 0  # D 換日那一輪：Nasdaq 還沒公布，不打
    state = misc._state(env.store, misc.TASK_MACRO)
    assert state["pending"] == {key: {"tries": 0} for key in PROXIES}  # 等著，這不算一次
    assert env.store.get_obs("macro.DXY") is None and env.store.get_obs("macro.BTC") is None
    assert payload(env, "macro.USDTWD")["source"] == "代理：期交所匯率"  # 台幣不走 Nasdaq：當場就有
    # SPY 沒有 D 的正式列：之後每一輪都不排、不送請求；CBOE 的 provisional 或前一天的正式列都不算
    nasdaq_published_d(env, provisional=True)
    nasdaq_published_d(env, day=date(2026, 10, 14))
    for offset in (60, 1800, 3 * 3600):
        assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + offset)) is None
        assert run(make_ctx(env, NOW_AFTER_D + offset), "macro.yahoo") == "not_due"
    assert env.http.count("api.nasdaq.com") == 0
    # SPY 有了 D 的正式列：下一輪每個代理抓一次，都拿到 D，之後不再排
    nasdaq_published_d(env)
    later = NOW_AFTER_D + 4 * 3600
    assert misc.macro_plan(make_ctx(env, later)) == {"d": D, "yahoo": False, "keys": sorted(PROXIES)}
    assert run(make_ctx(env, later), "macro.yahoo") == "ok"
    assert sorted(urlsplit(url).path for url in env.http.calls if "nasdaq" in url) == sorted(
        f"/api/quote/{etf}/historical" for etf in PROXIES.values())
    for key in PROXIES:
        assert payload(env, f"macro.{key}")["date"] == "2026-10-15", key
    assert misc.macro_plan(make_ctx(env, later + 3600)) is None


def test_proxy_without_the_d_bar_is_fetched_again_every_hour(env):
    env.http.route("finance.yahoo.com", market_http.FetchError("status"))
    late = {"yes": False}
    route_proxies(env, lambda etf: full_week(20.0) if etf != "UUP" or late["yes"] else full_week(20.0)[:-1])
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.3}, data_date="2026-10-15")
    nasdaq_published_d(env)
    assert run(make_ctx(env, NOW_AFTER_D), "macro.yahoo") == "partial"
    assert payload(env, "macro.DXY")["date"] == "2026-10-14"  # 先放前一天的值（卡上看資料日）
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 1800)) is None  # 舊的 30 分鐘間隔不再重抓
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 3599)) is None
    plan = misc.macro_plan(make_ctx(env, NOW_AFTER_D + 3600))
    assert plan == {"d": D, "yahoo": False, "keys": ["DXY"]}
    late["yes"] = True
    before = env.http.count("finance.yahoo.com")
    assert run(make_ctx(env, NOW_AFTER_D + 3600), "macro.yahoo") == "ok"  # 這次只重抓 UUP（MOVE 第一次就記了失敗）
    assert env.http.count("finance.yahoo.com") == before  # Yahoo 一個 D 只打一次
    assert payload(env, "macro.DXY")["date"] == "2026-10-15"
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 7200)) is None


def test_proxy_retries_are_limited_to_three_per_d_so_the_budget_stays_small(env):
    """每個代理每個 D：第一次＋最多再 3 次（每小時）。全部代理都抓不到 D 也只有 9×4＝36 個請求；平常第一次就拿到＝9 個。"""
    env.http.route("finance.yahoo.com", market_http.FetchError("status"))
    route_proxies(env, lambda etf: full_week(20.0)[:-1] if etf == "UUP" else full_week(20.0))
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.3}, data_date="2026-10-15")
    nasdaq_published_d(env)
    for hours in range(0, 8):
        run(make_ctx(env, NOW_AFTER_D + hours * 3600), "macro.yahoo")
    assert env.http.count("/api/quote/UUP/") == 4 == misc.PROXY_RETRIES + 1
    assert all(env.http.count(f"/api/quote/{etf}/") == 1 for key, etf in PROXIES.items() if key != "DXY")
    assert env.http.count("api.nasdaq.com") == 8 + 4
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 12 * 3600)) is None


def test_even_when_no_proxy_ever_gets_the_d_bar_a_day_costs_at_most_36_requests(env):
    env.http.route("finance.yahoo.com", market_http.FetchError("status"))
    route_proxies(env, lambda etf: full_week(20.0)[:-1])  # Nasdaq 一直沒有 D 那根
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.3}, data_date="2026-10-15")
    nasdaq_published_d(env)
    for hours in range(0, 10):
        run(make_ctx(env, NOW_AFTER_D + hours * 3600), "macro.yahoo")
    assert env.http.count("api.nasdaq.com") == len(PROXIES) * (misc.PROXY_RETRIES + 1) == 36
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 20 * 3600)) is None  # 放棄之後不再打，直到下一個 D（台北 10/17 04:30）


def test_usdtwd_proxy_fetches_taifex_once_when_the_tw_pipeline_has_nothing(env):
    env.http.route("finance.yahoo.com", market_http.FetchError("rate_limited"))
    route_proxies(env, lambda etf: full_week(20.0))
    env.http.route("DailyForeignExchangeRates", [
        {"Date": "20261014", "USD/NTD": "32.300"}, {"Date": "20261015", "USD/NTD": "32.338"},
        {"Date": "20261016", "USD/NTD": "32.400"},  # D 之後的不收
    ])
    run(make_ctx(env, NOW_AFTER_D), "macro.yahoo")
    assert env.http.count("DailyForeignExchangeRates") == 1
    usd = payload(env, "macro.USDTWD")
    # 期交所只給最新一天與前一天：最新那天（10/16）晚於 D 被丟掉，所以用 10/15、沒有前一筆可比
    assert usd["date"] == "2026-10-15" and usd["value"] == 32.338 and usd["chg"] is None


def _usdtwd_after_the_d_change(env, *, fx_date: str = "2026-10-14", prev_date: str = "2026-10-13") -> dict:
    """Yahoo 429、期交所的匯率檔還是前一天（tw.fx＝fx_date）：D 換日那一輪台幣用 tw.fx 的值（標「期交所代理」）。"""
    env.http.route("finance.yahoo.com", market_http.FetchError("rate_limited"))
    route_proxies(env, lambda etf: full_week(20.0))
    nasdaq_published_d(env)
    env.store.put_obs(tw.OBS_FX, {"date": fx_date, "usd_ntd": 32.3, "prev_date": prev_date, "prev_usd_ntd": 32.2}, data_date=fx_date)
    assert run(make_ctx(env, NOW_AFTER_D), "macro.yahoo") == "partial"
    return payload(env, "macro.USDTWD")


def test_usdtwd_follows_tw_fx_when_it_advances_later_in_the_day(env):
    """2026-10-23：期交所的匯率檔台北早上才更新，market_pipeline_tw 隔一陣子才讀到。宏觀任務原本只在 D 換日那一刻讀 tw.fx，
    之後它前進了卡上的台幣還是前一天；現在 tw.fx 的資料日比 macro.USDTWD 新就重算（只讀 obs，不送請求）。"""
    usd = _usdtwd_after_the_d_change(env)
    assert usd["source"] == "代理：期交所匯率" and usd["date"] == "2026-10-14" and usd["value"] == 32.3
    later = NOW_AFTER_D + 600  # 還沒到 pending 的 1 小時重試
    assert misc.macro_plan(make_ctx(env, later)) is None  # tw.fx 沒前進：台幣不重算
    nasdaq_before = env.http.count("api.nasdaq.com")
    env.store.put_obs(
        tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.338, "prev_date": "2026-10-14", "prev_usd_ntd": 32.3},
        data_date="2026-10-15",
    )
    assert misc.macro_plan(make_ctx(env, later)) == {"d": D, "yahoo": False, "keys": ["USDTWD"]}
    assert run(make_ctx(env, later), "macro.yahoo") == "ok"
    usd = payload(env, "macro.USDTWD")
    assert usd["date"] == "2026-10-15" and usd["value"] == 32.338 and usd["chg"] == pytest.approx(0.038)
    assert env.store.get_obs("macro.USDTWD")["data_date"] == "2026-10-15"
    assert env.http.count("taifex") == 0 and env.http.count("api.nasdaq.com") == nasdaq_before  # 沒有任何請求
    assert misc.macro_plan(make_ctx(env, later + 60)) is None and misc.macro_plan(make_ctx(env, later + 7200)) is None


def test_usdtwd_refresh_stops_at_d_and_does_not_loop_when_tw_fx_is_ahead_of_d(env):
    usd = _usdtwd_after_the_d_change(env, fx_date="2026-10-16", prev_date="2026-10-15")  # 台股日期已經是 10/16，D 是 10/15
    assert usd["date"] == "2026-10-15" and usd["value"] == 32.2  # 晚於 D 的那列丟掉（§18.8）
    for offset in (60, 600, 7200):
        assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + offset)) is None, offset
    assert run(make_ctx(env, NOW_AFTER_D + 600), "macro.yahoo") == "not_due"


def test_usdtwd_from_yahoo_is_not_replaced_by_tw_fx(env):
    entries = {symbol: full_week(10.0) for symbol in YAHOO.values()}
    env.http.route("finance.yahoo.com", spark(entries))
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.338, "prev_date": "2026-10-14", "prev_usd_ntd": 32.3},
                      data_date="2026-10-15")
    assert run(make_ctx(env, NOW_AFTER_D), "macro.yahoo") == "ok"
    assert payload(env, "macro.USDTWD")["source"] == "Yahoo"
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 33.0}, data_date="2026-10-15")  # 之後 tw.fx 換了值也不理它
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 600)) is None


def test_usdtwd_is_built_when_tw_fx_shows_up_after_the_taifex_fetch_failed(env):
    env.http.route("finance.yahoo.com", market_http.FetchError("rate_limited"))
    env.http.route("DailyForeignExchangeRates", market_http.FetchError("timeout"))
    route_proxies(env, lambda etf: full_week(20.0))
    nasdaq_published_d(env)
    assert run(make_ctx(env, NOW_AFTER_D), "macro.yahoo") == "partial"
    assert env.store.get_obs("macro.USDTWD") is None or payload(env, "macro.USDTWD") is None
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.338}, data_date="2026-10-15")
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 300)) == {"d": D, "yahoo": False, "keys": ["USDTWD"]}  # 不等 1 小時重試
    assert run(make_ctx(env, NOW_AFTER_D + 300), "macro.yahoo") == "ok"
    assert payload(env, "macro.USDTWD")["value"] == 32.338


def test_macro_payload_rules():
    cal = market_calendar.Calendar("us")
    bars = [(date(2026, 10, 14), 10.0), (D, 11.0), (date(2026, 10, 16), 12.0), (date(2026, 10, 13), float("nan")),
            ("bad", 1.0), (date(2026, 10, 12), -1.0)]
    item, day = misc.macro_payload(bars, D, cal, "Yahoo", "X")
    assert day == D and item["value"] == 11.0 and item["chg"] == pytest.approx(1.0)
    assert [row[0] for row in item["bars"]] == ["2026-10-14", "2026-10-15"]
    assert misc.macro_payload([(date(2026, 10, 16), 1.0)], D, cal, "Yahoo", "X") is None
    item, _ = misc.macro_payload([(date(2026, 10, 9), 10.0), (D, 11.0)], D, cal, "Yahoo", "X")
    assert item["chg"] is None  # 前一筆不是前一個交易日（10/14）


# ── BTC：和其他宏觀格一樣（Yahoo，沒有就用 IBIT 代理；2026-10-23 拿掉 Coinbase） ─────────────────────────────
def test_btc_uses_the_ibit_proxy_when_yahoo_fails_and_the_card_says_so(env):
    """Coinbase 在台灣被封鎖，Yahoo 又常回 429：BTC 改用現貨比特幣 ETF 的 IBIT（和 GOLD→GLD 同一條路）。
    存的值是 ETF 股價，卡上只能寫單日漲跌幅，不能把它印成 BTC 價格。"""
    env.http.route("finance.yahoo.com", market_http.FetchError("rate_limited"))
    route_proxies(env, lambda etf: [(date(2026, 10, 14), 47.84), (D, 46.06)] if etf == "IBIT" else full_week(20.0))
    env.store.put_obs(tw.OBS_FX, {"date": "2026-10-15", "usd_ntd": 32.3}, data_date="2026-10-15")
    nasdaq_published_d(env)
    assert run(make_ctx(env, NOW_AFTER_D), "macro.yahoo") == "partial"
    btc = payload(env, "macro.BTC")
    assert btc["source"] == "ETF 代理：IBIT" and btc["symbol"] == "IBIT" and btc["date"] == "2026-10-15"
    assert btc["value"] == 46.06 and btc["chg"] == pytest.approx(-1.78) and btc["chg_pct"] == pytest.approx(-3.7207, abs=1e-3)
    assert [urlsplit(url).path for url in env.http.calls].count("/api/quote/IBIT/historical") == 1
    assert env.http.count("coinbase") == 0
    import market_dashboard

    assert market_dashboard._macro_label("BTC", btc) == "比特幣（IBIT 代理）"
    value = market_dashboard._macro_value("BTC", btc)
    assert value == "單日 -3.72%" and "46" not in value  # 只寫漲跌幅，不印 ETF 的股價


def test_btc_has_no_task_of_its_own_and_no_coinbase_path():
    assert "macro.btc" not in TASKS
    for name in ("TASK_BTC", "OBS_BTC", "OBS_BTC_YAHOO", "MACRO_OBS_KEY", "_btc_run", "_btc_due"):
        assert not hasattr(misc, name), name
    assert "coinbase_spot" not in misc._DEFAULT_EVERY_S


def test_a_day_recorded_before_the_ibit_proxy_gets_one_catch_up_fetch_for_btc(env):
    """部署前這個 D 已經跑過宏觀：BTC 以前不走代理，紀錄裡既沒有來源也不在 pending。補抓一次 IBIT，之後不再排。"""
    route_proxies(env, lambda etf: [(date(2026, 10, 14), 47.84), (D, 46.06)] if etf == "IBIT" else full_week(20.0))
    nasdaq_published_d(env)
    legacy = {k: f"ETF 代理：{etf}" for k, etf in PROXIES.items() if k != "BTC"}
    misc._save(env.store, misc.TASK_MACRO, {"d": D.isoformat(), "yahoo": "rate_limited", "pending": {}, "sources": legacy})
    ctx = make_ctx(env, NOW_AFTER_D)
    assert misc.macro_plan(ctx) == {"d": D, "yahoo": False, "keys": ["BTC"]}
    assert run(ctx, "macro.yahoo") == "ok"
    assert [urlsplit(url).path for url in env.http.calls if "nasdaq" in url] == ["/api/quote/IBIT/historical"]
    assert payload(env, "macro.BTC")["source"] == "ETF 代理：IBIT"
    assert misc._state(env.store, misc.TASK_MACRO)["sources"]["BTC"] == "ETF 代理：IBIT"
    assert misc.macro_plan(make_ctx(env, NOW_AFTER_D + 3 * 3600)) is None


# ── 新聞 ─────────────────────────────────────────────────────────────────────────
def test_news_keeps_36_hours_and_cleans_titles(env):
    now = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
    env.http.route("news.google.com", news_rss([
        ("美股收黑 那指摔逾2% - 甲報", now - timedelta(hours=2), "甲報"),
        ("x @all y", now - timedelta(hours=3), "乙報"),
        ("太舊的新聞", now - timedelta(hours=40), "丙報"),
        ("看這裡 https://evil.example/a", now - timedelta(hours=1), "丁報"),
    ]))
    assert run(make_ctx(env, now.timestamp()), "news.us") == "ok"
    items = payload(env, "news.us")["items"]
    # 「@」被換掉（來源模組換成全形空白、這裡再洗一次 NFKC 又變回半形空白）：不會變成 LINE 的提及
    assert [item["title"] for item in items] == ["美股收黑 那指摔逾2%", "x all y"]
    assert items[0]["source"] == "甲報" and items[0]["published_utc"].startswith("2026-10-16T00:00")
    assert env.store.get_obs("news.us")["data_date"] == "2026-10-16"
    assert run(make_ctx(env, now.timestamp() + 1799), "news.us") == "not_due"
    assert run(make_ctx(env, now.timestamp() + 1800), "news.us") == "ok"
    # 伊朗一小時一次
    assert run(make_ctx(env, now.timestamp()), "news.iran") == "ok"
    assert run(make_ctx(env, now.timestamp() + 1800), "news.iran") == "not_due"


def test_news_items_drop_stale_future_and_naive_dates():
    now = datetime(2026, 10, 16, 2, 0, tzinfo=UTC)
    items = [
        SimpleNamespace(title="新", source="甲", published_utc=now - timedelta(hours=1)),
        SimpleNamespace(title="舊", source="甲", published_utc=now - timedelta(hours=36, seconds=1)),
        SimpleNamespace(title="未來", source="甲", published_utc=now + timedelta(hours=1)),
        SimpleNamespace(title="沒時區", source="甲", published_utc=datetime(2026, 10, 16, 1, 0)),
        SimpleNamespace(title="新", source="甲", published_utc=now - timedelta(hours=2)),  # 同一則
        SimpleNamespace(title="\u202e", source="甲", published_utc=now),
    ]
    assert [item["title"] for item in misc.news_items(items, now)] == ["新"]


def test_google_news_503_cools_the_host_down_without_more_requests(env, monkeypatch):
    import safe_fetch

    status = {"code": 503}
    hits: list[str] = []
    rss = news_rss([("台股收紅 - 甲報", datetime(2026, 10, 16, 1, 0, tzinfo=UTC), "甲報")]).encode()

    def fake_fetch(url, **kwargs):
        hits.append(url)
        return SimpleNamespace(status_code=status["code"], headers={"Content-Type": "application/rss+xml"}, content=rss)

    monkeypatch.setattr(safe_fetch, "fetch", fake_fetch)
    t0 = datetime(2026, 10, 16, 2, 0, tzinfo=UTC).timestamp()
    assert run(make_ctx(env, t0, http=market_http), "news.tw") == "rate_limited"
    assert env.store.fetch_counts(now=t0)["news.google.com"]["cooldown_until"] == pytest.approx(t0 + 7200)
    assert env.store.get_obs("news.tw")["error"] == "rate_limited"
    # 同一主機的其他新聞也不再打：帳本冷卻中，請求根本沒送出
    status["code"] = 200
    assert run(make_ctx(env, t0 + 60, http=market_http), "news.us") == "cooldown"
    assert len(hits) == 1
    assert misc._news_fns("tw")[0](make_ctx(env, t0 + 1800, http=market_http)) is False
    # 兩小時後冷卻結束
    assert run(make_ctx(env, t0 + 7201, http=market_http), "news.tw") == "ok"
    assert payload(env, "news.tw")["items"][0]["title"] == "台股收紅"
    assert len(hits) == 2


# ── 川普原文：依 originalId 累積、「（至少）」 ──────────────────────────────────────────────
def test_trump_posts_accumulate_by_original_id_and_flag_gaps(env):
    t0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    a = ("100000000000000001", t0 - timedelta(hours=47), "Iran must stop now", False)
    b = ("100000000000000002", t0 - timedelta(hours=5), "Big tariffs are coming", False)
    c = ("100000000000000003", t0 + timedelta(hours=1), "Tariffs on chips work", False)
    d = ("100000000000000004", t0 + timedelta(hours=2), "Thank you Ohio", False)
    feed = {"posts": [a, b]}
    env.http.route("trumpstruth", lambda url: trump_rss(feed["posts"]))

    def summary() -> dict:
        return payload(env, "trump.summary")

    assert run(make_ctx(env, t0.timestamp()), "trump.feed") == "ok"
    assert summary()["topics"] == {"iran": 1, "tariff": 1}
    assert summary()["labels"] == {"iran": "伊朗", "tariff": "關稅"}
    assert summary()["at_least"] is True  # 剛上線：最舊那則之前不知道
    # 30 分鐘後只剩最新一則還在 feed（中間被洗掉、沒有交集）→ 有缺口；前面累積的照算
    feed["posts"] = [c]
    assert run(make_ctx(env, (t0 + timedelta(hours=2)).timestamp()), "trump.feed") == "ok"
    assert summary()["topics"] == {"chips": 1, "tariff": 2}  # b 從累積資料來；a 已超過 48 小時
    assert summary()["at_least"] is True
    assert {post["id"] for post in env.store.posts("trump", since_utc=t0 - timedelta(days=3))} == {a[0], b[0], c[0]}
    # b 被移除：之前累積到的也拿掉；沒有主題的貼文只算則數
    feed["posts"] = [(b[0], b[1], "", True), c, d]
    assert run(make_ctx(env, (t0 + timedelta(hours=2, minutes=30)).timestamp()), "trump.feed") == "ok"
    assert summary()["topics"] == {"chips": 1, "tariff": 1}
    assert summary()["posts"] == 2 and summary()["matched"] == 1
    details = payload(env, "trump.posts")["items"]
    assert [item["id"] for item in details] == [c[0]] and details[0]["text"] == "Tariffs on chips work"
    # 缺口（b 到 c 之間）滑出 48 小時後就不是「至少」
    later = (t0 + timedelta(hours=1 + 48, minutes=30)).timestamp()
    feed["posts"] = [c, d]
    assert run(make_ctx(env, later), "trump.feed") == "ok"
    assert summary()["at_least"] is False
    # Mac 睡了 3 天：這輪最舊一則比上輪最新一則還新 → 又是「至少」
    e = ("100000000000000005", t0 + timedelta(days=4), "Taiwan chips", False)
    feed["posts"] = [e]
    assert run(make_ctx(env, (t0 + timedelta(days=4, hours=1)).timestamp()), "trump.feed") == "ok"
    assert summary()["at_least"] is True and summary()["topics"] == {"chips": 1, "taiwan": 1}


def test_coverage_and_at_least_rules():
    state: dict = {}
    assert misc.at_least(state, 1000.0) is True  # 從沒成功過
    misc.update_coverage(state, [], 1000.0)
    assert state == {}
    hour = 3600.0
    misc.update_coverage(state, [10 * hour, 20 * hour], 30 * hour)
    assert state["gaps"] == [[None, 10 * hour]] and misc.at_least(state, 30 * hour)
    misc.update_coverage(state, [15 * hour, 25 * hour], 58.5 * hour)  # 有交集：沒有新缺口；舊的滑出 48 小時
    assert state["gaps"] == [] and misc.at_least(state, 58.5 * hour) is False
    misc.update_coverage(state, [40 * hour], 60 * hour)
    assert state["gaps"] == [[25 * hour, 40 * hour]] and misc.at_least(state, 60 * hour)


def test_reaches_back_only_counts_when_there_is_no_anchor_inside_the_window():
    """2026-10-23 審查 r7b F1：feed 比視窗起點更舊（reaches_back）＝這一份自己就涵蓋了整個 48 小時，但只在沒有
    可接的錨點（剛上線、或上輪最新一則已滑出視窗）時採信；錨點還在視窗內，一則置頂的舊 item 不能蓋掉真缺口。"""
    hour = 3600.0
    # 剛上線，feed 比視窗（52h 起）還舊 → 整個視窗都涵蓋到了，不必標「（至少）」
    state: dict = {}
    misc.update_coverage(state, [60 * hour, 99 * hour], 100 * hour, reaches_back=True)
    assert state["gaps"] == [] and state["last_newest"] == 99 * hour and misc.at_least(state, 100 * hour) is False
    # 同樣的 feed 沒有更舊的 item → 最舊那則之前都不知道
    state = {}
    misc.update_coverage(state, [60 * hour, 99 * hour], 100 * hour)
    assert state["gaps"] == [[None, 60 * hour]] and misc.at_least(state, 100 * hour)
    # 錨點（上輪最新 90h）還在視窗內：這輪最舊 95h 比它新＝缺口；reaches_back 不採信
    state = {"last_newest": 90 * hour, "gaps": []}
    misc.update_coverage(state, [95 * hour, 99 * hour], 100 * hour, reaches_back=True)
    assert state["gaps"] == [[90 * hour, 95 * hour]] and misc.at_least(state, 100 * hour)
    # 錨點已滑出視窗（10h < 52h）：接不上，要靠這份 feed 自己涵蓋——涵蓋到就沒有缺口，涵蓋不到就記缺口
    state = {"last_newest": 10 * hour, "gaps": []}
    misc.update_coverage(state, [60 * hour, 99 * hour], 100 * hour, reaches_back=True)
    assert state["gaps"] == [] and state["last_newest"] == 99 * hour
    state = {"last_newest": 10 * hour, "gaps": []}
    misc.update_coverage(state, [60 * hour, 99 * hour], 100 * hour)
    assert state["gaps"] == [[10 * hour, 60 * hour]] and misc.at_least(state, 100 * hour)


def test_a_last_newest_in_the_future_is_discarded_instead_of_hiding_gaps_forever():
    """舊版沒擋時間打錯的 item，last_newest 可能已被拉到未來；max() 退不回來，缺口就永遠標不出來。"""
    hour = 3600.0
    state = {"last_newest": 1000 * hour, "gaps": []}
    misc.update_coverage(state, [95 * hour, 99 * hour], 100 * hour)
    assert state["last_newest"] == 99 * hour and state["gaps"] == [[None, 95 * hour]]
    assert misc.at_least(state, 100 * hour)


# ── 期中選舉：每 2 小時；結果出來後每天、7 天後停（2026-10-23 起台灣封鎖：永遠不到期） ──────────────────────────
@pytest.fixture
def unblocked(monkeypatch):
    """排程與解析的邏輯仍要測：把封鎖旗標關掉（正式環境是 True）。"""
    import market_sources_misc

    monkeypatch.setattr(market_sources_misc, "PREDICTION_MARKETS_BLOCKED", False)


ELECTION_TASKS = [f"election.{key}" for key in ("polymarket.house", "kalshi.house", "polymarket.senate", "kalshi.senate")]


def test_election_tasks_are_never_due_while_the_prediction_markets_are_blocked(env):
    """Polymarket、Kalshi 被台灣的 ISP 依法院命令封鎖（2026-10-21）：任務留在清單裡，但永遠不到期、一個請求都不送。"""
    import market_sources_misc as src

    assert src.PREDICTION_MARKETS_BLOCKED is True
    env.http.route("gamma-api.polymarket.com", polymarket_event())
    env.http.route("api.elections.kalshi.com", {"cursor": "", "markets": []})
    assert set(ELECTION_TASKS) <= set(TASKS)
    t0 = at(2026, 10, 24, 10, 0)
    for offset in (0, 7200, 86400, 40 * 86400):  # 從沒抓過、2 小時後、一天後、投票之後
        for name in ELECTION_TASKS:
            assert run(make_ctx(env, t0 + offset), name) == "not_due", (name, offset)
    # 結果出來後「每天一次」的節奏也不例外
    for name in ELECTION_TASKS:
        misc._save(env.store, name, {"resolved_at": t0 - 2 * 86400, "last_ok": t0 - 2 * 86400})
        assert run(make_ctx(env, t0), name) == "not_due"
    assert env.http.calls == [] and env.store.get_obs("election.polymarket.house") is None


@pytest.mark.parametrize("value", [True, 1, 0, None, "no"])
def test_only_an_explicit_false_unblocks_the_election_tasks(env, monkeypatch, value):
    import market_sources_misc as src

    monkeypatch.setattr(src, "PREDICTION_MARKETS_BLOCKED", value)
    env.http.route("gamma-api.polymarket.com", polymarket_event())
    assert run(make_ctx(env, at(2026, 10, 24, 10, 0)), "election.polymarket.house") == "not_due"
    assert env.http.calls == []


def test_election_tasks_are_due_again_once_the_flag_is_false(env, unblocked):
    env.http.route("gamma-api.polymarket.com", polymarket_event())
    assert run(make_ctx(env, at(2026, 10, 24, 10, 0)), "election.polymarket.house") == "ok"
    assert env.http.count("polymarket") == 1


def test_blocking_clears_the_old_failure_streak_so_maintenance_stops_reporting_it(env):
    """封鎖前這些任務已經連續失敗（kind=network）：框架留在 task_state 的次數不會自己歸零，每日維護會天天當失敗報。
    第一次看到封鎖時清掉失敗次數與退避時間（其他欄位不動）；還在退避中就先等退避結束。"""
    name = "election.polymarket.house"
    t0 = at(2026, 10, 24, 10, 0)
    env.store.set_task_state(name, {"fail_streak": 6, "next_try": t0 + 600, "last_kind": "network", "last_success": 123.0})
    assert run(make_ctx(env, t0), name) == "backoff"
    assert env.store.task_state(name)["fail_streak"] == 6
    assert run(make_ctx(env, t0 + 601), name) == "not_due"
    state = env.store.task_state(name)
    assert state["fail_streak"] == 0 and state["next_try"] is None
    assert state["last_kind"] == "network" and state["last_success"] == 123.0
    assert env.http.calls == []


def test_prediction_markets_poll_every_two_hours_then_daily_after_the_result(env, unblocked):
    event = {"value": polymarket_event()}
    env.http.route("gamma-api.polymarket.com", lambda url: event["value"])
    env.http.route("api.elections.kalshi.com", {"cursor": "", "markets": [{
        "ticker": "CONTROLH-2026-D", "status": "active", "result": "", "yes_sub_title": "Democratic Party",
        "title": "Will Democrats win the House in 2026?", "yes_bid_dollars": "0.9010", "yes_ask_dollars": "0.9020",
        "last_price_dollars": "0.9020",
    }]})
    t0 = at(2026, 10, 17, 10, 0)
    assert run(make_ctx(env, t0), "election.polymarket.house") == "ok"
    assert run(make_ctx(env, t0), "election.kalshi.house") == "ok"
    poly = payload(env, "election.polymarket.house")
    assert poly["mid"] == pytest.approx(0.905) and poly["last"] == 0.91 and poly["outcome"] == "Democratic Party"
    assert poly["updated_utc"].startswith("2026-10-17T02:37") and poly["result"] is None
    kalshi = payload(env, "election.kalshi.house")
    assert kalshi["mid"] == pytest.approx(0.9015) and kalshi["updated_utc"] is None
    assert run(make_ctx(env, t0 + 7199), "election.polymarket.house") == "not_due"
    assert run(make_ctx(env, t0 + 7200), "election.polymarket.house") == "ok"
    assert env.http.count("polymarket") == 2
    # 投票後結果出來：每天看一次，顯示 7 天後不再抓
    event["value"] = polymarket_event(closed=True, prices='["1", "0"]')
    resolved_at = at(2026, 11, 5, 10, 0)
    assert run(make_ctx(env, resolved_at), "election.polymarket.house") == "ok"
    assert payload(env, "election.polymarket.house")["result"] == "Yes"
    assert run(make_ctx(env, resolved_at + 7200), "election.polymarket.house") == "not_due"
    assert run(make_ctx(env, resolved_at + 86400), "election.polymarket.house") == "ok"
    assert run(make_ctx(env, resolved_at + 7 * 86400), "election.polymarket.house") == "not_due"
    assert run(make_ctx(env, resolved_at + 30 * 86400), "election.polymarket.house") == "not_due"


def test_every_fixed_market_including_the_senate_gets_its_own_task_and_obs(env, unblocked):
    import market_sources_misc

    def poly(url):
        slug = urlsplit(url).query.split("slug=", 1)[1]
        market_id = next(m.market_id for m in market_sources_misc.PREDICTION_MARKETS if m.event == slug)
        event = polymarket_event()
        event[0]["markets"][0]["id"] = market_id
        return event

    def kalshi(url):
        event = urlsplit(url).query.split("event_ticker=", 1)[1]
        ticker = next(m.market_id for m in market_sources_misc.PREDICTION_MARKETS if m.event == event)
        return {"cursor": "", "markets": [{"ticker": ticker, "status": "active", "result": "",
                                           "yes_sub_title": "Democratic Party", "yes_bid_dollars": "0.4000",
                                           "yes_ask_dollars": "0.4200", "last_price_dollars": "0.4100"}]}

    env.http.route("gamma-api.polymarket.com", poly)
    env.http.route("api.elections.kalshi.com", kalshi)
    t0 = at(2026, 10, 17, 10, 0)
    for market in market_sources_misc.PREDICTION_MARKETS:
        assert run(make_ctx(env, t0), f"election.{market.key}") == "ok"
        quote = payload(env, f"election.{market.key}")
        assert quote["market_id"] == market.market_id and quote["platform"] == market.platform
    assert env.http.count("polymarket") + env.http.count("kalshi") == len(market_sources_misc.PREDICTION_MARKETS)
    assert {"polymarket.senate", "kalshi.senate"} <= {m.key for m in market_sources_misc.PREDICTION_MARKETS}
    assert payload(env, "election.kalshi.senate")["mid"] == pytest.approx(0.41)
    assert payload(env, "election.polymarket.senate")["market_id"] == "562793"


def test_misc_reads_the_tw_fx_obs_without_importing_the_tw_pipeline():
    import subprocess
    import sys

    assert misc.OBS_TW_FX == tw.OBS_FX
    code = "import sys, market_pipeline_misc; print('market_pipeline_tw' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(misc.__file__).parent, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_schedule_intervals_come_from_the_source_table_not_the_fallback():
    """接縫（2026-10-18）：_every／_every_or 的預設值只是保險；實際要用 market_sources_misc.ENDPOINTS。"""
    import market_sources_misc as src

    for name in misc._DEFAULT_EVERY_S:
        assert misc._every(name) == float(src.ENDPOINTS[name].every_s), name
    for market in src.PREDICTION_MARKETS:
        assert misc._every_or(market.key, -1.0) == float(src.ENDPOINTS[market.key].every_s), market.key


def test_trump_coverage_counts_image_only_posts(env):
    """2026-10-18 審查 r6c N1：只有圖的貼文不進 posts，但還在 feed 裡；有沒有漏要看整份 feed 的頭尾，
    不然 K0、K1 捲出、只剩只有圖的 M2 時，會誤判成中間有缺口、多標「（至少）」。"""
    t0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    k0 = ("100000000000000010", t0 - timedelta(hours=3), "Iran talks", False)
    k1 = ("100000000000000011", t0 - timedelta(hours=2), "Tariffs today", False)
    m2 = ("100000000000000012", t0 - timedelta(hours=1), "", False)  # 只有圖：沒有本人文字
    k3 = ("100000000000000013", t0 + timedelta(minutes=20), "Fed must cut", False)
    feed = {"posts": [m2, k1, k0]}
    env.http.route("trumpstruth", lambda url: trump_rss(feed["posts"]))
    assert run(make_ctx(env, t0.timestamp()), "trump.feed") == "ok"
    feed["posts"] = [k3, m2]  # K0、K1 捲出，但 M2 兩輪都在：沒有漏
    assert run(make_ctx(env, (t0 + timedelta(minutes=31)).timestamp()), "trump.feed") == "ok"
    gaps = misc._state(env.store, misc.TASK_TRUMP).get("gaps") or []
    assert [gap for gap in gaps if gap[0] is not None] == []  # 只有剛上線那段「之前不知道」，沒有新缺口


# ── 川普缺口偵測的界線（2026-10-23 審查 r7b F1 的 T1–T3） ───────────────────────────────────
T0_TRUMP = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def trump_post(number: int, when: datetime, text: str = "", removed: bool = False) -> tuple:
    return (f"1000000000000001{number:02d}", when, text, removed)


def trump_state(env) -> dict:
    return misc._state(env.store, misc.TASK_TRUMP)


def trump_feed(env, posts: list) -> dict:
    feed = {"posts": posts}
    env.http.route("trumpstruth", lambda url: trump_rss(feed["posts"]))
    return feed


@pytest.mark.parametrize("later_hours", [1, 50])
def test_trump_feeds_without_overlap_still_mark_the_gap(env, later_hours):
    """T1：兩輪 feed 沒有交集（K0、K1 捲出，只剩更新的 K3）→ 中間可能漏了，缺口記下來、標「（至少）」。
    1 小時後上輪最新一則還在視窗內；50 小時後它已滑出視窗、剛上線那段也過期，只剩這個缺口讓 at_least 為真。"""
    k0, k1 = trump_post(0, T0_TRUMP - timedelta(hours=3), "Iran talks"), trump_post(1, T0_TRUMP - timedelta(hours=2), "Tariffs")
    k3 = trump_post(3, T0_TRUMP + timedelta(hours=later_hours) - timedelta(minutes=30), "Fed must cut")
    feed = trump_feed(env, [k1, k0])
    assert run(make_ctx(env, T0_TRUMP.timestamp()), "trump.feed") == "ok"
    feed["posts"] = [k3]
    assert run(make_ctx(env, (T0_TRUMP + timedelta(hours=later_hours)).timestamp()), "trump.feed") == "ok"
    assert [k1[1].timestamp(), k3[1].timestamp()] in trump_state(env)["gaps"]
    assert trump_state(env)["last_newest"] == k3[1].timestamp()
    assert payload(env, "trump.summary")["at_least"] is True


def test_trump_future_dated_item_does_not_move_last_newest_or_hide_a_gap(env):
    """T2：一則時間打成未來（> now + 5 分鐘）的 item 不能把 last_newest 拉過去（max() 退不回來）、讓後面真的缺口標不出來。"""
    k0, k1 = trump_post(0, T0_TRUMP - timedelta(hours=3), "Iran talks"), trump_post(1, T0_TRUMP - timedelta(hours=2), "Tariffs")
    typo = trump_post(9, T0_TRUMP + timedelta(days=10), "typo in the year")
    k3 = trump_post(3, T0_TRUMP + timedelta(hours=49, minutes=30), "Fed must cut")
    feed = trump_feed(env, [typo, k1, k0])
    assert run(make_ctx(env, T0_TRUMP.timestamp()), "trump.feed") == "ok"
    assert trump_state(env)["last_newest"] == k1[1].timestamp()  # 不是未來那則
    assert payload(env, "trump.summary")["posts"] == 2  # 解析端本來就把它丟掉
    feed["posts"] = [k3]  # 50 小時後（剛上線那段已過期）：和上輪沒有交集，真的缺口要標得出來
    assert run(make_ctx(env, (T0_TRUMP + timedelta(hours=50)).timestamp()), "trump.feed") == "ok"
    assert trump_state(env)["gaps"] == [[k1[1].timestamp(), k3[1].timestamp()]]
    assert payload(env, "trump.summary")["at_least"] is True


def test_trump_very_old_pinned_item_does_not_hide_a_gap(env):
    """T3a：feed 裡一則很舊的 item（置頂、被移除的空殼用原始時間）不能把「最舊一則」拉到上輪最新之前、蓋掉真缺口。"""
    k0, k1 = trump_post(0, T0_TRUMP - timedelta(hours=3), "Iran talks"), trump_post(1, T0_TRUMP - timedelta(hours=2), "Tariffs")
    k3 = trump_post(3, T0_TRUMP + timedelta(minutes=30), "Fed must cut")
    pinned = trump_post(8, T0_TRUMP - timedelta(days=30), "", True)
    feed = trump_feed(env, [k1, k0])
    assert run(make_ctx(env, T0_TRUMP.timestamp()), "trump.feed") == "ok"
    feed["posts"] = [k3, pinned]  # K1 捲出；沒有交集，只有一則 30 天前的空殼
    assert run(make_ctx(env, (T0_TRUMP + timedelta(hours=1)).timestamp()), "trump.feed") == "ok"
    assert [k1[1].timestamp(), k3[1].timestamp()] in trump_state(env)["gaps"]
    assert payload(env, "trump.summary")["at_least"] is True


def test_trump_first_success_with_a_feed_older_than_48_hours_is_not_at_least(env):
    """T3b：剛上線，feed 最舊的 item 已經超過 48 小時 → 整個視窗都涵蓋到了，不標「（至少）」；
    48 小時都沒新貼文（feed 整份都比視窗舊）也一樣，之後第一則新貼文進來不會被誤判成有缺口。"""
    a = trump_post(0, T0_TRUMP - timedelta(hours=60), "Iran talks")
    b = trump_post(1, T0_TRUMP - timedelta(hours=30), "Tariffs")
    c = trump_post(2, T0_TRUMP - timedelta(hours=1), "Fed must cut")
    trump_feed(env, [c, b, a])
    assert run(make_ctx(env, T0_TRUMP.timestamp()), "trump.feed") == "ok"
    assert payload(env, "trump.summary")["at_least"] is False and trump_state(env)["gaps"] == []
    assert payload(env, "trump.summary")["topics"] == {"fed": 1, "tariff": 1}


def test_trump_quiet_for_48_hours_does_not_mark_a_gap_when_the_next_post_arrives(env):
    older, old = trump_post(0, T0_TRUMP - timedelta(hours=90), "Iran talks"), trump_post(1, T0_TRUMP - timedelta(hours=60), "Tariffs")
    fresh = trump_post(2, T0_TRUMP + timedelta(hours=1), "Fed must cut")
    feed = trump_feed(env, [old, older])
    assert run(make_ctx(env, T0_TRUMP.timestamp()), "trump.feed") == "ok"
    assert payload(env, "trump.summary")["at_least"] is False and payload(env, "trump.summary")["posts"] == 0
    assert trump_state(env)["last_newest"] == old[1].timestamp()
    feed["posts"] = [fresh, old, older]  # 舊的 item 還在 feed 裡：前後接得上
    assert run(make_ctx(env, (T0_TRUMP + timedelta(hours=2)).timestamp()), "trump.feed") == "ok"
    assert trump_state(env)["gaps"] == [] and payload(env, "trump.summary")["at_least"] is False
    assert payload(env, "trump.summary")["topics"] == {"fed": 1}


def test_trump_feed_of_only_image_posts_still_updates_the_coverage(env):
    """T3c：整份 feed 都是只有圖的貼文（沒有一則進 posts）時，缺口紀錄照樣更新，下一輪不會誤判成有缺口。"""
    m1, m2 = trump_post(0, T0_TRUMP - timedelta(hours=2)), trump_post(1, T0_TRUMP - timedelta(hours=1))
    m3 = trump_post(2, T0_TRUMP + timedelta(minutes=10))
    feed = trump_feed(env, [m2, m1])
    assert run(make_ctx(env, T0_TRUMP.timestamp()), "trump.feed") == "ok"
    assert payload(env, "trump.summary")["posts"] == 0
    assert trump_state(env)["last_newest"] == m2[1].timestamp() and trump_state(env)["gaps"] == [[None, m1[1].timestamp()]]
    feed["posts"] = [m3, m2, m1]
    assert run(make_ctx(env, (T0_TRUMP + timedelta(minutes=30)).timestamp()), "trump.feed") == "ok"
    assert trump_state(env)["last_newest"] == m3[1].timestamp() and trump_state(env)["gaps"] == [[None, m1[1].timestamp()]]


def test_macro_gate_watches_the_same_etfs_as_us_hist():
    """2026-10-23 審查 r9 gp2：代理等的「Nasdaq 公布 D 了沒」和 market_pipeline_us 追的 ETF 要是同一組，改一邊另一邊不會知道。"""
    assert misc.GATE_SYMBOLS == market_pipeline_us.ETFS


def test_trump_window_and_skew_match_the_source_module():
    """2026-10-23 審查 r9：川普的 48 小時窗口與 5 分鐘未來容差在 pipeline 與 sources 各有一份，改一邊另一邊不會知道。"""
    import market_sources_misc

    assert misc.TRUMP_WINDOW_H == market_sources_misc.TRUMP_WINDOW_H
    assert misc.TRUMP_FUTURE_SKEW_S == market_sources_misc.FUTURE_SKEW.total_seconds()
