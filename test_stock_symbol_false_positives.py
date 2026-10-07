"""2026-10-07: everyday text became a market quote — 「明天0930出發」→ 0930.TW,
「門牌 1234 號」→ 1234.TW (黑松), 「看到 V 字反轉」→ V.  The explicit @咪寶 reply
then became that quote, or 【市場報價｜暫時無法取得】 after up to 5 s of fetching
under the webhook lock; Gemini's stock pre-fetch paid the same fetch time.
Synthetic inputs only; no network."""

from __future__ import annotations

import pytest

import main
import stock_quote


@pytest.fixture
def no_fetch(monkeypatch):
    """Everyday text must cost nothing: any quote fetch or name search fails the test."""
    def fail(*_a, **_k):
        pytest.fail("everyday text started a quote fetch")
    for name in (
        "_fetch_contextual_quotes_with_deadline",
        "_fetch_completed_daily_quotes_with_deadline",
        "_search_yahoo_symbols",
        "get_quote",
    ):
        monkeypatch.setattr(stock_quote, name, fail)


EVERYDAY = [
    # times
    "明天0930出發", "我們 0800 集合", "0930出發", "1830 下班", "晚上 1830 吃飯",
    "明天 1830 怎麼樣？", "明天0930出發，高鐵票多少錢？", "1830 那班車票多少錢？",
    "會議 14:00-1530", "09:30～1030 開盤前集合", "改成 14:30 或 1545，報價單先準備",
    "時間：1830", "1830:00 準時開始",
    # house / room / extension / phone numbers
    "門牌 1234 號", "門牌 1234 號那間房子多少錢？", "房間號碼 5566", "房間號碼 5566，一晚多少錢？",
    "分機 1234", "分機 1234 問一下報價", "電話 2345-6789", "我住 1234 巷，房租又漲了",
    # years
    "2026年", "2026年的目標", "3000 年前的文物",
    # one capital letter in an ordinary sentence
    "看到 V 字反轉", "Plan B 多少錢？", "我選 A 方案，報價比較低",
]


@pytest.mark.parametrize("text", EVERYDAY)
def test_everyday_text_is_not_a_quote_request(no_fetch, text):
    assert stock_quote.detect_symbols(text) == []
    assert not stock_quote.should_try_contextual_quote(text)
    assert stock_quote.get_contextual_quotes_text(text) is None  # Gemini pre-fetch
    assert main._get_explicit_market_quote_reply(text, context=[]) is None


@pytest.mark.parametrize(("text", "wrong"), [
    ("下午 1530 開會討論股價", "1530.TW"),
    ("股價出現 V 字反轉", "V"),
    ("台股 2026 年會漲嗎", "2026.TW"),
    ("日股 2026 年展望", "2026.T"),
])
def test_a_stock_word_does_not_turn_a_time_year_or_letter_into_a_ticker(text, wrong):
    assert wrong not in stock_quote.detect_symbols(text)


@pytest.mark.parametrize(("text", "symbols"), [
    ("2330 今天怎樣", ["2330.TW"]),
    ("今天 2330 怎樣", ["2330.TW"]),
    ("2330 多少", ["2330.TW"]),
    ("2330 股價？", ["2330.TW"]),
    ("2330", ["2330.TW"]),
    ("2330？", ["2330.TW"]),
    ("2330呢？", ["2330.TW"]),
    ("幫我查2330", ["2330.TW"]),
    ("明天 2330 會漲嗎", ["2330.TW"]),
    ("1234 股價多少", ["1234.TW"]),
    ("2330、2317、2454 現在多少", ["2330.TW", "2317.TW", "2454.TW"]),
    ("台積電今天多少", ["2330.TW"]),
    ("0050 收盤", ["0050.TW"]),
    ("00878 多少", ["00878.TW"]),
    ("台股 2330 今天怎樣", ["2330.TW"]),
    ("NVDA 現在多少", ["NVDA"]),
    ("V 股價", ["V"]),
    ("V 的股價多少", ["V"]),
    ("$V", ["V"]),
    ("V stock price", ["V"]),
    ("美股 V 多少", ["V"]),
])
def test_real_quote_asks_still_resolve(text, symbols):
    assert stock_quote.detect_symbols(text) == symbols
    assert stock_quote.should_try_contextual_quote(text)


def test_real_quote_ask_still_gets_the_deterministic_quote(monkeypatch):
    def fetch(symbols, _timeout):
        return {
            symbol: {
                "symbol": symbol, "last_price": 100.0, "prev_close": 99.0,
                "change": 1.0, "change_pct": 1.0, "high": 101.0, "low": 98.0,
                "timestamp": "2026-10-07 10:00", "last_date": "2026-10-07",
                "source": "yahoo_realtime",
            }
            for symbol in symbols
        }
    monkeypatch.setattr(stock_quote, "_fetch_contextual_quotes_with_deadline", fetch)

    out = main._get_explicit_market_quote_reply("2330 今天怎樣", context=[])

    assert out is not None
    assert out.startswith("【市場報價") and "台積電" in out  # 現股 or, after close, its future


def test_an_earlier_time_is_not_a_ticker_to_follow_up_on(no_fetch):
    assert not stock_quote.should_try_contextual_quote("現在多少？", context=[("user", "明天0930出發")])


def test_an_earlier_code_ask_still_carries_into_the_follow_up():
    context = [("user", "2330 今天怎樣")]
    assert stock_quote._resolve_quote_symbols("現在多少？", context=context) == ["2330.TW"]


def test_finance_view_command_still_reads_a_bare_code(monkeypatch):
    import finance_view_db
    seen = []
    monkeypatch.setattr(finance_view_db, "list_by_ticker", lambda _g, ticker, limit=10: seen.append(ticker) or [])
    main._handle_finance_view_command("group", "/觀點 2330")
    assert seen == ["2330.TW"]
