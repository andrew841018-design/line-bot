"""stock_picks 的抓資料、解析、快照、背景工作與 handle() 測試（2026-10-10）。

不連網：autouse fixture 把 requests.get 換成一呼叫就記下來並 raise 的替身，測試結束時檢查
沒有人呼叫過；需要資料的測試換 stock_picks._get_json（合成的證交所／櫃買／Yahoo JSON）。
狀態檔寫在 tmp 目錄；背景執行緒只在明確測它的地方起，而且 stop 一定會收掉。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote

import pytest
import requests

import stock_picks as sp

T = date(2026, 10, 8)
_REAL_GET_JSON = sp._get_json  # conftest 每個測試都把它換成擋網路的替身；直接測它時用這個


def _business_days(n: int, end: date = T) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


CAL = _business_days(300)
PREV = CAL[-2]


def _ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 13, 30, tzinfo=sp._TPE).timestamp())


def _chart(days, closes, meta=None) -> dict:
    return {
        "chart": {
            "result": [
                {"meta": meta or {}, "timestamp": [_ts(d) for d in days], "indicators": {"quote": [{"close": list(closes)}]}}
            ]
        }
    }


def _roc(day: date) -> str:
    return f"{day.year - 1911}{day.month:02d}{day.day:02d}"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    attempts: list[str] = []

    def _blocked(*args, **_kwargs):
        attempts.append(str(args[:1]))
        raise AssertionError("stock_picks tests must not touch the network")

    monkeypatch.setattr(sp.requests, "get", _blocked)
    monkeypatch.setattr(sp, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", False)
    monkeypatch.setattr(sp, "T86_SPACING_S", 0.0)
    monkeypatch.setattr(sp, "_snapshot", None)
    monkeypatch.setattr(sp, "_worker", sp.WorkerState())
    monkeypatch.setattr(sp, "_loaded", True)
    monkeypatch.setattr(sp, "_thread", None)
    monkeypatch.setattr(sp, "_stop", None)
    monkeypatch.setattr(sp, "_wake", None)
    sp.clear_caches()
    yield attempts
    sp.stop_background(timeout=2.0)
    sp.clear_caches()
    assert attempts == [], f"network was attempted: {attempts}"


# ── 合成市場 ───────────────────────────────────────────────────────────────
STOCKS = ["2330"] + [f"{1101 + i}" for i in range(59)]
INDUSTRIES = ["01", "02", "03", "04", "05", "06"]
OTC_CODES = [f"{6000 + i}" for i in range(320)]


class FakeNet:
    """照主機與路徑回合成資料；記下每次呼叫（含 If-Modified-Since）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.day_rows = [
            {"Date": _roc(T), "Code": code, "Name": f"股{code}", "ClosingPrice": "100.00", "Change": "0.0000"}
            for code in STOCKS
        ] + [{"Date": _roc(T), "Code": "0050", "Name": "元大台灣50", "ClosingPrice": "100.00", "Change": "0.0000"}]
        self.companies = [
            {
                "公司代號": code, "公司簡稱": f"股{code}", "產業別": INDUSTRIES[i % 6],
                "已發行普通股數或TDR原股發行股數": str((70 - i) * 1_000_000_000), "董事長": "不該被讀",
            }
            for i, code in enumerate(STOCKS)
        ]
        self.pe = [{"Date": _roc(T), "Code": code, "PEratio": f"{10 + i % 10}"} for i, code in enumerate(STOCKS)]
        self.revenue = [
            {"資料年月": "11508", "公司代號": code, "營業收入-去年同月增減(%)": "10.0", "累計營業收入-前期比較增減(%)": "5.0"}
            for code in STOCKS
        ]
        self.t86 = {
            d.strftime("%Y%m%d"): {
                "stat": "OK", "date": d.strftime("%Y%m%d"),
                "fields": ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數"],
                "data": [[code, "x", "1,000,000" if i % 2 == 0 else "-1,000,000", "0"] for i, code in enumerate(STOCKS)],
            }
            for d in CAL[-10:]
        }
        self.charts = {f"{code}.TW": _chart(CAL[:-1], [100.0] * (len(CAL) - 1)) for code in STOCKS + ["0050"]}
        self.charts["^TWII"] = _chart(CAL, [20000.0] * len(CAL))
        self.otc = [
            {"Date": _roc(T), "SecuritiesCompanyCode": code, "CompanyName": f"櫃{code}", "Close": "50.00", "Change": "0.00 "}
            for code in OTC_CODES
        ]
        self.charts.update({f"{code}.TWO": _chart(CAL[:-1], [50.0] * (len(CAL) - 1)) for code in OTC_CODES[:3]})
        # 證交所大盤（本月每個交易日；預設和 ^TWII 一樣平盤 20000，Yahoo 預設有 T 所以不會抓）
        self.fmtqik = [
            {"Date": _roc(d), "TAIEX": "20000.00", "Change": "0.00"} for d in CAL if (d.year, d.month) == (T.year, T.month)
        ]
        self.overrides: dict = {}
        self.limits: list = []  # (端點, 單次讀取 timeout, 總時間)

    def __call__(self, host, path, *, params=None, max_bytes, timeout, total_s, if_modified_since=None, stop=None):
        assert max_bytes > 0 and timeout > 0 and total_s > 0
        self.calls.append((host, path, dict(params or {}), if_modified_since))
        self.limits.append((path.rsplit("/", 1)[-1], timeout, total_s))
        key = path.rsplit("/", 1)[-1]
        if key in self.overrides:
            value = self.overrides[key]
            if isinstance(value, Exception):
                raise value
            return value
        if host in sp._YAHOO:
            symbol = unquote(key)
            data = self.charts.get(symbol)
            if data is None:
                raise sp.FetchError("status")
            return 200, data, None
        if key == "STOCK_DAY_ALL":
            return 200, self.day_rows, "Fri, 09 Oct 2026 21:20:43 GMT"
        if key == "t187ap03_L":
            return 200, self.companies, None
        if key == "BWIBBU_ALL":
            return 200, self.pe, None
        if key == "t187ap05_L":
            return 200, self.revenue, None
        if key == "T86":
            return 200, self.t86.get(params["date"], {"stat": "很抱歉，沒有符合條件的資料!"}), None
        if key == "tpex_mainboard_daily_close_quotes":
            return 200, self.otc, None
        if key == "FMTQIK":
            return 200, self.fmtqik, None
        raise AssertionError(f"unexpected fetch {host}{path}")

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[1].endswith(name))


@pytest.fixture
def net(monkeypatch) -> FakeNet:
    fake = FakeNet()
    monkeypatch.setattr(sp, "_get_json", fake)
    monkeypatch.setattr(sp, "_MIN_ROWS", 50)  # 真實資料 1000 多筆；合成市場 60 家
    return fake


def _day_rows(fake: FakeNet):
    t, rows = sp.parse_day_all(fake.day_rows)
    assert t == T
    return rows


def _fresh_snapshot(net: FakeNet) -> sp.Snapshot:
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=time.time())
    sp._commit_snapshot(snapshot)
    sp._worker.last_ok_check = time.time()
    sp._worker.seen_t = T
    return snapshot


# ── _get_json（替身 requests.get） ──────────────────────────────────────────
class _Resp:
    def __init__(self, status=200, body=b"{}", ctype="application/json", headers=None, chunks=None, raise_on_iter=None):
        self.status_code = status
        self.headers = {"Content-Type": ctype, **(headers or {})}
        self._chunks = chunks if chunks is not None else [body]
        self._raise = raise_on_iter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size):
        assert chunk_size > 0
        if self._raise:
            raise self._raise
        yield from self._chunks


def _fake_get(monkeypatch, response=None, exc=None):
    seen = {}

    def get(url, **kwargs):
        seen["url"], seen["kwargs"] = url, kwargs
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(sp.requests, "get", get)
    return seen


def test_get_json_success_uses_safe_request_options(monkeypatch):
    seen = _fake_get(monkeypatch, _Resp(body=b'{"a": 1}', headers={"Last-Modified": "Fri, 09 Oct 2026 21:20:43 GMT"}))
    status, data, last_modified = _REAL_GET_JSON("openapi.twse.com.tw", "/v1/x", max_bytes=100, timeout=3, total_s=6)
    assert (status, data, last_modified) == (200, {"a": 1}, "Fri, 09 Oct 2026 21:20:43 GMT")
    assert seen["url"] == "https://openapi.twse.com.tw/v1/x"
    assert seen["kwargs"]["allow_redirects"] is False and seen["kwargs"]["stream"] is True
    assert "verify" not in seen["kwargs"]


def test_get_json_sends_if_modified_since_and_returns_304(monkeypatch):
    seen = _fake_get(monkeypatch, _Resp(status=304, headers={"Last-Modified": "x"}))
    assert _REAL_GET_JSON("h", "/p", max_bytes=10, timeout=1, total_s=2, if_modified_since="LM")[:2] == (304, None)
    assert seen["kwargs"]["headers"]["If-Modified-Since"] == "LM"


@pytest.mark.parametrize(
    "response, kind",
    [
        (_Resp(status=302), "moved"),
        (_Resp(status=301), "moved"),
        (_Resp(status=403), "blocked"),
        (_Resp(status=429), "rate_limited"),
        (_Resp(status=500), "status"),
        (_Resp(ctype="text/html"), "not_json"),
        (_Resp(body=b"<html>"), "not_json"),
        (_Resp(chunks=[b"x" * 60, b"x" * 60]), "too_big"),
    ],
)
def test_get_json_failures(monkeypatch, response, kind):
    _fake_get(monkeypatch, response)
    with pytest.raises(sp.FetchError) as info:
        _REAL_GET_JSON("h", "/p", max_bytes=100, timeout=1, total_s=2)
    assert info.value.kind == kind


@pytest.mark.parametrize(
    "exc, kind", [(requests.Timeout(), "timeout"), (requests.ConnectionError(), "network"), (requests.TooManyRedirects(), "network")]
)
def test_get_json_request_exceptions(monkeypatch, exc, kind):
    _fake_get(monkeypatch, exc=exc)
    with pytest.raises(sp.FetchError) as info:
        _REAL_GET_JSON("h", "/p", max_bytes=100, timeout=1, total_s=2)
    assert info.value.kind == kind


def test_get_json_stops_when_asked(monkeypatch):
    _fake_get(monkeypatch, _Resp(chunks=[b"{", b"}"]))
    stop = threading.Event()
    stop.set()
    with pytest.raises(sp.FetchError) as info:
        _REAL_GET_JSON("h", "/p", max_bytes=100, timeout=1, total_s=2, stop=stop)
    assert info.value.kind == "stopped"


def test_last_modified_is_validated_before_storing():
    assert sp._valid_last_modified("Fri, 09 Oct 2026 21:20:43 GMT") == "Fri, 09 Oct 2026 21:20:43 GMT"
    assert sp._valid_last_modified("Fri, 09 Oct 2026 21:20:43 GMT\r\nX-Evil: 1") is None
    assert sp._valid_last_modified(None) is None
    assert sp._valid_last_modified("yesterday") is None


def test_fetch_chart_whitelists_symbols_and_tries_second_host(monkeypatch):
    calls = []

    def fake(host, path, **kwargs):
        calls.append((host, path))
        if host == sp._YAHOO[0]:
            raise sp.FetchError("timeout")
        return 200, {"chart": {}}, None

    monkeypatch.setattr(sp, "_get_json", fake)
    assert sp._fetch_chart("../../etc") is None and calls == []
    assert sp._fetch_chart("2330.TW") == {"chart": {}}
    assert [c[0] for c in calls] == list(sp._YAHOO)
    assert calls[-1][1] == "/v8/finance/chart/2330.TW"
    calls.clear()
    assert sp._fetch_chart("^TWII") == {"chart": {}}
    assert calls[-1][1] == "/v8/finance/chart/%5ETWII"


def test_fetch_chart_rate_limit_aborts(monkeypatch):
    def fake(host, path, **kwargs):
        raise sp.FetchError("rate_limited")

    monkeypatch.setattr(sp, "_get_json", fake)
    with pytest.raises(sp.FetchError):
        sp._fetch_chart("2330.TW")


# ── 解析 ───────────────────────────────────────────────────────────────────
def test_parse_day_all_and_duplicates():
    rows = [
        {"Date": "1151008", "Code": "2330", "Name": "台積電", "ClosingPrice": "2,550.00", "Change": "-35.0000"},
        {"Date": "1151008", "Code": "2327", "Name": "國巨*", "ClosingPrice": "--", "Change": ""},
        {"Date": "1151008", "Code": "1101", "Name": "台泥", "ClosingPrice": "30.00", "Change": "0"},
        {"Date": "1151008", "Code": "1101", "Name": "台泥", "ClosingPrice": "31.00", "Change": "0"},
        {"Date": "1151008", "Code": "1102", "Name": "亞泥", "ClosingPrice": "40.00", "Change": "0"},
        {"Date": "1151008", "Code": "1102", "Name": "亞泥", "ClosingPrice": "40.00", "Change": "0"},
        {"Date": "1151008", "Code": "../x", "Name": "壞", "ClosingPrice": "1", "Change": "0"},
        "not a row",
    ]
    t, table = sp.parse_day_all(rows)
    assert t == T
    assert table["2330"] == sp.DayRow("台積電", 2550.0, -35.0)
    assert table["2327"] == sp.DayRow("國巨", None, None)  # 名稱去「*」、沒有收盤
    assert "1101" not in table  # 同代號兩筆不一樣 → 不收
    assert table["1102"].close == 40.0  # 一模一樣的重複沒關係
    mixed = rows + [{"Date": "1151007", "Code": "2603", "Name": "長榮", "ClosingPrice": "1", "Change": "0"}]
    assert sp.parse_day_all(mixed)[0] is None
    assert sp.parse_day_all("x") == (None, {})


def test_parse_companies_reads_only_needed_fields():
    rows = [
        {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24", "已發行普通股數或TDR原股發行股數": "25932370067", "董事長": "某人"},
        {"公司代號": "9103", "公司簡稱": "名字<b>", "產業別": "91", "已發行普通股數或TDR原股發行股數": "1"},
        {"公司代號": "9105", "公司簡稱": "美德醫療-DR", "產業別": "91", "已發行普通股數或TDR原股發行股數": "1"},
        {"公司代號": "1234", "公司簡稱": "壞產業", "產業別": "超過四個字的產業", "已發行普通股數或TDR原股發行股數": "1"},
    ]
    table = sp.parse_companies(rows)
    assert table["2330"] == sp.Company("台積電", "24", 25932370067.0)
    assert table["9103"].name == "" and table["9103"].industry == "91"  # 名稱有白名單外的字元：只顯示代號
    assert table["9105"].name == "美德醫療-DR"
    assert "1234" not in table
    assert not hasattr(table["2330"], "董事長")


def test_parse_pe_and_conflicts():
    rows = [
        {"Date": "1151008", "Code": "2330", "PEratio": "29.56"},
        {"Date": "1151008", "Code": "2603", "PEratio": "-"},
        {"Date": "1151008", "Code": "1101", "PEratio": "10"},
        {"Date": "1151008", "Code": "1101", "PEratio": "11"},
        {"Date": "1151008", "Code": "1216", "PEratio": "0"},
    ]
    t, table = sp.parse_pe(rows)
    assert t == T and table == {"2330": 29.56}


def test_parse_revenue_takes_latest_month_and_drops_same_month_conflicts():
    rows = [
        {"資料年月": "11509", "公司代號": "2330", "營業收入-去年同月增減(%)": "30", "累計營業收入-前期比較增減(%)": "35"},
        {"資料年月": "11508", "公司代號": "2330", "營業收入-去年同月增減(%)": "53.3", "累計營業收入-前期比較增減(%)": "39.3"},
        {"資料年月": "11508", "公司代號": "2344", "營業收入-去年同月增減(%)": "1", "累計營業收入-前期比較增減(%)": "1"},
        {"資料年月": "11508", "公司代號": "2344", "營業收入-去年同月增減(%)": "2", "累計營業收入-前期比較增減(%)": "1"},
        {"資料年月": "11508", "公司代號": "1101", "營業收入-去年同月增減(%)": "", "累計營業收入-前期比較增減(%)": "1"},
    ]
    table = sp.parse_revenue(rows)
    assert table["2330"] == sp.Revenue(2026, 9, 30.0, 35.0)
    assert "2344" not in table
    assert table["1101"].yoy is None


def _t86_payload(day=T, stat="OK", rows=None, fields=None):
    return {
        "stat": stat,
        "date": day.strftime("%Y%m%d"),
        "fields": fields or ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數"],
        "data": rows if rows is not None else [["2330  ", "台積電", "1,234,000", "-234,000"], ["0050", "元大台灣50", "5", "0"]],
    }


def test_parse_t86():
    kind, table = sp.parse_t86(_t86_payload(), T)
    assert kind == sp.T86_OK and table == {"2330": 1_000_000, "0050": 5}
    assert sp.parse_t86({"stat": "很抱歉，沒有符合條件的資料!"}, T) == (sp.T86_NOT_YET, {})
    assert sp.parse_t86(_t86_payload(day=PREV), T)[0] == sp.T86_BAD  # 日期不對
    assert sp.parse_t86(_t86_payload(fields=["證券代號", "x", "y", "z"]), T)[0] == sp.T86_BAD
    assert sp.parse_t86([], T)[0] == sp.T86_BAD
    dup = _t86_payload(rows=[["2330", "a", "1", "0"], ["2330", "a", "2", "0"], ["2317", "b", "3", "0"]])
    assert sp.parse_t86(dup, T)[1] == {"2317": 3}


def test_parse_chart_uses_taipei_dates_and_drops_bad_values():
    days = CAL[-5:]
    bars = sp.parse_chart(_chart(days, [1.0, None, -2.0, float("inf"), 5.0], meta={"exchangeTimezoneName": "UTC"}))
    assert bars == {days[0]: 1.0, days[4]: 5.0}  # meta 的時區不採用，一律台北
    assert sp.parse_chart(None) == {}
    assert sp.parse_chart({"chart": {"result": [{"timestamp": "x"}]}}) == {}


def test_market_value_rank_and_medians():
    companies = {
        "2330": sp.Company("台積電", "24", 25e9),
        "0050": sp.Company("ETF", "", 1e12),
        "9103": sp.Company("DR", "91", 1e12),
        "2881A": sp.Company("特", "17", 1e12),
        "2317": sp.Company("鴻海", "31", 13e9),
        "1101": sp.Company("台泥", "01", None),
    }
    day = {"2330": sp.DayRow("台積電", 2550.0, 0.0), "2317": sp.DayRow("鴻海", 200.0, 0.0), "0050": sp.DayRow("x", 100.0, 0.0),
           "9103": sp.DayRow("x", 100.0, 0.0), "2881A": sp.DayRow("x", 60.0, 0.0), "1101": sp.DayRow("x", 30.0, 0.0)}
    assert [c for c, _ in sp.market_value_rank(companies, day)] == ["2330", "2317"]
    many = {f"{1000 + i}": sp.Company("x", "01", 1.0) for i in range(5)}
    many["2000"] = sp.Company("x", "02", 1.0)
    pe = {f"{1000 + i}": float(10 + i) for i in range(5)} | {"2000": 99.0}
    assert sp.industry_medians(many, pe) == {"01": 12.0}


# ── 拼接 ───────────────────────────────────────────────────────────────────
def _bars(values=None, days=None):
    days = days if days is not None else CAL[:-1]
    values = values if values is not None else [100.0] * len(days)
    return dict(zip(days, values))


def test_window_closes_happy_path_and_t_close_from_official():
    year, quarter = sp.window_closes(_bars(), CAL, T, 110.0, 100.0)
    assert len(year) == 240 and len(quarter) == 60 and year[-1] == quarter[-1] == 110.0
    trend = sp.trend_of(year, quarter)
    assert trend.close == 110.0 and trend.p60 == sp._permille(110 / ((100 * 59 + 110) / 60) - 1)


def test_window_closes_rejects_disconnected_series():
    assert sp.window_closes(_bars(days=CAL[:-2]), CAL, T, 100.0, None) is None  # Yahoo 最後一根不是前一個交易日
    with_t = _bars(days=CAL)
    with_t[T] = 102.0
    assert sp.window_closes(with_t, CAL, T, 100.0, None) is None  # Yahoo 的 T 和官方差 > 1%
    with_t[T] = 100.5
    assert sp.window_closes(with_t, CAL, T, 100.0, None) is not None
    assert sp.window_closes(_bars(), CAL, T, 100.0, 120.0) is None  # 前一日參考價差 > 15%
    assert sp.window_closes(_bars(), CAL, T, 100.0, None) is not None  # 漲跌缺值：不比參考價
    jump = _bars()
    jump[CAL[-100]] = 130.0
    assert sp.window_closes(jump, CAL, T, 100.0, None) is None  # 相鄰兩根差 > 25%
    assert sp.window_closes(_bars(), CAL, T, 0.0, None) is None
    assert sp.window_closes(_bars(), CAL[:-1], T, 100.0, None) is None  # 日曆最後不是 T


def test_window_closes_missing_days_limits_and_no_borrowing():
    holes5 = _bars()
    for day in CAL[-200:-195]:
        del holes5[day]
    year, _ = sp.window_closes(holes5, CAL, T, 100.0, None)
    assert len(year) == 235
    holes6 = _bars()
    for day in CAL[-200:-194]:
        del holes6[day]
    assert sp.window_closes(holes6, CAL, T, 100.0, None) is None  # 不拿更早的日子補
    quarter_holes = _bars()
    for day in CAL[-30:-27]:
        del quarter_holes[day]
    assert sp.window_closes(quarter_holes, CAL, T, 100.0, None) is None  # 季線窗缺 3 天
    extra = _bars()
    extra[CAL[-5] + timedelta(days=1) if (CAL[-5] + timedelta(days=1)).weekday() >= 5 else date(2026, 10, 3)] = 1.0
    assert sp.window_closes(extra, CAL, T, 100.0, None) is not None  # 日曆外的 Yahoo 日子不用


# 2026-10-23 臨時休市（不在證交所假日表，FMTQIK、T86 都沒有那天）：^TWII 那天的收盤是 null（日曆沒有那天），
# 個股的 Yahoo 日線卻多一根成交量 0、照抄前一天收盤的平盤 → 休市後第一個交易日（10/27）每一檔都接不上。
def _ramp_bars(closed=()):
    """合成個股日線：每天收盤都不同；休市日（日曆沒有）那根照抄前一個交易日的收盤，和 Yahoo 的平盤一樣。"""
    cal = [d for d in CAL if d not in closed]
    bars = {d: 100.0 + 0.01 * i for i, d in enumerate(CAL[:-1]) if d not in closed}
    for day in closed:
        bars[day] = bars[max(d for d in cal if d < day)]
    return cal, bars


@pytest.mark.parametrize("closed", [(CAL[-2],), (CAL[-3], CAL[-2])])
def test_window_closes_ignores_flat_copies_on_closed_days(closed):
    cal, bars = _ramp_bars(closed)
    prev_close = bars[cal[-2]]
    windows = sp.window_closes(bars, cal, T, prev_close, prev_close)
    assert windows is not None
    year, quarter = windows
    assert len(year) == 240 and len(quarter) == 60 and year[-2] == prev_close
    # 休市日的平盤不進窗：和那幾根根本不存在時的結果一樣
    assert windows == sp.window_closes({d: v for d, v in bars.items() if d not in closed}, cal, T, prev_close, prev_close)
    noisy = {**bars, closed[0]: prev_close * (1 + 1e-12)}  # float 表示法的誤差
    assert sp.window_closes(noisy, cal, T, prev_close, None) is not None


def test_window_closes_rejects_a_real_trading_day_missing_from_the_calendar():
    """日曆少了真的交易日（那天個股有成交、收盤不平盤）→ 不接，不能悄悄把那天跳過。"""
    cal, bars = _ramp_bars((CAL[-2],))
    prev_close = bars[cal[-2]]
    assert sp.window_closes({**bars, CAL[-2]: prev_close + 0.5}, cal, T, prev_close, None) is None
    cal2, bars2 = _ramp_bars((CAL[-3], CAL[-2]))  # 兩天空檔：一天平盤、一天有成交，只要有一天不平盤就不接
    assert sp.window_closes(bars2, cal2, T, bars2[cal2[-2]], None) is not None
    assert sp.window_closes({**bars2, CAL[-2]: bars2[cal2[-2]] - 0.5}, cal2, T, bars2[cal2[-2]], None) is None
    assert sp.window_closes({**bars2, CAL[-3]: bars2[cal2[-2]] + 0.5}, cal2, T, bars2[cal2[-2]], None) is None


def test_window_closes_still_needs_the_previous_trading_day():
    cal, bars = _ramp_bars((CAL[-2],))
    del bars[cal[-2]]  # 前一個交易日那根缺（停牌、缺資料），只剩休市日那根平盤：平盤不能頂替它
    assert CAL[-2] in bars
    assert sp.window_closes(bars, cal, T, 100.0, None) is None


# ── 查詢 ───────────────────────────────────────────────────────────────────
INDEX = sp.name_index({"台積電": "2330", "國巨": "2327", "矽力-KY": "6415", "成信實業-創": "1234", "元大台灣50": "0050",
                       "元大MSCI A股": "006205", "IKKA-KY": "2250", "聯發科": "2454"})


@pytest.mark.parametrize(
    "body, expected",
    [
        ("2330", ("code", "2330")),
        ("2330.TW", ("code", "2330")),
        ("２３３０", ("code", "2330")),
        ("2330台積電", ("code", "2330")),
        ("台積電", ("code", "2330")),
        ("台積電現在怎樣", ("code", "2330")),
        ("台積", ("code", "2330")),
        ("國巨", ("code", "2327")),
        ("矽力", ("code", "6415")),
        ("矽力-ky", ("code", "6415")),
        ("成信實業", ("code", "1234")),
        ("元大MSCI A股", ("code", "006205")),
        ("ikka", ("code", "2250")),
        ("0050", ("code", "0050")),
        ("00878", ("code", "00878")),
        ("00679B", ("code", "00679B")),
        ("2881A", (sp.UNSUPPORTED, None)),
        ("12345", (sp.UNSUPPORTED, None)),
        ("12345678", (sp.NOT_FOUND, None)),
        ("NVDA", (sp.US_SYMBOL, None)),
        ("NVDA 2330", ("code", "2330")),
        ("不知道", (sp.NOT_FOUND, None)),
        ("", (sp.NOT_FOUND, None)),
        ("2330 台積電 聯發科", ("code", "2330")),
    ],
)
def test_resolve_query(body, expected):
    assert sp.resolve_query(body, INDEX) == expected


def test_normalize_body_limits_and_zero_width():
    assert sp._normalize_body("​２３３０​") == "2330"
    assert sp._normalize_body("x" * 40) == "x" * 40
    assert sp._normalize_body("x" * 41) is None
    assert sp._normalize_body("a b c d e") == "a b c d e"
    assert sp._normalize_body("a b c d e f") is None
    assert sp._normalize_body(None) == ""


# ── 快照與工作狀態檔 ───────────────────────────────────────────────────────
def test_snapshot_round_trip(net):
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1_700_000_000.0)
    loaded = sp.snapshot_from_json(json.loads(json.dumps(sp.snapshot_to_json(snapshot), ensure_ascii=False)))
    assert loaded == snapshot
    assert loaded.index == snapshot.index and loaded.index["股2330"] == "2330" and loaded.index["櫃6000"] == "6000"
    assert loaded.calendar[-2] == PREV and loaded.full_names == snapshot.full_names


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o.update(schema=999),
        lambda o: o.update(t="2026-13-01"),
        lambda o: o["calendar"].pop(),
        lambda o: o.update(picks=["9999"]),
        lambda o: o["listed"].update({"../x": o["listed"]["2330"]}),
        lambda o: o["listed"]["2330"].__setitem__(3, float("nan")) if False else o["listed"]["2330"].__setitem__(3, -1),
        lambda o: o["listed"]["2330"].__setitem__(0, "名字\n換行"),
        lambda o: o["pool"][0].update(k="crypto"),
        lambda o: o.update(market=["boiling", 1, 2]),
    ],
)
def test_invalid_snapshot_is_rejected(net, mutate):
    obj = json.loads(json.dumps(sp.snapshot_to_json(sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9))))
    mutate(obj)
    with pytest.raises((ValueError, KeyError, TypeError)):
        sp.snapshot_from_json(obj)


def test_corrupt_symlinked_or_huge_state_files_are_ignored(net, tmp_path, monkeypatch):
    sp.STATE_DIR.mkdir(parents=True)
    path = sp.STATE_DIR / sp.SNAPSHOT_NAME
    path.write_text("{not json")
    assert sp.load_snapshot_file() is None
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(sp.snapshot_to_json(sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9))))
    path.symlink_to(target)
    assert sp.load_snapshot_file() is None  # 不跟隨 symlink
    path.unlink()
    monkeypatch.setattr(sp, "STATE_MAX_BYTES", 10)
    path.write_text(target.read_text())
    assert sp.load_snapshot_file() is None


def test_state_files_are_private_and_atomic(net):
    _fresh_snapshot(net)
    with sp._lock:
        sp._save_worker()
    for name in (sp.SNAPSHOT_NAME, sp.WORKER_NAME):
        mode = os.stat(sp.STATE_DIR / name).st_mode & 0o777
        assert mode == 0o600
    assert not [p for p in sp.STATE_DIR.iterdir() if p.name.endswith(".tmp")]
    assert sp.load_snapshot_file().t == T


def test_no_regression_only_blocks_older_dates(net):
    newer = sp.refresh(T, _day_rows(net), threading.Event(), now=2.0e9)
    assert sp._commit_snapshot(newer)
    older = sp.Snapshot(**{**newer.__dict__, "t": PREV, "calendar": (CAL[-241],) + newer.calendar[:-1]})
    sp._snapshot = None
    assert sp._commit_snapshot(older) == sp.COMMIT_DISK_NEWER  # 磁碟上的比較新：不寫，改用磁碟上的
    assert sp._snapshot.t == T and sp.load_snapshot_file().t == T
    same_t = sp.Snapshot(**{**newer.__dict__, "computed_at": 2.5e9})
    assert sp._commit_snapshot(same_t) == sp.COMMIT_WRITTEN  # 同一個 T 照寫
    assert sp.load_snapshot_file().computed_at == 2.5e9


def test_worker_state_round_trip_and_corrupt_file_backs_off():
    state = sp.WorkerState(
        last_ok_check=1.0e9, seen_t=T, seen_at=1.0e9, last_modified="Fri, 09 Oct 2026 21:20:43 GMT", fail_count=2,
        next_try=1.1e9, last_fail="t86_not_yet", first_round_failed=True, t86={"2026-10-08": {"2330": 5}},
        t86_requests=[1.0e9],
    )
    assert sp.worker_state_from_json(json.loads(json.dumps(sp.worker_state_to_json(state)))) == state
    sp.STATE_DIR.mkdir(parents=True)
    (sp.STATE_DIR / sp.WORKER_NAME).write_text("[]")
    before = time.time()
    loaded = sp.load_worker_file()
    assert loaded.next_try >= before + sp.STATE_CORRUPT_BACKOFF_S - 1 and loaded.last_fail == "state_corrupt"


# ── T86 請求帳 ─────────────────────────────────────────────────────────────
def test_t86_budget_is_reserved_before_sending_and_merged_across_processes(net):
    stop = threading.Event()
    sp._worker.t86_requests = [time.time() - 100 + i for i in range(sp.T86_DAILY_CAP - 2)]  # 每次請求的時間都不同
    with sp._lock:
        sp._save_worker()
    # 另一個程序在磁碟上又記了 1 次
    other = sp.load_worker_file()
    other.t86_requests.append(time.time() - 5)
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, sp.worker_state_to_json(other))
    net.overrides["T86"] = sp.FetchError("timeout")
    with pytest.raises(sp.RoundFailed) as info:
        sp._t86_day(CAL[-1], stop, time.monotonic() + 60)
    assert info.value.kind == "t86_timeout"
    on_disk = sp.load_worker_file()
    assert len(on_disk.t86_requests) == sp.T86_DAILY_CAP  # 送出前已經記帳（含別的程序那一次）
    with pytest.raises(sp.RoundFailed) as info:
        sp._t86_day(CAL[-2], stop, time.monotonic() + 60)
    assert info.value.kind == "t86_budget" and info.value.retry_s == 3600
    assert net.count("T86") == 1


def test_t86_blocked_responses_mark_round_blocked(net):
    net.overrides["T86"] = sp.FetchError("not_json")
    with pytest.raises(sp.RoundFailed) as info:
        sp._t86_day(CAL[-1], threading.Event(), time.monotonic() + 60)
    assert info.value.blocked is True


def test_t86_day_cache_skips_network(net):
    sp._worker.t86 = {CAL[-1].isoformat(): {"2330": 1}}
    assert sp._t86_day(CAL[-1], threading.Event(), time.monotonic() + 60) == {"2330": 1}
    assert net.count("T86") == 0


# ── 重算一輪 ───────────────────────────────────────────────────────────────
def test_refresh_end_to_end(net):
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert snapshot.t == T and snapshot.calendar[-1] == T and len(snapshot.calendar) == 240
    assert len(snapshot.universe) == 50 and snapshot.universe[0] == "2330"
    assert set(snapshot.pool) == set(snapshot.universe) | {"0050"}
    assert snapshot.pool["0050"].kind == sp.ETF
    candidates = [snapshot.pool[c] for c in snapshot.universe if sp.is_pick_candidate(snapshot.pool[c])]
    assert list(snapshot.picks) == [f.code for f in sorted(candidates, key=sp.pick_sort_key)[:3]]
    assert all(sp.light_of(snapshot.pool[c]).level == sp.MET for c in snapshot.picks)
    assert snapshot.market == sp.Market(sp.NEUTRAL, 0, 0)
    assert len(snapshot.otc) == 320 and snapshot.otc["6000"].name == "櫃6000"
    assert snapshot.listed["2330"].inst == 10 * 1_000_000
    assert net.count("T86") == 10
    assert all(call[3] is None for call in net.calls)


@pytest.mark.parametrize(
    "breakage, kind",
    [
        (lambda n: n.pe.__setitem__(0, {**n.pe[0], "Date": _roc(PREV)}), "pe_bad"),  # 日期不唯一：真的錯誤
        (lambda n: setattr(n, "pe", [{**row, "Date": _roc(PREV)} for row in n.pe]), "pe_not_t"),
        (lambda n: (n.charts.__setitem__("^TWII", _chart(CAL[:-1], [20000.0] * 299)), n.fmtqik.pop()), "index_not_t"),
        (lambda n: n.charts.__setitem__("^TWII", _chart(CAL[:-1], [1.0] * 299)), "index_mismatch"),
        (lambda n: n.charts.__setitem__("^TWII", _chart(CAL[-100:], [1.0] * 100)), "twii_short"),
        (lambda n: n.t86.pop(CAL[-3].strftime("%Y%m%d")), "t86_gap"),  # 早於 T 的日子缺：不會自己好
        (lambda n: n.t86.pop(CAL[-1].strftime("%Y%m%d")), "t86_not_yet"),
        (lambda n: n.companies.__setitem__(slice(None), n.companies[:100]), "batch_short"),
        (lambda n: [n.charts.pop(f"{c}.TW") for c in STOCKS[1:12]], "too_many_gray"),  # 留下 2330 抽查，測主池缺資料
        (lambda n: n.overrides.__setitem__("BWIBBU_ALL", sp.FetchError("moved")), "batch_moved"),
    ],
)
def test_refresh_whole_round_fails(net, breakage, kind):
    if kind == "batch_short":
        net.companies = net.companies[:40]
        net.revenue = net.revenue[:40]
    else:
        breakage(net)
    with pytest.raises(sp.RoundFailed) as info:
        sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert info.value.kind == kind


def test_refresh_ten_gray_is_still_ok(net):
    for code in STOCKS[1:11]:
        net.charts.pop(f"{code}.TW")
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert sum(1 for c in snapshot.universe if snapshot.pool[c].trend is None) == 10


def test_refresh_yahoo_rate_limit_is_blocked(net):
    net.overrides["1101.TW"] = sp.FetchError("rate_limited")  # 2330 抽查的 429 另測 not_yet
    with pytest.raises(sp.RoundFailed) as info:
        sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert info.value.kind == "yahoo_rate_limited" and info.value.blocked


def test_refresh_without_tpex_keeps_listed_and_otc_fills_later(net):
    net.overrides["tpex_mainboard_daily_close_quotes"] = sp.FetchError("timeout")
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=time.time())
    assert snapshot.otc is None
    sp._commit_snapshot(snapshot)
    del net.overrides["tpex_mainboard_daily_close_quotes"]
    sp._maybe_fill_otc(threading.Event())
    assert sp._snapshot.otc is not None and sp.load_snapshot_file().otc is not None


# ── 背景工作 ───────────────────────────────────────────────────────────────
def test_tick_bootstraps_then_uses_if_modified_since(net):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    assert sp._snapshot is not None and sp._snapshot.t == T
    assert sp._worker.seen_t == T and sp._worker.last_modified == "Fri, 09 Oct 2026 21:20:43 GMT"
    first_check = sp._worker.last_ok_check
    net.overrides["STOCK_DAY_ALL"] = (304, None, None)
    net.calls.clear()
    time.sleep(0.01)
    assert sp._tick(stop) == sp.POLL_S
    assert net.calls[0][3] == "Fri, 09 Oct 2026 21:20:43 GMT"
    assert sp._worker.last_ok_check > first_check
    assert sp.load_worker_file().last_ok_check == sp._worker.last_ok_check  # 304 也存檔


def test_failed_recompute_retries_without_if_modified_since(net, monkeypatch):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    new_t = T + timedelta(days=1)
    net.day_rows = [{**row, "Date": _roc(new_t)} for row in net.day_rows]
    calls = []

    def failing(t, rows, stop_event, *, now=None):
        calls.append(t)
        raise sp.RoundFailed("batch_status")

    monkeypatch.setattr(sp, "refresh", failing)
    assert sp._tick(stop) == sp.BACKOFF_S[0]
    assert sp._worker.seen_t == new_t and sp._worker.fail_count == 1
    seen_at = sp._worker.seen_at
    assert 0 < sp._tick(stop) <= sp.BACKOFF_S[0]  # 退避中不打
    sp._worker.next_try = time.time() - 1
    net.calls.clear()
    assert sp._tick(stop) == sp.BACKOFF_S[1]
    assert net.calls[0][3] is None  # 待重算時不帶 If-Modified-Since，304 擋不住重試
    assert sp._worker.seen_at == seen_at  # 同一個日期不重設首次看到的時間
    assert calls == [new_t, new_t]


def test_twse_blocked_failures_wait_one_hour(net):
    net.overrides["STOCK_DAY_ALL"] = sp.FetchError("blocked")
    assert sp._tick(threading.Event()) == sp.TWSE_BLOCKED_RETRY_S == 3600
    assert sp._worker.first_round_failed is True


def test_bad_day_file_fails(net):
    net.overrides["STOCK_DAY_ALL"] = (200, [{"Date": "1151008"}], None)
    assert sp._tick(threading.Event()) == sp.BACKOFF_S[0]
    assert sp._worker.last_fail == "day_bad"


def test_not_updated_rules(net):
    snapshot = _fresh_snapshot(net)
    now = time.time()
    assert not sp._not_updated(snapshot, now)
    sp._worker.seen_t, sp._worker.seen_at = T + timedelta(days=1), now - sp.NEW_DATE_GRACE_S + 60
    assert not sp._not_updated(snapshot, now)
    sp._worker.seen_at = now - sp.NEW_DATE_GRACE_S
    assert sp._not_updated(snapshot, now)
    sp._worker.seen_t = T
    sp._worker.last_ok_check = now - sp.CHECK_STALE_S
    stale_snapshot = sp.Snapshot(**{**snapshot.__dict__, "computed_at": now - sp.CHECK_STALE_S})
    assert sp._not_updated(stale_snapshot, now)
    assert not sp._not_updated(snapshot, now)  # 剛算好的快照就是一次成功的檢查
    assert sp.status_summary(now)["status"] == "ok"


def test_background_thread_starts_once_waits_and_stops(net, monkeypatch):
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0.05)
    ticks = []
    ran = threading.Event()

    def fake_tick(stop):
        ticks.append(time.monotonic())
        ran.set()
        return 30.0

    monkeypatch.setattr(sp, "_tick", fake_tick)
    started = time.monotonic()
    sp.start_background()
    first = sp._thread
    sp.start_background()
    assert sp._thread is first  # 還活著就不再起
    assert ran.wait(2.0) and ticks[0] - started >= 0.05
    sp.stop_background(timeout=2.0)
    assert not first.is_alive() and len(ticks) == 1


def test_background_survives_crashing_tick(net, monkeypatch):
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(sp, "MIN_WAIT_S", 0.01)
    monkeypatch.setattr(sp, "BACKOFF_S", (0.01, 0.01, 0.01, 0.01))
    count = []
    done = threading.Event()

    def boom(stop):
        count.append(1)
        if len(count) >= 3:
            done.set()
            return 30.0
        raise RuntimeError("boom")

    monkeypatch.setattr(sp, "_tick", boom)
    sp.start_background()
    assert done.wait(5.0)
    sp.stop_background(timeout=2.0)
    assert len(count) == 3


def test_wake_does_not_skip_startup_delay(net, monkeypatch):
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0.5)
    ticks = []
    first = threading.Event()
    monkeypatch.setattr(sp, "_tick", lambda stop: ticks.append(time.monotonic()) or first.set() or 30.0)
    started = time.monotonic()
    sp.start_background()
    sp._wake_worker()  # 啟動期間叫醒：不能提早，也不能讓第一輪一結束又多跑一輪
    assert first.wait(5.0)
    time.sleep(0.3)
    sp.stop_background(timeout=2.0)
    assert ticks[0] - started >= 0.45 and len(ticks) == 1


def test_disabled_background_never_starts():
    sp.start_background()
    assert sp._thread is None


# ── handle() ───────────────────────────────────────────────────────────────
def test_handle_without_snapshot_wakes_worker(net):
    wake = threading.Event()
    sp._wake = wake
    reply = sp.handle("")
    assert reply.text == sp.TEXT_NOT_READY and wake.is_set()
    sp._worker.first_round_failed = True
    assert sp.handle("").text == sp.TEXT_UNAVAILABLE


def test_handle_picks_and_detail_from_snapshot(net, monkeypatch):
    # 2026-10-24（計畫 C）：預設的推薦區已經是「AI 相關股推薦」（test_stock_picks_ai*.py）；這裡守住
    # LINE_BOT_STOCK_PICKS_AI=0 退回的舊推薦卡（舊推薦照樣每天算進快照），單檔卡與常問股兩邊都一樣。
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    snapshot = _fresh_snapshot(net)
    reply = sp.handle("")
    assert reply.flex["type"] == "carousel" and reply.text is None
    assert reply.alt_text.startswith("股票推薦：只供參考｜")
    assert len(reply.flex["contents"]) == len(snapshot.picks) + 1
    assert sp.handle("推薦").alt_text == reply.alt_text
    detail = sp.handle("2330")
    assert detail.flex["type"] == "bubble" and detail.alt_text.startswith("股票評估：只供參考｜股2330 2330｜")
    assert detail.fallback_text.startswith("股票評估：")
    assert net.count("2330.TW") == 1  # 只有重算那一次，單檔卡讀快照


def test_handle_not_updated_hides_picks(net, monkeypatch):
    monkeypatch.setattr(sp, "AI_ENABLED", False)  # 舊推薦卡的「看到新日期 3 小時」規則；AI 推薦改看 7 個日曆日
    _fresh_snapshot(net)
    sp._worker.seen_t, sp._worker.seen_at = T + timedelta(days=1), time.time() - sp.NEW_DATE_GRACE_S - 1
    reply = sp.handle("")
    assert reply.alt_text == "股票推薦：只供參考｜資料暫時沒有更新，先不推薦｜資料到 10/08"
    assert len(reply.flex["contents"]) == 2


@pytest.mark.parametrize(
    "body, text",
    [
        ("NVDA", sp.TEXT_US),
        ("2881A", sp.TEXT_UNSUPPORTED),
        ("不存在的公司", sp.TEXT_NOT_FOUND),
        ("9999", sp.TEXT_NOT_FOUND),
        ("x" * 41, sp.TEXT_TOO_LONG),
        ("\n@all 2330 2330 2330 2330 2330", sp.TEXT_TOO_LONG),
    ],
)
def test_handle_fixed_replies_never_echo_input(net, body, text):
    _fresh_snapshot(net)
    reply = sp.handle(body)
    assert reply.text == text and reply.flex is None


def test_handle_lookup_listed_outside_pool(net):
    _fresh_snapshot(net)
    code = STOCKS[55]  # 股票池以外的上市股
    net.calls.clear()
    reply = sp.handle(code, deadline=time.monotonic() + 30)
    assert reply.flex is not None and reply.alt_text.startswith(f"股票評估：只供參考｜股{code} {code}｜")
    yahoo = [c for c in net.calls if c[0] in sp._YAHOO]
    assert [c[0] for c in yahoo] == [sp._YAHOO[0]]  # 只打一個主機
    sp.handle(code, deadline=time.monotonic() + 30)
    assert len([c for c in net.calls if c[0] in sp._YAHOO]) == 1  # 同一個 T 快取


def test_handle_lookup_otc_by_code_and_name(net):
    _fresh_snapshot(net)
    reply = sp.handle("6001", deadline=time.monotonic() + 30)
    assert "上櫃只看走勢" in json.dumps(reply.flex, ensure_ascii=False)
    assert sp.handle("櫃6002", deadline=time.monotonic() + 30).alt_text.startswith("股票評估：只供參考｜櫃6002 6002｜")


def test_handle_lookup_limits(net):
    _fresh_snapshot(net)
    assert sp.handle(STOCKS[55], deadline=time.monotonic() + 3).text == sp.TEXT_NO_TIME
    for code in STOCKS[50:54]:
        sp.handle(code, deadline=time.monotonic() + 30)
    assert sp.handle(STOCKS[54], deadline=time.monotonic() + 30).text == sp.TEXT_BUSY
    assert sp.handle(STOCKS[50], deadline=time.monotonic() + 30).flex is not None  # 快取的不算次數


def test_handle_lookup_failure_is_remembered_briefly(net):
    _fresh_snapshot(net)
    net.overrides[f"{STOCKS[56]}.TW"] = sp.FetchError("timeout")
    assert sp.handle(STOCKS[56], deadline=time.monotonic() + 30).text == sp.TEXT_LOOKUP_FAILED
    del net.overrides[f"{STOCKS[56]}.TW"]
    assert sp.handle(STOCKS[56], deadline=time.monotonic() + 30).text == sp.TEXT_LOOKUP_FAILED  # 10 分鐘內不再抓


def test_lookup_works_during_an_ai_side_cooldown_and_never_sets_it(net, monkeypatch):
    """計畫 C 事後審查 GP1 I3／GP2 #3：單檔查詢和 2eb85e1 一樣——AI 段撞到 429 的共用冷卻擋不了它，
    它自己撞到 429 也不設共用冷卻（背景不必為了爸媽查單檔而多等 20 分鐘）。"""
    _fresh_snapshot(net)
    sp._block_yahoo()  # AI 段剛撞到 429：共用冷卻中
    assert sp._yahoo_blocked()
    net.calls.clear()
    reply = sp.handle(STOCKS[55], deadline=time.monotonic() + 30)
    assert reply.flex is not None and [c[0] for c in net.calls if c[0] in sp._YAHOO] == [sp._YAHOO[0]]  # 照樣查得到
    monkeypatch.setattr(sp, "_yahoo_blocked_until", 0.0)
    net.overrides[f"{STOCKS[56]}.TW"] = sp.FetchError("rate_limited")
    assert sp.handle(STOCKS[56], deadline=time.monotonic() + 30).text == sp.TEXT_LOOKUP_FAILED
    assert sp._yahoo_blocked_until == 0.0 and not sp._yahoo_blocked() and sp._worker.yahoo_blocked_until is None


def test_handle_otc_unavailable_without_tpex(net):
    net.overrides["tpex_mainboard_daily_close_quotes"] = sp.FetchError("timeout")
    _fresh_snapshot(net)
    assert sp.handle("6001").text == sp.TEXT_OTC_UNAVAILABLE
    assert sp.handle("00999").text == sp.TEXT_OTC_UNAVAILABLE  # 00 開頭的也可能是上櫃 ETF


# ── 實作複核（Codex p1）補的測試 ─────────────────────────────────────────────
def test_t86_missing_day_makes_that_stock_unknown(net):
    day = CAL[-4].strftime("%Y%m%d")
    net.t86[day]["data"] = [row for row in net.t86[day]["data"] if row[0] != "2330"]
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert snapshot.listed["2330"].inst is None and snapshot.pool["2330"].inst_net is None
    assert snapshot.listed["1101"].inst == -10 * 1_000_000  # 其他檔十天都有
    check = [c for c in sp.checks_of(snapshot.pool["2330"]) if c.key == sp.INST][0]
    assert check.state is None


def test_t86_spacing_waits_between_requests(net, monkeypatch):
    monkeypatch.setattr(sp, "T86_SPACING_S", 0.2)
    stop = threading.Event()
    started = time.monotonic()
    sp._t86_day(CAL[-1], stop, time.monotonic() + 60)
    sp._t86_day(CAL[-2], stop, time.monotonic() + 60)
    assert time.monotonic() - started >= 0.2
    stamps = sp.load_worker_file().t86_requests
    assert len(stamps) == 2 and stamps[1] - stamps[0] >= 0.2


def test_t86_spacing_beyond_budget_fails_round(net, monkeypatch):
    monkeypatch.setattr(sp, "T86_SPACING_S", 5.0)
    stop = threading.Event()
    sp._t86_day(CAL[-1], stop, time.monotonic() + 60)
    with pytest.raises(sp.RoundFailed) as info:
        sp._t86_day(CAL[-2], stop, time.monotonic() + 1)
    assert info.value.kind == "budget_t86"


def test_corrupt_ledger_in_running_process_backs_off_and_is_repaired(net):
    sp.STATE_DIR.mkdir(parents=True)
    (sp.STATE_DIR / sp.WORKER_NAME).write_text("{broken")
    before = time.time()
    with pytest.raises(sp.RoundFailed) as info:
        sp._t86_day(CAL[-1], threading.Event(), time.monotonic() + 60)
    assert info.value.kind == "state_corrupt"
    assert net.count("T86") == 0
    repaired = sp.load_worker_file()
    assert repaired.next_try >= before + sp.STATE_CORRUPT_BACKOFF_S - 1 and repaired.last_fail == "state_corrupt"


def test_other_process_cooldown_is_respected_but_own_cleared_backoff_is_not_revived(net):
    other = sp.WorkerState(next_try=time.time() + 3000, last_fail="t86_blocked", writer="other-process")
    sp.STATE_DIR.mkdir(parents=True)
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, sp.worker_state_to_json(other))
    with pytest.raises(sp.RoundFailed) as info:
        sp._t86_day(CAL[-1], threading.Event(), time.monotonic() + 60)
    assert info.value.kind == "t86_cooldown" and net.count("T86") == 0
    with sp._lock:  # 別的程序的退避還沒到期：存檔時照樣保留
        sp._worker.next_try = None
        sp._save_worker()
    assert sp._worker.next_try == pytest.approx(other.next_try)


def test_own_cleared_backoff_is_not_revived_from_disk(net):
    with sp._lock:
        sp._worker.next_try = time.time() + 3600
        sp._save_worker()  # 自己寫的退避
        sp._worker.next_try = None  # 這輪成功、清掉
        sp._save_worker()
        sp._save_worker()
    assert sp._worker.next_try is None and sp.load_worker_file().next_try is None


def test_commit_failure_keeps_old_snapshot_and_fails_the_round(net, monkeypatch):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    old = sp._snapshot
    new_t = T + timedelta(days=1)
    net.day_rows = [{**row, "Date": _roc(new_t)} for row in net.day_rows]
    newer = sp.Snapshot(**{**old.__dict__, "t": new_t, "calendar": old.calendar[1:] + (new_t,)})
    monkeypatch.setattr(sp, "refresh", lambda *a, **k: newer)
    monkeypatch.setattr(sp, "_write_json_file", MagicMockRaising())
    assert sp._tick(stop) == sp.BACKOFF_S[0]
    assert sp._snapshot is old and sp._worker.last_fail == "snapshot_write"


class MagicMockRaising:
    def __call__(self, path, payload):
        if path.name == sp.SNAPSHOT_NAME:
            raise OSError("disk full")
        return _REAL_WRITE(path, payload)


_REAL_WRITE = sp._write_json_file


def test_failed_recompute_then_success_clears_failure(net, monkeypatch):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    new_t = T + timedelta(days=1)
    net.day_rows = [{**row, "Date": _roc(new_t)} for row in net.day_rows]
    old = sp._snapshot
    outcomes = [sp.RoundFailed("batch_status"), sp.Snapshot(**{**old.__dict__, "t": new_t, "calendar": old.calendar[1:] + (new_t,)})]

    def scripted(*_a, **_k):
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sp, "refresh", scripted)
    assert sp._tick(stop) == sp.BACKOFF_S[0]
    sp._worker.next_try = time.time() - 1
    assert sp._tick(stop) == sp.POLL_S
    assert sp._snapshot.t == new_t and sp._worker.fail_count == 0 and sp._worker.next_try is None
    assert sp.load_worker_file().fail_count == 0 and sp.load_snapshot_file().t == new_t


def test_refresh_budget_covers_every_source(net, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(sp.time, "monotonic", lambda: clock["now"])
    real_charts = sp._fetch_charts

    def slow_charts(symbols, stop, give_up):
        clock["now"] = give_up - 10  # Yahoo 批次用到只剩 10 秒
        return real_charts(symbols, stop, give_up)

    monkeypatch.setattr(sp, "_fetch_charts", slow_charts)
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert snapshot.otc is None and net.count("tpex_mainboard_daily_close_quotes") == 0  # 剩不到 20 秒不抓櫃買

    def too_slow(symbols, stop, give_up):
        clock["now"] = give_up + 1
        return {}

    monkeypatch.setattr(sp, "_fetch_charts", too_slow)
    with pytest.raises(sp.RoundFailed) as info:
        sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert info.value.kind == "budget_yahoo"


def test_lookup_gray_result_is_only_cached_briefly(net, monkeypatch):
    _fresh_snapshot(net)
    code = STOCKS[57]
    net.charts[f"{code}.TW"] = {"chart": {"result": None}}
    first = sp.handle(code, deadline=time.monotonic() + 30)
    assert "資料不足" in json.dumps(first.flex, ensure_ascii=False)
    net.charts[f"{code}.TW"] = _chart(CAL[:-1], [100.0] * (len(CAL) - 1))
    assert "資料不足" in json.dumps(sp.handle(code, deadline=time.monotonic() + 30).flex, ensure_ascii=False)
    key = (T.isoformat(), code)
    stamp, facts, ok = sp._lookup_cache[key]
    assert ok is False
    sp._lookup_cache[key] = (stamp - sp.LOOKUP_FAIL_TTL_S - 1, facts, ok)
    fixed = sp.handle(code, deadline=time.monotonic() + 30)
    assert "資料不足" not in json.dumps(fixed.flex, ensure_ascii=False)


def test_market_threshold_from_real_closes():
    def closes_with_b20(target_permille):
        # 前 59 個都是 100，最後一個 x：b20 = x / ((19*100 + x)/20) - 1
        ratio = 1 + target_permille / 1000
        x = 1900 * ratio / (20 - ratio)
        return [100.0] * 59 + [x]

    assert sp.market_of(closes_with_b20(60)).b20 == 60
    assert sp.market_of(closes_with_b20(60)).level == sp.NEUTRAL  # 比月線高 6.0% 還不算偏熱
    assert sp.market_of(closes_with_b20(61)).level == sp.HOT
    assert sp.market_of(closes_with_b20(-60)).level == sp.NEUTRAL
    assert sp.market_of(closes_with_b20(-61)).level == sp.COLD



# ── Phase 6 第二輪（p2–p4）補的測試 ─────────────────────────────────────────
def test_full_company_names_win_over_short_name_prefixes(net):
    net.companies[1] = {**net.companies[1], "公司簡稱": "聯華", "公司名稱": "聯華實業控股股份有限公司"}
    net.companies[2] = {**net.companies[2], "公司簡稱": "聯電", "公司名稱": "聯華電子股份有限公司"}
    net.companies[3] = {**net.companies[3], "公司簡稱": "中華", "公司名稱": "中華汽車工業股份有限公司"}
    net.companies[4] = {**net.companies[4], "公司簡稱": "華航", "公司名稱": "中華航空股份有限公司"}
    snapshot = _fresh_snapshot(net)
    lianhua, umc, yulon, cal = STOCKS[1], STOCKS[2], STOCKS[3], STOCKS[4]
    assert sp.resolve_query("聯華電子", snapshot.index) == ("code", umc)
    assert sp.resolve_query("聯華電子現在怎樣", snapshot.index) == ("code", umc)
    assert sp.resolve_query("聯華", snapshot.index) == ("code", lianhua)
    assert sp.resolve_query("中華航空", snapshot.index) == ("code", cal)
    assert sp.resolve_query("中華", snapshot.index) == ("code", yulon)
    reply = sp.handle("中華航空")
    assert reply.alt_text.startswith(f"股票評估：只供參考｜華航 {cal}｜")


def test_otc_etf_and_no_trade_codes(net):
    net.otc.append({"Date": _roc(T), "SecuritiesCompanyCode": "00679B", "CompanyName": "元大美債20年", "Close": "29.50", "Change": "0.10"})
    net.otc.append({"Date": _roc(T), "SecuritiesCompanyCode": "6999", "CompanyName": "停牌股", "Close": "", "Change": ""})
    net.charts["00679B.TWO"] = _chart(CAL[:-1], [29.4] * (len(CAL) - 1))
    net.day_rows.append({"Date": _roc(T), "Code": "4581", "Name": "光隆精密-KY", "ClosingPrice": "--", "Change": "0"})
    snapshot = _fresh_snapshot(net)
    etf = sp.handle("00679B", deadline=time.monotonic() + 30)
    dumped = json.dumps(etf.flex, ensure_ascii=False)
    assert "ETF 只看走勢" in dumped and "依據：櫃買中心、Yahoo" in dumped
    assert sp.handle("元大美債20年", deadline=time.monotonic() + 30).alt_text == etf.alt_text
    assert sp.handle("4581").text == sp.no_trade_text(T)
    assert sp.handle("光隆精密").text == sp.no_trade_text(T)
    assert sp.handle("6999").text == sp.no_trade_text(T)
    assert snapshot.no_trade["4581"] == "光隆精密-KY"


def test_punctuation_only_body_shows_the_picks(net):
    _fresh_snapshot(net)
    for body in ("？", "。", "!", " 推薦？"):
        assert sp.handle(body).flex["type"] == "carousel", body


def test_crashing_tick_backs_off_and_wake_does_not_refetch(net, monkeypatch):
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0.0)
    calls = []

    def boom(stop):
        calls.append(1)
        raise RuntimeError("parser bug")

    real_tick = sp._tick
    monkeypatch.setattr(sp, "_tick", boom)
    sp.start_background()
    deadline = time.monotonic() + 5
    while sp._worker.last_fail != "crash_RuntimeError" and time.monotonic() < deadline:
        time.sleep(0.01)
    with sp._lock:  # _fail 拿著 _lock 存檔：拿得到鎖＝已經寫完
        pass
    assert sp._worker.next_try is not None and sp._worker.next_try > time.time() + 300
    assert sp._worker.last_fail == "crash_RuntimeError"
    assert sp.load_worker_file().next_try == pytest.approx(sp._worker.next_try)
    monkeypatch.setattr(sp, "_tick", real_tick)
    net.calls.clear()
    for _ in range(3):  # 沒有快照時每按一次都會叫醒，但退避中不能整輪重抓
        sp.handle("")
        time.sleep(0.05)
    sp.stop_background(timeout=2.0)
    assert net.count("STOCK_DAY_ALL") == 0


def test_stop_mid_round_is_not_a_failure(net, monkeypatch):
    stop = threading.Event()

    def stopping(*_a, **_k):
        stop.set()
        raise sp.RoundFailed("t86_stopped")

    monkeypatch.setattr(sp, "refresh", stopping)
    assert sp._tick(stop) == sp.POLL_S
    assert sp._worker.fail_count == 0 and sp._worker.next_try is None and sp._worker.first_round_failed is False


def test_up_to_date_check_resets_failures_and_seen_at_only_moves_forward(net):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    sp._worker.last_wait = "pe_not_t"
    sp._tick(stop)  # 證交所回 T、沒有待重算：追上了，等待一起清
    assert sp._worker.last_wait == ""
    sp._worker.fail_count, sp._worker.last_fail, sp._worker.last_wait = 3, "day_timeout", "pe_not_t"
    net.overrides["STOCK_DAY_ALL"] = (304, None, None)
    sp._tick(stop)
    assert sp._worker.fail_count == 0 and sp._worker.last_fail == "" and sp._worker.last_wait == ""
    del net.overrides["STOCK_DAY_ALL"]
    sp._worker.seen_t, sp._worker.seen_at = T + timedelta(days=1), 123.0
    sp._tick(stop)  # 證交所又回 T（日期回跳）：不重設首次看到新日期的時間
    assert sp._worker.seen_t == T + timedelta(days=1) and sp._worker.seen_at == 123.0


def test_history_records_one_line_per_trading_day(net, monkeypatch):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    path = sp.STATE_DIR / sp.HISTORY_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["t"] == T.isoformat() and record["market"] == sp.NEUTRAL
    assert [p["code"] for p in record["picks"]] == list(sp._snapshot.picks)
    assert all(set(p) == {"code", "name", "close", "score", "p60"} for p in record["picks"])
    assert os.stat(path).st_mode & 0o777 == 0o600
    net.overrides["STOCK_DAY_ALL"] = (304, None, None)
    sp._tick(stop)
    sp._append_history(sp._snapshot)  # 重啟後又記同一個 T：不重複
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    monkeypatch.setattr(sp, "HISTORY_MAX_BYTES", 10)
    newer = sp.Snapshot(**{**sp._snapshot.__dict__, "t": T + timedelta(days=1)})
    sp._append_history(newer)  # 檔案滿了就不寫
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_seeded_snapshot_is_recorded_once_when_the_worker_starts(net, monkeypatch):
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=time.time())
    sp._commit_snapshot(snapshot)
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0.0)
    ran = threading.Event()
    monkeypatch.setattr(sp, "_tick", lambda stop: ran.set() or 30.0)
    sp.start_background()
    assert ran.wait(5.0)
    sp.stop_background(timeout=2.0)
    lines = (sp.STATE_DIR / sp.HISTORY_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["t"] == T.isoformat()


def test_otc_refill_is_throttled(net, monkeypatch):
    net.overrides["tpex_mainboard_daily_close_quotes"] = sp.FetchError("timeout")
    _fresh_snapshot(net)
    monkeypatch.setattr(sp, "_otc_next_try", 0.0)
    sp._maybe_fill_otc(threading.Event())
    sp._maybe_fill_otc(threading.Event())
    assert net.count("tpex_mainboard_daily_close_quotes") == 2  # 重算那一次＋補抓一次，第二次補抓要等 2 小時


def test_twii_with_an_intraday_bar_after_t_still_works(net):
    later = T + timedelta(days=1)
    net.charts["^TWII"] = _chart(CAL + [later], [20000.0] * (len(CAL) + 1))
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert snapshot.t == T and snapshot.calendar[-1] == T


def test_parse_otc_rules():
    rows = [
        {"Date": "1151008", "SecuritiesCompanyCode": f"{6000 + i}", "CompanyName": f"櫃{i}", "Close": "10", "Change": "0"}
        for i in range(300)
    ] + [
        {"Date": "1151008", "SecuritiesCompanyCode": "00679B", "CompanyName": "元大美債20年", "Close": "29.5", "Change": "0.1 "},
        {"Date": "1151008", "SecuritiesCompanyCode": "6999", "CompanyName": "停牌", "Close": "", "Change": ""},
        {"Date": "1151008", "SecuritiesCompanyCode": "6000", "CompanyName": "櫃0", "Close": "11", "Change": "0"},
        {"Date": "1151008", "SecuritiesCompanyCode": "712345", "CompanyName": "權證", "Close": "1", "Change": "0"},
    ]
    table, no_trade = sp.parse_otc(rows, T)
    assert "6000" not in table and table["00679B"] == sp.OtcRow("元大美債20年", 29.5, 0.1)
    assert no_trade == {"6999": "停牌"} and "712345" not in table
    assert sp.parse_otc(rows, PREV) is None
    assert sp.parse_otc(rows[:100], T) is None



# ── Phase 6 第二輪（Codex q1）補的測試 ──────────────────────────────────────
def test_corrupt_ledger_backoff_is_not_shortened_by_the_round_failure(net):
    sp.STATE_DIR.mkdir(parents=True)
    (sp.STATE_DIR / sp.WORKER_NAME).write_text("{broken")
    before = time.time()
    delay = sp._tick(threading.Event())  # 存檔時發現壞檔：修好、退避 1 小時，這輪不開始重算
    assert sp._worker.last_fail == "state_corrupt"
    assert sp._worker.next_try >= before + sp.STATE_CORRUPT_BACKOFF_S - 1  # 一般失敗的 10 分鐘不能把 1 小時縮短
    assert delay >= sp.STATE_CORRUPT_BACKOFF_S - 5
    assert sp.load_worker_file().next_try == pytest.approx(sp._worker.next_try)


def test_every_request_in_a_round_fits_the_remaining_budget(net, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(sp.time, "monotonic", lambda: clock["now"])
    real_charts = sp._fetch_charts

    def slow_charts(symbols, stop, give_up):
        clock["now"] = give_up - 22  # Yahoo 用到只剩 22 秒：還會抓櫃買，但單次讀取不能等 25 秒
        return real_charts(symbols, stop, give_up)

    monkeypatch.setattr(sp, "_fetch_charts", slow_charts)
    sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    tpex = [lim for lim in net.limits if lim[0] == "tpex_mainboard_daily_close_quotes"]
    assert tpex and tpex[0][1] <= 22 and tpex[0][2] <= 22
    for endpoint, timeout, total in net.limits:
        assert timeout <= max(sp.BATCH_TIMEOUT_S, sp.T86_TIMEOUT_S, sp.YAHOO_TIMEOUT_S) and total <= sp.BATCH_TOTAL_S


def test_history_never_goes_backwards(net):
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    newer = sp.Snapshot(**{**snapshot.__dict__, "t": T + timedelta(days=1)})
    sp._append_history(newer)
    sp._append_history(snapshot)  # 舊的 T 接在新的後面：不寫
    lines = (sp.STATE_DIR / sp.HISTORY_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["t"] for line in lines] == [newer.t.isoformat()]


def test_full_name_of_a_company_without_a_trade_still_resolves(net):
    net.companies[1] = {**net.companies[1], "公司簡稱": "聯華", "公司名稱": "聯華實業控股股份有限公司"}
    net.companies[2] = {**net.companies[2], "公司簡稱": "聯電", "公司名稱": "聯華電子股份有限公司"}
    net.day_rows[2] = {**net.day_rows[2], "ClosingPrice": "--"}  # 聯電這天沒成交
    snapshot = _fresh_snapshot(net)
    umc = STOCKS[2]
    assert sp.resolve_query("聯華電子", snapshot.index) == ("code", umc)  # 不能對到「聯華」
    assert sp.handle("聯華電子").text == sp.no_trade_text(T)


def test_full_names_longer_than_short_names_are_matched(net):
    long_name = "中華民國測試用超長公司名稱電子工業"  # 17 個字
    net.companies[5] = {**net.companies[5], "公司簡稱": "超長", "公司名稱": long_name + "股份有限公司"}
    snapshot = _fresh_snapshot(net)
    assert sp.resolve_query(long_name, snapshot.index) == ("code", STOCKS[5])
    assert sp.resolve_query(long_name + "現在怎樣", snapshot.index) == ("code", STOCKS[5])



# ── Phase 6 第二輪（正確性 q3）補的測試 ─────────────────────────────────────
def _advance_one_trading_day(net: FakeNet) -> date:
    """合成市場往前一個交易日：證交所、本益比、T86、櫃買都換成新的 T，Yahoo 日線多 T 那根。"""
    t2 = T + timedelta(days=1)
    net.day_rows = [{**row, "Date": _roc(t2)} for row in net.day_rows]
    net.charts["^TWII"] = _chart(CAL + [t2], [20000.0] * (len(CAL) + 1))
    for code in STOCKS + ["0050"]:
        net.charts[f"{code}.TW"] = _chart(CAL, [100.0] * len(CAL))
    for d in (CAL + [t2])[-10:]:
        key = d.strftime("%Y%m%d")
        net.t86.setdefault(key, {**net.t86[CAL[-1].strftime("%Y%m%d")], "date": key})
    net.pe = [{**row, "Date": _roc(t2)} for row in net.pe]
    net.otc = [{**row, "Date": _roc(t2)} for row in net.otc]
    return t2


def test_stop_during_the_yahoo_batch_commits_nothing_and_is_not_a_failure(net, monkeypatch):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    old = sp._snapshot
    _advance_one_trading_day(net)
    real_charts = sp._fetch_charts

    def interrupted(symbols, stop_event, give_up):
        result = real_charts(symbols[:5], stop_event, give_up)  # 抓到一半
        stop.set()  # 重啟、部署
        return result

    monkeypatch.setattr(sp, "_fetch_charts", interrupted)
    assert sp._tick(stop) == sp.POLL_S
    assert sp._snapshot is old and sp.load_snapshot_file().t == T  # 一半的日線（大多 ⚪）不能寫成新快照
    assert sp._worker.fail_count == 0 and sp._worker.next_try is None
    assert net.count("2330.TW") >= 2  # 真的走到抓日線那一步才停


def test_second_trading_day_adds_a_history_line(net):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    t2 = _advance_one_trading_day(net)
    assert sp._tick(stop) == sp.POLL_S
    lines = (sp.STATE_DIR / sp.HISTORY_NAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["t"] for line in lines] == [T.isoformat(), t2.isoformat()]


def test_broken_last_history_line_is_skipped_and_not_glued(net):
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    path = sp.STATE_DIR / sp.HISTORY_NAME
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text('{"t":"2026-10-07"}\n{"t":"2026-10-08","mar', encoding="utf-8")  # 寫到一半就當機
    sp._append_history(snapshot)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"t":"2026-10-08","mar' and json.loads(lines[2])["t"] == T.isoformat()
    sp._append_history(snapshot)  # 同一個 T 不再記
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_pending_recompute_is_not_cleared_by_an_older_node(net):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    sp._worker.seen_t, sp._worker.seen_at = T + timedelta(days=1), time.time()
    sp._worker.fail_count, sp._worker.last_fail = 3, "t86_not_yet"
    sp._worker.last_wait = "pe_not_t"
    sp._worker.last_modified = "Sat, 10 Oct 2026 21:20:43 GMT"
    net.overrides["STOCK_DAY_ALL"] = (200, net.day_rows, "Thu, 08 Oct 2026 21:20:43 GMT")  # 舊節點回 T
    sp._tick(stop)
    assert sp._worker.fail_count == 3 and sp._worker.last_fail == "t86_not_yet"
    assert sp._worker.last_wait == "pe_not_t"  # 還在等新日期：不能被舊節點清掉
    assert sp._worker.last_modified == "Sat, 10 Oct 2026 21:20:43 GMT"


# ── Phase 6 第二輪（安全＋架構 q4）補的測試 ─────────────────────────────────
def test_future_times_in_the_worker_file_are_clamped():
    now = time.time()
    raw = sp.worker_state_to_json(sp.WorkerState(
        last_ok_check=now + 30 * 86400, seen_t=date(2099, 1, 1), seen_at=now + 30 * 86400,
        next_try=now + 365 * 86400, t86_requests=[now - 10, now + 86400 * 5],
    ))
    state = sp.worker_state_from_json(json.loads(json.dumps(raw)))
    assert state.last_ok_check <= time.time() + 601
    assert state.seen_t is None and state.seen_at is None
    assert state.next_try <= time.time() + sp._MAX_BACKOFF_S + 601
    assert len(state.t86_requests) == 2  # 未來的請求時間夾回現在、不丟（帳不能變少）
    assert state.t86_requests[0] == pytest.approx(now - 10) and state.t86_requests[1] <= time.time() + 1


def test_a_trading_date_after_today_is_rejected(net, monkeypatch):
    monkeypatch.setattr(sp, "_taipei_today", lambda: T - timedelta(days=1))
    assert sp._tick(threading.Event()) == sp.BACKOFF_S[0]
    assert sp._worker.last_fail == "day_bad" and sp._snapshot is None


def test_a_corrupted_history_date_does_not_block_new_lines(net):
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    path = sp.STATE_DIR / sp.HISTORY_NAME
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text('{"t":"2026-10-07"}\n{"t":"9999"}\n{"t":"9999-12-31"}\n', encoding="utf-8")  # 被改壞的日期
    sp._append_history(snapshot)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["t"] == T.isoformat() and len(lines) == 4



# ── Phase 6 第三輪補的測試 ─────────────────────────────────────────────────
def test_fail_never_shortens_a_later_deadline():
    later = time.time() + 3600
    sp._worker.next_try = later
    delay = sp._fail("t86_not_yet")
    assert sp._worker.next_try == pytest.approx(later) and delay >= 3590
    sp._worker.next_try = time.time() - 1  # 過期的期限照一般退避
    assert sp._fail("t86_not_yet") == pytest.approx(sp.BACKOFF_S[1], abs=1)


def test_other_process_cooldown_stops_the_round_before_any_fetch(net):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    disk = sp.load_worker_file()
    disk.next_try, disk.writer = time.time() + 3000, "other-process"
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, sp.worker_state_to_json(disk))
    _advance_one_trading_day(net)
    net.calls.clear()
    wait = sp._tick(stop)
    assert 2900 < wait <= 3000
    assert net.count("t187ap03_L") == 0 and net.count("T86") == 0  # 沒開始重算


def test_corrupt_worker_file_at_startup_is_repaired_once(net):
    sp.STATE_DIR.mkdir(parents=True)
    (sp.STATE_DIR / sp.WORKER_NAME).write_text("{broken")
    sp._loaded = False
    sp._ensure_loaded()
    repaired = sp.load_worker_file()  # 載入時就修好，退避 1 小時一起存
    assert repaired.last_fail == "state_corrupt" and repaired.next_try > time.time() + 3000
    sp._worker.next_try = time.time() - 1  # 1 小時後
    assert sp._tick(threading.Event()) == sp.POLL_S  # 不會再退避一次
    assert sp._snapshot is not None


def test_status_summary_from_another_process_only_reads(net):
    _fresh_snapshot(net)
    with sp._lock:
        sp._save_worker()
    stale = sp.STATE_DIR / ".stock_picks_tw.json.1.2.3.tmp"
    stale.write_text("x")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    sp._loaded, sp._snapshot, sp._worker = False, None, sp.WorkerState()  # 像每日維護另外開的程序
    info = sp.status_summary()
    assert info["status"] == "ok" and info["data_date"] == T.isoformat()
    assert sp._loaded is False and sp._snapshot is None and stale.exists()  # 不載入、不清暫存檔


def test_no_trade_sentence_names_the_date():
    assert sp.no_trade_text(T) == "股票評估：這一檔 10/08 沒有成交，查不到收盤"


# ── 2026-10-17：Yahoo 還沒有 T 時用證交所補 T 收盤；「還沒出來」與真的錯誤分開重試 ─────────────
def _ramp(days, start=20000.0, step=10.0) -> dict:
    return {d: start + step * i for i, d in enumerate(days)}


def _index_rows(days, closes, prev_close) -> dict:
    """證交所 FMTQIK 解析後的樣子：{日期: (收盤, 漲跌)}，漲跌＝收盤−前一個交易日收盤。"""
    rows, prev = {}, prev_close
    for d in days:
        rows[d] = (closes[d], round(closes[d] - prev, 2))
        prev = closes[d]
    return rows


OCT = [d for d in CAL if (d.year, d.month) == (2026, 10)]  # 10/01–10/08（合成日曆只有平日）
SEPT = [d for d in CAL if d < date(2026, 10, 1)]


def test_splice_mid_month_uses_the_twse_close():
    closes = _ramp(CAL)
    bars = {d: closes[d] for d in CAL[:-1]}  # Yahoo 還沒有 T
    rows = _index_rows(OCT, closes, closes[CAL[CAL.index(OCT[0]) - 1]])
    close, why, detail = sp.splice_t_close(bars, rows, T)
    assert close == closes[T] and why == "" and "yahoo_prev=" in detail


def test_splice_never_splices_at_a_month_start():
    """月初沒有獨立證據證明 Yahoo 停在前一個交易日：一律等 Yahoo（2026-10-17 審查第 2 輪）。"""
    sept = [d for d in CAL if d < date(2026, 10, 1)]
    closes = _ramp(sept + [date(2026, 10, 1)])
    rows = _index_rows([date(2026, 10, 1)], closes, closes[sept[-1]])
    bars = {d: closes[d] for d in sept}
    assert sp.splice_t_close(bars, rows, date(2026, 10, 1))[:2] == (None, "index_month_start")
    behind = {d: closes[d] for d in sept[:-1]}  # Yahoo 停在前前一天
    assert sp.splice_t_close(behind, rows, date(2026, 10, 1))[:2] == (None, "index_month_start")


def test_splice_month_start_catches_a_flat_missing_day():
    """Codex 審查的反例：9/29、9/30 收盤一樣，Yahoo 停在 9/29；只比數值會收下。月初一律不補，所以擋得住。"""
    sept = [d for d in CAL if d < date(2026, 10, 1)]
    bars = {d: 20000.0 for d in sept[:-1]}  # Yahoo 少了 9/30
    rows = {date(2026, 10, 1): (20100.0, 100.0)}  # 9/30 也是 20000
    assert sp.splice_t_close(bars, rows, date(2026, 10, 1))[:2] == (None, "index_month_start")


def test_splice_january_and_an_extra_holiday_bar():
    dec = _business_days(30, date(2026, 12, 31))
    t = date(2027, 1, 4)  # 1/1 休市
    closes = _ramp(dec + [t])
    rows = _index_rows([t], closes, closes[dec[-1]])
    bars = {d: closes[d] for d in dec}
    assert sp.splice_t_close(bars, rows, t)[:2] == (None, "index_month_start")
    with_holiday = {**bars, date(2027, 1, 1): closes[dec[-1]]}  # Yahoo 在休市日多一根（照抄前一天收盤）
    assert sp.splice_t_close(with_holiday, rows, t)[1] == "index_gap"


@pytest.mark.parametrize(
    "case, reason",
    [
        ("missing_t", "index_not_t"),
        ("yahoo_two_days_behind_flat", "index_gap"),
        ("yahoo_extra_bar_mid_month", "index_gap"),
        ("off_by_0_06", "index_mismatch"),
        ("jump", "index_jump"),
    ],
)
def test_splice_rejects(case, reason):
    closes = {d: 20000.0 for d in CAL}  # 平盤：只能靠日期擋
    bars = {d: closes[d] for d in CAL[:-1]}
    rows = _index_rows(OCT, closes, 20000.0)
    if case == "missing_t":
        rows.pop(T)
    elif case == "yahoo_two_days_behind_flat":
        bars.pop(CAL[-2])
    elif case == "yahoo_extra_bar_mid_month":
        bars[date(2026, 10, 3)] = 20000.0  # 週六
    elif case == "off_by_0_06":
        bars[CAL[-2]] = 20000.06
    elif case == "jump":
        rows[T] = (20000.0 * 1.13 + 0.0, 20000.0 * 0.13)
        bars[CAL[-2]] = 20000.0
    assert sp.splice_t_close(bars, rows, T)[:2] == (None, reason)


def test_splice_tolerates_float_noise_and_rows_after_t():
    closes = {d: 20000.0 for d in CAL}
    bars = {d: closes[d] for d in CAL[:-1]}
    bars[CAL[-2]] = 20000.04  # Yahoo 的 float 表示法誤差（實測 ≤ 0.002）
    rows = _index_rows(OCT, closes, 20000.0)
    rows[T + timedelta(days=1)] = (20100.0, 100.0)  # 晚於 T 的列不影響
    assert sp.splice_t_close(bars, rows, T)[0] == 20000.0


def test_parse_fmtqik_drops_bad_and_conflicting_rows():
    rows = [
        {"Date": "1151001", "TAIEX": "48,353.49", "Change": "413.36"},
        {"Date": "1151002", "TAIEX": "48475.74", "Change": "+122.25"},
        {"Date": "1151005", "TAIEX": "49712.04", "Change": "--"},  # 漲跌缺值
        {"Date": "1151006", "TAIEX": "0", "Change": "1.0"},  # 收盤 ≤ 0
        {"Date": "115/10/07", "TAIEX": "49806.37", "Change": "-16.18"},  # 日期格式不對
        {"Date": "1151008", "TAIEX": "49313.44", "Change": "-492.93"},
        {"Date": "1151008", "TAIEX": "49313.45", "Change": "-492.92"},  # 同一天兩個不同的值 → 那天不收
        {"Date": "1151012", "TAIEX": "49301.96", "Change": "-11.48"},
        {"Date": "1151012", "TAIEX": "49301.96", "Change": "-11.48"},  # 一模一樣的重複可以
        "not a dict",
    ]
    parsed = sp.parse_fmtqik(rows)
    assert parsed == {
        date(2026, 10, 1): (48353.49, 413.36), date(2026, 10, 2): (48475.74, 122.25),
        date(2026, 10, 12): (49301.96, -11.48),
    }
    assert sp.parse_fmtqik({"not": "a list"}) == {} and sp.parse_fmtqik(None) == {}


def test_refresh_without_yahoo_t_uses_the_twse_close(net):
    net.charts["^TWII"] = _chart(CAL[:-1], [20000.0] * (len(CAL) - 1))
    net.fmtqik[-1] = {"Date": _roc(T), "TAIEX": "22000.00", "Change": "2000.00"}  # 前收 20000、漲 10%
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert snapshot.calendar[-1] == T and net.count("FMTQIK") == 1
    assert snapshot.market.level == sp.HOT  # 用的是證交所的 22000，不是 Yahoo


def test_refresh_with_yahoo_t_never_asks_twse_for_the_index(net):
    sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert net.count("FMTQIK") == 0


def _closed_day_market(net, *, with_yahoo_t: bool) -> date:
    """2026-10-23 的樣子：CAL[-2] 臨時休市——^TWII、證交所 FMTQIK、T86 都沒有那天，個股的 Yahoo 日線卻有一根
    照抄前一天收盤的平盤（FakeNet 預設的個股日線本來就是平盤，所以那根就在裡面）。"""
    closed = CAL[-2]
    days = [d for d in CAL if d != closed]
    if not with_yahoo_t:
        days.remove(T)
    net.charts["^TWII"] = _chart(days, [20000.0] * len(days))
    net.fmtqik[:] = [row for row in net.fmtqik if row["Date"] != _roc(closed)]
    older = CAL[-11].strftime("%Y%m%d")  # 少了休市日，最後 10 個交易日的 T86 要往前多一天
    net.t86[older] = {**net.t86[T.strftime("%Y%m%d")], "date": older}
    return closed


@pytest.mark.parametrize("with_yahoo_t", [False, True])
def test_refresh_computes_t_right_after_a_closed_day(net, with_yahoo_t):
    closed = _closed_day_market(net, with_yahoo_t=with_yahoo_t)
    snapshot = sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert snapshot.calendar[-1] == T and closed not in snapshot.calendar and len(snapshot.calendar) == 240
    assert net.count("FMTQIK") == (0 if with_yahoo_t else 1)  # Yahoo 沒有 T 時照舊用證交所補
    assert net.count("T86") == 10 and closed.strftime("%Y%m%d") not in [c[2].get("date") for c in net.calls]
    assert all(snapshot.pool[c].trend is not None for c in snapshot.universe)


def test_refresh_still_holds_when_the_calendar_skips_a_real_trading_day(net):
    """同樣的日曆少一天，但那天個股真的有成交（收盤不是平盤）：不能悄悄跳過，整輪 too_many_gray。"""
    missing = _closed_day_market(net, with_yahoo_t=True)
    closes = [100.0] * (len(CAL) - 1)
    closes[CAL.index(missing)] = 103.0
    for code in STOCKS + ["0050"]:
        net.charts[f"{code}.TW"] = _chart(CAL[:-1], closes)
    with pytest.raises(sp.RoundFailed) as info:
        sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert info.value.kind == "too_many_gray"


def _no_yahoo_t(n):
    n.charts["^TWII"] = _chart(CAL[:-1], [20000.0] * (len(CAL) - 1))


@pytest.mark.parametrize(
    "breakage, kind, not_yet, blocked, retry_s",
    [
        (lambda n: (_no_yahoo_t(n), n.fmtqik.pop()), "index_not_t", True, False, None),
        (lambda n: (_no_yahoo_t(n), n.overrides.__setitem__("FMTQIK", sp.FetchError("timeout"))), "index_timeout", True, False, None),
        (lambda n: (_no_yahoo_t(n), n.overrides.__setitem__("FMTQIK", sp.FetchError("status"))), "index_status", True, False, None),
        (lambda n: (_no_yahoo_t(n), n.overrides.__setitem__("FMTQIK", sp.FetchError("blocked"))), "index_blocked", False, True, 3600),
        (lambda n: (n.charts.__setitem__("^TWII", _chart(SEPT, [20000.0] * len(SEPT))),
                    setattr(n, "fmtqik", [r for r in n.fmtqik if r["Date"] == _roc(T)])), "index_month_start", True, False, None),
        (lambda n: n.charts.pop("^TWII"), "twii_unavailable", False, False, None),
        (lambda n: n.charts.__setitem__("^TWII", _chart([], [])), "twii_bad", False, False, None),
        (lambda n: setattr(n, "pe", [{**row, "Date": _roc(PREV)} for row in n.pe]), "pe_not_t", True, False, None),
        (lambda n: n.pe.__setitem__(0, {**n.pe[0], "Date": _roc(PREV)}), "pe_bad", False, False, None),
        (lambda n: n.overrides.__setitem__("BWIBBU_ALL", sp.FetchError("blocked")), "batch_blocked", False, True, 3600),
        (lambda n: n.overrides.__setitem__("BWIBBU_ALL", sp.FetchError("status")), "batch_status", False, False, None),
        (lambda n: n.t86.pop(CAL[-1].strftime("%Y%m%d")), "t86_not_yet", True, False, 3600),
        (lambda n: n.t86.pop(CAL[-3].strftime("%Y%m%d")), "t86_gap", False, False, 3600),
        (lambda n: n.overrides.__setitem__("T86", sp.FetchError("timeout")), "t86_timeout", False, False, 3600),
        (lambda n: n.overrides.__setitem__("T86", sp.FetchError("not_json")), "t86_not_json", False, True, 3600),
        (lambda n: n.overrides.__setitem__("T86", (200, {"stat": "系統忙碌中"}, None)), "t86_bad", False, False, 3600),
    ],
)
def test_every_raise_site_says_how_to_retry(net, breakage, kind, not_yet, blocked, retry_s):
    breakage(net)
    with pytest.raises(sp.RoundFailed) as info:
        sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert (info.value.kind, info.value.not_yet, info.value.blocked, info.value.retry_s) == (kind, not_yet, blocked, retry_s)


def test_t86_not_yet_texts_are_recognised():
    future = {"stat": "查詢日期大於可查詢最大日期，請重新查詢!", "total": 0}
    holiday = {"stat": "很抱歉，沒有符合條件的資料!", "total": 0}
    assert sp.parse_t86(future, T)[0] == sp.T86_NOT_YET and sp.parse_t86(holiday, T)[0] == sp.T86_NOT_YET
    assert sp.parse_t86({"stat": "系統忙碌中，請稍後再試"}, T)[0] == sp.T86_BAD


def test_round_failed_flags_are_sanitised():
    assert sp.RoundFailed("x", blocked=True, not_yet=True).not_yet is False  # 都設時當錯誤
    for bad in (float("nan"), float("inf"), -1, 0, True, "60", 10**400):
        assert sp.RoundFailed("x", retry_s=bad).retry_s is None
    assert sp.RoundFailed("x", retry_s=60).retry_s == 60.0


def test_not_yet_waits_fifteen_minutes_and_is_not_a_failure(net):
    stop = threading.Event()
    assert sp._tick(stop) == sp.POLL_S
    t2 = _advance_one_trading_day(net)
    net.charts["^TWII"] = _chart(CAL, [20000.0] * len(CAL))  # Yahoo 還沒有 t2，證交所也還沒有
    assert sp._tick(stop) == pytest.approx(sp.NOT_YET_RETRY_S, abs=2) and sp.NOT_YET_RETRY_S == 900
    assert sp._worker.fail_count == 0 and sp._worker.last_fail == "" and sp._worker.last_wait == "index_not_t"
    assert sp.load_worker_file().last_wait == "index_not_t"
    summary = sp.status_summary()
    assert summary["waiting"] == "index_not_t" and summary["fail_count"] == 0
    sp._worker.next_try = time.time() - 1
    net.fmtqik.append({"Date": _roc(t2), "TAIEX": "20000.00", "Change": "0.00"})  # 證交所出來了
    assert sp._tick(stop) == sp.POLL_S and sp._snapshot.t == t2
    assert sp._worker.last_wait == "" and sp.status_summary()["waiting"] is None
    assert isinstance(sp.status_summary()["lag_minutes"], int)


def test_real_errors_wait_ten_then_twenty_minutes(net):
    sp._worker.last_wait = "pe_not_t"  # 真的錯誤要清掉「在等」
    delays = []
    for _ in range(3):
        sp._worker.next_try = None
        delays.append(round(sp._fail("batch_status")))
    assert delays == [600, 1200, 1200] and sp.BACKOFF_S == (600, 1200)
    sp._worker.next_try = None
    assert round(sp._fail("yahoo_rate_limited", blocked=True)) == sp.BLOCKED_BACKOFF_S == 1200
    sp._worker.next_try = None
    assert round(sp._fail("t86_timeout", retry_s=sp.T86_RETRY_S)) == sp.T86_RETRY_S == 3600
    assert sp._worker.last_wait == ""


def test_not_yet_never_shortens_a_longer_wait(net):
    sp._worker.next_try = time.time() + 3000  # 別的程序的冷卻、壞檔的 1 小時
    assert sp._fail("index_not_t", not_yet=True) == pytest.approx(3000, abs=2)
    assert sp._worker.fail_count == 0


def test_every_wait_fits_under_the_load_clamp():
    for value in (*sp.BACKOFF_S, sp.BLOCKED_BACKOFF_S, sp.NOT_YET_RETRY_S, sp.T86_RETRY_S, sp.TWSE_BLOCKED_RETRY_S,
                  sp.STATE_CORRUPT_BACKOFF_S):
        assert sp._MAX_BACKOFF_S >= value


@pytest.mark.parametrize("value", [["x"], "a" * 100, 5, None, "bad\nline"])
def test_a_strange_last_wait_never_resets_the_t86_ledger(value):
    state = sp.worker_state_to_json(sp.WorkerState(t86_requests=[time.time() - 60], last_wait="pe_not_t"))
    state["last_wait"] = value
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, state)
    loaded = sp.load_worker_file()
    assert loaded.last_fail != "state_corrupt" and len(loaded.t86_requests) == 1
    assert loaded.last_wait == ("a" * 40 if value == "a" * 100 else "")


def test_an_old_state_file_without_last_wait_still_loads():
    state = sp.worker_state_to_json(sp.WorkerState(fail_count=2))
    del state["last_wait"]
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, state)
    assert sp.load_worker_file().fail_count == 2 and sp.load_worker_file().last_wait == ""


def test_deeply_nested_json_is_not_json(monkeypatch):
    deep = b"[" * 100_000 + b"]" * 100_000
    _fake_get(monkeypatch, response=_Resp(body=deep))
    with pytest.raises(sp.FetchError) as info:
        _REAL_GET_JSON("openapi.twse.com.tw", "/x", max_bytes=10_000_000, timeout=5, total_s=5)
    assert info.value.kind == "not_json"


def test_lag_minutes_is_only_for_the_newest_date(net):
    snap = _fresh_snapshot(net)
    worker = sp.WorkerState(seen_t=snap.t, seen_at=snap.computed_at - 125)
    assert sp._lag_minutes(snap, worker) == 2
    assert sp._lag_minutes(snap, sp.WorkerState(seen_t=snap.t - timedelta(days=1), seen_at=0.0)) is None
    assert sp._lag_minutes(snap, sp.WorkerState(seen_t=snap.t, seen_at=snap.computed_at + 60)) is None
    assert sp._lag_minutes(None, worker) is None



def test_t86_cooldown_and_round_budget_wait_an_hour(net, monkeypatch):
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    other = sp.WorkerState(next_try=time.time() + 60, writer="other-process")
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, sp.worker_state_to_json(other))
    with pytest.raises(sp.RoundFailed) as info:
        sp._reserve_t86_request(threading.Event(), time.monotonic() + 60)
    assert (info.value.kind, info.value.retry_s) == ("t86_cooldown", 3600)
    sp._worker.next_try = None
    (sp.STATE_DIR / sp.WORKER_NAME).unlink()
    monkeypatch.setattr(sp, "T86_SPACING_S", 5.0)
    sp._worker.t86_requests = [time.time()]  # 間隔還沒到，整輪又沒時間了
    with pytest.raises(sp.RoundFailed) as info:
        sp._reserve_t86_request(threading.Event(), time.monotonic() - 1)
    assert (info.value.kind, info.value.retry_s) == ("budget_t86", 3600)


def test_huge_json_numbers_do_not_crash_the_parsers():
    assert sp._num(10**400) is None
    rows = [{"Date": "1151001", "TAIEX": 10**400, "Change": "0"}, {"Date": "1151002", "TAIEX": "48475.74", "Change": 10**400}]
    assert sp.parse_fmtqik(rows) == {}


def test_fail_ignores_a_bad_retry_value(net):
    for bad in (float("nan"), -5, float("inf")):
        sp._worker.next_try, sp._worker.fail_count = None, 0
        assert round(sp._fail("batch_status", retry_s=bad)) == sp.BACKOFF_S[0]


def test_fail_reports_the_wait_after_merging_another_process(net):
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    other = sp.WorkerState(next_try=time.time() + 3000, writer="other-process")
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, sp.worker_state_to_json(other))
    sp._worker.next_try = None
    assert sp._fail("index_not_t", not_yet=True) == pytest.approx(3000, abs=5)


def test_round_budget_running_out_at_the_t86_stage_waits_an_hour(net, monkeypatch):
    real = sp.parse_revenue

    def then_out_of_time(rows):  # 批次處理完、進 T86 迴圈前，整輪期限已過
        monkeypatch.setattr(sp.time, "monotonic", lambda: 1e12)
        return real(rows)

    monkeypatch.setattr(sp, "parse_revenue", then_out_of_time)
    with pytest.raises(sp.RoundFailed) as info:
        sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert (info.value.kind, info.value.retry_s) == ("budget_t86", 3600)


@pytest.mark.parametrize("stamp_huge", [True, False])
def test_huge_numbers_in_a_yahoo_chart_are_skipped(stamp_huge):
    payload = _chart([CAL[-3], CAL[-2]], [100.0, 101.0])
    result = payload["chart"]["result"][0]
    result["timestamp"].append(10**400 if stamp_huge else _ts(CAL[-1]))  # 一次只讓一個值超大，兩條檢查都測得到
    result["indicators"]["quote"][0]["close"].append(102.0 if stamp_huge else 10**400)
    assert sp.parse_chart(payload) == {CAL[-3]: 100.0, CAL[-2]: 101.0}


def test_not_yet_without_a_snapshot_is_not_a_failed_first_round(net):
    sp._snapshot = None
    sp._fail("index_not_t", not_yet=True)
    assert sp._worker.first_round_failed is False  # 使用者看到「準備中」，不是「暫時抓不到」
    sp._fail("batch_status")
    assert sp._worker.first_round_failed is True


def test_a_huge_retry_is_capped_at_the_longest_wait(net):
    sp._worker.next_try = None
    assert round(sp._fail("x", retry_s=1e7)) == sp._MAX_BACKOFF_S == 3600


def test_month_start_never_asks_twse_for_the_index(net):
    net.charts["^TWII"] = _chart(SEPT, [20000.0] * len(SEPT))  # Yahoo 在 T 這個月還沒有日線
    with pytest.raises(sp.RoundFailed) as info:
        sp.refresh(T, _day_rows(net), threading.Event(), now=1.0e9)
    assert (info.value.kind, info.value.not_yet) == ("index_month_start", True) and net.count("FMTQIK") == 0
