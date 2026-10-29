"""/盤勢 資金輪動（必需消費 XLP／KO 對半導體 SOXX；2026-10-28）的測試。

Andrew 2026-10-28：新增消費型類股（KO、XLP）的監控，用技術面預判資金輪動；放在美股卡、不寫死單一組條件、不推播、KO 也能觸發
（B 案）、研判那一列用描述句。規格是資金輪動計畫 v2（§3 計算、§4 快照、§5 卡片、§2 抓取）與第 4 階段的取捨表。

分四段（每段先在還沒改產品碼時跑紅，再實作）：
- calc：門檻、三值邏輯的真值表、20 日報酬的窗口與隔離區間、連續 2 個交易日才換、values 的驗證。
- publish：us.rot.XLP／us.rot.KO／us.rot.state 三列。
- dashboard：美股卡的三列與 footer。
- pipeline：us.rot 任務（XLP、KO 日線）。

2026-10-29 增補 v3.1（SOXX「高位插針」＝長上影線，偏熱的第三條路，B 案）：calc 的 rot_wick／開高低的解析／分類，pipeline 存
us.ohlc.SOXX，publish 讀它，卡片 footer 多一行。每段一樣先在還沒改產品碼時跑紅。

新的符號一律在測試函式裡才取（parametrize 只放字面值）：舊程式沒有這些名字時，只讓用到的測試紅，不讓整個檔案收集失敗。
合成資料都是程式產生的收盤序列，不連網、不放家人資料。
"""

from __future__ import annotations

import copy
import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import pytest

import market_calc
import market_publish
import output_validator
from test_market_dashboard import ALL_NOWS, DISCLAIMER, ONELINER_US
from test_market_dashboard import DAY as DASH_DAY
from test_market_dashboard import NOW as DASH_NOW
from test_market_dashboard import PREV as DASH_PREV
from test_market_dashboard import (  # noqa: F401  md／paths 是 fixture（用名字被 pytest 找到）
    _never,
    assert_main_gate,
    make_snapshot,
    md,
    paths,
    summary_bubble,
    texts,
    worst_snapshot,
    write_snapshot,
)
from test_market_pipeline_us import D as PIPE_D
from test_market_pipeline_us import HISTORY as PIPE_HISTORY
from test_market_pipeline_us import NOW as PIPE_NOW
from test_market_pipeline_us import (  # noqa: F401  env 是 fixture
    TASKS,
    _num_text,
    closes,
    ctx_at,
    env,
    etfs_ready,
    flat,
    market_http,
    market_tasks,
    nasdaq,
    obs,
    pipe,
    run,
    seed,
    seed_universe,
    sessions,
    src,
)
from test_market_publish import D as PUB_D
from test_market_publish import NOW as PUB_NOW
from test_market_publish import US_DAYS, _set_close
from test_market_publish import world  # noqa: F401  fixture（用名字被 pytest 找到）

TPE = ZoneInfo("Asia/Taipei")


# ═════════════════════════════════════════════════════════════════════════════
# calc：market_calc 的資金輪動區塊
# ═════════════════════════════════════════════════════════════════════════════
CAL = market_calc.nyse_calendar()
DAY = date(2027, 3, 31)  # 週三；往回 300 個交易日都在 NYSE 表內（表從 2026-01-01 起）
SESSIONS = CAL.sessions_ending(DAY, 300)
STATES = ("staples", "semis", "watch_staples", "watch_semis", "none", "insufficient")
VALUE_KEYS = ("xlp_ret20", "soxx_ret20", "soxx_rsi", "soxx_ma60", "soxx_dd240", "soxx_wick_pct", "soxx_wick_day", "xlp_rsi",
              "xlp_above60", "ko_rsi", "ko_above60")
ROT_TEXTS = {
    "TEXT_ROT_XLP_NAME": "必需消費（XLP）",
    "TEXT_ROT_KO_NAME": "可口可樂（KO）",
    "TEXT_ROT_LABEL": "資金輪動",
    "TEXT_ROT_STAPLES": "必需消費比半導體強",
    "TEXT_ROT_SEMIS": "半導體比必需消費強",
    "TEXT_ROT_GAP": " {gap} 個百分點",
    "TEXT_ROT_WATCH_STAPLES": "半導體偏熱、必需消費偏弱",
    "TEXT_ROT_WATCH_SEMIS": "半導體偏冷、必需消費偏熱",
    "TEXT_ROT_NONE": "沒有明顯方向",
    "TEXT_ROT_NOTE_RETURNS": "資金輪動只看價格：近 20 個交易日 XLP {xlp}、SOXX {soxx}",
    "TEXT_ROT_NOTE_WATCH": "資金輪動只看價格：SOXX RSI {soxx_rsi}、離季線 {soxx_ma60}、離 240 日高點 {soxx_dd240}；"
                           "XLP RSI {xlp_rsi}、{xlp_ma60}",
    "TEXT_ROT_NOTE_KO": "；KO RSI {ko_rsi}、{ko_ma60}",
    "TEXT_ROT_NOTE_HELD": "狀態要連續 2 個交易日都一樣才會換",
    "TEXT_ROT_NOTE_WICK": "SOXX {day} 衝高回落（長上影線 {pct}），也算偏熱",
}
VALIDATOR_NOWS = (datetime(2026, 10, 29, 9, 0, tzinfo=TPE), datetime(2027, 1, 15, 9, 0, tzinfo=TPE),
                  datetime(2028, 1, 3, 9, 0, tzinfo=TPE))


def half_up(value, digits):
    """獨立的參考寫法：repr → Decimal → ROUND_HALF_UP（和卡上的 _half_up 同一個規則，但不呼叫它）。"""
    return Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def wave(base, *, drift=0.0004, amp=0.03, phase=0.0, period=7.0):
    """一檔的日收盤（每個交易日一筆）：緩慢的趨勢＋波動，四位小數。"""
    return {day: round(base * (1 + drift * i) * (1 + amp * math.sin(i / period + phase)), 4)
            for i, day in enumerate(SESSIONS)}


def rot_closes():
    return {"XLP": wave(80.0, phase=0.3), "SOXX": wave(250.0, drift=0.001, amp=0.06, phase=1.1, period=5.0),
            "KO": wave(68.0, phase=2.0, period=9.0)}


def window(day=DAY):
    return CAL.sessions_ending(day, 21)


def neutral(**over):
    """注入的指標（都已經是顯示精度）：預設什麼條件都不成立 → none。插針的兩個鍵預設是 None（沒有開高低）：既有的真值表照樣要過，
    「插針缺值回 None 而不是 False」這種錯也會被那幾個 weak／未知的案例抓到（2026-10-29 增補）。"""
    values = {"xlp_ret20": 0.0, "soxx_ret20": 0.0, "soxx_rsi": 50, "soxx_ma60": 0.0, "soxx_dd240": -20.0,
              "soxx_wick_pct": None, "soxx_wick_day": None, "xlp_rsi": 50, "xlp_above60": True, "ko_rsi": 50,
              "ko_above60": True}
    values.update(over)
    return values


def test_starting_thresholds_and_the_numbers_written_in_the_sentences():
    mc = market_calc
    assert (mc.ROT_WINDOW, mc.ROT_GAP_PP, mc.ROT_RSI_HOT, mc.ROT_RSI_COLD, mc.ROT_WEAK_RSI, mc.ROT_NEAR_HIGH_PCT,
            mc.ROT_STRETCH_MA60_PCT, mc.ROT_LOOKBACK, mc.ROT_CONFIRM_DAYS) == (
        20, Decimal("5.0"), 70, 30, 40, Decimal("-3.0"), Decimal("10.0"), 5, 2)
    # 句子裡的 20、2 和常數綁在一起：調了常數，字也要跟著改（不然卡上寫的天數是錯的）
    assert f"近 {mc.ROT_WINDOW} 個交易日" in mc.TEXT_ROT_NOTE_RETURNS
    assert f"連續 {mc.ROT_CONFIRM_DAYS} 個交易日" in mc.TEXT_ROT_NOTE_HELD


def test_state_codes_and_their_sentences():
    mc = market_calc
    assert (mc.ROT_STAPLES, mc.ROT_SEMIS, mc.ROT_WATCH_STAPLES, mc.ROT_WATCH_SEMIS, mc.ROT_NONE,
            mc.ROT_INSUFFICIENT) == STATES
    assert mc.ROT_STATES == STATES
    assert mc.ROT_STATE_TEXT == {
        "staples": mc.TEXT_ROT_STAPLES, "semis": mc.TEXT_ROT_SEMIS, "watch_staples": mc.TEXT_ROT_WATCH_STAPLES,
        "watch_semis": mc.TEXT_ROT_WATCH_SEMIS, "none": mc.TEXT_ROT_NONE, "insufficient": mc.TEXT_INSUFFICIENT,
    }


def test_rot_fixed_texts_are_exact_and_pass_every_gate():
    for name, text in ROT_TEXTS.items():
        assert getattr(market_calc, name) == text, name
        assert text in market_calc.FIXED_TEXTS, name  # 會過 test_market_calc 的禁用字、validator、年份三個掃描
        assert not any(word in text for word in market_calc.FORBIDDEN_WORDS), name
        assert not any(str(year) in text for year in range(2020, 2040)), name
        for now in VALIDATOR_NOWS:
            result = output_validator.validate_outbound_text(text, now=now)
            assert result.ok and result.text == text, (name, now, result.reason)
    assert {name for name in dir(market_calc) if name.startswith("TEXT_ROT_")} == set(ROT_TEXTS)  # 沒有漏測的句子


def test_the_three_rows_sit_right_after_the_us_etf_rows():
    mc = market_calc
    keys = list(mc.ROW_IDS)
    first = keys.index("us.etf.VOO")
    assert keys[first:first + 7] == ["us.etf.VOO", "us.etf.QQQ", "us.etf.SOXX", "us.etf.SPY", "us.rot.XLP", "us.rot.KO",
                                     "us.rot.state"]
    assert mc.ROW_IDS["us.rot.XLP"] is mc.ETF_SPEC and mc.ROW_IDS["us.rot.KO"] is mc.ETF_SPEC
    assert mc.ROW_IDS["us.rot.state"] is mc.ROT_SPEC
    assert mc.ROT_SPEC == {
        "state": frozenset(STATES), "raw": frozenset(STATES),
        "xlp_ret20": "num?", "soxx_ret20": "num?",
        "soxx_rsi": "int?", "soxx_ma60": "num?", "soxx_dd240": "num?",
        "soxx_wick_pct": "num?", "soxx_wick_day": "date?",
        "xlp_rsi": "int?", "xlp_above60": "bool?",
        "ko_rsi": "int?", "ko_above60": "bool?",
    }


# ── 三值邏輯的真值表（直接注入顯示精度的指標；邊界不用收盤去湊，浮點雜訊會讓剛好 −3、−10 算不成立） ──
CLASSIFY_CASES = [
    ("neutral", {}, "none"),
    # 趨勢 T：兩個已四捨五入的報酬相減，±5 含等號
    ("gap_plus_5_0", {"xlp_ret20": 2.5, "soxx_ret20": -2.5}, "staples"),
    ("gap_plus_4_9", {"xlp_ret20": 2.4, "soxx_ret20": -2.5}, "none"),
    ("gap_minus_5_0", {"xlp_ret20": -2.5, "soxx_ret20": 2.5}, "semis"),
    ("gap_minus_4_9", {"xlp_ret20": -2.4, "soxx_ret20": 2.5}, "none"),
    ("both_falling_staples_falls_less", {"xlp_ret20": -1.2, "soxx_ret20": -13.6}, "staples"),
    ("no_xlp_return", {"xlp_ret20": None}, "insufficient"),
    ("no_soxx_return", {"soxx_ret20": None}, "insufficient"),
    # 半導體偏熱＋必需消費偏弱 → watch_staples；RSI 70／69
    ("soxx_rsi_70_xlp_below_ma60", {"soxx_rsi": 70, "xlp_above60": False}, "watch_staples"),
    ("soxx_rsi_69_is_not_hot", {"soxx_rsi": 69, "xlp_above60": False}, "none"),
    # 另一條偏熱：離 240 日高點 ≥−3.0 而且離季線 ≥+10.0
    ("near_high_and_stretched", {"soxx_dd240": -3.0, "soxx_ma60": 10.0, "xlp_above60": False}, "watch_staples"),
    ("dd240_minus_3_1_is_not_near", {"soxx_dd240": -3.1, "soxx_ma60": 10.0, "xlp_above60": False}, "none"),
    ("ma60_9_9_is_not_stretched", {"soxx_dd240": -3.0, "soxx_ma60": 9.9, "xlp_above60": False}, "none"),
    # 必需消費偏弱：RSI 40／41；KO 單獨就算
    ("xlp_rsi_40_is_weak", {"soxx_rsi": 75, "xlp_rsi": 40}, "watch_staples"),
    ("xlp_rsi_41_is_not_weak", {"soxx_rsi": 75, "xlp_rsi": 41}, "none"),
    ("ko_below_ma60_alone", {"soxx_rsi": 75, "ko_above60": False}, "watch_staples"),
    ("ko_rsi_40_alone", {"soxx_rsi": 75, "ko_rsi": 40}, "watch_staples"),
    ("ko_rsi_41_alone_is_not_weak", {"soxx_rsi": 75, "ko_rsi": 41}, "none"),
    # 半導體偏冷＋必需消費偏熱 → watch_semis；RSI 30／31、離季線 −10.0／−9.9、必需消費 RSI 70／69
    ("soxx_rsi_30_xlp_rsi_70", {"soxx_rsi": 30, "xlp_rsi": 70}, "watch_semis"),
    ("soxx_rsi_31_is_not_cold", {"soxx_rsi": 31, "xlp_rsi": 70}, "none"),
    ("ma60_minus_10_is_cold", {"soxx_ma60": -10.0, "xlp_rsi": 70}, "watch_semis"),
    ("ma60_minus_9_9_is_not_cold", {"soxx_ma60": -9.9, "xlp_rsi": 70}, "none"),
    ("xlp_rsi_69_is_not_hot", {"soxx_rsi": 30, "xlp_rsi": 69}, "none"),
    ("ko_rsi_70_alone", {"soxx_rsi": 30, "ko_rsi": 70}, "watch_semis"),
    # SOXX 同時偏熱又偏冷：X＝none，回到 T
    ("hot_and_cold_t_none", {"soxx_rsi": 70, "soxx_ma60": -10.0, "xlp_above60": False, "xlp_rsi": 70}, "none"),
    ("hot_and_cold_t_staples", {"soxx_rsi": 70, "soxx_ma60": -10.0, "xlp_above60": False, "xlp_rsi": 70,
                                "xlp_ret20": 5.0}, "staples"),
    ("hot_and_cold_t_semis", {"soxx_rsi": 70, "soxx_ma60": -10.0, "xlp_above60": False, "xlp_rsi": 70,
                              "soxx_ret20": 5.0}, "semis"),
    # X 不是 none 就用 X（和 T 方向不同也一樣）
    ("watch_staples_over_t_semis", {"soxx_rsi": 75, "xlp_above60": False, "soxx_ret20": 10.0}, "watch_staples"),
    ("watch_semis_over_t_staples", {"soxx_rsi": 25, "xlp_rsi": 75, "xlp_ret20": 10.0}, "watch_semis"),
    # 三值邏輯：未知不當成假
    ("ko_unknown_soxx_hot_t_none", {"soxx_rsi": 75, "ko_rsi": None, "ko_above60": None}, "insufficient"),
    ("ko_unknown_soxx_hot_t_semis", {"soxx_rsi": 75, "ko_rsi": None, "ko_above60": None, "soxx_ret20": 10.0},
     "semis"),
    ("ko_unknown_xlp_already_weak", {"soxx_rsi": 75, "ko_rsi": None, "ko_above60": None, "xlp_above60": False},
     "watch_staples"),
    ("ko_unknown_soxx_neither", {"ko_rsi": None, "ko_above60": None}, "none"),
    ("xlp_unknown_soxx_hot", {"soxx_rsi": 75, "xlp_rsi": None, "xlp_above60": None}, "insufficient"),
    ("soxx_unknown_staples_neither", {"soxx_rsi": None, "soxx_ma60": None, "soxx_dd240": None}, "none"),
    ("soxx_unknown_staples_weak", {"soxx_rsi": None, "soxx_ma60": None, "soxx_dd240": None, "xlp_above60": False},
     "insufficient"),
    ("soxx_unknown_staples_weak_t_staples", {"soxx_rsi": None, "soxx_ma60": None, "soxx_dd240": None,
                                             "xlp_above60": False, "xlp_ret20": 6.0}, "staples"),
    ("near_high_unknown_rsi_low", {"soxx_rsi": 50, "soxx_dd240": None, "soxx_ma60": 12.0, "xlp_above60": False},
     "insufficient"),
    ("hot_known_cold_unknown", {"soxx_rsi": 75, "soxx_ma60": None, "xlp_above60": False}, "watch_staples"),
    ("staples_hot_unknown_soxx_cold", {"soxx_rsi": 25, "xlp_rsi": None, "xlp_above60": None, "ko_rsi": None,
                                       "ko_above60": None}, "insufficient"),
]


@pytest.mark.parametrize("over,expected", [case[1:] for case in CLASSIFY_CASES], ids=[case[0] for case in CLASSIFY_CASES])
def test_classify_truth_table(over, expected):
    values = neutral(**over)
    before = dict(values)
    assert market_calc.rot_classify(values) == expected
    assert values == before  # 純函式：不改傳進來的 dict


def test_the_truth_table_reaches_all_six_states():
    assert {expected for _name, _over, expected in CLASSIFY_CASES} == set(STATES)
    assert set(market_calc.ROT_STATES) == set(STATES)


A, B, C, NONE, INS = "staples", "watch_staples", "semis", "none", "insufficient"
CONFIRM_CASES = [
    ([A, A], A), ([A, B], NONE), ([A, A, B], A), ([A, B, A], NONE), ([A, A, B, A], A),
    ([A, A, INS], INS), ([A, A, INS, B], NONE), ([A, A, B, C, B], A), ([A, B, C, B, C], NONE),
    ([], INS), ([A], NONE), ([NONE, NONE], NONE), ([B, B, INS, INS], INS), ([INS, B, B], B),
]


@pytest.mark.parametrize("raws,expected", CONFIRM_CASES, ids=["-".join(raws) or "empty" for raws, _ in CONFIRM_CASES])
def test_a_state_needs_two_trading_days_in_a_row(raws, expected):
    before = list(raws)
    assert market_calc.rot_confirmed(raws) == expected
    assert raws == before


@pytest.mark.parametrize("value", [69.5, 70.5, 4.95, -9.95, 0.05, -0.05, 12.25, 99.95])
def test_calc_rounding_matches_the_card_rounding(value):
    """門檻比的是存下來的顯示值；calc 的 half-up 和卡上的 _half_up 要一模一樣（漂移測試）。"""
    import market_dashboard

    one, whole = market_calc._rot_1dp(value), market_calc._rot_int(value)
    assert one == float(market_dashboard._half_up(value, 1)) == float(half_up(value, 1))
    assert whole == int(market_dashboard._half_up(value, 0)) == int(half_up(value, 0))
    assert type(one) is float and type(whole) is int


def test_rot_inputs_rounds_the_technicals_half_up():
    closes = rot_closes()
    got = market_calc.rot_inputs(closes, {}, CAL, DAY)
    assert set(got) == set(VALUE_KEYS)
    soxx = market_calc.technicals(closes["SOXX"], CAL, DAY)
    xlp = market_calc.technicals(closes["XLP"], CAL, DAY)
    ko = market_calc.technicals(closes["KO"], CAL, DAY)
    assert None not in (soxx.rsi14, soxx.ma_dist[60], soxx.dd240_pct, xlp.rsi14, xlp.above[60], ko.rsi14, ko.above[60])
    assert got["soxx_rsi"] == int(half_up(soxx.rsi14, 0)) and type(got["soxx_rsi"]) is int
    assert got["soxx_ma60"] == float(half_up(soxx.ma_dist[60], 1)) and type(got["soxx_ma60"]) is float
    assert got["soxx_dd240"] == float(half_up(soxx.dd240_pct, 1))
    assert got["xlp_rsi"] == int(half_up(xlp.rsi14, 0)) and got["xlp_above60"] is xlp.above[60]
    assert got["ko_rsi"] == int(half_up(ko.rsi14, 0)) and got["ko_above60"] is ko.above[60]
    first = window()[0]
    for sym in ("XLP", "SOXX"):
        expected = float(half_up((closes[sym][DAY] / closes[sym][first] - 1) * 100, 1))
        assert got[f"{sym.lower()}_ret20"] == expected and type(got[f"{sym.lower()}_ret20"]) is float


def test_the_gap_subtracts_the_two_rounded_returns():
    """XLP −1.24%、SOXX −13.56%：各自四捨五入是 −1.2、−13.6，差 12.4（先相減再四捨五入會是 12.3，和 footer 對不上）。"""
    closes = rot_closes()
    first = window()[0]
    closes["XLP"][first], closes["XLP"][DAY] = 100.0, 98.76
    closes["SOXX"][first], closes["SOXX"][DAY] = 100.0, 86.44
    got = market_calc.rot_inputs(closes, {}, CAL, DAY)
    assert (got["xlp_ret20"], got["soxx_ret20"]) == (-1.2, -13.6)
    assert market_calc.rot_gap(got) == Decimal("12.4")
    assert market_calc.rot_gap({"xlp_ret20": -1.2, "soxx_ret20": None}) is None
    assert market_calc.rot_gap({"xlp_ret20": None, "soxx_ret20": 3.0}) is None


@pytest.mark.parametrize("sym,which", [("XLP", "first"), ("SOXX", "first"), ("XLP", "last"), ("SOXX", "last")])
def test_a_missing_window_end_drops_both_returns(sym, which):
    closes = rot_closes()
    del closes[sym][window()[0] if which == "first" else DAY]
    got = market_calc.rot_inputs(closes, {}, CAL, DAY)
    assert got["xlp_ret20"] is None and got["soxx_ret20"] is None
    other = "SOXX" if sym == "XLP" else "XLP"
    assert got[f"{other.lower()}_rsi"] is not None  # 另一檔的技術面照算


def test_a_missing_close_inside_the_window_does_not_drop_the_returns():
    closes = rot_closes()
    full = market_calc.rot_inputs(closes, {}, CAL, DAY)
    for sym in ("XLP", "SOXX"):
        del closes[sym][window()[10]]
    holed = market_calc.rot_inputs(closes, {}, CAL, DAY)
    assert holed["xlp_ret20"] == full["xlp_ret20"] is not None
    assert holed["soxx_ret20"] == full["soxx_ret20"] is not None


@pytest.mark.parametrize("sym", ["XLP", "SOXX"])
def test_a_quarantine_counts_only_when_it_touches_the_window(sym):
    """隔離區間結束在窗口第一天的前一天 → 沒碰到；結束在第一天 → 碰到（_touches_quarantine 的 finish >= start）。"""
    closes = rot_closes()
    first = window()[0]
    before = first - timedelta(days=1)
    clear = market_calc.rot_inputs(closes, {sym: [(before - timedelta(days=20), before)]}, CAL, DAY)
    assert clear["xlp_ret20"] is not None and clear["soxx_ret20"] is not None
    touched = market_calc.rot_inputs(closes, {sym: [(before - timedelta(days=20), first)]}, CAL, DAY)
    assert touched["xlp_ret20"] is None and touched["soxx_ret20"] is None
    open_ended = market_calc.rot_inputs(closes, {sym: [(DAY, None)]}, CAL, DAY)  # 還沒解除的隔離
    assert open_ended["xlp_ret20"] is None and open_ended["soxx_ret20"] is None


def test_ko_counts_only_when_it_has_the_day_itself():
    full = market_calc.rot_inputs(rot_closes(), {}, CAL, DAY)
    assert full["ko_rsi"] is not None and full["ko_above60"] is not None
    closes = rot_closes()
    del closes["KO"][DAY]
    late = market_calc.rot_inputs(closes, {}, CAL, DAY)
    assert late["ko_rsi"] is None and late["ko_above60"] is None
    assert {k: v for k, v in late.items() if not k.startswith("ko_")} == {k: v for k, v in full.items()
                                                                          if not k.startswith("ko_")}
    closes.pop("KO")
    assert market_calc.rot_inputs(closes, {}, CAL, DAY) == late


@pytest.mark.parametrize("how", ["saturday", "holiday", "closed_override"])
def test_a_research_day_that_is_not_a_session_is_insufficient(how):
    """R 不是交易日（週末、休市、日曆覆寫成休市）：收盤表裡就算有那天的列也不拿來算，窗口不錯位。"""
    closes = rot_closes()
    cal = CAL
    if how == "saturday":
        day = date(2027, 3, 27)
    elif how == "holiday":
        day = date(2027, 3, 26)  # NYSE 休市（耶穌受難日）
    else:
        day, cal = DAY, market_calc.nyse_calendar(closed_overrides=[DAY])
    for sym in closes:
        closes[sym][day] = closes[sym].get(day, closes[sym][SESSIONS[-2]])
    got = market_calc.rotation_values(closes, {}, cal, day)
    assert got["state"] == got["raw"] == "insufficient"
    assert all(got[key] is None for key in VALUE_KEYS), got


def test_rotation_values_are_plain_json_values_that_pass_the_check():
    closes = rot_closes()
    got = market_calc.rotation_values(closes, {}, CAL, DAY)
    assert set(got) == {*VALUE_KEYS, "raw", "state"}
    assert got["raw"] == market_calc.rot_classify(got) == market_calc.rot_classify(
        market_calc.rot_inputs(closes, {}, CAL, DAY))
    assert all(type(got[key]) is float for key in ("xlp_ret20", "soxx_ret20", "soxx_ma60", "soxx_dd240"))
    assert all(type(got[key]) is int for key in ("soxx_rsi", "xlp_rsi", "ko_rsi"))
    assert json.loads(json.dumps(got)) == got  # 沒有 Decimal 之類 publish 編不出來的型別
    assert market_calc.rotation_values_ok(got)


def test_rotation_values_does_not_touch_its_inputs():
    closes = rot_closes()
    quarantine = {"XLP": [(SESSIONS[0], SESSIONS[3])], "SOXX": [], "KO": [(SESSIONS[5], None)]}
    before = copy.deepcopy((closes, quarantine))
    market_calc.rotation_values(closes, quarantine, CAL, DAY)
    assert (closes, quarantine) == before
    assert list(closes["XLP"]) == list(before[0]["XLP"])  # 連 dict 的順序都沒動


@pytest.mark.parametrize("plan,expected", [
    (["semis", A, A, B, C, B], A),  # 往回 5 天找得到連續兩天一樣：[A,A,B,C,B] → A（只看 4 天就找不到）
    ([A, A, B, C, B, C], NONE),  # 往回 5 天是 [A,B,C,B,C] → none（看 6 天會找到第一天的 A）
])
def test_rotation_values_recomputes_the_last_five_sessions(monkeypatch, plan, expected):
    days = CAL.sessions_ending(DAY, len(plan))
    states = dict(zip(days, plan))
    seen = []

    def fake_inputs(closes, quarantine, cal, day, *, ohlc=None):
        seen.append((closes, quarantine, cal, day, ohlc))
        return {"day": day}

    monkeypatch.setattr(market_calc, "rot_inputs", fake_inputs)
    monkeypatch.setattr(market_calc, "rot_classify", lambda values: states[values["day"]])
    closes, quarantine, ohlc = {"XLP": {}}, {}, {DAY: (250.0, 256.0, 249.9, 250.0)}
    got = market_calc.rotation_values(closes, quarantine, CAL, DAY, ohlc=ohlc)
    assert got == {"day": DAY, "raw": plan[-1], "state": expected}
    assert sorted({item[3] for item in seen}) == days[-5:]  # 研判日＋往前 4 個交易日，用同一份輸入
    assert all(item[0] is closes and item[1] is quarantine and item[2] is CAL for item in seen)
    # 開高低也一樣：5 個回看日拿到的是同一個 ohlc 物件（只傳給研判日的話，沿用前幾天的插針就會消失；2026-10-29 增補 C3b）
    assert len(seen) == 5 and all(item[4] is ohlc for item in seen)


def good_values(**over):
    values = neutral(soxx_rsi=75, xlp_above60=False)  # 半導體偏熱＋必需消費偏弱
    values.update(raw="watch_staples", state="watch_staples")
    values.update(over)
    return values


def test_rotation_values_ok_accepts_consistent_values_and_the_limits():
    ok = market_calc.rotation_values_ok
    assert ok(good_values())
    assert ok(good_values(state="staples"))  # 狀態沿用前幾天的（state≠raw）可以，只要兩邊都算得出來
    limits = neutral(xlp_ret20=1000.0, soxx_ret20=-99.9, soxx_rsi=100, soxx_ma60=1000.0, soxx_dd240=-100.0, xlp_rsi=0,
                     xlp_above60=None, ko_rsi=100, ko_above60=None)
    raw = market_calc.rot_classify(limits)
    assert ok({**limits, "raw": raw, "state": raw})
    zero = neutral(soxx_dd240=0.0, soxx_ma60=-99.9, xlp_rsi=None)
    raw = market_calc.rot_classify(zero)
    assert ok({**zero, "raw": raw, "state": raw})
    gone = neutral(xlp_ret20=None, soxx_ret20=None, soxx_rsi=None, soxx_ma60=None, soxx_dd240=None, xlp_rsi=None,
                   xlp_above60=None, ko_rsi=None, ko_above60=None)
    assert ok({**gone, "raw": "insufficient", "state": "insufficient"})


BAD_VALUES = [
    ("raw_changed", {"raw": "staples"}),
    ("raw_none_when_watch", {"raw": "none"}),
    ("soxx_rsi_120", {"soxx_rsi": 120}),
    ("xlp_rsi_negative", {"xlp_rsi": -1}),
    ("ko_rsi_101", {"ko_rsi": 101}),
    ("rsi_is_a_float", {"soxx_rsi": 75.0}),
    ("return_not_rounded", {"xlp_ret20": 4.96}),
    ("ma60_not_rounded", {"soxx_ma60": 12.34}),
    ("dd240_not_rounded", {"soxx_dd240": -2.25}),
    ("dd240_above_zero", {"soxx_dd240": 0.1}),
    ("dd240_below_minus_100", {"soxx_dd240": -100.1}),
    ("return_at_minus_100", {"xlp_ret20": -100.0}),
    ("return_over_1000", {"soxx_ret20": 1000.1}),
    ("ma60_at_minus_100", {"soxx_ma60": -100.0}),
    ("ma60_over_1000", {"soxx_ma60": 1000.1}),
    ("state_insufficient_raw_not", {"state": "insufficient"}),
    ("raw_insufficient_state_not", {"xlp_ret20": None, "soxx_rsi": 50, "raw": "insufficient"}),
    ("unknown_state", {"state": "rally"}),
    ("unknown_raw", {"raw": "rally"}),
    ("above_is_not_bool", {"xlp_above60": 0}),
    ("return_is_a_string", {"xlp_ret20": "1.0"}),
    ("return_is_nan", {"soxx_ret20": float("nan")}),
    # 插針的兩個鍵（2026-10-29 增補 C6）：範圍 [0, 100] 在一位小數之前比、兩個要一起有或一起沒有、日期是嚴格的 ISO 字串
    ("wick_pct_1e300", {"soxx_wick_pct": 1e300, "soxx_wick_day": "2027-03-29"}),
    ("wick_pct_big_int", {"soxx_wick_pct": 10 ** 400, "soxx_wick_day": "2027-03-29"}),
    ("wick_pct_negative", {"soxx_wick_pct": -0.1, "soxx_wick_day": "2027-03-29"}),
    ("wick_pct_over_100", {"soxx_wick_pct": 100.1, "soxx_wick_day": "2027-03-29"}),
    ("wick_pct_not_rounded", {"soxx_wick_pct": 3.14, "soxx_wick_day": "2027-03-29"}),
    ("wick_pct_is_a_string", {"soxx_wick_pct": "3.1", "soxx_wick_day": "2027-03-29"}),
    ("wick_pct_without_a_day", {"soxx_wick_pct": 3.1}),
    ("wick_day_without_a_pct", {"soxx_wick_day": "2027-03-29"}),
    ("wick_day_is_a_date_object", {"soxx_wick_pct": 3.1, "soxx_wick_day": date(2027, 3, 29)}),
    ("wick_day_compact", {"soxx_wick_pct": 3.1, "soxx_wick_day": "20271027"}),
    ("wick_day_iso_week", {"soxx_wick_pct": 3.1, "soxx_wick_day": "2027-W13-3"}),
    ("wick_day_impossible", {"soxx_wick_pct": 3.1, "soxx_wick_day": "2027-02-30"}),
]


@pytest.mark.parametrize("over", [case[1] for case in BAD_VALUES], ids=[case[0] for case in BAD_VALUES])
def test_rotation_values_ok_rejects_bad_values(over):
    assert not market_calc.rotation_values_ok(good_values(**over))


@pytest.mark.parametrize("junk", [None, [], "x", 1, {"state": "none"}])
def test_rotation_values_ok_never_raises_on_junk(junk):
    assert market_calc.rotation_values_ok(junk) is False
    missing = good_values()
    del missing["ko_rsi"]
    assert market_calc.rotation_values_ok(missing) is False


# ── 插針（SOXX 長上影線；2026-10-29 增補 v3.1）：rot_wick、開高低的解析、偏熱的第三條路（B 案） ──────────────────
# 研判日 DAY＝3/31；往回的交易日是 3/30、3/29、3/25、3/24、3/23（3/26 耶穌受難日休市）：WICK_DAYS[-1 - k] 就是 R−k。
# 價格一律寫 float（開高低只收 float：寫入端寫的是 float，JSON 讀回來也是 float）。
WICK_DAYS = CAL.sessions_ending(DAY, 6)
WICK_BAR = (249.90, 256.00, 249.80, 250.00)  # 上影線 6.00（2.4%）、實體 0.10、下影線 0.10
PLAIN_BAR = (250.00, 250.10, 249.90, 250.00)  # 上下影線一樣長：不是長上影線


def wick_at(k):
    return WICK_DAYS[-1 - k]


def wick_case(bars, official=None, base=250.0):
    """{k: bar} → (開高低 {R−k: bar}, SOXX 的正式收盤)。bar 那天的正式收盤預設等於它自己的收盤（收盤是 float 時）；
    official＝{k: 收盤} 覆寫，None＝那天沒有正式收盤。"""
    ohlc = {wick_at(k): bar for k, bar in bars.items()}
    closes = {day: base for day in SESSIONS}
    for day, bar in ohlc.items():
        if isinstance(bar, tuple) and len(bar) == 4 and type(bar[3]) is float:
            closes[day] = bar[3]
    for k, value in (official or {}).items():
        if value is None:
            closes.pop(wick_at(k))
        else:
            closes[wick_at(k)] = value
    return ohlc, closes


# C1 a–c、j、k＋長腳十字：一根 K 棒（研判日當天、正式收盤＝K 棒收盤）量出來的上影線
WICK_SHAPES = [
    # a. 剛好 2 倍（非整數價格：用 float 算會判成不到 2 倍）；1.9 倍不算
    ("exactly_twice_the_body", (250.02, 250.95, 249.90, 250.33), 0.2),
    ("1_9_times_the_body", (250.02, 250.92, 249.90, 250.33), None),
    # b. 1.74 ÷ 120 剛好 1.45%：half-up 是 1.5（float 會算成 1.4499… → 1.4）；121.73 是 1.4
    ("pct_1_45_rounds_half_up", (119.90, 121.74, 119.80, 120.00), 1.5),
    ("pct_1_44_rounds_down", (119.90, 121.73, 119.80, 120.00), 1.4),
    # c. 十字線有上影線就照記；開高低收都一樣（上影線 0）不記
    ("doji_with_an_upper_wick", (250.00, 251.00, 249.90, 250.00), 0.4),
    ("flat_bar", (250.00, 250.00, 250.00, 250.00), None),
    # 長腳十字（主執行緒加）：上影線 3.70、下影線 4.00 → 不算；同一根把低改成 249.90（下影線 0.10）→ 算
    ("long_legged_doji", (250.00, 254.00, 246.00, 250.30), None),
    ("same_bar_with_a_short_lower_wick", (250.00, 254.00, 249.90, 250.30), 1.5),
    # j. 上限：上影線 ÷ 收盤（還沒四捨五入）>15% 當壞資料；剛好 15.0 收、15.04（四捨五入是 15.0）與 15.05 不收
    ("high_three_times_the_close", (249.90, 750.00, 249.80, 250.00), None),
    ("high_1e308", (249.90, 1e308, 249.80, 250.00), None),
    ("cap_exactly_15_0", (199.90, 230.00, 199.80, 200.00), 15.0),
    ("cap_15_04_is_over", (199.90, 230.08, 199.80, 200.00), None),
    ("cap_15_05_is_over", (199.90, 230.10, 199.80, 200.00), None),
    # k. 型別：字串、int、大整數、bool、NaN、inf、少一個值、高比收盤低 → 那一根當缺漏
    ("strings", ("249.9", "256.0", "249.8", "250.0"), None),
    ("ints", (250, 256, 249, 250), None),
    ("big_int", (249.90, 10 ** 400, 249.80, 250.00), None),
    ("bool", (249.90, True, 249.80, 250.00), None),
    ("nan", (249.90, float("nan"), 249.80, 250.00), None),
    ("inf", (249.90, float("inf"), 249.80, 250.00), None),
    ("three_values", (249.90, 256.00, 250.00), None),
    ("high_below_the_close", (249.00, 249.50, 248.00, 250.00), None),
]


@pytest.mark.parametrize("bar,expected", [case[1:] for case in WICK_SHAPES], ids=[case[0] for case in WICK_SHAPES])
def test_rot_wick_measures_the_upper_wick_of_one_bar(bar, expected):
    ohlc, closes = wick_case({0: bar})
    got = market_calc.rot_wick(ohlc, closes, None, CAL, DAY)
    assert got == ((expected, DAY) if expected is not None else (None, None))
    assert expected is None or type(got[0]) is float


# C1 d、e：交叉檢查（和 _evaluate 同一個 float 公式、0.5%）、沒有正式收盤、隔離區間
WICK_EXCLUSIONS = [
    ("official_0_6_pct_away", {0: 251.50}, None, None),
    ("official_0_4_pct_away", {0: 251.00}, None, 2.4),
    ("no_official_close", {0: None}, None, None),
    ("official_close_is_zero", {0: 0.0}, None, None),
    ("quarantine_on_the_day", {}, (0, 0), None),
    ("quarantine_still_open", {}, (2, None), None),
    ("quarantine_ends_the_session_before", {}, (3, 1), 2.4),
]


@pytest.mark.parametrize("official,quarantine,expected", [case[1:] for case in WICK_EXCLUSIONS],
                         ids=[case[0] for case in WICK_EXCLUSIONS])
def test_rot_wick_needs_a_matching_official_close_and_no_quarantine(official, quarantine, expected):
    ohlc, closes = wick_case({0: WICK_BAR}, official)
    intervals = None
    if quarantine is not None:
        intervals = [(wick_at(quarantine[0]), None if quarantine[1] is None else wick_at(quarantine[1]))]
    got = market_calc.rot_wick(ohlc, closes, intervals, CAL, DAY)
    assert got == ((expected, DAY) if expected is not None else (None, None))


# C1 f：窗口＝研判日往回 3 個交易日（含研判日），錨在研判日：研判日沒有開高低時不改錨到最後一根有資料的日子
WICK_WINDOWS = [
    ("wick_two_sessions_back_counts", {2: WICK_BAR}, 2),
    ("wick_three_sessions_back_does_not", {3: WICK_BAR}, None),
    ("no_bar_on_the_day_and_a_wick_three_back", {4: PLAIN_BAR, 3: WICK_BAR}, None),
    ("wick_on_the_day", {0: WICK_BAR, 1: PLAIN_BAR}, 0),
]


@pytest.mark.parametrize("bars,expected", [case[1:] for case in WICK_WINDOWS], ids=[case[0] for case in WICK_WINDOWS])
def test_rot_wick_looks_at_the_last_three_sessions(bars, expected):
    ohlc, closes = wick_case(bars)
    got = market_calc.rot_wick(ohlc, closes, None, CAL, DAY)
    assert got == ((2.4, wick_at(expected)) if expected is not None else (None, None))


def test_rot_wick_on_a_day_that_is_not_a_session_and_with_a_list_calendar():
    """C1 g：研判日不是交易日（週六、休市日）→ (None, None)；日曆給排好的交易日清單時照樣算。"""
    ohlc, closes = wick_case({0: WICK_BAR, 3: WICK_BAR})
    # 週六：不檢查「窗口最後一天是研判日」的話，窗口 3/31–4/2 會撿到 3/31 的插針
    assert market_calc.rot_wick(ohlc, closes, None, CAL, date(2027, 4, 3)) == (None, None)
    assert market_calc.rot_wick(ohlc, closes, None, CAL, date(2027, 3, 26)) == (None, None)  # 休市（窗口會是 3/23–3/25）
    listed = list(CAL.sessions_ending(DAY, 10))
    assert market_calc.rot_wick(ohlc, closes, None, listed, DAY) == (2.4, DAY)
    assert market_calc.rot_wick(ohlc, closes, None, listed, date(2027, 3, 26)) == (None, None)


def test_rot_wick_takes_the_largest_long_wick_and_the_latest_on_a_tie():
    """C1 h：一樣長取最近那天；只在「長上影線」裡取最大：R−1 上影線 3%，但只有實體的 1.5 倍（不算），R 是 1.6%。"""
    ohlc, closes = wick_case({1: WICK_BAR, 0: WICK_BAR})
    assert market_calc.rot_wick(ohlc, closes, None, CAL, DAY) == (2.4, DAY)
    ohlc, closes = wick_case({1: (102.00, 105.00, 99.90, 100.00), 0: (100.00, 101.80, 99.90, 100.20)}, base=100.0)
    assert market_calc.rot_wick(ohlc, closes, None, CAL, DAY) == (1.6, DAY)


def test_rot_wick_divides_by_the_bars_own_close():
    """C1 i：K 棒收 100.00、正式收盤 100.40（差 0.4%，過得了交叉檢查）、高 101.45 → 1.45% → 1.5（用正式收盤會是 1.4）。"""
    ohlc, closes = wick_case({0: (99.95, 101.45, 99.90, 100.00)}, {0: 100.40}, base=100.0)
    assert market_calc.rot_wick(ohlc, closes, None, CAL, DAY) == (1.5, DAY)


def test_a_close_above_the_wick_high_cancels_the_wick():
    """C1 l（w3 X1）：插針那天之後、到研判日為止（只看 ≤ 研判日）有一天正式收盤高於插針的高，這根就不算。"""
    bar = (250.00, 256.00, 249.00, 250.50)  # w3 反例 1：上影線 5.50（2.2%）、實體 0.50、下影線 1.00
    ohlc, closes = wick_case({1: bar})
    assert market_calc.rot_wick(ohlc, closes, None, CAL, wick_at(1)) == (2.2, wick_at(1))
    closes[DAY] = 258.40  # 隔天收盤站上 256.00
    assert market_calc.rot_wick(ohlc, closes, None, CAL, DAY) == (None, None)
    assert market_calc.rot_wick(ohlc, closes, None, CAL, wick_at(1)) == (2.2, wick_at(1))  # 研判日之後的收盤不影響
    closes[DAY] = 256.00  # 剛好等於高：沒有突破
    assert market_calc.rot_wick(ohlc, closes, None, CAL, DAY) == (2.2, wick_at(1))
    # w3 反例 2：插針那天不在高位，之後兩天的收盤站上插針的高 → 研判日的窗口裡不算
    ohlc, closes = wick_case({2: (238.60, 244.40, 238.00, 239.20)}, {1: 245.90, 0: 249.60})
    assert market_calc.rot_wick(ohlc, closes, None, CAL, wick_at(2)) == (2.2, wick_at(2))
    assert market_calc.rot_wick(ohlc, closes, None, CAL, DAY) == (None, None)


def test_rot_wick_does_not_touch_its_inputs_and_never_raises():
    """C1 m：純函式；壞的輸入（不是 dict、沒有收盤）回 (None, None)，不丟例外。"""
    ohlc, closes = wick_case({1: WICK_BAR, 0: (250.00, 251.00, 249.90, 250.00)})
    intervals = [(wick_at(5), wick_at(4))]
    before = copy.deepcopy((ohlc, closes, intervals))
    assert market_calc.rot_wick(ohlc, closes, intervals, CAL, DAY) == (2.4, wick_at(1))
    assert (ohlc, closes, intervals) == before and list(closes) == list(before[1])
    for junk_ohlc, junk_closes in ((None, closes), ([WICK_BAR], closes), ("x", closes), (ohlc, None), (ohlc, [1.0]),
                                   ({wick_at(0): None}, closes), ({wick_at(0): "250.0"}, closes)):
        assert market_calc.rot_wick(junk_ohlc, junk_closes, None, CAL, DAY) == (None, None)


# C2：開高低的解析（ohlc_bar 一根、ohlc_from_payload 整份 payload；寫入端、publish、rot_wick 共用這一份定義）
OHLC_BARS = [
    ("good", [250.0, 256.0, 249.0, 250.5], (250.0, 256.0, 249.0, 250.5)),
    ("tuple", (250.0, 256.0, 249.0, 250.5), (250.0, 256.0, 249.0, 250.5)),
    ("flat", [250.0, 250.0, 250.0, 250.0], (250.0, 250.0, 250.0, 250.0)),
    ("high_below_the_close", [250.0, 250.4, 249.0, 250.5], None),
    ("low_above_the_open", [250.0, 256.0, 250.2, 250.5], None),
    ("zero", [0.0, 256.0, 249.0, 250.5], None),
    ("negative", [-250.0, 256.0, -249.0, 250.5], None),
    ("an_int", [250, 256.0, 249.0, 250.5], None),
    ("big_int", [250.0, 10 ** 400, 249.0, 250.5], None),
    ("bool", [250.0, True, 249.0, 250.5], None),
    ("string", ["250.0", 256.0, 249.0, 250.5], None),
    ("nan", [250.0, float("nan"), 249.0, 250.5], None),
    ("inf", [250.0, float("inf"), 249.0, 250.5], None),
    ("three_values", [250.0, 256.0, 249.0], None),
    ("five_values", [250.0, 256.0, 249.0, 250.5, 1.0], None),
    ("nested", [[250.0], 256.0, 249.0, 250.5], None),
    ("a_dict", {"o": 250.0, "h": 256.0, "l": 249.0, "c": 250.5}, None),
    ("a_string", "250.0,256.0,249.0,250.5", None),
    ("none", None, None),
]


@pytest.mark.parametrize("values,expected", [case[1:] for case in OHLC_BARS], ids=[case[0] for case in OHLC_BARS])
def test_ohlc_bar_takes_four_positive_floats_in_order(values, expected):
    got = market_calc.ohlc_bar(values)
    assert got == expected
    assert expected is None or (type(got) is tuple and all(type(value) is float for value in got))


_BIG = "1" + "0" * 400
OHLC_JUNK = [
    ("null", "null"), ("a_list", "[]"), ("a_string", '"x"'), ("a_number", "1"), ("no_bars", '{"nobars": 1}'),
    ("bars_is_a_list", '{"bars": []}'), ("bars_is_a_string", '{"bars": "x"}'),
    ("value_is_a_string", '{"bars": {"2026-10-27": "y"}}'),
    ("key_is_not_a_date", '{"bars": {"x": [250.0, 256.0, 249.0, 250.5]}}'),
    ("two_values", '{"bars": {"2026-10-27": [1.0, 2.0]}}'),
    ("three_values", '{"bars": {"2026-10-27": [250.0, 256.0, 249.0]}}'),
    ("five_values", '{"bars": {"2026-10-27": [250.0, 256.0, 249.0, 250.5, 1.0]}}'),
    ("string_values", '{"bars": {"2026-10-27": ["250.0", "256.0", "249.0", "250.5"]}}'),
    ("bool_value", '{"bars": {"2026-10-27": [250.0, true, 249.0, 250.5]}}'),
    ("nested_value", '{"bars": {"2026-10-27": [[250.0], 256.0, 249.0, 250.5]}}'),
    ("dict_value", '{"bars": {"2026-10-27": {"o": 250.0}}}'),
    ("int_values", '{"bars": {"2026-10-27": [250, 256, 249, 250]}}'),
    ("nan_token", '{"bars": {"2026-10-27": [250.0, NaN, 249.0, 250.5]}}'),
    ("inf_1e400", '{"bars": {"2026-10-27": [250.0, 1e400, 249.0, 250.5]}}'),
    ("big_int_401_digits", '{"bars": {"2026-10-27": [250.0, ' + _BIG + ', 249.0, 250.5]}}'),
    ("compact_date", '{"bars": {"20261027": [250.0, 256.0, 249.0, 250.5]}}'),
    ("iso_week_date", '{"bars": {"2026-W44-2": [250.0, 256.0, 249.0, 250.5]}}'),
    ("full_width_digits", '{"bars": {"２０２６-10-27": [250.0, 256.0, 249.0, 250.5]}}'),
    ("impossible_date", '{"bars": {"2026-02-30": [250.0, 256.0, 249.0, 250.5]}}'),
    ("date_with_a_time", '{"bars": {"2026-10-27T00:00:00": [250.0, 256.0, 249.0, 250.5]}}'),
    ("negative_values", '{"bars": {"2026-10-27": [-250.0, 256.0, -249.0, 250.5]}}'),
]


@pytest.mark.parametrize("text", [case[1] for case in OHLC_JUNK], ids=[case[0] for case in OHLC_JUNK])
def test_ohlc_payload_junk_is_dropped_without_raising(text):
    payload = json.loads(text)
    assert market_calc.ohlc_from_payload(payload) == {}
    assert market_calc.ohlc_from_payload(payload, upto=date(2026, 10, 30)) == {}


def test_ohlc_from_payload_keeps_the_good_bars_drops_later_days_and_round_trips():
    payload = {"bars": {
        "2027-03-29": [250.0, 256.0, 249.9, 250.0], "2027-03-31": [250.0, 251.0, 249.9, 250.0],
        "2027-03-30": [250, 256, 249, 250], "x": [250.0, 256.0, 249.9, 250.0], "2027-04-01": [250.0, 252.0, 249.9, 250.0],
    }}
    before = copy.deepcopy(payload)
    good = {date(2027, 3, 29): (250.0, 256.0, 249.9, 250.0), date(2027, 3, 31): (250.0, 251.0, 249.9, 250.0)}
    assert market_calc.ohlc_from_payload(payload, upto=DAY) == good
    assert market_calc.ohlc_from_payload(payload, upto=date(2027, 3, 30)) == {date(2027, 3, 29): good[date(2027, 3, 29)]}
    everything = market_calc.ohlc_from_payload(payload)
    assert everything == {**good, date(2027, 4, 1): (250.0, 252.0, 249.9, 250.0)}
    assert payload == before  # 不修改傳進來的 payload（publish 傳的是共用快取裡的那一份）
    written = {"bars": {day.isoformat(): list(bar) for day, bar in everything.items()}}  # 寫入端的形狀
    assert market_calc.ohlc_from_payload(json.loads(json.dumps(written))) == everything


# C3：rot_inputs／rotation_values 帶開高低
def wick_world():
    """真收盤（C3c）：SOXX 緩漲、一直在 240 日高點附近（RSI 約 50～60、離季線 +1～+2%），XLP 一直在季線下（必需消費偏弱），
    KO 小波動；20 日報酬差都在 ±5 個百分點內。沒有插針時 5 個回看日都是「沒有明顯方向」。"""
    def series(base, drift, amp, period):
        return {day: round(base * (1 + drift * i) * (1 + amp * math.sin(i / period)), 2) for i, day in enumerate(SESSIONS)}

    return {"XLP": series(80.0, -0.0005, 0.005, 3.0), "SOXX": series(250.0, 0.0008, 0.01, 2.0),
            "KO": series(68.0, 0.0002, 0.01, 4.0)}


def test_rot_inputs_adds_the_wick_as_a_rounded_float_and_an_iso_day():
    """C3a：values 多兩個鍵：soxx_wick_pct 是一位小數的 float、soxx_wick_day 是 ISO 字串；JSON 來回一樣、過 rotation_values_ok。"""
    closes = rot_closes()
    c = closes["SOXX"][DAY]
    bar = (round(c - 0.1, 4), round(c * 1.031, 4), round(c - 0.2, 4), c)
    plain = market_calc.rot_inputs(closes, {}, CAL, DAY)
    got = market_calc.rot_inputs(closes, {}, CAL, DAY, ohlc={DAY: bar})
    expected = float((Decimal(repr(bar[1])) - Decimal(repr(c))) / Decimal(repr(c)) * 100)
    assert got["soxx_wick_pct"] == float(half_up(expected, 1)) == 3.1 and type(got["soxx_wick_pct"]) is float
    assert got["soxx_wick_day"] == "2027-03-31"
    assert plain["soxx_wick_pct"] is None and plain["soxx_wick_day"] is None
    assert {k: v for k, v in got.items() if not k.startswith("soxx_wick")} == {
        k: v for k, v in plain.items() if not k.startswith("soxx_wick")}
    assert json.loads(json.dumps(got)) == got
    values = market_calc.rotation_values(closes, {}, CAL, DAY, ohlc={DAY: bar})
    assert values["soxx_wick_pct"] == 3.1 and values["soxx_wick_day"] == "2027-03-31"
    assert json.loads(json.dumps(values)) == values and market_calc.rotation_values_ok(values)


def test_a_wick_three_sessions_back_is_held_on_the_research_day():
    """C3c（端到端，真收盤）：插針在 R−3、必需消費一直偏弱 → 原始狀態 [none, ws, ws, ws, none]：研判日 raw＝none、
    state＝watch_staples（沿用）。只把開高低傳給研判日的話，state 會是 none。"""
    closes = wick_world()
    wick_day = wick_at(3)
    close = closes["SOXX"][wick_day]
    low_open = round(close * 0.998, 2)
    ohlc = {wick_day: (low_open, round(close * 1.03, 2), round(low_open * 0.999, 2), close)}
    plain = market_calc.rotation_values(closes, {}, CAL, DAY)
    assert plain["raw"] == plain["state"] == "none"
    raws = [market_calc.rot_classify(market_calc.rot_inputs(closes, {}, CAL, day, ohlc=ohlc)) for day in WICK_DAYS[-5:]]
    assert raws == ["none", "watch_staples", "watch_staples", "watch_staples", "none"]
    values = market_calc.rotation_values(closes, {}, CAL, DAY, ohlc=ohlc)
    assert values["raw"] == "none" and values["state"] == "watch_staples"
    assert values["soxx_wick_pct"] is None and values["soxx_wick_day"] is None  # 研判日的窗口已經沒有 R−3
    assert market_calc.rotation_values_ok(values)
    held = market_calc.rot_inputs(closes, {}, CAL, wick_at(1), ohlc=ohlc)
    assert (held["soxx_wick_pct"], held["soxx_wick_day"]) == (3.0, wick_day.isoformat())


# C4：rot_wick_hot（只回 True／False：缺值、舊的 values 都是 False）
@pytest.mark.parametrize("pct,dd240,expected", [
    (1.5, -5.0, True), (15.0, 0.0, True), (3.1, -2.0, True),
    (1.4, -2.0, False), (3.1, -5.1, False), (None, -2.0, False), (3.1, None, False), (None, None, False),
])
def test_rot_wick_hot(pct, dd240, expected):
    values = neutral(soxx_wick_pct=pct, soxx_wick_day=None if pct is None else "2027-03-29", soxx_dd240=dd240)
    before = dict(values)
    assert market_calc.rot_wick_hot(values) is expected
    assert values == before


def test_old_values_without_the_wick_keys_are_not_hot_and_classify_like_v2():
    old = {key: value for key, value in neutral(soxx_dd240=-2.0, xlp_above60=False).items() if not key.startswith("soxx_wick")}
    assert market_calc.rot_wick_hot(old) is False
    assert market_calc.rot_classify(old) == "none"
    assert market_calc.rot_classify({**old, "soxx_rsi": 72}) == "watch_staples"


# C5：分類（B 案：插針是 SOXX 偏熱的第三條路，和 RSI≥70 走同一套三值邏輯）
SWAP_CASES = [case for case in CLASSIFY_CASES if (neutral(**case[1])["soxx_rsi"] or 0) >= 70]
SWAP_WICK = {"soxx_rsi": 65, "soxx_dd240": -2.0, "soxx_wick_pct": 3.1, "soxx_wick_day": "2027-03-29"}


@pytest.mark.parametrize("over,expected", [case[1:] for case in SWAP_CASES], ids=[case[0] for case in SWAP_CASES])
def test_a_wick_is_hot_exactly_like_rsi_70(over, expected):
    """C5a 換觸發條件：真值表裡 SOXX RSI≥70 的案例，RSI 換成 65、改由插針（3.1%、離 240 日高點 −2.0）讓 SOXX 偏熱，結果一樣。"""
    values = neutral(**{**over, **SWAP_WICK})
    assert market_calc.rot_wick_hot(values) is True
    assert market_calc.rot_classify(values) == expected


def test_the_swap_table_has_all_fifteen_rsi_cases():
    assert len(SWAP_CASES) == 15  # 有人刪了真值表的案例，這裡會先紅


def test_sb_cold_soxx_near_the_high_with_a_wick_has_no_clear_direction():
    """C5b（w3 Sb）：SOXX RSI 28（偏冷）又在高點 5% 內插針（偏熱）→ 同時偏熱又偏冷 → 照趨勢 T（none）；v2 是 watch_semis。"""
    values = neutral(soxx_rsi=28, soxx_ma60=-3.0, soxx_dd240=-4.5, soxx_wick_pct=1.8, soxx_wick_day="2027-03-29",
                     ko_above60=False, ko_rsi=45, xlp_rsi=72)
    assert market_calc.rot_classify(values) == "none"


def test_andrews_example_e0_is_watch_staples_and_a_wick_alone_follows_the_trend():
    """C5c（E0，Andrew 的例子）：KO 低位盤整（季線下、RSI 45）、SOXX 高位插針（RSI 65、離季線 +6.0、離高點 −2.0、2.4%）
    → watch_staples（v2 是 none）。插針成立但必需消費不弱 → 照趨勢 T。"""
    values = neutral(soxx_rsi=65, soxx_ma60=6.0, soxx_dd240=-2.0, soxx_wick_pct=2.4, soxx_wick_day="2027-03-29",
                     ko_above60=False, ko_rsi=45)
    assert market_calc.rot_classify(values) == "watch_staples"
    assert market_calc.rot_classify({**values, "ko_above60": True, "xlp_ret20": 6.0}) == "staples"
    assert market_calc.rot_classify({**values, "ko_above60": True}) == "none"


def test_a_wick_that_is_not_hot_changes_nothing():
    """C5e（可選）：插針不成立（太短、不在高點附近、沒有）時，整張真值表的結果和不帶插針一樣。"""
    for name, over, expected in CLASSIFY_CASES:
        base = neutral(**over)
        variants = [(None, None), (0.0, "2027-03-29"), (1.4, "2027-03-29")]
        dd240 = base["soxx_dd240"]
        if dd240 is None or dd240 < -5.0:
            variants.append((15.0, "2027-03-29"))  # 離 240 日高點 <−5% 或算不出來：多長都不算偏熱
        for pct, day in variants:
            values = {**base, "soxx_wick_pct": pct, "soxx_wick_day": day}
            assert market_calc.rot_wick_hot(values) is False, (name, pct)
            assert market_calc.rot_classify(values) == expected, (name, pct)


def test_rotation_values_ok_recomputes_the_wick_and_takes_the_range_ends():
    """C6：插針成立時 raw 要是重算的 watch_staples（寫 none 就不過）；範圍 [0, 100] 的兩端可以；1e300、大整數不丟例外。"""
    ok = market_calc.rotation_values_ok
    hot = neutral(soxx_rsi=65, soxx_dd240=-2.0, xlp_above60=False, soxx_wick_pct=3.1, soxx_wick_day="2027-03-29")
    assert ok({**hot, "raw": "watch_staples", "state": "watch_staples"})
    assert ok({**hot, "raw": "watch_staples", "state": "none"})  # 狀態沿用前幾天的
    assert not ok({**hot, "raw": "none", "state": "none"})  # 重算對不上
    assert ok(good_values(soxx_wick_pct=0.0, soxx_wick_day="2027-03-29"))
    assert ok(good_values(soxx_wick_pct=100.0, soxx_wick_day="2027-03-29"))
    for pct in (1e300, -1e300, 10 ** 400, 10 ** 20):
        assert ok(good_values(soxx_wick_pct=pct, soxx_wick_day="2027-03-29")) is False


def test_wick_starting_thresholds_and_the_cross_check_matches_the_pipeline_guard():
    """C8：起始值，以及 rot_wick 的交叉檢查和 pipeline 的覆寫守門同一個門檻（calc 不能 import pipeline，用測試綁住）。"""
    mc = market_calc
    assert (mc.ROT_WICK_SESSIONS, mc.ROT_WICK_BODY_RATIO, mc.ROT_WICK_MIN_PCT, mc.ROT_WICK_MAX_PCT,
            mc.ROT_WICK_NEAR_HIGH_PCT) == (3, Decimal("2"), Decimal("1.5"), Decimal("15.0"), Decimal("-5.0"))
    assert all(type(value) is Decimal for value in (mc.ROT_WICK_BODY_RATIO, mc.ROT_WICK_MIN_PCT, mc.ROT_WICK_MAX_PCT,
                                                     mc.ROT_WICK_NEAR_HIGH_PCT))
    assert mc.ROT_OHLC_MATCH == pipe.GUARD_DIFF == 0.005
    assert mc.ROT_WICK_NEAR_HIGH_PCT < mc.ROT_NEAR_HIGH_PCT  # 比「近高點且拉很開」那條路的 −3 寬


# ═════════════════════════════════════════════════════════════════════════════
# publish：us.rot.XLP／us.rot.KO／us.rot.state（test_market_publish 的合成 world：XLP、KO 都有 290 個交易日的收盤）
# ═════════════════════════════════════════════════════════════════════════════
ROT_ROWS = ("us.rot.XLP", "us.rot.KO", "us.rot.state")


def published(world_):
    outcome = world_.publish()
    assert outcome["kind"] == "ok", outcome
    return outcome, world_.snapshot()["rows"]


def sql(world_, statement, *args):
    with world_.store.transaction() as conn:
        conn.execute(statement, args)


def store_closes(world_, symbols=("XLP", "SOXX", "KO"), upto=PUB_D):
    """和 publish 同一份輸入：≤ D 的全部列（含 CBOE 暫定列）。"""
    return {sym: {row[0]: row[1] for row in world_.store.closes("us", sym) if row[0] <= upto} for sym in symbols}


def test_the_three_rows_are_ok_and_the_etf_style_rows_match_the_etf_builder(world, monkeypatch):
    recorded = []
    original = market_publish._Ctx.record_facts

    def spy(self, facts, data_date, stale_after):
        recorded.extend(facts)
        return original(self, facts, data_date, stale_after)

    monkeypatch.setattr(market_publish._Ctx, "record_facts", spy)
    outcome, rows = published(world)
    assert "row_errors" not in outcome, outcome
    for row_id in ROT_ROWS:
        row = rows[row_id]
        assert row["state"] == "ok" and market_calc.row_ok(row_id, row), row_id
        assert row["data_date"] == PUB_D.isoformat() and row["source"] == "Nasdaq", row_id
        assert row["basis"] == market_calc.TEXT_US_NO_DIVIDEND and row["stale_after"] == rows["us.etf.SOXX"]["stale_after"]
    assert rows["us.rot.XLP"]["label"] == rows["us.rot.KO"]["label"] == "必需消費類股"
    assert rows["us.rot.state"]["label"] == "資金輪動"
    # 和 ETF 列同一個算法（_us_etf 只用列 id 切出代號）：XLP、KO 的 values 一模一樣
    ctx = market_publish._Ctx(world.store, world.paths, PUB_NOW)
    for sym in ("XLP", "KO"):
        assert rows[f"us.rot.{sym}"]["values"] == json.loads(json.dumps(market_publish._us_etf(ctx, f"us.etf.{sym}").values))
    values = rows["us.rot.state"]["values"]
    assert market_calc.rotation_values_ok(values) and values["state"] != "insufficient"
    assert values == market_calc.rotation_values(store_closes(world), {}, ctx.trading("us"), PUB_D)
    assert not [key for key in recorded if key.split(".")[0] in ("XLP", "KO")]  # 新列都不寫 facts（不進一句話與矛盾）


def test_too_little_history_is_an_ok_row_that_says_insufficient(world):
    sql(world, "DELETE FROM closes WHERE market = 'us' AND symbol = 'XLP' AND date < ?", US_DAYS[-10].isoformat())
    outcome, rows = published(world)
    row = rows["us.rot.state"]
    assert row["state"] == "ok" and row["data_date"] == PUB_D.isoformat() and "row_errors" not in outcome
    assert row["values"]["state"] == row["values"]["raw"] == "insufficient"
    assert row["values"]["xlp_ret20"] is None and row["values"]["soxx_ret20"] is None
    assert rows["us.rot.XLP"]["state"] == "ok"  # 收盤照樣顯示


def test_no_official_xlp_or_soxx_close_at_all_is_the_never_case(world):
    sql(world, "DELETE FROM closes WHERE market = 'us' AND symbol = 'XLP'")
    outcome, rows = published(world)
    assert rows["us.rot.state"]["state"] == "never" and rows["us.rot.XLP"]["state"] == "never"
    assert rows["us.rot.state"]["label"] == "資金輪動" and rows["us.rot.KO"]["state"] == "ok"
    assert "row_errors" not in outcome and outcome["never_reasons"]["missing"] >= 2
    root = world.root / "soxx_only_provisional"
    root.mkdir()
    other = type(world)(root)
    try:
        sql(other, "UPDATE closes SET provisional = 1, source = 'cboe' WHERE market = 'us' AND symbol = 'SOXX'")
        _outcome, rows = published(other)
        assert rows["us.rot.state"]["state"] == "never"  # SOXX 只有暫定值：沒有正式收盤
        assert rows["us.etf.SOXX"]["state"] == "ok" and rows["us.rot.XLP"]["state"] == "ok"
    finally:
        other.close()


def test_bad_values_only_break_their_own_row(world, monkeypatch):
    real = market_calc.rotation_values

    def broken(*args, **kwargs):  # publish 會帶 ohlc＝（2026-10-29 增補）
        values = real(*args, **kwargs)
        values["raw"] = "semis" if values["raw"] == "staples" else "staples"  # 和重算的原始狀態對不上
        return values

    monkeypatch.setattr(market_calc, "rotation_values", broken)
    outcome, rows = published(world)
    assert rows["us.rot.state"]["state"] == "never"
    assert outcome["row_errors"] == {"us.rot.state": "bad:rot_values"}
    assert all(rows[row_id]["state"] == "ok" for row_id in ("us.rot.XLP", "us.rot.KO", "us.etf.SOXX", "oneliner.us"))


def test_a_provisional_soxx_close_moves_the_research_day_back(world):
    _set_close(world, "SOXX", 255.0, provisional=True)  # D 只有 CBOE 暫定值
    outcome, rows = published(world)
    assert rows["us.etf.SOXX"]["data_date"] == PUB_D.isoformat()
    assert rows["us.etf.SOXX"]["source"] == market_calc.SOURCE_CBOE_FALLBACK
    state = rows["us.rot.state"]
    assert state["state"] == "ok" and state["data_date"] == US_DAYS[-2].isoformat()
    assert rows["us.rot.XLP"]["data_date"] == PUB_D.isoformat()  # XLP 自己那一列照常是 D
    ctx = market_publish._Ctx(world.store, world.paths, PUB_NOW)
    assert state["values"] == market_calc.rotation_values(store_closes(world), {}, ctx.trading("us"), US_DAYS[-2])


def test_days_before_the_research_day_use_the_same_rows_as_the_soxx_etf_row(world):
    """R 那天要正式收盤；R 之前的日子和 us.etf.SOXX 同一份列（含 CBOE 暫定列），研判的 SOXX 數字和 SOXX 那一列一致。"""
    hole = US_DAYS[-4]
    sql(world, "DELETE FROM closes WHERE market = 'us' AND symbol = 'SOXX' AND date = ?", hole.isoformat())
    world.store.upsert_closes("us", "SOXX", [(hole, 250.0, 1_000_000)], "cboe", provisional=True)
    _outcome, rows = published(world)
    values, etf = rows["us.rot.state"]["values"], rows["us.etf.SOXX"]["values"]
    assert rows["us.rot.state"]["data_date"] == PUB_D.isoformat()
    assert values["soxx_rsi"] == int(half_up(etf["rsi14"], 0))
    assert values["soxx_ma60"] == float(half_up(etf["ma_dist"]["60"], 1))
    assert values["soxx_dd240"] == float(half_up(etf["dd240_pct"], 1))


def test_ko_without_the_research_day_is_unknown_and_its_row_shows_the_older_day(world):
    sql(world, "DELETE FROM closes WHERE market = 'us' AND symbol = 'KO' AND date = ?", PUB_D.isoformat())
    _outcome, rows = published(world)
    assert rows["us.rot.KO"]["state"] == "ok" and rows["us.rot.KO"]["data_date"] == US_DAYS[-2].isoformat()
    state = rows["us.rot.state"]
    assert state["data_date"] == PUB_D.isoformat()
    assert state["values"]["ko_rsi"] is None and state["values"]["ko_above60"] is None


def test_the_research_day_must_be_a_session(world):
    """R 那天日曆覆寫成休市（SPY 沒有那一列）、XLP 與 SOXX 卻有列：往前找下一個是交易日的共同正式日，窗口不錯位。"""
    closed = US_DAYS[-2]
    sql(world, "DELETE FROM closes WHERE market = 'us' AND symbol = 'XLP' AND date = ?", PUB_D.isoformat())
    sql(world, "DELETE FROM closes WHERE market = 'us' AND symbol = 'SPY' AND date = ?", closed.isoformat())
    world.store.set_calendar_override("us", closed, "closed", "market_info")
    _outcome, rows = published(world)
    state = rows["us.rot.state"]
    assert state["state"] == "ok" and state["data_date"] == US_DAYS[-3].isoformat()
    ctx = market_publish._Ctx(world.store, world.paths, PUB_NOW)
    assert ctx.trading("us").sessions_ending(closed, 1) == [US_DAYS[-3]]  # 那天真的不是交易日
    assert state["values"] == market_calc.rotation_values(store_closes(world), {}, ctx.trading("us"), US_DAYS[-3])


def test_the_shared_caches_are_not_modified(world):
    """us.etf.SOXX、圖、矛盾句都用 ctx.shared 快取裡的同一個 list／dict：組 us.rot.state 不能就地改它們。開高低的 obs 也是快取裡
    的同一份（2026-10-29 增補 B5）：解析不能把壞的鍵洗掉、也不能把 list 換成 tuple。"""
    world.store.add_quarantine("us", "SOXX", US_DAYS[5], US_DAYS[6], "split_pending")
    world.store.add_quarantine("us", "XLP", US_DAYS[8], US_DAYS[9], "conflict")
    world.store.put_obs("us.ohlc.SOXX", {"bars": {
        PUB_D.isoformat(): [250.0, 251.0, 249.9, 250.0], US_DAYS[-2].isoformat(): [250, 256, 249, 250], "x": [1.0, 2.0, 1.0, 1.5],
    }}, data_date=PUB_D.isoformat())
    ctx = market_publish._Ctx(world.store, world.paths, PUB_NOW)
    for row_id in ("us.etf.SOXX", "us.rot.XLP", "us.rot.KO"):
        assert market_publish._build_row(ctx, row_id)["state"] == "ok", row_id
    assert ctx.obs("us.ohlc.SOXX") is not None  # 先載進共用快取
    keys = ("closes:us:SOXX", "closes:us:XLP", "closes:us:KO", "quarantine:us", "obs:us.ohlc.SOXX")
    before = {key: copy.deepcopy(ctx._cache[key]) for key in keys}
    assert ctx._cache["quarantine:us"] and set(ctx._cache["quarantine:us"]) == {"SOXX", "XLP"}
    row = market_publish._build_row(ctx, "us.rot.state")
    assert row["state"] == "ok" and not ctx.row_errors
    assert {key: ctx._cache[key] for key in keys} == before


def test_the_labels_put_the_exact_row_before_the_prefix():
    labels = list(market_publish._LABELS)
    assert market_publish._label_for("us.rot.state") == "資金輪動"
    assert market_publish._label_for("us.rot.XLP") == market_publish._label_for("us.rot.KO") == "必需消費類股"
    # _label_for 照 dict 的順序取第一個符合的：精確的 us.rot.state 要在前綴 us.rot. 前面；兩個都放在 us.etf. 旁邊
    assert labels[labels.index("us.etf.") + 1:labels.index("us.etf.") + 3] == ["us.rot.state", "us.rot."]


# ── 插針（2026-10-29 增補）：publish 讀 us.ohlc.SOXX，交叉檢查在 rot_wick 裡做、只看 ≤R ──────────────────────────
# SOXX 的 D 收 294.0（離 240 日高點 −1.8%、RSI 52：v2 的兩條路都不偏熱）、XLP 的 D 收 85.0（跌到季線下）：沒有插針時研判日的
# 原始狀態是 semis；D 有一根 3.1% 的長上影線時是 watch_staples。
WICK_SOXX_D, WICK_XLP_D = 294.0, 85.0
WICK_D_BAR = [293.71, 303.11, 293.42, 294.0]  # 上影線 9.11（3.1%）、實體 0.29、下影線 0.29


def wick_world_ready(world_):
    _set_close(world_, "SOXX", WICK_SOXX_D)
    _set_close(world_, "XLP", WICK_XLP_D)


def test_a_wick_on_the_research_day_reaches_the_values(world):
    """B1：寫一份 D 那天有插針的 obs、XLP 偏弱 → values 帶 pct 和 day（ISO），raw＝watch_staples；和 calc 直接算的一樣。"""
    wick_world_ready(world)
    _outcome, rows = published(world)
    plain = rows["us.rot.state"]["values"]
    assert plain["raw"] == "semis" and plain["soxx_wick_pct"] is None and plain["soxx_wick_day"] is None
    world.store.put_obs("us.ohlc.SOXX", {"bars": {PUB_D.isoformat(): WICK_D_BAR}}, data_date=PUB_D.isoformat())
    outcome, rows = published(world)
    row = rows["us.rot.state"]
    assert row["state"] == "ok" and row["data_date"] == PUB_D.isoformat() and "row_errors" not in outcome
    values = row["values"]
    assert (values["soxx_wick_pct"], values["soxx_wick_day"]) == (3.1, PUB_D.isoformat())
    assert values["raw"] == "watch_staples" and values["state"] == "none"  # 只有 D 一天：還沒連續 2 天
    ctx = market_publish._Ctx(world.store, world.paths, PUB_NOW)
    assert values == market_calc.rotation_values(store_closes(world), {}, ctx.trading("us"), PUB_D,
                                                 ohlc={PUB_D: tuple(WICK_D_BAR)})


@pytest.mark.parametrize("payload", [
    None,
    {"bars": "x"},
    {"bars": {"2026-10-16": ["293.71", "303.11", "293.42", "294.0"]}},
    {"bars": {"2026-10-16": [-293.71, 303.11, 293.42, 294.0]}},
    {"bars": {"2026-13-45": [293.71, 303.11, 293.42, 294.0]}},
    {"bars": {"20261016": [293.71, 303.11, 293.42, 294.0]}},
    {"bars": {"2026-10-16": [293.71, 10 ** 400, 293.42, 294.0]}},
], ids=["missing", "bars_is_a_string", "strings", "negative", "bad_date", "compact_date", "big_int"])
def test_a_missing_or_junk_ohlc_obs_leaves_the_row_ok_without_a_wick(world, payload):
    """B2：obs 不存在或內容是壞的 → 這一列照常 ok、wick 是 None、研判和沒有插針時一樣。"""
    wick_world_ready(world)
    if payload is not None:
        world.store.put_obs("us.ohlc.SOXX", payload, data_date=None)
    outcome, rows = published(world)
    row = rows["us.rot.state"]
    assert row["state"] == "ok" and "row_errors" not in outcome and "shared_errors" not in outcome, outcome
    assert row["values"]["soxx_wick_pct"] is None and row["values"]["soxx_wick_day"] is None
    assert row["values"]["raw"] == "semis"


def test_a_failing_ohlc_read_leaves_the_row_ok_and_shows_in_shared_errors(world, monkeypatch):
    """B3：store.get_obs 對這個鍵丟例外 → ctx.obs 變成 _Unavailable（shared_errors 記類別名）→ 接住，這一列照常、沒有插針。"""
    wick_world_ready(world)
    world.store.put_obs("us.ohlc.SOXX", {"bars": {PUB_D.isoformat(): WICK_D_BAR}}, data_date=PUB_D.isoformat())
    real = world.store.get_obs

    def get_obs(obs_id):
        if obs_id == "us.ohlc.SOXX":
            raise sqlite3.OperationalError("disk I/O error")
        return real(obs_id)

    monkeypatch.setattr(world.store, "get_obs", get_obs)
    outcome, rows = published(world)
    row = rows["us.rot.state"]
    assert row["state"] == "ok" and "row_errors" not in outcome
    assert row["values"]["soxx_wick_pct"] is None and row["values"]["raw"] == "semis"
    assert outcome["shared_errors"] == {"obs:us.ohlc.SOXX": "unexpected:OperationalError"}


def test_a_provisional_close_after_the_research_day_does_not_cancel_its_wick(world):
    """B4：SOXX 的 D 只有 CBOE 暫定收盤（R 退到 10/15），而且高於 R 那根插針的高 → R 的插針照算：突破失效只看 ≤R。"""
    day = US_DAYS[-2]
    close = store_closes(world, ("SOXX",))["SOXX"][day]
    bar = [round(close - 0.3, 4), round(close * 1.031, 4), round(close - 0.6, 4), close]
    world.store.put_obs("us.ohlc.SOXX", {"bars": {day.isoformat(): bar}}, data_date=day.isoformat())
    _set_close(world, "SOXX", round(bar[1] * 1.05, 2), provisional=True)
    _outcome, rows = published(world)
    state = rows["us.rot.state"]
    assert state["state"] == "ok" and state["data_date"] == day.isoformat()
    assert (state["values"]["soxx_wick_pct"], state["values"]["soxx_wick_day"]) == (3.1, day.isoformat())


def test_publish_reads_the_key_the_pipeline_writes():
    """P12：publish 不 import pipeline，鍵用測試綁住。"""
    assert market_publish.OBS_US_OHLC.format("SOXX") == pipe.OBS_OHLC["SOXX"] == "us.ohlc.SOXX"
    assert pipe.OHLC_SYMBOLS == ("SOXX",) and pipe.OHLC_KEEP_SESSIONS == 30


# ═════════════════════════════════════════════════════════════════════════════
# dashboard：美股卡的三列（XLP、KO、資金輪動）與 footer
# make_snapshot：標題日 10/16（VOO、QQQ），SOXX 與研判日 10/15；預設研判是「半導體偏熱、必需消費偏弱」
# ═════════════════════════════════════════════════════════════════════════════
US_HEAD = "美股（2/5）"
ROT_XLP_DEFAULT = "必需消費（XLP）80.5 -0.40% 在月線下 RSI 38"
ROT_KO_DEFAULT = "可口可樂（KO）68.4 +0.10% 在月線上 RSI 55"
FIRST_FIVE = [
    ("標普500（VOO）612.34 +0.52% 在月線上 RSI 55", "#1E5AA8"),
    ("那斯達克100（QQQ）540.12 -0.31% 在月線下 RSI 55", "#B7791F"),
    ("半導體（SOXX）245.6 +1.20% 在月線上（10/15）", "#1E5AA8"),
    ("成分股站上月線：VOO 62%、QQQ 55%、SOXX 資料不足", "#555555"),
    ("量能：VOO 量偏少、QQQ 量偏少、SOXX 量偏少", "#555555"),
]
WATCH_NOTE = "資金輪動只看價格：SOXX RSI 74、離季線 +12.3%、離 240 日高點 -2.1%；XLP RSI 38、在季線下；KO RSI 55、在季線上"
HELD = "狀態要連續 2 個交易日都一樣才會換"


def us_bubble(md_, snap, now=DASH_NOW):
    return md_._card_us(md_._Ctx(snap, now), US_HEAD)


def us_body(md_, snap, now=DASH_NOW):
    return [(node["text"], node.get("color")) for node in us_bubble(md_, snap, now)["body"]["contents"]]


def us_notes(md_, snap, now=DASH_NOW):
    footer = texts(us_bubble(md_, snap, now)["footer"])
    return footer[:footer.index(DISCLAIMER)]


def set_rot(snap, values, *, state=None, day=None):
    """研判那一列換成這組指標：raw 用 rot_classify 從同一組數字算，state 預設等於 raw（給了就是「沿用前幾天的」）。"""
    row = snap["rows"]["us.rot.state"]
    raw = market_calc.rot_classify(values)
    row["values"] = {**values, "raw": raw, "state": state or raw}
    if day is not None:
        row["data_date"] = day.isoformat()
    return row["values"]


def passes_every_gate(text):
    import market_dashboard

    assert not market_dashboard.has_forbidden(text), text
    for now in ALL_NOWS:
        result = output_validator.validate_outbound_text(text, now=datetime.fromtimestamp(now, TPE))
        assert result.ok and result.text == text, (text, now, result.reason)


def test_the_fixtures_carry_valid_rotation_rows(md):
    """fixture 的研判值一定要過 rotation_values_ok：不然卡上悄悄變成「暫缺」，下面的測試就什麼也沒測到。"""
    for snap in (make_snapshot(DASH_NOW), worst_snapshot(DASH_NOW)):
        values = snap["rows"]["us.rot.state"]["values"]
        assert market_calc.rotation_values_ok(values) and values["raw"] == market_calc.rot_classify(values)
        assert "暫缺" not in us_body(md, snap)[7][0]
    worst = worst_snapshot(DASH_NOW)["rows"]["us.rot.state"]["values"]
    assert worst["state"] == "watch_staples" and worst["raw"] == "semis"  # footer 最長：watch＋KO＋插針＋「連續 2 個交易日」
    assert market_calc.rot_wick_hot(worst) and worst["soxx_wick_pct"] == 100.0  # 插針句也在（2026-10-29 增補 D5）


def test_the_us_card_shows_every_row_it_builds_and_the_one_liner_stays_last(md, monkeypatch):
    built = []
    original = md._summary_bubble

    def spy(title, rows, **kwargs):
        built.append((list(rows), kwargs.get("max_rows", "default")))
        return original(title, rows, **kwargs)

    monkeypatch.setattr(md, "_summary_bubble", spy)
    snap = make_snapshot(DASH_NOW)
    body = us_body(md, snap)
    (rows, max_rows), = built
    assert body == [(text, color) for text, color in rows] and len(body) == 9  # 一列都沒被截掉
    assert max_rows is None  # 美股卡不截斷；太大由 _guarded_bubbles(max_bytes) 換短卡、看得到
    assert body[5:] == [(ROT_XLP_DEFAULT, md.ORANGE), (ROT_KO_DEFAULT, md.BLUE),
                        ("資金輪動 10/15：半導體偏熱、必需消費偏弱", md.ORANGE), (ONELINER_US, md.GRAY)]
    sentence = market_calc.CONTRA_TEXT["up_low_volume"].format(subject=market_calc.SUBJECT_NAME["VOO"])
    snap["rows"]["contra.us"]["values"] = {"codes": ["up_low_volume:VOO"], "text": sentence, "provisional": False}
    body = us_body(md, snap)
    assert len(body) == 9 and body[-1] == (sentence, md.ORANGE)  # 矛盾句也一樣留在最後一列


def test_the_first_five_rows_are_exactly_what_they_were(md):
    snap = make_snapshot(DASH_NOW)
    old = copy.deepcopy(snap)
    for row_id in ROT_ROWS:
        del old["rows"][row_id]
    assert us_body(md, snap)[:5] == us_body(md, old)[:5] == FIRST_FIVE


def test_summary_bubble_still_cuts_other_cards_at_six(md):
    rows = [(f"第 {i} 列", md.GRAY) for i in range(10)]
    assert md.MAX_SUMMARY_ROWS == 6
    assert len(md._summary_bubble("t", rows)["body"]["contents"]) == 6
    assert len(md._summary_bubble("t", rows, max_rows=None)["body"]["contents"]) == 10


def test_the_bot_reads_the_three_rows_next_to_the_etf_rows(md):
    rows = list(md.READ_ROWS)
    first = rows.index("us.etf.SOXX")
    assert rows[first + 1:first + 4] == list(ROT_ROWS) and set(md.READ_ROWS) <= set(market_calc.ROW_IDS)


# 研判那一列：每個狀態 ×（有日期／沒日期）× 一般值與 rotation_values_ok 允許的極限值
LINE_CASES = [
    ("staples", {"xlp_ret20": 2.0, "soxx_ret20": -10.4}, None, "資金輪動：必需消費比半導體強 12.4 個百分點"),
    ("staples", {"xlp_ret20": 2.0, "soxx_ret20": -10.4}, DASH_PREV, "資金輪動 10/15：必需消費比半導體強 12.4 個百分點"),
    ("staples_gap_99_9", {"xlp_ret20": 49.9, "soxx_ret20": -50.0}, DASH_PREV, "資金輪動 10/15：必需消費比半導體強 99.9 個百分點"),
    ("staples_gap_100", {"xlp_ret20": 50.0, "soxx_ret20": -50.0}, DASH_PREV, "資金輪動 10/15：必需消費比半導體強"),
    ("staples_limit", {"xlp_ret20": 1000.0, "soxx_ret20": -99.9}, None, "資金輪動：必需消費比半導體強 1099.9 個百分點"),
    ("staples_limit", {"xlp_ret20": 1000.0, "soxx_ret20": -99.9}, DASH_PREV, "資金輪動 10/15：必需消費比半導體強"),
    ("semis", {"xlp_ret20": -10.4, "soxx_ret20": 2.0}, None, "資金輪動：半導體比必需消費強 12.4 個百分點"),
    ("semis", {"xlp_ret20": -10.4, "soxx_ret20": 2.0}, DASH_PREV, "資金輪動 10/15：半導體比必需消費強 12.4 個百分點"),
    ("semis_limit", {"xlp_ret20": -99.9, "soxx_ret20": 1000.0}, None, "資金輪動：半導體比必需消費強 1099.9 個百分點"),
    ("semis_limit", {"xlp_ret20": -99.9, "soxx_ret20": 1000.0}, DASH_PREV, "資金輪動 10/15：半導體比必需消費強"),
    ("watch_staples", {"soxx_rsi": 74, "xlp_above60": False}, None, "資金輪動：半導體偏熱、必需消費偏弱"),
    ("watch_staples", {"soxx_rsi": 74, "xlp_above60": False}, DASH_PREV, "資金輪動 10/15：半導體偏熱、必需消費偏弱"),
    ("watch_staples_limit", {"soxx_rsi": 100, "soxx_ma60": 1000.0, "soxx_dd240": 0.0, "xlp_rsi": 0, "xlp_above60": None,
                             "ko_rsi": 0, "ko_above60": None, "xlp_ret20": 1000.0, "soxx_ret20": -99.9}, DASH_PREV,
     "資金輪動 10/15：半導體偏熱、必需消費偏弱"),
    ("watch_semis", {"soxx_rsi": 25, "xlp_rsi": 72}, None, "資金輪動：半導體偏冷、必需消費偏熱"),
    ("watch_semis", {"soxx_rsi": 25, "xlp_rsi": 72}, DASH_PREV, "資金輪動 10/15：半導體偏冷、必需消費偏熱"),
    ("watch_semis_limit", {"soxx_rsi": 0, "soxx_ma60": -99.9, "soxx_dd240": -100.0, "xlp_rsi": 100, "ko_rsi": 100,
                           "xlp_ret20": -99.9, "soxx_ret20": 1000.0}, DASH_PREV, "資金輪動 10/15：半導體偏冷、必需消費偏熱"),
    ("none", {}, None, "資金輪動：沒有明顯方向"),
    ("none", {"xlp_ret20": 4.9, "soxx_ret20": 0.0}, DASH_PREV, "資金輪動 10/15：沒有明顯方向"),
    ("insufficient", {"xlp_ret20": None, "soxx_ret20": None}, None, "資金輪動：資料不足"),
    ("insufficient", {"xlp_ret20": None, "soxx_ret20": None}, DASH_PREV, "資金輪動 10/15：資料不足"),
]


@pytest.mark.parametrize("name,over,day,expected", LINE_CASES,
                         ids=[f"{case[0]}-{'dated' if case[2] else 'undated'}" for case in LINE_CASES])
def test_the_rotation_row_for_every_state_fits_and_keeps_its_date(md, name, over, day, expected):
    snap = make_snapshot(DASH_NOW)
    values = set_rot(snap, neutral(**over), day=day or DASH_DAY)
    assert values["state"] == name.removesuffix("_limit").split("_gap")[0]
    text, color = us_body(md, snap)[7]
    assert text == expected and md.row_width(text) <= 24 and "…" not in text, (text, md.row_width(text))
    assert color == (md.ORANGE if values["state"].startswith("watch_") else md.GRAY)
    passes_every_gate(text)


def test_the_widths_of_the_fixed_sentences():
    """列名＋日期（最寬的 12/31）＋狀態句的寬：帶差距最寬 24.0；差距到 100 以上（24.5）退到不帶差距那一段。"""
    import market_dashboard

    mc, width = market_calc, market_dashboard.row_width
    head, plain = f"{mc.TEXT_ROT_LABEL} 12/31：", f"{mc.TEXT_ROT_LABEL}："
    assert (width(head), width(plain)) == (8.0, 5.0)
    for sentence in (mc.TEXT_ROT_STAPLES, mc.TEXT_ROT_SEMIS):
        assert width(head + sentence + mc.TEXT_ROT_GAP.format(gap="99.9")) == 24.0
        assert width(head + sentence + mc.TEXT_ROT_GAP.format(gap="100.0")) == 24.5
        assert width(plain + sentence + mc.TEXT_ROT_GAP.format(gap="1099.9")) == 22.0
    assert {width(head + mc.ROT_STATE_TEXT[s]) for s in ("watch_staples", "watch_semis")} == {20.0}
    assert (width(head + mc.TEXT_ROT_NONE), width(plain + mc.TEXT_ROT_NONE)) == (14.0, 11.0)
    assert (width(mc.TEXT_ROT_XLP_NAME), width(mc.TEXT_ROT_KO_NAME)) == (7.5, 7.0)  # 比「那斯達克100（QQQ）」9.0 短


FOOTER_CASES = [
    ("watch_with_ko", {"soxx_rsi": 74, "soxx_ma60": 12.3, "soxx_dd240": -2.1, "xlp_rsi": 38, "xlp_above60": False,
                       "ko_rsi": 55, "ko_above60": True}, None, [WATCH_NOTE]),
    ("watch_without_ko", {"soxx_rsi": 74, "soxx_ma60": 12.3, "soxx_dd240": -2.1, "xlp_rsi": 38, "xlp_above60": False,
                          "ko_rsi": None, "ko_above60": None}, None,
     ["資金輪動只看價格：SOXX RSI 74、離季線 +12.3%、離 240 日高點 -2.1%；XLP RSI 38、在季線下"]),
    ("watch_missing_numbers", {"soxx_rsi": 74, "soxx_ma60": None, "soxx_dd240": None, "xlp_rsi": None,
                               "xlp_above60": False, "ko_rsi": None, "ko_above60": False}, None,
     ["資金輪動只看價格：SOXX RSI 74、離季線 暫缺、離 240 日高點 暫缺；XLP RSI 暫缺、在季線下；KO RSI 暫缺、在季線下"]),
    ("watch_semis", {"soxx_rsi": 25, "xlp_rsi": 72, "xlp_above60": None}, None,
     ["資金輪動只看價格：SOXX RSI 25、離季線 0.0%、離 240 日高點 -20.0%；XLP RSI 72、季線暫缺；KO RSI 50、在季線上"]),
    ("returns", {"xlp_ret20": -1.2, "soxx_ret20": -13.6}, None, ["資金輪動只看價格：近 20 個交易日 XLP -1.2%、SOXX -13.6%"]),
    ("none", {"xlp_ret20": 4.9, "soxx_ret20": 0.0}, None, ["資金輪動只看價格：近 20 個交易日 XLP +4.9%、SOXX 0.0%"]),
    ("held_trend", {"xlp_ret20": 2.0, "soxx_ret20": -10.4, "soxx_rsi": 74, "xlp_above60": False}, "staples",
     ["資金輪動只看價格：近 20 個交易日 XLP +2.0%、SOXX -10.4%", HELD]),
    ("held_watch", {"xlp_ret20": -10.4, "soxx_ret20": 2.0}, "watch_staples",
     ["資金輪動只看價格：SOXX RSI 50、離季線 0.0%、離 240 日高點 -20.0%；XLP RSI 50、在季線上；KO RSI 50、在季線上", HELD]),
    ("insufficient", {"xlp_ret20": None, "soxx_ret20": None}, None, []),
]


@pytest.mark.parametrize("over,state,expected", [case[1:] for case in FOOTER_CASES], ids=[case[0] for case in FOOTER_CASES])
def test_the_footer_says_what_the_read_is_based_on(md, over, state, expected):
    snap = make_snapshot(DASH_NOW)
    values = set_rot(snap, neutral(**over), state=state)
    assert market_calc.rotation_values_ok(values)
    assert us_notes(md, snap) == expected
    for note in expected:
        passes_every_gate(note)


def test_a_held_state_prints_no_gap_and_the_footer_says_why(md):
    snap = make_snapshot(DASH_NOW)
    values = set_rot(snap, neutral(xlp_ret20=2.0, soxx_ret20=-10.4, soxx_rsi=74, xlp_above60=False), state="staples",
                     day=DASH_PREV)
    assert values["raw"] == "watch_staples" and values["state"] == "staples"  # 當天是 watch，顯示的沿用前幾天的 staples
    assert us_body(md, snap)[7] == ("資金輪動 10/15：必需消費比半導體強", md.GRAY)  # 當天的數字不能拿來佐證：不印差距
    assert us_notes(md, snap)[-1] == HELD


# ── 插針那一行（2026-10-29 增補）：顯示的狀態是 watch_staples 而且插針成立時，WATCH 句之後、HELD 之前獨立一行 ──────────
WICK_NOTE = "SOXX 10/14 衝高回落（長上影線 3.1%），也算偏熱"
WICK_HOT = {"soxx_rsi": 65, "soxx_ma60": 6.0, "soxx_dd240": -2.0, "soxx_wick_pct": 3.1, "soxx_wick_day": "2026-10-14"}


def test_the_footer_names_the_wick_after_the_watch_sentence(md):
    """D1：watch_staples＋插針 → [WATCH 句, 插針句]；列上的字不變。"""
    snap = make_snapshot(DASH_NOW)
    values = set_rot(snap, neutral(**WICK_HOT, xlp_rsi=38, xlp_above60=False, ko_rsi=55))
    assert values["raw"] == values["state"] == "watch_staples"
    notes = us_notes(md, snap)
    assert notes == ["資金輪動只看價格：SOXX RSI 65、離季線 +6.0%、離 240 日高點 -2.0%；XLP RSI 38、在季線下；KO RSI 55、在季線上",
                     WICK_NOTE]
    assert us_body(md, snap)[7] == ("資金輪動 10/15：半導體偏熱、必需消費偏弱", md.ORANGE)
    for note in notes:
        passes_every_gate(note)


def test_a_held_watch_with_todays_wick_prints_the_wick_and_the_held_sentence(md):
    """D1（w3 X3-10）：state＝watch_staples 是沿用的、raw＝none（必需消費今天不弱）、插針今天仍在窗口裡 → [WATCH, 插針, HELD]：
    和 WATCH 句看同一個 state，不看 raw。"""
    snap = make_snapshot(DASH_NOW)
    values = set_rot(snap, neutral(**WICK_HOT), state="watch_staples")
    assert values["raw"] == "none" and values["state"] == "watch_staples"
    assert us_notes(md, snap) == [
        "資金輪動只看價格：SOXX RSI 65、離季線 +6.0%、離 240 日高點 -2.0%；XLP RSI 50、在季線上；KO RSI 50、在季線上",
        WICK_NOTE, HELD]


@pytest.mark.parametrize("over", [
    {"soxx_rsi": 72, "soxx_wick_pct": 1.4, "soxx_wick_day": "2026-10-14", "soxx_dd240": -2.0},
    {"soxx_rsi": 72, "soxx_wick_pct": 3.1, "soxx_wick_day": "2026-10-14", "soxx_dd240": -6.0},
], ids=["pct_1_4", "dd240_minus_6"])
def test_a_wick_that_is_not_hot_is_not_printed(md, over):
    """D2：插針不成立（太短、或離 240 日高點超過 5%）就不印；用 RSI 72 讓狀態仍是 watch_staples。"""
    snap = make_snapshot(DASH_NOW)
    values = set_rot(snap, neutral(**over, xlp_above60=False))
    assert values["state"] == "watch_staples"
    notes = us_notes(md, snap)
    assert len(notes) == 1 and notes[0].startswith(market_calc.TEXT_ROT_LABEL) and "衝高回落" not in notes[0]


def test_a_held_watch_semis_does_not_print_todays_wick(md):
    """D3：顯示的是 watch_semis（沿用）而今天插針成立 → 不印插針句（那一行只接「半導體偏熱」）。"""
    snap = make_snapshot(DASH_NOW)
    values = set_rot(snap, neutral(**WICK_HOT), state="watch_semis")
    assert values["raw"] == "none" and market_calc.rot_wick_hot(values)
    assert us_notes(md, snap) == [
        "資金輪動只看價格：SOXX RSI 65、離季線 +6.0%、離 240 日高點 -2.0%；XLP RSI 50、在季線上；KO RSI 50、在季線上", HELD]


@pytest.mark.parametrize("day,pct,expected", [
    ("2026-12-31", 15.0, "SOXX 12/31 衝高回落（長上影線 15.0%），也算偏熱"),
    ("2027-01-02", 1.5, "SOXX 1/2 衝高回落（長上影線 1.5%），也算偏熱"),
    ("2028-02-29", 100.0, "SOXX 2/29 衝高回落（長上影線 100.0%），也算偏熱"),
])
def test_the_wick_sentence_for_other_days_and_sizes_passes_every_gate(md, day, pct, expected):
    snap = make_snapshot(DASH_NOW)
    set_rot(snap, neutral(**{**WICK_HOT, "soxx_wick_pct": pct, "soxx_wick_day": day}, xlp_above60=False))
    notes = us_notes(md, snap)
    assert notes[1] == expected and len(notes) == 2
    passes_every_gate(expected)


def test_the_held_sentence_makes_no_promise(md):
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("bot_promise_scan_for_rotation", Path(__file__).resolve().parent / "jobs" /
                                                  "bot_promise_scan.py")
    scan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scan
    try:
        spec.loader.exec_module(scan)
        for name in ROT_TEXTS:
            assert scan.promise_sentences(getattr(market_calc, name)) == [], name
    finally:
        sys.modules.pop(spec.name, None)


def test_the_provisional_hours_print_these_rows(md):
    """CBOE 暫定時段：VOO／QQQ／SOXX 是 D（暫定），XLP、KO、研判都還是 D−1。"""
    snap = make_snapshot(DASH_NOW)
    rows = snap["rows"]
    for sym in ("VOO", "QQQ", "SOXX"):
        rows[f"us.etf.{sym}"]["data_date"] = DASH_DAY.isoformat()
        rows[f"us.etf.{sym}"]["source"] = market_calc.SOURCE_CBOE_FALLBACK
    for row_id in ROT_ROWS:
        rows[row_id]["data_date"] = DASH_PREV.isoformat()
    body = [text for text, _color in us_body(md, snap)]
    assert all(market_calc.TEXT_PROVISIONAL in text for text in body[:3])
    assert body[5:] == [
        "必需消費（XLP）80.5 -0.40% 在月線下（10/15）",  # 帶日期放不下 RSI：照 _etf_line 既有的退化順序先拿掉 RSI
        "可口可樂（KO）68.4 +0.10% 在月線上（10/15）",
        "資金輪動 10/15：半導體偏熱、必需消費偏弱",
        ONELINER_US,
    ]
    assert us_notes(md, snap) == [market_calc.TEXT_PROVISIONAL_CLOSE, WATCH_NOTE]  # 既有的註記在前、資金輪動在後


def test_an_old_snapshot_without_the_rows_shows_missing(md, paths):
    snap = make_snapshot(DASH_NOW)
    for row_id in ROT_ROWS:
        del snap["rows"][row_id]
    write_snapshot(paths, snap)
    reply = md.handle("", now=DASH_NOW, paths=paths)
    assert reply.flex is not None and reply.render_errors == ()
    bubble = summary_bubble(md, reply.flex, "美股")
    assert texts(bubble["body"])[5:] == ["必需消費（XLP）暫缺", "可口可樂（KO）暫缺", "資金輪動：暫缺", ONELINER_US]
    assert not any(note.startswith(market_calc.TEXT_ROT_LABEL) for note in texts(bubble["footer"]))
    assert_main_gate(reply)


def test_stale_rows_show_their_data_date(md):
    snap = make_snapshot(DASH_NOW)
    for row_id in ROT_ROWS:
        snap["rows"][row_id]["stale_after"] = DASH_NOW - 1
    assert [text for text, _color in us_body(md, snap)[5:8]] == [
        "必需消費（XLP）過期（資料日 10/16）", "可口可樂（KO）過期（資料日 10/16）", "資金輪動：過期（資料日 10/15）"]
    assert us_notes(md, snap) == []


@pytest.mark.parametrize("how", ["raw_mismatch", "rsi_120", "unrounded", "unknown_state", "missing_key", "never",
                                 "values_list", "wick_day_not_iso", "wick_pct_without_a_day", "wick_day_without_a_pct",
                                 "wick_pct_1e300", "wick_keys_missing"])
def test_bad_rotation_values_show_missing_and_nothing_breaks(md, paths, how):
    snap = make_snapshot(DASH_NOW)
    row = snap["rows"]["us.rot.state"]
    values = row["values"]
    if how.startswith("wick_"):  # 插針的兩個鍵壞掉（2026-10-29 增補 D4）：一樣只讓研判列暫缺
        values.update({
            "wick_day_not_iso": {"soxx_wick_pct": 3.1, "soxx_wick_day": "10/14"},
            "wick_pct_without_a_day": {"soxx_wick_pct": 3.1, "soxx_wick_day": None},
            "wick_day_without_a_pct": {"soxx_wick_pct": None, "soxx_wick_day": "2026-10-14"},
            "wick_pct_1e300": {"soxx_wick_pct": 1e300, "soxx_wick_day": "2026-10-14"},
            "wick_keys_missing": {},
        }[how])
        if how == "wick_keys_missing":  # v2 產的舊快照：沒有這兩個鍵
            del values["soxx_wick_pct"], values["soxx_wick_day"]
    elif how == "raw_mismatch":
        values["raw"] = "staples"
    elif how == "rsi_120":
        values["soxx_rsi"] = 120
    elif how == "unrounded":
        values["soxx_ma60"] = 12.34
    elif how == "unknown_state":
        values["state"] = "rally"
    elif how == "missing_key":
        del values["ko_rsi"]
    elif how == "never":
        snap["rows"]["us.rot.state"] = _never()
    else:
        row["values"] = [1, 2]
    write_snapshot(paths, snap)
    reply = md.handle("", now=DASH_NOW, paths=paths)
    assert reply.flex is not None and reply.render_errors == ()
    bubble = summary_bubble(md, reply.flex, "美股")
    body = texts(bubble["body"])
    assert body[7] == "資金輪動：暫缺" and body[5] == ROT_XLP_DEFAULT and body[-1] == ONELINER_US
    assert not any(note.startswith(market_calc.TEXT_ROT_LABEL) for note in texts(bubble["footer"]))
    assert not any("衝高回落" in note for note in texts(bubble["footer"]))
    assert_main_gate(reply)


def test_the_worst_us_bubble_and_carousel_stay_within_the_limits(md, paths):
    import market_dashboard

    write_snapshot(paths, worst_snapshot(DASH_NOW))
    reply = md.handle("", now=DASH_NOW, paths=paths)
    assert reply.flex is not None and reply.render_errors == ()
    bubble = summary_bubble(md, reply.flex, "美股")
    body = texts(bubble["body"])
    assert len(body) == 9 and all(md.row_width(text) <= 24 and "…" not in text for text in body), body
    assert body[5:8] == ["必需消費（XLP）9,999,999 在月線下（10/14）", "可口可樂（KO）9,999,999 在月線下（10/14）",
                         "資金輪動 10/14：半導體偏熱、必需消費偏弱"]
    notes = texts(bubble["footer"])
    assert notes[:notes.index(DISCLAIMER)][-3:] == [
        "資金輪動只看價格：SOXX RSI 100、離季線 +1,000.0%、離 240 日高點 -5.0%；XLP RSI 100、在季線上；KO RSI 100、在季線上",
        "SOXX 10/14 衝高回落（長上影線 100.0%），也算偏熱",
        HELD,
    ]
    assert md.card_bytes(bubble) <= market_dashboard.SUMMARY_BUBBLE_MAX_BYTES == 6656
    assert md.card_bytes(reply.flex) <= market_dashboard.CARD_MAX_BYTES == 28 * 1024


# ═════════════════════════════════════════════════════════════════════════════
# pipeline：us.rot 任務（XLP、KO 日線）。test_market_pipeline_us 的假 Nasdaq、真的 Store、真的框架（run_task／run_collect）
# D＝10/16（週五），NOW＝美東 10/16 17:00
# ═════════════════════════════════════════════════════════════════════════════
ROT_DAYS = sessions(PIPE_D, 260)  # 假 Nasdaq 上 XLP、KO 的一年＋D


def d_published(env_):
    """四檔 ETF 到 10/15、SPY 已經有 D 的正式列（Nasdaq 公布了 D）；假 Nasdaq 上 XLP、KO 都有一年＋D。"""
    etfs_ready(env_)
    seed(env_, "SPY", {PIPE_D: 100.0})
    env_.http.prices["XLP"] = flat(ROT_DAYS, 80.0)
    env_.http.prices["KO"] = flat(ROT_DAYS, 60.0)


def pending_jump(day=PIPE_D, close=61.0, *, next_at=PIPE_NOW - 1.0):
    return {"kind": "jump", "dates": [day.isoformat()], "closes": {day.isoformat(): close},
            "disputed": [day.isoformat(), day.isoformat()], "seen_at": PIPE_NOW - 900.0, "since": day.isoformat(),
            "disagree": 0, "quarantined": False, "next_at": next_at}


def test_rot_symbols_the_task_slot_and_the_endpoint_note():
    assert pipe.ROT_SYMBOLS == (("XLP", True), ("KO", False))
    core = [task.name for task in pipe.TASKS if task.group == "core"]
    assert core[-2:] == ["us.events.econ", "us.rot"]  # 美股 core 的最後一個：輪動列優先序最低（_interleave 保持先後）
    task = {task.name: task for task in pipe.TASKS}["us.rot"]
    assert task.budget_s == 20.0 and task.market == "us" and task in market_tasks.registered()
    # 規劃值（程式沒拿它擋）：每輪 60 檔是 us.hist 的，us.rot 自己再 2 檔
    assert src.ENDPOINTS["nasdaq_historical"].per_run == pipe.HIST_MAX_SYMBOLS_PER_RUN + len(pipe.ROT_SYMBOLS) == 62


def test_xlp_is_fetched_as_an_etf_and_ko_as_a_stock_even_when_ko_is_a_member(env):
    d_published(env)
    seed_universe(env, ["KO", "AAA"])
    seed(env, "KO", flat(PIPE_HISTORY, 60.0))  # KO 在成分股清單裡、已經有歷史（B 案：us.rot 照樣自己抓）
    assert run(env, "us.rot") == "ok"
    xlp, ko = env.http.query("hist", "XLP"), env.http.query("hist", "KO")
    assert [query["assetclass"] for query in xlp] == ["etf"] and [query["assetclass"] for query in ko] == ["stocks"]
    assert xlp[0]["fromdate"] == (PIPE_D - timedelta(days=pipe.HIST_BOOTSTRAP_DAYS)).isoformat()  # 第一次補一年
    assert ko[0]["fromdate"] < PIPE_HISTORY[-1].isoformat()  # KO 是一般更新（最後有效日往前 3 個交易日起）
    assert PIPE_D in closes(env, "XLP") and PIPE_D in closes(env, "KO")
    assert env.store.task_state("us.rot")["fail_streak"] == 0
    assert run(env, "us.rot", PIPE_NOW + 900) == "not_due"  # 兩檔都有 D 了：不白打
    assert len(env.http.requested("hist")) == 2


def test_due_needs_a_published_d_no_block_and_a_symbol_without_d(env):
    def due(now=PIPE_NOW):
        return TASKS["us.rot"].due(ctx_at(env, now))

    etfs_ready(env)  # 四檔 ETF 都還沒有 D 的正式列
    env.http.prices["XLP"] = flat(ROT_DAYS, 80.0)
    assert not due()  # Nasdaq 還沒公布 D
    seed(env, "SPY", {PIPE_D: 100.0})
    assert due()
    env.store.set_task_state("sched:us.hist", {"blocked": {"kind": "blocked", "at": PIPE_NOW - 60}})
    assert not due()  # us.hist 撞到 403／429
    env.store.set_task_state("sched:us.hist", {})
    env.store.set_task_state("sched:us.rot", {"blocked": {"kind": "rate_limited", "at": PIPE_NOW - 60}})
    assert not due()  # us.rot 自己撞到（nasdaq_blocked 只看 us.hist）
    assert due(PIPE_NOW + pipe.NASDAQ_BLOCK_VALID_S + 60)  # 超過 3 小時就不當被擋
    env.store.set_task_state("sched:us.rot", {})
    seed(env, "XLP", flat(PIPE_HISTORY + [PIPE_D], 80.0))
    seed(env, "KO", flat(PIPE_HISTORY + [PIPE_D], 60.0))
    assert not due()  # XLP 與 KO 都已經是最新
    assert pipe._run_rot(ctx_at(env, PIPE_NOW)) is None and env.http.requested("hist") == []


def test_a_missing_d_waits_in_us_rots_own_retry_and_is_not_a_failure(env):
    d_published(env)
    env.http.prices["XLP"] = flat(sessions(date(2026, 10, 15), 260), 80.0)  # XLP 的 D 還沒出來
    assert run(env, "us.rot") == "partial"  # KO 好了、XLP 還沒有 D
    assert pipe._state(ctx_at(env, PIPE_NOW), "us.rot")["retry"]["XLP"]["n"] == 1
    assert "XLP" not in pipe._state(ctx_at(env, PIPE_NOW), "us.hist").get("retry", {})
    assert env.store.task_state("us.rot")["fail_streak"] == 0
    assert run(env, "us.rot", PIPE_NOW + 900) == "not_due"  # 30 分鐘內不重打
    env.http.prices["XLP"] = flat(ROT_DAYS, 80.0)
    assert run(env, "us.rot", PIPE_NOW + 1800) == "ok"
    assert len(env.http.query("hist", "XLP")) == 2 and PIPE_D in closes(env, "XLP")


def test_us_rot_never_touches_the_us_hist_state(env):
    """us.hist 的排程紀錄（retry、run、gate_open…）、框架紀錄與 us.hist.status 完全不變；us.rot 只有自己的鍵。"""
    etfs_ready(env)
    seed_universe(env, ["FFF"])
    seed(env, "FFF", flat(PIPE_HISTORY, 30.0))
    env.http.prices["FFF"] = flat(PIPE_HISTORY, 30.0)  # FFF 還沒有 D：us.hist 記一筆重排
    env.http.prices["XLP"] = flat(ROT_DAYS, 80.0)
    env.http.prices["KO"] = flat(ROT_DAYS, 60.0)
    assert run(env, "us.hist") == "partial"
    before = (env.store.task_state("sched:us.hist"), env.store.task_state("us.hist"), obs(env, pipe.OBS_HIST_STATUS))
    assert before[0]["retry"]["FFF"]["n"] == 1 and before[0]["gate_open"] == PIPE_D.isoformat()
    assert run(env, "us.rot", PIPE_NOW + 60) == "ok"
    assert (env.store.task_state("sched:us.hist"), env.store.task_state("us.hist"),
            obs(env, pipe.OBS_HIST_STATUS)) == before
    own = env.store.task_state("sched:us.rot")
    assert set(own) <= {"retry", "split_retry", "run", "blocked"} and set(own["retry"]) <= {"XLP", "KO"}
    assert own["run"]["used"] == 2


def test_us_rot_keeps_its_own_two_requests_when_us_hist_used_all_sixty(env):
    d_published(env)
    names = [f"S{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(70)]
    seed_universe(env, names)
    for name in names:
        seed(env, name, flat(PIPE_HISTORY, 10.0))
        env.http.prices[name] = flat(PIPE_HISTORY + [PIPE_D], 10.0)
    ctx = ctx_at(env, PIPE_NOW)  # 同一輪
    market_tasks.run_task(ctx, TASKS["us.hist"])
    assert len(env.http.requested("hist")) == pipe.HIST_MAX_SYMBOLS_PER_RUN
    assert market_tasks.run_task(ctx, TASKS["us.rot"]) == "ok"
    requested = env.http.requested("hist")
    assert len(requested) == pipe.HIST_MAX_SYMBOLS_PER_RUN + len(pipe.ROT_SYMBOLS) and requested[-2:] == ["XLP", "KO"]
    assert pipe._state(ctx, "us.hist")["run"]["used"] == 60 and pipe._state(ctx, "us.rot")["run"]["used"] == 2
    assert not TASKS["us.hist.backfill"].due(ctx)  # us.hist 那 60 檔的上限不受影響


def test_after_us_rot_writes_kos_d_us_hist_does_not_ask_for_ko_again(env):
    d_published(env)
    seed_universe(env, ["KO"])
    seed(env, "XLP", flat(PIPE_HISTORY, 80.0))
    seed(env, "KO", flat(PIPE_HISTORY, 60.0))
    ctx = ctx_at(env, PIPE_NOW)
    assert market_tasks.run_task(ctx, TASKS["us.rot"]) == "ok"
    assert PIPE_D in closes(env, "KO") and len(env.http.query("hist", "KO")) == 1
    market_tasks.run_task(ctx, TASKS["us.hist"])  # 同一輪輪到 us.hist
    market_tasks.run_task(ctx_at(env, PIPE_NOW + 900), TASKS["us.hist"])  # 下一輪
    assert len(env.http.query("hist", "KO")) == 1  # 都沒有再對 KO 發請求（_job_for 看到 KO 已經有 D）
    assert set(env.http.requested("hist")) == {"XLP", "KO", "VOO", "QQQ", "SOXX"}


@pytest.mark.parametrize("in_universe", [True, False], ids=["ko_member", "ko_not_member"])
def test_a_due_pending_jump_for_ko_has_one_owner(env, in_universe):
    """待確認的跳空存在 store、以代號為鍵，兩條管線共用：KO 在 us.hist 的清單裡時由 us.hist 確認，us.rot 不碰。"""
    d_published(env)
    seed(env, "XLP", flat(PIPE_HISTORY + [PIPE_D], 80.0))  # XLP 已經是最新：這裡只看 KO
    seed(env, "KO", flat(PIPE_HISTORY, 60.0))
    env.http.prices["KO"] = {**flat(sessions(date(2026, 10, 15), 260), 60.0), PIPE_D: 61.0}
    if in_universe:
        seed_universe(env, ["KO"])
    env.store.set_pending_jump("us", "KO", pending_jump())
    if in_universe:
        assert run(env, "us.rot") == "not_due" and env.http.query("hist", "KO") == []
        assert run(env, "us.hist") in ("ok", "partial")  # us.hist 才是這一檔的確認者
    else:
        assert run(env, "us.rot") == "ok"
    (query,) = env.http.query("hist", "KO")
    assert query["fromdate"] == (PIPE_D - timedelta(days=pipe.HIST_FULL_DAYS)).isoformat()  # 確認＝整段重抓一次
    assert closes(env, "KO")[PIPE_D][0] == 61.0 and env.store.pending_jump("us", "KO") is None


@pytest.mark.parametrize("in_universe", [True, False], ids=["ko_member", "ko_not_member"])
def test_a_due_split_refetch_for_ko_has_one_owner(env, in_universe):
    d_published(env)
    seed(env, "XLP", flat(PIPE_HISTORY + [PIPE_D], 80.0))
    seed(env, "KO", {date(2025, 1, 2): 120.0, **flat(PIPE_HISTORY, 60.0)})  # 2:1 分割前的舊值還在
    env.store.upsert_splits([("KO", date(2026, 10, 1), 2.0, 1.0)], "nasdaq")
    env.http.prices["KO"] = flat(ROT_DAYS, 60.0)  # 來源已經調整
    if in_universe:
        seed_universe(env, ["KO"])
        assert run(env, "us.rot") == "not_due" and env.http.query("hist", "KO") == []
        assert run(env, "us.hist") in ("ok", "partial")
    else:
        assert run(env, "us.rot") == "ok"
    (query,) = env.http.query("hist", "KO")  # 整段重抓只有一次：不會兩條管線各抓一次
    assert query["fromdate"] == (PIPE_D - timedelta(days=pipe.HIST_FULL_DAYS)).isoformat()
    assert env.store.last_full_refetch("us", "KO") == PIPE_D and date(2025, 1, 2) not in closes(env, "KO")


def test_a_round_that_runs_out_before_us_rot_is_stopped_not_failed(env):
    """（a）整輪時間到、us.rot 沒輪到：結果是 STOPPED，框架不呼叫 _record，task_state 原封不動。"""
    d_published(env)
    previous = {"fail_streak": 2, "next_try": None, "last_kind": "budget"}
    env.store.set_task_state("us.rot", dict(previous))
    ctx = ctx_at(env, PIPE_NOW)
    ctx.deadline = env.clock() + 30.0
    env.http.seconds_per_call = 45.0  # us.hist 的第一個請求就把這一輪用完
    outcome = market_tasks.run_collect(ctx, [TASKS["us.hist"], TASKS["us.rot"]])
    assert outcome["stopped"] == "deadline" and outcome["results"]["us.rot"] == market_tasks.STOPPED
    assert env.store.task_state("us.rot") == previous
    assert env.http.query("hist", "XLP") == [] and env.http.query("hist", "KO") == []


def test_us_rot_starting_with_no_time_left_is_budget_not_a_failure(env):
    """（b）us.rot 到期、開始跑，但第一個 job 之前 should_stop() 就是真的：_Tally.status() 丟 FetchError("budget")，
    框架記 budget、fail_streak 不變（NON_FAILURE_KINDS）。"""
    d_published(env)
    env.store.set_task_state("us.rot", {"fail_streak": 2, "next_try": None})
    ctx = ctx_at(env, PIPE_NOW)
    ctx.deadline = env.clock()  # 這一輪剛好用完
    assert TASKS["us.rot"].due(ctx)
    with pytest.raises(market_http.FetchError) as caught:
        pipe._run_rot(ctx)
    assert caught.value.kind == "budget"
    assert market_tasks.run_task(ctx, TASKS["us.rot"]) == "budget"
    state = env.store.task_state("us.rot")
    assert state["fail_streak"] == 2 and state["last_kind"] == "budget" and state.get("next_try") is None
    assert env.http.requested("hist") == []


def test_the_us_bubble_with_everything_on_stays_within_the_limits(md):
    """F3-6 的 everything_on（所有標示同時開著；worst＝最長的字）：美股泡泡 ≤6,656 B、整個摘要輪播 ≤28,672 B。"""
    from test_market_f2_display import everything_on

    for now in ALL_NOWS:
        for worst in (False, True):
            reply = md._summary_reply(md._Ctx(everything_on(now, worst=worst), now))
            assert reply.flex is not None and reply.render_errors == (), (now, worst, reply.render_errors)
            bubble = summary_bubble(md, reply.flex, "美股")
            assert len(bubble["body"]["contents"]) == 9
            assert md.card_bytes(bubble) <= md.SUMMARY_BUBBLE_MAX_BYTES == 6656, md.card_bytes(bubble)
            assert md.card_bytes(reply.flex) <= md.CARD_MAX_BYTES == 28 * 1024, md.card_bytes(reply.flex)


# ── 插針（2026-10-29 增補）：_fetch_rows 順手存 SOXX 的開高低（us.ohlc.SOXX）。假 Nasdaq 預設的開高低都是 "1"（每一根都不合格），
# 這裡用 hist_raw 給真的開高低；收盤和已存的一樣（100.0），不會碰到覆寫守門 ──────────────────────────────────────────────
SOXX_BAR = (99.8, 100.6, 99.7, 100.0)
UPDATE_DAYS = sessions(PIPE_D, 5)  # SOXX 的一般更新區間：10/12（最後有效日 10/15 往前 3 個交易日）～ D


def nasdaq_bars(symbol, bars, *, etf=True):
    """假 Nasdaq 日線的整份回應（hist_raw 用，不看查詢的日期區間）：bars＝{日期: (開, 高, 低, 收)}，None 寫成 "N/A"。"""
    money = "" if etf else "$"

    def text(value):
        return "N/A" if value is None else money + _num_text(value)

    rows = [{"date": day.strftime("%m/%d/%Y"), "close": text(c), "volume": "1,000", "open": text(o), "high": text(h),
             "low": text(low)} for day, (o, h, low, c) in sorted(bars.items(), reverse=True)]
    return nasdaq({"symbol": symbol, "totalRecords": len(rows),
                   "tradesTable": {"asOf": None, "headers": {"date": "Date", "close": "Close/Last"}, "rows": rows}})


def soxx_ohlc_ready(env_, days=UPDATE_DAYS, bar=SOXX_BAR):
    """四檔 ETF 到 10/15（收盤 100）、Nasdaq 有 D；SOXX 這次抓到的每一根都是同一個合格的開高低。"""
    etfs_ready(env_)
    env_.http.hist_raw["SOXX"] = nasdaq_bars("SOXX", {day: bar for day in days})


def stored_ohlc_json(env_):
    """store 裡原始的 JSON 字（不經過 get_obs）。"""
    row = env_.store._db().execute("SELECT json FROM obs WHERE obs_id = 'us.ohlc.SOXX'").fetchone()
    return None if row is None else row[0]


def fetched_bars(days=UPDATE_DAYS, bar=SOXX_BAR):
    return {"bars": {day.isoformat(): list(bar) for day in days}}


def test_a_soxx_update_stores_its_ohlc(env):
    """P1：只有 ISO 日期鍵和 4 個 float、日期都 ≤D、data_date 是最新那天；收盤路徑不變。"""
    soxx_ohlc_ready(env)
    assert run(env, "us.hist") == "ok"
    assert closes(env, "SOXX")[PIPE_D] == (100.0, "nasdaq", False)
    found = obs(env, "us.ohlc.SOXX")
    assert found["data_date"] == PIPE_D.isoformat() and found["payload"] == fetched_bars()
    raw = json.loads(stored_ohlc_json(env))
    assert set(raw) == {"bars"} and all(date.fromisoformat(key) <= PIPE_D for key in raw["bars"])
    assert all(len(bar) == 4 and all(type(value) is float for value in bar) for bar in raw["bars"].values())
    assert market_calc.ohlc_from_payload(raw) == {day: SOXX_BAR for day in UPDATE_DAYS}


def test_this_fetch_wins_for_its_days_and_only_30_sessions_are_kept(env):
    """P2：先放 30 天舊資料（到 10/14）；這次抓 5 根，10/13 的 open 是 N/A → 那一天的舊 bar 被拿掉；10/12、10/14 用這次的；
    10/15、D 新加進來；最舊的一天掉出 30 天。"""
    old_days = sessions(date(2026, 10, 14), 30)
    env.store.put_obs("us.ohlc.SOXX", {"bars": {day.isoformat(): [99.0, 101.0, 98.9, 99.5] for day in old_days}},
                      data_date="2026-10-14")
    bars = {day: SOXX_BAR for day in UPDATE_DAYS}
    bars[date(2026, 10, 13)] = (None, 100.6, 99.7, 100.0)
    etfs_ready(env)
    env.http.hist_raw["SOXX"] = nasdaq_bars("SOXX", bars)
    assert run(env, "us.hist") == "ok"
    found = obs(env, "us.ohlc.SOXX")
    got = found["payload"]["bars"]
    kept = sorted(set(old_days) - {date(2026, 10, 13)} | {date(2026, 10, 15), PIPE_D})[-30:]
    assert list(got) == [day.isoformat() for day in kept] and len(got) == 30
    assert old_days[0].isoformat() not in got and "2026-10-13" not in got
    assert got["2026-10-12"] == got["2026-10-14"] == got["2026-10-16"] == list(SOXX_BAR)
    assert got[old_days[1].isoformat()] == [99.0, 101.0, 98.9, 99.5]
    assert found["data_date"] == PIPE_D.isoformat()
    assert closes(env, "SOXX")[date(2026, 10, 13)] == (100.0, "nasdaq", False)  # 收盤照樣寫


def test_a_soxx_bootstrap_keeps_only_the_newest_30_sessions(env):
    """P3：沒有歷史的 SOXX 抓一年（約 252 根）→ 只留最新 30 根。"""
    days = sessions(PIPE_D, 252)
    soxx_ohlc_ready(env, days)
    sql(env, "DELETE FROM closes WHERE market = 'us' AND symbol = 'SOXX'")
    assert run(env, "us.hist") == "ok"
    (query,) = env.http.query("hist", "SOXX")
    assert query["fromdate"] == (PIPE_D - timedelta(days=pipe.HIST_BOOTSTRAP_DAYS)).isoformat()
    assert len(closes(env, "SOXX")) > 200
    assert obs(env, "us.ohlc.SOXX")["payload"] == fetched_bars(days[-30:])


def test_only_soxx_stores_ohlc(env):
    """P4：XLP（us.rot）、KO、VOO、成分股的回應就算有合格的開高低，也不寫 us.ohlc.*（SOXX 這裡是預設的假資料，不合格）。"""
    d_published(env)
    seed_universe(env, ["AAA"])
    seed(env, "AAA", flat(PIPE_HISTORY, 50.0))
    for symbol, price, etf, days in (("XLP", 80.0, True, ROT_DAYS), ("KO", 60.0, False, ROT_DAYS),
                                     ("VOO", 100.0, True, UPDATE_DAYS), ("AAA", 50.0, False, UPDATE_DAYS)):
        env.http.hist_raw[symbol] = nasdaq_bars(symbol, {day: (price - 0.2, price + 0.6, price - 0.3, price)
                                                         for day in days}, etf=etf)
    assert run(env, "us.hist") == "ok"
    assert run(env, "us.rot") == "ok"
    assert all(PIPE_D in closes(env, symbol) for symbol in ("XLP", "KO", "VOO", "AAA", "SOXX"))
    assert env.store.obs_ids("us.ohlc.") == []


def test_a_failing_ohlc_write_is_logged_by_type_only_and_the_closes_still_update(env, monkeypatch, caplog):
    """P5：put_obs 對 us.ohlc.* 丟例外 → 確實呼叫過、SOXX 收盤照常、job 結果和沒有插針時一樣；log 只有類別名、沒有例外訊息。"""
    soxx_ohlc_ready(env)
    real = env.store.put_obs
    calls = []

    def put_obs(obs_id, payload, **kwargs):
        if obs_id.startswith("us.ohlc."):
            calls.append(obs_id)
            raise RuntimeError("SECRET-SOURCE-TEXT")
        return real(obs_id, payload, **kwargs)

    monkeypatch.setattr(env.store, "put_obs", put_obs)
    caplog.set_level(logging.INFO, logger="market_pipeline_us")
    assert run(env, "us.hist") == "ok"
    assert calls == ["us.ohlc.SOXX"]
    assert closes(env, "SOXX")[PIPE_D] == (100.0, "nasdaq", False)
    assert env.store.task_state("us.hist")["fail_streak"] == 0 and obs(env, "us.ohlc.SOXX") is None
    assert "unexpected:RuntimeError" in caplog.text and "SECRET" not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


def test_a_busy_store_skips_the_ohlc_write_and_the_closes_still_update(env, monkeypatch, caplog):
    """P6：get_obs 對 us.ohlc.* 丟 sqlite3.OperationalError（資料庫忙）→ 這次不寫，收盤照常。"""
    soxx_ohlc_ready(env)
    real_get, real_put = env.store.get_obs, env.store.put_obs
    asked, written = [], []

    def get_obs(obs_id):
        if obs_id.startswith("us.ohlc."):
            asked.append(obs_id)
            raise sqlite3.OperationalError("database is locked SECRET-SOURCE-TEXT")
        return real_get(obs_id)

    def put_obs(obs_id, payload, **kwargs):
        written.append(obs_id)
        return real_put(obs_id, payload, **kwargs)

    monkeypatch.setattr(env.store, "get_obs", get_obs)
    monkeypatch.setattr(env.store, "put_obs", put_obs)
    caplog.set_level(logging.INFO, logger="market_pipeline_us")
    assert run(env, "us.hist") == "ok"
    assert asked == ["us.ohlc.SOXX"] and not [obs_id for obs_id in written if obs_id.startswith("us.ohlc.")]
    assert closes(env, "SOXX")[PIPE_D] == (100.0, "nasdaq", False) and real_get("us.ohlc.SOXX") is None
    assert "unexpected:OperationalError" in caplog.text and "SECRET" not in caplog.text


def test_a_corrupt_stored_payload_is_rebuilt_from_this_fetch(env, caplog):
    """P7：直接用 SQL 把那一筆的 JSON 寫壞（get_obs 丟 ValueError）→ 當成沒有舊資料，用這次抓到的 bar 重建。"""
    env.store.put_obs("us.ohlc.SOXX", fetched_bars([date(2026, 10, 9)]), data_date="2026-10-09")
    sql(env, "UPDATE obs SET json = ? WHERE obs_id = 'us.ohlc.SOXX'", '{"bars": {"2026-10-09": [99.8,')
    with pytest.raises(ValueError):
        env.store.get_obs("us.ohlc.SOXX")
    soxx_ohlc_ready(env)
    caplog.set_level(logging.INFO, logger="market_pipeline_us")
    assert run(env, "us.hist") == "ok"
    assert obs(env, "us.ohlc.SOXX")["payload"] == fetched_bars()
    assert closes(env, "SOXX")[PIPE_D] == (100.0, "nasdaq", False)
    assert "unexpected:JSONDecodeError" in caplog.text


PIPE_OHLC_JUNK = [  # 舊 payload 是垃圾：日期都 ≤D、不在這次的抓取區間（10/12～D）裡，只能靠驗證丟掉
    ("null", "null"), ("a_list", "[]"), ("a_string", '"x"'), ("no_bars", '{"nobars": 1}'),
    ("bars_is_a_list", '{"bars": []}'), ("bars_is_a_string", '{"bars": "x"}'),
    ("value_is_a_string", '{"bars": {"2026-10-09": "y"}}'),
    ("key_is_not_a_date", '{"bars": {"x": [99.8, 100.6, 99.7, 100.0]}}'),
    ("two_values", '{"bars": {"2026-10-09": [1.0, 2.0]}}'),
    ("five_values", '{"bars": {"2026-10-09": [99.8, 100.6, 99.7, 100.0, 1.0]}}'),
    ("string_values", '{"bars": {"2026-10-09": ["99.8", "100.6", "99.7", "100.0"]}}'),
    ("bool_value", '{"bars": {"2026-10-09": [99.8, true, 99.7, 100.0]}}'),
    ("nested_value", '{"bars": {"2026-10-09": [[99.8], 100.6, 99.7, 100.0]}}'),
    ("int_values", '{"bars": {"2026-10-09": [99, 101, 98, 100]}}'),
    ("nan_token", '{"bars": {"2026-10-09": [99.8, NaN, 99.7, 100.0]}}'),
    ("inf_1e400", '{"bars": {"2026-10-09": [99.8, 1e400, 99.7, 100.0]}}'),
    ("big_int_401_digits", '{"bars": {"2026-10-09": [99.8, ' + _BIG + ', 99.7, 100.0]}}'),
    ("compact_date", '{"bars": {"20261009": [99.8, 100.6, 99.7, 100.0]}}'),
    ("iso_week_date", '{"bars": {"2026-W41-5": [99.8, 100.6, 99.7, 100.0]}}'),
    ("full_width_digits", '{"bars": {"２０２６-10-09": [99.8, 100.6, 99.7, 100.0]}}'),
    ("a_day_after_d", '{"bars": {"2026-10-19": [99.8, 100.6, 99.7, 100.0]}}'),
]


@pytest.mark.parametrize("text", [case[1] for case in PIPE_OHLC_JUNK], ids=[case[0] for case in PIPE_OHLC_JUNK])
def test_a_junk_stored_payload_never_breaks_the_soxx_closes(env, text):
    """P8：舊 payload 是垃圾 → _fetch_rows 不丟例外、收盤照常，寫回去的只剩這次抓到的合格 bar（ISO 鍵）。"""
    env.store.put_obs("us.ohlc.SOXX", {"bars": {}}, data_date=None)
    sql(env, "UPDATE obs SET json = ? WHERE obs_id = 'us.ohlc.SOXX'", text)
    soxx_ohlc_ready(env)
    assert run(env, "us.hist") == "ok"
    assert closes(env, "SOXX")[PIPE_D] == (100.0, "nasdaq", False)
    assert json.loads(stored_ohlc_json(env)) == fetched_bars()


def test_the_same_fetch_again_does_not_rewrite_and_the_default_fake_writes_nothing(env, monkeypatch):
    """P9：既有的假資料（開高低都是 "1"）又沒有舊資料 → 不寫空的 payload；同樣的資料再跑一輪 → 不再寫。"""
    etfs_ready(env)
    assert run(env, "us.hist") == "ok" and obs(env, "us.ohlc.SOXX") is None
    assert env.store.obs_ids("us.ohlc.") == []
    env.http.hist_raw["SOXX"] = nasdaq_bars("SOXX", {day: SOXX_BAR for day in UPDATE_DAYS})
    job = pipe._Job("SOXX", True, "update", date(2026, 10, 15))
    rows = pipe._fetch_rows(ctx_at(env, PIPE_NOW + 900), job, UPDATE_DAYS[0], PIPE_D)
    assert [row[0] for row in rows] == UPDATE_DAYS
    first = obs(env, "us.ohlc.SOXX")
    assert first["payload"] == fetched_bars()
    real = env.store.put_obs
    written = []

    def put_obs(obs_id, payload, **kwargs):
        written.append(obs_id)
        return real(obs_id, payload, **kwargs)

    monkeypatch.setattr(env.store, "put_obs", put_obs)
    assert pipe._fetch_rows(ctx_at(env, PIPE_NOW + 1800), job, UPDATE_DAYS[0], PIPE_D) == rows
    assert written == [] and obs(env, "us.ohlc.SOXX") == first


def test_note_ohlc_takes_only_dated_bars_up_to_d(env):
    """P10：直接呼叫 _note_ohlc（正式來源已經濾掉 >D，只能這樣測）：>D、datetime、沒有日期的 bar 都不收。"""
    bars = [src.Bar(PIPE_D, 100.0, 1000, 99.8, 100.6, 99.7), src.Bar(date(2026, 10, 19), 101.0, 1000, 100.8, 101.6, 100.7),
            src.Bar(datetime(2026, 10, 15, 16, 0), 100.0, 1000, 99.8, 100.6, 99.7), src.Bar(None, 100.0, 1000, 99.8, 100.6, 99.7)]
    pipe._note_ohlc(ctx_at(env, PIPE_NOW), "SOXX", bars, PIPE_D)
    found = obs(env, "us.ohlc.SOXX")
    assert found["payload"] == fetched_bars([PIPE_D]) and found["data_date"] == PIPE_D.isoformat()
    pipe._note_ohlc(ctx_at(env, PIPE_NOW), "XLP", bars, PIPE_D)  # 不在 OHLC_SYMBOLS：不寫、不丟例外
    assert env.store.obs_ids("us.ohlc.") == ["us.ohlc.SOXX"]


def test_the_confirm_path_also_stores_the_ohlc(env):
    """P11（nit）：待確認的 SOXX 走 confirm（整段重抓）也會順手存開高低。"""
    days = sessions(PIPE_D, 260)
    soxx_ohlc_ready(env, days)
    env.store.set_pending_jump("us", "SOXX", pending_jump(close=100.0))
    assert run(env, "us.hist") == "ok"
    (query,) = env.http.query("hist", "SOXX")
    assert query["fromdate"] == (PIPE_D - timedelta(days=pipe.HIST_FULL_DAYS)).isoformat()  # 確認＝整段重抓
    assert env.store.pending_jump("us", "SOXX") is None
    assert obs(env, "us.ohlc.SOXX")["payload"] == fetched_bars(days[-30:])
