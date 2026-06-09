"""Tests for stock_quote real-time fallback chain.

Covers:
  1. _parse_yahoo_tw_html parses sample TW HTML correctly
  2. _parse_yahoo_us_html parses sample US HTML correctly
  3. get_quote falls through real-time → fast_info → history
  4. get_quotes_text header shows source + timestamp from real-time
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import stock_quote


# ── 1. Yahoo TW parser ──────────────────────────────────────────────────────


_TW_SAMPLE_HTML = """
<html><body>
  <span>2330</span>
  <span class="Fz(32px)">2,325</span>
  <span>75.00</span>
  <span>(3.33%)</span>
  <span>資料時間：2026/05/07 11:03</span>
  <ul>
    <li><span>成交</span><span>2,325</span></li>
    <li><span>開盤</span><span>2,335</span></li>
    <li><span>最高</span><span>2,345</span></li>
    <li><span>最低</span><span>2,310</span></li>
    <li><span>昨收</span><span>2,250</span></li>
    <li><span>漲跌幅</span><span>3.33%</span></li>
    <li><span>漲跌</span><span>75.00</span></li>
  </ul>
</body></html>
"""


def test_parse_yahoo_tw_html_extracts_all_fields():
    p = stock_quote._parse_yahoo_tw_html(_TW_SAMPLE_HTML)
    assert p is not None, "parser returned None on valid TW sample"
    assert p["last_price"] == 2325.0
    assert p["open"] == 2335.0
    assert p["high"] == 2345.0
    assert p["low"] == 2310.0
    assert p["prev_close"] == 2250.0
    assert p["change"] == 75.0
    assert abs(p["change_pct"] - 3.33) < 0.01
    assert p["timestamp"] == "2026/05/07 11:03"


def test_parse_yahoo_tw_html_falling_stock_negates_change():
    """頁面上「漲跌」是絕對值，下跌時 parser 要把它變負。"""
    html = _TW_SAMPLE_HTML.replace(
        "<span>成交</span><span>2,325</span>",
        "<span>成交</span><span>2,200</span>",
    )
    p = stock_quote._parse_yahoo_tw_html(html)
    assert p is not None
    assert p["last_price"] == 2200.0
    # last < prev_close (2250) → change/pct should be negative
    assert p["change"] is not None and p["change"] < 0
    assert p["change_pct"] is not None and p["change_pct"] < 0


def test_parse_yahoo_tw_html_returns_none_on_garbage():
    assert stock_quote._parse_yahoo_tw_html("<html><body>nothing</body></html>") is None
    assert stock_quote._parse_yahoo_tw_html("") is None


# ── 2. Yahoo US parser ──────────────────────────────────────────────────────


_US_SAMPLE_HTML = """
<html><body>
  <span data-testid="qsp-price">287.51 </span>
  <span data-testid="qsp-price-change">+3.28 </span>
  <span data-testid="qsp-price-change-percent">(+1.16%)</span>
  <ul>
    <li><span>Previous Close</span>
      <span><fin-streamer data-field="regularMarketPreviousClose" data-value="284.23">284.23</fin-streamer></span>
    </li>
    <li><span>Open </span>
      <span><fin-streamer data-field="regularMarketOpen" data-value="281.92">281.92</fin-streamer></span>
    </li>
    <li><span>Day's Range</span>
      <span><fin-streamer data-field="regularMarketDayRange" data-value="281.08 - 288.03">281.08 - 288.03</fin-streamer></span>
    </li>
  </ul>
</body></html>
"""


def test_parse_yahoo_us_html_extracts_all_fields():
    p = stock_quote._parse_yahoo_us_html(_US_SAMPLE_HTML, "AAPL")
    assert p is not None, "parser returned None on valid US sample"
    assert p["last_price"] == 287.51
    assert p["change"] == 3.28
    assert abs(p["change_pct"] - 1.16) < 0.01
    assert p["prev_close"] == 284.23
    assert p["open"] == 281.92
    assert p["high"] == 288.03
    assert p["low"] == 281.08


def test_parse_yahoo_us_html_returns_none_on_garbage():
    assert stock_quote._parse_yahoo_us_html("<html></html>", "AAPL") is None
    assert stock_quote._parse_yahoo_us_html("", "AAPL") is None


# ── 2.5. Yahoo chart parser ─────────────────────────────────────────────────


_SOX_CHART_PAYLOAD = {
    "chart": {
        "result": [
            {
                "meta": {
                    "symbol": "^SOX",
                    "exchangeTimezoneName": "America/New_York",
                    "timezone": "EDT",
                    "regularMarketPrice": 12220.76,
                    "previousClose": 13617.495,
                    "regularMarketTime": 1780694159,
                    "regularMarketDayHigh": 13111.438,
                    "regularMarketDayLow": 12217.316,
                },
                "timestamp": [1780666200, 1780689540, 1780689600],
                "indicators": {
                    "quote": [
                        {
                            "open": [13062.5498, 12247.7597, 12220.7597],
                            "high": [13111.4404, 12290.8037, 12220.7597],
                            "low": [13062.5498, 12222.3251, 12220.7597],
                            "close": [13062.5498, 12222.3251, 12220.7597],
                            "volume": [0, 0, 0],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}


def test_parse_yahoo_chart_json_extracts_market_time_and_prev_close():
    p = stock_quote._parse_yahoo_chart_json(_SOX_CHART_PAYLOAD, "^SOX")

    assert p is not None
    assert p["symbol"] == "^SOX"
    assert p["source"] == "yahoo_chart"
    assert p["last_price"] == 12220.76
    assert p["prev_close"] == 13617.495
    assert abs(p["change"] - (-1396.735)) < 0.01
    assert abs(p["change_pct"] - (-10.2569)) < 0.01
    assert p["open"] == 13062.5498
    assert p["high"] == 13111.438
    assert p["low"] == 12217.316
    assert p["timestamp"] == "2026-06-05 17:15 EDT"
    assert p["market_date"] == "2026-06-05"


# ── 3. get_realtime_quote uses correct URL per market ───────────────────────


def test_get_realtime_quote_tw_uses_tw_yahoo():
    captured_urls = []

    def fake_fetch(url):
        captured_urls.append(url)
        return _TW_SAMPLE_HTML

    with patch.object(stock_quote, "_fetch_yahoo_html", side_effect=fake_fetch):
        q = stock_quote.get_realtime_quote("2330.TW")

    assert q is not None
    assert q["symbol"] == "2330.TW"
    assert q["source"] == "yahoo_realtime"
    assert q["last_price"] == 2325.0
    assert captured_urls == ["https://tw.stock.yahoo.com/quote/2330.TW"]


def test_get_realtime_quote_us_uses_us_yahoo():
    captured_urls = []

    def fake_fetch(url):
        captured_urls.append(url)
        return _US_SAMPLE_HTML

    with (
        patch.object(stock_quote, "_fetch_yahoo_chart_json", return_value=None),
        patch.object(stock_quote, "_fetch_yahoo_html", side_effect=fake_fetch),
    ):
        q = stock_quote.get_realtime_quote("AAPL")

    assert q is not None
    assert q["symbol"] == "AAPL"
    assert q["source"] == "yahoo_realtime"
    assert q["last_price"] == 287.51
    assert captured_urls == ["https://finance.yahoo.com/quote/AAPL"]


def test_get_realtime_quote_us_prefers_chart_json_over_html():
    with (
        patch.object(stock_quote, "_fetch_yahoo_chart_json", return_value=_SOX_CHART_PAYLOAD),
        patch.object(stock_quote, "_fetch_yahoo_html") as html_fetch,
    ):
        q = stock_quote.get_realtime_quote("^SOX")

    assert q is not None
    assert q["last_price"] == 12220.76
    assert q["source"] == "yahoo_chart"
    html_fetch.assert_not_called()


def test_get_realtime_quote_tw_future_uses_tw_yahoo():
    captured_urls = []

    def fake_fetch(url):
        captured_urls.append(url)
        return _TW_SAMPLE_HTML

    with patch.object(stock_quote, "_fetch_yahoo_html", side_effect=fake_fetch):
        q = stock_quote.get_realtime_quote("WCDFM6")

    assert q is not None
    assert q["symbol"] == "WCDFM6"
    assert q["source"] == "yahoo_realtime"
    assert captured_urls == ["https://tw.stock.yahoo.com/quote/WCDFM6"]


def test_get_realtime_quote_returns_none_when_fetch_fails():
    with patch.object(stock_quote, "_fetch_yahoo_html", return_value=None):
        assert stock_quote.get_realtime_quote("2330.TW") is None


# ── 4. Fallback chain ───────────────────────────────────────────────────────


def test_get_quote_uses_realtime_first():
    """real-time succeeds → fast_info / history not called."""
    rt_quote = {
        "symbol": "2330.TW", "last_price": 2325.0, "source": "yahoo_realtime",
        "timestamp": "2026-05-07 11:03", "change": 75.0, "change_pct": 3.33,
    }
    with patch.object(stock_quote, "get_realtime_quote", return_value=rt_quote) as rt, \
         patch.object(stock_quote, "get_fast_info_quote") as fi, \
         patch.object(stock_quote, "get_history_quote") as hi:
        out = stock_quote.get_quote("2330.TW")

    assert out is rt_quote
    assert rt.called
    assert not fi.called
    assert not hi.called


def test_get_quote_falls_back_to_fast_info_when_realtime_fails():
    fi_quote = {
        "symbol": "2330.TW", "last_price": 2320.0, "source": "fast_info",
        "timestamp": "2026-05-07 11:00", "change": 70.0, "change_pct": 3.11,
    }
    with patch.object(stock_quote, "get_realtime_quote", return_value=None), \
         patch.object(stock_quote, "get_fast_info_quote", return_value=fi_quote) as fi, \
         patch.object(stock_quote, "get_history_quote") as hi:
        out = stock_quote.get_quote("2330.TW")

    assert out is fi_quote
    assert fi.called
    assert not hi.called


def test_get_quote_falls_back_to_history_when_both_fail():
    hi_quote = {
        "symbol": "2330.TW", "last_price": 2250.0, "source": "history",
        "last_date": "2026-05-06", "timestamp": "2026-05-06",
        "change": 30.0, "change_pct": 1.35,
    }
    with patch.object(stock_quote, "get_realtime_quote", return_value=None), \
         patch.object(stock_quote, "get_fast_info_quote", return_value=None), \
         patch.object(stock_quote, "get_history_quote", return_value=hi_quote) as hi:
        out = stock_quote.get_quote("2330.TW")

    assert out is hi_quote
    assert hi.called


def test_get_quote_returns_none_when_all_fail():
    with patch.object(stock_quote, "get_realtime_quote", return_value=None), \
         patch.object(stock_quote, "get_fast_info_quote", return_value=None), \
         patch.object(stock_quote, "get_history_quote", return_value=None):
        assert stock_quote.get_quote("BOGUS.XX") is None


# ── 5. get_quotes_text header shows source + timestamp ──────────────────────


def test_get_quotes_text_header_shows_realtime_source():
    rt_quote = {
        "symbol": "2330.TW",
        "last_price": 2325.0,
        "prev_close": 2250.0,
        "change": 75.0,
        "change_pct": 3.33,
        "open": 2335.0,
        "high": 2345.0,
        "low": 2310.0,
        "timestamp": "2026-05-07 11:03",
        "last_date": "2026-05-07",
        "source": "yahoo_realtime",
    }
    with patch.object(stock_quote, "get_quote", return_value=rt_quote):
        out = stock_quote.get_quotes_text("台積電現在多少？")

    assert out is not None
    # Header must include the real-time timestamp + source label
    assert "2026-05-07 11:03" in out
    assert "Yahoo 即時" in out
    # Quote line must include H/L
    assert "2,325" in out
    assert "2,345" in out  # high
    assert "2,310" in out  # low
    assert "+75.00" in out
    assert "+3.33%" in out


def test_get_quotes_text_header_shows_history_source_when_only_history():
    hi_quote = {
        "symbol": "2330.TW",
        "last_price": 2250.0,
        "prev_close": 2220.0,
        "change": 30.0,
        "change_pct": 1.35,
        "open": None,
        "high": None,
        "low": None,
        "timestamp": "2026-05-06",
        "last_date": "2026-05-06",
        "source": "history",
    }
    with patch.object(stock_quote, "get_quote", return_value=hi_quote):
        out = stock_quote.get_quotes_text("台積電現在多少？")

    assert out is not None
    assert "2026-05-06" in out
    assert "日線收盤" in out


def test_get_quotes_text_returns_none_when_no_symbols():
    assert stock_quote.get_quotes_text("今天天氣很好") is None


def test_get_quotes_text_returns_none_when_all_quotes_fail():
    with patch.object(stock_quote, "get_quote", return_value=None):
        assert stock_quote.get_quotes_text("台積電現在多少？") is None


# ── 6. Context-aware quote selection ─────────────────────────────────────────


def _quote(symbol: str, price: float = 100.0):
    return {
        "symbol": symbol,
        "last_price": price,
        "prev_close": price - 1,
        "change": 1.0,
        "change_pct": 1.0,
        "high": price + 2,
        "low": price - 2,
        "timestamp": "2026-06-05 10:00",
        "last_date": "2026-06-05",
        "source": "yahoo_realtime",
    }


def _taiex_quote(last: float, low: float, high: float = 44507.49):
    return {
        "symbol": "^TWII",
        "last_price": last,
        "prev_close": 45070.94,
        "change": last - 45070.94,
        "change_pct": (last - 45070.94) / 45070.94 * 100,
        "high": high,
        "low": low,
        "timestamp": "2026-06-08 13:33 CST",
        "last_date": "2026-06-08",
        "market_date": "2026-06-08",
        "source": "yahoo_chart",
    }


def _ma_closes(value: float = 43100.0):
    return [(f"2026-05-{i:02d}", value) for i in range(1, 20)]


def test_to_float_rejects_nan():
    assert stock_quote._to_float(float("nan")) is None
    assert stock_quote._to_float("nan") is None


def test_detects_taiex_month_line_question():
    assert stock_quote.is_taiex_month_line_query("咪寶今天台股大盤有跌破月線嗎")
    assert stock_quote.is_taiex_month_line_query("加權指數有守住20日線？")
    assert not stock_quote.is_taiex_month_line_query("台股現在多少")


def test_taiex_month_line_reports_intraday_break_but_close_hold(monkeypatch):
    monkeypatch.setattr(stock_quote, "get_quote", lambda symbol: _taiex_quote(43502.78, 42376.86))
    monkeypatch.setattr(stock_quote, "_get_recent_daily_closes", lambda symbol: _ma_closes(43100.0))

    out = stock_quote.get_taiex_month_line_text("咪寶今天台股大盤有跌破月線嗎")

    assert out is not None
    assert out.startswith("有，台股大盤盤中跌破月線，但收盤守住月線。")
    assert "43,502.78" in out
    assert "20日線約 43,120.14" in out
    assert "42,376.86" in out


def test_taiex_month_line_reports_close_break(monkeypatch):
    monkeypatch.setattr(stock_quote, "get_quote", lambda symbol: _taiex_quote(42000.0, 41900.0))
    monkeypatch.setattr(stock_quote, "_get_recent_daily_closes", lambda symbol: _ma_closes(43100.0))

    out = stock_quote.get_taiex_month_line_text("加權指數跌破月線嗎")

    assert out is not None
    assert out.startswith("有，台股大盤盤中跌破月線，收盤也沒有收回。")
    assert "42,000.00" in out


def test_candidate_future_symbols_current_month_first():
    now = datetime(2026, 6, 5, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert stock_quote._candidate_future_symbols("CDF", now, months_ahead=4) == [
        "WCDFM6",
        "WCDFN6",
        "WCDFQ6",
        "WCDFU6",
    ]


def test_detect_symbols_tsmc_english_alias_maps_to_tw_stock():
    assert stock_quote.detect_symbols("TSMC 現在多少？") == ["2330.TW"]


def test_detect_symbols_gold_queries_map_to_gold_futures():
    assert stock_quote.detect_symbols("黃金現在多少？") == ["GC=F"]
    assert stock_quote.detect_symbols("XAU/USD 現在價格") == ["GC=F"]
    assert stock_quote.detect_symbols("Gold price now") == ["GC=F"]


def test_detect_symbols_gold_price_number_not_treated_as_tw_stock():
    assert stock_quote.detect_symbols("金價跌到4313嗎？") == ["GC=F"]


def test_detect_symbols_gold_without_market_context_ignored():
    assert stock_quote.detect_symbols("When it rains gold, put out the bucket") == []


def test_contextual_quotes_daytime_uses_stock_only():
    now = datetime(2026, 6, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    calls = []

    def fake_quote(symbol):
        calls.append(symbol)
        return _quote(symbol, 2325.0)

    with patch.object(stock_quote, "get_realtime_quote", side_effect=fake_quote):
        out = stock_quote.get_contextual_quotes_text("台積電今天怎麼看", now=now)

    assert out is not None
    assert calls == ["2330.TW"]
    assert "2330.TW" in out
    assert "TSM" not in out
    assert "近月期貨" not in out


def test_contextual_quotes_night_uses_adr_and_near_month_future():
    now = datetime(2026, 6, 5, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    calls = []

    def fake_quote(symbol):
        calls.append(symbol)
        if symbol == "TSM":
            return _quote(symbol, 250.0)
        if symbol == "WCDFM6":
            return _quote(symbol, 2330.0)
        return None

    with (
        patch.object(stock_quote, "get_realtime_quote", side_effect=fake_quote),
        patch.object(stock_quote, "get_fast_info_quote") as fast_info,
        patch.object(stock_quote, "get_history_quote") as history,
    ):
        out = stock_quote.get_contextual_quotes_text("台積電今天怎麼看", now=now)

    assert out is not None
    assert set(calls) == {"TSM", "WCDFM6", "WCDFN6", "WCDFQ6", "WCDFU6"}
    fast_info.assert_not_called()
    history.assert_not_called()
    assert "TSM" in out
    assert "ADR" in out
    assert "WCDFM6" in out
    assert "近月期貨" in out


def test_contextual_quotes_tsmc_alias_night_uses_adr_and_future():
    now = datetime(2026, 6, 5, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    def fake_quote(symbol):
        if symbol == "TSM":
            return _quote(symbol, 250.0)
        if symbol == "WCDFM6":
            return _quote(symbol, 2330.0)
        return None

    with patch.object(stock_quote, "get_realtime_quote", side_effect=fake_quote):
        out = stock_quote.get_contextual_quotes_text("TSMC 現在多少？", now=now)

    assert out is not None
    assert "TSM" in out
    assert "WCDFM6" in out


def test_contextual_quotes_price_query_infers_recent_context_symbol():
    now = datetime(2026, 6, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    context = [("user", "我覺得 NVDA 會再突破"), ("assistant", "要看財報")]
    calls = []

    def fake_quote(symbol):
        calls.append(symbol)
        return _quote(symbol, 180.0)

    with patch.object(stock_quote, "get_realtime_quote", side_effect=fake_quote):
        out = stock_quote.get_contextual_quotes_text("現在多少？", context=context, now=now)

    assert out is not None
    assert calls == ["NVDA"]
    assert "NVDA" in out


def test_contextual_quotes_gold_uses_commodity_label():
    now = datetime(2026, 6, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    calls = []

    def fake_quote(symbol):
        calls.append(symbol)
        return _quote(symbol, 4313.0)

    with patch.object(stock_quote, "get_realtime_quote", side_effect=fake_quote):
        out = stock_quote.get_contextual_quotes_text("黃金現在多少？", now=now)

    assert out is not None
    assert calls == ["GC=F"]
    assert "GC=F" in out
    assert "COMEX 黃金近月期貨/USD" in out
    assert "現股" not in out


def test_contextual_quote_request_ignores_how_many_days_question():
    context = [("user", "我覺得 NVDA 會再突破")]

    assert not stock_quote.should_try_contextual_quote(
        "距離 6/15 還有多少天？",
        context=context,
    )
    assert not stock_quote.should_try_contextual_quote(
        "NVDA 距離 6/15 還有多少天？",
        context=context,
    )


def test_contextual_quotes_no_price_trigger_ignores_old_context_symbol():
    now = datetime(2026, 6, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    context = [("user", "我覺得 NVDA 會再突破")]

    with patch.object(stock_quote, "get_realtime_quote") as quote:
        out = stock_quote.get_contextual_quotes_text("晚餐吃什麼？", context=context, now=now)

    assert out is None
    quote.assert_not_called()


def test_contextual_quotes_zero_deadline_skips_fetch():
    now = datetime(2026, 6, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    with patch.object(stock_quote, "get_realtime_quote") as quote:
        out = stock_quote.get_contextual_quotes_text(
            "台積電現在多少？",
            now=now,
            total_timeout_s=0,
        )

    assert out is None
    quote.assert_not_called()


def test_contextual_quotes_non_future_does_not_use_unbounded_yfinance_fallback():
    now = datetime(2026, 6, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    with (
        patch.object(stock_quote, "get_realtime_quote", return_value=None),
        patch.object(stock_quote, "get_fast_info_quote") as fast_info,
        patch.object(stock_quote, "get_history_quote") as history,
    ):
        out = stock_quote.get_contextual_quotes_text("NVDA 現在多少？", now=now)

    assert out is None
    fast_info.assert_not_called()
    history.assert_not_called()


def test_contextual_quotes_night_unmapped_tw_does_not_mislabel_adr_future():
    now = datetime(2026, 6, 5, 21, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    with patch.object(stock_quote, "get_realtime_quote", return_value=_quote("2317.TW", 210.0)):
        out = stock_quote.get_contextual_quotes_text("鴻海今天怎麼看", now=now)

    assert out is not None
    assert "2317.TW" in out
    assert "【市場報價｜夜間報價參考" in out
    assert "ADR/期貨" not in out


def test_contextual_quotes_index_daytime_not_labeled_stock():
    now = datetime(2026, 6, 5, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    with patch.object(stock_quote, "get_realtime_quote", return_value=_quote("^SOX", 12220.76)):
        out = stock_quote.get_contextual_quotes_text("費半現在多少？", now=now)

    assert out is not None
    assert "【市場報價｜日間報價" in out
    assert "日間現股" not in out


def test_contextual_quotes_prior_market_date_labeled_close_quote():
    now = datetime(2026, 6, 6, 14, 37, tzinfo=ZoneInfo("Asia/Taipei"))
    sox_quote = {
        "symbol": "^SOX",
        "last_price": 12220.76,
        "prev_close": 13617.495,
        "change": -1396.735,
        "change_pct": -10.2569,
        "high": 13111.438,
        "low": 12217.316,
        "timestamp": "2026-06-05 17:15 EDT",
        "last_date": "2026-06-05",
        "market_date": "2026-06-05",
        "source": "yahoo_chart",
    }

    with patch.object(stock_quote, "get_realtime_quote", return_value=sox_quote):
        out = stock_quote.get_contextual_quotes_text("費半現在多少？", now=now)

    assert out is not None
    assert "【市場報價｜收盤報價｜2026-06-05 17:15 EDT】" in out
    assert "12,220.76" in out
    assert "13,617.50" not in out
    assert "[Yahoo Chart]" in out
