from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import main
import stock_quote


@pytest.mark.parametrize(
    ("text", "symbol"),
    [
        ("005930.KS 股價多少？", "005930.KS"),
        ("^KS11 指數多少？", "^KS11"),
        ("ES=F 期貨多少？", "ES=F"),
        ("ASML.AS 股價多少？", "ASML.AS"),
        ("7203.T 股價多少？", "7203.T"),
        ("0700.HK 股價多少？", "0700.HK"),
    ],
)
def test_detect_symbols_accepts_explicit_global_yahoo_symbols(text, symbol):
    assert stock_quote.detect_symbols(text) == [symbol]
    assert stock_quote.should_try_contextual_quote(text)


@pytest.mark.parametrize(
    ("text", "symbol"),
    [
        ("韓股 005930 股價多少？", "005930.KS"),
        ("韓國 KOSDAQ 247540 股價", "247540.KQ"),
        ("日股 7203 股價多少？", "7203.T"),
        ("港股 0700 股價多少？", "0700.HK"),
        ("台股 2330 股價多少？", "2330.TW"),
    ],
)
def test_detect_symbols_uses_explicit_market_context_for_numeric_codes(text, symbol):
    assert stock_quote.detect_symbols(text) == [symbol]


def test_name_lookup_resolves_global_equity_with_bounded_yahoo_search(monkeypatch):
    calls = []

    class FakeSearch:
        def __init__(self, query, **kwargs):
            calls.append((query, kwargs))
            self.quotes = [
                {
                    "symbol": "005930.KS",
                    "quoteType": "EQUITY",
                    "shortname": "Samsung Electronics Co., Ltd.",
                }
            ]

    monkeypatch.setattr(stock_quote.yf, "Search", FakeSearch)

    assert stock_quote.should_try_contextual_quote("MercadoLibre股價多少？")
    assert stock_quote._resolve_quote_symbols(
        "MercadoLibre股價多少？",
        timeout_s=1.5,
    ) == ["005930.KS"]
    assert calls == [
        (
            "MercadoLibre",
            {
                "max_results": 8,
                "news_count": 0,
                "lists_count": 0,
                "include_research": False,
                "timeout": 1.5,
                "raise_errors": False,
            },
        )
    ]


def test_current_turn_company_name_beats_stale_context_symbol():
    assert stock_quote._resolve_quote_symbols(
        "三星電子股價多少？",
        context=[("user", "NVDA 最近很強")],
    ) == ["005930.KS"]


def test_market_scoped_name_search_prefers_primary_listing(monkeypatch):
    class FakeSearch:
        def __init__(self, _query, **_kwargs):
            self.quotes = [
                {"symbol": "TM", "quoteType": "EQUITY"},
                {"symbol": "7203.T", "quoteType": "EQUITY"},
            ]

    monkeypatch.setattr(stock_quote.yf, "Search", FakeSearch)

    assert stock_quote._resolve_quote_symbols("日股 Toyota 股價多少？") == ["7203.T"]


def test_yahoo_chart_parser_retains_market_session_metadata():
    start = int(datetime(2026, 8, 28, 9, 30, tzinfo=ZoneInfo("America/New_York")).timestamp())
    end = int(datetime(2026, 8, 28, 16, 0, tzinfo=ZoneInfo("America/New_York")).timestamp())
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": "^GSPC",
                        "instrumentType": "INDEX",
                        "exchangeName": "SNP",
                        "exchangeTimezoneName": "America/New_York",
                        "regularMarketPrice": 6500.0,
                        "previousClose": 6480.0,
                        "regularMarketTime": start + 3600,
                        "currentTradingPeriod": {
                            "regular": {"start": start, "end": end},
                        },
                    },
                    "indicators": {"quote": [{"close": [6500.0]}]},
                }
            ],
            "error": None,
        }
    }

    quote = stock_quote._parse_yahoo_chart_json(payload, "^GSPC")

    assert quote is not None
    assert quote["exchange_timezone"] == "America/New_York"
    assert quote["regular_session_start"] == start
    assert quote["regular_session_end"] == end
    assert quote["instrument_type"] == "INDEX"


def _quote(
    symbol: str,
    price: float,
    *,
    start: datetime,
    end: datetime,
    timestamp: datetime | None = None,
    source: str = "yahoo_chart",
):
    quoted_at = timestamp or start
    return {
        "symbol": symbol,
        "last_price": price,
        "prev_close": price - 10,
        "change": 10.0,
        "change_pct": 0.2,
        "high": price + 20,
        "low": price - 20,
        "timestamp": quoted_at.strftime("%Y-%m-%d %H:%M %Z"),
        "last_date": quoted_at.strftime("%Y-%m-%d"),
        "market_date": quoted_at.strftime("%Y-%m-%d"),
        "quote_epoch": int(quoted_at.timestamp()),
        "exchange_timezone": str(start.tzinfo.key),
        "regular_session_start": int(start.timestamp()),
        "regular_session_end": int(end.timestamp()),
        "source": source,
    }


def test_us_index_during_regular_session_uses_intraday_index_quote():
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 8, 28, 9, 30, tzinfo=ny)
    end = datetime(2026, 8, 28, 16, 0, tzinfo=ny)
    now = datetime(2026, 8, 28, 10, 15, tzinfo=ny)
    base = _quote("^GSPC", 6500.0, start=start, end=end, timestamp=now)
    batches = []

    def fake_fetch(symbols, _timeout):
        batches.append(list(symbols))
        return {"^GSPC": base} if "^GSPC" in symbols else {}

    with patch.object(stock_quote, "_fetch_contextual_quotes_with_deadline", side_effect=fake_fetch):
        out = stock_quote.get_contextual_quotes_text("^GSPC 指數多少？", now=now)

    assert out is not None
    assert batches == [["^GSPC"]]
    assert "^GSPC" in out
    assert "ES=F" not in out
    assert "盤中" in out


def test_us_index_after_close_switches_to_verified_index_future():
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 8, 28, 9, 30, tzinfo=ny)
    end = datetime(2026, 8, 28, 16, 0, tzinfo=ny)
    now = datetime(2026, 8, 28, 18, 0, tzinfo=ny)
    base = _quote("^GSPC", 6500.0, start=start, end=end, timestamp=end)
    future = _quote(
        "ES=F",
        6525.0,
        start=datetime(2026, 8, 28, 18, 0, tzinfo=ny),
        end=datetime(2026, 8, 29, 17, 0, tzinfo=ny),
        timestamp=now,
    )
    batches = []

    def fake_fetch(symbols, _timeout):
        batches.append(list(symbols))
        return {symbol: {"^GSPC": base, "ES=F": future}[symbol] for symbol in symbols}

    with patch.object(stock_quote, "_fetch_contextual_quotes_with_deadline", side_effect=fake_fetch):
        out = stock_quote.get_contextual_quotes_text("^GSPC 指數多少？", now=now)

    assert out is not None
    assert batches == [["^GSPC"], ["ES=F"]]
    assert "ES=F" in out
    assert "^GSPC (" not in out
    assert "現貨市場已收盤" in out


def test_closed_unmapped_equity_never_invents_a_future_counterpart():
    seoul = ZoneInfo("Asia/Seoul")
    start = datetime(2026, 8, 28, 9, 0, tzinfo=seoul)
    end = datetime(2026, 8, 28, 15, 30, tzinfo=seoul)
    now = datetime(2026, 8, 28, 18, 0, tzinfo=seoul)
    base = _quote("005930.KS", 122000.0, start=start, end=end, timestamp=end)

    with patch.object(
        stock_quote,
        "_fetch_contextual_quotes_with_deadline",
        return_value={"005930.KS": base},
    ):
        out = stock_quote.get_contextual_quotes_text("005930.KS 股價多少？", now=now)

    assert out is not None
    assert "005930.KS" in out
    assert "已收盤" in out
    assert "沒有可驗證的對應期貨／ADR" in out


def test_public_yahoo_quote_is_not_mislabeled_as_exchange_realtime():
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 8, 28, 9, 30, tzinfo=ny)
    end = datetime(2026, 8, 28, 16, 0, tzinfo=ny)
    now = datetime(2026, 8, 28, 10, 15, tzinfo=ny)
    quote = _quote("AAPL", 230.0, start=start, end=end, timestamp=now)

    line = stock_quote._format_contextual_quote_line(
        quote,
        stock_quote._base_quote_spec("AAPL"),
    )

    assert "Yahoo 最新公開報價（可能延遲）" in line
    assert quote["timestamp"] in line
    assert "Yahoo 即時" not in line


def test_named_future_request_does_not_duplicate_underlying_index():
    assert stock_quote.detect_symbols("標普期貨多少？") == ["ES=F"]
    assert stock_quote.detect_symbols("納指期貨多少？") == ["NQ=F"]


def test_nasdaq_composite_does_not_invent_nasdaq_100_future_mapping():
    now = datetime(2026, 8, 28, 18, 0, tzinfo=ZoneInfo("America/New_York"))
    assert stock_quote._after_close_specs_for_symbol("^IXIC", now=now) == []


def test_exchange_suffix_is_not_misrouted_as_futu_us_symbol():
    assert stock_quote._futu_code_for_symbol("ASML.AS") is None
    assert stock_quote._futu_code_for_symbol("BRK.B") == "US.BRK.B"


def test_adr_ticker_and_primary_listing_remain_distinct():
    assert stock_quote.detect_symbols("ASML ADR 股價多少？") == ["ASML"]
    assert stock_quote.detect_symbols("ASML.AS 股價多少？") == ["ASML.AS"]


def test_explicit_quote_failure_is_fail_closed_before_llm():
    with (
        patch.object(stock_quote, "should_try_contextual_quote", return_value=True),
        patch.object(stock_quote, "get_taiex_month_line_text", return_value=None),
        patch.object(stock_quote, "get_contextual_quotes_text", return_value=None),
    ):
        out = main._get_explicit_market_quote_reply("005930.KS 股價多少？", context=[])

    assert out is not None
    assert out.startswith("【市場報價｜暫時無法取得】")
    assert "沒有交給模型猜價格" in out


def test_non_quote_exception_does_not_turn_into_market_failure_reply():
    with patch.object(
        stock_quote,
        "should_try_contextual_quote",
        side_effect=RuntimeError("detector unavailable"),
    ):
        out = main._get_explicit_market_quote_reply("晚餐要吃什麼？", context=[])

    assert out is None


@pytest.mark.parametrize(
    "text",
    ["晚餐價格多少？", "裝潢報價多少？", "台北房價多少？"],
)
def test_non_financial_price_questions_do_not_enter_market_quote_route(text):
    assert stock_quote._extract_lookup_query(text) == ""
    assert not stock_quote.should_try_contextual_quote(text)


def test_us_magnificent_seven_request_expands_fixed_basket_with_tsm_adr_and_micron():
    text = "給我昨天美股七巨頭、台積電、美光，跌幅"

    assert stock_quote.detect_symbols(text) == [
        "AAPL",
        "MSFT",
        "GOOGL",
        "AMZN",
        "NVDA",
        "META",
        "TSLA",
        "TSM",
        "MU",
    ]
    assert stock_quote._resolve_quote_symbols(text) == stock_quote.detect_symbols(text)
    assert "2330.TW" not in stock_quote.detect_symbols(text)
    assert stock_quote.should_try_contextual_quote(text)


def test_magnificent_seven_knowledge_question_is_not_misrouted_as_quote_request():
    text = "美股七巨頭是誰？"

    assert stock_quote.detect_symbols(text) == []
    assert not stock_quote.should_try_contextual_quote(text)


def test_historical_basket_uses_completed_daily_session_without_after_close_proxy():
    text = "給我昨天美股七巨頭、台積電、美光，跌幅"
    symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "TSM", "MU"]
    quotes = {
        symbol: {
            "symbol": symbol,
            "last_price": 100.0 + idx,
            "prev_close": 102.0 + idx,
            "change": -2.0,
            "change_pct": -1.96,
            "high": 103.0 + idx,
            "low": 99.0 + idx,
            "timestamp": "2026-08-28 收盤",
            "last_date": "2026-08-28",
            "market_date": "2026-08-28",
            "source": "yahoo_daily",
        }
        for idx, symbol in enumerate(symbols)
    }
    now = datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    with (
        patch.object(
            stock_quote,
            "_fetch_completed_daily_quotes_with_deadline",
            return_value=quotes,
        ) as daily,
        patch.object(stock_quote, "_fetch_contextual_quotes_with_deadline") as realtime,
        patch.object(stock_quote, "_after_close_specs_for_symbol") as proxy,
    ):
        out = stock_quote.get_contextual_quotes_text(text, now=now)

    assert out is not None
    daily.assert_called_once()
    assert daily.call_args.args[0] == symbols
    realtime.assert_not_called()
    proxy.assert_not_called()
    assert out.startswith("【美股最近完成交易日漲跌幅｜2026-08-28】")
    assert "1. Apple（AAPL）" in out
    assert "8. 台積電 ADR（TSM）" in out
    assert "9. 美光（MU）" in out
    assert out.count("漲跌幅：-1.96%") == 9
    assert out.count("資料來源：Yahoo 公開日線（可能延遲）") == 1
    assert "=F" not in out


def test_historical_basket_lists_partial_quote_failures_without_inventing_values():
    text = "給我昨天美股七巨頭、台積電、美光，跌幅"
    quote = {
        "symbol": "AAPL",
        "last_price": 230.0,
        "prev_close": 235.0,
        "change": -5.0,
        "change_pct": -2.13,
        "timestamp": "2026-08-28 收盤",
        "last_date": "2026-08-28",
        "market_date": "2026-08-28",
        "source": "yahoo_daily",
    }

    with patch.object(
        stock_quote,
        "_fetch_completed_daily_quotes_with_deadline",
        return_value={"AAPL": quote},
    ):
        out = stock_quote.get_contextual_quotes_text(
            text,
            now=datetime(2026, 8, 29, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")),
        )

    assert out is not None
    assert "Apple（AAPL）" in out
    assert "未取得：MSFT、GOOGL、AMZN、NVDA、META、TSLA、TSM、MU" in out
    assert "暫時無法取得" not in out


def test_exact_historical_basket_is_answered_by_main_deterministic_route():
    quote = {
        "symbol": "AAPL",
        "last_price": 230.0,
        "prev_close": 235.0,
        "change": -5.0,
        "change_pct": -2.13,
        "timestamp": "2026-08-28 收盤",
        "last_date": "2026-08-28",
        "market_date": "2026-08-28",
        "source": "yahoo_daily",
    }

    with patch.object(
        stock_quote,
        "_fetch_completed_daily_quotes_with_deadline",
        return_value={"AAPL": quote},
    ):
        out = main._get_explicit_market_quote_reply(
            "給我昨天美股七巨頭、台積電、美光，跌幅",
            context=[],
        )

    assert out is not None
    assert out.startswith("【美股最近完成交易日漲跌幅｜2026-08-28】")
    assert "沒有交給模型猜價格" not in out


def test_yahoo_quote_block_is_line_readable_instead_of_one_dense_row():
    ny = ZoneInfo("America/New_York")
    start = datetime(2026, 8, 28, 9, 30, tzinfo=ny)
    end = datetime(2026, 8, 28, 16, 0, tzinfo=ny)
    quote = _quote("AAPL", 230.0, start=start, end=end, timestamp=start)

    block = stock_quote._format_contextual_quote_line(
        quote,
        stock_quote._base_quote_spec("AAPL"),
    )

    assert block.splitlines() == [
        "AAPL｜Apple 現股",
        "價格：230.00 USD",
        "漲跌：+10.00（+0.20%）",
        "高低：250.00 / 210.00",
        f"時間：{quote['timestamp']}",
        "來源：Yahoo 最新公開報價（可能延遲）",
    ]


def test_daily_chart_parser_uses_last_completed_session_not_live_partial_bar():
    ny = ZoneInfo("America/New_York")
    thursday = int(datetime(2026, 8, 27, 13, 0, tzinfo=ny).timestamp())
    friday = int(datetime(2026, 8, 28, 13, 0, tzinfo=ny).timestamp())
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "exchangeTimezoneName": "America/New_York",
                        "currentTradingPeriod": {
                            "regular": {
                                "start": int(datetime(2026, 8, 28, 9, 30, tzinfo=ny).timestamp()),
                                "end": int(datetime(2026, 8, 28, 16, 0, tzinfo=ny).timestamp()),
                            }
                        },
                    },
                    "timestamp": [thursday, friday],
                    "indicators": {
                        "quote": [
                            {
                                "open": [99.0, 104.0],
                                "high": [102.0, 110.0],
                                "low": [98.0, 103.0],
                                "close": [100.0, 105.0],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }

    quote = stock_quote._parse_completed_daily_quote(
        payload,
        "AAPL",
        now=datetime(2026, 8, 28, 12, 0, tzinfo=ny),
    )

    assert quote is not None
    assert quote["market_date"] == "2026-08-27"
    assert quote["last_price"] == 100.0
    assert quote["prev_close"] is None
