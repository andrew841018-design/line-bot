"""2026-09-26: tickers must never be read out of a URL (`watch?v=` → V,
`/share/v/` → V, `/shorts/` → SHORTS), while a ticker written next to a
link is still found.  Synthetic inputs only; no network."""

from __future__ import annotations

import pytest

import stock_quote


@pytest.mark.parametrize("text", [
    "https://www.youtube.com/watch?v=DEMO1234567",
    "https://www.facebook.com/share/v/DEMO01/",
    "https://www.tiktok.com/@spy/video/1234567890",
    "youtu.be/DEMO1234567?v=1",
    "youtube-nocookie.com/embed/DEMO1234567?share=v",
    "看這個 https://youtube.com/shorts/DEMO1234567?si=SYNTHETIC",
    "（以下是影片連結 https://www.youtube.com/watch?v=DEMO1234567 的內容）",
    "m.youtu.be/DEMO1234567?v=SPY",
    "https://example.test/ａ?share=synthetic-private-id&symbol=SPY",
    "https://example.test/a「b」?symbol=SPY",   # CJK punctuation inside the link
    "https://example.test/a「b」/SPY/",
    "https://example.test/a?q=中「文」&symbol=SPY",
    "https://example.test/a，https://example.test/NVDA 股價多少?",   # second link, same run
    "https://example.test/x「b」?u=https://example.test/SPY",
    "https://www.google.com/search?q=「SPY」",   # bracketed value, last in the link
    "https://example.test/wiki/「SPY」",
])
def test_urls_never_become_tickers(text):
    assert stock_quote.detect_symbols(text) == []


def test_bracketed_link_value_does_not_join_the_request():
    assert stock_quote.detect_symbols("https://www.google.com/search?q=「SPY」 NVDA現在多少?") == ["NVDA"]


@pytest.mark.parametrize("words", [
    "台積電2330現在多少?", "SPY/QQQ現在多少？", "ES=F現在多少?", "SPY&QQQ現在多少?", "ES=F&NQ=F現在多少?",
    "SPY現在多少（?）", "「SPY」多少？",
])
@pytest.mark.parametrize("link", ["https://example.test/a", "https://news.example.com/a?v=1"])
def test_a_link_in_front_does_not_change_the_tickers(link, words):
    assert stock_quote.detect_symbols(f"{link}，{words}") == stock_quote.detect_symbols(words) != []


@pytest.mark.parametrize("text,expected", [
    ("https://example.test/a，SPY&QQQ現在多少?", ["SPY", "QQQ"]),
    ("https://example.test/a，SPY現在多少（?）", ["SPY"]),
    ("https://example.test/a「SPY現在多少?", ["SPY"]),     # never closed: the user's words
    ("https://example.test/a（SPY）現在多少？", ["SPY"]),
    ("https://youtu.be/abcdefghijk（SPY現在多少）?", ["SPY"]),
])
def test_named_target_after_a_link_beats_context(monkeypatch, text, expected):
    monkeypatch.setattr(stock_quote, "_search_yahoo_symbols", lambda *_a, **_k: pytest.fail("no search"))
    assert stock_quote._resolve_quote_symbols(text, context=[("user", "NVDA 現在多少？")]) == expected


@pytest.mark.parametrize("text", [
    "台積電 2330 現在多少 https://news.example.com/a?v=1",
    "https://news.example.com/a?v=1，台積電2330現在多少？",
    "https://news.example.com/a?v=1，台積電2330現在多少?",   # ASCII question mark
])
def test_ticker_next_to_a_link_is_still_found(text):
    assert "2330.TW" in stock_quote.detect_symbols(text)


def test_two_tickers_after_a_link_both_count():
    symbols = stock_quote.detect_symbols("https://news.example.com/a?v=1，SPY/QQQ現在多少？")
    assert "SPY" in symbols and "QQQ" in symbols


def test_long_link_free_text_is_cheap():
    import time

    start = time.monotonic()
    stock_quote._extract_lookup_query("股價多少 " + "a" * 20000)
    stock_quote.detect_symbols("b" * 20000)
    assert time.monotonic() - start < 1.0


def test_lookup_query_never_carries_a_url():
    query = stock_quote._extract_lookup_query("這家公司股價多少 https://example.com/company?id=v")
    assert "http" not in query and "example" not in query


def test_context_link_is_not_an_earlier_ticker():
    context = [("user", "https://www.facebook.com/share/v/DEMO01/")]
    assert not stock_quote.should_try_contextual_quote("現在多少？", context=context)


def test_lookup_query_drops_links_with_punctuation_inside():
    query = stock_quote._extract_lookup_query("合成公司股價多少 https://example.test/a「b」?share=synthetic-private-id")
    assert "share" not in query and "synthetic" not in query and "「b」" not in query
