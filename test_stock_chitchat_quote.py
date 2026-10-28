"""2026-10-24 (PLAN B8): 「股票」閒聊不再被當成報價查詢。

10/16 「咪寶，股票部分你算很久嗎」被抽成名稱「部分你算很久」送去 Yahoo 搜尋，回了
【市場報價｜暫時無法取得】。任何含 股票／個股／指數／期貨 的句子，剩下的字都成了「名稱」。

規則（stock_quote._extract_lookup_query）：名稱查詢要有明確的價格詢問詞、名稱寫在
股價／股票／指數… 前面（後面只能是多少、現在、怎麼樣、呢、嗎這類詢問詞）、剩下的字要像名稱，
而且家人別名和親屬稱謂一律不送搜尋服務。失敗句「暫時無法取得」只在真的偵測到代號時才回。
取捨（寫進規則）：只有「名稱＋股票」、沒有價格詞時不抓報價。
All sentences are synthetic; family aliases come from a monkeypatched configuration, never from code.
"""
from __future__ import annotations

import re
import signal
import time
from unittest.mock import patch

import pytest

import line_mentions
import main
import stock_quote

# 10/24 診斷 agent 的閒聊句，再加上審查（GP1 #11、GP2）列的探測句。
SMALL_TALK = [
    "咪寶，股票部分你算很久嗎",
    "股票那一塊你想很久了嗎",
    "咪寶你的股票是怎麼選的",
    "咪寶，你覺得這支股票的部分要不要等一下",
    "今天股票好難玩",
    "我們家的股票帳戶要不要換券商",
    "股票的事晚點再說",
    "指數型基金的部分我不太懂",
    "期貨的部分你幫我看一下",
    "最近股價的事你有看嗎",
    "股票還有多少天可以賣",
    "咪寶，你覺得這支股價會漲嗎",
    "那股票的漲跌到底是誰在決定？",
    "有人說今年的股價會有泡沫，你怎麼看",
]
# 沒有名稱的感嘆：有「股價」這個價格詞，但名稱不在它前面。
EXCLAMATIONS = [
    "股價跌了很多",
    "今天股價好慘",
    "股價崩了",
    "股價這樣正常嗎",
    "股價怎麼會這樣",
    "這支股價怎麼了",
]
# 指稱不是名稱：代名詞、指示詞、時間詞開頭，或「買的」這種句子片段。
NOT_A_NAME = [
    "他的股價怎麼樣",
    "我們家的股價怎麼樣",
    "這家公司股價多少",
    "哪一支股票股價多少",
    "那檔股價多少",
    "大家的股價都跌了嗎",
    "上次買的股票現在多少",
]
CHAT = SMALL_TALK + EXCLAMATIONS + NOT_A_NAME

REAL_LOOKUPS = [
    ("MercadoLibre股價多少？", "MercadoLibre"),
    ("三星電子股價多少", "三星電子"),
    ("豐田汽車股票現在多少", "豐田汽車"),
    ("咪寶，Samsung Electronics 股價", "Samsung Electronics"),
    ("日經指數現在多少", "日經"),
    ("騰訊股價怎麼樣", "騰訊"),
    ("查一下 ASML 股價", "ASML"),
    ("ASML Holding 股價多少", "ASML Holding"),
    ("日股 Toyota Motor 股價多少？", "Toyota Motor"),
    ("星展銀行股價多少", "星展銀行"),
    ("星展銀行的股價多少", "星展銀行"),  # 所有格的「的」不算名稱
    ("Tesla stock price", "Tesla"),
    ("Procter & Gamble 股價", "Procter & Gamble"),
    ("小米股價多少", "小米"),  # 小X 開頭的真公司不能被當成暱稱擋掉
    ("太古股份股價多少", "太古股份"),
    # 詢問詞之後常見的尾巴（是多少錢、語尾助詞、謝謝、英文的 today／now）不能把真的查價擋掉
    ("三星電子股價現在是多少錢", "三星電子"),
    ("三星電子股價多少啊", "三星電子"),
    ("請問三星電子股價多少，謝謝", "三星電子"),
    ("Tesla stock price today", "Tesla"),
    ("日經225指數現在多少", "日經225"),  # 漢字後面緊接的數字／英文字屬於名稱
]


@pytest.fixture
def no_search(monkeypatch):
    """Chat must cost nothing: no name search, no quote fetch."""

    def fail(*_a, **_k):
        pytest.fail("chat started a symbol search or a quote fetch")

    for name in (
        "_search_yahoo_symbols",
        "_fetch_contextual_quotes_with_deadline",
        "_fetch_completed_daily_quotes_with_deadline",
        "get_quote",
    ):
        monkeypatch.setattr(stock_quote, name, fail)
    monkeypatch.setattr(stock_quote.yf, "Search", fail)


@pytest.fixture
def family(monkeypatch):
    """Two synthetic configured aliases (the real ones live in a local, untracked file)."""
    monkeypatch.setattr(line_mentions, "configured_family_aliases", lambda **_kw: ("成員甲", "小乙"))


# ── 閒聊不是名稱查詢 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", CHAT)
def test_chat_is_not_a_name_lookup(no_search, text):
    assert stock_quote._extract_lookup_query(text) == ""
    assert not stock_quote.should_try_contextual_quote(text)
    assert stock_quote._resolve_quote_symbols(text) == []  # and nothing was sent to a search


@pytest.mark.parametrize("text", CHAT)
def test_explicit_quote_route_stays_silent_for_chat(no_search, text):
    with patch.object(stock_quote, "get_contextual_quotes_text", return_value=None), patch.object(
        stock_quote, "get_taiex_month_line_text", return_value=None
    ):
        out = main._get_explicit_market_quote_reply(text, context=[])
    assert out is None  # no fabricated 【市場報價｜暫時無法取得】 notice


@pytest.mark.parametrize("text", CHAT)
def test_gemini_pre_fetch_does_nothing_for_chat(no_search, text):
    assert stock_quote.get_contextual_quotes_text(text) is None


@pytest.mark.parametrize(("text", "query"), REAL_LOOKUPS)
def test_real_name_lookups_keep_working(text, query):
    assert stock_quote._extract_lookup_query(text) == query
    assert stock_quote.should_try_contextual_quote(text)


@pytest.mark.parametrize(
    "text",
    [
        "股價 三星電子",          # the name has to stand in front of the topic word
        "股票三星電子多少",
        "三星電子股票",           # no price word at all: the documented trade-off
        "三星電子指數",
        "三星電子股票還有多少天",  # 「多少天」 is not a price ask
        "三星電子股價漲了嗎",      # more than ask words after the topic word
        "",
    ],
)
def test_a_lookup_needs_a_price_ask_and_the_name_in_front(no_search, text):
    assert stock_quote._extract_lookup_query(text) == ""


def test_a_link_never_becomes_part_of_a_name(no_search):
    text = "這家公司股價多少 https://example.com/company?id=v"
    assert stock_quote._extract_lookup_query(text) == ""
    named = stock_quote._extract_lookup_query("MercadoLibre股價多少 https://example.com/company?id=v")
    assert named == "MercadoLibre"


# ── 家人別名與親屬稱謂不送搜尋服務 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "成員甲股價多少",       # a configured alias on its own
        "小乙的股價多少",        # the possessive form
        "成員甲Samsung股價多少",
        "媽媽的股價多少",        # kinship words need no configuration
        "爸爸股票現在多少",
        "阿嬤的股價多少",
        "老公股價多少",
    ],
)
def test_family_aliases_and_kinship_never_reach_the_name_search(family, no_search, text):
    assert stock_quote._extract_lookup_query(text) == ""
    assert not stock_quote.should_try_contextual_quote(text)
    assert stock_quote._resolve_quote_symbols(text) == []


def test_without_a_configured_alias_the_same_name_is_just_a_name(monkeypatch):
    # The alias list, not the shape of the text, is what denies 「成員甲」: nothing in the code
    # names a family member (the repository is public).
    monkeypatch.setattr(line_mentions, "configured_family_aliases", lambda **_kw: ())
    assert stock_quote._extract_lookup_query("成員甲股價多少") == "成員甲"


def test_the_alias_list_is_read_when_the_query_is_checked(monkeypatch):
    aliases: list[str] = []
    monkeypatch.setattr(line_mentions, "configured_family_aliases", lambda **_kw: tuple(aliases))
    assert stock_quote._extract_lookup_query("成員甲股價多少") == "成員甲"
    aliases.append("成員甲")  # a config change takes effect without a restart
    assert stock_quote._extract_lookup_query("成員甲股價多少") == ""


def test_a_company_name_that_merely_contains_a_kinship_character_is_not_denied(monkeypatch):
    monkeypatch.setattr(line_mentions, "configured_family_aliases", lambda **_kw: ())
    for name in ("公司", "哥倫比亞", "和泰汽車", "婆羅洲礦業", "弟兄", "公共電視"):
        assert not stock_quote._names_a_family_member(name), name
    assert stock_quote._names_a_family_member("媽媽買的")
    assert stock_quote._names_a_family_member("我家的阿嬤")


def test_an_unreadable_alias_configuration_denies_the_lookup(monkeypatch):
    def broken(**_kw):
        raise OSError("alias file unreadable")

    monkeypatch.setattr(line_mentions, "configured_family_aliases", broken)
    assert stock_quote._extract_lookup_query("三星電子股價多少") == ""
    assert stock_quote._search_yahoo_symbols("三星電子", timeout_s=1.5, max_symbols=3) == []


def test_the_search_itself_refuses_a_family_query(family, monkeypatch):
    # The egress is guarded as well as the extraction, in case another caller ever passes a query in.
    def fail(*_a, **_k):
        pytest.fail("a family query reached the search provider")

    monkeypatch.setattr(stock_quote.yf, "Search", fail)
    assert stock_quote._search_yahoo_symbols("成員甲", timeout_s=1.5, max_symbols=3) == []
    assert stock_quote._search_yahoo_symbols("我家媽媽", timeout_s=1.5, max_symbols=3) == []


def test_an_ordinary_query_still_reaches_the_search(monkeypatch):
    monkeypatch.setattr(line_mentions, "configured_family_aliases", lambda **_kw: ("成員甲",))
    seen = []

    class FakeSearch:
        def __init__(self, query, **_kw):
            seen.append(query)
            self.quotes = [{"symbol": "MELI", "quoteType": "EQUITY"}]

    monkeypatch.setattr(stock_quote.yf, "Search", FakeSearch)
    assert stock_quote._resolve_quote_symbols("MercadoLibre股價多少？", timeout_s=1.5) == ["MELI"]
    assert seen == ["MercadoLibre"]


# ── 「暫時無法取得」只在真的偵測到代號時才回 ─────────────────────────────────────


def test_has_detected_symbol_counts_tickers_known_names_and_follow_ups_only():
    assert stock_quote.has_detected_symbol("005930.KS 股價多少？")
    assert stock_quote.has_detected_symbol("台積電今天多少")
    assert stock_quote.has_detected_symbol("現在多少？", context=[("user", "NVDA 最近很強")])
    # A name only Yahoo's search could resolve is not a detected symbol, and neither is chat.
    assert not stock_quote.has_detected_symbol("MercadoLibre股價多少？")
    assert not stock_quote.has_detected_symbol("現在多少？", context=[("user", "晚餐吃什麼")])
    assert not stock_quote.has_detected_symbol("咪寶，股票部分你算很久嗎")
    assert not stock_quote.has_detected_symbol("")


def test_a_named_instrument_whose_quote_failed_still_gets_the_notice():
    with (
        patch.object(stock_quote, "get_taiex_month_line_text", return_value=None),
        patch.object(stock_quote, "get_contextual_quotes_text", return_value=None),
    ):
        out = main._get_explicit_market_quote_reply("005930.KS 股價多少？", context=[])
    assert out is not None and out.startswith("【市場報價｜暫時無法取得】")
    assert "沒有交給模型猜價格" in out


def test_a_follow_up_to_a_named_instrument_still_gets_the_notice():
    context = [("user", "NVDA 最近很強")]
    with (
        patch.object(stock_quote, "get_taiex_month_line_text", return_value=None),
        patch.object(stock_quote, "get_contextual_quotes_text", return_value=None),
    ):
        out = main._get_explicit_market_quote_reply("現在多少？", context=context)
    assert out is not None and out.startswith("【市場報價｜暫時無法取得】")


def test_a_name_lookup_that_finds_nothing_gets_no_failure_notice(monkeypatch):
    class EmptySearch:
        def __init__(self, *_a, **_kw):
            self.quotes = []

    monkeypatch.setattr(stock_quote.yf, "Search", EmptySearch)
    monkeypatch.setattr(stock_quote, "get_taiex_month_line_text", lambda *_a, **_k: None)
    text = "FooBarCorp股價多少？"
    assert stock_quote.should_try_contextual_quote(text)  # still a quote-shaped ask (reply-only flag)
    assert not stock_quote.has_detected_symbol(text)
    assert main._get_explicit_market_quote_reply(text, context=[]) is None  # ordinary routing, no fixed sentence


def test_an_error_gets_the_retry_notice_only_when_a_symbol_was_detected():
    with (
        patch.object(stock_quote, "get_taiex_month_line_text", return_value=None),
        patch.object(stock_quote, "get_contextual_quotes_text", side_effect=RuntimeError("fetch blew up")),
    ):
        named = main._get_explicit_market_quote_reply("005930.KS 股價多少？", context=[])
        name_only = main._get_explicit_market_quote_reply("FooBarCorp股價多少？", context=[])
    assert named is not None and named.endswith("請稍後再試一次。")
    assert name_only is None


# ── H5：新的 pattern 都是單純的詞表；5k／20k 字都要線性 ─────────────────────────────

_PATTERNS = {
    "_LOOKUP_PRICE_ASK_RE.search": lambda text: stock_quote._LOOKUP_PRICE_ASK_RE.search(text),
    "_LOOKUP_SENTENCE_WORD_RE.search": lambda text: stock_quote._LOOKUP_SENTENCE_WORD_RE.search(text),
    "_LOOKUP_NOT_A_NAME_START_RE.match": lambda text: stock_quote._LOOKUP_NOT_A_NAME_START_RE.match(text),
    "_LOOKUP_NAME_SHAPE_RE.fullmatch": lambda text: stock_quote._LOOKUP_NAME_SHAPE_RE.fullmatch(text),
    "_KINSHIP_RE.search": lambda text: stock_quote._KINSHIP_RE.search(text),
    "_extract_lookup_query": stock_quote._extract_lookup_query,
    "should_try_contextual_quote": stock_quote.should_try_contextual_quote,
}

_SHAPES = {
    "ask and topic words": lambda n: ("股價多少股票" * (n // 6 + 1))[:n],
    "topic words only": lambda n: ("股票" * (n // 2 + 1))[:n],
    "ask words only": lambda n: ("多少" * (n // 2 + 1))[:n],
    "latin words": lambda n: ("ab " * (n // 3 + 1))[:n],
    "one latin run": lambda n: "A" * n,
    "latin with symbols": lambda n: ("S&P " * (n // 4 + 1))[:n],
    "name then topic": lambda n: ("三星電子股價多少" * (n // 8 + 1))[:n],
    "greetings": lambda n: ("咪寶，" * (n // 3 + 1))[:n],
    "greeting then spaces": lambda n: "咪寶" + " " * n,
    "punctuation": lambda n: "：" * n,
    "spaces": lambda n: " " * n,
    "cjk run": lambda n: "字" * n,
    "digits": lambda n: "1" * n,
    "kinship near-misses": lambda n: ("媽爸" * (n // 2 + 1))[:n],
}


class _Deadline(Exception):
    pass


def _seconds(check, text):
    """How long ``check(text)`` takes; a hang fails after 5 s instead of stalling the run."""

    def expire(*_):
        raise _Deadline(text[:12])

    previous = signal.signal(signal.SIGALRM, expire)
    outer = signal.setitimer(signal.ITIMER_REAL, 5)
    started = time.perf_counter()
    try:
        check(text)
        return time.perf_counter() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if outer[0]:  # someone else's timer: give back what is left of it
            left = max(outer[0] - (time.perf_counter() - started), 0.001)
            signal.setitimer(signal.ITIMER_REAL, left, outer[1])


@pytest.fixture
def no_aliases(monkeypatch):
    monkeypatch.setattr(line_mentions, "configured_family_aliases", lambda **_kw: ())


@pytest.mark.parametrize("name", sorted(_PATTERNS))
def test_new_lookup_checks_run_in_linear_time(no_aliases, name):
    check = _PATTERNS[name]
    for shape, make in _SHAPES.items():
        small = min(_seconds(check, make(5000)) for _ in range(3))
        large = min(_seconds(check, make(20000)) for _ in range(3))
        assert large < 1.0, (name, shape)
        assert large < 10 * max(small, 0.002), (name, shape, small, large)  # 4× the text, not 16× the time


@pytest.mark.parametrize(
    "name", ["_LOOKUP_PRICE_ASK_RE", "_LOOKUP_SENTENCE_WORD_RE", "_LOOKUP_NOT_A_NAME_START_RE", "_KINSHIP_RE"]
)
def test_word_list_patterns_have_no_repeated_group(name):
    # H5 (line_bot.md 2026-10-05): a repeated alternation must split every string one way only.
    # Plain word lists have no repeated group at all: no ``)*``, ``)+`` or ``){n,m}``.
    source = getattr(stock_quote, name).pattern
    assert not re.search(r"\)[*+]|\)\{", source), source


def test_the_name_shape_pattern_only_repeats_space_separated_words():
    # The one repeat is `(?: word){0,3}`: a literal space between words, at most three times, and a
    # word can never contain a space, so a string has one way to split (and a hard upper bound).
    source = stock_quote._LOOKUP_NAME_SHAPE_RE.pattern
    assert len(re.findall(r"\)\{0,3\}", source)) == 1 and not re.search(r"\)[*+]", source)
    assert " (?:" in source
