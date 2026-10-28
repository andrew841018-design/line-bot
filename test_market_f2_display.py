"""/盤勢 批 1 審查（Phase 6 第 1 輪）後的顯示層修正 F2（2026-10-28）的測試：F2-1～F2-16。

來源：reviews/w1_phase4_triage.md 的 F2 表，與它引用的 w1_review_gp1／gp2／seat3／seat4。這個檔案單獨一份（不和 F1 資料層的測試檔
改同一處）；只借 test_market_dashboard 的 helper 與 fixture，不借它的 test_* 函式。每個測試都在基底（w1-int 4e984e8）上是紅的
（Prove-It）：先放「對照」（沒有標示的情形），再放「該有標示」的情形。

字寬一律用 market_dashboard.row_width 量；日期用 fixture 的 NOW（台北 10/17 週六 16:30）：最新場次 DAY＝10/16、前一天 PREV＝10/15。

第 2 輪複審後的 F3（reviews/w1_r2_triage.md）刻意改了這裡幾個預期：F2-1 川普壓力指數落後時的寫法（換短名稱、留住「高壓」；3/3
第一行不帶日期）、F2-8「（暫定）」的位置（列名後面，不在列尾）；everything_on 另外量整個摘要輪播（F3-6）。F3 新的測試在
test_market_f3.py。
"""

from __future__ import annotations

import itertools
import random
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest

import market_calc
import output_validator
from test_market_dashboard import (  # noqa: F401  md／paths 是 fixture（用名字被 pytest 找到）
    ALL_NOWS,
    DAY,
    DISCLAIMER,
    NOW,
    PREV,
    TPE,
    _never,
    _options,
    _row,
    actions,
    assert_main_gate,
    digest_bubble,
    digest_snapshot,
    digest_texts,
    make_snapshot,
    md,
    paths,
    row_ctx,
    set_etf,
    set_options,
    set_taiex,
    set_ust,
    set_vix,
    size,
    summary_bubble,
    texts,
    worst_snapshot,
    write_snapshot,
)

MEANING, SOURCE = market_calc.TEXT_TACO_MEANING, market_calc.TEXT_TACO_SOURCE
PROVISIONAL = market_calc.TEXT_PROVISIONAL
CBOE = market_calc.SOURCE_CBOE_FALLBACK
TACO_STATE = "川普壓力指數：暫缺"


def digest_footer(md_, snap, now=NOW):
    return texts(digest_bubble(md_, snap, now)["footer"])


def notes_before_disclaimer(footer):
    return footer[: footer.index(DISCLAIMER)]


def card(md_, paths_, snap, name, now=NOW, body=""):
    write_snapshot(paths_, snap)
    reply = md_.handle(body, now=now, paths=paths_)
    assert reply.flex is not None and reply.render_errors == (), reply.text
    return summary_bubble(md_, reply.flex, name)


def with_progress(snap, **progress):
    snap["progress"].update(progress)
    return snap


# ═════════════════════════════════════════════════════════════════════════════
# F2-1：重點摘要第 3、4、6 列資料日落後時帶日期；footer 的資料日加 10 年債與川普壓力指數；TACO 細節帶資料日
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_1_the_risk_row_dates_itself_when_vix_is_behind_the_expected_session(md):
    snap = digest_snapshot()
    risk = lambda: md._digest_risk_row(row_ctx(md, snap))[0]  # noqa: E731
    assert risk() == "風險：VIX 18.0 平穩"  # 對照：同一天不寫
    set_vix(snap, "fresh", 18.04, PREV)  # 其他美股列都是 10/16：預期的最新場次是 10/16，VIX 落後一個場次
    assert risk() == "風險 10/15：VIX 18.0 平穩"
    set_options(snap, put=-0.6)  # 牆距寫進這一列：日期是這個寫法裡有數字的資料最舊的那天（VIX 10/15、期權 10/16）
    assert risk() == "風險 10/15：VIX 18.0 平穩，QQQ 賣權牆距 -0.6%"
    set_vix(snap, "fresh", 18.04, DAY)  # 反過來：VIX 當天、期權落後
    set_options(snap, put=-0.6, day=PREV)
    assert risk() == "風險 10/15：VIX 18.0 平穩，QQQ 賣權牆距 -0.6%"
    set_options(snap, put=-0.6, day=DAY)
    assert risk() == "風險：VIX 18.0 平穩，QQQ 賣權牆距 -0.6%"  # 都當天：不寫
    with_progress(snap, us_expected_day="2026-10-19")  # publisher 說預期的最新場次是 10/19：兩者都落後 → 最舊的 10/16
    assert risk() == "風險 10/16：VIX 18.0 平穩，QQQ 賣權牆距 -0.6%"


def test_f2_1_the_risk_row_drops_the_wall_before_it_drops_the_date(md, monkeypatch):
    """最壞值：VIX 100.0 恐慌＋SOXX 賣權牆距 -0.9%＋日期＝23.5，放得下；窄到放不下時先丟牆（次要成分），日期一直留著。"""
    snap = digest_snapshot(vix=99.99)
    set_vix(snap, "fresh", 99.99, PREV)
    set_options(snap, put=-0.9)
    snap["rows"]["us.options.SOXX"]["values"].update(put_wall_dist_pct=-0.9)
    snap["rows"]["us.options.QQQ"]["values"].update(put_wall_dist_pct=-4.0)  # 只剩 SOXX 的牆在 1% 內
    ctx = row_ctx(md, snap)
    text = md._digest_risk_row(ctx)[0]
    assert text == "風險 10/15：VIX 100.0 恐慌，SOXX 賣權牆距 -0.9%" and md.row_width(text) == 23.5
    monkeypatch.setattr(md, "MAX_ROW_WIDTH", 15.0)
    assert md._digest_risk_row(ctx)[0] == "風險 10/15：VIX 100.0 恐慌"  # 牆先丟，日期還在


def test_f2_1_the_risk_row_does_not_date_an_expired_or_missing_vix(md):
    snap = digest_snapshot()
    set_vix(snap, "stale", 18.04, PREV)  # 過期的 VIX 自己已經寫了資料日（「過期（資料日 10/15）」），不另外在列名後面加
    assert md._digest_risk_row(row_ctx(md, snap))[0] == "風險：VIX 過期（資料日 10/15）"
    set_vix(snap, "never")
    assert md._digest_risk_row(row_ctx(md, snap))[0] == "風險：VIX 暫缺"


def test_f2_1_an_expired_vix_with_a_lagging_wall_still_gets_the_wall_date(md):
    """VIX 過期（自己寫了資料日）、牆落後一個場次：列名後面的日期只代表牆；長寫法放不下就換短的『過期』，日期與牆都留著。"""
    snap = digest_snapshot()
    set_vix(snap, "stale", 18.04, DAY)
    set_walls(snap, "SOXX", put=-0.9, day=PREV)
    text = md._digest_risk_row(row_ctx(md, snap))[0]
    assert text == "風險 10/15：VIX 過期，SOXX 賣權牆距 -0.9%" and md.row_width(text) == 20.5


def test_f2_1_the_rate_row_dates_itself_and_drops_the_change_before_the_date(md, monkeypatch):
    snap = digest_snapshot()
    rate = lambda: md._digest_rate_row(row_ctx(md, snap))[0]  # noqa: E731
    assert rate() == "利率：10 年債 5.09%（+12bp）"  # 對照
    set_ust(snap, "fresh", 5.09, 0.12, PREV)  # VIX、ETF 等都是 10/16：10 年債落後一個場次
    assert rate() == "利率 10/15：10 年債 5.09%（+12bp）" and md.row_width(rate()) == 17.0
    set_ust(snap, "fresh", 99.99, 49.99, PREV)  # 最壞值
    assert rate() == "利率 10/15：10 年債 99.99%（+4999bp）" and md.row_width(rate()) <= 24
    monkeypatch.setattr(md, "MAX_ROW_WIDTH", 17.0)
    assert rate() == "利率 10/15：10 年債 99.99%"  # 先丟變動，日期留著
    monkeypatch.undo()
    set_ust(snap, "stale", 5.09, 0.12, PREV)
    assert rate() == "利率：10 年債過期（資料日 10/15）"  # 過期的自己寫了資料日
    del snap["progress"]  # 舊快照沒有 progress：預期的場次退回其他列的證據，照樣有效
    set_ust(snap, "fresh", 5.09, 0.12, PREV)
    assert rate() == "利率 10/15：10 年債 5.09%（+12bp）"


def lagging_taco(snap, day=PREV):
    snap["rows"]["macro.TACO"]["data_date"] = day.isoformat()
    return snap


def test_f2_1_the_trump_pressure_line_dates_itself_everywhere_it_appears(md, paths):
    snap = lagging_taco(digest_snapshot())  # TACO 10/15、其他美股列 10/16
    # 帶狀態詞的寫法加日期是 25.5，放不下：換短名稱，狀態詞和日期都留著（第 2 輪複審 F3-2；以前是拿掉狀態詞的 22.5）
    dated = "川普壓力 10/15 +9.0（高壓，任期內第 88 百分位）"
    assert md.row_width(dated) == 23.5
    assert md._digest_trump_row(row_ctx(md, snap)) == (dated, md.GRAY)  # 重點摘要第 6 列
    assert md._taco_line(row_ctx(md, snap)) == dated
    macro = card(md, paths, snap, "宏觀與事件")
    assert texts(macro["footer"])[:2] == [dated, MEANING]  # 宏觀與事件 5/5 的頁尾
    plain = "川普壓力指數 +9.0（高壓，任期內第 88 百分位）"
    detail = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["footer"])
    # 新聞與宏觀 3/3 的頁尾：第一行是不帶日期的完整寫法（F3-2：「高壓」永遠在），第二行是這一筆的資料日
    assert detail[:2] == [plain, "資料日 10/15"]
    # 對照：當天的資料三處都不帶日期
    fresh = digest_snapshot()
    assert md._digest_trump_row(row_ctx(md, fresh))[0] == plain
    assert texts(card(md, paths, fresh, "宏觀與事件")["footer"])[0] == plain


def test_f2_1_the_dated_trump_pressure_line_keeps_the_date_through_every_variant(md, monkeypatch):
    ctx = row_ctx(md, lagging_taco(digest_snapshot()))
    variants = [
        (23.5, "川普壓力 10/15 +9.0（高壓，任期內第 88 百分位）"),  # 短名稱，狀態詞留著（第 2 輪複審 F3-2）
        (22.5, "川普壓力指數 10/15 +9.0（任期內第 88 百分位）"),  # 拿掉狀態詞
        (16.5, "川普壓力 10/15 +9.0｜第 88 百分位"),  # 短名稱
        (11.5, "川普壓力指數 10/15 +9.0"),  # 只有數字
    ]
    for width, expected in variants:
        monkeypatch.setattr(md, "MAX_ROW_WIDTH", width)
        assert md._taco_line(ctx) == expected and md.row_width(expected) == width


@pytest.mark.parametrize("value", [-100.0, -12.3, 0.0, 9.0005, 99.96, 100.0])
@pytest.mark.parametrize("percentile", [0, 7, 88, 100])
@pytest.mark.parametrize("day", [date(2026, 12, 31), date(2026, 10, 5)])
def test_f2_1_the_worst_dated_trump_pressure_line_fits_in_24(md, value, percentile, day):
    snap = digest_snapshot()
    values = snap["rows"]["macro.TACO"]["values"]
    values.update(value=value, percentile=percentile, max=max(values["max"], value), min=min(values["min"], value))
    snap["rows"]["macro.TACO"]["data_date"] = day.isoformat()
    snap["progress"]["us_expected_day"] = "2027-01-04" if day.month == 12 else "2026-10-16"
    text = md._taco_line(row_ctx(md, snap))
    assert f" {day.month}/{day.day} " in text and md.row_width(text) <= 24 and "…" not in text, text


def test_f2_1_nothing_says_what_is_expected_means_no_date(md):
    """沒有 progress、也沒有任何美股列有資料日可比：不知道預期的場次，就不猜、不寫日期（舊快照照常）。"""
    snap = lagging_taco(digest_snapshot())
    for row_id in ("us.etf.VOO", "us.etf.QQQ", "us.etf.SOXX", "us.options.QQQ", "us.options.SPY", "us.options.SOXX",
                   "macro.VIX", "macro.UST10Y"):
        snap["rows"][row_id] = _never()
    ctx = row_ctx(md, snap)
    assert md._digest_trump_row(ctx)[0] == "川普壓力指數 +9.0（高壓，任期內第 88 百分位）"
    assert "資料日 10/15" in md._taco_detail_notes(ctx)  # 細節卡的資料日不看預期的場次


def test_f2_1_the_digest_footer_lists_ten_year_and_trump_pressure_dates(md):
    snap = lagging_taco(digest_snapshot())
    set_ust(snap, "fresh", 5.09, 0.12, PREV)
    assert digest_footer(md, snap)[0] == "資料日：美股 10/16、台股 10/16、VIX 10/16、10 年債 10/15、川普壓力指數 10/15"


def test_f2_1_the_trump_pressure_detail_footer_always_has_its_data_date(md, paths):
    snap = digest_snapshot()
    write_snapshot(paths, snap)
    footer = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["footer"])
    assert footer[:2] == ["川普壓力指數 +9.0（高壓，任期內第 88 百分位）", "資料日 10/16"]
    snap["rows"]["macro.TACO"]["stale_after"] = NOW - 1  # 過期：整段說明都不寫（行上自己有資料日）
    write_snapshot(paths, snap)
    footer = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["footer"])
    assert footer[0] == "川普壓力指數：過期（資料日 10/16）" and not any(t.startswith("資料日 ") for t in footer)
    assert market_calc.TEXT_TACO_DATA_DAY == "資料日 {md}" and market_calc.TEXT_TACO_DATA_DAY in market_calc.FIXED_TEXTS


# ═════════════════════════════════════════════════════════════════════════════
# F2-2：摘要與宏觀 5/5 頁尾加「不是退讓的機率」（摘要另加來源句）；位階行「個交易日」
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_2_meaning_and_source_are_in_the_digest_footer_and_meaning_in_the_macro_footer(md, paths):
    snap = digest_snapshot()
    footer = digest_footer(md, snap)
    assert footer[footer.index(MEANING) + 1] == SOURCE and footer.index(DISCLAIMER) == footer.index(SOURCE) + 1
    macro = texts(card(md, paths, snap, "宏觀與事件")["footer"])
    assert MEANING in macro and SOURCE not in macro  # 5/5 只加「不是退讓的機率」；來源句在摘要和 3/3
    detail = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["footer"])
    assert MEANING in detail and SOURCE in detail  # 3/3 本來就有
    others = [name for name, _builder in md.SUMMARY_CARDS if name not in ("重點摘要", "宏觀與事件")]
    for name in others:  # 其他三張卡不放
        assert MEANING not in texts(card(md, paths, snap, name)["footer"]), name


@pytest.mark.parametrize("broken", ["stale", "never", "bad_value", "no_row"])
def test_f2_2_no_number_means_no_meaning_sentence(md, paths, broken):
    """「不是退讓的機率」是在說那個數字：指數沒有數字（過期、暫缺、壞掉、舊快照沒有這一列）就不放，也不放來源句。"""
    snap = digest_snapshot()
    if broken == "stale":
        snap["rows"]["macro.TACO"]["stale_after"] = NOW - 1
    elif broken == "never":
        snap["rows"]["macro.TACO"] = _never()
    elif broken == "bad_value":
        snap["rows"]["macro.TACO"]["values"]["percentile"] = 101
    else:
        del snap["rows"]["macro.TACO"]
    assert MEANING not in digest_footer(md, snap) and SOURCE not in digest_footer(md, snap)
    assert MEANING not in texts(card(md, paths, snap, "宏觀與事件")["footer"])


def test_f2_2_the_position_line_counts_trading_days_not_days(md, paths):
    snap = digest_snapshot()
    write_snapshot(paths, snap)
    footer = texts(md.handle(" 新聞", now=NOW, paths=paths).flex["contents"][2]["footer"])
    (line,) = [t for t in footer if t.startswith("位階：")]
    assert line == "位階：2025-03-26 起 394 個交易日中第 88 百分位（最高 +17.3，2026-03-27；最低 -5.0，2025-05-20）"
    assert "天中" not in line and "任期內第" in market_calc.TEXT_TACO_RANK  # 「任期內」保留


def test_f2_2_the_worst_position_line_still_reaches_the_card(md, paths):
    """「個交易日」比「天」多 3 個字：n 到上限 20000、±100.0、第 100 百分位時 79 字，離 TACO_NOTE_MAX（80）只差 1；
    超過會被 ctx.note 悄悄丟掉（只多記一次 external_dropped），所以量長度還不夠，要確認這一行真的出現在卡上。"""
    snap = digest_snapshot()
    snap["rows"]["macro.TACO"]["values"].update(
        n=20000, percentile=100, value=100.0, max=100.0, min=-100.0, since="2025-12-31", max_date="2026-12-31",
        min_date="2026-10-26")
    write_snapshot(paths, snap)
    reply = md.handle(" 新聞", now=NOW, paths=paths)
    footer = texts(reply.flex["contents"][2]["footer"])
    expected = "位階：2025-12-31 起 20000 個交易日中第 100 百分位（最高 +100.0，2026-12-31；最低 -100.0，2026-10-26）"
    assert expected in footer and len(expected) == 79 <= md.TACO_NOTE_MAX
    assert md.status_summary(now=NOW, paths=paths)["external_dropped"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# F2-3：「今天休市」→「{md} 休市」
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_3_the_closed_notice_names_the_snapshot_day_not_today(md, paths):
    snap = with_progress(make_snapshot(NOW), tw_closed_today=True, tw_closed_confirmed=False)
    tw = card(md, paths, snap, "台股")
    assert texts(tw["header"])[1] == "10/17 休市（暫定）"
    snap["progress"]["tw_closed_confirmed"] = True
    assert texts(card(md, paths, snap, "台股")["header"])[1] == "10/17 休市"
    for template in (market_calc.TEXT_TW_CLOSED, market_calc.TEXT_TW_CLOSED_TENTATIVE):
        assert "今天" not in template and "{md}" in template and template in market_calc.FIXED_TEXTS
    assert market_calc.TEXT_TW_CLOSED == "{md} 休市" and market_calc.TEXT_TW_CLOSED_TENTATIVE == "{md} 休市（暫定）"
    for day in (date(2026, 12, 31), date(2026, 1, 3)):  # 最寬與最窄的日期
        assert md.row_width(market_calc.TEXT_TW_CLOSED_TENTATIVE.format(md=md._md(day))) <= 9.0


# ═════════════════════════════════════════════════════════════════════════════
# F2-4：重點摘要 footer 的美股「尚未取得」、台股休市／尚未取得、第 3 列牆距基準
# ═════════════════════════════════════════════════════════════════════════════
US_WAITING = dict(us_day="2026-10-16", us_expected_day="2026-10-19", us_waiting_for_source=True)
TW_WAITING = dict(tw_day="2026-10-16", tw_expected_day="2026-10-19", tw_waiting_for_source=True)


def test_f2_4_the_digest_footer_says_the_us_close_has_not_arrived(md, paths):
    snap = digest_snapshot()
    assert not any("尚未取得" in note for note in digest_footer(md, snap))  # 對照：沒有證據不寫
    with_progress(snap, **US_WAITING)
    assert "美股 10/19 收盤資料尚未取得" in digest_footer(md, snap)
    subtitle = texts(card(md, paths, snap, "美股")["header"])[-1]  # 美股卡副標用的是同一個判斷：兩處的日期永遠一致
    assert subtitle == "10/19 收盤資料尚未取得"
    assert f"美股 {subtitle}" in digest_footer(md, snap)
    snap["progress"]["us_waiting_for_source"] = False
    assert not any("尚未取得" in note for note in digest_footer(md, snap))


def test_f2_4_the_digest_footer_says_the_tw_market_is_closed_or_waiting(md):
    snap = digest_snapshot()
    with_progress(snap, **TW_WAITING)
    assert "台股 10/19 收盤資料尚未取得" in digest_footer(md, snap)
    with_progress(snap, tw_closed_today=True, tw_closed_confirmed=False)
    footer = digest_footer(md, snap)
    assert "台股 10/17 休市（暫定）" in footer and not any("台股 10/19" in note for note in footer)  # 休市優先於尚未取得
    snap["progress"]["tw_closed_confirmed"] = True
    assert "台股 10/17 休市" in digest_footer(md, snap)
    snap["progress"]["tw_closed_today"] = False
    assert "台股 10/19 收盤資料尚未取得" in digest_footer(md, snap)


def test_f2_4_the_digest_footer_names_the_wall_basis_only_when_the_row_shows_a_wall(md):
    snap = digest_snapshot()
    assert market_calc.TEXT_WALL_DIST_CLOSE not in digest_footer(md, snap)  # 對照：第 3 列沒有牆
    set_options(snap, put=-0.6)  # fixture 的 QQQ 是收盤基準
    assert market_calc.TEXT_WALL_DIST_CLOSE in digest_footer(md, snap)
    snap["rows"]["us.options.QQQ"]["values"].update(dist_basis="spot", ref_close=None)
    footer = digest_footer(md, snap)
    assert market_calc.TEXT_WALL_DIST_SPOT in footer and market_calc.TEXT_WALL_DIST_CLOSE not in footer


# ═════════════════════════════════════════════════════════════════════════════
# F2-5：「收盤資料尚未取得」比照休市：快照不是今天做的就不寫
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_5_waiting_notices_need_a_snapshot_made_today(md, paths):
    def everything(snap):
        us = texts(card(md, paths, snap, "美股")["header"])
        tw = texts(card(md, paths, snap, "台股")["header"])
        return us, tw, digest_footer(md, snap)

    snap = with_progress(digest_snapshot(), **US_WAITING, **TW_WAITING)
    us, tw, footer = everything(snap)
    assert us[-1] == "10/19 收盤資料尚未取得" and tw[-1] == "10/19 收盤資料尚未取得"  # 對照：今天做的就寫
    assert "美股 10/19 收盤資料尚未取得" in footer and "台股 10/19 收盤資料尚未取得" in footer
    snap["generated_at"] = NOW - 86400  # job 掛了：昨天做的快照今天才被讀到
    us, tw, footer = everything(snap)
    assert not any("尚未取得" in text for text in us + tw + footer)
    del snap["generated_at"]  # 不知道什麼時候做的：也不寫
    us, tw, footer = everything(snap)
    assert not any("尚未取得" in text for text in us + tw + footer)


# ═════════════════════════════════════════════════════════════════════════════
# F2-6：0050 細節卡下半段資料日≠標題日時帶日期
# ═════════════════════════════════════════════════════════════════════════════
def tw_detail_lines(md_, paths_, snap):
    write_snapshot(paths_, snap)
    return texts(md_.handle(" 0050", now=NOW, paths=paths_).flex)


def lagging_0050():
    snap = with_progress(make_snapshot(NOW), tw_exrights_pending=True)
    snap["rows"]["tw.etf.0050"]["data_date"] = PREV.isoformat()  # 標題日 10/15；加權、漲跌家數、成交金額、三大法人、融資都是 10/16
    snap["rows"]["tw.breadth.0050"]["data_date"] = PREV.isoformat()
    return snap


def test_f2_6_the_lower_half_of_the_0050_card_dates_itself_when_it_differs_from_the_title(md, paths):
    lines = tw_detail_lines(md, paths, lagging_0050())
    assert lines[0] == "台灣50（0050）｜10/15 收盤"
    assert "上市漲跌家數：上漲 684（漲停 19）、下跌 300（跌停 2）、平盤 50（10/16）" in lines
    assert "成交金額 5,234 億，量偏少（0.78 倍），近 250 日第 30 百分位（10/16）" in lines  # 日期放句尾，不是兩組括號
    assert "三大法人（10/16 當天）：外資賣超 123.4 億、投信買超 45.6 億、自營買超 10.2 億" in lines
    assert "融資餘額 3,456 億（+12.3 億），10/16（參考）" in lines
    assert not any("今日" in line for line in lines)  # 「今日」是相對詞，0050 比加權舊一天時是錯的


def test_f2_6_the_same_day_card_has_no_extra_dates_except_the_margin_date(md, paths):
    lines = tw_detail_lines(md, paths, make_snapshot(NOW))
    assert "上市漲跌家數：上漲 684（漲停 19）、下跌 300（跌停 2）、平盤 50" in lines
    assert "成交金額 5,234 億，量偏少（0.78 倍），近 250 日第 30 百分位" in lines
    assert "三大法人（當天）：外資賣超 123.4 億、投信買超 45.6 億、自營買超 10.2 億" in lines
    assert "融資餘額 3,456 億（+12.3 億），10/16（參考）" in lines  # 融資一律寫日期，取代「今日（參考）」
    assert not any("今日" in line for line in lines)


def test_f2_6_a_margin_row_without_a_date_says_so(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["tw.margin"]["data_date"] = None
    assert "融資餘額 3,456 億（+12.3 億），日期暫缺（參考）" in tw_detail_lines(md, paths, snap)


def test_f2_6_stale_rows_in_the_lower_half_keep_saying_stale(md, paths):
    snap = lagging_0050()
    for row_id in ("tw.ad", "tw.turnover", "tw.inst3", "tw.margin"):
        snap["rows"][row_id]["stale_after"] = NOW - 1
    lines = tw_detail_lines(md, paths, snap)
    for expected in ("上市漲跌家數：過期（資料日 10/16）", "成交金額 過期（資料日 10/16）", "三大法人：過期（資料日 10/16）",
                     "融資：過期（資料日 10/16）"):
        assert expected in lines, expected


# ═════════════════════════════════════════════════════════════════════════════
# F2-7：摘要第 3 列 QQQ、SOXX 取較近的一道牆並帶代號
# ═════════════════════════════════════════════════════════════════════════════
def set_walls(snap, symbol, *, call=3.0, put=-4.0, status="ok", day=DAY, spot=None):
    row = _row(_options(spot or (540.0 if symbol == "QQQ" else 245.0)), NOW, day)
    row["values"].update(call_wall_dist_pct=call, put_wall_dist_pct=put, wall_status=status)
    snap["rows"][f"us.options.{symbol}"] = row


def test_f2_7_the_risk_row_picks_the_nearer_of_the_qqq_and_soxx_walls(md):
    snap = digest_snapshot()
    risk = lambda: md._digest_risk_row(row_ctx(md, snap))[0]  # noqa: E731
    assert risk() == "風險：VIX 18.0 平穩"  # 兩檔的牆都遠
    set_walls(snap, "SOXX", put=-0.4)
    assert risk() == "風險：VIX 18.0 平穩，SOXX 賣權牆距 -0.4%"  # QQQ 的牆遠、SOXX 近：以前摘要看不到
    set_walls(snap, "QQQ", put=-0.6)
    assert risk() == "風險：VIX 18.0 平穩，SOXX 賣權牆距 -0.4%"  # 兩檔都近：寫比較近的
    set_walls(snap, "QQQ", put=-0.3)
    assert risk() == "風險：VIX 18.0 平穩，QQQ 賣權牆距 -0.3%"
    set_walls(snap, "QQQ", put=-0.5)
    set_walls(snap, "SOXX", put=-0.5)
    assert risk() == "風險：VIX 18.0 平穩，QQQ 賣權牆距 -0.5%"  # 一樣近：QQQ 先
    set_walls(snap, "QQQ", call=0.5, put=-4.0)
    assert risk() == "風險：VIX 18.0 平穩，SOXX 賣權牆距 -0.5%"  # 跨檔一樣近：賣權牆先（和單檔的規則一致）
    set_walls(snap, "SOXX", put=-0.4, day=DAY)
    snap["rows"]["us.options.SOXX"]["stale_after"] = NOW - 1  # SOXX 過期：不看
    assert risk() == "風險：VIX 18.0 平穩，QQQ 買權牆距 +0.5%"
    set_walls(snap, "SOXX", put=-0.4, status="insufficient")  # 牆本身不可信：不看
    assert risk() == "風險：VIX 18.0 平穩，QQQ 買權牆距 +0.5%"
    snap["rows"]["us.options.QQQ"] = _never()
    snap["rows"]["us.options.SOXX"] = _never()
    assert risk() == "風險：VIX 18.0 平穩"


def test_f2_7_the_footer_follows_the_symbol_that_was_written(md):
    """footer 的『期權 M/D』與牆距基準句看的是第 3 列真的寫了的那一檔（不是固定看 QQQ）。"""
    snap = digest_snapshot()
    set_walls(snap, "SOXX", put=-0.4, day=PREV)  # SOXX 的期權資料落後一天；QQQ 的牆遠
    snap["rows"]["us.options.SOXX"]["values"].update(dist_basis="spot", ref_close=None)
    footer = digest_footer(md, snap)
    assert footer[0].endswith("、期權 10/15、10 年債 10/16、川普壓力指數 10/16")
    assert market_calc.TEXT_WALL_DIST_SPOT in footer and market_calc.TEXT_WALL_DIST_CLOSE not in footer  # SOXX 是最新價基準
    set_walls(snap, "SOXX", put=-4.0)  # SOXX 的牆也遠：第 3 列沒有牆，footer 也沒有期權日期與基準句
    footer = digest_footer(md, snap)
    assert "期權" not in footer[0] and not any(note.startswith("牆距") for note in footer)


# ═════════════════════════════════════════════════════════════════════════════
# F2-8：第 1 列三檔同向點名、暫定標籤（第 2 輪複審 F3-1 起放在列名後面）、三檔都過期帶資料日
# ═════════════════════════════════════════════════════════════════════════════
def us_row(md_, snap):
    return md_._digest_us_row(row_ctx(md_, snap))[0]


def test_f2_8_three_etfs_that_agree_are_named(md, monkeypatch):
    snap = digest_snapshot()
    assert us_row(md, snap) == "美股：標普、半導體在月線上，那指在月線下"  # 混合時本來就點名
    for sym in ("VOO", "QQQ", "SOXX"):
        set_etf(snap, sym, "up")
    text = us_row(md, snap)
    assert text == "美股：標普、那指、半導體都在月線上" and md.row_width(text) == 17.0
    for sym in ("VOO", "QQQ", "SOXX"):
        set_etf(snap, sym, "down")
    assert us_row(md, snap) == "美股：標普、那指、半導體都在月線下"
    assert "三檔都" not in us_row(md, snap)
    # 縮短的寫法拿掉「在」，點名與「都」留著
    monkeypatch.setattr(md, "MAX_ROW_WIDTH", 16.5)
    assert us_row(md, snap) == "美股：標普、那指、半導體都月線下"


def test_f2_8_all_three_expired_says_the_data_date(md):
    snap = digest_snapshot()
    for sym in ("VOO", "QQQ", "SOXX"):
        set_etf(snap, sym, "stale")
    text = us_row(md, snap)
    assert text == "美股：三檔都過期（資料日 10/16）" and md.row_width(text) == 16.0
    set_etf(snap, "QQQ", "stale", PREV)  # 日期不同：寫最舊的（和一列有新有舊時寫舊不寫新同一個作法）
    assert us_row(md, snap) == "美股：三檔都過期（資料日 10/15）"
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["data_date"] = None
    assert us_row(md, snap) == "美股：三檔都過期"  # 沒有日期可寫
    set_etf(snap, "VOO", "up")  # 只有部分過期：維持原樣（過期的那檔不另外帶日期）
    set_etf(snap, "SOXX", "missing")
    assert us_row(md, snap) == "美股：標普在月線上，那指過期，半導體暫缺"
    for sym in ("VOO", "QQQ", "SOXX"):
        set_etf(snap, sym, "missing")
    assert us_row(md, snap) == "美股：三檔都暫缺"


def test_f2_8_a_provisional_close_gets_a_tag_after_the_row_name_when_it_fits(md):
    """標籤放在列名（和日期）後面、冒號前面：整列的結論用到了暫定收盤（第 2 輪複審 F3-1；以前接在列尾）。"""
    snap = digest_snapshot()
    for sym in ("VOO", "QQQ", "SOXX"):
        set_etf(snap, sym, "up")
    snap["rows"]["us.etf.QQQ"]["source"] = CBOE
    text = us_row(md, snap)
    assert text == "美股（暫定）：標普、那指、半導體都在月線上" and md.row_width(text) == 21.0
    assert market_calc.TEXT_PROVISIONAL_CLOSE in digest_footer(md, snap)  # footer 的固定句照舊
    # 日期＋暫定＋三檔點名＝剛好 24.0
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["data_date"] = PREV.isoformat()
    text = us_row(md, snap)
    assert text == "美股 10/15（暫定）：標普、那指、半導體都在月線上" and md.row_width(text) == 24.0


def test_f2_8_the_tag_is_left_out_when_it_does_not_fit_or_does_not_apply(md):
    snap = digest_snapshot()
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    mixed = us_row(md, snap)  # 「美股：標普、半導體在月線上，那指在月線下」20.0＋4.0＝24.0 剛好放得下
    assert mixed == "美股（暫定）：標普、半導體在月線上，那指在月線下"
    for sym in ("VOO", "QQQ", "SOXX"):
        snap["rows"][f"us.etf.{sym}"]["data_date"] = PREV.isoformat()
    behind = us_row(md, snap)  # 加上日期 23.0＋4.0＝27.0：放不下就不加，由 footer 說明
    assert behind == "美股 10/15：標普、半導體在月線上，那指在月線下" and PROVISIONAL not in behind
    assert market_calc.TEXT_PROVISIONAL_CLOSE in digest_footer(md, snap)
    # 暫定的那檔沒有數字（過期、月線狀態缺）：列上沒有它的月線狀態，不標
    snap = digest_snapshot()
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    set_etf(snap, "VOO", "flagless")
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    assert PROVISIONAL not in us_row(md, snap)
    set_etf(snap, "VOO", "stale")
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    assert PROVISIONAL not in us_row(md, snap)


# ═════════════════════════════════════════════════════════════════════════════
# F2-9：美股卡的一句話在 VOO 是暫定收盤時標暫定
# ═════════════════════════════════════════════════════════════════════════════
SHORT_ONELINER = "標普收在月線上、量偏少"  # job 在沒有成分股比例時寫的短句型（_ONELINER_US_RE 的選項部分都省略）


def test_f2_9_a_short_one_liner_is_tagged_when_voo_is_provisional(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["oneliner.us"]["values"]["text"] = SHORT_ONELINER
    assert md._ONELINER_US_RE.fullmatch(SHORT_ONELINER)
    plain = card(md, paths, snap, "美股")
    assert texts(plain["body"])[-1] == SHORT_ONELINER and market_calc.TEXT_PROVISIONAL_CLOSE not in texts(plain["footer"])  # 對照
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    tagged = card(md, paths, snap, "美股")
    assert texts(tagged["body"])[-1] == SHORT_ONELINER + PROVISIONAL
    assert market_calc.TEXT_PROVISIONAL_CLOSE not in texts(tagged["footer"])  # 放得下就標在句尾，不重複說明
    snap["rows"]["us.etf.VOO"]["source"] = "合成"
    snap["rows"]["us.etf.QQQ"]["source"] = CBOE  # 這句話只看 VOO：別檔暫定不影響
    assert texts(card(md, paths, snap, "美股")["body"])[-1] == SHORT_ONELINER


def test_f2_9_the_usual_one_liner_is_too_wide_for_the_tag_so_the_footer_says_it(md, paths):
    """fixture 的一句話（標普收在月線上、成分股站上月線 62%、量偏少）21.0＋「（暫定）」4.0＝25.0，放不下：句子不動，footer 放固定句。"""
    snap = make_snapshot(NOW)
    line = snap["rows"]["oneliner.us"]["values"]["text"]
    assert md.row_width(line) == 21.0 and md.row_width(line + PROVISIONAL) > 24
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    bubble = card(md, paths, snap, "美股")
    assert texts(bubble["body"])[-1] == line
    assert texts(bubble["footer"]).count(market_calc.TEXT_PROVISIONAL_CLOSE) == 1


def test_f2_9_the_widest_one_liner_moves_the_sentence_to_the_footer(md, paths):
    snap = make_snapshot(NOW)
    long_line = "標普收在月線下、成分股站上月線 100%、量明顯偏少"
    assert md.row_width(long_line) == 23.5 and md._ONELINER_US_RE.fullmatch(long_line)
    snap["rows"]["oneliner.us"]["values"]["text"] = long_line
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    bubble = card(md, paths, snap, "美股")
    assert texts(bubble["body"])[-1] == long_line
    assert texts(bubble["footer"]).count(market_calc.TEXT_PROVISIONAL_CLOSE) == 1


def test_f2_9_a_contradiction_sentence_still_wins_and_keeps_its_own_rule(md, paths):
    snap = make_snapshot(NOW)
    sentence = market_calc.CONTRA_TEXT["up_low_volume"].format(subject=market_calc.SUBJECT_NAME["VOO"])
    snap["rows"]["contra.us"]["values"] = {"codes": ["up_low_volume:VOO"], "text": sentence, "provisional": True}
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    body = texts(card(md, paths, snap, "美股")["body"])
    assert body[-1] == sentence + PROVISIONAL and body.count(snap["rows"]["oneliner.us"]["values"]["text"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
# F2-10：期權牆的列多一個更窄的變體，SOXX 半美元牆價也留得住距離
# ═════════════════════════════════════════════════════════════════════════════
def wall_rows(md_, paths_, snap):
    body = texts(card(md_, paths_, snap, "期權與資金")["body"])
    return body[1], body[2]


def test_f2_10_soxx_half_dollar_strikes_keep_their_distances(md, paths):
    snap = make_snapshot(NOW)
    snap["rows"]["us.options.SOXX"]["values"].update(call_wall=262.5, put_wall=237.5, call_wall_dist_pct=4.9,
                                                      put_wall_dist_pct=-5.0)
    _qqq, soxx = wall_rows(md, paths, snap)
    assert soxx == "SOXX 買權牆262.5（+4.9%）、賣權牆237.5（-5.0%）" and md.row_width(soxx) == 23.5
    assert "買 " not in soxx and "賣 " not in soxx  # 不用「買／賣」縮寫：單獨一個「買 262.5」讀起來像買賣建議
    # 對照：完整寫法（牆價前有空格）是 24.5，放不下；QQQ 的整數牆價仍是完整寫法
    full = "SOXX 買權牆 262.5（+4.9%）、賣權牆 237.5（-5.0%）"
    assert md.row_width(full) == 24.5
    qqq, _soxx = wall_rows(md, paths, make_snapshot(NOW))
    assert qqq == "QQQ 買權牆 556（+3.0%）、賣權牆 518（-4.0%）"


@pytest.mark.parametrize("call, put, call_dist, put_dist", [
    (262.5, 237.5, 4.9, -5.0), (262.5, 237.5, 14.9, -14.9), (1050.0, 1010.0, 14.9, -14.9), (1234.5, 987.5, 3.0, -0.6),
    (99999.5, 99999.0, 14.9, -14.9), (262.5, None, 4.9, None), (None, 237.5, None, -5.0),
])
@pytest.mark.parametrize("day_offset", [0, 1])
def test_f2_10_every_wall_row_variant_fits_and_the_date_outranks_the_distance(md, paths, call, put, call_dist, put_dist,
                                                                           day_offset):
    snap = make_snapshot(NOW)
    for symbol in ("QQQ", "SOXX"):
        snap["rows"][f"us.options.{symbol}"]["values"].update(call_wall=call, put_wall=put, call_wall_dist_pct=call_dist,
                                                               put_wall_dist_pct=put_dist)
    if day_offset:
        snap["rows"]["us.options.SOXX"]["data_date"] = PREV.isoformat()
    for symbol, row in zip(("QQQ", "SOXX"), wall_rows(md, paths, snap)):
        assert md.row_width(row) <= 24 and "…" not in row and row.startswith(symbol), row
        if symbol == "SOXX" and day_offset:
            assert row.endswith("（10/15）"), row  # 落後的資料日最後才丟


# ═════════════════════════════════════════════════════════════════════════════
# F2-11：_summary_alt 壞了退回固定 altText；F2-12：單一泡泡太大只換那一張
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_11_a_broken_alt_text_keeps_the_carousel_and_reports_by_name(md, paths, monkeypatch):
    write_snapshot(paths, make_snapshot(NOW))
    good = md.handle("", now=NOW, paths=paths)
    monkeypatch.setattr(md, "_summary_alt", lambda ctx: 1 / 0)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.text is None and reply.flex == good.flex  # 不再整個回「資料準備中」
    assert reply.alt_text == market_calc.TEXT_SUMMARY_ALT_FALLBACK == "盤勢：摘要卡片，往左滑看全部"
    assert reply.render_errors == ("_summary_alt:ZeroDivisionError",)
    assert_main_gate(reply)
    assert md.status_summary(now=NOW, paths=paths)["render_error"] == "bubble:_summary_alt:ZeroDivisionError"


def big_bubble(md_, head, subtitle=None, extra=0):
    return md_._bubble(head, [md_._text("字" * (md_.SUMMARY_BUBBLE_MAX_BYTES // 3 + extra))], subtitle=subtitle)


def test_f2_12_one_oversized_bubble_is_replaced_and_the_others_stay(md, paths, monkeypatch):
    write_snapshot(paths, make_snapshot(NOW))
    good = md.handle("", now=NOW, paths=paths)
    names = [builder for _card, builder in md.SUMMARY_CARDS]
    position = names.index("_card_flow")

    def huge(ctx, head, subtitle=None):
        return big_bubble(md, head, subtitle)

    huge.__name__ = "_card_flow"
    monkeypatch.setattr(md, "_card_flow", huge)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.flex is not None and reply.text is None  # 以前是整組換成 TEXT_CARD_FAILED
    assert reply.render_errors == ("_card_flow:TooLarge",)
    assert len(reply.flex["contents"]) == len(good.flex["contents"])
    for index, (now_bubble, was) in enumerate(zip(reply.flex["contents"], good.flex["contents"])):
        if index == position:
            assert texts(now_bubble["body"]) == [md.TEXT_BUBBLE_FAILED] and not actions(now_bubble)
            assert now_bubble["header"]["contents"][0]["text"] == f"{md.SUMMARY_CARDS[index][0]}{md._page(index + 1)}"
            assert md.card_bytes(now_bubble) < 2000
        else:
            assert now_bubble == was, index
    assert_main_gate(reply)
    assert md.status_summary(now=NOW, paths=paths)["render_error"] == "bubble:_card_flow:TooLarge"


def test_f2_12_the_limit_is_exactly_the_summary_bubble_budget(md, paths, monkeypatch):
    write_snapshot(paths, make_snapshot(NOW))
    # 找出剛好等於上限的內容長度：多一個字就超過
    for extra in range(-600, 10):  # 泡泡本身的結構約 0.5 KB：長度要比『上限÷3』少幾百個字才剛好壓線
        if md.card_bytes(big_bubble(md, "x", None, extra)) > md.SUMMARY_BUBBLE_MAX_BYTES:
            over = extra
            break
    else:
        raise AssertionError("no oversized bubble found")
    ctx = md._Ctx(make_snapshot(NOW), NOW)
    fits, errors = md._guarded_bubbles(ctx, [("x", lambda c, h, s: big_bubble(md, h, s, over - 1))], max_bytes=md.SUMMARY_BUBBLE_MAX_BYTES)
    assert errors == () and md.card_bytes(fits[0]) <= md.SUMMARY_BUBBLE_MAX_BYTES
    too_big, errors = md._guarded_bubbles(ctx, [("x", lambda c, h, s: big_bubble(md, h, s, over))], max_bytes=md.SUMMARY_BUBBLE_MAX_BYTES)
    assert errors == ("<lambda>:TooLarge",) and texts(too_big[0]["body"]) == [md.TEXT_BUBBLE_FAILED]
    # 上限是呼叫時才讀的模組常數：測試（和之後的調整）改它看得到
    monkeypatch.setattr(md, "SUMMARY_BUBBLE_MAX_BYTES", 500)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.render_errors and all(error.endswith(":TooLarge") for error in reply.render_errors)


def test_f2_12_detail_carousels_have_no_per_bubble_cap_so_the_worst_news_card_still_goes_out(md, paths, monkeypatch):
    """期權、新聞細節從來沒有單張的上限（最壞值的新聞 1/3 已經 6.3 KB）；多加一個上限等於多一種失敗方式，所以只有摘要輪播傳 max_bytes。"""
    write_snapshot(paths, worst_snapshot(NOW))
    for body in (" 新聞", " 期權"):
        reply = md.handle(body, now=NOW, paths=paths)
        assert reply.flex is not None and reply.render_errors == (), body
    monkeypatch.setattr(md, "SUMMARY_BUBBLE_MAX_BYTES", 1000)  # 就算摘要的上限被調得很小，細節卡不受影響
    for body in (" 新聞", " 期權"):
        assert md.handle(body, now=NOW, paths=paths).render_errors == (), body


# ═════════════════════════════════════════════════════════════════════════════
# F2-13：ETF 細節 altText 不要雙括號
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_13_the_etf_alt_text_has_one_pair_of_parentheses(md, paths):
    snap = make_snapshot(NOW)
    alt = lambda: md.handle(" VOO", now=NOW, paths=paths).alt_text  # noqa: E731
    write_snapshot(paths, snap)
    assert alt() == "盤勢：VOO 細節｜收盤 612.34（+0.52%）、在月線上｜資料 10/16"  # 對照：正式收盤
    snap["rows"]["us.etf.VOO"]["source"] = CBOE
    write_snapshot(paths, snap)
    assert alt() == "盤勢：VOO 細節｜收盤 612.34（暫定，+0.52%）、在月線上｜資料 10/16"
    snap["rows"]["us.etf.VOO"]["values"]["chg_pct"] = None
    write_snapshot(paths, snap)
    assert alt() == "盤勢：VOO 細節｜收盤 612.34（暫定）、在月線上｜資料 10/16"
    for change, source in itertools.product((None, 0.52, -12.34), ("合成", CBOE)):
        snap["rows"]["us.etf.VOO"].update(source=source)
        snap["rows"]["us.etf.VOO"]["values"]["chg_pct"] = change
        write_snapshot(paths, snap)
        assert "）（" not in alt(), (change, source)


# ═════════════════════════════════════════════════════════════════════════════
# F2-14：第 6 列退回「川普近 48 小時」時，摘要 footer 補 TACO 的狀態句
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_14_the_footer_says_why_the_sixth_row_changed(md):
    snap = digest_snapshot()
    assert not any(note.startswith("川普壓力指數：") for note in digest_footer(md, snap))  # 對照：指數正常，第 6 列放它
    snap["rows"]["macro.TACO"]["stale_after"] = NOW - 1
    assert digest_texts(md, snap)[5].startswith("川普近 48 小時")
    footer = digest_footer(md, snap)
    assert "川普壓力指數：過期（資料日 10/16）" in footer and MEANING not in footer and SOURCE not in footer
    snap["rows"]["macro.TACO"] = _never()
    assert TACO_STATE in digest_footer(md, snap)
    snap = digest_snapshot()
    snap["rows"]["macro.TACO"]["values"]["percentile"] = 101  # 壞掉的值：也是暫缺
    assert TACO_STATE in digest_footer(md, snap)
    del snap["rows"]["macro.TACO"]  # 舊快照沒有這一列
    assert TACO_STATE in digest_footer(md, snap)
    assert md.row_width("川普壓力指數：過期（資料日 10/16）") <= 24  # 狀態句是 _taco_line 的原句（同一組固定句）


# ═════════════════════════════════════════════════════════════════════════════
# F2-15：財報行標題
# ═════════════════════════════════════════════════════════════════════════════
def test_f2_15_the_earnings_line_says_it_is_about_the_top_ten(md, paths):
    write_snapshot(paths, make_snapshot(NOW))
    lines = texts(md.handle(" VOO", now=NOW, paths=paths).flex)
    head = "前十大 45 天內財報（美東日期）"
    assert market_calc.TEXT_EARNINGS_HEAD == head == md.TEXT_EARNINGS_HEAD and head in market_calc.FIXED_TEXTS
    assert any(line.startswith(f"{head}：") for line in lines)
    assert not any(line.startswith("45 天內財報") for line in lines)


# ═════════════════════════════════════════════════════════════════════════════
# F2-16：顯示用的百分比與帶正負號的小數改四捨五入進位（ROUND_HALF_UP）
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("value, digits, plain, signed", [
    (62.5, 0, "63%", "+63"),  # 銀行家捨入是 62
    (0.5, 0, "1%", "+1"),  # 銀行家捨入是 0
    (1.5, 0, "2%", "+2"),
    (2.5, 0, "3%", "+3"),  # 銀行家捨入是 2
    (88.0, 0, "88%", "+88"),
    (1.45, 1, "1.5%", "+1.5"),  # round(1.45, 1) 是 1.4
    (3.15, 1, "3.2%", "+3.2"),  # 3.1
    (5.85, 1, "5.9%", "+5.9"),  # 5.8
    (-0.85, 1, "-0.9%", "-0.9"),  # -0.8
    (2.675, 2, "2.68%", "+2.68"),  # 2.67
    (0.04, 1, "0.0%", "0.0"),  # 進位後是 0：不帶正負號
    (-0.04, 1, "-0.0%", "0.0"),
    (-0.004, 2, "-0.00%", "0.00"),
    (1234.5, 1, "1234.5%", "+1,234.5"),
])
def test_f2_16_rounding_is_half_up(md, value, digits, plain, signed):
    assert md._plain_pct(value, digits) == plain
    assert md._signed(value, digits) == signed
    assert md._pct(value, digits) == signed + "%"


def test_f2_16_rounding_agrees_with_an_independent_decimal_reference(md):
    """隨機的一到兩位小數（以整數除以 100 造出來，所以十進位的值是確定的）：和獨立寫的整數運算參考答案逐一比對。"""
    rng = random.Random(20261028)
    for _ in range(20000):
        k = rng.randint(-10_000_000, 10_000_000)
        value = k / 100
        for digits in (0, 1, 2):
            reference = Decimal(k).scaleb(-2).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
            assert md._plain_pct(value, digits) == f"{reference:.{digits}f}%", (value, digits)
            expected = f"{0:.{digits}f}" if reference == 0 else f"{reference:+,.{digits}f}"
            assert md._signed(value, digits) == expected, (value, digits)


def test_f2_16_the_season_card_and_the_election_line_use_the_same_rounding(md, paths):
    snap = make_snapshot(NOW)  # SOX 62.5％、S&P 500 60.5％、Polymarket 眾院 0.905
    write_snapshot(paths, snap)
    season = texts(md.handle(" 季節性", now=NOW, paths=paths).flex)
    assert "上漲比例 63%（32 年），中位數 +3.3%、平均 +2.5%" in season  # SOX
    assert "上漲比例 61%（76 年），中位數 +0.9%、平均 +1.1%" in season  # S&P 500
    macro = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["body"])
    assert "距期中選舉 17 天，Polymarket 民主黨拿下眾院 91%" in macro


def test_f2_16_the_other_percent_and_percentile_displays_round_half_up_too(md, paths):
    """除了 _plain_pct／_signed，卡上其他『百分比、百分位』也一律四捨五入進位（以前是 f"{x:.1f}"：看二進位表示，平手往偶數）。
    每個值都挑『剛好在正中間、又能用二進位精確表示』的數（18.25、40.5、5.125…）：舊寫法會多印小的那一個。"""
    snap = make_snapshot(NOW)
    rows = snap["rows"]
    rows["us.options.QQQ"]["values"]["iv30"] = 18.25  # IV30：18.2 → 18.3
    rows["us.etf.VOO"]["values"]["vol_pct250"] = 40.5  # 近 250 日百分位：40 → 41
    rows["tw.turnover"]["values"]["vol_pct250"] = 30.5  # 31
    rows["macro.UST10Y"]["values"]["value"] = 5.125  # 10 年債殖利率：5.12 → 5.13
    rows["tw.options"]["values"]["pcr_oi_pct"] = 60.25  # 台指 PCR：60.2 → 60.3
    for symbol in ("VOO", "QQQ", "SOXX"):
        first = rows[f"us.13f.{symbol}"]["values"]["rows"][0]
        first.update(weight=9.25, own_pct=65.5)  # 權重 9.2 → 9.3、機構持股 65 → 66
    write_snapshot(paths, snap)
    options = texts(md.handle(" 期權", now=NOW, paths=paths).flex)
    assert any("隱含波動率（IV30）18.3%" in line for line in options)
    assert "賣買權未平倉比（PCR）60.3%（全部台指選擇權，和月選範圍不同）" in options
    assert "量能：0.78 倍（前 20 日平均），近 250 日第 41 百分位，量偏少" in texts(md.handle(" VOO", now=NOW, paths=paths).flex)
    assert any(line.endswith("，近 250 日第 31 百分位") for line in texts(md.handle(" 0050", now=NOW, paths=paths).flex))
    news = texts(md.handle(" 新聞", now=NOW, paths=paths).flex)
    assert "美債 10 年（UST10Y）：5.13%（+0.12 個百分點）｜10/16" in news
    macro = texts(summary_bubble(md, md.handle("", now=NOW, paths=paths).flex, "宏觀與事件")["body"])
    assert "美債 10 年（UST10Y）5.13%（+0.12）" in macro
    assert digest_texts(md, snap)[3] == "利率：10 年債 5.13%（+12bp）"
    holdings = texts(md.handle(" 13F QQQ", now=NOW, paths=paths).flex)
    assert any(" 權重 9.3%" in line for line in holdings) and any("；機構持股 66%" in line for line in holdings)


def test_f2_16_rounding_never_raises_on_odd_numbers(md):
    for value in (float("nan"), float("inf"), -float("inf"), 1e15, -1e15, 0.0, -0.0, 5, True):
        for digits in (0, 1, 2):
            assert isinstance(md._signed(value, digits), str) and isinstance(md._plain_pct(value, digits), str)


def test_f2_16_the_under_one_percent_test_matches_the_printed_distance(md):
    """判斷和印出來的字不能分家：round(0.95, 1) 是 0.9（二進位的 0.95 比 0.95 小一點），『不到 1%』會成立，印出來卻是 +1.0%。
    掃 -1.3 到 +1.3、每 0.01 一格：第 3 列有寫牆距 ⇔ 印出來的距離絕對值不到 1.0。"""
    checked = 0
    for k in range(-130, 131):
        dist = k / 100
        snap = digest_snapshot()
        set_options(snap, call=dist, put=-9.0)
        text = md._digest_risk_row(row_ctx(md, snap))[0]
        printed = md._pct(dist, 1)
        shown_value = abs(Decimal(printed.rstrip("%")))
        assert ("QQQ 買權牆距" in text) == (shown_value < 1), (dist, printed, text)
        if "QQQ 買權牆距" in text:
            assert text.endswith(f"QQQ 買權牆距 {printed}"), (dist, text)
        checked += 1
    assert checked == 261


# ═════════════════════════════════════════════════════════════════════════════
# 全部：固定句、每個變體、最壞值、舊快照
# ═════════════════════════════════════════════════════════════════════════════
NEW_FIXED = ("TEXT_TW_CLOSED", "TEXT_TW_CLOSED_TENTATIVE", "TEXT_TACO_POSITION", "TEXT_TACO_DATA_DAY",
             "TEXT_EARNINGS_HEAD", "TEXT_SUMMARY_ALT_FALLBACK")  # 名字而不是值：舊程式沒有的名字只讓這幾個測試紅，不讓整個檔案載入失敗


@pytest.mark.parametrize("name", NEW_FIXED)
def test_new_fixed_sentences_pass_every_gate_in_every_year(name):
    text = getattr(market_calc, name)
    assert text in market_calc.FIXED_TEXTS
    assert not any(word in text for word in market_calc.FORBIDDEN_WORDS)
    assert not any(str(year) in text for year in range(2020, 2040))
    filled = text.format(md="10/23", since="2025-03-26", n=394, p=88, hi="+17.3", hi_md="2026-03-27", lo="-5.0",
                         lo_md="2025-05-20") if "{" in text else text
    for now in ALL_NOWS:
        result = output_validator.validate_outbound_text(filled, now=datetime.fromtimestamp(now, TPE))
        assert result.ok and result.text == filled, (filled, now, result.reason)


def everything_on(now=NOW, *, worst=False):
    """重點摘要所有標示同時開著：落後的資料日、暫定收盤、SOXX 的牆、美股尚未取得、台股休市、川普壓力指數落後一個場次。
    日期固定用 10/15（落後）與 10/16（預期的最新場次）；rows 的新鮮度與快照產生時間隨 now 走，所以三個年份都是真的「全部開著」。"""
    snap = worst_snapshot(now) if worst else make_snapshot(now)
    rows = snap["rows"]
    for sym in ("VOO", "QQQ", "SOXX"):
        rows[f"us.etf.{sym}"]["data_date"] = PREV.isoformat()
        rows[f"us.etf.{sym}"]["values"]["above_ma20"] = True
    rows["us.etf.VOO"]["source"] = CBOE
    rows["macro.VIX"]["values"]["value"] = 18.04
    for row_id in ("macro.VIX", "macro.UST10Y", "macro.TACO"):
        rows[row_id]["data_date"] = PREV.isoformat()
    rows["us.options.SOXX"]["values"].update(put_wall_dist_pct=-0.4)
    rows["us.options.SOXX"]["data_date"] = PREV.isoformat()
    rows["us.options.QQQ"]["values"].update(call_wall_dist_pct=3.0, put_wall_dist_pct=-4.0)
    snap["progress"].update(us_expected_day=DAY.isoformat(), us_waiting_for_source=True, tw_closed_today=True,
                            tw_closed_confirmed=False, tw_expected_day=DAY.isoformat())
    return snap


@pytest.mark.parametrize("worst", [False, True], ids=["normal", "worst"])
def test_everything_on_at_once_stays_inside_the_limits_in_every_year(md, worst):
    for now in ALL_NOWS:
        snap = everything_on(now, worst=worst)
        bubble = md._card_digest(md._Ctx(snap, now), "重點摘要（1/5）", md.TEXT_SWIPE)
        body = [node["text"] for node in bubble["body"]["contents"]]
        assert [t[:2] for t in body] == ["美股", "台股", "風險", "利率", "事件", "川普"], body
        assert all(md.row_width(t) <= 24 and "…" not in t for t in body), body
        footer = texts(bubble["footer"])
        notes = notes_before_disclaimer(footer)
        assert len(notes) == 7, notes  # 資料日、暫定、美股尚未取得、台股休市、牆距基準、意義、來源：一句都沒少、也沒有多的
        assert md.card_bytes(bubble) <= md.SUMMARY_BUBBLE_MAX_BYTES, md.card_bytes(bubble)
        # 整個摘要輪播（5 張）也要在上限內（第 2 輪複審 gp1 的測試缺口，F3-6）。render_errors 也要空：太大的泡泡會被
        # _guarded_bubbles 換成固定短卡，只量總大小會被那張小卡蓋過去
        reply = md._summary_reply(md._Ctx(snap, now))
        assert reply.flex is not None and reply.render_errors == (), (reply.text, reply.render_errors)
        assert len(reply.flex["contents"]) == md.SUMMARY_CARD_COUNT
        assert md.card_bytes(reply.flex) <= md.CARD_MAX_BYTES, md.card_bytes(reply.flex)
        at = datetime.fromtimestamp(now, TPE)
        for value in [*body, *footer]:
            result = output_validator.validate_outbound_text(value, now=at)
            assert result.ok and result.text == value, (value, result.reason)
            assert not md.has_forbidden(value), value


def test_everything_on_at_once_reads_like_this(md):
    """把所有標示開著的實際輸出釘下來（正常值）：日期、點名、暫定、footer 的順序一眼看得到。"""
    snap = everything_on()
    body = [node["text"] for node in md._card_digest(md._Ctx(snap, NOW), "重點摘要（1/5）", md.TEXT_SWIPE)["body"]["contents"]]
    assert body[0] == "美股 10/15（暫定）：標普、那指、半導體都在月線上" and md.row_width(body[0]) == 24.0  # F3-1
    assert body[2] == "風險 10/15：VIX 18.0 平穩，SOXX 賣權牆距 -0.4%"
    assert body[3] == "利率 10/15：10 年債 5.09%（+12bp）"
    assert body[5] == "川普壓力 10/15 +9.0（高壓，任期內第 88 百分位）"  # F3-2：落後時換短名稱，「高壓」留著
    footer = texts(md._card_digest(md._Ctx(snap, NOW), "重點摘要（1/5）", md.TEXT_SWIPE)["footer"])
    assert notes_before_disclaimer(footer) == [
        "資料日：美股 10/15、台股 10/16、VIX 10/15、期權 10/15、10 年債 10/15、川普壓力指數 10/15",
        market_calc.TEXT_PROVISIONAL_CLOSE,
        "美股 10/16 收盤資料尚未取得",
        "台股 10/17 休市（暫定）",
        market_calc.TEXT_WALL_DIST_CLOSE,
        MEANING,
        SOURCE,
    ]


def test_old_snapshots_without_the_new_fields_keep_working(md, paths):
    """舊快照（沒有 progress 的新鍵、沒有 TACO 列、沒有 SOXX 期權列）：不丟例外、六列都在、每列 ≤24。"""
    snap = digest_snapshot()
    snap["progress"] = {"us_members": [520, 520]}
    del snap["rows"]["macro.TACO"]
    del snap["rows"]["us.options.SOXX"]
    write_snapshot(paths, snap)
    reply = md.handle("", now=NOW, paths=paths)
    assert reply.render_errors == () and reply.flex is not None
    bubble = summary_bubble(md, reply.flex, "重點摘要")
    body = [node["text"] for node in bubble["body"]["contents"]]
    assert len(body) == 6 and all(md.row_width(t) <= 24 for t in body)
    assert body[5].startswith("川普近 48 小時") and TACO_STATE in texts(bubble["footer"])
    assert_main_gate(reply)
    assert size(reply.flex) <= 28 * 1024
