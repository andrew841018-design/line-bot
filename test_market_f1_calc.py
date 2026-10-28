"""批 1 審查後修正 F1（資料／安全層）的計算與正規式部分（2026-10-28）：F1-2 RSI 的最少收盤數、F1-9 日期／月份正規式加 re.ASCII、
F1-10 損益兩平 5 日變化的日曆天上限。

審查原文：reviews/w1_review_seat3.md 的 I-1、nit2；reviews/w1_review_gp2.md 的 N9。判定：reviews/w1_phase4_triage.md 的 F1-2、F1-9、F1-10。
這個檔案單獨一份（不和別的修正軌改同一個測試檔）；每個測試都要在 w1-int（4e984e8）上失敗、修正後通過，
「對照」性質的測試（確認原本就對的情形沒被改壞）會在註解裡說明。
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta

import pytest

import market_calc as mc
import market_paths
import market_publish
from test_market_dashboard import NOW, make_snapshot, md, paths, write_snapshot  # noqa: F401  md／paths 是 fixture


# ═══ F1-2：RSI 要有足夠的收盤才算 ═══════════════════════════════════════════════════════════════════════


def weekdays(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


SESSIONS = weekdays(date(2025, 6, 2), 300)
DAY = SESSIONS[-1]
# 不是單調上漲／下跌的合成數列（單調的話 RSI 恆為 100 或 0，看不出收斂與否）
CLOSES = {s: round(100 + 0.04 * i + 6 * math.sin(i / 5.0) + 3 * math.cos(i / 2.3), 4) for i, s in enumerate(SESSIONS)}


def rsi_with_tail(tail: int, quarantine=None):
    """D 往回剛好 `tail` 個連續收盤（第 tail+1 個預期交易日沒有收盤）時 technicals 算出的 RSI14。"""
    closes = dict(CLOSES)
    del closes[SESSIONS[-(tail + 1)]]
    return mc.technicals(closes, SESSIONS, DAY, quarantine).rsi14


def test_the_minimum_is_eighty_closes():
    assert mc.RSI_MIN_CLOSES == 80 and mc.RSI_MIN_CLOSES > mc.RSI_PERIOD + 1


@pytest.mark.parametrize("tail", [15, 16, 20, 40, 60, 79])
def test_a_tail_shorter_than_the_minimum_gives_no_rsi(tail):
    """seat3 I-1：缺口後 15～60 日 Wilder 的種子還沒被平滑掉，這份合成數列的完整序列 RSI 是 41.0，tail=20 卻算出 56.9。
    base 在 tail≥15 就算（tail=14 本來就是 None，所以從 15 開始）。"""
    assert rsi_with_tail(tail) is None


@pytest.mark.parametrize("tail", [80, 81, 100, 120, 200])
def test_a_long_enough_tail_matches_the_full_series_to_the_displayed_integer(tail):
    """不會剛好相等（Wilder 的種子不同，只是影響被平滑掉了）：誤差要小到卡上的整數不變。對照：原本就會算的情形沒被改壞。"""
    full = mc.technicals(CLOSES, SESSIONS, DAY).rsi14
    got = rsi_with_tail(tail)
    assert full is not None and got is not None
    assert abs(got - full) < 0.5 and round(got) == round(full), (got, full)


def test_the_full_series_is_unchanged():
    assert mc.technicals(CLOSES, SESSIONS, DAY).rsi14 == pytest.approx(41.0297, abs=1e-3)


def test_a_quarantine_interval_cuts_the_tail_the_same_way():
    near = [(SESSIONS[-41], SESSIONS[-40])]  # 碰到之後只剩 39 筆
    far = [(SESSIONS[-101], SESSIONS[-100])]  # 99 筆
    assert mc.technicals(CLOSES, SESSIONS, DAY, near).rsi14 is None
    assert mc.technicals(CLOSES, SESSIONS, DAY, far).rsi14 is not None
    assert mc.technicals(CLOSES, SESSIONS, DAY, near).ma[20] is not None  # 其他指標不受影響（20 日窗口沒碰到）


def test_short_histories_have_no_rsi_but_keep_the_other_indicators():
    short = SESSIONS[-60:]
    tech = mc.technicals({s: CLOSES[s] for s in short}, short, DAY)
    assert tech.rsi14 is None and tech.ma[20] is not None and tech.chg_pct is not None


def test_the_publish_comment_names_the_real_rule():
    source = open(market_publish.__file__, encoding="utf-8").read()
    line = next(row for row in source.splitlines() if '"rsi14": tech.rsi14' in row)
    assert "往回 14 個變化有缺口" not in line and "RSI_MIN_CLOSES" in line


# ═══ F1-9：日期與月份的比對只收 ASCII 數字 ═══════════════════════════════════════════════════════════════

ARABIC_DATE, FULLWIDTH_DATE = "٢٠٢٦-١٠-٢٢", "２０２６-１０-２２"
ARABIC_MONTH = "٢٠٢٦-09"  # 阿拉伯數字的年、ASCII 的月


@pytest.mark.parametrize("pattern", [mc._ISO_DATE_RE, market_publish._DATE_RE])
def test_date_regexes_take_only_ascii_digits(pattern):
    assert pattern.fullmatch("2026-10-22") and pattern.flags & re.ASCII
    for text in (ARABIC_DATE, FULLWIDTH_DATE, "2026-10-٢٢"):
        assert pattern.fullmatch(text) is None, text


def test_the_publish_month_regex_takes_only_ascii_digits():
    pattern = market_publish._MONTH_RE
    assert pattern.fullmatch("2026-10") and pattern.flags & re.ASCII
    for text in ("٢٠٢٦-١٠", "２０２６-１０", ARABIC_MONTH):
        assert pattern.fullmatch(text) is None, text


def test_non_ascii_digits_never_become_a_published_cpi_series_key():
    assert market_publish._cpi_series({"CUUR0000SA0": {ARABIC_MONTH: 300.0, "2026-09": 301.0}}) == {
        "CUUR0000SA0": {"2026-09": 301.0}}


def test_the_status_summary_cpi_month_takes_only_ascii_digits(md, paths):
    """outcome 的 infl_cpi_month 原樣放進 status_summary（給每日維護看）：阿拉伯數字的年份不能通過。"""
    write_snapshot(paths, make_snapshot(NOW))
    base = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"}}
    for month, expect in (("2026-09", "2026-09"), (ARABIC_MONTH, None), ("２０２６-09", None), ("2026-٠٩", None)):
        market_paths.write_private_atomic(paths.outcome, json.dumps({**base, "infl_cpi_month": month}).encode())
        assert md.status_summary(now=NOW, paths=paths)["outcome"]["infl_cpi_month"] == expect, month


# ═══ F1-10：損益兩平的 5 日變化不能跨太久 ═══════════════════════════════════════════════════════════════


def row(day: str, n5=4.50, n10=4.70, r5=2.20, r10=2.30) -> dict:
    return {"date": day, "n5": n5, "n10": n10, "r5": r5, "r10": r10}


def rows_on(days: list[str], last_n5: float = 4.80) -> list[dict]:
    """每天兩個檔都有值；最後一天的名目 5 年是 last_n5，其他是 4.50（所以 5 日變化＝最新 − 第 6 個共同日期）。"""
    return [row(day, n5=last_n5 if index == len(days) - 1 else 4.50) for index, day in enumerate(days)]


GAP_DAYS = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-28", "2026-09-29", "2026-09-30"]  # 實質檔缺 9/11–9/25


def test_a_long_gap_in_the_common_dates_gives_no_five_day_change():
    """seat3 nit2：實質檔缺兩週，「5 日變化」跨 22 個日曆日（+30 bp、標 up）。"""
    values = mc.breakeven_values(rows_on(GAP_DAYS))
    assert values["y5"]["date"] == "2026-09-30" and values["y5"]["value"] == 2.60  # 最新值照給
    for tenor in ("y5", "y10"):  # 兩個天期的共同日期一樣，都跨了 22 天
        assert values[tenor]["chg5_bp"] is None and values[tenor]["move"] is None, tenor
    assert mc.values_ok("us.infl.be", values)


@pytest.mark.parametrize("days, span, has_change", [
    # 感恩節那週（11/26 休市）：第 6 個共同日期＝11/20，到 11/30 剛好 10 個日曆天——自然會發生，要算
    (["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30"], 10, True),
    # 再多一天（週末加上連假把窗口拉到 11 天）：不算
    (["2026-10-02", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-13"], 11, False),
    (["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-13"], 8, True),
    # 聖誕與元旦（12/25、1/1 休市）
    (["2026-12-23", "2026-12-24", "2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31"], 8, True),
    (["2026-12-24", "2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-04"], 11, False),
])
def test_the_calendar_span_limit_is_ten_days_inclusive(days, span, has_change):
    assert (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days == span
    values = mc.breakeven_values(rows_on(days))
    assert (values["y5"]["chg5_bp"] is not None) is has_change
    assert (values["y5"]["move"] is not None) is has_change
    if has_change:
        assert values["y5"]["chg5_bp"] == 30 and values["y5"]["move"] == "up"
    assert mc.values_ok("us.infl.be", values)


def test_each_tenor_is_judged_on_its_own_common_dates():
    """只有 5 年的實質格缺了 9/8–9/14：5 年的 6 個共同日期是 9/1、9/2、9/3、9/15、9/16、9/17（跨 16 天，不算），
    10 年每天都有（第 6 個共同日期是 9/10，只隔 7 天，照算）。"""
    days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
            "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
    rows = [row(day, n5=4.50, n10=4.70 + (0.20 if day == days[-1] else 0.0)) for day in days]
    for item in rows[3:-3]:
        item["r5"] = None
    values = mc.breakeven_values(rows)
    assert values["y5"]["date"] == values["y10"]["date"] == "2026-09-17"
    assert values["y5"]["chg5_bp"] is None and values["y5"]["move"] is None
    assert values["y10"]["chg5_bp"] == 20 and values["y10"]["move"] == "up"
    assert values["spread_bp"] is not None and mc.values_ok("us.infl.be", values)


def test_the_normal_october_series_keeps_its_five_day_change():
    """對照：保存的 2026-10 檔（test_market_calc_infl 的 OCT_ROWS）最新 10/22 對 10/15，只隔 7 天，結果不變。"""
    from test_market_calc_infl import oct_rows

    values = mc.breakeven_values(oct_rows())
    assert values["y5"]["chg5_bp"] == 12 and values["y5"]["move"] == "up"
    assert values["y10"]["chg5_bp"] == 0 and values["y10"]["move"] == "flat"
