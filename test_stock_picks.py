"""stock_picks（台股推薦，2026-10-10）的測試：全部用合成資料，不連網。"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
import output_validator  # noqa: E402
import stock_picks as sp  # noqa: E402
from linebot.v3.messaging import FlexContainer  # noqa: E402

# LINE 限制：altText 1500 字、單張 bubble 30 KB、carousel 50 KB、action label 20 字、message text 300 字。
ALT_TEXT_MAX = 1500
BUBBLE_MAX_BYTES = 30 * 1024
CAROUSEL_MAX_BYTES = 50 * 1024
LABEL_MAX = 20
ACTION_TEXT_MAX = 300
# 卡片上不能出現的指示語氣（「買超／賣超」是法人資料的名詞，可以出現）。
INSTRUCTION_WORDS = ("買進", "賣出", "停損", "掛單", "觀望", "等回檔", "等站回", "可分批", "加碼", "進場", "出場", "趕快")

T = date(2026, 10, 8)


def _closes(last: float, ma60_target: float | None = None, ma240_target: float | None = None) -> list[float]:
    """造 240 個收盤：前 180 個是 a、後 59 個是 b、最後一個是 last，讓季線與年線剛好是目標值。"""
    ma60 = ma60_target if ma60_target is not None else last
    ma240 = ma240_target if ma240_target is not None else last
    b = (ma60 * 60 - last) / 59
    a = (ma240 * 240 - ma60 * 60) / 180
    return [a] * 180 + [b] * 59 + [last]


def _stock(code="2330", *, trend=None, revenue=None, pe=None, pe_median=None, inst=None, cap=1e12, kind=sp.STOCK):
    return sp.Facts(
        code=code, name="測試", kind=kind, data_date=T, trend=trend, revenue=revenue,
        pe=pe, pe_median=pe_median, inst_net=inst, cap=cap,
    )


def _green_trend(p60: int = 0) -> sp.Trend:
    return sp.Trend(close=100.0 * (1 + p60 / 1000), ma60=100.0, ma240=90.0, p60=p60)


GOOD_REV = sp.Revenue(2026, 8, 25.0, 39.0)
BAD_REV = sp.Revenue(2026, 8, -3.0, 10.0)


# ── 數字與日期 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, expected",
    [("1,234.5", 1234.5), (" 29.56 ", 29.56), ("", None), ("--", None), ("-", None), ("N/A", None),
     ("X", None), ("abc", None), ("nan", None), ("inf", None), (None, None), (True, None), (7, 7.0),
     ("-35.0000", -35.0)],
)
def test_num(raw, expected):
    assert sp._num(raw) == expected


def test_roc_dates():
    assert sp._roc_date("1151008") == date(2026, 10, 8)
    assert sp._roc_date(" 1151008 ") == date(2026, 10, 8)
    assert sp._roc_date("1151332") is None
    assert sp._roc_date("991231") == date(2010, 12, 31)  # 6 碼＝民國 2 位數年
    assert sp._roc_date("１１５１００８") is None  # 全形不收
    assert sp._roc_month("11508") == (2026, 8)
    assert sp._roc_month("11513") is None
    assert sp._roc_month("abc") is None


@pytest.mark.parametrize(
    "ratio, expected",
    [(0.0495, 50), (0.04949, 49), (0.0505, 51), (0.05049, 50), (-0.0495, -50), (-0.0505, -51), (0.0, 0)],
)
def test_permille_half_up(ratio, expected):
    assert sp._permille(ratio) == expected


@pytest.mark.parametrize(
    "price, text",
    [(2550.0, "2,550"), (1000.0, "1,000"), (114.95, "114.95"), (85.30, "85.3"), (9.8, "9.8"), (100.0, "100"),
     (12.34, "12.34"), (2450.37, "2,450"), (112.37, "112.37"), (85.333, "85.33"), (100.004, "100"),
     (999.996, "1,000"), (2180.5, "2,181")],
)
def test_fmt_price(price, text):
    """收盤與均價同一套顯示（判斷年線也用顯示出來的數字）。"""
    assert sp._fmt_price(price) == text


@pytest.mark.parametrize(
    "close, ma240, above",
    [(245.50, 245.53, False), (245.53, 245.53, True), (245.534, 245.53, True), (2180.4, 2180.0, True),
     (2179.4, 2180.0, False), (999.98, 1000.4, False), (1000.4, 999.98, True)],
)
def test_year_line_uses_the_displayed_prices(close, ma240, above):
    trend = sp.Trend(close=close, ma60=close, ma240=ma240, p60=0)
    assert trend.above_year is above
    shown_close, shown_ma = sp._fmt_price(close), sp._fmt_price(ma240)
    assert (float(shown_close.replace(",", "")) >= float(shown_ma.replace(",", ""))) is above


def test_fmt_lots():
    assert sp._fmt_lots(12_345_678) == "1.2 萬張"
    assert sp._fmt_lots(-12_345_678) == "1.2 萬張"
    assert sp._fmt_lots(3_456_400) == "3,456 張"
    assert sp._fmt_lots(9_999_400) == "9,999 張"
    assert sp._fmt_lots(9_999_600) == "1.0 萬張"


# ── 走勢與燈號 ─────────────────────────────────────────────────────────────
def _trend(closes):
    return sp.trend_of(closes, closes[-60:])


def test_trend_needs_enough_positive_closes():
    assert _trend([100.0] * 234) is None  # 年線窗最多缺 5 天
    assert _trend([100.0] * 235) is not None
    assert _trend([100.0] * 239 + [0.0]) is None
    assert _trend([100.0] * 239 + [float("nan")]) is None
    trend = _trend([1.0] * 50 + [100.0] * 240)  # 只看最後 240 個
    assert trend is not None and trend.ma240 == 100.0 and trend.p60 == 0
    assert sp.trend_of([100.0] * 240, [100.0] * 57) is None  # 季線窗最多缺 2 天
    assert sp.trend_of([100.0] * 240, [99.0] * 58) is None  # 兩個窗的最後一個（收盤）要一樣
    quarter = [90.0] * 58 + [100.0]
    trend = sp.trend_of([100.0] * 240, quarter)
    assert trend.ma60 == pytest.approx((90 * 58 + 100) / 59)


def test_close_equal_to_year_line_is_not_red_and_counts_as_long():
    trend = _trend(_closes(100.0, ma60_target=100.0, ma240_target=100.0))
    assert trend.above_year is True
    facts = _stock(trend=trend, revenue=GOOD_REV, pe=10, pe_median=20, inst=5_000_000)
    assert sp.light_of(facts).level == sp.MET
    assert [c.state for c in sp.checks_of(facts)][:2] == [True, True]


@pytest.mark.parametrize(
    "p60, level, text",
    [
        (50, sp.MET, "符合 5 條（共 5 條）"),
        (51, sp.CAUTION, "漲多了（比季線高 5.1%）"),
        (-50, sp.MET, "符合 5 條（共 5 條）"),
        (-51, sp.CAUTION, "比季線低 5.1%"),
    ],
)
def test_quarter_band_boundary_uses_displayed_permille(p60, level, text):
    facts = _stock(trend=_green_trend(p60), revenue=GOOD_REV, pe=10, pe_median=20, inst=5_000_000)
    light = sp.light_of(facts)
    assert (light.level, light.text) == (level, text)
    mid = [c for c in sp.checks_of(facts) if c.key == sp.MID][0]
    assert mid.state is (abs(p60) <= 50)


def test_light_order_red_before_yellow_and_gray_first():
    assert sp.light_of(_stock(trend=None)).level == sp.NO_DATA
    below_year_and_far = sp.Trend(close=80.0, ma60=100.0, ma240=90.0, p60=-200)
    assert sp.light_of(_stock(trend=below_year_and_far)).text == "長期偏弱（在年線之下）"


def test_score_counts_only_true_and_four_of_five_is_green():
    four = _stock(trend=_green_trend(), revenue=GOOD_REV, pe=None, pe_median=20, inst=5_000_000)
    assert sp.score_of(four) == 4
    assert sp.light_of(four).text == "符合 4 條（共 5 條）"
    three = _stock(trend=_green_trend(), revenue=BAD_REV, pe=None, pe_median=20, inst=5_000_000)
    assert sp.score_of(three) == 3
    assert sp.light_of(three) == sp.Light(sp.CAUTION, "走勢可以，其他條件不足（符合 3 條，共 5 條）", score_in_text=3)


@pytest.mark.parametrize("kind, words", [(sp.ETF, "ETF 只看走勢"), (sp.OTC, "上櫃只看走勢")])
def test_etf_and_otc_trend_only(kind, words):
    facts = _stock(code="0050", trend=_green_trend(), kind=kind, cap=None)
    assert sp.light_of(facts) == sp.Light(sp.MET, f"走勢 2 條都符合（{words}）")
    assert [c.key for c in sp.checks_of(facts)] == [sp.LONG, sp.MID]
    assert not sp.is_pick_candidate(facts)
    high = _stock(code="0050", trend=_green_trend(80), kind=kind, cap=None)
    assert sp.light_of(high).text == "漲多了（比季線高 8.0%）"


def test_light_words_never_instruct():
    words = []
    for p60 in (-80, 0, 80):
        for revenue in (GOOD_REV, BAD_REV, None):
            for kind in (sp.STOCK, sp.ETF, sp.OTC):
                words.append(sp.light_of(_stock(trend=_green_trend(p60), revenue=revenue, kind=kind)).text)
    words.append(sp.light_of(_stock(trend=None)).text)
    for text in words:
        for banned in ("買", "賣", "等", "停損", "掛單", "觀望", "推薦"):
            assert banned not in text, (banned, text)


# ── 條件文字 ───────────────────────────────────────────────────────────────
def test_growth_text_and_state():
    check = sp._growth_check(sp.Revenue(2026, 8, 53.32, 39.26))
    assert (check.state, check.text) == (True, "8 月營收比去年 8 月多 53.3%，1–8 月累計比去年同期多 39.3%")
    january = sp._growth_check(sp.Revenue(2027, 1, 5.0, 5.0))
    assert january.text == "1 月營收比去年 1 月多 5.0%"
    tiny = sp._growth_check(sp.Revenue(2026, 8, 0.04, 10.0))  # 顯示 0.0% → 不算成長
    assert tiny.state is False and "多 0.0%" in tiny.text
    down = sp._growth_check(sp.Revenue(2026, 8, -3.0, 10.0))
    assert down.state is False and "少 3.0%" in down.text
    assert sp._growth_check(None).state is None
    assert sp._growth_check(sp.Revenue(2026, 8, None, 1.0)).state is None


def test_cheap_text_and_state():
    assert sp._cheap_check(18.2, 22.0) == sp.Check(sp.CHEAP, True, "本益比 18.2 倍，比同產業中位數 22.0 倍低")
    assert sp._cheap_check(29.56, 22.0).text == "本益比 29.6 倍，比同產業中位數 22.0 倍高"
    assert sp._cheap_check(22.04, 22.0) == sp.Check(sp.CHEAP, True, "本益比 22.0 倍，和同產業中位數一樣")
    assert sp._cheap_check(None, 22.0).state is None
    assert sp._cheap_check(-5.0, 22.0).state is None
    assert sp._cheap_check(15.0, None).state is None


def test_inst_text_and_state():
    assert sp._inst_check(12_345_678) == sp.Check(sp.INST, True, "外資和投信近 10 個交易日合計買超 1.2 萬張")
    assert sp._inst_check(-3_456_000).text == "外資和投信近 10 個交易日合計賣超 3,456 張"
    assert sp._inst_check(400).state is False
    assert sp._inst_check(None).state is None


# ── 大盤 ───────────────────────────────────────────────────────────────────
def test_market_levels_and_hot_first():
    flat = [100.0] * 60
    assert sp.market_of(flat) == sp.Market(sp.NEUTRAL, 0, 0)
    assert sp.market_of([100.0] * 59) is None
    hot = [100.0] * 59 + [110.0]
    market = sp.market_of(hot)
    assert market.level == sp.HOT
    assert sp.market_line(market).startswith("大盤：偏熱（比月線高 ") and sp.market_line(market).endswith("，追高風險較大")
    cold = [100.0] * 59 + [90.0]
    assert sp.market_of(cold).level == sp.COLD
    assert sp.market_line(sp.market_of(cold)).startswith("大盤：偏冷（比月線低 ")
    # 比月線高 7%、比季線低 11%：兩條都觸發時先判偏熱
    both = sp.Market(sp.HOT, 70, -110)
    assert sp.market_line(both) == "大盤：偏熱（比月線高 7.0%），追高風險較大"
    assert sp.market_line(sp.Market(sp.HOT, 30, 130)) == "大盤：偏熱（比季線高 13.0%），追高風險較大"
    assert sp.market_line(None) == "大盤：資料不足"
    assert sp.market_line(sp.Market(sp.NEUTRAL, 34, 78)) == "大盤：中性"


def test_market_threshold_is_strict():
    assert sp.HOT_B20 == 60
    edge = sp.Market(sp.NEUTRAL, 60, 120)
    assert sp.market_line(edge) == "大盤：中性"


# ── 推薦排序 ───────────────────────────────────────────────────────────────
def test_choose_picks_order_and_limit():
    rows = [
        _stock("1111", trend=_green_trend(30), revenue=GOOD_REV, pe=10, pe_median=20, inst=1_000_000, cap=5e11),
        _stock("2222", trend=_green_trend(-5), revenue=GOOD_REV, pe=10, pe_median=20, inst=1_000_000, cap=1e11),
        _stock("3333", trend=_green_trend(10), revenue=GOOD_REV, pe=10, pe_median=20, inst=1_000_000, cap=9e11),
        _stock("4444", trend=_green_trend(10), revenue=GOOD_REV, pe=10, pe_median=20, inst=1_000_000, cap=9e11),
        _stock("5555", trend=_green_trend(0), revenue=GOOD_REV, pe=None, pe_median=20, inst=1_000_000, cap=9e12),
        _stock("0050", trend=_green_trend(0), kind=sp.ETF, cap=None),
        _stock("6666", trend=_green_trend(0), revenue=BAD_REV, pe=None, inst=1_000_000, cap=9e12),
    ]
    picks = sp.choose_picks(rows)
    assert [p.code for p in picks] == ["2222", "3333", "4444"]
    assert [p.code for p in sp.choose_picks(rows, limit=10)] == ["2222", "3333", "4444", "1111", "5555"]


def test_most_missed_counts_unknown_as_missed():
    rows = [
        _stock("1111", trend=_green_trend(80), revenue=GOOD_REV, pe=None, inst=1_000_000),
        _stock("2222", trend=_green_trend(90), revenue=GOOD_REV, pe=30, pe_median=20, inst=1_000_000),
        _stock("3333", trend=_green_trend(0), revenue=GOOD_REV, pe=10, pe_median=20, inst=-1_000_000),
    ]
    assert sp.most_missed(rows) == (sp.MID, 2, 3)
    assert sp.most_missed([]) is None


# ── 卡片 ───────────────────────────────────────────────────────────────────
def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _card_texts(card: dict) -> list[str]:
    texts = []
    for node in _walk(card):
        if node.get("type") == "text":
            texts.append(node["text"])
        action = node.get("action")
        if isinstance(action, dict):
            texts += [action.get("label", ""), action.get("text", "")]
    return texts


def _actions(card: dict) -> list[dict]:
    return [node["action"] for node in _walk(card) if isinstance(node.get("action"), dict)]


def _assert_sendable(card: dict, alt: str, prefix: str) -> None:
    assert alt.startswith(prefix) and 0 < len(alt) <= ALT_TEXT_MAX
    raw = json.dumps(card, ensure_ascii=False).encode("utf-8")
    if card["type"] == "carousel":
        assert len(raw) <= CAROUSEL_MAX_BYTES and 1 <= len(card["contents"]) <= 12
        for bubble in card["contents"]:
            assert len(json.dumps(bubble, ensure_ascii=False).encode("utf-8")) <= BUBBLE_MAX_BYTES
    else:
        assert len(raw) <= BUBBLE_MAX_BYTES
    assert FlexContainer.from_dict(card).to_dict() == card
    for node in _walk(card):
        assert not {"uri", "url", "data", "altUri"} & set(node)
    for action in _actions(card):
        assert action["type"] == "message"
        assert 0 < len(action["label"]) <= LABEL_MAX
        assert 0 < len(action["text"]) <= ACTION_TEXT_MAX
    for text in [alt, *_card_texts(card)]:
        result = output_validator.validate_outbound_text(text)
        assert result.ok and result.text == text, (text, result)
        assert main._prepare_outbound_text(text, source="reply") == text
        assert "@" not in text and "＠" not in text
        for word in INSTRUCTION_WORDS:
            assert word not in text, (word, text)
    assert not main._is_system_status_outbound(alt)
    main._validated_flex_card_message(alt, card)  # main 送卡前的同一道檢查


def _named(code, name, **kwargs):
    facts = _stock(code, **kwargs)
    return sp.Facts(**{**facts.__dict__, "name": name})


PICK = _named("2344", "華邦電", trend=sp.Trend(85.3, 85.1, 61.4, 2), revenue=sp.Revenue(2026, 8, 289.43, 177.39),
              pe=15.2, pe_median=22.0, inst=34_000_000, cap=3.8e11)
PICK4 = _named("2886", "兆豐金", trend=sp.Trend(42.15, 42.07, 38.2, 2), revenue=sp.Revenue(2026, 8, 12.0, 8.0),
               pe=16.1, pe_median=14.0, inst=5_400_000, cap=6.1e11)
TSMC = _named("2330", "台積電", trend=sp.Trend(2550.0, 2360.4, 2100.2, 80), revenue=sp.Revenue(2026, 8, 53.32, 39.26),
              pe=29.56, pe_median=22.0, inst=12_345_678, cap=6.6e13)
ETF0050 = _named("0050", "元大台灣50", trend=sp.Trend(114.95, 112.37, 98.2, 23), kind=sp.ETF, cap=None)


def test_pick_carousel_is_sendable_and_explains_reasons():
    market = sp.Market(sp.HOT, 70, 80)
    bubbles = [sp.pick_bubble(p, i + 1, 2, market, lead_note=(i == 0)) for i, p in enumerate([PICK, PICK4])]
    bubbles.append(sp.faq_bubble([ETF0050, TSMC], T))
    card = sp.carousel(bubbles)
    alt = sp.picks_alt_text([PICK, PICK4], T, not_updated=False)
    assert alt == "股票推薦：只供參考｜華邦電、兆豐金｜資料到 10/08"
    _assert_sendable(card, alt, sp.PREFIX)
    first = _card_texts(bubbles[0])
    assert first[0] == "股票推薦：第 1 檔（共 2 檔）"
    assert sp.TEXT_LEAD_NOTE in first and sp.TEXT_LEAD_NOTE not in _card_texts(bubbles[1])
    assert "大盤：偏熱（比月線高 7.0%），追高風險較大" in first
    assert "✅ 華邦電 2344" in first
    assert "符合 5 條（共 5 條）｜收盤 85.3 元（10/08）" in first
    assert "✅ 有在成長：8 月營收比去年 8 月多 289.4%，1–8 月累計比去年同期多 177.4%" in first
    assert "季線（近 3 個月均價）85.1 元｜年線（近 1 年均價）61.4 元" in first
    assert _actions(bubbles[0]) == []  # 推薦卡已經列出五條，不再放重複的「看細節」
    second = _card_texts(bubbles[1])
    assert "沒過的條件" in second and "⬜ 不算貴：本益比 16.1 倍，比同產業中位數 14.0 倍高" in second
    faq = _card_texts(bubbles[2])
    assert "✅ 元大台灣50 0050" in faq and "走勢 2 條都符合（ETF 只看走勢）" in faq
    assert "⚠️ 台積電 2330" in faq and "漲多了（比季線高 8.0%）" in faq
    for text in [*first, *second, *faq]:
        for red_green in ("🟢", "🟡", "🔴"):  # Andrew 10/10：台股紅漲綠跌，不用紅綠燈
            assert red_green not in text
    assert [a["text"] for a in _actions(bubbles[2])] == ["/股票 0050", "/股票 2330"]


@pytest.mark.parametrize("not_updated", [False, True])
def test_no_pick_bubble(not_updated):
    bubble = sp.no_pick_bubble(T, sp.Market(sp.NEUTRAL, 3, 8), (sp.MID, 38, 50), not_updated=not_updated, lead_note=True)
    card = sp.carousel([bubble, sp.faq_bubble([ETF0050, TSMC], T)])
    alt = sp.picks_alt_text([], T, not_updated=not_updated)
    _assert_sendable(card, alt, sp.PREFIX)
    texts = _card_texts(bubble)
    if not_updated:
        assert sp.TEXT_NOT_UPDATED in texts and alt == "股票推薦：只供參考｜資料暫時沒有更新，先不推薦｜資料到 10/08"
    else:
        assert "這次（資料到 10/08）台股沒有符合條件的" in texts
        assert "最常差的條件：靠近季線（50 檔裡有 38 檔沒過）" in texts
        assert alt == "股票推薦：只供參考｜這次台股沒有符合條件的｜資料到 10/08"
    assert sp.TEXT_LEAD_NOTE in texts


def test_detail_bubble_stock_and_pool_note():
    green_not_picked = _named("2382", "廣達", trend=sp.Trend(300.0, 298.0, 260.0, 7),
                              revenue=sp.Revenue(2026, 8, 20.0, 15.0), pe=12.0, pe_median=20.0, inst=-2_000_000, cap=1e12)
    bubble = sp.detail_bubble(green_not_picked, market=sp.Market(sp.NEUTRAL, 3, 8))
    alt = sp.detail_alt_text(green_not_picked)
    assert alt == "股票評估：只供參考｜廣達 2382｜✅ 符合 4 條（共 5 條）"
    _assert_sendable(bubble, alt, sp.EVAL_PREFIX)
    texts = _card_texts(bubble)
    assert texts[0] == "股票評估：廣達 2382"
    assert texts[1] == "✅ 符合 4 條（共 5 條）" and texts[2] == "收盤 300 元（10/08）"  # 分數不重複寫
    assert "⬜ 有人在買：外資和投信近 10 個交易日合計賣超 2,000 張" in texts
    assert sp.TEXT_POOL_NOTE in texts and not any("沒列進" in t for t in texts)
    assert "依據：證交所、Yahoo｜資料到 10/08" in texts
    picked = sp.detail_bubble(PICK, market=None)
    assert "大盤：資料不足" in _card_texts(picked)
    yellow = _named("2330", "台積電", trend=sp.Trend(2550.0, 2360.4, 2100.2, 80), revenue=sp.Revenue(2026, 8, 53.32, 39.26),
                    pe=29.56, pe_median=22.0, inst=12_345_678, cap=6.6e13)
    yellow_texts = _card_texts(sp.detail_bubble(yellow, market=None))
    assert yellow_texts[1] == "⚠️ 漲多了（比季線高 8.0%）" and yellow_texts[2] == "收盤 2,550 元（10/08）｜符合 3 條（共 5 條）"


def test_detail_bubble_etf_and_gray():
    bubble = sp.detail_bubble(ETF0050, market=None)
    _assert_sendable(bubble, sp.detail_alt_text(ETF0050), sp.EVAL_PREFIX)
    texts = _card_texts(bubble)
    assert "營收、本益比、法人：ETF 只看走勢，不適用" in texts
    assert not any("共 5 條" in t for t in texts)  # ETF 只看走勢，不寫五條的分數
    gray = _named("2330", "台積電", trend=None)
    bubble = sp.detail_bubble(gray, market=None)
    _assert_sendable(bubble, sp.detail_alt_text(gray), sp.EVAL_PREFIX)
    gray_texts = _card_texts(bubble)
    assert "⚪ 資料不足" in gray_texts and gray_texts[2] == "資料到 10/08"  # ⚪ 不補分數
    assert not any("共 5 條" in t for t in gray_texts)



def test_otc_cards_name_the_right_source():
    otc_stock = sp.Facts(code="6488", name="環球晶", kind=sp.OTC, data_date=T,
                         trend=sp.Trend(1130.0, 1005.0, 980.0, 124), market=sp.TPEX)
    otc_etf = sp.Facts(code="00679B", name="元大美債20年", kind=sp.ETF, data_date=T,
                       trend=sp.Trend(29.5, 29.4, 30.1, 3), market=sp.TPEX)
    for facts, words in ((otc_stock, "上櫃只看走勢"), (otc_etf, "ETF 只看走勢")):
        bubble = sp.detail_bubble(facts, market=None)
        _assert_sendable(bubble, sp.detail_alt_text(facts), sp.EVAL_PREFIX)
        texts = _card_texts(bubble)
        assert "依據：櫃買中心、Yahoo｜資料到 10/08" in texts
        assert f"營收、本益比、法人：{words}，不適用" in texts
        assert sp.TEXT_POOL_NOTE not in texts
    assert _card_texts(sp.detail_bubble(otc_etf, market=None))[1] == "⏸️ 長期偏弱（在年線之下）"


def test_light_symbols_follow_andrews_choice():
    assert sp._EMOJI == {sp.MET: "✅", sp.CAUTION: "⚠️", sp.BELOW_YEAR: "⏸️", sp.NO_DATA: "⚪"}
    for color in sp._COLOR.values():  # 字色也避開紅綠
        red, green = int(color[1:3], 16), int(color[3:5], 16)
        assert not (red > 0xA0 and green < 0x60) and not (green > 0x70 and red < 0x40)



@pytest.mark.parametrize(
    "body, expected",
    [
        ("中華精測", (sp.NOT_FOUND, None)),  # 開頭「中華」是中華汽車：後面接的不是問句字就不猜
        ("台灣大車隊", (sp.NOT_FOUND, None)),
        ("長華科技", (sp.NOT_FOUND, None)),
        ("中華的", ("code", "2204")),
        ("台積電現在怎樣", ("code", "2330")),
        ("台積電可以買嗎", ("code", "2330")),
        ("台積電的股價", ("code", "2330")),
        ("台積電?", ("code", "2330")),
        ("台灣大", ("code", "3045")),
        ("中華航空", ("code", "2610")),
        ("台積電好嗎", ("code", "2330")),
        ("台積電要買嗎", ("code", "2330")),
        ("台積電值得買嗎", ("code", "2330")),
        ("台積電漲了嗎", ("code", "2330")),
        ("台積電2330", ("code", "2330")),
        ("台積電(2330)", ("code", "2330")),
        ("台積電～", ("code", "2330")),
        ("台積電？", ("code", "2330")),
        ("中華能源", (sp.NOT_FOUND, None)),  # 單字「能」不算問句字
        ("中華能買嗎", ("code", "2204")),
    ],
)
def test_name_prefix_only_with_question_tails(body, expected):
    index = sp.name_index({"中華": "2204", "台積電": "2330", "台灣大": "3045", "長華": "8070", "華航": "2610",
                           "中華航空": "2610"})
    assert sp.resolve_query(body, index) == expected
