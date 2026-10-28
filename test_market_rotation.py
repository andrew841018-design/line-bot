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
import output_validator

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
