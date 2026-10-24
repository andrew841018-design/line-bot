"""market_sources_us 的解析與抓取測試（/盤勢，2026-10-17；PLAN §11、INTERFACES §3 A、§8）。

fixture 都是照 實測抓下來的真實格式（樣本不進 repo）裁小或合成：
JSON 只留幾列、HTML 用最小結構、xlsx 在測試裡用 openpyxl 產生；CFTC 三列是公開報告的原樣。
沒有任何家人資料、email；不連網（抓取一律注入假的 http）。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import openpyxl
import pytest

import market_sources_us as src
from market_sources_us import ParseError

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
HERE = Path(__file__).resolve().parent


# ── 共用小工具 ────────────────────────────────────────────────────────────
def error_of(fn, *args, **kwargs) -> ParseError:
    with pytest.raises(ParseError) as caught:
        fn(*args, **kwargs)
    return caught.value


def kind_of(fn, *args, **kwargs) -> str:
    return error_of(fn, *args, **kwargs).kind


def guard_reason(fn, *args, **kwargs) -> tuple[str, str]:
    """(kind, detail)：防護測試要確定是防護擋下的，不是 openpyxl 自己剛好讀不了。"""
    error = error_of(fn, *args, **kwargs)
    return error.kind, getattr(error, "detail", "")


def nasdaq(data) -> dict:
    return {"data": data, "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}


class FakeHttp:
    """假的 market_http：記下每次呼叫的參數，回固定內容。"""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[dict] = []

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        self.calls.append({"url": url, "max_bytes": max_bytes, "timeout": timeout, "accept": accept,
                           "headers": headers, "ledger": ledger})
        return SimpleNamespace(status=200, body=self.body, headers={}, url=url)


def as_bytes(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def cusip(base8: str) -> str:
    """測試自己算 CUSIP 檢查碼（和被測程式分開寫）。"""
    values = []
    for index, ch in enumerate(base8):
        value = int(ch) if ch.isdigit() else ord(ch) - 55
        values.append(value * 2 if index % 2 else value)
    total = sum(v // 10 + v % 10 for v in values)
    return base8 + str((10 - total % 10) % 10)


# ── 代號 ──────────────────────────────────────────────────────────────────
def test_canonical_symbol_rules_per_source():
    assert src.canonical_us_symbol("BRK.B", "ssga") == "BRK.B"
    assert src.canonical_us_symbol("BRK/B", "nasdaq") == "BRK.B"
    assert src.canonical_us_symbol("brk.b.us", "moneydj") == "BRK.B"
    assert src.canonical_us_symbol("INTC", "moneydj") is None  # MoneyDJ 一定帶 .US
    assert src.canonical_us_symbol("2330.TW", "moneydj") is None
    assert src.canonical_us_symbol("BRK-B", "yahoo") == "BRK.B"
    assert src.canonical_us_symbol(" ＱＱＱ ", "cboe") == "QQQ"  # 全形先 NFKC
    for bad in ("ABCDEF", "BRK.BB", "2602335D", "-", "", "A B"):
        assert src.canonical_us_symbol(bad, "ssga") is None
    assert src.canonical_us_symbol(None, "nasdaq") is None
    with pytest.raises(ValueError):
        src.canonical_us_symbol("AAPL", "bloomberg")


def test_to_source_symbol_and_back():
    assert src.to_source_symbol("BRK.B", "yahoo") == "BRK-B"
    assert src.to_source_symbol("BRK.B", "moneydj") == "BRK.B.US"
    assert src.to_source_symbol("BRK.B", "nasdaq") == "BRK.B"
    for source in ("yahoo", "moneydj", "nasdaq", "ssga", "cboe"):
        assert src.canonical_us_symbol(src.to_source_symbol("BRK.B", source), source) == "BRK.B"
    with pytest.raises(ValueError):
        src.to_source_symbol("BRK/B", "yahoo")


# ── Nasdaq historical ────────────────────────────────────────────────────
AAPL_ROWS = [  # 裁自 nasdaq_hist_aapl_trim.json（新的在上、個股帶 $）
    {"date": "10/16/2026", "close": "$343.46", "volume": "59,946,370", "open": "$346.085", "high": "$347.82", "low": "$340.46"},
    {"date": "10/15/2026", "close": "$342.00", "volume": "39,767,260", "open": "$346.95", "high": "$347.85", "low": "$341.60"},
    {"date": "10/14/2026", "close": "$342.96", "volume": "50,061,050", "open": "$344.605", "high": "$347.09", "low": "$341.72"},
]
QQQ_ROWS = [  # 裁自 nasdaq_hist_qqq_trim.json（ETF 不帶 $）
    {"date": "10/16/2026", "close": "709.80", "volume": "66,926,660", "open": "725.52", "high": "725.96", "low": "707.23"},
    {"date": "10/15/2026", "close": "728.00", "volume": "49,828,710", "open": "718.55", "high": "729.43", "low": "715.435"},
]
KNDI_ROWS = [  # 1:10 反分割 10/16 生效：Nasdaq 已調整，前後連續（PLAN §16）
    {"date": "10/16/2026", "close": "$3.77", "volume": "84,078", "open": "$4.15", "high": "$4.2143", "low": "$3.61"},
    {"date": "10/15/2026", "close": "$4.125", "volume": "33,878", "open": "$3.80", "high": "$4.20", "low": "$3.80"},
    {"date": "10/14/2026", "close": "$4.03", "volume": "91,085", "open": "$4.80", "high": "$4.80", "low": "$4.03"},
]


def hist(symbol, rows):
    headers = {"date": "Date", "close": "Close/Last", "volume": "Volume", "open": "Open", "high": "High", "low": "Low"}
    return nasdaq({"symbol": symbol, "totalRecords": len(rows), "tradesTable": {"asOf": None, "headers": headers, "rows": rows}})


def test_historical_stock_and_etf_formats_oldest_first():
    bars = src.parse_nasdaq_historical(hist("AAPL", AAPL_ROWS), expect_symbol="AAPL")
    assert [b.date for b in bars] == [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
    assert bars[-1] == src.Bar(date(2026, 10, 16), 343.46, 59946370, 346.085, 347.82, 340.46)
    etf = src.parse_nasdaq_historical(as_bytes(hist("QQQ", QQQ_ROWS)))
    assert etf[-1].close == 709.80 and etf[-1].volume == 66926660 and etf[0].date == date(2026, 10, 15)


def test_historical_is_already_split_adjusted():
    bars = src.parse_nasdaq_historical(hist("KNDI", KNDI_ROWS))
    ratio = bars[-1].close / bars[-2].close
    assert 0.85 < ratio < 1.15  # 沒有 10 倍跳動


def test_historical_duplicates_and_missing_values():
    rows = AAPL_ROWS + [dict(AAPL_ROWS[0])]
    assert len(src.parse_nasdaq_historical(hist("AAPL", rows))) == 3  # 完全一樣的重複列合併
    changed = dict(AAPL_ROWS[0], close="$343.47")
    assert kind_of(src.parse_nasdaq_historical, hist("AAPL", AAPL_ROWS + [changed])) == "conflict"
    na = [dict(QQQ_ROWS[0], volume="N/A", open="--")]
    bar = src.parse_nasdaq_historical(hist("QQQ", na))[0]
    assert bar.volume is None and bar.open is None and bar.close == 709.80


@pytest.mark.parametrize(
    "payload, kind",
    [
        (nasdaq(None), "empty"),  # 代號不存在：data 是 null
        (nasdaq({"symbol": "AAPL", "tradesTable": None}), "empty"),
        (hist("AAPL", []), "empty"),
        (nasdaq({"symbol": "AAPL", "tradesTable": {"rows": "x"}}), "format"),
        (hist("AAPL", [dict(AAPL_ROWS[0], date="2026-10-16")]), "date"),
        (hist("AAPL", [dict(AAPL_ROWS[0], date="13/45/2026")]), "date"),
        (hist("AAPL", [dict(AAPL_ROWS[0], date="")]), "missing_field"),
        (hist("AAPL", [dict(AAPL_ROWS[0], close="")]), "missing_field"),
        (hist("AAPL", [dict(AAPL_ROWS[0], close="abc")]), "format"),
        (hist("AAPL", [dict(AAPL_ROWS[0], close="$0.00")]), "format"),
        (hist("AAPL", [dict(AAPL_ROWS[0], volume="-5")]), "format"),
        (hist("AAPL", [dict(AAPL_ROWS[0], volume="1.5")]), "format"),
        (hist("AAPL", ["not a row"]), "format"),
        ([1, 2], "format"),
        (b"{not json", "format"),
        (b"\xff\xfe\x00{", "format"),
    ],
)
def test_historical_errors(payload, kind):
    assert kind_of(src.parse_nasdaq_historical, payload) == kind


def test_historical_rejects_another_symbol():
    assert kind_of(src.parse_nasdaq_historical, hist("AAPL", AAPL_ROWS), expect_symbol="MSFT") == "format"


# BF.B 2026-10-23 的活回應原樣（investigations/format_20261023.md §1.1）：Nasdaq 沒有這檔的資料
BF_B_SHELL = {"data": {"symbol": None, "totalRecords": 0, "tradesTable": None}, "message": None,
              "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}
# 廠商錯誤的外殼是假設的形狀（審查 r7b F2；repo 沒有真實樣本，a17f7a9 只記了 COO／CPRT 的「2001」）。
# TODO: 抓到真實的錯誤回應後存進 fixtures_raw，換成原樣。
VENDOR_ERROR = {"data": None, "message": None,
                "status": {"rCode": 400, "bCodeMessage": [{"code": 2001, "errorMessage": "Error while calling vendor"}],
                           "developerMessage": None}}


def test_historical_empty_shell_is_empty_not_a_symbol_mismatch():
    """2026-10-23：空殼要先判成 empty——以前先比代號，null 對不上就是 format，us.hist.backfill 連續 44 次失敗。
    代號不是 null 卻對不上才是 format。"""
    assert kind_of(src.parse_nasdaq_historical, as_bytes(BF_B_SHELL), expect_symbol="BF.B") == "empty"
    rows_null = nasdaq({"symbol": None, "totalRecords": 0, "tradesTable": {"asOf": None, "headers": {}, "rows": None}})
    assert kind_of(src.parse_nasdaq_historical, rows_null, expect_symbol="BF.B") == "empty"
    blank = nasdaq({"symbol": " ", "tradesTable": None})
    assert kind_of(src.parse_nasdaq_historical, blank, expect_symbol="BF.B") == "empty"
    assert kind_of(src.parse_nasdaq_historical, hist("AAPL", AAPL_ROWS), expect_symbol="MSFT") == "format"
    # 代號是 null 卻有列：認不出是哪一檔，照樣 format；totalRecords 是 False 不能當成 0
    assert kind_of(src.parse_nasdaq_historical, hist(None, AAPL_ROWS), expect_symbol="AAPL") == "format"
    no_count = nasdaq(dict(hist(None, AAPL_ROWS)["data"], totalRecords=False))
    assert kind_of(src.parse_nasdaq_historical, no_count, expect_symbol="AAPL") == "format"
    # 真的抓取路徑（帶 expect_symbol）也一樣
    http = FakeHttp(as_bytes(BF_B_SHELL))
    assert kind_of(src.fetch_nasdaq_historical, "BF.B", date(2025, 10, 22), date(2026, 10, 22), http=http) == "empty"


def test_nasdaq_vendor_error_is_its_own_kind():
    """2026-10-23 審查 r7b F2：data: null 而且 rCode 是 200 以外的整數＝廠商錯誤（vendor），不再和「沒有資料」混在一起
    被吞掉；rCode 200（TSM 季報、BF.B 的空殼）照舊是 empty。"""
    for parse in (src.parse_nasdaq_historical, src.parse_nasdaq_financials, src.parse_nasdaq_splits):
        assert kind_of(parse, as_bytes(VENDOR_ERROR)) == "vendor"
        assert kind_of(parse, nasdaq(None)) == "empty"
    assert kind_of(src.parse_nasdaq_historical, as_bytes(VENDOR_ERROR), expect_symbol="COO") == "vendor"
    for code in (200, "400", True, None):  # 只認整數的非 200；200 帶了 bCodeMessage 也不算
        shell = dict(VENDOR_ERROR, status=dict(VENDOR_ERROR["status"], rCode=code))
        assert kind_of(src.parse_nasdaq_historical, as_bytes(shell)) == "empty"
    http = FakeHttp(as_bytes(VENDOR_ERROR))
    assert kind_of(src.fetch_nasdaq_historical, "COO", date(2025, 10, 22), date(2026, 10, 22), http=http) == "vendor"


def test_fetch_historical_url_window_and_endpoint():
    rows = [{"date": "10/19/2026", "close": "$1.00", "volume": "1", "open": "$1", "high": "$1", "low": "$1"}] + AAPL_ROWS
    http = FakeHttp(as_bytes(hist("AAPL", rows)))
    ledger = object()
    bars = src.fetch_nasdaq_historical("AAPL", date(2026, 10, 15), date(2026, 10, 16), http=http, ledger=ledger)
    assert [b.date for b in bars] == [date(2026, 10, 15), date(2026, 10, 16)]  # 晚於 end 與早於 start 的都丟
    call = http.calls[0]
    assert call["url"].startswith("https://api.nasdaq.com/api/quote/AAPL/historical?")
    assert "assetclass=stocks" in call["url"] and "fromdate=2026-10-15" in call["url"]
    assert "todate=2026-10-16" in call["url"] and "limit=9999" in call["url"]  # 小 limit 會觸發 Nasdaq 的 vendor 錯誤（10/18 實測）
    endpoint = src.ENDPOINTS["nasdaq_historical"]
    assert (call["max_bytes"], call["timeout"], call["accept"]) == (endpoint.max_bytes, endpoint.timeout, ("json",))
    assert call["ledger"] is ledger
    http_etf = FakeHttp(as_bytes(hist("UUP", QQQ_ROWS)))
    src.fetch_nasdaq_historical("UUP", date(2026, 10, 1), date(2026, 10, 16), etf=True, http=http_etf)
    assert "assetclass=etf" in http_etf.calls[0]["url"]


@pytest.mark.parametrize("symbol", ["BRK/B", "../x", "AAPL?x=1", "aapl", "", None])
def test_fetchers_validate_path_symbols_before_any_request(symbol):
    http = FakeHttp(b"{}")
    for fetch in (
        lambda: src.fetch_nasdaq_historical(symbol, date(2026, 10, 1), date(2026, 10, 2), http=http),
        lambda: src.fetch_nasdaq_13f(symbol, http=http),
        lambda: src.fetch_nasdaq_financials(symbol, http=http),
        lambda: src.fetch_nasdaq_earnings_date(symbol, http=http),
    ):
        assert kind_of(fetch) == "format"
    assert http.calls == []


def test_fetch_historical_start_after_end_is_a_programming_error():
    with pytest.raises(ValueError):
        src.fetch_nasdaq_historical("AAPL", date(2026, 10, 2), date(2026, 10, 1), http=FakeHttp(b"{}"))


# ── market-info ──────────────────────────────────────────────────────────
MARKET_INFO = {  # 裁自 nasdaq_market_info.json（美東 10/16 晚上抓的）
    "marketOpeningTime": "Oct 16, 2026 09:30 AM ET",
    "marketClosingTime": "Oct 16, 2026 04:00 PM ET",
    "previousTradeDate": "Oct 15, 2026",
    "nextTradeDate": "Oct 19, 2026",
    "isBusinessDay": True,
    "openRaw": "2026-10-16T09:30:00",
    "closeRaw": "2026-10-16T16:00:00",
}


def test_market_info_normal():
    info = src.parse_nasdaq_market_info(nasdaq(MARKET_INFO))
    assert info.closing_time == datetime(2026, 10, 16, 16, 0, tzinfo=ET)
    assert info.closing_time.utcoffset() == timedelta(hours=-4)
    assert info.opening_time == datetime(2026, 10, 16, 9, 30, tzinfo=ET)
    assert (info.previous_trade_date, info.next_trade_date, info.is_business_day) == (
        date(2026, 10, 15), date(2026, 10, 19), True)


def test_market_info_early_close_from_text_only():
    data = dict(MARKET_INFO, marketClosingTime="Nov 27, 2026 01:00 PM ET", closeRaw=None,
                previousTradeDate="Nov 25, 2026", nextTradeDate="Nov 30, 2026")
    assert src.parse_nasdaq_market_info(nasdaq(data)).closing_time == datetime(2026, 11, 27, 13, 0, tzinfo=ET)


@pytest.mark.parametrize(
    "change, kind",
    [
        ({"closeRaw": "2026-10-16T13:00:00"}, "conflict"),  # 兩個收盤時刻對不上
        ({"marketClosingTime": None, "closeRaw": None}, "missing_field"),
        ({"marketClosingTime": "16:00 Oct 16"}, "date"),
        ({"isBusinessDay": "true"}, "format"),
        ({"isBusinessDay": None}, "missing_field"),
        ({"previousTradeDate": "Oct 19, 2026"}, "conflict"),
        ({"nextTradeDate": "Foo 19, 2026"}, "date"),
        ({"previousTradeDate": ""}, "missing_field"),
    ],
)
def test_market_info_errors(change, kind):
    assert kind_of(src.parse_nasdaq_market_info, nasdaq(dict(MARKET_INFO, **change))) == kind


def test_market_info_bad_opening_time_does_not_block():
    info = src.parse_nasdaq_market_info(nasdaq(dict(MARKET_INFO, marketOpeningTime="later", openRaw=None)))
    assert info.opening_time is None and info.closing_time.hour == 16


# ── 分割日曆 ──────────────────────────────────────────────────────────────
def splits(rows):
    headers = {"symbol": "SYMBOL", "name": "COMPANY", "ratio": "RATIO", "executionDate": "EFFECTIVE DATE"}
    return nasdaq({"asOf": "Fri, Oct 16, 2026", "headers": headers, "rows": rows})


def split_row(symbol, ratio, when):
    return {"symbol": symbol, "name": "Example Co", "ratio": ratio, "executionDate": when}


def test_split_ratio_formats():
    rows = [
        split_row("ITA", "3 : 1", "11/05/2026"),
        split_row("KNDI", "1:10", "10/16/2026"),
        split_row("IBO", "1 : 12.62", "9/23/2026"),
        split_row("RUSHA", "1.5:1", "9/01/2026"),
        split_row("FBYDP", "2%", "9/16/2026"),
        split_row("ODD", "one for ten", "9/02/2026"),
    ]
    got = {s.symbol: s for s in src.parse_nasdaq_splits(splits(rows))}
    assert (got["ITA"].new, got["ITA"].old, got["ITA"].factor) == (3.0, 1.0, 3.0)
    assert (got["KNDI"].new, got["KNDI"].old) == (1.0, 10.0) and got["KNDI"].factor == pytest.approx(0.1)
    assert (got["IBO"].new, got["IBO"].old) == (1.0, 12.62)
    assert got["RUSHA"].factor == 1.5
    assert got["FBYDP"].factor == pytest.approx(1.02) and got["FBYDP"].ratio_text == "2%"
    assert got["ODD"].factor is None and got["ODD"].ratio_text == ""  # 看不懂的比例：事件還在、字不留
    assert got["ITA"].execution_date == date(2026, 11, 5) and got["IBO"].execution_date == date(2026, 9, 23)


def test_splits_symbols_dates_and_order():
    rows = [
        split_row("BRK/B", "2 : 1", "12/01/2026"),
        split_row("TOOLONG", "2 : 1", "12/01/2026"),  # 不是 canonical：不可能在成分裡
        split_row("AMTD", "1 : 60", "TBA"),  # 日期壞掉的那列略過
        split_row("GP", "1:5", "10/19/2026"),
        split_row("GP", "1:5", "10/19/2026"),  # 完全一樣的重複
        split_row("LU", "1 : 10", "10/23/2026"),
        split_row("LU", "1 : 20", "10/23/2026"),  # 同一天兩個比例 → 比例未知
    ]
    got = src.parse_nasdaq_splits(splits(rows))
    assert [(s.symbol, s.execution_date) for s in got] == [
        ("GP", date(2026, 10, 19)), ("LU", date(2026, 10, 23)), ("BRK.B", date(2026, 12, 1))]
    assert got[1].factor is None


def test_splits_empty_and_broken():
    assert src.parse_nasdaq_splits(splits(None)) == []
    assert src.parse_nasdaq_splits(splits([])) == []
    assert kind_of(src.parse_nasdaq_splits, splits([split_row("GP", "1:5", "19/10/2026")])) == "date"
    assert kind_of(src.parse_nasdaq_splits, splits("rows")) == "format"
    assert kind_of(src.parse_nasdaq_splits, nasdaq(None)) == "empty"


def test_fetch_splits_url():
    http = FakeHttp(as_bytes(splits([split_row("GP", "1:5", "10/19/2026")])))
    assert src.fetch_nasdaq_splits(date(2026, 10, 9), http=http)[0].symbol == "GP"
    assert http.calls[0]["url"] == "https://api.nasdaq.com/api/calendar/splits?date=2026-10-09"


# ── Nasdaq-100 名單 ──────────────────────────────────────────────────────
def ndx_rows(n):
    rows = [
        {"symbol": "AAPL", "companyName": "Apple Inc. Common Stock", "marketCap": "5,027,985,434,182", "lastSalePrice": "$344.55"},
        {"symbol": "GOOGL", "companyName": "Alphabet Inc. Class A Common Stock", "marketCap": "2,000,000,000,000"},
        {"symbol": "GOOG", "companyName": "Alphabet Inc. Class C Capital Stock", "marketCap": "1,900,000,000,000"},
    ]
    for i in range(n - len(rows)):
        rows.append({"symbol": f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}", "companyName": f"Example {i} Inc.", "marketCap": "1,000"})
    return rows


def ndx(rows, total=None):
    return nasdaq({"totalrecords": len(rows) if total is None else total, "limit": 101, "offset": 0,
                   "date": "Oct 16, 2026 12:34 PM", "data": {"asOf": None, "headers": {}, "rows": rows}})


def test_nasdaq100_list():
    result = src.parse_nasdaq100(ndx(ndx_rows(101)))
    assert result.as_of == date(2026, 10, 16) and len(result) == 101 and result.dropped_count == 0
    first = next(iter(result))
    assert first == src.Holding("AAPL", "Apple Inc. Common Stock", None, 5027985434182.0)
    symbols = [h.symbol for h in result]
    assert "GOOGL" in symbols and "GOOG" in symbols  # 股別分開列，13F 才在 market_calc 合併


def test_nasdaq100_counts_and_bad_rows():
    assert kind_of(src.parse_nasdaq100, ndx(ndx_rows(15), total=101)) == "too_few"  # 被截斷的名單
    assert kind_of(src.parse_nasdaq100, ndx(ndx_rows(60))) == "too_few"
    rows = ndx_rows(100) + [{"symbol": "BAD SYMBOL", "companyName": "x"}]
    rows[1]["marketCap"] = "lots"
    result = src.parse_nasdaq100(ndx(rows))
    assert result.dropped_count == 1 and len(result) == 100
    assert next(h for h in result if h.symbol == "GOOGL").market_cap is None
    assert kind_of(src.parse_nasdaq100, ndx([])) == "empty"


# ── SSGA SPY xlsx ────────────────────────────────────────────────────────
SSGA_HEADER = ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"]
SSGA_ROWS = [  # 裁自 ssga_spy_holdings.xlsx（真實的代號與 CUSIP）
    ["NVIDIA CORP", "NVDA", "67066G104", "2379504", 7.877639, "-", 296896008.0, "USD"],
    ["BERKSHIRE HATHAWAY INC CL B", "BRK.B", "084670702", "2073390", 1.467162, "-", 22849924.0, "USD"],
    ["BROWN FORMAN CORP CLASS B", "BF.B", "115637209", "2146838", 0.007581, "-", 2171571.0, "USD"],
    ["US DOLLAR", "-", "999USDZ92", "-", 0.286774, "-", 2347837191.76, "USD"],
    ["TPG INC", "2602335D", "436CVR021", "-", 3e-06, "-", 2578626.0, "USD"],
]


def build_xlsx(sheets: dict[str, list[list]]) -> bytes:
    workbook = openpyxl.Workbook()
    first = True
    for title, rows in sheets.items():
        sheet = workbook.active if first else workbook.create_sheet()
        sheet.title = title
        first = False
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def ssga_xlsx(rows, *, ticker="SPY", as_of="As of 15-Oct-2026", header=SSGA_HEADER) -> bytes:
    sheet = [["Fund Name:", "SPDR S&P 500 ETF Trust"], ["Ticker Symbol:", ticker], ["Holdings:", as_of], [], header]
    sheet += rows
    sheet += [[], [], ["Past performance is not a reliable indicator of future performance."]]
    return build_xlsx({"holdings": sheet})


def filler_rows(n):
    return [[f"EXAMPLE CO {i}", f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}", cusip(f"9{i:07d}"), "-", 0.1, "-", 1.0, "USD"]
            for i in range(n)]


def test_ssga_parses_and_drops_non_equity_rows():
    extra = [
        ["CASH MANAGEMENT FUND", "CMF", cusip("12345678"), "-", 0.01, "-", 1.0, "USD"],  # 名稱有 CASH
        ["BAD CUSIP CORP", "BADC", "123456780", "-", 0.02, "-", 1.0, "USD"],  # 檢查碼錯
        ["NEGATIVE CORP", "NEGC", cusip("22345678"), "-", -0.5, "-", 1.0, "USD"],
        ["NO WEIGHT CORP", "NOWC", cusip("32345678"), "-", None, "-", 1.0, "USD"],
    ]
    result = src.parse_ssga_spy_xlsx(ssga_xlsx(SSGA_ROWS + extra + filler_rows(450)))
    assert result.as_of == date(2026, 10, 15)
    assert result.dropped_count == 6  # US DOLLAR、CVR、CASH、CUSIP、負權重、沒權重
    assert len(result) == 453 and result.unmapped_weight == 0.0
    assert result.rows[0] == src.Holding("NVDA", "NVIDIA CORP", 7.877639)
    assert [h.symbol for h in result.rows[1:3]] == ["BRK.B", "BF.B"]


def test_ssga_disclaimer_rows_end_the_table():
    rows = SSGA_ROWS[:3] + [[], ["Bonds generally present less short-term risk than stocks."]]
    result = src.parse_ssga_spy_xlsx(ssga_xlsx(rows), min_rows=3)
    assert len(result) == 3 and result.dropped_count == 0


@pytest.mark.parametrize(
    "kwargs, kind",
    [
        ({"ticker": "QQQ"}, "format"),
        ({"as_of": "As of yesterday"}, "date"),
        ({"header": ["Name", "Ticker", "Identifier", "Sector"]}, "format"),  # 沒有 Weight
        ({"header": ["Security", "Symbol"]}, "format"),  # 找不到表頭
    ],
)
def test_ssga_layout_errors(kwargs, kind):
    assert kind_of(src.parse_ssga_spy_xlsx, ssga_xlsx(SSGA_ROWS, **kwargs), min_rows=1) == kind


def test_ssga_too_few_and_not_xlsx():
    assert kind_of(src.parse_ssga_spy_xlsx, ssga_xlsx(SSGA_ROWS)) == "too_few"
    assert kind_of(src.parse_ssga_spy_xlsx, b"<html>blocked</html>") == "format"
    assert kind_of(src.parse_ssga_spy_xlsx, b"") == "empty"
    assert kind_of(src.parse_ssga_spy_xlsx, "not bytes") == "format"


# ── xlsx 防護（zip 炸彈、DOCTYPE／ENTITY、UTF-16）──────────────────────────
def rewrite_member(data: bytes, name: str, transform) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            content = source.read(info)
            target.writestr(info.filename, transform(content) if info.filename == name else content)
    return out.getvalue()


def test_xlsx_guard_member_count(monkeypatch):
    valid = zipfile.ZipFile(io.BytesIO(finra(FINRA_ROWS)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for info in valid.infolist():
            archive.writestr(info.filename, valid.read(info))
        for i in range(src.XLSX_MAX_MEMBERS):  # openpyxl 會忽略這些多出來的成員
            archive.writestr(f"customXml/item{i}.xml", "<a/>")
    assert guard_reason(src.parse_finra_margin_xlsx, out.getvalue()) == ("format", "zip_members")
    monkeypatch.setattr(src, "XLSX_MAX_MEMBERS", 1000)
    assert len(src.parse_finra_margin_xlsx(out.getvalue())) == 12  # 拿掉上限就讀得了：是上限擋的


def test_xlsx_guard_uncompressed_size(monkeypatch):
    data = ssga_xlsx(SSGA_ROWS)
    big = rewrite_member(data, "xl/worksheets/sheet1.xml", lambda content: content + b" " * 200_000)
    assert len(src.parse_ssga_spy_xlsx(big, min_rows=1)) == 3  # 檔案本身合法
    monkeypatch.setattr(src, "XLSX_MAX_UNCOMPRESSED", 150_000)
    assert len(big) < 150_000  # 壓縮後很小、解壓後超過上限：看的是宣告的解壓大小
    assert guard_reason(src.parse_ssga_spy_xlsx, big, min_rows=1) == ("format", "zip_size")


@pytest.mark.parametrize(
    "member, inject",
    [
        ("xl/worksheets/sheet1.xml", b'<!DOCTYPE x [<!ENTITY boom "boom">]>'),
        ("xl/sharedStrings.xml", b'<!ENTITY lol "lol">'),
        ("[Content_Types].xml", b"<!doctype html>"),
    ],
)
def test_xlsx_rejects_doctype_and_entity(member, inject):
    data = ssga_xlsx(SSGA_ROWS)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    if member not in names:  # openpyxl 用 inline 字串時沒有 sharedStrings：改放進工作表
        member = "xl/worksheets/sheet1.xml"

    def transform(content: bytes) -> bytes:
        head, sep, tail = content.partition(b"?>")
        return head + sep + inject + tail if sep else inject + content

    bad = rewrite_member(data, member, transform)
    assert guard_reason(src.parse_ssga_spy_xlsx, bad, min_rows=1) == ("format", "xml_doctype")
    assert guard_reason(src.parse_finra_margin_xlsx, rewrite_member(finra(FINRA_ROWS), "xl/workbook.xml", transform)) == (
        "format", "xml_doctype")


def test_xlsx_rejects_utf16_member():
    data = ssga_xlsx(SSGA_ROWS)
    wide = rewrite_member(data, "xl/workbook.xml",
                          lambda content: content.decode("utf-8").replace("UTF-8", "UTF-16").encode("utf-16"))
    assert guard_reason(src.parse_ssga_spy_xlsx, wide, min_rows=1) == ("format", "xml_encoding")
    no_bom = rewrite_member(data, "xl/workbook.xml",
                            lambda content: content.decode("utf-8").replace("UTF-8", "UTF-16").encode("utf-16-le"))
    assert guard_reason(src.parse_ssga_spy_xlsx, no_bom, min_rows=1) == ("format", "xml_encoding")


def _with_member(data: bytes, drop: str, name: str, content: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename != drop:
                target.writestr(info.filename, source.read(info))
        target.writestr(name, content)
    return out.getvalue()


def test_xlsx_guard_checks_renamed_and_padded_xml_members():
    """2026-10-18 安全審查 I4：工作表改名成 .dat、或前面加 BOM／空白，DOCTYPE 照樣要被擋。"""
    data = ssga_xlsx(SSGA_ROWS)
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml")
    dtd = b'<!DOCTYPE x [<!ENTITY boom "boom">]>'
    doctype = sheet.replace(b"?>", b"?>" + dtd, 1) if b"?>" in sheet else dtd + sheet  # openpyxl 寫的工作表沒有 XML 宣告
    for name, content in (
        ("xl/worksheets/sheet1.dat", doctype),
        ("xl/worksheets/sheet1.dat", b"\xef\xbb\xbf" + doctype),
        ("xl/worksheets/sheet1.dat", b"  \n" + doctype),
    ):
        bad = _with_member(data, "xl/worksheets/sheet1.xml", name, content)
        assert guard_reason(src.parse_ssga_spy_xlsx, bad, min_rows=1) == ("format", "xml_doctype"), content[:8]
    wide = _with_member(data, "xl/worksheets/sheet1.xml", "xl/worksheets/sheet1.dat",
                        doctype.decode("utf-8").encode("utf-16-le"))
    assert guard_reason(src.parse_ssga_spy_xlsx, wide, min_rows=1) == ("format", "xml_encoding")


def test_xlsx_guard_rejects_utf16be_after_a_leading_newline():
    """2026-10-23 審查 r7a N1：沒有 BOM 的 UTF-16BE 前面加換行（\\x00\\n\\x00<）原本被當二進位略過，實體照樣被展開。"""
    data = ssga_xlsx(SSGA_ROWS)
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml")
    dtd = b'<!DOCTYPE x [<!ENTITY boom "boom">]>'
    doctype = sheet.replace(b"?>", b"?>" + dtd, 1) if b"?>" in sheet else dtd + sheet
    wide = ("\n" + doctype.decode("utf-8")).encode("utf-16-be")
    for name in ("xl/worksheets/sheet1.dat", "xl/worksheets/sheet1.bin"):  # 改成二進位副檔名也一樣擋
        bad = _with_member(data, "xl/worksheets/sheet1.xml", name, wide)
        assert guard_reason(src.parse_ssga_spy_xlsx, bad, min_rows=1) == ("format", "xml_encoding"), name


def test_xlsx_guard_reads_wide_members_with_binary_names_too():
    """2026-10-23 審查 r9 gp2：二進位副檔名放行只看名字的話，UTF-16 的 DOCTYPE 改名成 .bin 就躲得過。"""
    data = ssga_xlsx(SSGA_ROWS)
    dtd = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY boom "boom">]><x>&boom;</x>'
    for name in ("xl/styles.bin", "xl/media/image9.png"):
        bad = _with_member(data, "", name, dtd.encode("utf-16"))
        assert guard_reason(src.parse_ssga_spy_xlsx, bad, min_rows=1) == ("format", "xml_encoding"), name
    # 開頭剛好有 NUL 的真二進位（TrueType 字型）不是 XML：副檔名不在任何清單裡也照過
    font = _with_member(data, "", "xl/embeddings/font1.ttf", b"\x00\x01\x00\x00" + bytes(64))
    assert len(src.parse_ssga_spy_xlsx(font, min_rows=3)) == 3


def test_xlsx_guard_keeps_real_binary_members():
    """真的 SSGA 檔有 xl/printerSettings/printerSettings1.bin（內含 NUL）：二進位成員不能被當成 XML 擋掉。"""
    data = _with_member(ssga_xlsx(SSGA_ROWS), "", "xl/printerSettings/printerSettings1.bin",
                        b"M\x00i\x00c\x00r\x00o\x00s\x00o\x00f\x00t\x00" + bytes(64))
    assert len(src.parse_ssga_spy_xlsx(data, min_rows=3)) == 3


def test_xlsx_clean_file_still_parses_after_guard():
    assert len(src.parse_ssga_spy_xlsx(ssga_xlsx(SSGA_ROWS), min_rows=3)) == 3


# ── FINRA ────────────────────────────────────────────────────────────────
FINRA_HEADER = ["Year-Month", "Debit Balances in Customers' Securities Margin Accounts",
                "Free Credit Balances in Customers' Cash Accounts",
                "Free Credit Balances in Customers' Securities Margin Accounts"]
FINRA_ROWS = [  # 來源新的月份在上（裁自 finra_margin.xlsx）
    ["2026-09", 1480054, 215328, 218581],
    ["2026-08", 1453832, 207641, 217499],
    ["2026-07", 1417225, 205132, 217305],
    ["2026-06", 1502072, 217441, 223412],
    ["2026-05", 1415557, 206600, 217256],
    ["2026-04", 1304281, 217836, 215445],
    ["2026-03", 1220922, 221860, 205600],
    ["2026-02", 1253192, 205060, 200047],
    ["2026-01", 1279042, 203700, 196911],
    ["2025-12", 1225597, 211720, 199762],
    ["2025-11", 1214321, 202131, 194418],
    ["2025-10", 1199000, 200000, 190000],
]


def finra(rows, header=FINRA_HEADER):
    return build_xlsx({"Customer Margin Balances": [header] + rows})


def test_finra_newest_first_becomes_oldest_first():
    rows = src.parse_finra_margin_xlsx(finra(FINRA_ROWS + [["1997-01", 103337, 68856, None]]))
    assert rows[0] == src.MarginRow(date(1997, 1, 1), 103337)
    assert rows[-1] == src.MarginRow(date(2026, 9, 1), 1480054)
    assert [r.month for r in rows] == sorted(r.month for r in rows)


def test_finra_excel_dates_and_footnotes():
    rows = [[datetime(2026, 9, 1), 1480054, 1, 1]] + FINRA_ROWS[1:] + [[], ["Note: figures are in millions"]]
    parsed = src.parse_finra_margin_xlsx(finra(rows))
    assert parsed[-1] == src.MarginRow(date(2026, 9, 1), 1480054) and len(parsed) == 12


@pytest.mark.parametrize(
    "rows, header, kind",
    [
        (FINRA_ROWS, ["Month", "Debit"], "format"),
        (FINRA_ROWS, ["Year-Month", "Credit Balances"], "format"),
        (FINRA_ROWS + [["2026-09", 1480055, 0, 0]], FINRA_HEADER, "conflict"),
        ([["2026-09", None, 1, 1]] + FINRA_ROWS[1:], FINRA_HEADER, "missing_field"),
        ([["2026-13", 1, 1, 1]] + FINRA_ROWS[1:], FINRA_HEADER, "date"),
        (FINRA_ROWS[:5], FINRA_HEADER, "too_few"),
    ],
)
def test_finra_errors(rows, header, kind):
    assert kind_of(src.parse_finra_margin_xlsx, finra(rows, header)) == kind


# ── MoneyDJ 持股 ─────────────────────────────────────────────────────────
def mdj_link(code, name, back):
    return f"<a href='/etf/ea/ETZCW.djhtm?c=1&etfid={code}&back={back}'>{name}({code})</a>"


def moneydj(label, rows, *, as_of="2026/09/30", title="ETF"):
    body = "".join(
        f'<tr class="even"><td class="col05">{name}</td><td class="col06">{weight}</td><td class="col07">1,000</td></tr>'
        for name, weight in rows
    )
    return (
        '<html><head><meta charset="UTF-8"></head><body><table><tr><td>layout</td></tr></table>'
        f'<div class="eTitle">{title}({label})-全部持股'
        f'<div id="x_sdate3" align="right">資料日期：{as_of}</div></div>'
        '<table class="datalist" id="x_stable3"><thead><tr><th scope="col">個股名稱</th>'
        '<th scope="col">投資比例(%)</th><th scope="col">持有股數 / 口數</th></tr></thead>'
        f"<tbody>{body}</tbody><tfoot></tfoot></table></body></html>"
    )


SOXX_ROWS = [  # 照 moneydj_soxx.html：有連結的美股、沒連結的外國股與 ADR、現金列
    (mdj_link("INTC.US", "Intel", "SOXX"), "9.59"),
    (mdj_link("AMD.US", "AMD", "SOXX"), "9.28"),
    ("SK HYNIX AMERICAN DEPOSITARY SHARE", "3.06"),
    ("TAIWAN SEMICONDUCTOR MANUFACTURING", "3.00"),
    ("ASML HOLDING ADR REPRESENTING", "1.46"),
    ("TOWER SEMICONDUCTOR", "1.18"),
    ("ASE TECHNOLOGY HOLDING ADR REPRESE", "0.89"),
    ("UNITED MICRO ELECTRONICS ADR REP", "0.68"),
    ("ARM HOLDINGS AMERICAN DEPOSITARY S", "0.50"),
    ("STMICROELECTRONICS ADR", "0.42"),
    ("EXAMPLE FOREIGN FOUNDRY GDR", "0.30"),
    ("BLK CSH FND TREASURY SL AGENCY", "0.12"),
    ("USD CASH", "0.03"),
    ("CASH COLLATERAL USD WFFUT", "0.01"),
]


def test_moneydj_name_table_adrs_and_unmapped_weight():
    result = src.parse_moneydj_holdings(moneydj("SOXX", SOXX_ROWS, title="iShares半導體ETF"), "SOXX", min_rows=1)
    assert result.as_of == date(2026, 9, 30)
    assert [h.symbol for h in result] == ["INTC", "AMD", "TSM", "ASML", "TSEM", "ASX", "UMC", "ARM", "STM"]
    assert result.rows[0] == src.Holding("INTC", "Intel", 9.59)
    assert result.dropped_count == 3  # 現金基金、現金、擔保品
    assert result.unmapped_count == 2 and result.unmapped_weight == pytest.approx(3.36)  # SK Hynix＋表外外國股
    market_calc = pytest.importorskip("market_calc")  # 名稱先過共用清洗（≤24 字）才會被存
    assert result.unmapped_names == tuple(
        market_calc.clean_external(name, 24) for name in ("SK HYNIX AMERICAN DEPOSITARY SHARE", "EXAMPLE FOREIGN FOUNDRY GDR"))
    assert all(len(name) <= 24 for name in result.unmapped_names)
    assert result.covered_weight == pytest.approx(27.0)


def test_moneydj_unmapped_names_are_cleaned_before_storage():
    pytest.importorskip("market_calc")
    rows = [("FOREIGN CO see www.example.com", "1.00"), ("x @all y HOLDINGS", "2.00"),
            ("EVIL\u202eCO\u200b LTD", "0.50")]
    result = src.parse_moneydj_holdings(moneydj("SOXX", rows), "SOXX", min_rows=1)
    assert result.unmapped_count == 3 and result.unmapped_weight == pytest.approx(3.5)  # 權重照算
    assert "www.example.com" not in " ".join(result.unmapped_names)  # 含網址的整則不存
    assert all("@" not in name and "\u202e" not in name and "\u200b" not in name for name in result.unmapped_names)
    assert len(result.unmapped_names) == 2


def test_moneydj_cash_futures_and_share_classes():
    rows = [
        (mdj_link("NVDA.US", "NVIDIA", "QQQ"), "8.35"),
        (mdj_link("GOOGL.US", "Alphabet Class A", "QQQ"), "3.10"),
        (mdj_link("GOOG.US", "Alphabet Class C", "QQQ"), "2.90"),
        (mdj_link("DG.US", "Dollar General", "QQQ"), "0.04"),  # 「Dollar」不是現金字
        ("CASH &amp; EQUIVALENTS", "0.23"),
        ("CME E-Mini NASDAQ 100 Index Future 12/18/2026", "0.14"),
        ("CONTRA FUTURE FUTURE DEC 18 26 12/18/2026", "-0.14"),
    ]
    result = src.parse_moneydj_holdings(moneydj("QQQ", rows), "QQQ", min_rows=1)
    assert [h.symbol for h in result] == ["NVDA", "GOOGL", "GOOG", "DG"]
    assert result.dropped_count == 3 and result.unmapped_count == 0


def test_moneydj_voo_berkshire_and_brown_forman_without_links():
    rows = [
        (mdj_link("AAPL.US", "Apple", "VOO"), "7.03"),
        ("Berkshire Hathaway Inc", "1.40"),
        ("Brown-Forman Corp", "0.01"),
        (mdj_link("JNJ.US", "Johnson &amp; Johnson", "VOO"), "1.10"),
    ]
    result = src.parse_moneydj_holdings(moneydj("VOO", rows, as_of="2026/08/31"), "VOO", min_rows=1)
    assert [(h.symbol, h.name) for h in result] == [
        ("AAPL", "Apple"), ("BRK.B", "Berkshire Hathaway Inc"), ("BF.B", "Brown-Forman Corp"),
        ("JNJ", "Johnson & Johnson")]


def test_moneydj_0050_codes_and_futures_rows():
    rows = [
        (mdj_link("2330.TW", "台積電", "0050.TW"), "56.66"),
        (mdj_link("2454.TW", "聯發科", "0050.TW"), "5.99"),
        (mdj_link("FITXN*1.TF", "臺股期貨 202610", "0050.TW"), "0.21"),
        ("台灣50ETF股票期貨 202610", "0.03"),
    ]
    html = moneydj("0050.TW", rows, as_of="2026/10/16", title="元大台灣50")
    for etf in ("0050", "0050.TW"):
        result = src.parse_moneydj_holdings(html, etf, min_rows=1)
        assert [(h.symbol, h.name, h.weight) for h in result] == [("2330", "台積電", 56.66), ("2454", "聯發科", 5.99)]
        assert result.dropped_count == 2 and result.as_of == date(2026, 10, 16)


def test_moneydj_parenthesised_words_are_not_codes():
    rows = [("TAIWAN SEMICONDUCTOR MANUFACTURING (ADR)", "3.00")]
    result = src.parse_moneydj_holdings(moneydj("SOXX", rows), "SOXX", min_rows=1)
    assert [h.symbol for h in result] == ["TSM"] and result.rows[0].name == "TAIWAN SEMICONDUCTOR MANUFACTURING (ADR)"


def test_moneydj_errors():
    page = moneydj("SOXX", SOXX_ROWS)
    assert guard_reason(src.parse_moneydj_holdings, page, "QQQ", min_rows=1) == ("format", "etf")  # 別檔的頁
    assert kind_of(src.parse_moneydj_holdings, page.replace("資料日期：2026/09/30", ""), "SOXX", min_rows=1) == "date"
    assert kind_of(src.parse_moneydj_holdings, "<html><body>維護中</body></html>", "SOXX") == "format"
    assert kind_of(src.parse_moneydj_holdings, page, "SOXX") == "too_few"  # SOXX 預設至少 20 檔
    assert kind_of(src.parse_moneydj_holdings, page, "SO XX") == "format"
    assert kind_of(src.parse_moneydj_holdings, page.encode("utf-8")[:-3] + b"\xff", "SOXX") == "format"


def test_fetch_moneydj_urls():
    rows = [(mdj_link(f"Z{chr(65 + i)}.US", f"Example {i}", "SOXX"), "1.00") for i in range(20)]
    http = FakeHttp(moneydj("SOXX", rows).encode("utf-8"))
    assert len(src.fetch_moneydj_holdings("SOXX", http=http)) == 20
    assert http.calls[0]["url"] == "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid=SOXX"
    assert http.calls[0]["accept"] == ("html",)
    tw = [(mdj_link(f"{1100 + i}.TW", f"公司{i}", "0050.TW"), "2.00") for i in range(40)]
    http_tw = FakeHttp(moneydj("0050.TW", tw, as_of="2026/10/16").encode("utf-8"))
    assert len(src.fetch_moneydj_holdings("0050", http=http_tw)) == 40
    assert http_tw.calls[0]["url"].endswith("?etfid=0050.TW")


# ── CBOE 期權 ────────────────────────────────────────────────────────────
def option(code, oi=100.0, iv=0.25, gamma=0.01, delta=0.5, volume=10.0):
    return {"option": code, "open_interest": oi, "iv": iv, "gamma": gamma, "delta": delta, "volume": volume,
            "bid": 1.0, "ask": 1.1, "last_trade_time": "2026-10-16T15:59:00"}


def chain(options, *, symbol="QQQ", timestamp="2026-10-16 20:45:00", last_trade="2026-10-16T16:15:00", **data):
    body = {"symbol": symbol, "current_price": 709.8, "close": 709.8, "prev_day_close": 728.0,
            "volume": 66926660, "iv30": 20.799, "last_trade_time": last_trade, "options": options}
    body.update(data)
    return {"timestamp": timestamp, "symbol": symbol, "data": body}


def test_cboe_chain_times_spot_and_iv_zero():
    options = [
        option("QQQ261120P00700000", oi=5000.0, iv=0.21),
        option("QQQ261120C00690000", oi=88.0, iv=0.0, gamma=0.0013, delta=0.9949),  # 價內 IV=0：照樣收
        option("QQQ261120C00720000", oi=3198.0, iv=float("nan")),  # NaN（json 會寫成 NaN）
    ]
    parsed = src.parse_cboe_options(json.dumps(chain(options)).encode("utf-8"), expect_symbol="QQQ")
    assert parsed.timestamp_utc == datetime(2026, 10, 16, 20, 45, tzinfo=UTC)
    assert parsed.quote_last_trade_et == datetime(2026, 10, 16, 16, 15, tzinfo=ET)
    assert parsed.quote_last_trade_et.utcoffset() == timedelta(hours=-4)
    assert (parsed.spot, parsed.iv30, parsed.close, parsed.volume, parsed.symbol) == (709.8, 20.799, 709.8, 66926660, "QQQ")
    assert [c.strike for c in parsed.contracts] == [690.0, 700.0, 720.0]
    zero = parsed.contracts[0]
    assert (zero.kind, zero.iv, zero.oi, zero.gamma, zero.expiry) == ("C", 0.0, 88, 0.0013, date(2026, 11, 20))
    assert parsed.contracts[2].iv is None
    assert (parsed.raw_count, parsed.bad_count, parsed.conflict_count, parsed.conflict_oi_known) == (3, 0, 0, True)


def test_cboe_conflicting_duplicates_are_excluded_with_oi_knowledge():
    same_oi = option("QQQ261120C00700000", oi=300.0, gamma=0.010)
    same_oi_other_gamma = option("QQQ261120C00700000", oi=300.0, gamma=0.020)
    other_oi_a = option("QQQ261120P00690000", oi=100.0)
    other_oi_b = option("QQQ261120P00690000", oi=900.0)
    identical = option("QQQ261120C00710000", oi=50.0)
    options = [same_oi, same_oi_other_gamma, other_oi_a, other_oi_b, identical, dict(identical)]
    parsed = src.parse_cboe_options(chain(options))
    assert [c.symbol for c in parsed.contracts] == ["QQQ261120C00710000"]  # 完全一樣的只是重複
    assert parsed.conflict_count == 4 and parsed.conflict_oi_known is False
    by_symbol = {c.symbol: c for c in parsed.conflicts}
    assert by_symbol["QQQ261120C00700000"].oi == 300 and by_symbol["QQQ261120C00700000"].rows == 2
    assert by_symbol["QQQ261120P00690000"].oi is None
    only_known = src.parse_cboe_options(chain([same_oi, same_oi_other_gamma, identical]))
    assert only_known.conflict_oi_known is True and only_known.conflict_count == 2


def test_cboe_bad_rows_are_counted():
    good = [option(f"QQQ261120C00{700 + i}000") for i in range(7)]
    bad = [
        option("SPY261120C00700000"),  # 別的標的
        option("QQQ261120C00700000", oi=-1.0),
        option("QQQ261120C00701500", oi=1.5),
        option("QQQ261120C00000000"),  # 履約價 0
        option("QQQ261332C00700000"),  # 不存在的日期
        {"open_interest": 10.0},
        "row",
    ]
    parsed = src.parse_cboe_options(chain(good + bad))
    assert (parsed.raw_count, parsed.bad_count, len(parsed.contracts)) == (14, 7, 7)
    assert kind_of(src.parse_cboe_options, chain(good[:3] + bad)) == "format"  # 超過一半壞掉：格式換了


@pytest.mark.parametrize(
    "payload, kind",
    [
        ({"timestamp": "2026-10-16 20:45:00", "data": None}, "empty"),
        (chain([]), "empty"),
        (chain([option("QQQ261120C00700000")], symbol="TSLA"), "format"),
        (chain([option("QQQ261120C00700000")], timestamp=None), "missing_field"),
        (chain([option("QQQ261120C00700000")], timestamp="yesterday"), "date"),
        (chain([option("QQQ261120C00700000")], last_trade=""), "missing_field"),
        (chain([option("QQQ261120C00700000")], current_price=None, close=0), "missing_field"),
        (chain("options"), "format"),
        ("[]", "format"),
    ],
)
def test_cboe_chain_errors(payload, kind):
    assert kind_of(src.parse_cboe_options, payload) == kind


def test_cboe_chain_symbol_must_match_request():
    assert kind_of(src.parse_cboe_options, chain([option("QQQ261120C00700000")]), expect_symbol="SPY") == "format"


def test_fetch_cboe_options_url_and_whitelist():
    http = FakeHttp(as_bytes(chain([option("SOXX261120C00300000")], symbol="SOXX")))
    assert src.fetch_cboe_options("SOXX", http=http).symbol == "SOXX"
    assert http.calls[0]["url"] == "https://cdn-api.cboe.com/api/global/delayed_quotes/options/SOXX.json"
    assert http.calls[0]["timeout"] == 30.0 and http.calls[0]["max_bytes"] == 16 * 1024 * 1024
    assert kind_of(src.fetch_cboe_options, "TSLA", http=http) == "format"
    assert len(http.calls) == 1


# ── CBOE 指數報價 ────────────────────────────────────────────────────────
VIX = {  # 裁自 cboe_vix.json（收盤後：prev_day_close 等於 close）
    "timestamp": "2026-10-17 01:37:16",
    "symbol": "_VIX",
    "data": {"symbol": "^VIX", "current_price": 19.99, "price_change": 3.53, "price_change_percent": 17.6588,
             "close": 19.99, "prev_day_close": 19.99, "last_trade_time": "2026-10-16T16:15:01"},
}


def test_cboe_vix_quote():
    quote = src.parse_cboe_quote(VIX, expect_symbol="VIX")
    assert (quote.close, quote.price_change, quote.price_change_pct, quote.symbol) == (19.99, 3.53, 17.6588, "VIX")
    assert quote.last_trade_et == datetime(2026, 10, 16, 16, 15, 1, tzinfo=ET)
    assert quote.timestamp_utc == datetime(2026, 10, 17, 1, 37, 16, tzinfo=UTC)
    assert quote.last_trade_et.date() == date(2026, 10, 16)  # 資料日看美東日期，不看 UTC 的 10/17


def test_cboe_quote_errors():
    def with_data(**change):
        return {**VIX, "data": {**VIX["data"], **change}}

    assert kind_of(src.parse_cboe_quote, with_data(close=None)) == "missing_field"
    assert kind_of(src.parse_cboe_quote, with_data(close=0)) == "format"
    assert kind_of(src.parse_cboe_quote, with_data(last_trade_time="soon")) == "date"
    assert kind_of(src.parse_cboe_quote, VIX, expect_symbol="VIX3M") == "format"
    assert src.parse_cboe_quote(with_data(price_change="--")).price_change is None


def test_fetch_cboe_quote_url():
    http = FakeHttp(as_bytes({**VIX, "symbol": "_VIX3M", "data": {**VIX["data"], "symbol": "^VIX3M"}}))
    assert src.fetch_cboe_quote("VIX3M", http=http).symbol == "VIX3M"
    assert http.calls[0]["url"] == "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_VIX3M.json"
    assert kind_of(src.fetch_cboe_quote, "SPX", http=http) == "format"


# ETF 報價（investigations/freshness_20261023.md 附錄 A 的 QQQ 形狀；VOO 同一型，ETF 網址不帶底線）
VOO_QUOTE = {"timestamp": "2026-10-23 00:04:23", "symbol": "VOO",
             "data": {"symbol": "VOO", "current_price": 702.3, "price_change": -8.42, "close": 702.3,
                      "prev_day_close": 710.72, "volume": 6123456, "last_trade_time": "2026-10-22T15:59:59"}}


def test_fetch_cboe_etf_quote_for_the_voo_close():
    """2026-10-23：VOO 沒有期權鏈，Nasdaq 日線還沒有 D 時用這個約 1 KB 的報價當收盤備援。"""
    http = FakeHttp(as_bytes(VOO_QUOTE))
    ledger = object()
    quote = src.fetch_cboe_etf_quote("VOO", http=http, ledger=ledger)
    assert (quote.symbol, quote.close) == ("VOO", 702.3)
    assert quote.last_trade_et == datetime(2026, 10, 22, 15, 59, 59, tzinfo=ET)
    assert quote.timestamp_utc == datetime(2026, 10, 23, 0, 4, 23, tzinfo=UTC)
    call = http.calls[0]
    assert call["url"] == "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/VOO.json"
    endpoint = src.ENDPOINTS["cboe_quote"]
    assert (call["max_bytes"], call["ledger"]) == (endpoint.max_bytes, ledger)
    assert kind_of(src.fetch_cboe_etf_quote, "QQQ", http=http) == "format"  # 有期權鏈的不走這裡
    assert len(http.calls) == 1
    assert kind_of(src.parse_cboe_quote, VOO_QUOTE, expect_symbol="QQQ") == "format"


# ── 美債 ─────────────────────────────────────────────────────────────────
TREASURY = """Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"
10/16/2026,4.12,4.14,4.17,4.26,4.32,4.34,4.46,4.74,4.82,4.89,4.98,5.09,5.47,5.43
10/15/2026,4.17,4.15,4.19,4.28,4.35,4.35,4.49,4.77,4.85,4.91,5.00,5.10,5.48,5.43
10/09/2026,4.13,4.13,4.13,4.25,4.29,4.32,4.47,4.80,4.89,5.02,5.13,5.24,5.65,5.60
"""


def test_treasury_rows_oldest_first():
    rows = src.parse_treasury_csv(TREASURY.encode("utf-8"))
    assert [r.date for r in rows] == [date(2026, 10, 9), date(2026, 10, 15), date(2026, 10, 16)]
    assert rows[-1] == src.YieldRow(date(2026, 10, 16), 4.26, 4.74, 5.09, 5.43)


def test_treasury_missing_values_and_header_only():
    text = TREASURY.replace("4.12,4.14,4.17,4.26", "4.12,4.14,4.17,N/A").replace(",5.47,5.43\n", ",5.47,\n", 1)
    latest = src.parse_treasury_csv(text)[-1]
    assert latest.m3 is None and latest.y30 is None and latest.y10 == 5.09
    assert src.parse_treasury_csv(TREASURY.splitlines()[0] + "\n") == []  # 月初還沒有列
    assert src.parse_treasury_csv("\ufeff" + TREASURY)[0].date == date(2026, 10, 9)


@pytest.mark.parametrize(
    "text, kind",
    [
        ("", "empty"),
        ("\n\n", "empty"),
        (TREASURY.replace('"10 Yr",', ""), "format"),
        (TREASURY.replace("Date,", "Day,"), "format"),
        (TREASURY.replace("10/09/2026,", "2026-10-09,"), "date"),
        (TREASURY.replace(",5.60\n", ",5.60,9\n"), "format"),
        (TREASURY.replace("4.26,", "426,", 1), "format"),
        (TREASURY.replace("4.26,", "abc,", 1), "format"),
        (TREASURY + "10/16/2026,4.12,4.14,4.17,4.27,4.32,4.34,4.46,4.74,4.82,4.89,4.98,5.09,5.47,5.43\n", "conflict"),
    ],
)
def test_treasury_errors(text, kind):
    assert kind_of(src.parse_treasury_csv, text) == kind


def test_fetch_treasury_month_url_and_filter():
    text = TREASURY + "09/30/2026,4.1,4.1,4.1,4.2,4.3,4.3,4.4,4.7,4.8,4.9,5.0,5.1,5.5,5.4\n"
    http = FakeHttp(text.encode("utf-8"))
    rows = src.fetch_treasury(date(2026, 10, 1), http=http)
    assert [r.date.month for r in rows] == [10, 10, 10]  # 回應混到別的月份也只留要的月
    url = http.calls[0]["url"]
    assert url.startswith("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/")
    assert "/all/202610?" in url and "field_tdr_date_value_month=202610" in url
    assert http.calls[0]["accept"] == ("csv",)


# ── CFTC TFF ─────────────────────────────────────────────────────────────
CFTC_ES = '"E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",261013,2026-10-13,13874A,CME ,00,138 , 1931607,  200960,  803318,   49131, 1110384,  194549,   96043,  121404,  530130,   46956,   62418,   73497,     302, 1687598, 1793926,  244009,  137681,    -620,    -205,    1312,    7002,    3667,   -5553,   -3280,    8181,    1820,   -1480,  -10369,    -982,     -28,    3488,   -1189,   -4108,     569,  100.0,   10.4,   41.6,    2.5,   57.5,   10.1,    5.0,    6.3,   27.4,    2.4,    3.2,    3.8,    0.0,   87.4,   92.9,   12.6,    7.1,    444,     39,     31,     15,    164,     41,     79,     62,     79,     55,     17,     11,.,    339,    257,     9.7,    28.1,    16.9,    39.8,     9.3,    26.7,    15.5,    38.2,"($50 X S&P 500 INDEX)","13874A","CME ","138 ","F20","FutOnly"'
CFTC_NQ_CONS = '"NASDAQ-100 Consolidated - CHICAGO MERCANTILE EXCHANGE",261013,2026-10-13,20974+,CME ,00,209 ,  294776,   49082,  122889,    7515,  113679,   37277,    5269,   43778,   65669,   14043,   10909,    8044,       5,  244280,  260711,   50496,   34065,   -2879,    -328,    4698,    1057,     858,    -688,     671,   -4361,   -9209,    2311,   -4592,     124,       5,   -4379,   -1031,    1500,   -1848,  100.0,   16.7,   41.7,    2.5,   38.6,   12.6,    1.8,   14.9,   22.3,    4.8,    3.7,    2.7,    0.0,   82.9,   88.4,   17.1,   11.6,    326,     34,     26,     14,     90,     28,     34,     53,     59,     30,     21,     15,.,    240,    165,    20.8,    31.3,    30.1,    44.0,    20.5,    29.2,    29.7,    41.7,"(NASDAQ 100 INDEX X $20)","20974+","CME ","209 ","F20","FutOnly"'
CFTC_NQ_MINI = '"NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE",261013,2026-10-13,209742,CME ,00,209 ,  279515,   48575,  126507,    3012,  112604,   37281,    5238,   48106,   59699,    8647,   10809,    7911,       5,  236996,  248300,   42519,   31215,   -4071,    -256,    5114,     136,     877,    -648,     702,   -3243,  -10862,    1838,   -4618,     137,       5,   -4559,   -3578,     488,    -493,  100.0,   17.4,   45.3,    1.1,   40.3,   13.3,    1.9,   17.2,   21.4,    3.1,    3.9,    2.8,    0.0,   84.8,   88.8,   15.2,   11.2,    283,     31,     21,      9,     84,     27,     30,     45,     48,     22,     16,     11,.,    205,    140,    21.9,    31.4,    31.7,    44.7,    21.6,    30.8,    31.4,    43.4,"(NASDAQ 100 STOCK INDEX X $20)","209742","CME ","209 ","F20","FutOnly"'


def test_cftc_headerless_87_column_rows():
    rows = src.parse_cftc_tff("\n".join([CFTC_ES, CFTC_NQ_CONS, CFTC_NQ_MINI]) + "\n")
    es = next(r for r in rows if r.code == "13874A")
    assert (es.market, es.report_date, es.oi) == ("E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE", date(2026, 10, 13), 1931607)
    assert (es.dealer_l, es.dealer_s, es.am_l, es.am_s, es.lev_l, es.lev_s, es.other_l, es.other_s) == (
        200960, 803318, 1110384, 194549, 121404, 530130, 62418, 73497)
    assert (es.lev_net, es.am_net, es.lev_net_change, es.am_net_change, es.chg_oi) == (-408726, 915835, 6361, 9220, -620)
    assert (es.nonrept_l, es.nonrept_s) == (244009, 137681)
    assert {r.code for r in rows} == {"13874A", "20974+", "209742"}


def test_cftc_quoted_comma_stays_one_column():
    line = CFTC_ES.replace('"($50 X S&P 500 INDEX)"', '"(CONTRACTS OF $50 X 1,000 INDEX)"')
    assert line.count(",") == 87  # 逗號數多一個，但引號裡的不算欄
    assert src.parse_cftc_tff(line)[0].oi == 1931607


def test_cftc_bad_rows_are_skipped():
    cells = CFTC_ES.split(",")
    short = ",".join(cells[:-1])  # 86 欄
    wrong_date = CFTC_ES.replace(",261013,", ",261006,")  # 兩個日期對不上
    shifted = ",".join(cells[:9] + cells[10:] + ["X"])  # 欄位往前位移一格：合計對不上
    rows = src.parse_cftc_tff("\n".join([short, wrong_date, shifted, CFTC_NQ_MINI]))
    assert [r.code for r in rows] == ["209742"]
    assert kind_of(src.parse_cftc_tff, "\n".join([short, shifted])) == "format"
    assert kind_of(src.parse_cftc_tff, "\n \n") == "empty"
    broken_total = CFTC_ES.replace(" 1931607,", " 1931608,")  # OI 和合計對不上：當壞列略過
    assert [r.oi for r in src.parse_cftc_tff(CFTC_ES + "\n" + broken_total)] == [1931607]
    other_change = CFTC_ES.replace(",    -620,", ",    -621,")  # 同合約同一週、變化欄不同
    assert kind_of(src.parse_cftc_tff, CFTC_ES + "\n" + other_change) == "conflict"
    assert len(src.parse_cftc_tff(CFTC_ES + "\n" + CFTC_ES)) == 1  # 完全一樣的重複列合併


def test_cftc_pick_uses_one_basis_for_both():
    rows = src.parse_cftc_tff("\n".join([CFTC_ES, CFTC_NQ_CONS, CFTC_NQ_MINI]))
    pair = src.pick_cftc_contracts(rows)  # 只有 Nasdaq 有 Consolidated → 兩個都用 E-mini
    assert (pair.basis, pair.es.code, pair.nq.code) == ("e-mini", "13874A", "209742")
    consolidated_es = CFTC_ES.replace("E-MINI S&P 500 -", "S&P 500 Consolidated -").replace("13874A", "13874+")
    both = src.parse_cftc_tff("\n".join([consolidated_es, CFTC_ES, CFTC_NQ_CONS, CFTC_NQ_MINI]))
    pair = src.pick_cftc_contracts(both)
    assert (pair.basis, pair.es.code, pair.nq.code) == ("consolidated", "13874+", "20974+")
    assert kind_of(src.pick_cftc_contracts, [r for r in rows if r.code != "209742"]) == "missing_field"
    other_week = src.parse_cftc_tff(CFTC_NQ_MINI.replace("261013,2026-10-13", "261006,2026-10-06"))
    assert kind_of(src.pick_cftc_contracts, [rows[0]] + other_week) == "conflict"


# ── Fed FOMC ─────────────────────────────────────────────────────────────
def fomc_panel(year, meetings):
    items = "".join(
        f'<div class="row fomc-meeting" "><div class="fomc-meeting__month col-xs-5"><strong>{month}</strong></div>'
        f'<div class="fomc-meeting__date col-xs-4">{days}</div><div class="col-xs-12"><a href="/x">PDF</a></div></div>'
        for month, days in meetings
    )
    return (f'<div class="panel panel-default"><div class="panel-heading"><h4><a id="1">{year} FOMC Meetings</a>'
            f"</h4></div>{items}</div>")


FOMC_HTML = (
    "\ufeff<html><body><h5>FOMC Search</h5>"
    + fomc_panel(2026, [("January", "27-28"), ("March", "17-18*"), ("October", "27-28"), ("December", "8-9*")])
    + fomc_panel(2025, [("August", "22 (notation vote)"), ("October", "28-29")])
    + fomc_panel(2024, [("Apr/May", "30-1")])
    + fomc_panel(2027, [("January", "26-27"), ("March", "16-17*")])  # 未來年份排在最後
    + "<p>Note: A two-day meeting is scheduled for January 25-26, 2028.</p></body></html>"
)


def test_fed_fomc_meetings():
    meetings = src.parse_fed_fomc(FOMC_HTML.encode("utf-8"))
    assert [(m.start, m.end) for m in meetings] == [
        (date(2024, 4, 30), date(2024, 5, 1)),
        (date(2025, 10, 28), date(2025, 10, 29)),
        (date(2026, 1, 27), date(2026, 1, 28)),
        (date(2026, 3, 17), date(2026, 3, 18)),
        (date(2026, 10, 27), date(2026, 10, 28)),
        (date(2026, 12, 8), date(2026, 12, 9)),
        (date(2027, 1, 26), date(2027, 1, 27)),
        (date(2027, 3, 16), date(2027, 3, 17)),
    ]
    by_end = {m.end: m for m in meetings}
    assert by_end[date(2026, 3, 18)].sep is True and by_end[date(2026, 1, 28)].sep is False
    assert by_end[date(2026, 10, 28)].decision_utc == datetime(2026, 10, 28, 18, 0, tzinfo=UTC)  # 夏令 14:00
    assert by_end[date(2026, 12, 9)].decision_utc == datetime(2026, 12, 9, 19, 0, tzinfo=UTC)  # 冬令 14:00


def test_fed_fomc_errors():
    assert kind_of(src.parse_fed_fomc, "<html><body><h4>Calendar</h4></body></html>") == "format"
    assert kind_of(src.parse_fed_fomc, fomc_panel(2026, [("January", "27-28")])) == "too_few"
    garbage = fomc_panel(2026, [("Smarch", "27-28"), ("January", "TBD"), ("June", "31-32")])
    assert kind_of(src.parse_fed_fomc, garbage) == "format"


# ── 經濟日曆、財報日 ─────────────────────────────────────────────────────
def econ(rows):
    return nasdaq({"asOf": "Fri, Oct 16, 2026", "headers": {"gmt": "Time"}, "rows": rows})


def event(name, clock="08:30", country="United States"):
    return {"gmt": clock, "country": country, "eventName": name, "actual": "&nbsp;", "consensus": " ",
            "previous": "&nbsp;", "description": ""}


def test_econ_calendar_gmt_column_is_eastern_time():
    rows = [
        event("Core CPI"), event("Core CPI"),  # 年增、月增各一列，同名同時刻 → 一則
        event("CPI"), event("Cleveland CPI", "11:00"), event("CPI Index, n.s.a."),
        event("Core CPI", "03:00", "Spain"), event("MBA Mortgage Applications", "07:00"),
        event("Fed Chair Powell Speaks", "13:30"), event("Retail Sales (MoM)"), event("GDP", "All Day"),
    ]
    events = src.parse_nasdaq_econ_calendar(econ(rows), date(2026, 10, 14))
    assert [(e.time_utc, e.name) for e in events] == [
        (datetime(2026, 10, 14, 12, 30, tzinfo=UTC), "CPI"),  # 08:30 EDT
        (datetime(2026, 10, 14, 12, 30, tzinfo=UTC), "Core CPI"),
        (datetime(2026, 10, 14, 12, 30, tzinfo=UTC), "Retail Sales"),
        (datetime(2026, 10, 14, 17, 30, tzinfo=UTC), "Fed Chair Speaks"),
    ]
    assert all(e.country == "US" for e in events)
    winter = src.parse_nasdaq_econ_calendar(econ([event("Nonfarm Payrolls")]), date(2026, 12, 4))
    assert winter[0].time_utc == datetime(2026, 12, 4, 13, 30, tzinfo=UTC)  # 08:30 EST


def test_econ_calendar_wednesday_sentinel_catches_a_shifted_date():
    """Nasdaq 的 date=X 回 X 前一天（2026-10-18 實測）；MBA 房貸申請每週三公布，對到別天就整天不收。"""
    rows = [event("MBA Mortgage Applications", "07:00"), event("CPI")]
    assert [e.name for e in src.parse_nasdaq_econ_calendar(econ(rows), date(2026, 10, 14))] == ["CPI"]  # 週三
    assert kind_of(src.parse_nasdaq_econ_calendar, econ(rows), date(2026, 10, 15)) == "conflict"  # 週四
    claims = src.parse_nasdaq_econ_calendar(econ([event("Initial Jobless Claims")]), date(2026, 10, 22))  # 週四
    assert [(e.time_utc, e.name) for e in claims] == [(datetime(2026, 10, 22, 12, 30, tzinfo=UTC), "Initial Jobless Claims")]


def test_econ_calendar_require_sentinel():
    rows = [event("MBA Mortgage Applications", "07:00"), event("CPI")]
    assert [e.name for e in src.parse_nasdaq_econ_calendar(econ(rows), date(2026, 10, 14), require_sentinel=True)] == ["CPI"]
    assert kind_of(src.parse_nasdaq_econ_calendar, econ([event("CPI")]), date(2026, 10, 14), require_sentinel=True) == "conflict"
    # 2026-10-23 審查 r7 #4：rows 是 null 也不能繞過（原本直接回 []，整批照寫、CPI 早一天）
    assert kind_of(src.parse_nasdaq_econ_calendar, econ(None), date(2026, 10, 14), require_sentinel=True) == "conflict"
    assert src.parse_nasdaq_econ_calendar(econ(None), date(2026, 10, 14)) == []  # 一般請求：空日曆照收


def test_econ_calendar_empty_and_errors():
    assert src.parse_nasdaq_econ_calendar(econ(None), date(2026, 10, 17)) == []
    assert src.parse_nasdaq_econ_calendar(econ([]), date(2026, 10, 17)) == []
    assert kind_of(src.parse_nasdaq_econ_calendar, econ("x"), date(2026, 10, 17)) == "format"
    assert kind_of(src.parse_nasdaq_econ_calendar, nasdaq(None), date(2026, 10, 17)) == "empty"
    assert src.parse_nasdaq_econ_calendar(econ([event("CPI", "25:00")]), date(2026, 10, 14)) == []


def test_event_labels_cover_the_whitelist_and_fit_a_row():
    names = set(src.US_EVENT_NAMES.values()) | {"Fed Chair Speaks"}
    assert names == set(src.US_EVENT_LABELS)
    assert all(len(label) <= 24 and "（" in label for label in src.US_EVENT_LABELS.values())


EARNINGS = {  # 裁自 nasdaq_earnings_nvda.json
    "reportText": "NVIDIA Corporation Common Stock is estimated to report earnings on  11/18/2026. The upcoming "
                  "earnings date is derived from an algorithm based on a company's historical reporting dates.",
    "heading": "NVDA Earnings Date",
    "announcement": "Earnings announcement* for NVDA: Nov 18, 2026",
}


def test_earnings_date_estimated_and_confirmed():
    assert src.parse_nasdaq_earnings_date(nasdaq(EARNINGS), expect_symbol="NVDA") == src.EarningsDate(date(2026, 11, 18), True)
    confirmed = {"reportText": "NVIDIA Corporation is expected to report earnings on 11/18/2026 after market close.",
                 "announcement": "Earnings announcement for NVDA: Nov 18, 2026"}
    assert src.parse_nasdaq_earnings_date(nasdaq(confirmed)).estimated is False


def test_earnings_date_errors():
    conflict = dict(EARNINGS, announcement="Earnings announcement* for NVDA: Nov 19, 2026")
    assert kind_of(src.parse_nasdaq_earnings_date, nasdaq(conflict)) == "conflict"
    assert kind_of(src.parse_nasdaq_earnings_date, nasdaq({"reportText": "No upcoming date.", "announcement": ""})) == "empty"
    assert kind_of(src.parse_nasdaq_earnings_date, nasdaq(EARNINGS), expect_symbol="AAPL") == "format"
    assert kind_of(src.parse_nasdaq_earnings_date, nasdaq(dict(EARNINGS, reportText="on 13/45/2026"))) == "date"


# ── 13F、基本面 ──────────────────────────────────────────────────────────
INST = {  # 裁自 nasdaq_13f_nvda.json（前十大只留三家）
    "ownershipSummary": {"SharesOutstandingPCT": {"label": "Institutional Ownership", "value": "79.53%"}},
    "activePositions": {"rows": [
        {"positions": "Increased Positions", "holders": "3,369", "shares": "653,445,644"},
        {"positions": "Decreased Positions", "holders": "2,574", "shares": "419,330,425"},
        {"positions": "Held Positions", "holders": "348", "shares": "18,093,073,185"},
        {"positions": "Total Institutional Shares", "holders": "6,291", "shares": "19,165,849,254"},
    ]},
    "newSoldOutPositions": {"rows": [
        {"positions": "New Positions", "holders": "287", "shares": "175,666,187"},
        {"positions": "Sold Out Positions", "holders": "116", "shares": "31,776,088"},
    ]},
    "holdingsTransactions": {"table": {"rows": [
        {"ownerName": "Vanguard Group Inc", "date": "12/31/2025", "marketValue": "$492,391,608"},
        {"ownerName": "Blackrock, Inc.", "date": "6/30/2026", "marketValue": "$421,842,931"},
        {"ownerName": "Fmr Llc", "date": "6/30/2026", "marketValue": "$222,889,178"},
    ]}},
}


def test_13f_summary():
    result = src.parse_nasdaq_13f(nasdaq(INST))
    assert result == src.Inst13F(79.53, 3369, 2574, 348, 287, 116,
                                 [date(2025, 12, 31), date(2026, 6, 30), date(2026, 6, 30)], 6291)


def test_13f_missing_pieces():
    no_pct = dict(INST, ownershipSummary={})
    assert src.parse_nasdaq_13f(nasdaq(no_pct)).own_pct is None  # 暫缺，不是 0
    no_holders = dict(INST, holdingsTransactions=None)
    assert src.parse_nasdaq_13f(nasdaq(no_holders)).holder_periods == []
    assert kind_of(src.parse_nasdaq_13f, nasdaq(dict(INST, newSoldOutPositions={"rows": []}))) == "missing_field"
    assert kind_of(src.parse_nasdaq_13f, nasdaq({"ownershipSummary": {}})) == "empty"


def financials(rows, headers=None):
    headers = headers or {"value1": "Quarter:", "value2": "2nd", "value3": "1st", "value4": "4th", "value5": "3rd"}
    return nasdaq({"symbol": "NVDA", "incomeStatementTable": {"asOf": None, "headers": headers, "rows": rows}})


QUARTER_ENDING = {"value1": "Quarter Ending:", "value2": "7/26/2026", "value3": "4/26/2026", "value4": "1/25/2026",
                  "value5": "10/26/2025"}
NET_INCOME = {"value1": "Net Income Applicable to Common Shareholders", "value2": "$26,422,000",
              "value3": "$18,775,000", "value4": "($1,234)", "value5": "--"}


def test_financials_quarter_ending_row():
    quarters = src.parse_nasdaq_financials(financials([QUARTER_ENDING, {"value1": "Total Revenue", "value2": "$1"},
                                                       NET_INCOME]))
    assert quarters == [
        src.Quarter(date(2025, 10, 26), None),
        src.Quarter(date(2026, 1, 25), -1234.0),
        src.Quarter(date(2026, 4, 26), 18775000.0),
        src.Quarter(date(2026, 7, 26), 26422000.0),
    ]


def test_financials_dates_in_headers():
    headers = {"value1": "Period Ending:", "value2": "7/26/2026", "value3": "4/26/2026"}
    quarters = src.parse_nasdaq_financials(financials([NET_INCOME], headers))
    assert [q.end_date for q in quarters] == [date(2026, 4, 26), date(2026, 7, 26)]


def test_financials_errors():
    assert kind_of(src.parse_nasdaq_financials, nasdaq({"symbol": "NVDA", "incomeStatementTable": None})) == "empty"
    assert kind_of(src.parse_nasdaq_financials, financials([NET_INCOME])) == "format"  # 找不到期末日
    assert kind_of(src.parse_nasdaq_financials, financials([QUARTER_ENDING])) == "missing_field"
    twice = dict(QUARTER_ENDING, value3="7/26/2026")
    assert kind_of(src.parse_nasdaq_financials, financials([twice, NET_INCOME])) == "conflict"
    garbage = dict(NET_INCOME, value2="lots")
    assert kind_of(src.parse_nasdaq_financials, financials([QUARTER_ENDING, garbage])) == "format"


# ── 抓取共用：端點表、每個 fetch 用對的設定 ───────────────────────────────
MEASURED_CONTENT_TYPES = {  # 主 Claude 10/17 實測
    "api.nasdaq.com": "application/json; charset=utf-8",
    "cdn-api.cboe.com": "application/json",
    "www.ssga.com": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "www.finra.org": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "home.treasury.gov": "text/csv; charset=utf-8",
    "www.cftc.gov": "text/plain",
    "www.federalreserve.gov": "text/html; charset=utf-8",
    "www.moneydj.com": "text/html; charset=utf-8",
}


def test_endpoint_table_matches_transport_and_measured_types():
    market_http = pytest.importorskip("market_http")
    for key, endpoint in src.ENDPOINTS.items():
        assert endpoint.host in market_http.ALLOWED_HOSTS, key
        measured = MEASURED_CONTENT_TYPES[endpoint.host]
        assert any(token in measured for token in endpoint.accept), key
        assert not any(token in "text/html" for token in endpoint.accept) or endpoint.host.endswith(
            ("moneydj.com", "federalreserve.gov")), key  # 被擋時的 HTML 錯誤頁不能當成資料
        assert endpoint.timeout == (30.0 if key in ("cboe_options", "ssga_spy") else 15.0), key
        assert 0 < endpoint.max_bytes <= 16 * 1024 * 1024 and endpoint.per_day >= 1, key
    for url in (src.SSGA_SPY_URL, src.MONEYDJ_URL, src.CFTC_TFF_URL, src.FINRA_MARGIN_URL, src.FED_FOMC_URL,
                src.CBOE_OPTIONS_URL.format(symbol="QQQ"), src.CBOE_QUOTE_URL.format(symbol="VIX"),
                src.TREASURY_URL.format(ym="202610"), src.NASDAQ_API + "/market-info"):
        assert market_http.host_of(url) is not None, url


def test_ssga_url_is_the_final_one():
    assert src.SSGA_SPY_URL == "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
    assert "intermediary" not in src.SSGA_SPY_URL  # 那條路徑會 301，market_http 不轉址


@pytest.fixture(scope="module")
def bodies() -> dict[str, bytes]:
    return {
        "market_info": as_bytes(nasdaq(MARKET_INFO)),
        "ndx": as_bytes(ndx(ndx_rows(100))),
        "13f": as_bytes(nasdaq(INST)),
        "financials": as_bytes(financials([QUARTER_ENDING, NET_INCOME])),
        "econ": as_bytes(econ([event("CPI")])),
        "earnings": as_bytes(nasdaq(EARNINGS)),
        "ssga": ssga_xlsx(SSGA_ROWS + filler_rows(450)),
        "vix": as_bytes(VIX),
        "cftc": "\n".join([CFTC_ES, CFTC_NQ_MINI]).encode("utf-8"),
        "finra": finra(FINRA_ROWS),
        "fed": FOMC_HTML.encode("utf-8"),
    }


@pytest.mark.parametrize(
    "key, body, call, url",
    [
        ("nasdaq_market_info", "market_info", lambda h, l: src.fetch_nasdaq_market_info(http=h, ledger=l),
         "https://api.nasdaq.com/api/market-info"),
        ("nasdaq_nasdaq100", "ndx", lambda h, l: src.fetch_nasdaq100(http=h, ledger=l),
         "https://api.nasdaq.com/api/quote/list-type/nasdaq100"),
        ("nasdaq_13f", "13f", lambda h, l: src.fetch_nasdaq_13f("BRK.B", http=h, ledger=l),
         "https://api.nasdaq.com/api/company/BRK.B/institutional-holdings?limit=10&type=TOTAL&sortColumn=marketValue"),
        ("nasdaq_financials", "financials", lambda h, l: src.fetch_nasdaq_financials("NVDA", http=h, ledger=l),
         "https://api.nasdaq.com/api/company/NVDA/financials?frequency=2"),
        ("nasdaq_econ_calendar", "econ",
         lambda h, l: src.fetch_nasdaq_econ_calendar(date(2026, 10, 14), http=h, ledger=l),
         "https://api.nasdaq.com/api/calendar/economicevents?date=2026-10-15"),  # date=X 回 X 前一天的事件
        ("nasdaq_earnings", "earnings", lambda h, l: src.fetch_nasdaq_earnings_date("NVDA", http=h, ledger=l),
         "https://api.nasdaq.com/api/analyst/NVDA/earnings-date"),
        ("ssga_spy", "ssga", lambda h, l: src.fetch_ssga_spy(http=h, ledger=l), src.SSGA_SPY_URL),
        ("cboe_quote", "vix", lambda h, l: src.fetch_cboe_quote("VIX", http=h, ledger=l),
         "https://cdn-api.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"),
        ("cftc_tff", "cftc", lambda h, l: src.fetch_cftc_tff(http=h, ledger=l), src.CFTC_TFF_URL),
        ("finra_margin", "finra", lambda h, l: src.fetch_finra_margin(http=h, ledger=l), src.FINRA_MARGIN_URL),
        ("fed_fomc", "fed", lambda h, l: src.fetch_fed_fomc(http=h, ledger=l), src.FED_FOMC_URL),
    ],
)
def test_each_fetcher_uses_its_endpoint(bodies, key, body, call, url):
    http, ledger = FakeHttp(bodies[body]), object()
    assert call(http, ledger)
    endpoint = src.ENDPOINTS[key]
    assert http.calls == [{"url": url, "max_bytes": endpoint.max_bytes, "timeout": endpoint.timeout,
                           "accept": endpoint.accept, "headers": None, "ledger": ledger}]


def test_fetch_refuses_bodies_over_the_endpoint_limit(monkeypatch):
    monkeypatch.setitem(src.ENDPOINTS, "nasdaq_market_info",
                        src.Endpoint("api.nasdaq.com", ("json",), 64, 15.0, 1, 1, 1))
    assert kind_of(src.fetch_nasdaq_market_info, http=FakeHttp(as_bytes(nasdaq(MARKET_INFO)))) == "format"
    no_body = SimpleNamespace(fetch=lambda url, **kwargs: SimpleNamespace(status=200, body=None, headers={}, url=url))
    assert kind_of(src.fetch_cftc_tff, http=no_body) == "format"


def test_parse_error_is_shared_and_kinds_are_the_six():
    import market_calc

    assert ParseError is market_calc.ParseError  # job 只要 except 一種
    assert issubclass(ParseError, ValueError)
    for kind in ("format", "missing_field", "empty", "too_few", "date", "conflict"):
        assert src._err(kind).kind == kind


def test_import_does_not_load_transport_or_openpyxl():
    code = ("import sys; import market_sources_us; "
            "print(sorted(m for m in ('market_http', 'openpyxl', 'requests', 'main', 'stock_picks', 'card_pages') "
            "if m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "[]"
