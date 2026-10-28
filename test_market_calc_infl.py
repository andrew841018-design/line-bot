"""market_calc 的通膨計算（損益兩平、CPI；2026-10-24 批 2 C 軌）的測試：純函式，不連網、不碰檔案。

數字從保存的真實回應算（research_bodies/infl_diag/），不抄研究文件的文字（審查 r9 s3-F17）。
"""

from __future__ import annotations

import decimal
import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import market_calc as mc

# 裁自 research_bodies/infl_diag/ust_nominal_202610.body 與 ust_real_202610.body（2026-10 全部 15 個共同日期；沒有 10/12 哥倫布日）
OCT_ROWS = (  # (日期, 名目5, 名目10, 實質5, 實質10)
    ("2026-10-01", 5.01, 5.24, 2.65, 2.88),
    ("2026-10-02", 5.06, 5.28, 2.69, 2.92),
    ("2026-10-05", 5.06, 5.31, 2.70, 2.95),
    ("2026-10-06", 5.03, 5.27, 2.66, 2.91),
    ("2026-10-07", 5.03, 5.28, 2.66, 2.92),
    ("2026-10-08", 4.99, 5.22, 2.62, 2.87),
    ("2026-10-09", 5.02, 5.24, 2.68, 2.91),
    ("2026-10-13", 5.00, 5.21, 2.67, 2.88),
    ("2026-10-14", 5.00, 5.19, 2.64, 2.84),
    ("2026-10-15", 4.91, 5.10, 2.58, 2.78),
    ("2026-10-16", 4.89, 5.09, 2.52, 2.74),
    ("2026-10-19", 4.94, 5.15, 2.56, 2.80),
    ("2026-10-20", 4.94, 5.12, 2.55, 2.77),
    ("2026-10-21", 4.95, 5.13, 2.55, 2.76),
    ("2026-10-22", 4.78, 4.96, 2.33, 2.64),
)


def row(day, n5=None, n10=None, r5=None, r10=None) -> dict:
    return {"date": day, "n5": n5, "n10": n10, "r5": r5, "r10": r10}


def oct_rows(upto: str = "2026-10-22") -> list[dict]:
    return [row(*item) for item in OCT_ROWS if item[0] <= upto]


# ── 殖利率 → 整數 bp ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, expected", [
    ("4.78", 478), (4.78, 478), (2.3, 230), (0, 0), (-0.25, -25), ("5", 500), (4.35, 435), (4.3, 430), (1, 100),
    (4.785, None),  # 3 位小數：不是整數 bp，不收（財政部給 2 位）
    ("4.7800000001", None), (50.01, None), (-50.01, None), (1e400, None),
    (True, None), (None, None), ("abc", None), ("", None), (float("nan"), None), (float("inf"), None), ([4.78], None),
    ("٤.٧٨", None),  # 阿拉伯數字：Decimal() 會收，這裡只收 ASCII
])
def test_yield_bp_is_exact_integer_basis_points(value, expected):
    assert mc.yield_bp(value) == expected


def test_yield_bp_ignores_the_global_decimal_context():
    """bot 也 import 這個模組：別的程式把全域精度調很低，也不能改變結果；我們也不動全域設定。"""
    before = decimal.getcontext().prec
    with decimal.localcontext() as ctx:
        ctx.prec = 2
        assert mc.yield_bp("4.78") == 478 and mc.yield_bp(0.07) == 7
    assert decimal.getcontext().prec == before


# ── 5 年／10 年損益兩平（保存的 2026-10 檔） ─────────────────────────────────────


def test_breakeven_from_the_saved_october_files():
    values = mc.breakeven_values(oct_rows())
    # 10/22：5 年 4.78−2.33＝2.45、10 年 4.96−2.64＝2.32；5 個共同日期前是 10/15（2.33、2.32）
    assert values == {
        "y5": {"date": "2026-10-22", "value": 2.45, "chg5_bp": 12, "move": "up"},
        "y10": {"date": "2026-10-22", "value": 2.32, "chg5_bp": 0, "move": "flat"},
        "spread_bp": 13,
        "spread": "five_higher",
    }
    assert mc.values_ok("us.infl.be", values)


def test_the_series_is_oldest_first_and_only_has_dates_both_curves_have():
    series = mc.breakeven_series(oct_rows(), 10)
    assert [day for day, _ in series] == sorted(day for day, _ in series) and len(series) == 15
    assert series[-1] == (date(2026, 10, 22), 232) and series[0] == (date(2026, 10, 1), 236)
    assert date(2026, 10, 12) not in {day for day, _ in series}


def test_five_day_change_counts_common_dates_not_calendar_days():
    """10/12（哥倫布日）債市休、股市開：兩個檔都沒有那天。到 10/14 為止往前數 5 個共同日期是 10/06，不是日曆上的 10/09。"""
    series = mc.breakeven_series(oct_rows("2026-10-14"), 5)
    assert [day.isoformat() for day, _ in series][-6:] == [
        "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-13", "2026-10-14"]
    tenor = mc.breakeven_tenor(series)
    assert tenor.date == date(2026, 10, 14) and tenor.chg5_bp == (500 - 264) - (503 - 266)  # 10/14 比 10/06：236−237


def test_veterans_day_gap_is_skipped_the_same_way():
    base = [row(f"2026-11-{d:02d}", 4.5, 4.7, 2.2, 2.3) for d in (2, 3, 4, 5, 6, 9, 10)]  # 11/11 兩個檔都沒有
    base += [row("2026-11-12", 4.6, 4.8, 2.2, 2.3), row("2026-11-13", 4.6, 4.8, 2.2, 2.3)]
    tenor = mc.breakeven_tenor(mc.breakeven_series(base, 5))
    assert tenor.date == date(2026, 11, 13) and tenor.chg5_bp == (460 - 220) - (450 - 220)  # 往前 5 個共同日期＝11/05


@pytest.mark.parametrize("count, has_change", [(5, False), (6, True), (7, True), (1, False)])
def test_fewer_than_six_common_dates_have_no_five_day_change(count, has_change):
    rows = oct_rows()[-count:]
    values = mc.breakeven_values(rows)
    assert (values["y10"]["chg5_bp"] is not None) is has_change
    assert (values["y10"]["move"] is not None) is has_change
    assert mc.values_ok("us.infl.be", values)


def test_dates_where_only_one_curve_has_a_value_do_not_count_toward_the_six():
    rows = oct_rows("2026-10-16")[-5:]  # 5 個共同日期
    rows.append(row("2026-10-19", 4.94, 5.15))  # 名目有、實質還沒有：不是共同日期
    rows.append(row("2026-10-20", None, None, 2.55, 2.77))  # 反過來
    values = mc.breakeven_values(rows)
    assert values["y10"]["date"] == "2026-10-16" and values["y10"]["chg5_bp"] is None


def test_nominal_leading_by_a_day_keeps_the_last_common_date():
    rows = oct_rows() + [row("2026-10-23", 4.80, 4.99)]  # 財政部先放名目
    values = mc.breakeven_values(rows)
    assert values["y5"]["date"] == "2026-10-22" and values["y10"]["date"] == "2026-10-22"
    rows = oct_rows() + [row("2026-10-23", 4.80, 4.99, 2.30, 2.60)]  # 實質也到了
    values = mc.breakeven_values(rows)
    assert values["y5"]["date"] == "2026-10-23" and values["y5"]["value"] == 2.5 and values["y10"]["value"] == 2.39
    assert values["y5"]["chg5_bp"] == (480 - 230) - (489 - 252)  # 往前 5 個共同日期＝10/16（10/19、20、21、22 在中間）


def test_a_missing_real_cell_only_drops_that_tenor():
    rows = oct_rows() + [row("2026-10-23", 4.80, 4.99, None, 2.60)]  # 實質 5 年缺格
    values = mc.breakeven_values(rows)
    assert values["y10"]["date"] == "2026-10-23" and values["y5"]["date"] == "2026-10-22"
    assert values["spread_bp"] is None and values["spread"] is None  # 兩個天期不同一天：不比
    assert mc.values_ok("us.infl.be", values)


@pytest.mark.parametrize("change, move", [
    (5, "up"), (4, "flat"), (0, "flat"), (-4, "flat"), (-5, "down"), (12, "up"), (-30, "down"),
])
def test_move_code_exactly_five_basis_points_counts(change, move):
    assert mc.be_move_code(change) == move


@pytest.mark.parametrize("spread, code", [
    (10, "five_higher"), (9, "similar"), (0, "similar"), (-9, "similar"), (-10, "five_lower"), (13, "five_higher"),
])
def test_spread_code_exactly_ten_basis_points_counts(spread, code):
    assert mc.be_spread_code(spread) == code


def test_a_float_subtraction_would_have_called_exactly_five_basis_points_flat():
    """4.35−4.30 在 float 是 0.04999999999999982（<0.05）；整數 bp 是剛好 5，算上升（審查 r9 s3-F3）。"""
    assert 4.35 - 4.30 < 0.05
    rows = [row(f"2026-10-{d:02d}", 4.30, 4.30, 2.00, 2.00) for d in (1, 2, 5, 6, 7)] + [row("2026-10-08", 4.35, 4.35, 2.00, 2.00)]
    values = mc.breakeven_values(rows)
    assert values["y5"]["chg5_bp"] == 5 and values["y5"]["move"] == "up"
    rows[-1] = row("2026-10-08", 4.40, 4.40, 2.00, 2.00)  # 兩個天期一樣：5 年減 10 年是 0
    assert mc.breakeven_values(rows)["spread"] == "similar"
    rows[-1] = row("2026-10-08", 4.35, 4.35, 2.00, 1.90)  # 5 年 2.35、10 年 2.45：差剛好 −10 bp
    values = mc.breakeven_values(rows)
    assert values["spread_bp"] == -10 and values["spread"] == "five_lower"


def test_garbage_rows_are_ignored_and_nothing_usable_is_none():
    garbage = [
        None, 5, "x", [1, 2, 3], {}, {"date": "2026-10-22"}, {"date": 20261022, "n5": 4.78, "n10": 4.96, "r5": 2.33, "r10": 2.64},
        row("10/22/2026", 4.78, 4.96, 2.33, 2.64), row("2026-02-30", 4.78, 4.96, 2.33, 2.64),
        row("2026-10-22", True, True, 2.33, 2.64), row("2026-10-22", "abc", "x", None, float("nan")),  # bool 不是數字
        row("2026-10-22", 4.785, 4.965, 2.33, 2.64),  # 3 位小數：兩個天期都不是整數 bp
    ]
    assert mc.breakeven_values(garbage) is None
    assert mc.breakeven_values([]) is None and mc.breakeven_values(None) is None and mc.breakeven_values("rows") is None
    good = oct_rows()
    assert mc.breakeven_values(garbage + good) == mc.breakeven_values(good)


def test_the_same_date_twice_keeps_the_later_row():
    rows = oct_rows() + [row("2026-10-22", 4.80, 4.96, 2.33, 2.64)]
    assert mc.breakeven_values(rows)["y5"]["value"] == 2.47


def test_tenor_must_be_five_or_ten():
    with pytest.raises(ValueError):
        mc.breakeven_series(oct_rows(), 7)


# ── 欄位表：列舉碼只收白名單裡的字 ────────────────────────────────────────────────


def test_enum_spec_accepts_only_known_codes():
    spec = ("?", frozenset({"up", "down"}))
    assert mc.spec_ok(spec, "up") and mc.spec_ok(spec, None)
    assert not mc.spec_ok(spec, "sideways") and not mc.spec_ok(spec, 5) and not mc.spec_ok(spec, ["up"])
    strict = frozenset({"up", "down"})
    assert mc.spec_ok(strict, "down") and not mc.spec_ok(strict, None) and not mc.spec_ok(strict, True)


def _good() -> dict:
    return mc.breakeven_values(oct_rows())


@pytest.mark.parametrize("mutate", [
    lambda v: v["y5"].update(move="sideways"),
    lambda v: v["y10"].update(move=1),
    lambda v: v.update(spread="x"),
    lambda v: v["y5"].update(date=20261022),
    lambda v: v["y5"].update(date="2026-13-40"),
    lambda v: v["y5"].update(value="2.45"),
    lambda v: v["y5"].update(chg5_bp=1.5),
    lambda v: v.update(spread_bp=True),
    lambda v: v["y10"].pop("move"),
    lambda v: v.pop("spread_bp"),
    lambda v: v.update(y5="2.45"),
])
def test_the_breakeven_spec_rejects_free_text_wrong_types_and_missing_keys(mutate):
    values = _good()
    mutate(values)
    assert not mc.values_ok("us.infl.be", values)


def test_nullable_parts_validate():
    values = _good()
    values["y5"] = None
    values["spread_bp"] = None
    values["spread"] = None
    assert mc.values_ok("us.infl.be", values)
    values["y10"]["chg5_bp"] = None
    values["y10"]["move"] = None
    assert mc.values_ok("us.infl.be", values)


def test_the_fixed_basis_text_passes_the_forbidden_word_check():
    assert mc.TEXT_BE_BASIS in mc.FIXED_TEXTS
    assert not any(word in mc.TEXT_BE_BASIS for word in mc.FORBIDDEN_WORDS)


# 裁自 research_bodies/infl_diag/bls_get_cuur0000sa0.body（CUUR0000SA0）與 bls_post_3series.body（其餘三個序列）：
# 每個序列 2024-01…2026-09 共 33 個月，值照原字串（"336.940"）；2025-10 的 value 是 "-"（2025 年聯邦政府撥款中斷，BLS 註腳
# "Data unavailable due to the 2025 lapse in appropriations"）。CUUR0000SA0 的原始回應另有 2 個年平均列（2024、2025 的 M13），
# 所以是 35 列；季調序列沒有 M13（33 列）——用位置往前數 12 列，未季調會落在 2025-M10 的 "-"、季調剛好是對的月份。
BLS_YEARS = {
    "CUUR0000SA0": {
        "2024": ['308.417', '310.326', '312.332', '313.548', '314.069', '314.175', '314.540', '314.796', '315.301', '315.664', '315.493', '315.605'],
        "2025": ['317.671', '319.082', '319.799', '320.795', '321.465', '322.561', '323.048', '323.976', '324.800', '-', '324.122', '324.054'],
        "2026": ['325.252', '326.785', '330.213', '333.020', '335.123', '333.952', '333.918', '334.980', '336.940'],
    },
    "CUSR0000SA0": {
        "2024": ['309.698', '310.967', '312.345', '313.023', '313.175', '313.044', '313.569', '314.062', '314.732', '315.631', '316.528', '317.604'],
        "2025": ['318.961', '319.679', '319.785', '320.302', '320.620', '321.435', '322.169', '323.291', '324.245', '-', '325.063', '326.031'],
        "2026": ['326.588', '327.460', '330.293', '332.407', '333.979', '332.568', '332.813', '334.131', '336.298'],
    },
    "CUUR0000SA0L1E": {
        "2024": ['313.623', '315.419', '317.088', '317.978', '318.629', '319.003', '319.214', '320.017', '321.109', '321.758', '321.947', '322.007'],
        "2025": ['323.842', '325.252', '325.933', '326.815', '327.509', '328.364', '328.980', '329.970', '330.804', '-', '330.425', '330.506'],
        "2026": ['331.950', '333.242', '334.391', '335.803', '336.846', '336.882', '337.133', '338.041', '339.287'],
    },
    "CUSR0000SA0L1E": {
        "2024": ['314.319', '315.564', '316.792', '317.649', '318.095', '318.390', '318.940', '319.750', '320.732', '321.731', '322.657', '323.259'],
        "2025": ['324.638', '325.465', '325.690', '326.467', '326.893', '327.658', '328.682', '329.700', '330.418', '-', '331.043', '331.814'],
        "2026": ['332.793', '333.512', '334.165', '335.423', '336.121', '336.065', '336.789', '337.765', '338.896'],
    },
}


# ═══ 美國 CPI 與核心 CPI（BLS；2026-10-24 批 2 C 軌） ═══════════════════════════════════════════════

TAIPEI = ZoneInfo("Asia/Taipei")
EASTERN = ZoneInfo("America/New_York")


def D(text: str) -> Decimal:
    return Decimal(text)


def saved_series() -> dict[str, dict[str, str]]:
    """四個序列 2024-01…2026-09（保存的真實值，字串）；2025-10 的 "-" 不放。"""
    return {sid: {f"{year}-{index:02d}": value for year, values in years.items() for index, value in enumerate(values, 1)
                  if value != "-"}
            for sid, years in BLS_YEARS.items()}


def grow(series: dict[str, dict[str, str]], through: str, step: str = "0.002") -> dict[str, dict[str, str]]:
    """合成：保存的回應只到 2026-09；後面的月份每月＋0.2%（只給測試「基期缺值」「跨年」用，不是行情）。"""
    out = {sid: dict(values) for sid, values in series.items()}
    for sid, values in out.items():
        month = max(values)
        while month < through:
            month = mc.month_shift(month, 1)
            values[month] = str((D(values[mc.month_shift(month, -1)]) * (1 + D(step))).quantize(D("0.001")))
    return out


def two_decimals(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=decimal.ROUND_HALF_UP)


def test_month_arithmetic_crosses_years():
    assert mc.month_shift("2026-01", -3) == "2025-10" and mc.month_shift("2026-02", -3) == "2025-11"
    assert mc.month_shift("2026-12", 1) == "2027-01" and mc.month_shift("2027-01", -12) == "2026-01"
    assert mc.month_shift("2026-09", 0) == "2026-09" and mc.month_shift("2026-09", -25) == "2024-08"
    for bad in ("2026-13", "2026-00", "2026-9", "202609", "", None, 5):
        with pytest.raises(ValueError):
            mc.month_shift(bad, 1)


def test_the_saved_responses_give_the_september_numbers_to_two_decimals():
    """從保存的指數重算（不抄研究文件）：年增 3.74／2.56、月增 0.65／0.33、近 3 月年化 4.56／3.41（核心月增 0.3348，研究文件寫 0.34）。"""
    s = saved_series()
    sa, nsa, csa, cnsa = s["CUSR0000SA0"], s["CUUR0000SA0"], s["CUSR0000SA0L1E"], s["CUUR0000SA0L1E"]
    assert two_decimals((D(nsa["2026-09"]) / D(nsa["2025-09"]) - 1) * 100) == D("3.74")
    assert two_decimals((D(cnsa["2026-09"]) / D(cnsa["2025-09"]) - 1) * 100) == D("2.56")
    assert two_decimals((D(sa["2026-09"]) / D(sa["2026-08"]) - 1) * 100) == D("0.65")
    assert two_decimals((D(csa["2026-09"]) / D(csa["2026-08"]) - 1) * 100) == D("0.33")
    assert two_decimals(((D(sa["2026-09"]) / D(sa["2026-06"])) ** 4 - 1) * 100) == D("4.56")
    assert two_decimals(((D(csa["2026-09"]) / D(csa["2026-06"])) ** 4 - 1) * 100) == D("3.41")
    # 公布精度是一位小數（BLS）：一律先 ROUND_HALF_UP 取一位，門檻與差值都用這個顯示值
    assert mc.cpi_yoy(nsa, "2026-09") == D("3.7") and mc.cpi_yoy(cnsa, "2026-09") == D("2.6")
    assert mc.cpi_mom(sa, "2026-09") == D("0.6") and mc.cpi_mom(csa, "2026-09") == D("0.3")  # 0.6485 → 0.6、0.3348 → 0.3
    assert mc.cpi_ann3(sa, "2026-09") == D("4.6") and mc.cpi_ann3(csa, "2026-09") == D("3.4")


def test_september_2026_values_and_codes():
    values = mc.cpi_values(saved_series(), date(2026, 10, 24))
    assert values["cpi"] == {
        "month": "2026-09-01", "yoy": 3.7, "yoy_month": "2026-09-01", "yoy_prev": 3.4, "mom": 0.6, "ann3": 4.6,
        "sa_month": "2026-09-01", "level": "well_above", "momentum": "accelerating", "yoy_move": "up", "yoy_move_pp": 0.3}
    assert values["core"] == {
        "month": "2026-09-01", "yoy": 2.6, "yoy_month": "2026-09-01", "yoy_prev": 2.4, "mom": 0.3, "ann3": 3.4,
        "sa_month": "2026-09-01", "level": "above", "momentum": "accelerating", "yoy_move": "up", "yoy_move_pp": 0.2}
    # 研究文件用沒四捨五入的 1.17 寫「整體高於核心 1.2」、核心年增月變「上升 0.1」；顯示值是 3.7 與 2.6、2.4 → 2.6：1.1、0.2
    assert values["aligned"] is True and values["gap"] == "headline_higher" and values["gap_pp"] == 1.1
    assert values["retrieved"] == "2026-10-24"
    assert mc.spec_ok(mc.INFL_SPEC, values)


def test_values_carry_only_numbers_codes_and_iso_dates():
    values = mc.cpi_values(saved_series(), date(2026, 10, 24))
    codes = (set(mc.INFL_LEVEL_CODES) | set(mc.INFL_MOMENTUM_CODES) | set(mc.INFL_MOVE_CODES) | set(mc.INFL_GAP_CODES))

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, str):
            assert node in codes or mc._ISO_DATE_RE.fullmatch(node), (key, node)
        else:
            assert node is None or isinstance(node, (bool, int, float)), (key, node)

    walk(values)


# 保存的真實月份：未四捨五入判與用 BLS 公布的一位小數判，結果不同的例子（審查 r9 s3-F3）
def test_rules_judge_the_displayed_one_decimal_not_the_raw_ratio():
    s = saved_series()
    nsa, cnsa = s["CUUR0000SA0"], s["CUUR0000SA0L1E"]
    raw = (D(nsa["2026-06"]) / D(nsa["2025-06"]) - 1) * 100
    assert raw > D("3.5") and mc.cpi_yoy(nsa, "2026-06") == D("3.5")  # 3.5314：顯示 3.5
    assert mc.infl_level(mc.cpi_yoy(nsa, "2026-06")) == "above"  # 差 1.5 → 高於，不是明顯高於（沒四捨五入會是 well_above）
    raw = (D(cnsa["2026-01"]) / D(cnsa["2025-01"]) - 1) * 100
    assert raw > D("2.5") and mc.cpi_yoy(cnsa, "2026-01") == D("2.5")  # 2.5037
    assert mc.infl_level(mc.cpi_yoy(cnsa, "2026-01")) == "slightly_above"  # 差 0.5 → 略高於，不是高於
    # 核心 2026-07 → 08：2.478 → 2.446，沒四捨五入差 −0.03 會判持平，但卡上寫的是 2.5% → 2.4%
    july, august = mc.cpi_yoy(cnsa, "2026-07"), mc.cpi_yoy(cnsa, "2026-08")
    assert (july, august) == (D("2.5"), D("2.4")) and mc.infl_move(august, july) == ("down", D("0.1"))


@pytest.mark.parametrize("which, month, yoy, previous, ann3, momentum, move", [
    # 沒四捨五入的年增減年化只差 0.458 → 持平，但卡上寫 2.9% 與 3.4%（差 0.5）
    ("cpi", "2025-08", "2.9", "2.7", "3.4", "accelerating", "up"),
    ("cpi", "2025-12", "2.7", "2.7", "2.2", "cooling", "flat"),  # 差 −0.456 沒四捨五入是持平；年增月變 −0.058 沒四捨五入是下降
    ("core", "2025-06", "2.9", "2.8", "2.4", "cooling", "up"),  # 差 −0.495 沒四捨五入是持平
    ("cpi", "2025-05", "2.4", "2.3", "1.2", "cooling", "up"),  # 年增月變 0.044 沒四捨五入是持平，但卡上寫 2.3% → 2.4%
    ("core", "2025-08", "3.1", "3.1", "3.5", "steady", "flat"),  # 年增月變 0.051 沒四捨五入是上升，但卡上寫 3.1% → 3.1%
])
def test_momentum_and_year_over_year_move_on_the_displayed_numbers_for_real_months(which, month, yoy, previous, ann3, momentum, move):
    s = saved_series()
    nsa, sa = ("CUUR0000SA0", "CUSR0000SA0") if which == "cpi" else ("CUUR0000SA0L1E", "CUSR0000SA0L1E")
    now_yoy, before = mc.cpi_yoy(s[nsa], month), mc.cpi_yoy(s[nsa], mc.month_shift(month, -1))
    now_ann3 = mc.cpi_ann3(s[sa], month)
    assert (now_yoy, before, now_ann3) == (D(yoy), D(previous), D(ann3))
    assert mc.infl_momentum(now_ann3, now_yoy) == momentum and mc.infl_move(now_yoy, before)[0] == move


@pytest.mark.parametrize("month, headline_level, core_level", [
    ("2026-03", "above", "above"), ("2026-04", "well_above", "above"), ("2026-05", "well_above", "above"),
    ("2026-06", "above", "above"), ("2026-07", "above", "slightly_above"), ("2026-08", "above", "slightly_above"),
    ("2026-09", "well_above", "above"),
])
def test_level_codes_for_the_saved_months(month, headline_level, core_level):
    s = saved_series()
    assert mc.infl_level(mc.cpi_yoy(s["CUUR0000SA0"], month)) == headline_level
    assert mc.infl_level(mc.cpi_yoy(s["CUUR0000SA0L1E"], month)) == core_level


@pytest.mark.parametrize("yoy, code", [
    ("-1.0", "not_above"), ("0.0", "not_above"), ("2.0", "not_above"), ("2.1", "slightly_above"), ("2.5", "slightly_above"),
    ("2.6", "above"), ("3.5", "above"), ("3.6", "well_above"), ("12.4", "well_above"),
])
def test_level_thresholds_are_inclusive_at_the_upper_edge(yoy, code):
    assert mc.infl_level(D(yoy)) == code  # 差 ≤0 不高於、≤0.5 略高於、≤1.5 高於、>1.5 明顯高於


@pytest.mark.parametrize("ann3, yoy, code", [
    ("4.2", "3.7", "accelerating"),  # 剛好 +0.5
    ("4.1", "3.7", "steady"), ("3.7", "3.7", "steady"), ("3.3", "3.7", "steady"),
    ("3.2", "3.7", "cooling"),  # 剛好 −0.5
    ("1.0", "3.4", "cooling"), ("9.9", "2.0", "accelerating"),
])
def test_momentum_exactly_half_a_point_counts(ann3, yoy, code):
    assert mc.infl_momentum(D(ann3), D(yoy)) == code


@pytest.mark.parametrize("headline, core, code, gap", [
    ("3.7", "2.6", "headline_higher", D("1.1")), ("3.1", "2.6", "headline_higher", D("0.5")),  # 剛好 0.5
    ("3.0", "2.6", "similar", D("0.4")), ("2.6", "3.1", "headline_lower", D("-0.5")), ("2.6", "3.0", "similar", D("-0.4")),
    ("2.6", "2.6", "similar", D("0.0")),
])
def test_headline_versus_core_gap(headline, core, code, gap):
    assert mc.infl_gap(D(headline), D(core)) == (code, gap)


@pytest.mark.parametrize("now, before, code, size", [
    ("3.7", "3.4", "up", D("0.3")), ("3.4", "3.7", "down", D("0.3")), ("3.4", "3.4", "flat", D("0.0")),
    ("2.4", "2.5", "down", D("0.1")),
])
def test_yoy_move(now, before, code, size):
    assert mc.infl_move(D(now), D(before)) == (code, size)


def test_round_half_up_not_bankers_rounding():
    base = {"2025-09": "100.000", "2026-09": "103.450"}  # 年增剛好 3.45 → 3.5（四捨五入到偶數會是 3.4）
    assert mc.cpi_yoy(base, "2026-09") == D("3.5")
    assert mc.cpi_yoy({"2025-09": "100.000", "2026-09": "103.350"}, "2026-09") == D("3.4")  # 3.35 → 3.4
    assert mc.cpi_yoy({"2025-09": "100.000", "2026-09": "99.950"}, "2026-09") == D("-0.1")  # −0.05 → −0.1（遠離 0）
    assert mc.cpi_mom({"2026-08": "100.000", "2026-09": "100.250"}, "2026-09") == D("0.3")


def test_calculation_does_not_use_or_change_the_global_decimal_context():
    series = saved_series()
    before = decimal.getcontext().prec
    with decimal.localcontext() as ctx:
        ctx.prec = 3
        ctx.rounding = decimal.ROUND_DOWN
        assert mc.cpi_yoy(series["CUUR0000SA0"], "2026-09") == D("3.7")
        assert mc.cpi_ann3(series["CUSR0000SA0"], "2026-09") == D("4.6")
    assert decimal.getcontext().prec == before


def test_float_inputs_are_read_by_their_shortest_text_not_their_binary_value():
    floats = {key: float(value) for key, value in saved_series()["CUUR0000SA0"].items()}
    assert mc.cpi_yoy(floats, "2026-09") == D("3.7") and mc.cpi_yoy(floats, "2026-06") == D("3.5")


@pytest.mark.parametrize("month, expected", [("2026-01", None), ("2026-02", "computed"), ("2026-03", "computed")])
def test_three_month_annualised_crosses_the_year_and_the_missing_october(month, expected):
    """t−3 跨年：2026-01 的 t−3 是 2025-10（缺值，BLS 停擺）→ 算不出；2026-02／03 用 2025-11／12。"""
    sa = saved_series()["CUSR0000SA0"]
    got = mc.cpi_ann3(sa, month)
    if expected is None:
        assert got is None
    else:
        base = mc.month_shift(month, -3)
        assert got == (((D(sa[month]) / D(sa[base])) ** 4 - 1) * 100).quantize(D("0.1"), rounding=decimal.ROUND_HALF_UP)


def test_missing_base_month_keeps_the_latest_month_with_a_year_over_year():
    """2026-10 的年增要 2025-10（缺值）：保留最近一個算得出年增的月份並標月份（R8）；月增與近 3 月年化照算（R9：各值帶自己的月份）。"""
    values = mc.cpi_values(grow(saved_series(), "2026-10"), date(2026, 11, 10))
    cpi = values["cpi"]
    assert cpi["month"] == "2026-10-01" and cpi["yoy"] == 3.7 and cpi["yoy_month"] == "2026-09-01"
    assert cpi["yoy_prev"] == 3.4 and cpi["yoy_move"] == "up" and cpi["level"] == "well_above"
    assert cpi["sa_month"] == "2026-10-01" and cpi["mom"] is not None and cpi["ann3"] is not None
    # 動能用「同一個月」的近 3 月年化與年增（2026-09 的 4.6 vs 3.7），不拿 10 月的年化去減 9 月的年增
    assert cpi["momentum"] == "accelerating" and values["aligned"] is True and values["gap"] == "headline_higher"
    assert mc.spec_ok(mc.INFL_SPEC, values)


def test_the_month_after_a_missing_base_skips_the_monthly_change_of_year_over_year():
    """2026-11：年增算得出（基期 2025-11 有值），但上月（2026-10）年增缺 → R3 跳過。"""
    values = mc.cpi_values(grow(saved_series(), "2026-11"), date(2026, 12, 10))
    for key in ("cpi", "core"):
        part = values[key]
        assert part["month"] == part["yoy_month"] == "2026-11-01" and part["yoy"] is not None
        assert part["yoy_prev"] is None and part["yoy_move"] is None and part["yoy_move_pp"] is None
        assert part["level"] is not None and part["momentum"] is not None
    assert mc.spec_ok(mc.INFL_SPEC, values)


def test_december_and_january_cross_the_year_in_every_formula():
    series = grow(saved_series(), "2027-01")
    for month in ("2026-12", "2027-01"):
        yoy = mc.cpi_yoy(series["CUUR0000SA0"], month)
        base = D(series["CUUR0000SA0"][mc.month_shift(month, -12)])
        assert yoy == ((D(series["CUUR0000SA0"][month]) / base - 1) * 100).quantize(D("0.1"), rounding=decimal.ROUND_HALF_UP)
    values = mc.cpi_values(series, date(2027, 2, 10))
    assert values["cpi"]["month"] == "2027-01-01" and values["cpi"]["yoy_prev"] is not None
    assert values["cpi"]["mom"] is not None and values["cpi"]["ann3"] is not None  # t−3＝2026-10 是合成的有值
    assert mc.spec_ok(mc.INFL_SPEC, values)


def test_months_that_do_not_line_up_publish_no_dependent_calculation():
    """四個序列最新月份要一樣才算動能與整體 vs 核心：這裡季調全項還在 8 月。"""
    series = saved_series()
    del series["CUSR0000SA0"]["2026-09"]
    values = mc.cpi_values(series, date(2026, 10, 24))
    assert values["aligned"] is False and values["gap"] is None and values["gap_pp"] is None
    assert values["cpi"]["momentum"] is None and values["core"]["momentum"] is None
    assert values["cpi"]["yoy"] == 3.7 and values["cpi"]["level"] == "well_above"  # 不依賴別的序列的照算
    assert values["cpi"]["sa_month"] == "2026-08-01" and values["cpi"]["month"] == "2026-09-01"  # 各值帶自己的月份
    assert mc.spec_ok(mc.INFL_SPEC, values)


def test_a_missing_core_series_leaves_the_core_part_empty():
    series = saved_series()
    del series["CUUR0000SA0L1E"], series["CUSR0000SA0L1E"]
    values = mc.cpi_values(series, date(2026, 10, 24))
    assert values["core"] is None and values["aligned"] is False and values["gap"] is None
    assert values["cpi"]["yoy"] == 3.7 and mc.spec_ok(mc.INFL_SPEC, values)


def test_without_the_headline_unadjusted_series_there_is_no_row():
    assert mc.cpi_values({}, date(2026, 10, 24)) is None
    series = saved_series()
    del series["CUUR0000SA0"]
    assert mc.cpi_values(series, date(2026, 10, 24)) is None
    assert mc.cpi_values({"CUUR0000SA0": {}}, date(2026, 10, 24)) is None


def test_an_implausible_year_over_year_is_treated_as_missing():
    nsa = {"2025-08": "300.000", "2025-09": "300.000", "2026-08": "310.000", "2026-09": "400.000"}  # 2026-09 年增 33%
    assert mc.cpi_yoy(nsa, "2026-09") is None and mc.cpi_yoy(nsa, "2026-08") == D("3.3")
    values = mc.cpi_values({"CUUR0000SA0": nsa}, date(2026, 10, 24))
    assert values["cpi"]["yoy_month"] == "2026-08-01" and values["cpi"]["month"] == "2026-09-01"


def test_zero_negative_or_missing_index_values_are_not_divided():
    assert mc.cpi_yoy({"2026-09": "0", "2025-09": "300"}, "2026-09") is None
    assert mc.cpi_yoy({"2026-09": "300", "2025-09": "0"}, "2026-09") is None
    assert mc.cpi_yoy({"2026-09": "300"}, "2026-09") is None
    assert mc.cpi_yoy({"2026-09": "abc", "2025-09": "300"}, "2026-09") is None
    assert mc.cpi_yoy({"2026-09": "٣٠٠", "2025-09": "300"}, "2026-09") is None
    assert mc.cpi_mom({"2026-09": True, "2026-08": 300}, "2026-09") is None
    assert mc.cpi_ann3(None, "2026-09") is None


def test_the_cpi_spec_rejects_free_text_and_wrong_types():
    good = mc.cpi_values(saved_series(), date(2026, 10, 24))
    assert mc.spec_ok(mc.INFL_SPEC, good)

    def with_change(mutate):
        values = json.loads(json.dumps(good))
        mutate(values)
        return mc.spec_ok(mc.INFL_SPEC, values)

    assert not with_change(lambda v: v["cpi"].update(level="very high"))
    assert not with_change(lambda v: v["cpi"].update(momentum="<b>x</b>"))
    assert not with_change(lambda v: v["core"].update(yoy_move="rising"))
    assert not with_change(lambda v: v.update(gap="big"))
    assert not with_change(lambda v: v["cpi"].update(month="2026-09"))  # 要 ISO 日期（月初）
    assert not with_change(lambda v: v["cpi"].update(yoy="3.7"))
    assert not with_change(lambda v: v.update(aligned="yes"))
    assert not with_change(lambda v: v.update(retrieved=20261024))
    assert not with_change(lambda v: v.pop("retrieved"))
    assert not with_change(lambda v: v["cpi"].pop("sa_month"))
    assert with_change(lambda v: v.update(core=None, gap=None, gap_pp=None))


def test_basis_text_is_a_fixed_sentence_without_forbidden_words():
    assert mc.TEXT_INFL_BASIS in mc.FIXED_TEXTS and not any(word in mc.TEXT_INFL_BASIS for word in mc.FORBIDDEN_WORDS)
    assert "目前" not in mc.TEXT_INFL_BASIS and not any(ch.isdigit() for ch in mc.TEXT_INFL_BASIS)  # 不寫年份


def test_cpi_stale_rule_is_month_end_plus_62_days():
    stale = mc.stale_after(mc.KIND_US_CPI, date(2026, 9, 30), {}, 0)
    assert stale == datetime(2026, 12, 1, tzinfo=TAIPEI).timestamp()  # 9 月底＋62 天，台北 0 點（和 FINRA 同一套）
    assert mc.stale_after(mc.KIND_US_CPI, date(2026, 9, 1), {}, 0) == stale  # 取該月月底


# ── 輪詢排程（collect 與 outcome 共用） ───────────────────────────────────────────


def et(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)


@pytest.mark.parametrize("day, expected", [
    (date(2026, 10, 7), "2026-08"), (date(2026, 10, 8), "2026-09"), (date(2026, 10, 24), "2026-09"),
    (date(2026, 11, 7), "2026-09"), (date(2026, 11, 8), "2026-10"), (date(2027, 1, 5), "2026-11"),
    (date(2027, 1, 8), "2026-12"), (date(2026, 3, 1), "2026-01"), (date(2026, 2, 5), "2025-12"), (date(2026, 2, 20), "2026-01"),
])
def test_expected_month_flips_on_the_eighth(day, expected):
    assert mc.cpi_expected_month(day) == expected


@pytest.mark.parametrize("stored, expected, lag", [
    ("2026-09", "2026-09", 0), ("2026-08", "2026-09", 1), ("2026-07", "2026-09", 2), ("2025-12", "2026-02", 2),
    ("2026-10", "2026-09", 0), (None, "2026-09", None),
])
def test_lag_months(stored, expected, lag):
    assert mc.cpi_lag_months(stored, expected) == lag


def test_stored_month_is_the_oldest_latest_month_of_the_four_series():
    series = {sid: {"2026-08": 1.0, "2026-09": 2.0} for sid in mc.CPI_SERIES}
    assert mc.cpi_stored_month(series) == "2026-09"
    series["CUSR0000SA0"] = {"2026-08": 1.0}
    assert mc.cpi_stored_month(series) == "2026-08"  # 有一個序列落後：整組只算到它
    del series["CUSR0000SA0"]
    assert mc.cpi_stored_month(series) is None  # 缺一個序列：當沒有（冷啟動）
    assert mc.cpi_stored_month({}) is None and mc.cpi_stored_month(None) is None and mc.cpi_stored_month("x") is None
    assert mc.cpi_stored_month({sid: {"bad": 1.0, "2026-13": 2.0} for sid in mc.CPI_SERIES}) is None


def poll(now: datetime, stored, last_probe=None, enabled=True):
    return mc.cpi_poll(now, stored, last_probe, enabled=enabled)


def test_poll_state_disabled_and_done_never_ask():
    assert poll(et(2026, 10, 14, 9), "2026-08", enabled=False) == mc.CpiPoll("disabled", False, "2026-09")
    assert poll(et(2026, 10, 14, 9), "2026-09") == mc.CpiPoll("done", False, "2026-09")
    assert poll(et(2026, 10, 14, 9), "2026-10") == mc.CpiPoll("done", False, "2026-09")
    assert poll(et(2026, 10, 14, 9), None, enabled=False).due is False


def test_cold_start_asks_at_any_time_but_not_again_within_a_day():
    saturday_night = et(2026, 10, 24, 3, 0)
    assert poll(saturday_night, None) == mc.CpiPoll("pending", True, "2026-09")
    assert poll(saturday_night, None, last_probe=saturday_night.timestamp() - 3600).due is False  # 剛問過、沒有進展
    assert poll(saturday_night, None, last_probe=saturday_night.timestamp() - 86400).due is True


@pytest.mark.parametrize("stored", ["2026-07", "2026-06", "2025-12"])
def test_more_than_one_month_behind_catches_up_at_once_like_a_cold_start(stored):
    sunday = et(2026, 10, 25, 2, 0)  # 日期（25 號）、星期與時段都不在輪詢範圍
    assert poll(sunday, stored) == mc.CpiPoll("pending", True, "2026-09")
    assert poll(sunday, stored, last_probe=sunday.timestamp() - 86399).due is False
    assert poll(sunday, stored, last_probe=sunday.timestamp() - 86400).due is True


def test_one_month_behind_is_the_normal_wait_and_is_gated_by_window_weekday_and_hour():
    stored = "2026-08"
    # 7 號：期待的是 8 月；已存 7 月＝上一輪（9 月的窗口）沒更新到 → 窗口外，每個平日 08:30 後最多問一次
    assert poll(et(2026, 10, 7, 9), "2026-07") == mc.CpiPoll("exhausted", True, "2026-08")
    for when in (et(2026, 10, 14, 8, 29), et(2026, 10, 14, 0, 0), et(2026, 10, 14, 23, 59), et(2026, 10, 10, 9), et(2026, 10, 11, 9)):
        assert poll(when, stored) == mc.CpiPoll("pending", False, "2026-09"), when  # 08:30 前、20:00 後、週末都不問
    assert poll(et(2026, 10, 14, 8, 30), stored) == mc.CpiPoll("pending", True, "2026-09")
    assert poll(et(2026, 10, 14, 19, 59), stored).due is True and poll(et(2026, 10, 14, 20, 0), stored).due is False
    assert poll(et(2026, 10, 8, 8, 30), stored).due is True and poll(et(2026, 10, 20, 12, 0), stored).due is True
    assert poll(et(2026, 10, 7, 12, 0), "2026-08").state == "done"


def test_inside_the_window_it_asks_every_two_hours():
    stored, now = "2026-08", et(2026, 10, 14, 10, 30)
    stamp = now.timestamp()
    assert poll(now, stored, last_probe=stamp - 7199).due is False
    assert poll(now, stored, last_probe=stamp - 7200).due is True
    assert poll(now, stored, last_probe=None).due is True


def test_after_the_window_a_late_release_is_still_probed_once_a_day():
    stored, now = "2026-08", et(2026, 10, 22, 9, 0)  # 22 號：窗口過了（8–20 號）
    assert poll(now, stored) == mc.CpiPoll("exhausted", True, "2026-09")
    assert poll(now, stored, last_probe=now.timestamp() - 7200) == mc.CpiPoll("exhausted", False, "2026-09")
    assert poll(now, stored, last_probe=now.timestamp() - 86400).due is True
    assert poll(et(2026, 10, 24, 9), stored).due is False  # 週六不問
    assert poll(et(2026, 10, 26, 7, 0), stored).due is False  # 08:30 前不問


def test_release_time_follows_daylight_saving_time():
    """CPI 08:30 美東：10/14（夏令）＝台北 20:30、11/10（冬令）＝台北 21:30；輪詢的 08:30 一律用美東時區算，不是台北固定時刻。"""
    def at_taipei(year, month, day, hour, minute):
        return datetime(year, month, day, hour, minute, tzinfo=TAIPEI).astimezone(EASTERN)

    assert at_taipei(2026, 10, 14, 20, 30) == et(2026, 10, 14, 8, 30) and at_taipei(2026, 11, 10, 21, 30) == et(2026, 11, 10, 8, 30)
    assert poll(at_taipei(2026, 10, 14, 20, 29), "2026-08").due is False and poll(at_taipei(2026, 10, 14, 20, 30), "2026-08").due is True
    assert poll(at_taipei(2026, 11, 10, 20, 30), "2026-09").due is False  # 冬令：台北 20:30 才是美東 07:30
    assert poll(at_taipei(2026, 11, 10, 21, 29), "2026-09").due is False and poll(at_taipei(2026, 11, 10, 21, 30), "2026-09").due is True
    assert poll(at_taipei(2027, 1, 13, 21, 30), "2026-11").due is True  # 1/13 公布 12 月資料
    # 11/1 換冬令：前後兩天的 08:30 美東相差一小時（台北）
    gap = datetime(2026, 11, 2, 8, 30, tzinfo=EASTERN).timestamp() - datetime(2026, 10, 30, 8, 30, tzinfo=EASTERN).timestamp()
    assert gap == 3 * 86400 + 3600


def test_the_poll_result_is_json_friendly_enumerations():
    got = poll(et(2026, 10, 14, 9), "2026-08")
    assert got.state in ("disabled", "done", "pending", "exhausted") and isinstance(got.due, bool) and got.expected == "2026-09"
