"""/盤勢 資金輪動（必需消費 XLP／KO 對半導體 SOXX；2026-10-28）的測試。

Andrew 2026-10-28：新增消費型類股（KO、XLP）的監控，用技術面預判資金輪動；放在美股卡、不寫死單一組條件、不推播、KO 也能觸發
（B 案）、研判那一列用描述句。規格是資金輪動計畫 v2（§3 計算、§4 快照、§5 卡片、§2 抓取）與第 4 階段的取捨表。

分四段（每段先在還沒改產品碼時跑紅，再實作）：
- calc：門檻、三值邏輯的真值表、20 日報酬的窗口與隔離區間、連續 2 個交易日才換、values 的驗證。
- publish：us.rot.XLP／us.rot.KO／us.rot.state 三列。
- dashboard：美股卡的三列與 footer。
- pipeline：us.rot 任務（XLP、KO 日線）。

新的符號一律在測試函式裡才取（parametrize 只放字面值）：舊程式沒有這些名字時，只讓用到的測試紅，不讓整個檔案收集失敗。
合成資料都是程式產生的收盤序列，不連網、不放家人資料。
"""

from __future__ import annotations

import copy
import json
import math
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
    closes,
    ctx_at,
    env,
    etfs_ready,
    flat,
    market_http,
    market_tasks,
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
VALUE_KEYS = ("xlp_ret20", "soxx_ret20", "soxx_rsi", "soxx_ma60", "soxx_dd240", "xlp_rsi", "xlp_above60", "ko_rsi",
              "ko_above60")
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
    """注入的指標（都已經是顯示精度）：預設什麼條件都不成立 → none。"""
    values = {"xlp_ret20": 0.0, "soxx_ret20": 0.0, "soxx_rsi": 50, "soxx_ma60": 0.0, "soxx_dd240": -20.0,
              "xlp_rsi": 50, "xlp_above60": True, "ko_rsi": 50, "ko_above60": True}
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

    def fake_inputs(closes, quarantine, cal, day):
        seen.append((closes, quarantine, cal, day))
        return {"day": day}

    monkeypatch.setattr(market_calc, "rot_inputs", fake_inputs)
    monkeypatch.setattr(market_calc, "rot_classify", lambda values: states[values["day"]])
    closes, quarantine = {"XLP": {}}, {}
    got = market_calc.rotation_values(closes, quarantine, CAL, DAY)
    assert got == {"day": DAY, "raw": plan[-1], "state": expected}
    assert sorted({item[3] for item in seen}) == days[-5:]  # 研判日＋往前 4 個交易日，用同一份輸入
    assert all(item[0] is closes and item[1] is quarantine and item[2] is CAL for item in seen)


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

    def broken(*args):
        values = real(*args)
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
    """us.etf.SOXX、圖、矛盾句都用 ctx.shared 快取裡的同一個 list／dict：組 us.rot.state 不能就地改它們。"""
    world.store.add_quarantine("us", "SOXX", US_DAYS[5], US_DAYS[6], "split_pending")
    world.store.add_quarantine("us", "XLP", US_DAYS[8], US_DAYS[9], "conflict")
    ctx = market_publish._Ctx(world.store, world.paths, PUB_NOW)
    for row_id in ("us.etf.SOXX", "us.rot.XLP", "us.rot.KO"):
        assert market_publish._build_row(ctx, row_id)["state"] == "ok", row_id
    keys = ("closes:us:SOXX", "closes:us:XLP", "closes:us:KO", "quarantine:us")
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
    assert worst["state"] == "watch_staples" and worst["raw"] == "semis"  # footer 最長：watch＋KO＋「連續 2 個交易日」


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
                                 "values_list"])
def test_bad_rotation_values_show_missing_and_nothing_breaks(md, paths, how):
    snap = make_snapshot(DASH_NOW)
    row = snap["rows"]["us.rot.state"]
    values = row["values"]
    if how == "raw_mismatch":
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
    assert notes[:notes.index(DISCLAIMER)][-2:] == [
        "資金輪動只看價格：SOXX RSI 100、離季線 +1,000.0%、離 240 日高點 -100.0%；XLP RSI 100、在季線上；KO RSI 100、在季線上",
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
