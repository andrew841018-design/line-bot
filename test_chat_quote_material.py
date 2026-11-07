"""2026-11-07 聊行情時帶上即時報價 (Andrew approved the 11/06 proposal).

The prices of what the family talks about reach the model as material, in the burst path and the
research path.  Only symbols leave the bot.  All sentences here are synthetic.
"""
from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import main
import public_research
import reply_guards
import stock_quote

TW_CLOSED = datetime(2026, 11, 6, 20, 0, tzinfo=ZoneInfo("Asia/Taipei"))
TW_OPEN = datetime(2026, 11, 6, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _quote(price=1000.0, **extra):
    return {
        "last_price": price, "change": -10.0, "change_pct": -0.99, "timestamp": "2026/11/06 13:30",
        "last_date": "2026-11-06", "source": "yahoo_realtime", "exchange_timezone": "Asia/Taipei",
        **extra,
    }


# ── which symbols ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("台積電今天跌好多", ["2330.TW"]),
    ("聯發科漲停", ["2454.TW"]),
    ("2330 收盤多少", ["2330.TW"]),
    ("今天台股大漲", ["^TWII"]),
    ("大盤今天崩了", ["^TWII"]),
    ("美股大跌", ["^GSPC"]),
    ("那斯達克跌 3%", ["^IXIC"]),
    ("費半大跌，台股也跟著跌", ["^SOX", "^TWII"]),
    ("輝達暴跌", ["NVDA"]),
    ("Google 跌了 10%", ["GOOGL"]),
    ("谷歌暴跌", ["GOOGL"]),
    ("特斯拉跌破 300", ["TSLA"]),
    ("SOXL 怎麼跌成這樣", ["SOXL"]),
    ("0050 跌到 60 了", ["0050.TW"]),
    ("00878 今天漲", ["00878.TW"]),
    ("00631L 大跌", ["00631L.TW"]),
    ("NVDA跌了", ["NVDA"]),
    ("nvda 跌了", ["NVDA"]),
    ("Apple 股價", ["AAPL"]),
    ("META 大跌", ["META"]),
    ("台積電目前1455", ["2330.TW"]),
    ("2317 大跌", ["2317.TW"]),
    ("買了 2317 跌了", ["2317.TW"]),
    ("黃金破新高", ["GC=F"]),
    ("ＮＶＤＡ跌了", ["NVDA"]),
    ("２３３０ 跌了", ["2330.TW"]),
    ("臺積電跌了", ["2330.TW"]),
    ("南亞科漲停", ["2408.TW"]),
    ("長榮航大漲", ["2618.TW"]),
    ("台積電 2295 跌了", ["2330.TW"]),
    ("長榮航空股價大漲", ["2618.TW"]),
    ("美超微大跌", ["SMCI"]),
    ("長榮航太漲停", ["2645.TW"]),
    ("台積電ADR 跌了", ["TSM"]),
    ("台積ADR昨晚漲了", ["TSM"]),
    ("TSMC ADR 大漲", ["TSM"]),
    ("台塑石化今天跌了", ["6505.TW"]),
    ("台積電的ADR昨晚跌了", ["TSM"]),
    ("台達化今天漲停", ["1309.TW"]),
    ("南亞電路板跌停", ["8046.TW"]),
    ("台積電大跌，友達成交量暴增", ["2330.TW", "2409.TW"]),
])
def test_a_named_instrument_and_a_market_word_on_one_line(text, expected):
    assert stock_quote.chat_quote_symbols(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("台積電跌至2295", ["2330.TW"]),
    ("台積電繼續跌目前2320", ["2330.TW"]),
    ("鴻海現在 215 跌了", ["2317.TW"]),
    ("合成光跌至2295", []),
    ("合成光目前2295 又跌了", []),
    ("大立光跌至2295", ["3008.TW"]),
    ("我買了一張0050股價 102.45 沒想到現在跌到101.80", ["0050.TW"]),
    ("台積電有3000股都跌了", ["2330.TW"]),
    ("台積電 2330 跌了", ["2330.TW"]),
])
def test_a_number_next_to_a_name_is_a_price_not_a_listing(text, expected):
    assert stock_quote.chat_quote_symbols(text) == expected


@pytest.mark.parametrize("text", [
    "長榮航空機票漲價",
    "南亞地區地震",
    "台塑加油站油價漲了",
    "中華電信帳單漲了",
    "華航飛日本多少錢",
    "我體重跌到 60",
    "便當 120 元漲價了",
    "明天 0930 出發",
    "門牌 2330 號",
    "他跌倒了",
    "高鐵票漲價",
    "物價上漲",
    "三星手機很好用",
    "美股開戶要帶什麼",
    "大盤雞好吃",
    "買了 3000 股",
    "血糖 120 跌到 90",
    "跌倒 0050",
    "三星蔥又漲了",
    "爬陽明山跌了一跤",
    "華航機票又漲了",
    "Apple Watch 又漲價了",
    "門牌 2330 又漲了",
    "GOOGLEMAPS 跌了",
    "V 字反轉跌了",
    "黃金比例漲了",
    "東南亞股市大跌",
    "朋友達到目標就賣出",
    "昨天收1455跌了35",
    "保費 3500 漲了",
    "價格從 3500 漲到 4000",
    "管理費2400漲到2800",
    "Andy Mu 說房價又漲了",
    "mu.lin 寄來的那家股價跌了",
    "跌破月MA了",
    "車位 3500 漲了",
    "價格從 2330 漲到 2400",
    "月租 2330 漲了",
    "PG 13 級的電影票漲了",
    "台塑牛排漲了",
    "今日經濟不好，股市跌了",
    "浪費半天，股票跌了",
    "比特幣跟其他 coin 都在跌",
    "我室友達成目標，業績漲了 3 成",
    "黃金地段的房子又漲了",
    "成績加權平均跌了",
    "",
])
def test_everyday_sentences_name_no_market_move(text):
    assert stock_quote.chat_quote_symbols(text) == []


def test_two_letters_are_no_ticker_but_the_name_still_is():
    assert stock_quote.chat_quote_symbols("曼聯 MU 輸球，球票漲了") == []
    assert stock_quote.chat_quote_symbols("美光今天大漲") == ["MU"]
    assert stock_quote.chat_quote_symbols("amd 今天大漲") == ["AMD"]


@pytest.mark.parametrize("text, expected", [
    ("2330 今天跌了", ["2330.TW"]),
    ("大家注意：2454 大漲", ["2454.TW"]),
    ("買了 2317 跌了", ["2317.TW"]),
    ("保險 2882 漲了", []),
    ("車資 2330 漲了", []),
    ("原本 2330 漲到 2454", []),
    ("由 2454 跌到 2330", []),
])
def test_a_bare_code_starts_its_clause(text, expected):
    assert stock_quote.chat_quote_symbols(text) == expected


def test_the_market_word_must_be_on_the_line_of_the_name():
    assert stock_quote.chat_quote_symbols("台積電的員工餐廳很大\n我昨天跌倒了") == []
    assert stock_quote.chat_quote_symbols("今天天氣好\n台積電跌了") == ["2330.TW"]


def test_a_link_is_no_symbol():
    text = "https://example.test/watch?v=AAPL&t=NVDA 跌了"
    assert stock_quote.chat_quote_symbols(text) == []


def test_at_most_three_in_the_order_written():
    text = "台積電、鴻海、聯發科、廣達都跌"
    assert stock_quote.chat_quote_symbols(text) == ["2330.TW", "2317.TW", "2454.TW"]


@pytest.mark.parametrize("text", [
    "AMY 說這個價格漲了",
    "寄到 chen.t@example.com 問股價漲跌",
    "PETER 的股價跌了",
    "ABC 股價大跌",
])
def test_only_known_instruments_ever_reach_the_source(text):
    assert stock_quote.chat_quote_symbols(text) == []


def test_the_newest_words_of_a_long_text_are_read():
    assert stock_quote.chat_quote_symbols("一" * 3000 + "\n台積電跌了") == ["2330.TW"]
    assert stock_quote.chat_quote_symbols("台積電跌了\n" + "一" * 3000) == []


def test_detection_stays_offline(monkeypatch):
    def network(*_a, **_kw):
        raise AssertionError("no network while detecting")

    monkeypatch.setattr(stock_quote, "_search_yahoo_symbols", network)
    monkeypatch.setattr(stock_quote, "_fetch_contextual_quotes_with_deadline", network)
    monkeypatch.setattr(stock_quote.requests, "get", network)
    monkeypatch.setattr(stock_quote, "detect_symbols", network)
    assert stock_quote.chat_quote_symbols("台積電跌了，輝達也跌") == ["2330.TW", "NVDA"]


# ── the block ────────────────────────────────────────────────────────────────

def _chat(symbol="2330.TW", now=TW_CLOSED, **quote):
    return stock_quote._chat_quote(symbol, _quote(**quote), now)


def test_only_the_symbol_goes_to_the_quote_source(monkeypatch):
    asked = []
    monkeypatch.setattr(stock_quote, "_get_contextual_quote", lambda s: asked.append(s) or _quote())
    monkeypatch.setattr(stock_quote, "_search_yahoo_symbols", lambda *_a, **_kw: pytest.fail("no name search"))
    stock_quote.get_chat_quotes(["2330.TW", "NVDA"], now=TW_CLOSED)
    assert sorted(asked) == ["2330.TW", "NVDA"]


def test_line_has_price_move_time_and_says_closed():
    line = _chat().line
    assert "台積電 2330.TW：1,000.00 TWD" in line
    assert "-10.00（-0.99%）" in line
    assert "2026/11/06 13:30" in line
    assert line.endswith("已收盤")


def test_line_says_intraday(monkeypatch):
    monkeypatch.setattr(stock_quote, "_quote_regular_session_state", lambda *_a: "open")
    assert _chat(now=TW_OPEN).line.endswith("盤中")


def test_a_quote_from_an_earlier_day_is_a_close_whatever_the_window_says(monkeypatch):
    monkeypatch.setattr(stock_quote, "_quote_regular_session_state", lambda *_a: "open")
    later = datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert _chat(now=later).line.endswith("已收盤")


def test_a_time_the_page_did_not_give_is_not_made_up():
    assert "時間未知" in _chat(timestamp="2026-11-07 10:05").line


def test_a_quote_without_a_move_still_shows_the_price():
    line = _chat(change=None, change_pct=None).line
    assert "1,000.00" in line and "漲跌" not in line


def test_index_is_in_points():
    assert "加權指數 ^TWII：25,000.00 點" in _chat("^TWII", price=25000.0).line


def test_an_etf_has_no_label_and_still_reads_well():
    quote = _chat("0050.TW")
    assert quote.line.startswith("0050.TW：")
    assert "" not in quote.names


def test_a_quote_knows_what_it_is_called():
    names = _chat().names
    assert {"台積電", "台積", "2330", "2330.TW", "TSMC"} <= set(names)


def test_the_block_is_what_user_typed_text_takes_out_again():
    block = stock_quote.chat_quote_block([_chat()])
    assert "1,000.00" in block
    assert main.reply_guards.user_typed_text(f"台積電今天跌了\n\n{block}").strip() == "台積電今天跌了"


@pytest.mark.parametrize("name", sorted(
    alias for aliases in public_research._MARKET_STOCKS.values() for alias in aliases
    if not alias.isdigit() and alias not in ("蘋果", "aapl", "gm", "ge", "goog")
))
def test_the_research_route_and_the_chat_know_the_same_names(name):
    assert stock_quote.chat_quote_symbols(f"{name} 跌了"), name


def test_only_the_quotes_that_came_back_are_listed(monkeypatch):
    monkeypatch.setattr(
        stock_quote, "_get_contextual_quote", lambda s: _quote() if s == "2330.TW" else None
    )
    quotes = stock_quote.get_chat_quotes(["2330.TW", "2317.TW"], now=TW_CLOSED)
    assert [q.line.split()[1].rstrip("：").split("：")[0] for q in quotes] == ["2330.TW"]


def test_nothing_came_back_is_empty(monkeypatch):
    monkeypatch.setattr(stock_quote, "_get_contextual_quote", lambda _s: None)
    assert stock_quote.get_chat_quotes(["2330.TW"], now=TW_CLOSED) == []


def test_a_stuck_source_does_not_hold_the_reply(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(stock_quote, "_get_contextual_quote", lambda _s: release.wait(5) and None)
    started = time.monotonic()
    try:
        assert stock_quote.get_chat_quotes(["2330.TW"], now=TW_CLOSED, total_timeout_s=0.2) == []
        assert time.monotonic() - started < 2
    finally:
        release.set()


def test_the_log_has_counts_only(monkeypatch, caplog):
    monkeypatch.setattr(stock_quote, "_get_contextual_quote", lambda _s: _quote())
    with caplog.at_level("INFO", logger="stock_quote"):
        stock_quote.get_chat_quotes(["2330.TW"], now=TW_CLOSED)
    assert "chat quotes symbols=1 priced=1" in caplog.text
    assert "2330" not in caplog.text


# ── the number check ─────────────────────────────────────────────────────────

TSMC = stock_quote.ChatQuote(
    "台積電 2330.TW：2,135.00 TWD，漲跌 -105.00（-4.69%），2026/11/06 14:30，已收盤",
    ("台積電", "台積", "2330", "2330.TW", "TSMC"), 2135.0, -105.0, -4.69,
)
TAIEX = stock_quote.ChatQuote(
    "加權指數 ^TWII：39,701.64 點，漲跌 -1,873.08（-4.51%），2026-11-06 13:33 CST，已收盤",
    ("台股", "加權", "加權指數", "大盤", "^TWII"), 39701.64, -1873.08, -4.51,
)
NVDA = stock_quote.ChatQuote(
    "NVIDIA NVDA：186.64 USD，漲跌 +5.04（+2.78%），2026-11-06 16:00 EST，已收盤",
    ("輝達", "NVDA", "NVIDIA"), 186.64, 5.04, 2.78,
)
SP500 = stock_quote.ChatQuote(
    "標普 ^GSPC：6,500.00 點，漲跌 -79.00（-1.21%），2026-11-06 16:00 EST，已收盤",
    ("標普", "標普500", "S&P 500", "^GSPC"), 6500.0, -79.0, -1.21,
)
ETF = stock_quote.ChatQuote("0050.TW：92.50", ("0050.TW", "0050"), 92.5, -4.65, -4.79)


def _guard(reply, quotes=(TSMC,), evidence=""):
    return reply_guards.strip_unbacked_quote_numbers(reply, list(quotes), evidence)


@pytest.mark.parametrize("reply, quotes", [
    ("台積電 11/06 收盤是 2,135 元。", [TSMC]),
    ("台積電 11/06 收 2135.00 元，跌了 105 元，跌幅 4.69%。", [TSMC]),
    ("台積電跌幅約 4.7%。", [TSMC]),
    ("台積電跌了快 5%。", [TSMC]),
    ("台積電跌超過 4%。", [TSMC]),
    ("台積電 11/06 14:30 收在 2,135。", [TSMC]),
    ("11/06 收盤 2,135 元。", [TSMC]),
    ("台積電 13 日收 2,135 元。", [TSMC]),
    ("台積電 13 點 30 分收 2,135 元。", [TSMC]),
    ("台積電現在沒有開盤。", [TSMC]),
    ("這是定期定額 3000 元的做法。", [TSMC]),
    ("台積電（2330）收 2,135 元。", [TSMC]),
    ("台積電約 2,140 元。", [TSMC]),
    ("台積電 -4.69%。", [TSMC]),
    ("台積電有 3 家子公司也漲了。", [TSMC]),
    ("台積電連跌 3 天，收 2,135 元。", [TSMC]),
    ("第三季台積電 EPS 17.44 元，毛利率 59.5%。", [TSMC]),
    ("外資目標價看 3,000 元。", [TSMC]),
    ("台積電昨天收 2,240 元。", [TSMC]),
    ("台積電跌破 2,200 元了。", [TSMC]),
    ("台積電不是 2,295 元，是 2,135 元。", [TSMC]),
    ("你說的 2,295 不對。", [TSMC]),
    ("台積電 100 多元的時候我還沒買。", [TSMC]),
    ("美股今晚 10 點半才開盤。", [NVDA]),
    ("輝達 11/6 美股收盤是上漲 2.78%，不是大跌 10%。", [NVDA]),
    ("收盤價 186.64 美元，單日漲 5.04 美元。", [NVDA]),
    ("標普 500 跌 1.21%。", [SP500]),
    ("加權指數收在 39,701 點。", [TAIEX]),
    ("加權指數收在 3.97 萬點。", [TAIEX]),
    ("0050 週五收盤價是 92.50 元。\n當天跌 4.65 元（-4.79%）。", [ETF]),
    ("0050 權重約 60%。", [ETF]),
    ("美國目前利率 4.25%。", [TSMC]),
    ("輝達收 186.64 美元，台積電收 2,135 元。", [TSMC, NVDA]),
    ("標普本週累計跌 3%。", [SP500]),
    ("類股偏弱，但不等於輝達跌了 10%。", [NVDA]),
    ("等跌到 2,000 元再看。", [TSMC]),
    ("有法人認為跌到 2,000 元是買點。", [TSMC]),
    ("跌 10% 才算修正，現在只跌 4.69%。", [TSMC]),
    ("外資連賣三天，股價從 2,400 元跌到 2,135 元。", [TSMC]),
    ("今天開高走低，盤中最高 2,260 元，收在 2,135 元。", [TSMC]),
    ("台積電週四收 2,240 元。", [TSMC]),
    ("台積電 2,135 元左右。", [TSMC]),
    ("0050 目前 92 元左右。", [ETF]),
    ("今年配息約 2.8 元。", [ETF]),
    ("公司營收 3,000 億元。", [TSMC]),
    ("美股現在 22 點開盤。", [NVDA]),
    ("輝達收 186.64 美元，市值大約 4.5 兆美元。", [NVDA]),
    ("指數收 39,701 點，一檔跌 2.78%。", [TAIEX, NVDA]),
    # v6: an instrument without a quote is left to the prompt, as before this check; deleting
    # its sentence took the right numbers in it along (round 3, seats 1 and 3).
    ("台積電收 2,135 元，跌 4.69%，拖累台股跌 1.2%。", [TSMC]),
    ("大盤跌了 812 點，台積電收 2,135 元。", [TSMC]),
    ("台積電ADR漲 1.5%。", [TSMC]),
    ("聯發科收 1,300 元。", [TSMC]),
    ("輝達最近一路跌到 186.64 美元，今天小漲。", [NVDA]),
    ("輝達漲跌幅 2.78%。", [NVDA]),
    ("台積電收 2,135 元，而非收 2,300 元。", [TSMC]),
    ("大盤跌了 2%，收 26,512 點。", [TSMC]),
    ("這個月跌 10%，今天跌 4.69%。", [TSMC]),
    ("台積電的ADR漲 1.5%。", [TSMC]),
    ("跟大盤一樣跌了 2%，台積電收 2,135 元。", [TSMC]),
    ("台積電近一個月跌了 8%，今天跌 4.69%。", [TSMC]),
    ("台積電連跌三天共跌 6%，今天跌 4.69%。", [TSMC]),
    # v6.2 (round 4b): a span keeps its move; the reply check still knows MU; causes are all causes.
    ("台積電最近跌了 15%，今天跌 4.69%。", [TSMC]),
    ("台積電近期已經跌了 15%。", [TSMC]),
    ("台積電近 3 個月跌了 15%。", [TSMC]),
    ("輝達收 186.64 美元，MU 收 95 美元。", [NVDA]),
    ("受費半大跌 3% 拖累，台積電收 2,135 元。", [TSMC]),
    ("台積電跌 4.69%，美股拖累大盤，收 26,512 點。", [TSMC]),
])
def test_what_is_right_or_not_a_price_stays(reply, quotes):
    assert _guard(reply, quotes) == (reply, 0)


@pytest.mark.parametrize("reply, quotes", [
    ("台積電 11/06 收盤是 2,300 元。", [TSMC]),
    ("台積電跌了 6%。", [TSMC]),
    ("台積電收在 2,100 元。", [TSMC]),
    ("台積電收 3,000 元。", [TSMC]),
    ("台積電現在 900 元。", [TSMC]),
    ("台積電現在每股 1,500 元。", [TSMC]),
    ("台積電跌 5 元。", [TSMC]),
    ("台積電收 186.64。", [TSMC, NVDA]),
    ("輝達收 2,135 元，台積電收 186.64 美元。", [TSMC, NVDA]),
    ("台積電跌不少。它現在 1,200 元。", [TSMC]),
    ("台積電收 2,300 元，拖累台股跌 1.2%。", [TSMC]),
    ("拖累台股跌 1.2%，台積電收 2,300 元。", [TSMC]),
    # v6.1 (round 4): the quote's own code names it; a span word with 今天 is today's; an everyday
    # phrase is no instrument; a cause keeps the subject; the next sentence is held to it again.
    ("0050 跟著大盤跌了 9%。", [ETF]),
    ("0050 今天跟著台股大跌，收在 85 元。", [ETF]),
    ("台積電最近一個交易日收 2,300 元。", [TSMC]),
    ("輝達最近的收盤價是 200 美元。", [NVDA]),
    ("現在 2,300 元接近日內高點。", [TSMC]),
    ("今天一路漲到 200 美元。", [NVDA]),
    ("今天一路跌到 2,000 元。", [TSMC]),
    ("今天收 2,300 元創近期新低。", [TSMC]),
    ("台積電今天大跌。日線黃金交叉，收 2,300 元。", [TSMC]),
    ("台積電跌了 4.69%。受東南亞需求影響，股價 2,300 元。", [TSMC]),
    ("受美股拖累，收在 2,350 元。", [TSMC]),
    ("台積電走弱。大盤也跌了。收盤 2,300 元。", [TSMC]),
    ("台積電的ADR漲 1.5%，台積電收 2,300 元。", [TSMC]),
    ("11/12 收在 2,350 元，創近三個月新低。", [TSMC]),
    ("連跌三天收 2,350 元。", [TSMC]),
    ("受費半拖累跌了 8.5%。", [TSMC]),
    ("台積電今天收 2,330 元。", [TSMC]),
])
def test_a_wrong_level_or_move_goes(reply, quotes):
    assert _guard(reply, quotes)[1] == 1


@pytest.mark.parametrize("reply", [
    "台積電漲了 4.69%。",
    "台積電上漲 105 元。",
    "台積電大漲4.69%",
    "台積電漲到 2,135 元。",
])
def test_the_right_number_with_the_wrong_direction_goes(reply):
    assert _guard(reply)[1] == 1


def test_only_the_sentence_goes():
    text, dropped = _guard("台積電收 2,300 元。這波要看外資動向。")
    assert dropped == 1 and text == "這波要看外資動向。"


def test_the_last_digit_may_be_rounded_or_cut_off():
    half = stock_quote.ChatQuote("合成", ("合成電",), 256.5, -2.25, -2.25)
    assert _guard("合成電收 257 元。", [half])[1] == 0
    assert _guard("合成電收 256 元。", [half])[1] == 0
    assert _guard("合成電跌 2.3%。", [half])[1] == 0
    assert _guard("合成電收 1,500 元。", [half])[1] == 1


def test_a_number_the_search_found_stands():
    reply = "標普收 6,400 點。"
    assert _guard(reply, [SP500])[1] == 1
    assert _guard(reply, [SP500], evidence="標普 500 指數收在 6,400.00 點")[1] == 0
    assert _guard("台積電收 2,240 元。", evidence="收盤 2,240.00 元")[1] == 0
    assert _guard("道瓊收 47,000 點。", [SP500])[1] == 0  # no quote for it: v6 leaves it to the prompt


def test_an_indicator_is_not_an_instrument():
    assert _guard("台積電跌不少。MACD 翻多，現在 2,135 元。")[1] == 0


def test_what_stands_in_quotes_is_left_alone():
    assert _guard("台積電收 2,135 元，你說的「台積電 3000」不對。")[1] == 0


# ── the burst path ───────────────────────────────────────────────────────────

MOVE_TEXT = "台積電今天跌了"
ANSWER = "台積電 11/06 收盤是 2,135 元，跌 4.69%。"


@pytest.fixture
def burst_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_BOT_CHAT_QUOTES", "1")
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr(main.memory, "_DB_PATH", tmp_path / "memory.sqlite3")
    main.memory._init_db()
    assert main.memory.begin_inbound_event("GRP001", "MSG001") == "new"
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda *_a, **_kw: False)
    monkeypatch.setattr(main.memory, "get_context", lambda _gid: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda _gid: ["合成事實"])
    monkeypatch.setattr(main, "_get_persona_notes", lambda _gid: [])
    monkeypatch.setattr(main, "_requires_public_research", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _gid: nullcontext())
    monkeypatch.setattr(main, "_maybe_capture_calendar_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a, **_kw: None)
    env = SimpleNamespace(calls=[], sent=[], stored=[], asked=[], looked_up=[], silent=[])

    def chat(prompt, context, facts, pnotes):
        env.calls.append((prompt, list(facts)))
        return ANSWER

    monkeypatch.setattr(main, "_llm_chat", chat)
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **kw: env.sent.append((text, kw)) or True)
    monkeypatch.setattr(main, "_finish_burst_without_reply", lambda *a, **k: env.silent.append(True))
    monkeypatch.setattr(main.memory, "store_fact_cache", lambda *a, **k: env.stored.append(a))
    monkeypatch.setattr(main.memory, "check_fact_cache", lambda *a, **k: env.looked_up.append(a))
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda symbols, **_kw: env.asked.append(symbols) or [TSMC])
    return env


def _flush(text=MOVE_TEXT):
    main._handle_burst_flush("GRP001", text, "TOKEN001", ["MSG001"])


def test_burst_prices_come_last_in_the_newest_message(burst_env):
    _flush()
    prompt, facts = burst_env.calls[0]
    assert burst_env.asked == [["2330.TW"]]
    assert facts == ["合成事實"]
    assert prompt.rstrip().endswith("--- 市場報價結束 ---")
    assert prompt.index(MOVE_TEXT) < prompt.index("--- 市場報價開始 ---")
    assert "不要說沒有股價工具" in prompt
    assert burst_env.sent[0][0] == ANSWER


def test_burst_guards_see_no_search_and_no_evidence(burst_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda text, **kw: seen.update(kw) or text)
    _flush()
    assert seen["searched"] is False
    assert not seen.get("evidence_text")
    assert seen["prices"] == [TSMC]
    assert "2,135" not in seen["material_text"] and "2,135" not in seen["source_text"]


def test_a_reply_that_claims_a_search_is_still_dropped(burst_env, monkeypatch):
    # The wrong price is in the sentence with the claim: checked first, it would take the claim
    # along and the rest would go out.  The search-claim check has to come before.
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "我查了一下，台積電收 2,300 元。這波要看外資。")
    _flush()
    assert burst_env.sent == [] and burst_env.silent


def test_the_lite_fallback_does_not_read_the_prices(monkeypatch):
    import lite_reply

    seen = []
    monkeypatch.setattr(main, "_gemini_last_tier_reply", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_local_text_llm_fallback", lambda *_a, **_kw: "")
    monkeypatch.setattr(lite_reply, "lite_reply", lambda text, **_kw: seen.append(text) or None)
    prompt = main._with_chat_prices("輝達今天漲了", [NVDA])
    assert "186.64 USD" in prompt  # what its forex handler answered
    assert main._gemini_quota_fallback(prompt, [], []) == ""
    assert seen == ["輝達今天漲了"]


def test_burst_a_wrong_price_never_goes_out(burst_env, monkeypatch):
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "台積電收 2,300 元。")
    _flush()
    assert burst_env.sent == [] and burst_env.silent


def test_burst_a_reply_the_validator_would_replace_stays_silent(burst_env, monkeypatch):
    monkeypatch.setattr(main, "_llm_chat", lambda *_a: "最新資料顯示台積電收盤 2,135 元。")
    _flush()
    assert burst_env.sent == [] and burst_env.silent


def test_burst_price_replies_are_neither_replayed_nor_kept(burst_env):
    _flush()
    assert burst_env.looked_up == []
    assert burst_env.stored == []


def test_burst_price_reply_never_becomes_a_late_push(burst_env):
    _flush()
    assert burst_env.sent[0][1]["allow_push_fallback"] is False


def test_burst_without_a_price_goes_on_as_before(burst_env, monkeypatch):
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: [])
    monkeypatch.setattr(main, "_is_market_quote_request", lambda *_a, **_kw: False)
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: burst_env.calls.append(prompt) or "這波要看外資。")
    _flush()
    assert "市場報價" not in burst_env.calls[0]
    assert burst_env.sent[0][0] == "這波要看外資。"
    assert burst_env.sent[0][1]["allow_push_fallback"] is True


def test_burst_a_failing_fetch_changes_nothing(burst_env, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(stock_quote, "get_chat_quotes", boom)
    _flush()
    assert "市場報價" not in burst_env.calls[0][0]
    assert burst_env.sent[0][0] == ANSWER


def test_burst_a_slow_fetch_is_not_waited_for(burst_env, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(main, "_CHAT_PRICE_WAIT_SEC", 0.1)
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: release.wait(5) and [TSMC])
    started = time.monotonic()
    try:
        _flush()
        assert time.monotonic() - started < 2
    finally:
        release.set()
    assert "市場報價" not in burst_env.calls[0][0]


def test_the_setting_turns_it_off(burst_env, monkeypatch):
    monkeypatch.setenv("LINE_BOT_CHAT_QUOTES", "0")
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: pytest.fail("no fetch"))
    _flush()
    assert burst_env.looked_up
    assert "市場報價" not in burst_env.calls[0][0]


def test_burst_other_talk_fetches_nothing_and_uses_the_cache_as_before(burst_env, monkeypatch):
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: pytest.fail("no fetch"))
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: burst_env.calls.append(prompt) or "可以吃麵。")
    _flush("今天晚餐吃什麼")
    assert burst_env.looked_up
    assert burst_env.sent[0][1]["allow_push_fallback"] is True


def test_burst_a_retry_keeps_the_prices(burst_env, monkeypatch):
    seen = {}

    def retry(outcome, chat_args, enforce_kwargs):
        seen["prompt"] = chat_args[0]
        return ""

    monkeypatch.setattr(main, "_enforce_new_value_reply", lambda *_a, **_kw: "")
    monkeypatch.setattr(main, "_retry_unbacked_reply_with_search", retry)
    _flush()
    assert "--- 市場報價開始 ---" in seen["prompt"]


def test_burst_the_quota_fallback_keeps_the_prices(burst_env, monkeypatch):
    def over_quota(*_a):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    seen = {}
    monkeypatch.setattr(main, "_llm_chat", over_quota)
    monkeypatch.setattr(main, "_is_quota_error", lambda _e: True)
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: None)
    monkeypatch.setattr(main, "_gemini_llm_chat", lambda prompt, *_a: seen.update(prompt=prompt) or ANSWER)
    _flush()
    assert "--- 市場報價開始 ---" in seen["prompt"]


def test_a_shared_page_comes_before_the_prices(burst_env, monkeypatch):
    monkeypatch.setattr(main, "_prefetch_urls", lambda text: f"（以下是連結的網頁內容）\n{text}")
    _flush(f"{MOVE_TEXT} https://example.test/a")
    assert burst_env.calls[0][0].rstrip().endswith("--- 市場報價結束 ---")


# ── the explicit path ────────────────────────────────────────────────────────

def _explicit(monkeypatch, llm_reply=ANSWER):
    from linebot.v3.webhooks import MessageEvent, TextMessageContent

    monkeypatch.setenv("LINE_BOT_CHAT_QUOTES", "1")
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: [NVDA])
    text = "我的輝達又跌了"
    event = MagicMock(spec=MessageEvent)
    event.message = TextMessageContent(id="MSG902", text=f"咪寶 {text}", quoteToken="qt")
    event.source = SimpleNamespace(type="group", group_id="GRP001", user_id="U_TEST")
    event.reply_token = "TOKEN902"
    calls = []
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._build_quoted_block", return_value=""),
        patch("main._requires_public_research", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=None),
        patch("main._llm_chat", side_effect=lambda *a: calls.append(a) or llm_reply),
        patch("main.memory.append_turn"),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._maybe_extract_facts"),
        patch("main._mark_inbound_reply_completed_no_reply"),
        patch("main._reply") as sent,
    ):
        main._handle_explicit_text(event, "GRP001", text)
    return calls, sent


def test_explicit_chat_about_a_move_gets_the_prices(monkeypatch):
    calls, sent = _explicit(monkeypatch, "輝達 11/06 收 186.64 美元，漲了 2.78%。")
    assert calls[0][0].rstrip().endswith("--- 市場報價結束 ---")
    assert sent.call_args.kwargs["allow_push_fallback"] is False


def test_explicit_a_wrong_price_never_goes_out(monkeypatch):
    _calls, sent = _explicit(monkeypatch, "輝達收 150 美元。")
    assert not sent.called or "150" not in str(sent.call_args)


# ── the research path ────────────────────────────────────────────────────────

ROW = {"title": "合成新聞", "url": "https://news.example.test/1", "domain": "news.example.test",
       "published": "Fri, 06 Nov 2026", "full_text": "合成新聞：這家公司財報不如預期，法人下修目標價，股價收跌。",
       "evidence_kind": "full_text"}


@pytest.fixture
def research_env(monkeypatch):
    monkeypatch.setenv("LINE_BOT_CHAT_QUOTES", "1")
    monkeypatch.setenv("LINE_BOT_RESTATEMENT_JUDGE", "0")
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_: nullcontext())
    monkeypatch.setattr(main.memory, "get_context", lambda *_: [])
    monkeypatch.setattr(main.memory, "top_facts", lambda *a, **k: [])
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_: [])
    monkeypatch.setattr(main, "_inbound_reply_by_token", {})
    monkeypatch.setattr(main.memory, "append_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    env = SimpleNamespace(prompts=[], sent=[], crawl=[ROW], silent=[], second=[])
    monkeypatch.setattr(main, "_finish_research_without_reply", lambda *a, **k: env.silent.append(True))
    monkeypatch.setattr(main, "_reply", lambda _tok, text, **kw: env.sent.append((text, kw)))
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: env.prompts.append(prompt) or ANSWER)
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda *_a: list(env.crawl))
    monkeypatch.setattr(main, "_plain_question_answer", lambda *_a, **_kw: env.second.append(True) or ("", False, ""))
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: [TSMC])
    env.event = SimpleNamespace(source=SimpleNamespace(user_id="U_TEST"), reply_token="T_V", message=None)
    return env


def _research(env, text="台積電暴跌"):
    return main._handle_web_research_question(env.event, "GRP001", text, addressed=False)


def test_research_the_prices_stand_apart_from_the_crawl(research_env):
    assert _research(research_env) is True
    prompt = research_env.prompts[0]
    assert prompt.index("2,135.00") < prompt.index("本機爬蟲資料：") < prompt.index("財報不如預期")
    assert research_env.sent[0][0] == ANSWER


def test_research_price_replies_do_not_become_late_pushes(research_env):
    _research(research_env)
    assert research_env.sent[0][1]["allow_push_fallback"] is False


def test_research_nothing_found_stays_as_it_was(research_env):
    research_env.crawl = []
    _research(research_env)
    assert research_env.second and research_env.prompts == [] and research_env.silent


def test_research_without_a_price_is_as_before(research_env, monkeypatch):
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: [])
    _research(research_env)
    assert "市場報價" not in research_env.prompts[0]
    assert research_env.sent[0][1].get("allow_push_fallback", True) is True


def test_research_with_no_market_move_fetches_nothing(research_env, monkeypatch):
    monkeypatch.setattr(stock_quote, "get_chat_quotes", lambda *_a, **_kw: pytest.fail("no fetch"))
    _research(research_env, "今年的颱風假有幾天")
    assert "市場報價" not in research_env.prompts[0]


# ── past a Claude miss ───────────────────────────────────────────────────────

def test_gemini_does_not_fetch_prices_the_prompt_already_has(monkeypatch):
    import gemini_client

    seen = {}
    prompt = main._with_chat_prices(MOVE_TEXT, [TSMC])
    monkeypatch.setattr(stock_quote, "get_contextual_quotes_text", lambda *_a, **_kw: pytest.fail("no refetch"))
    monkeypatch.setattr(gemini_client.settings, "use_rag_graph", True)
    monkeypatch.setattr(gemini_client, "_chat_via_graph", lambda **kw: seen.update(kw) or "")
    gemini_client.chat(prompt, [], ["合成事實"])
    assert seen["facts"] == ["合成事實"]


def test_gemini_still_fetches_for_everyone_else(monkeypatch):
    import gemini_client

    seen = {}
    monkeypatch.setattr(stock_quote, "get_contextual_quotes_text", lambda *_a, **_kw: "【市場報價｜合成】")
    monkeypatch.setattr(gemini_client.settings, "use_rag_graph", True)
    monkeypatch.setattr(gemini_client, "_chat_via_graph", lambda **kw: seen.update(kw) or "")
    gemini_client.chat(MOVE_TEXT, [], ["合成事實"])
    assert any("【市場報價｜合成】" in fact for fact in seen["facts"])
