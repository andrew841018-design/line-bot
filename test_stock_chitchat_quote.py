"""2026-10-24 (PLAN B8): 「股票」閒聊不再被當成報價查詢。

10/16 「咪寶，股票部分你算很久嗎」被抽成名稱「部分你算很久」送去 Yahoo 搜尋，回了
【市場報價｜暫時無法取得】。任何含 股票／個股／指數／期貨 的句子，剩下的字都成了「名稱」。

規則（stock_quote._extract_lookup_query）：名稱查詢要有明確的價格詢問詞、名稱寫在
股價／股票／指數… 前面（後面只能是多少、現在、怎麼樣、呢、嗎這類詢問詞）、剩下的字要像名稱，
而且家人別名和親屬稱謂一律不送搜尋服務。失敗句「暫時無法取得」只在真的偵測到代號時才回。
取捨（寫進規則）：只有「名稱＋股票」、沒有價格詞時不抓報價。
家人名字的來源是 fact_filters.load_name_terms(require_names=True)（別名檔、角色、短名、LINE 顯示名稱、
隱私詞；NFKC＋casefold）；讀不到或是空的就一律不送搜尋（fail closed）。
All sentences and names are synthetic; the family names come from temporary alias / display-name
files that the tests write, never from code.
"""
from __future__ import annotations

import json
import re
import signal
import time
from unittest.mock import patch

import pytest

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


def use_names(monkeypatch, tmp_path, aliases, *, display=None, roles=None):
    """Point the name sources at synthetic files (the real ones live in local, untracked files).

    ``aliases`` is the user_aliases.json mapping, ``display`` the LINE display names the bot looked
    up, ``roles`` the family_roles.local.json mapping.  The real loader reads them on every call."""
    alias_path = tmp_path / "user_aliases.json"
    alias_path.write_text(json.dumps(aliases, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(alias_path))
    if roles is not None:
        role_path = tmp_path / "family_roles.local.json"
        role_path.write_text(json.dumps(roles, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(role_path))
    if display is not None:
        display_path = tmp_path / "member_display_names.json"
        display_path.write_text(json.dumps(display, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv("LINE_MEMBER_DISPLAY_NAMES_PATH", str(display_path))


@pytest.fixture
def family(monkeypatch, tmp_path):
    """Two synthetic family members in the alias file."""
    use_names(monkeypatch, tmp_path, {"U_ONE": "成員甲", "U_TWO": "小乙"})


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


# ── 家人別名、親屬稱謂與 LINE 顯示名稱不送搜尋服務 ─────────────────────────────


def forbid_search(monkeypatch):
    """Make the Yahoo search itself fail the test if anything reaches it."""

    def fail(*_a, **_k):
        pytest.fail("a family query reached the search provider")

    monkeypatch.setattr(stock_quote.yf, "Search", fail)


@pytest.mark.parametrize(
    "text",
    [
        "成員甲股價多少",       # a configured alias on its own
        "小乙的股價多少",        # the possessive form
        "成員甲Samsung股價多少",
        "員甲股價多少",          # the two-character short form of a three-character alias
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


def test_a_name_that_is_not_on_the_list_is_just_a_name(monkeypatch, tmp_path):
    # The list, not the shape of the text, is what denies 「成員甲」: nothing in the code names a
    # family member (the repository is public).
    use_names(monkeypatch, tmp_path, {"U_TWO": "小乙"})
    assert stock_quote._extract_lookup_query("成員甲股價多少") == "成員甲"
    assert stock_quote._extract_lookup_query("小乙股價多少") == ""


def test_the_name_list_is_read_when_the_query_is_checked(monkeypatch, tmp_path):
    use_names(monkeypatch, tmp_path, {"U_TWO": "小乙"})
    assert stock_quote._extract_lookup_query("成員甲股價多少") == "成員甲"
    # a config change takes effect without a restart
    use_names(monkeypatch, tmp_path, {"U_TWO": "小乙", "U_ONE": "成員甲"})
    assert stock_quote._extract_lookup_query("成員甲股價多少") == ""


def test_a_company_name_that_merely_contains_a_kinship_character_is_not_denied(family):
    for name in ("公司", "哥倫比亞", "和泰汽車", "婆羅洲礦業", "弟兄", "公共電視", "台灣大哥大"):
        assert not stock_quote._names_a_family_member(name), name
    assert stock_quote._names_a_family_member("媽媽買的")
    assert stock_quote._names_a_family_member("我家的阿嬤")


def test_a_listed_company_that_looks_like_a_kinship_word_is_looked_up(family, monkeypatch):
    # 大哥／二哥／大姊／大姐 used to be in the kinship list, which refused 台灣大哥大 (Taiwan Mobile).
    seen = []

    class FakeSearch:
        def __init__(self, query, **_kw):
            seen.append(query)
            self.quotes = [{"symbol": "3045.TW", "quoteType": "EQUITY"}]

    monkeypatch.setattr(stock_quote.yf, "Search", FakeSearch)
    assert stock_quote._extract_lookup_query("台灣大哥大股價多少") == "台灣大哥大"
    assert stock_quote.should_try_contextual_quote("台灣大哥大股價多少")
    # The search is asked directly: _resolve_quote_symbols answers 「台灣…多少」 with the default Taiwan
    # package before it looks any name up (_EXPLICIT_TW_DEFAULT_QUOTE_RE) -- a rule that predates this
    # and has nothing to do with kinship words.
    assert stock_quote._search_yahoo_symbols("台灣大哥大", timeout_s=1.5, max_symbols=3) == ["3045.TW"]
    assert seen == ["台灣大哥大"]


def test_a_family_that_calls_someone_big_brother_has_it_in_its_role_aliases(monkeypatch, tmp_path):
    # ...and a household that really says 「大哥」 for a member lists it in family_roles.local.json,
    # which is a name source: there the same words are refused, by configuration, not by code.
    use_names(monkeypatch, tmp_path, {"U_ONE": "成員甲"}, roles={"大哥": "成員甲"})
    assert stock_quote._extract_lookup_query("大哥股價多少") == ""
    assert stock_quote._extract_lookup_query("台灣大哥大股價多少") == ""


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (None, "the alias file does not exist"),
        ("{not json", "the alias file is corrupt"),
        ("[]", "the alias file is not a mapping"),
        ("{}", "the alias file lists nobody"),
        ('{"U_ONE": ""}', "the alias file has only blank names"),
    ],
)
def test_an_unreadable_or_empty_name_list_refuses_every_lookup(monkeypatch, tmp_path, content, why):
    # The REAL loader runs (fact_filters.load_name_terms, require_names=True): it raises when the
    # alias file cannot be read or names nobody.  Nothing may be sent to a search provider when
    # nobody knows whose names to keep out -- not even an ordinary company name.
    path = tmp_path / "user_aliases.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(path))
    forbid_search(monkeypatch)
    text = "MercadoLibre股價多少？"
    assert stock_quote._names_a_family_member("MercadoLibre"), why
    assert stock_quote._extract_lookup_query(text) == "", why
    assert stock_quote._search_yahoo_symbols("MercadoLibre", timeout_s=1.5, max_symbols=3) == [], why
    assert stock_quote._resolve_quote_symbols(text) == [], why


def test_unreadable_privacy_terms_refuse_every_lookup_too(family, monkeypatch, tmp_path):
    terms = tmp_path / "privacy_terms.json"
    terms.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LINE_BOT_PRIVACY_TERMS_PATH", str(terms))
    forbid_search(monkeypatch)
    assert stock_quote._names_a_family_member("MercadoLibre")
    assert stock_quote._extract_lookup_query("MercadoLibre股價多少？") == ""
    assert stock_quote._search_yahoo_symbols("MercadoLibre", timeout_s=1.5, max_symbols=3) == []


def test_a_missing_display_name_file_is_normal(family):
    # Nobody has been looked up yet: the display names only ADD names, the lookup is not refused.
    assert not stock_quote._names_a_family_member("MercadoLibre")
    assert stock_quote._extract_lookup_query("MercadoLibre股價多少？") == "MercadoLibre"


def test_a_display_name_alone_is_enough_to_deny_the_lookup(monkeypatch, tmp_path):
    # A member without an alias is known to the bot only by the LINE display name it looked up.
    use_names(monkeypatch, tmp_path, {"U_ONE": "成員甲"})
    assert stock_quote._extract_lookup_query("Zeta Quill股價多少") == "Zeta Quill"  # not on any list yet
    use_names(monkeypatch, tmp_path, {"U_ONE": "成員甲"}, display={"U_NINE": "Zeta Quill"})
    for text in ("Zeta Quill股價多少", "ZETA QUILL 股價多少", "ZetaQuill股價多少", "zeta quill stock price"):
        assert stock_quote._extract_lookup_query(text) == "", text
    forbid_search(monkeypatch)
    assert stock_quote._search_yahoo_symbols("Zeta Quill", timeout_s=1.5, max_symbols=3) == []


def test_names_are_matched_without_regard_to_case_or_width(monkeypatch, tmp_path):
    # Terms are NFKC + casefold; so is the query.  Dropping either lets 「PETER」 or the full-width
    # 「ＰＥＴＥＲ」 through when the list says 「Peter」 -- and the egress guard sees raw text,
    # not what the shape pattern let in.
    use_names(monkeypatch, tmp_path, {"U_ONE": "成員甲"}, display={"U_NINE": "Zeta Quill"})
    for query in ("zeta quill", "ZETA QUILL", "Ｚｅｔａ Ｑｕｉｌｌ", "ＺＥＴＡ\u3000ＱＵＩＬＬ"):
        assert stock_quote._names_a_family_member(query), query
    forbid_search(monkeypatch)
    assert stock_quote._search_yahoo_symbols("ＺＥＴＡ ＱＵＩＬＬ", timeout_s=1.5, max_symbols=3) == []
    assert stock_quote._search_yahoo_symbols("ZETA QUILL", timeout_s=1.5, max_symbols=3) == []


def test_the_search_itself_refuses_a_family_query(family, monkeypatch):
    # The egress is guarded as well as the extraction, in case another caller ever passes a query in.
    forbid_search(monkeypatch)
    assert stock_quote._search_yahoo_symbols("成員甲", timeout_s=1.5, max_symbols=3) == []
    assert stock_quote._search_yahoo_symbols("我家媽媽", timeout_s=1.5, max_symbols=3) == []


def test_an_ordinary_query_still_reaches_the_search(family, monkeypatch):
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


class _FoundSearch:
    """A Yahoo search that resolves the name to MELI; remembers every query it was asked."""

    queries: list[str] = []

    def __init__(self, query, **_kw):
        type(self).queries.append(query)
        self.quotes = [{"symbol": "MELI", "quoteType": "EQUITY"}]


@pytest.fixture
def found_but_unpriced(monkeypatch):
    """Yahoo resolves 「MercadoLibre」 to MELI, then no price can be fetched."""
    _FoundSearch.queries = []
    monkeypatch.setattr(stock_quote.yf, "Search", _FoundSearch)
    monkeypatch.setattr(stock_quote, "get_taiex_month_line_text", lambda *_a, **_k: None)
    monkeypatch.setattr(stock_quote, "_fetch_contextual_quotes_with_deadline", lambda *_a, **_k: {})
    monkeypatch.setattr(stock_quote, "_fetch_completed_daily_quotes_with_deadline", lambda *_a, **_k: {})
    return _FoundSearch.queries


def test_a_name_yahoo_resolved_but_whose_quote_failed_still_gets_the_notice(found_but_unpriced):
    text = "MercadoLibre股價多少？"
    assert not stock_quote.has_detected_symbol(text)  # offline it is only a name...
    out = main._get_explicit_market_quote_reply(text, context=[])
    # ...but the model must not guess the price of an instrument that WAS found
    assert out is not None and out.startswith("【市場報價｜暫時無法取得】")
    assert "沒有交給模型猜價格" in out
    assert found_but_unpriced == ["MercadoLibre"]  # one search, not a second one to decide on the notice


def test_a_resolved_name_whose_fetch_raised_gets_the_retry_notice(found_but_unpriced, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("fetch blew up")

    monkeypatch.setattr(stock_quote, "_fetch_contextual_quotes_with_deadline", boom)
    out = main._get_explicit_market_quote_reply("MercadoLibre股價多少？", context=[])
    assert out is not None and out.endswith("請稍後再試一次。")
    assert found_but_unpriced == ["MercadoLibre"]


def test_the_quote_path_reports_what_it_resolved_before_it_fetches(found_but_unpriced, monkeypatch):
    resolved: list[str] = []
    assert stock_quote.get_contextual_quotes_text("MercadoLibre股價多少？", resolved_symbols=resolved) is None
    assert resolved == ["MELI"]

    # a text that names nothing leaves the list empty, and the argument stays optional
    class EmptySearch:
        def __init__(self, *_a, **_kw):
            self.quotes = []

    monkeypatch.setattr(stock_quote.yf, "Search", EmptySearch)
    resolved = []
    assert stock_quote.get_contextual_quotes_text("FooBarCorp股價多少？", resolved_symbols=resolved) is None
    assert resolved == []
    assert stock_quote.get_contextual_quotes_text("FooBarCorp股價多少？") is None


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
    "_names_a_family_member": stock_quote._names_a_family_member,
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


# 4× the text should take about 4× the time, not 16×.  The floor is generous on purpose: on a loaded
# machine one scheduling stall is longer than a whole fast check, so a ratio of two tiny numbers
# flakes.  A real blow-up (a repeated alternation) is caught by the 1 s ceiling and by the
# structural no-repeated-group tests below.
_TIMING_FLOOR_S = 0.05


@pytest.mark.parametrize("name", sorted(_PATTERNS))
def test_new_lookup_checks_run_in_linear_time(name):
    check = _PATTERNS[name]
    for shape, make in _SHAPES.items():
        small = min(_seconds(check, make(5000)) for _ in range(3))
        large = min(_seconds(check, make(20000)) for _ in range(3))
        assert large < 1.0, (name, shape)
        assert large < 10 * max(small, _TIMING_FLOOR_S), (name, shape, small, large)


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
