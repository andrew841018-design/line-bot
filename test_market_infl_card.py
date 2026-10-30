"""/盤勢 通膨 細節卡與重點摘要第 4 列（2026-10-30 批 2；ops/state/line_bot_market_dashboard_20261017/PLAN_b2_v2.md §1–§5、§7）的測試。

- 快照全是合成的（不連網、不放家人資料），時間一律注入。CPI 列儘量用 market_calc.cpi_values 從保存的真實 BLS 序列
  （test_market_calc_infl 的 BLS_YEARS）算出來，再接上合成的月份；手寫的只有「快照被改過或版本不一致」的壞值。
- 新的名字（_card_infl、_infl_read、TEXT_INFL_* …）都在測試裡才取：舊程式上只讓這些測試紅，不讓整個檔案載入失敗。
- 期望的句子照計畫寫死（S5：數字來自正式 9 月資料與 R1 邊界值），字樣一改這裡就紅。
- 只借 test_market_dashboard／test_market_calc_infl 的 helper 與 fixture，不借它們的 test_* 函式（借了會被收集第二次）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, datetime
from decimal import Decimal

import pytest

import card_pages
import market_calc
import output_validator
from test_market_calc_infl import grow, saved_series
from test_market_dashboard import (  # noqa: F401  md／paths 是 fixture（用名字被 pytest 找到）
    ALL_NOWS,
    DISCLAIMER,
    HERE,
    LONG_PAGE_URL,
    NOW,
    PLAN_FORBIDDEN,
    TPE,
    _never,
    _row,
    _variants,
    actions,
    assert_main_gate,
    digest_snapshot,
    make_snapshot,
    md,
    paths,
    reply_strings,
    size,
    texts,
    worst_snapshot,
    write_snapshot,
)

DAY = date(2026, 10, 16)  # make_snapshot(NOW) 的資料日（NOW 是 10/17 台北 16:30）；CPI 的取得日也是這天
TITLE = "通膨｜消費者物價與損益兩平"
BASIS = "BLS 指數：年增用未季調，月增與年化用季調，數字由程式計算"  # 原常數 TEXT_INFL_BASIS，值不能改（G13）
VOUCH = "資料自 BLS.gov 取得後，BLS.gov 無法擔保這些資料，也無法擔保由資料衍生的數字與分析"
ORIGINAL = ("BLS.gov cannot vouch for the data or analyses derived from these data after the data have been retrieved "
            "from BLS.gov.")
PCE = "聯準會的 2% 目標看的是 PCE（另一種物價指標），目前沒有 PCE 資料"
BE_NOTE = "損益兩平通膨＝債市隱含的未來平均通膨；名目與實質殖利率取自美國財政部，損益兩平由程式相減"
TACO_NOTES = (market_calc.TEXT_TACO_MEANING, market_calc.TEXT_TACO_SOURCE)
BUTTONS = [("期權與資金細節", "/盤勢 期權"), ("新聞與宏觀", "/盤勢 新聞"), ("通膨細節", "/盤勢 通膨")]
MOMENTUM_WORDS = ("加速", "持平", "降溫", "比年增快", "和年增差不多", "比年增慢")
# altText 不能有的字（B3）：「BLS」與判斷字
ALT_FORBIDDEN = ("BLS", "高於", "加速", "持平", "降溫", "快", "慢", "差不多", "升", "降")
# 數字都是正式 2026-09（BLS_YEARS 算出來的）：年增 3.7／核心 2.6、月增 0.6、近 3 月年化 4.6、上月年增 3.4；損益兩平是 make_snapshot 的
SUMMARY_A = "通膨：CPI 3.7%（9 月）明顯高於 2%，近 3 月加速"
CPI_A = [
    "消費者物價（CPI）年增 3.7%（9 月）",
    "明顯高於 2% 目標",
    "核心 CPI（不含食品能源）年增 2.6%（9 月）",
    "月增 0.6%（季調，9 月）",
    "近 3 月年化 4.6%（季調，9 月），比年增快",
    "年增由 8 月 3.4% 升到 9 月 3.7%",
    "整體比核心高 1.1 個百分點",
]
BE_A = ["5 年損益兩平通膨 2.45%（10/16）", "10 年損益兩平通膨 2.32%（10/16）", "5 年期比 10 年期高 13bp"]
DATES_INFL = "資料日：美股 10/16、台股 10/16、VIX 10/16、川普壓力指數 10/16"  # 第 4 列是通膨句：不列 10 年債（I1）
DATES_RATE = "資料日：美股 10/16、台股 10/16、VIX 10/16、10 年債 10/16、川普壓力指數 10/16"  # 退回：bbef843 的樣子


def bls_digest(day: str) -> str:
    return (f"通膨列的 CPI 指數取自美國勞工統計局（BLS），取得日 {day}；年增與「高於 2%」「加速」等判斷由程式計算，"
            "BLS 無法擔保取得後的資料，以及衍生的數字與分析")


def bls_card(day: str) -> str:
    return f"CPI 指數取自美國勞工統計局（BLS），取得日 {day}；卡上的數字與判斷都由程式計算"


# ── 快照 ─────────────────────────────────────────────────────────────────────
def real_values(series=None, retrieved=DAY) -> dict:
    """publish 的 cpi_values（保存的真實序列，可接上合成月份）；一定合 INFL_SPEC（job 寫快照用的同一份欄位表）。"""
    values = market_calc.cpi_values(saved_series() if series is None else series, retrieved)
    assert market_calc.spec_ok(market_calc.INFL_SPEC, values)
    return values


def october(head_mom: str, core_mom: str) -> dict:
    """情形 B：模擬 10 月公布（四個序列都到 2026-10，aligned）。未季調每月 +0.2%（2025-10 是缺值，所以 10 月年增算不出來），
    季調 10 月的月增由參數決定。seat1 sim_flip：整體 −0.3%、核心 +0.1% 時，10 月的年化和 9 月算的動能碼相反。"""
    series = grow(saved_series(), "2026-10")
    for sid, mom in (("CUSR0000SA0", head_mom), ("CUSR0000SA0L1E", core_mom)):
        points = series[sid]
        points["2026-10"] = str((Decimal(points["2026-09"]) * (1 + Decimal(mom))).quantize(Decimal("0.001")))
    return real_values(series, date(2026, 11, 10))


def with_cpi(values, snap=None, *, data_date=date(2026, 9, 30), now=NOW) -> dict:
    snap = digest_snapshot() if snap is None else snap
    snap["rows"]["us.infl.cpi"] = _row(values, now, data_date)
    return snap


def changed(**changes) -> dict:
    """正式 9 月的 values，改幾個欄位（「cpi.yoy」這種寫法改巢狀的欄位）。"""
    values = real_values()
    for key, value in changes.items():
        target = values
        *parents, leaf = key.split("__")
        for parent in parents:
            target = target[parent]
        target[leaf] = value
    return values


def old_snapshot(now=NOW) -> dict:
    snap = digest_snapshot() if now == NOW else make_snapshot(now)
    del snap["rows"]["us.infl.cpi"], snap["rows"]["us.infl.be"]
    return snap


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def digest(md, snap, now=NOW):
    """重點摘要第一張：(六列, footer 免責句之前的註記, 泡泡, ctx)。直接呼叫 builder：丟例外就是失敗。"""
    ctx = md._Ctx(snap, now)
    bubble = md._card_digest(ctx, "重點摘要（1/5）", md.TEXT_SWIPE)
    footer = texts(bubble["footer"])
    return [node["text"] for node in bubble["body"]["contents"]], footer[: footer.index(DISCLAIMER)], bubble, ctx


def card(md, snap, now=NOW):
    """/盤勢 通膨：(Reply, 標題, 內文各列, footer 免責句之前的註記, ctx)。直接呼叫 builder，ctx 用來看 dropped。"""
    ctx = md._Ctx(snap, now)
    reply = md._card_infl(ctx)
    bubble = reply.flex
    footer = texts(bubble["footer"])
    body = [node["text"] for node in bubble["body"]["contents"] if node.get("type") == "text"]
    return reply, bubble["header"]["contents"][0]["text"], body, footer[: footer.index(DISCLAIMER)], ctx


def fallback(md, snap, now=NOW):
    """退回時的第 4 列與 footer：bbef843 的 _digest_rate_row 與 _digest_notes（infl 預設 None），逐字不變。"""
    ctx = md._Ctx(snap, now)
    return md._digest_rate_row(ctx)[0], md._digest_notes(ctx)


# ── 指令、SHIPPED、按鈕（§5、Q-B2） ────────────────────────────────────────────
@pytest.mark.parametrize("body", [" 通膨", "通膨", " 通膨？", "　通膨 ", " 通​膨"])
def test_the_inflation_word_is_a_command(md, body):
    assert md.parse_command(body) == (md.INFL, None)


@pytest.mark.parametrize("body", [" CPI", " cpi", " 物價", " 通膨 細節", " CPI 圖", " 通膨圖", " 核心通膨"])
def test_no_aliases_only_the_one_word(md, paths, body):
    """Andrew 10/29 定案：不加別名，只認「通膨」。其他字照舊回用法說明。"""
    assert md.parse_command(body) is None
    write_snapshot(paths, make_snapshot(NOW))
    assert md.handle(body, now=NOW, paths=paths).text == md.TEXT_USAGE


def test_the_shipped_list_the_usage_text_and_the_rows_it_reads(md):
    assert md.SHIPPED_MARKET_TEXTS[-1] == "/盤勢 通膨" and md.SHIPPED_MARKET_TEXTS.count("/盤勢 通膨") == 1
    assert "/盤勢 通膨" in md.TEXT_USAGE and md.TEXT_USAGE.splitlines()[1].startswith("/盤勢 → ")  # 第 2 行照舊
    assert {"us.infl.cpi", "us.infl.be"} <= set(md.READ_ROWS) <= set(market_calc.ROW_IDS)
    assert md._DETAILS[md.INFL] is not None


def test_the_digest_buttons_are_the_same_with_or_without_inflation_numbers(md):
    """摘要的按鈕：期權與資金細節、新聞與宏觀、通膨細節（拿掉「台股細節」，台股卡自己還有）。退回 10 年債時也一樣。"""
    for snap in (digest_snapshot(), old_snapshot()):
        _rows, _notes, bubble, _ctx = digest(md, snap)
        assert [(a["label"], a["text"]) for a in actions(bubble)] == BUTTONS
        assert all(text in md.SHIPPED_MARKET_TEXTS for _label, text in BUTTONS)


# ── 情形 A：正式 2026-09，四序列對齊 ─────────────────────────────────────────────
def test_case_a_digest_row_and_footer(md):
    snap = with_cpi(real_values())
    rows, notes, bubble, ctx = digest(md, snap)
    assert rows[3] == SUMMARY_A and md.row_width(SUMMARY_A) == 23.0
    assert bubble["body"]["contents"][3].get("color") == md.GRAY  # 顏色和以前一樣（不用顏色表達好壞）
    assert notes == [DATES_INFL, bls_digest("10/16"), ORIGINAL, *TACO_NOTES]
    assert ctx.dropped == 0 and ctx.render_errors == []


def test_case_a_detail_card(md, paths):
    snap = with_cpi(real_values())
    reply, title, body, notes, ctx = card(md, snap)
    assert title == TITLE and body == [*CPI_A, *BE_A]
    assert notes == [BASIS, bls_card("10/16"), VOUCH, ORIGINAL, PCE, BE_NOTE]
    assert reply.alt_text == "盤勢：通膨 CPI 年增 3.7%（9 月）" and reply.render_errors == ()
    assert not actions(reply.flex)  # 細節卡不放按鈕（Andrew 定案）
    assert ctx.dropped == 0 and all(md.row_width(line) <= 24 and "…" not in line for line in body)
    write_snapshot(paths, snap)
    sent = md.handle(" 通膨", now=NOW, paths=paths)
    assert sent.flex == reply.flex and sent.alt_text == reply.alt_text
    assert_main_gate(sent)


def test_the_make_snapshot_fixture_reads_the_same_as_the_real_values(md):
    """test_market_dashboard 的 make_snapshot 手寫的 CPI 列（3.7／2.6…）和 publish 算的一樣：其他檔案的期望字串用它。"""
    assert digest(md, digest_snapshot())[0][3] == SUMMARY_A
    assert card(md, digest_snapshot())[2] == [*CPI_A, *BE_A]


# ── 情形 B、C、D：基期缺值、沒對齊 ───────────────────────────────────────────────
@pytest.mark.parametrize("head_mom, core_mom, mom_line, ann3_line", [
    ("-0.003", "0.001", "月增 -0.3%（季調，10 月）", "近 3 月年化 3.0%（季調，10 月）"),  # 整體、核心的動能碼都和 10 月的年化相反
    ("0", "0", "月增 0.0%（季調，10 月）", "近 3 月年化 4.3%（季調，10 月）"),  # 邊界：整體還一致、核心已經不一致
])
def test_case_b_the_base_gap_month_hides_the_momentum_and_labels_october(md, head_mom, core_mom, mom_line, ann3_line):
    values = october(head_mom, core_mom)
    assert values["aligned"] is True and values["cpi"]["momentum"] == "accelerating"  # publish 存的是 9 月算的碼
    snap = with_cpi(values, data_date=date(2026, 10, 31))
    rows, notes, _bubble, _ctx = digest(md, snap)
    assert rows[3] == "通膨：CPI 3.7%（9 月）明顯高於 2%"  # 沒有動能半句（C1）
    assert notes == [DATES_INFL, bls_digest("11/10"), ORIGINAL, "10 月年增算不出來，先列 9 月", *TACO_NOTES]
    _reply, _title, body, card_notes, _ctx = card(md, snap)
    assert body == [CPI_A[0], CPI_A[1], CPI_A[2], mom_line, ann3_line, CPI_A[5], CPI_A[6], *BE_A]
    assert card_notes == [BASIS, bls_card("11/10"), VOUCH, ORIGINAL, PCE, "10 月年增算不出來，先列 9 月", BE_NOTE]
    assert not any(word in line for line in [rows[3], *body] for word in MOMENTUM_WORDS)


def test_case_c_unaligned_with_a_base_gap(md):
    """只有全項未季調前進到 10 月（另外 3 個序列還在 9 月）：aligned=False，沒有動能、沒有整體比核心；全部都標 9 月。"""
    series = saved_series()
    nsa = series["CUUR0000SA0"]
    nsa["2026-10"] = str((Decimal(nsa["2026-09"]) * Decimal("1.002")).quantize(Decimal("0.001")))
    values = real_values(series, date(2026, 11, 10))
    assert values["aligned"] is False and values["cpi"]["month"] == "2026-10-01"
    snap = with_cpi(values, data_date=date(2026, 10, 31))
    rows, notes, _bubble, _ctx = digest(md, snap)
    assert rows[3] == "通膨：CPI 3.7%（9 月）明顯高於 2%"
    assert "10 月年增算不出來，先列 9 月" in notes
    body = card(md, snap)[2]
    assert body == [CPI_A[0], CPI_A[1], CPI_A[2], "月增 0.6%（季調，9 月）", "近 3 月年化 4.6%（季調，9 月）", CPI_A[5], *BE_A]


def test_case_d_unaligned_core_carries_its_own_month(md):
    """一般月份的 aligned=False（全項未季調到 12 月，其餘 11 月）：整體 12 月、核心與季調 11 月，各自標月份（G4、I4）。"""
    series = grow(saved_series(), "2026-12")
    for sid in market_calc.CPI_SERIES[1:]:
        del series[sid]["2026-12"]
    values = real_values(series, date(2027, 1, 14))
    snap = with_cpi(values, data_date=date(2026, 12, 31))
    rows, notes, _bubble, _ctx = digest(md, snap)
    assert rows[3] == "通膨：CPI 4.6%（12 月）明顯高於 2%" and not any("算不出來" in note for note in notes)
    assert card(md, snap)[2] == [
        "消費者物價（CPI）年增 4.6%（12 月）",
        "明顯高於 2% 目標",
        "核心 CPI（不含食品能源）年增 3.1%（11 月）",
        "月增 0.2%（季調，11 月）",
        "近 3 月年化 4.3%（季調，11 月）",
        "年增由 11 月 4.4% 升到 12 月 4.6%",
        *BE_A,
    ]


# ── E／E2、G1、R1 邊界 ──────────────────────────────────────────────────────────
def test_case_e_flat_similar_and_zero(md):
    values = changed(cpi__yoy=2.0, cpi__yoy_prev=2.0, cpi__yoy_move="flat", cpi__yoy_move_pp=0.0, cpi__level="not_above",
                     cpi__momentum="steady", cpi__mom=0.0, cpi__ann3=2.0, core__yoy=2.0, gap="similar", gap_pp=0.0)
    snap = with_cpi(values)
    snap["rows"]["us.infl.be"]["values"].update(spread_bp=3, spread="similar")
    assert digest(md, snap)[0][3] == "通膨：CPI 2.0%（9 月）不高於 2%，近 3 月持平"
    assert card(md, snap)[2] == [
        "消費者物價（CPI）年增 2.0%（9 月）",
        "不高於 2% 目標",
        "核心 CPI（不含食品能源）年增 2.0%（9 月）",
        "月增 0.0%（季調，9 月）",
        "近 3 月年化 2.0%（季調，9 月），和年增差不多",
        "年增和 8 月一樣，都是 2.0%",
        "整體和核心差不多",
        *BE_A[:2],
        "5 年期和 10 年期差不多",
    ]


def test_case_e2_negative_zero_never_shows(md, paths):
    """−0.0 與 −0.04 四捨五入後是 0：寫 0.0、0.00，不寫 -0.0（G6）；真的負數（−0.05 → −0.1）照樣保留。"""
    values = changed(cpi__yoy=-0.04, cpi__yoy_prev=-0.0, cpi__yoy_move="flat", cpi__level="not_above", cpi__mom=-0.0,
                     cpi__ann3=-0.04, core__yoy=-0.0, gap="similar", gap_pp=-0.0)
    snap = with_cpi(values)
    snap["rows"]["us.infl.be"]["values"]["y5"]["value"] = -0.004
    snap["rows"]["us.infl.be"]["values"]["y10"]["value"] = -0.0
    rows, notes, _bubble, _ctx = digest(md, snap)
    reply, _title, body, card_notes, _ctx = card(md, snap)
    assert rows[3] == "通膨：CPI 0.0%（9 月）不高於 2%，近 3 月加速"
    assert body[:6] == ["消費者物價（CPI）年增 0.0%（9 月）", "不高於 2% 目標", "核心 CPI（不含食品能源）年增 0.0%（9 月）",
                        "月增 0.0%（季調，9 月）", "近 3 月年化 0.0%（季調，9 月），比年增快", "年增和 8 月一樣，都是 0.0%"]
    assert "5 年損益兩平通膨 0.00%（10/16）" in body and "10 年損益兩平通膨 0.00%（10/16）" in body
    assert reply.alt_text == "盤勢：通膨 CPI 年增 0.0%（9 月）"
    for value in [*rows, *notes, *body, *card_notes, reply.alt_text]:
        assert not re.search(r"-0\.0+(?!\d*[1-9])", value), value
    snap = with_cpi(changed(cpi__mom=-0.05))
    assert "月增 -0.1%（季調，9 月）" in card(md, snap)[2]


def test_g1_a_falling_year_over_year_is_written_by_its_code(md):
    """yoy_move_pp 是無號的：方向由碼決定、數字是兩端點（3.7 → 3.4 寫「降到」，不會變成 +0.3）。"""
    snap = with_cpi(changed(cpi__yoy=3.4, cpi__yoy_prev=3.7, cpi__yoy_move="down", cpi__level="above", gap_pp=0.8))
    body = card(md, snap)[2]
    assert "年增由 8 月 3.7% 降到 9 月 3.4%" in body and "整體比核心高 0.8 個百分點" in body
    assert digest(md, snap)[0][3] == "通膨：CPI 3.4%（9 月）高於 2%，近 3 月加速"


@pytest.mark.parametrize("yoy, word", [(2.0, "不高於 2%"), (2.5, "略高於 2%"), (3.5, "高於 2%"), (3.6, "明顯高於 2%")])
def test_r1_boundaries_in_full_sentences(md, yoy, word):
    level = market_calc.infl_level(Decimal(str(yoy)))  # publish 的分界（上緣含等號）
    snap = with_cpi(changed(cpi__yoy=yoy, cpi__level=level, cpi__yoy_prev=None, cpi__yoy_move=None, cpi__yoy_move_pp=None))
    assert digest(md, snap)[0][3] == f"通膨：CPI {yoy:.1f}%（9 月）{word}，近 3 月加速"
    assert card(md, snap)[2][:2] == [f"消費者物價（CPI）年增 {yoy:.1f}%（9 月）", f"{word} 目標"]


# ── G5：月份規則（年增那個月＝最新月，或它的上一個月） ─────────────────────────────────
def test_g5_january_takes_december_from_the_real_series(md):
    """拿掉 2026-01 的未季調基期、四個序列延長到 2027-01：年增落在 2026-12，1 月對上一年 12 月要接受（年×12＋月）。"""
    series = grow(saved_series(), "2027-01")
    for sid in (market_calc.CPI_NSA, market_calc.CORE_NSA):
        del series[sid]["2026-01"]
    values = real_values(series, date(2027, 2, 12))
    assert values["cpi"]["month"] == "2027-01-01" and values["cpi"]["yoy_month"] == "2026-12-01"
    snap = with_cpi(values, data_date=date(2027, 1, 31))
    rows, notes, _bubble, _ctx = digest(md, snap)
    assert rows[3] == "通膨：CPI 4.6%（12 月）明顯高於 2%"  # 季調是 1 月、年增是 12 月：動能不寫
    assert "1 月年增算不出來，先列 12 月" in notes
    assert "年增由 11 月 4.4% 升到 12 月 4.6%" in card(md, snap)[2]


@pytest.mark.parametrize("month, yoy_month", [
    ("2027-01-01", "2026-11-01"),  # 差兩個月
    ("2026-10-01", "2025-09-01"),  # 差 13 個月（N2(b)：月份看起來一樣、其實是去年）
    ("2027-01-01", "2027-02-01"),  # 年增那個月比最新月還新
    ("2026-09-01", "2026-07-01"),
])
def test_g5_other_month_pairs_make_cpi_unusable(md, month, yoy_month):
    snap = with_cpi(changed(cpi__month=month, cpi__yoy_month=yoy_month))
    rows, notes, _bubble, _ctx = digest(md, snap)
    assert (rows[3], notes) == fallback(md, snap)
    assert card(md, snap)[2][0] == "消費者物價（CPI）：暫缺"


# ── 壞值：G4、G7、G8、G12、N5 ────────────────────────────────────────────────────
@pytest.mark.parametrize("changes", [
    {"cpi__month": None}, {"cpi__yoy_month": None}, {"cpi__month": "2026-09"}, {"cpi__yoy_month": "bogus"},  # G4、N4
    {"retrieved": None}, {"retrieved": "10/16"}, {"retrieved": "2026-10-16T08:00"},  # N5、B5：沒有取得日不顯示 BLS 數字
    {"cpi__yoy": None}, {"cpi__yoy": 25.1}, {"cpi__yoy": -5.1}, {"cpi__yoy": 1e14}, {"cpi__yoy": "3.7"},  # G7
    {"cpi": None}, {"cpi": "x"},
])
def test_an_unusable_cpi_row_falls_back_and_has_no_bls_lines(md, changes):
    snap = with_cpi(changed(**changes))
    rows, notes, bubble, ctx = digest(md, snap)
    assert (rows[3], notes) == fallback(md, snap)
    reply, title, body, card_notes, card_ctx = card(md, snap)
    assert title == TITLE and body == ["消費者物價（CPI）：暫缺", *BE_A] and card_notes == [BE_NOTE]
    assert reply.alt_text == "盤勢：通膨，CPI 暫無數字"
    assert not any("BLS" in text for text in [*texts(bubble), *texts(reply.flex), reply.alt_text])
    assert ctx.render_errors == card_ctx.render_errors == [] and ctx.dropped == card_ctx.dropped == 0


def test_g4_the_momentum_needs_both_months_and_the_lines_need_the_seasonal_month(md):
    snap = with_cpi(changed(cpi__sa_month=None))  # 動能碼還在，但季調月份讀不到
    assert digest(md, snap)[0][3] == "通膨：CPI 3.7%（9 月）明顯高於 2%"
    assert card(md, snap)[2] == [*CPI_A[:3], *CPI_A[5:], *BE_A]  # 月增、年化兩列都不寫
    snap = with_cpi(changed(cpi__sa_month="2026-08-01"))  # 季調比年增舊一個月：數字照寫、標 8 月，不寫動能
    body = card(md, snap)[2]
    assert "月增 0.6%（季調，8 月）" in body and "近 3 月年化 4.6%（季調，8 月）" in body
    assert digest(md, snap)[0][3] == "通膨：CPI 3.7%（9 月）明顯高於 2%"


def without(*indexes: int) -> list[str]:
    return [line for index, line in enumerate(CPI_A) if index not in indexes]


NO_CORE = [*CPI_A[:2], "核心 CPI：暫缺", *CPI_A[3:6]]  # 核心不可用：寫暫缺，整體比核心也不寫


@pytest.mark.parametrize("changes, cpi_lines", [
    ({"cpi__mom": 10.1}, without(3)),  # 讀值給明確範圍（G7）：超過就當缺值，只少那一列
    ({"cpi__ann3": 100.1}, without(4)),
    ({"cpi__ann3": -50.1}, without(4)),
    ({"cpi__yoy_prev": 25.1}, without(5)),
    ({"gap_pp": 30.1}, without(6)),
    ({"core__yoy": 25.1}, NO_CORE),
    ({"core": None}, NO_CORE),
    ({"core__yoy_month": None}, NO_CORE),
])
def test_g7_out_of_range_numbers_drop_only_their_line(md, changes, cpi_lines):
    assert card(md, with_cpi(changed(**changes)))[2] == [*cpi_lines, *BE_A]


def test_g7_breakeven_values_out_of_range(md):
    snap = with_cpi(real_values())
    be = snap["rows"]["us.infl.be"]["values"]
    be["y5"]["value"] = 100.01
    be["spread_bp"] = 20001
    assert card(md, snap)[2] == [*CPI_A, BE_A[1]]


@pytest.mark.parametrize("changes, cpi_lines", [
    ({"cpi__yoy_prev": 3.9}, without(5)),  # 碼是 up，但上月比較高（G8：碼和數字對不上，整句不寫）
    ({"cpi__yoy_prev": 3.7}, without(5)),  # 碼是 up，兩個數字一樣
    ({"cpi__yoy_prev": 3.66}, without(5)),  # 顯示成 3.7＝3.7：在印出來的一位小數上比
    ({"cpi__yoy_move": "flat"}, without(5)),  # flat 但數字不同
    ({"cpi__yoy_move": "down"}, without(5)),
    ({"gap_pp": -1.1}, without(6)),  # headline_higher 配負數
    ({"gap": "headline_lower"}, without(6)),
    ({"gap_pp": 0.0}, without(6)),
    ({"aligned": False}, without(6)),  # 沒對齊卻有 gap（版本不一致）
    ({"aligned": "yes"}, without(6)),
    # 核心的年增是 8 月（它自己的基期缺值）：核心照寫並標 8 月，但和整體不是同一個月，整體比核心不寫
    ({"core__yoy_month": "2026-08-01"}, [*CPI_A[:2], "核心 CPI（不含食品能源）年增 2.6%（8 月）", *CPI_A[3:6]]),
])
def test_g8_a_code_that_disagrees_with_the_numbers_drops_the_sentence(md, changes, cpi_lines):
    assert card(md, with_cpi(changed(**changes)))[2] == [*cpi_lines, *BE_A]


def test_g8_breakeven_spread_code_and_dates(md):
    for change in ({"spread_bp": -13}, {"spread": "five_lower"}, {"spread_bp": 0}):
        snap = with_cpi(real_values())
        snap["rows"]["us.infl.be"]["values"].update(change)
        assert card(md, snap)[2] == [*CPI_A, *BE_A[:2]], change
    snap = with_cpi(real_values())
    snap["rows"]["us.infl.be"]["values"]["y5"]["date"] = "2026-10-15"  # 兩個天期不同天：利差不寫
    assert card(md, snap)[2] == [*CPI_A, "5 年損益兩平通膨 2.45%（10/15）", BE_A[1]]
    snap = with_cpi(real_values())
    snap["rows"]["us.infl.be"]["values"].update(spread_bp=-13, spread="five_lower")
    assert card(md, snap)[2][-1] == "5 年期比 10 年期低 13bp"  # 數字取絕對值、方向由碼決定


@pytest.mark.parametrize("changes, row, cpi_lines", [
    ({"cpi__level": "very_high"}, "通膨：CPI 3.7%（9 月）", without(1)),  # R1 不認得：只有數字，不印原始的碼（G12）
    ({"cpi__level": None}, "通膨：CPI 3.7%（9 月）", without(1)),
    # 動能碼不認得：摘要用去動能的寫法；細節卡年化的數字照寫，只拿掉比較句
    ({"cpi__momentum": "rising"}, "通膨：CPI 3.7%（9 月）明顯高於 2%",
     [*CPI_A[:4], "近 3 月年化 4.6%（季調，9 月）", *CPI_A[5:]]),
    ({"cpi__yoy_move": "rising"}, SUMMARY_A, without(5)),
    ({"gap": "big"}, SUMMARY_A, without(6)),
])
def test_g12_unknown_codes_drop_only_their_part(md, changes, row, cpi_lines):
    snap = with_cpi(changed(**changes))
    assert digest(md, snap)[0][3] == row
    assert card(md, snap)[2] == [*cpi_lines, *BE_A]


def test_a_stale_cpi_row_with_no_data_date(md):
    snap = with_cpi(real_values(), data_date=None)
    snap["rows"]["us.infl.cpi"]["stale_after"] = NOW - 1
    assert card(md, snap)[2][0] == "消費者物價（CPI）：過期"


# ── 舊快照、狀態表（G3）、一律回 Flex（P8） ──────────────────────────────────────────
def test_an_old_snapshot_without_the_inflation_rows(md, paths):
    snap = old_snapshot()
    rows, notes, _bubble, _ctx = digest(md, snap)
    assert rows[3] == "利率：10 年債 5.09%（+12bp）" and notes == [DATES_RATE, *TACO_NOTES]
    assert (rows[3], notes) == fallback(md, snap)
    write_snapshot(paths, snap)
    reply = md.handle(" 通膨", now=NOW, paths=paths)
    assert reply.flex is not None and reply.alt_text == "盤勢：通膨資料暫缺" and reply.render_errors == ()
    assert texts(reply.flex["body"]) == ["消費者物價（CPI）：暫缺", "損益兩平通膨：暫缺"]
    assert texts(reply.flex["footer"]) == [DISCLAIMER]
    assert_main_gate(reply)


CPI_STATES = ("usable", "unusable", "stale", "missing")
BE_STATES = ("fresh", "stale", "missing")


def state_snapshot(cpi_state: str, be_state: str, now=NOW) -> dict:
    snap = make_snapshot(now)
    rows = snap["rows"]
    if cpi_state == "unusable":
        del rows["us.infl.cpi"]["values"]["retrieved"]
    elif cpi_state == "stale":
        rows["us.infl.cpi"]["stale_after"] = now - 1
    elif cpi_state == "missing":
        rows["us.infl.cpi"] = _never()
    if be_state == "stale":
        rows["us.infl.be"]["stale_after"] = now - 1
    elif be_state == "missing":
        rows["us.infl.be"] = _never()
    return snap


@pytest.mark.parametrize("be_state", BE_STATES)
@pytest.mark.parametrize("cpi_state", CPI_STATES)
def test_the_detail_card_state_table(md, paths, cpi_state, be_state):
    """CPI 四種 × 損益兩平三種：標題固定、altText 與 notes 跟著兩段有沒有數字走、每列 ≤24、沒有 None、ctx 沒丟字、過 main 閘門。"""
    snap = state_snapshot(cpi_state, be_state)
    reply, title, body, notes, ctx = card(md, snap)
    cpi_part = {
        "usable": CPI_A,
        "unusable": ["消費者物價（CPI）：暫缺"],
        "stale": ["消費者物價（CPI）：過期，資料到 9 月"],
        "missing": ["消費者物價（CPI）：暫缺"],
    }[cpi_state]
    be_part = {"fresh": BE_A, "stale": ["損益兩平通膨：過期（資料日 10/16）"], "missing": ["損益兩平通膨：暫缺"]}[be_state]
    assert title == TITLE and body == [*cpi_part, *be_part]
    cpi_notes = [BASIS, bls_card("10/16"), VOUCH, ORIGINAL, PCE] if cpi_state == "usable" else []
    assert notes == cpi_notes + ([BE_NOTE] if be_state == "fresh" else [])
    if cpi_state == "usable":
        assert reply.alt_text == "盤勢：通膨 CPI 年增 3.7%（9 月）"
    else:
        assert reply.alt_text == ("盤勢：通膨，CPI 暫無數字" if be_state == "fresh" else "盤勢：通膨資料暫缺")
    strings = reply_strings(reply)
    assert not any("None" in value or "{" in value for value in strings)
    assert all(md.row_width(line) <= 24 and "…" not in line for line in body)
    assert ctx.dropped == 0 and reply.render_errors == ()
    write_snapshot(paths, snap)
    sent = md.handle(" 通膨", now=NOW, paths=paths)
    assert sent.flex is not None and sent.alt_text == reply.alt_text  # P8：連全部暫缺也是 Flex
    assert_main_gate(sent)


# ── 單一判斷（B7）、退回（G2）、壞掉要看得到（P2）、altText（B3） ──────────────────────────
def b7_snapshots(now):
    stale, half = _variants(now)["stale"], _variants(now)["half_never"]
    gap = make_snapshot(now)
    gap["rows"]["us.infl.cpi"] = _row(october("-0.003", "0.001"), now, date(2026, 10, 31))
    no_day = make_snapshot(now)
    del no_day["rows"]["us.infl.cpi"]["values"]["retrieved"]
    return {"normal": make_snapshot(now), "stale": stale, "half_never": half, "old": old_snapshot(now), "base_gap": gap,
            "no_retrieved": no_day}


def retrieved_md(snap) -> str | None:
    row = snap["rows"].get("us.infl.cpi") or {}
    value = (row.get("values") or {}).get("retrieved")
    return f"{int(value[5:7])}/{int(value[8:10])}" if isinstance(value, str) else None


@pytest.mark.parametrize("now", ALL_NOWS, ids=["2026-10-17", "2027-01-15", "2028-01-03"])
def test_one_judgement_decides_every_cpi_number_and_every_bls_line(md, paths, now):
    """gp2 B7：同一個判斷決定第 4 列、摘要的兩行 BLS 句、細節卡 CPI 段、細節卡的 BLS 行、altText 的數字。逐泡泡：
    「有 CPI 衍生的數字」⇔「有含取得日的 BLS 行＋英文原句」，取得日等於 values.retrieved；沒有 CPI 數字的泡泡沒有「BLS」。"""
    at = datetime.fromtimestamp(now, TPE)
    for name, snap in b7_snapshots(now).items():
        write_snapshot(paths, snap)
        day = retrieved_md(snap)
        summary, detail = md.handle("", now=now, paths=paths), md.handle(" 通膨", now=now, paths=paths)
        assert summary.render_errors == detail.render_errors == (), name
        first, *others = summary.flex["contents"]
        has_number = texts(first["body"])[3].startswith("通膨：CPI")
        footer = texts(first["footer"])
        assert has_number == (bls_digest(day) in footer and ORIGINAL in footer if day else False), name
        assert has_number or not any("BLS" in text for text in texts(first)), name
        assert not any("BLS" in text for bubble in others for text in texts(bubble)), name  # 其他四張沒有 BLS 數字
        card_texts = texts(detail.flex)
        card_number = any(text.startswith("消費者物價（CPI）年增") for text in card_texts)
        assert card_number == has_number, name  # 兩張卡用同一個判斷：一張有數字、另一張沒有不會發生
        assert card_number == (bls_card(day) in card_texts and VOUCH in card_texts and ORIGINAL in card_texts
                               if day else False), name
        assert card_number or not any("BLS" in text for text in card_texts), name
        assert ("CPI 年增" in detail.alt_text) == card_number, name
        for alt in (summary.alt_text, detail.alt_text):
            assert alt.startswith("盤勢：") and not any(word in alt for word in ALT_FORBIDDEN), (name, alt)
        for value in [*reply_strings(summary), *reply_strings(detail)]:
            result = output_validator.validate_outbound_text(value, now=at)
            assert result.ok and result.text == value, (name, value, result.reason)


def test_the_summary_alt_text_never_carries_cpi(md):
    """輪播的 altText 不放 CPI（B3）：有沒有 us.infl.* 都逐字相同。"""
    for now in ALL_NOWS:
        snap = make_snapshot(now)
        with_rows = md._summary_alt(md._Ctx(snap, now))
        del snap["rows"]["us.infl.cpi"], snap["rows"]["us.infl.be"]
        assert md._summary_alt(md._Ctx(snap, now)) == with_rows
        assert "CPI" not in with_rows and "BLS" not in with_rows and "通膨" not in with_rows


def test_a_crash_while_reading_falls_back_word_for_word_and_is_recorded(md, paths, monkeypatch):
    """_infl_view 接住例外：第 4 列與 footer 和退回版（bbef843）逐字相同（G2），只記「_infl_view:型別名」（P2），不記訊息、數字。"""
    snap = digest_snapshot()
    expected_row, expected_notes = fallback(md, snap)

    def boom(_ctx):
        raise RuntimeError("secret 3.7 /盤勢")

    monkeypatch.setattr(md, "_infl_read", boom)
    rows, notes, bubble, ctx = digest(md, snap)
    assert (rows[3], notes) == (expected_row, expected_notes) and rows[3].startswith("利率：")
    assert ctx.render_errors == ["_infl_view:RuntimeError"]
    assert [(a["label"], a["text"]) for a in actions(bubble)] == BUTTONS
    write_snapshot(paths, snap)
    summary = md.handle("", now=NOW, paths=paths)
    assert summary.flex is not None and "_infl_view:RuntimeError" in summary.render_errors
    detail = md.handle(" 通膨", now=NOW, paths=paths)
    assert detail.flex is not None and "_infl_view:RuntimeError" in detail.render_errors
    assert texts(detail.flex["body"]) == ["消費者物價（CPI）：暫缺", *BE_A] and detail.alt_text == "盤勢：通膨，CPI 暫無數字"
    status = md.status_summary(now=NOW, paths=paths)
    assert "_infl_view:RuntimeError" in status["render_error"] and "secret" not in status["render_error"]
    assert not any("secret" in error or "3.7" in error for error in (*summary.render_errors, *detail.render_errors))


def test_a_crash_in_the_detail_card_reaches_the_status_summary(md, paths, monkeypatch):
    """P2：每日維護的 status_summary 也跑 /盤勢 通膨（清單最後），細節卡壞掉看得到；任何一張壞掉 smoke 都會 FAIL。"""
    write_snapshot(paths, make_snapshot(NOW))
    assert "render_error" not in md.status_summary(now=NOW, paths=paths)

    def boom(_ctx):
        raise RuntimeError("secret")

    monkeypatch.setattr(md, "_card_infl", boom)
    assert md.status_summary(now=NOW, paths=paths)["render_error"] == "unexpected:RuntimeError"
    assert md.handle(" 通膨", now=NOW, paths=paths).text == md.TEXT_NOT_READY  # 單張細節卡：handle 照舊回固定句


# ── 快照字串不進卡片（P4） ─────────────────────────────────────────────────────────
P4_JUNK = ("@all", "x y", "https://x.y/z", "最新公布", "建議買進", [], {}, 1e300, True)
P4_LEAKS = ("@", " ", "http", "x.y", "最新公布", "建議買進", "[]", "{", "}", "1e+300", "True", "true", "None")


def field_paths(node, prefix=()):
    """每個欄位的路徑：葉子，加上整個子物件（cpi、core、y5、y10）。"""
    if isinstance(node, dict):
        if prefix:
            yield prefix
        for key, value in node.items():
            yield from field_paths(value, (*prefix, key))
    else:
        yield prefix


@pytest.mark.parametrize("junk", P4_JUNK, ids=[repr(j)[:14] for j in P4_JUNK])
def test_junk_in_any_inflation_field_never_reaches_the_cards(md, junk):
    base = make_snapshot(NOW)
    for row_id in ("us.infl.cpi", "us.infl.be"):
        for path in list(field_paths(base["rows"][row_id]["values"])):
            snap = make_snapshot(NOW)
            target = snap["rows"][row_id]["values"]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = junk
            snap["rows"][row_id].update(source="建議買進 @all", basis="https://x.y/z")  # 列上的字一律不讀（P4）
            for build in ("_summary_reply", "_card_infl"):
                ctx = md._Ctx(snap, NOW)
                reply = getattr(md, build)(ctx)
                assert reply.flex is not None and reply.render_errors == (), (row_id, path, build)
                assert ctx.dropped == 0, (row_id, path, build)
                for value in reply_strings(reply):
                    assert not any(leak in value for leak in P4_LEAKS), (row_id, path, build, value)
                if build == "_card_infl":
                    assert_main_gate(reply)


def test_the_row_source_and_basis_strings_are_not_printed(md):
    snap = make_snapshot(NOW)
    plain = (card(md, snap)[0].flex, digest(md, snap)[1])
    for row_id in ("us.infl.cpi", "us.infl.be"):
        snap["rows"][row_id].update(source="最新公布 @all", basis="建議買進")
    assert (card(md, snap)[0].flex, digest(md, snap)[1]) == plain


# ── 固定句（P3、G10、G13）、寬度、大小 ───────────────────────────────────────────────
def test_the_code_tables_have_exactly_the_publish_codes():
    """dict 只做「碼→常數」（P3）：鍵＝publish 端的碼集合，值都是 TEXT_* 常數（FIXED_TEXTS 自動收進掃描）。"""
    pairs = (("INFL_LEVEL_TEXT", "INFL_LEVEL_CODES"), ("INFL_MOMENTUM_TEXT", "INFL_MOMENTUM_CODES"),
             ("INFL_ANN3_TEXT", "INFL_MOMENTUM_CODES"), ("INFL_MOVE_TEXT", "INFL_MOVE_CODES"),
             ("INFL_GAP_TEXT", "INFL_GAP_CODES"), ("BE_SPREAD_TEXT", "BE_SPREAD_CODES"))
    constants = {value for name, value in vars(market_calc).items() if name.startswith("TEXT_") and isinstance(value, str)}
    for table, codes in pairs:
        mapping = getattr(market_calc, table)
        assert set(mapping) == set(getattr(market_calc, codes)), table
        assert set(mapping.values()) <= constants and len(set(mapping.values())) == len(mapping), table
        assert all(value in market_calc.FIXED_TEXTS for value in mapping.values()), table


def test_the_publish_basis_sentences_keep_their_values():
    """TEXT_BE_BASIS、TEXT_INFL_BASIS 是 publish 的 basis=：值一個字都不能改（G13）。"""
    assert market_calc.TEXT_BE_BASIS == "名目殖利率減實質殖利率（%）"
    assert market_calc.TEXT_INFL_BASIS == BASIS


NEW_TEXTS = (
    "TEXT_BE_LINE", "TEXT_BE_STATE", "TEXT_BE_SPREAD_FIVE_HIGHER", "TEXT_BE_SPREAD_FIVE_LOWER", "TEXT_BE_SPREAD_SIMILAR",
    "TEXT_BE_CARD_NOTE", "TEXT_INFL_LEVEL_NOT_ABOVE", "TEXT_INFL_LEVEL_SLIGHTLY_ABOVE", "TEXT_INFL_LEVEL_ABOVE",
    "TEXT_INFL_LEVEL_WELL_ABOVE", "TEXT_INFL_MOMENTUM_ACCELERATING", "TEXT_INFL_MOMENTUM_STEADY",
    "TEXT_INFL_MOMENTUM_COOLING", "TEXT_INFL_ANN3_FASTER", "TEXT_INFL_ANN3_SIMILAR", "TEXT_INFL_ANN3_SLOWER",
    "TEXT_INFL_MOVE_UP", "TEXT_INFL_MOVE_DOWN", "TEXT_INFL_MOVE_FLAT", "TEXT_INFL_GAP_HEADLINE_HIGHER",
    "TEXT_INFL_GAP_HEADLINE_LOWER", "TEXT_INFL_GAP_SIMILAR", "TEXT_INFL_DIGEST_FULL", "TEXT_INFL_DIGEST_LEVEL",
    "TEXT_INFL_DIGEST_NUMBER", "TEXT_INFL_TITLE", "TEXT_INFL_CPI_LINE", "TEXT_INFL_TARGET_LINE", "TEXT_INFL_CORE_LINE",
    "TEXT_INFL_CORE_MISSING", "TEXT_INFL_MOM_LINE", "TEXT_INFL_ANN3_LINE", "TEXT_INFL_CPI_STALE_MONTH",
    "TEXT_INFL_CPI_STALE", "TEXT_INFL_CPI_MISSING", "TEXT_INFL_BASE_GAP", "TEXT_INFL_PCE", "TEXT_INFL_BLS_DIGEST",
    "TEXT_INFL_BLS_CARD", "TEXT_INFL_BLS_VOUCH", "TEXT_INFL_BLS_ORIGINAL", "TEXT_INFL_ALT", "TEXT_INFL_ALT_NO_CPI",
    "TEXT_INFL_ALT_MISSING",
)
# 自己的 kwargs（G10：不放進 test_market_f2_display 的 NEW_FIXED）；最寬的數字、兩位數月份
FILL = {"yoy": "25.0", "m": 12, "n": 11, "p": 11, "prev": "-5.0", "mom": "-10.0", "ann3": "100.0", "pp": "30.0",
        "bp": 20000, "v": "-100.00", "md": "10/16", "level": "明顯高於 2%", "momentum": "降溫", "state": "過期（資料日 10/16）"}
GATE_TIMES = [datetime(*args, tzinfo=TPE) for args in ((2026, 10, 30, 9), (2026, 11, 13, 9), (2027, 1, 15, 9),
                                                       (2028, 1, 3, 9), (2028, 12, 31, 9))]


@pytest.fixture(scope="module")
def promise_scan():
    spec = importlib.util.spec_from_file_location("bot_promise_scan_for_infl", HERE / "jobs" / "bot_promise_scan.py")
    scan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scan  # 它的 dataclass 要在 sys.modules 找得到自己
    try:
        spec.loader.exec_module(scan)
        yield scan
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("name", NEW_TEXTS)
def test_new_fixed_sentences_pass_every_gate(name, promise_scan):
    text = getattr(market_calc, name)
    assert text in market_calc.FIXED_TEXTS
    assert not any(str(year) in text for year in range(2020, 2040))
    filled = text.format(**FILL)
    assert "{" not in filled and "}" not in filled
    plain = filled.replace(DISCLAIMER, "")
    assert not any(word in plain for word in (*market_calc.FORBIDDEN_WORDS, *PLAN_FORBIDDEN, "最新公布", "官方"))
    assert promise_scan.promise_sentences(filled) == [] and not re.search(r"下次會|之後會|我會", filled)
    for at in GATE_TIMES:
        result = output_validator.validate_outbound_text(filled, now=at)
        assert result.ok and result.text == filled, (filled, at, result.reason)


def worst_values() -> dict:
    """卡上最寬的組合（被改過或版本不一致的快照才會有）：兩位數月份、四到五個字的數字、動能比較句、基期缺值、整體比核心。"""
    values = changed(cpi__month="2026-12-01", cpi__yoy_month="2026-11-01", cpi__sa_month="2026-11-01", cpi__yoy=25.0,
                     cpi__yoy_prev=-5.0, cpi__mom=-10.0, cpi__ann3=100.0, cpi__momentum="steady",
                     core__month="2026-12-01", core__yoy_month="2026-11-01", core__yoy=-5.0, gap_pp=30.0,
                     retrieved="2026-12-31")
    return values


def test_worst_case_widths_keep_every_row_whole(md):
    snap = with_cpi(worst_values())
    be = snap["rows"]["us.infl.be"]["values"]
    for tenor in ("y5", "y10"):
        be[tenor].update(date="2026-12-31", value=-100.0)
    be.update(spread_bp=-20000, spread="five_lower")
    rows, notes, _bubble, _ctx = digest(md, snap)
    assert rows[3] == "通膨：CPI 25.0%（11 月）明顯高於 2%，近 3 月持平" and md.row_width(rows[3]) == 24.0  # _fit 用 <=，保留
    assert "12 月年增算不出來，先列 11 月" in notes
    body = card(md, snap)[2]
    assert "近 3 月年化 100.0%（季調，11 月），和年增差不多" in body and "年增由 10 月 -5.0% 升到 11 月 25.0%" in body
    assert "10 年損益兩平通膨 -100.00%（12/31）" in body and "5 年期比 10 年期低 20000bp" in body
    assert all(md.row_width(line) <= 24 and "…" not in line for line in body), body
    snap = with_cpi(changed(cpi__month="2026-12-01", cpi__yoy_month="2026-12-01", cpi__sa_month="2026-12-01",
                            cpi__yoy=-5.0, cpi__level="not_above", cpi__momentum="cooling"))
    assert digest(md, snap)[0][3] == "通膨：CPI -5.0%（12 月）不高於 2%，近 3 月降溫"


def test_the_worst_snapshot_still_fits(md, paths):
    """test_market_dashboard 的 worst_snapshot（批 2 在最後加了通膨的最壞值）：摘要泡泡 ≤6.5 KB、細節卡加大字版按鈕放得下。"""
    snap = worst_snapshot(NOW)
    write_snapshot(paths, snap)
    summary = md.handle("", now=NOW, paths=paths)
    first = summary.flex["contents"][0]
    assert summary.render_errors == () and texts(first["body"])[3].startswith("通膨：CPI")
    assert any("算不出來" in text for text in texts(first["footer"]))  # 最壞值有基期缺值句（footer 最多的情形）
    assert md.card_bytes(first) <= md.SUMMARY_BUBBLE_MAX_BYTES and size(summary.flex) <= md.CARD_MAX_BYTES
    detail = md.handle(" 通膨", now=NOW, paths=paths)
    assert detail.flex is not None and detail.render_errors == ()
    assert size(detail.flex) <= md.SUMMARY_BUBBLE_MAX_BYTES  # 單張泡泡也在摘要泡泡的上限內
    assert card_pages.fits_line_limits(card_pages.with_page_button(detail.flex, LONG_PAGE_URL))
    assert all(md.row_width(text) <= 24 and "…" not in text for text in texts(detail.flex["body"]))
