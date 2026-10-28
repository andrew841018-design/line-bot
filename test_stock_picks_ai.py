"""AI 相關股推薦（計畫 C，2026-10-24）：條件邊界、排序、卡片、7 個日曆日規則、狀態檔與狀態欄位。

全部用合成資料、不連網。背景計算（_maybe_fill_ai、名單、Yahoo 懶抓、_run 的 hook）在
test_stock_picks_ai_worker.py；00952 網頁與核心名單檔在 test_stock_picks_ai_source.py。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import stock_picks as sp

T = date(2026, 10, 8)
TODAY = date(2026, 10, 24)
ROOT = Path(__file__).resolve().parent


def _facts(code="2330", **kwargs):
    values = dict(code=code, name=f"股{code}", kind=sp.STOCK, data_date=T,
                  trend=sp.Trend(100.0, 100.0, 90.0, 0),
                  revenue=sp.Revenue(2026, 8, 10.0, 30.0), pe=10.0,
                  pe_median=20.0, inst_net=1000, cap=1e12)
    values.update(kwargs)
    return sp.Facts(**values)


def _calendar(t=T):
    days = []
    while len(days) < 240:
        if t.weekday() < 5:
            days.append(t)
        t -= timedelta(days=1)
    return tuple(reversed(days))


def _snapshot(facts=(), *, t=T, pool=True):
    """facts 是 listed 裡的上市股；pool=True 時它們的走勢也在主池裡。常問股（0050、2330）一律在主池，卡片才有常問股那張。"""
    rows = {f.code: sp.ListedRow(f.name, f.kind, "01", 100.0, 0.0, f.pe, f.revenue, f.inst_net)
            for f in facts}
    in_pool = {f.code: f for f in facts} if pool else {}
    for faq in (_facts("0050", kind=sp.ETF, revenue=None, pe=None, pe_median=None), _facts("2330")):
        in_pool.setdefault(faq.code, faq)
    return sp.Snapshot(t=t, calendar=_calendar(t), computed_at=time.time(),
                       market=sp.Market(sp.MET, 0, 0), universe=tuple(rows),
                       picks=tuple(rows)[:3], pool=in_pool,
                       listed=rows, medians={"01": 20.0}, otc=None, missed=None)


def _state(facts=(), *, t=T, source="00952", fail_count=0, universe_as_of=None, ai_source="00952"):
    codes = [f.code for f in facts]
    as_of = (universe_as_of or T).isoformat()
    return dict(schema=1, rule="ai_v1", t=t.isoformat() if t else None,
                universe=dict(as_of=as_of, source=source, codes=codes,
                              etf_codes=codes if ai_source == "00952" else [], fetched_day=T.isoformat()),
                picks=[dict(sp._facts_json(f), ai_source=ai_source) for f in facts],
                funnel=dict(universe=len(codes), growth=len(codes), cheap=len(codes), trend=len(codes)),
                computed_at=time.time() if t else 0.0, last_try=time.time(), fail_count=fail_count,
                core_as_of=T.isoformat(), core_version="test-v1")


@pytest.fixture(autouse=True)
def _isolate_ai(monkeypatch):
    monkeypatch.setattr(sp, "AI_ENABLED", True)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", False)
    monkeypatch.setattr(sp, "_taipei_today", lambda: TODAY)


def _checks(facts):
    return {check.key: check for check in sp.ai_checks_of(facts)}


# ── 四個條件都用卡片上顯示的精度判斷 ────────────────────────────────────────
@pytest.mark.parametrize("monthly,expected", [(0.049, False), (0.05, True), (0.0, False), (-5.0, False), (0.051, True)])
def test_monthly_growth_display_boundary(monthly, expected):
    facts = _facts(revenue=sp.Revenue(2026, 8, monthly, 30.0))
    assert _checks(facts)["growth"].state is expected
    assert sp.ai_eligible(facts) is expected


@pytest.mark.parametrize("cumulative,expected", [(14.949, False), (14.95, True), (15.0, True), (0.0, False)])
def test_cumulative_growth_display_boundary(cumulative, expected):
    facts = _facts(revenue=sp.Revenue(2026, 8, 10.0, cumulative))
    assert _checks(facts)["growth"].state is expected


@pytest.mark.parametrize("pe,median,expected", [
    (4.949, 20.0, False), (4.95, 20.0, True), (5.0, 20.0, True),  # 下限 5.0（顯示精度）
    (20.049, 20.0, True), (20.05, 20.0, False),  # 不高於同業中位數（顯示精度）
    (20.05, 20.05, True), (20.0, 19.949, False), (20.0, 19.95, True),
])
def test_pe_display_boundary(pe, median, expected):
    assert _checks(_facts(pe=pe, pe_median=median))["cheap"].state is expected


@pytest.mark.parametrize("percentage,expected", [
    (10.0, True), (10.049, True), (10.05, False), (10.1, False), (-14.1, True), (-50.0, True),
])
def test_quarter_distance_display_boundary(percentage, expected):
    close = 100.0 * (1 + percentage / 100)
    trend = sp.Trend(close, 100.0, min(close, 50.0), sp._permille(percentage / 100))
    assert _checks(_facts(trend=trend))["trend"].state is expected


@pytest.mark.parametrize("close,ma240,expected", [
    (100.0, 100.0, True),  # 相等算在年線之上
    (100.004, 100.0, True), (99.996, 100.0, True),  # 顯示到分，差在顯示精度以下＝相等
    (99.99, 100.0, False), (99.0, 100.0, False),
])
def test_year_line_equality_uses_displayed_prices(close, ma240, expected):
    trend = sp.Trend(close, close, ma240, 0)
    assert _checks(_facts(trend=trend))["trend"].state is expected


def test_general_rules_are_unchanged_by_the_ai_rule():
    facts = _facts(trend=sp.Trend(100.0, 100.0, 100.0, 0), revenue=sp.Revenue(2026, 8, 10.0, 14.949))
    assert sp._growth_check(facts.revenue).state is True  # 單檔卡：累計年增 > 0 就算有在成長
    assert _checks(facts)["growth"].state is False  # AI 推薦：累計要 ≥ 15.0%
    assert [c.key for c in sp.ai_checks_of(facts)] == ["ai", "growth", "cheap", "trend"]
    assert len(sp.checks_of(facts)) == 5 and sp.CONDITION_ORDER == (sp.LONG, sp.MID, sp.GROWTH, sp.CHEAP, sp.INST)
    # 靠近季線仍是 ±5%：比季線高 8% 在單檔卡是 ⚠️，在 AI 推薦（只要不高過 10%）是符合
    far = _facts(trend=sp.Trend(108.0, 100.0, 90.0, 80))
    assert sp.light_of(far).level == sp.CAUTION and _checks(far)["trend"].state is True


@pytest.mark.parametrize("changes", [
    {"revenue": None}, {"revenue": sp.Revenue(2026, 8, None, 30.0)},
    {"revenue": sp.Revenue(2026, 8, 10.0, None)}, {"revenue": sp.Revenue(2026, 8, float("nan"), 30.0)},
    {"pe": None}, {"pe": 0.0}, {"pe": -1.0}, {"pe": float("nan")}, {"pe": float("inf")},
    {"pe_median": None}, {"pe_median": 0.0}, {"pe_median": float("nan")}, {"trend": None},
    {"kind": sp.ETF}, {"kind": sp.OTC}, {"market": sp.TPEX},
])
def test_missing_or_invalid_data_is_never_eligible(changes):
    assert _facts() and sp.ai_eligible(_facts())
    assert not sp.ai_eligible(_facts(**changes))


def test_empty_source_is_not_ai_related():
    assert sp.ai_eligible(_facts(), "00952") and sp.ai_eligible(_facts(), "AI 伺服器")
    assert not sp.ai_eligible(_facts(), "")


def test_growth_text_does_not_repeat_january():
    january = _checks(_facts(revenue=sp.Revenue(2026, 1, 20.0, 20.0)))["growth"].text
    assert january == "1 月營收比去年同期多 20.0%" and "1–1" not in january
    assert _checks(_facts())["growth"].text == "8 月營收比去年同期多 10.0%，1–8 月累計多 30.0%"
    assert _checks(_facts(revenue=sp.Revenue(2026, 8, -3.0, 30.0)))["growth"].text.startswith("8 月營收比去年同期少 3.0%")
    assert _checks(_facts())["cheap"].text == "本益比 10.0 倍，同產業中位數 20.0 倍"
    assert _checks(_facts())["trend"].text == "在年線之上，比季線高 0.0%"
    far = sp.Trend(85.0, 100.0, 80.0, -150)
    assert _checks(_facts(trend=far))["trend"].text == "在年線之上，比季線低 15.0%"


# ── 排序：累計營收年增 ÷ 本益比（原始數值），同分按代號 ─────────────────────────
def test_sort_uses_growth_to_pe_ratio_not_growth_alone():
    steady = _facts("1101", revenue=sp.Revenue(2026, 8, 10.0, 60.0), pe=20.0, pe_median=25.0)  # 3.0
    cheap = _facts("1102", revenue=sp.Revenue(2026, 8, 10.0, 40.0), pe=8.0)  # 5.0
    assert [f.code for f in sorted([steady, cheap], key=sp.ai_sort_key)] == ["1102", "1101"]


def test_sort_uses_raw_values_then_code_and_ignores_market_cap():
    low = _facts("1101", revenue=sp.Revenue(2026, 8, 10.0, 30.01))
    high = _facts("2330", revenue=sp.Revenue(2026, 8, 10.0, 30.04))  # 顯示都是 30.0，原始數值不同
    tie = replace(high, code="1216", cap=1.0)
    assert [f.code for f in sorted([low, high, tie], key=sp.ai_sort_key)] == ["1216", "2330", "1101"]


def test_sort_puts_unusable_denominators_last_without_dividing():
    good = _facts("2330")
    bad = [_facts(f"11{n:02d}", pe=pe) for n, pe in enumerate([0.0, -2.0, float("nan"), None, float("inf")])]
    ordered = [f.code for f in sorted([*bad, good], key=sp.ai_sort_key)]
    assert ordered[0] == "2330" and ordered[1:] == sorted(f.code for f in bad)


def test_candidates_apply_conditions_one_to_three_and_count_growth():
    names = {
        "1101": _facts("1101"),
        "1102": _facts("1102", revenue=sp.Revenue(2026, 8, 10.0, 14.9)),  # 成長不合格
        "1103": _facts("1103", pe=25.0),  # 成長合格、太貴
        "1104": _facts("1104", pe=4.9),  # 成長合格、本益比低於 5.0
        "1105": _facts("1105", revenue=sp.Revenue(2026, 8, 10.0, 90.0), pe=9.0),
        "1106": _facts("1106", kind=sp.ETF),
    }
    snapshot = _snapshot(list(names.values()))
    sources = {code: "00952" for code in names}
    candidates, growth = sp.ai_candidates(snapshot, [*names, "9999"], sources)  # 9999 不在 listed：略過
    assert [f.code for f in candidates] == ["1105", "1101"]  # 90.0/9.0 排在 30.0/10.0 前面
    assert growth == 4  # 1101、1103、1104、1105 營收合格（1102 累計 14.9% 不到、ETF 不是 AI 相關股）


# ── 卡片：零檔和「還沒有結果」是不同狀態 ───────────────────────────────────────
def _reply(monkeypatch, facts=(), *, ai=None, t=T, seen=None):
    monkeypatch.setattr(sp, "_snapshot", _snapshot(facts, t=t))
    monkeypatch.setattr(sp, "_ai_state", ai)
    sp._worker.last_ok_check = time.time()
    sp._worker.seen_t = seen or t
    return sp.handle("")


def _text(reply):
    return json.dumps(reply.flex, ensure_ascii=False)


def _texts(reply):
    from test_stock_picks import _card_texts
    return _card_texts(reply.flex)


APPROVED = (sp.TEXT_LEAD_NOTE, sp.TEXT_DISCLAIMER)


def _assert_state_words_only(reply):
    """爸媽看的字：只描述狀態，不寫買、賣、等、停損（兩句核准的固定句「買賣自己決定」「不是買賣指示」除外）。"""
    for text in [reply.alt_text, *_texts(reply)]:
        for sentence in APPROVED:
            text = text.replace(sentence, "")
        for word in ("買", "賣", "等", "停損"):
            assert word not in text, (word, text)


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_cards_zero_through_three_are_sendable(monkeypatch, count):
    from test_stock_picks import _assert_sendable
    facts = [_facts(str(1101 + n)) for n in range(count)]
    reply = _reply(monkeypatch, facts, ai=_state(facts))
    _assert_sendable(reply.flex, reply.alt_text, "股票推薦：只供參考｜")
    _assert_state_words_only(reply)
    text = _text(reply)
    assert "AI 相關股推薦" in text and "AI 推薦" not in text
    assert len(reply.flex["contents"]) == max(count, 1) + 1  # 推薦卡（零檔也有一張）＋常問股
    assert "市值前 50" not in text
    assert text.count(sp.TEXT_LEAD_NOTE) == 1 and text.count("從 AI 相關股挑") == (1 if count else 0)  # 導言只在第一張卡
    if count:
        assert text.count("4 條都符合") == count
        assert text.count("凱基台灣AI50 持股（名單 10/08）") == count
        assert "從 AI 相關股挑：凱基台灣AI50 持股＋核心名單（名單 10/08），最多 3 檔" in text
        assert reply.alt_text == "股票推薦：只供參考｜" + "、".join(f.name for f in facts) + "｜資料到 10/08"
        assert [t for t in _texts(reply) if t.startswith("AI 相關股推薦：第")] == [
            f"AI 相關股推薦：第 {n + 1} 檔（共 {count} 檔）" for n in range(count)]
    else:
        assert "AI 名單上市 0 檔 → 營收成長 0 檔 → 本益比不高於同業 0 檔 → 走勢符合 0 檔" in text
        assert "計算中" not in text and "最常差" not in text  # 零檔改寫篩選漏斗，不寫「最常差哪一條」
        assert reply.alt_text.startswith("股票推薦：只供參考｜AI 名單上市 0 檔")


def test_pick_card_shows_reasons_as_dedicated_sentences(monkeypatch):
    facts = [_facts("1101", revenue=sp.Revenue(2026, 8, 12.34, 56.78), pe=9.0, pe_median=15.5,
                    trend=sp.Trend(104.0, 100.0, 90.0, 40))]
    texts = _texts(_reply(monkeypatch, facts, ai=_state(facts)))
    assert "股1101 1101" in texts and "✅ 4 條都符合" in texts and "收盤 104 元（10/08）" in texts
    assert "AI 相關：凱基台灣AI50 持股（名單 10/08）" in texts
    assert "成長：8 月營收比去年同期多 12.3%，1–8 月累計多 56.8%" in texts
    assert "便宜：本益比 9.0 倍，同產業中位數 15.5 倍" in texts
    assert "走勢：在年線之上，比季線高 4.0%" in texts
    assert "依據：證交所、Yahoo｜資料到 10/08" in texts and sp.TEXT_DISCLAIMER in texts
    assert sp.TEXT_LEAD_NOTE in texts
    assert "大盤：中性（10/08）" in texts


def test_core_list_picks_name_their_fixed_segment(monkeypatch):
    facts = [_facts("2368", name="金像電")]
    state = _state(facts, source="cached", ai_source="AI 伺服器 PCB")
    text = _text(_reply(monkeypatch, facts, ai=state))
    assert "AI 相關：核心名單（AI 伺服器 PCB）" in text and "凱基台灣AI50 持股（" not in text


@pytest.mark.parametrize("source,expected", [
    ("00952", "從 AI 相關股挑：凱基台灣AI50 持股＋核心名單（名單 10/07），最多 3 檔"),
    ("cached", "從 AI 相關股挑：凱基台灣AI50 持股＋核心名單（名單 10/07），最多 3 檔"),
    ("core_only", "從 AI 相關股挑：核心名單（名單 10/07），最多 3 檔"),  # 沒有 00952 時不說有凱基
])
def test_lead_sentence_follows_the_universe_source(monkeypatch, source, expected):
    facts = [_facts("1101")]
    ai_source = "00952" if source != "core_only" else "AI 伺服器"
    state = _state(facts, source=source, universe_as_of=T - timedelta(days=1), ai_source=ai_source)
    assert expected in _texts(_reply(monkeypatch, facts, ai=state))


def test_initial_pending_and_repeated_failure_have_distinct_cards(monkeypatch):
    from test_stock_picks import _assert_sendable
    pending = _reply(monkeypatch)
    _assert_sendable(pending.flex, pending.alt_text, "股票推薦：只供參考｜")
    assert "AI 相關股推薦計算中，請稍後再按一次" in _text(pending)
    for fails, word in [(0, "計算中"), (2, "計算中"), (3, "暫時算不出來"), (7, "暫時算不出來")]:
        failed = _state(t=None, fail_count=fails)
        reply = _reply(monkeypatch, ai=failed)
        _assert_sendable(reply.flex, reply.alt_text, "股票推薦：只供參考｜")
        _assert_state_words_only(reply)
        assert (f"AI 相關股推薦{word}" in _text(reply)) and (word == "計算中") == ("暫時算不出來" not in _text(reply))
    assert len(reply.flex["contents"]) == 2  # 推薦區文字卡＋常問股卡；常問股照常


def test_no_result_yet_is_not_a_valid_zero_pick_state(monkeypatch):
    zero = _reply(monkeypatch, ai=_state([]))
    assert "AI 名單上市 0 檔" in _text(zero) and "計算中" not in _text(zero)
    none = _reply(monkeypatch, ai=_state(t=None))
    assert "AI 名單上市" not in _text(none) and "計算中" in _text(none)


def test_switch_off_restores_legacy_cards_and_footer(monkeypatch):
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    facts = _facts()
    text = _text(_reply(monkeypatch, [facts], ai=_state([facts])))
    assert sp.TEXT_POOL_NOTE in text and "AI 相關股推薦" not in text and sp.TEXT_AI_POOL_NOTE not in text
    detail = json.dumps(sp.detail_bubble(facts, market=None), ensure_ascii=False)
    assert sp.TEXT_POOL_NOTE in detail and sp.TEXT_AI_POOL_NOTE not in detail
    monkeypatch.setattr(sp, "AI_ENABLED", True)
    detail = json.dumps(sp.detail_bubble(facts, market=None), ensure_ascii=False)
    assert sp.TEXT_AI_POOL_NOTE in detail and sp.TEXT_POOL_NOTE not in detail


# ── 「資料到 M/D」：上一份推薦在 7 個日曆日內照樣顯示（Andrew 10/24：「照樣顯示上一份」） ──────
@pytest.mark.parametrize("old,new,show", [
    (date(2026, 10, 8), date(2026, 10, 9), True),  # 隔 0 個平日
    (date(2026, 10, 9), date(2026, 10, 12), True),  # 週五→週一
    (date(2026, 10, 8), date(2026, 10, 12), True),  # 隔 1 個平日（10/9 剛好是休市日）
    (date(2026, 10, 7), date(2026, 10, 14), True),  # 剛好 7 個日曆日
    (date(2026, 10, 8), date(2026, 10, 15), True),
    (date(2026, 10, 8), date(2026, 10, 16), False),  # 8 個日曆日
    (date(2026, 9, 30), date(2026, 10, 7), True),  # 跨月
    (date(2026, 9, 30), date(2026, 10, 8), False),
    (date(2025, 12, 31), date(2026, 1, 7), True),  # 跨年
    (date(2025, 12, 31), date(2026, 1, 8), False),
])
def test_previous_recommendations_have_seven_calendar_day_window(monkeypatch, old, new, show):
    facts = replace(_facts(), data_date=old)
    reply = _reply(monkeypatch, [facts], ai=_state([facts], t=old), t=new)
    text = _text(reply)
    shown_old, shown_new = sp._fmt_date(old), sp._fmt_date(new)
    if show:
        assert "4 條都符合" in text
        assert f"推薦資料到 {shown_old}，新資料計算中" in text
        assert text.count("推薦資料到") == 1  # 只寫在第一張卡，不是每張都寫
        assert f"（{shown_new}）" in text  # 大盤那行是主快照的日期，兩個日期都看得出來
        assert reply.alt_text == f"股票推薦：只供參考｜{facts.name}｜資料到 {shown_old}"
    else:
        assert "資料暫時沒有更新，先不推薦" in text and "4 條都符合" not in text
        assert reply.alt_text == f"股票推薦：只供參考｜資料暫時沒有更新，先不推薦｜資料到 {shown_old}"
        assert "計算中" not in text  # 超過一週不再說新資料計算中


def test_stale_note_and_lead_sentences_appear_on_the_first_card_only(monkeypatch):
    facts = [_facts(str(1101 + n)) for n in range(3)]
    reply = _reply(monkeypatch, facts, ai=_state(facts), t=T + timedelta(days=1))
    text = _text(reply)
    assert text.count("推薦資料到 10/08，新資料計算中") == 1 and text.count(sp.TEXT_LEAD_NOTE) == 1
    assert text.count("4 條都符合") == 3 and text.count("（10/09）") == 3  # 大盤日期每張都寫


def test_same_day_recommendation_has_no_stale_note(monkeypatch):
    facts = _facts()
    text = _text(_reply(monkeypatch, [facts], ai=_state([facts])))
    assert "推薦資料到" not in text and "4 條都符合" in text


def test_seen_t_newer_than_snapshot_counts_toward_the_window(monkeypatch):
    facts = _facts()
    ai = _state([facts])
    assert "推薦資料到 10/08，新資料計算中" in _text(_reply(monkeypatch, [facts], ai=ai, seen=T + timedelta(days=3)))
    assert "資料暫時沒有更新" in _text(_reply(monkeypatch, [facts], ai=ai, seen=T + timedelta(days=8)))


def test_thirty_six_hours_no_longer_hides_recommendations(monkeypatch):
    facts = _facts()
    _reply(monkeypatch, [facts], ai=_state([facts]))
    sp._worker.last_ok_check = time.time() - 36 * 3600
    assert "4 條都符合" in _text(sp.handle(""))


def test_ai_failure_keeps_date_without_claiming_computation_in_progress(monkeypatch):
    facts = _facts()
    state = _state([facts], fail_count=1)
    reply = _reply(monkeypatch, [facts], ai=state, t=T + timedelta(days=4))
    text = _text(reply)
    assert f"推薦資料到 {sp._fmt_date(T)}" in text and "新資料計算中" not in text and "4 條都符合" in text


def test_zero_pick_state_older_than_snapshot_keeps_the_funnel_and_date(monkeypatch):
    zero = _reply(monkeypatch, ai=_state([]), t=T + timedelta(days=1))
    text = _text(zero)
    assert "推薦資料到 10/08，新資料計算中" in text and "AI 名單上市 0 檔" in text
    assert zero.alt_text.endswith("資料到 10/08")


@pytest.mark.parametrize("elapsed,stale", [(7 * 86400, False), (7 * 86400 + 1, True)])
def test_no_successful_check_for_a_week_is_stale(monkeypatch, elapsed, stale):
    facts = _facts()
    state = _state([facts])
    base = time.time()
    state["computed_at"] = base
    snapshot = replace(_snapshot([facts]), computed_at=base)
    sp._worker.last_ok_check = base
    assert sp._ai_stale(state, snapshot, now=base + elapsed) is stale


@pytest.mark.parametrize("last_ok_age,snapshot_age,state_age,stale", [
    (None, 0, 0, False),  # 剛重啟、工作狀態還沒有「成功檢查」的時間：看快照與推薦自己的計算時間
    (10 * 86400, 0, 10 * 86400, False),  # 檢查很久沒記錄，但快照剛算好
    (10 * 86400, 10 * 86400, 0, False),  # 推薦剛算好
    (0, 10 * 86400, 10 * 86400, False),  # 剛檢查過
    (10 * 86400, 10 * 86400, 10 * 86400, True),  # 三個時間都超過一週
])
def test_week_without_any_sign_of_life_is_stale_only_when_all_three_clocks_are_old(last_ok_age, snapshot_age, state_age, stale):
    facts = _facts()
    now = time.time()
    state = _state([facts])
    state["computed_at"] = now - state_age
    snapshot = replace(_snapshot([facts]), computed_at=now - snapshot_age)
    sp._worker.last_ok_check = None if last_ok_age is None else now - last_ok_age
    assert sp._ai_stale(state, snapshot, now=now) is stale


def test_every_fixed_core_segment_passes_card_guardrails():
    from stock_picks_ai_source import SEGMENTS
    from test_stock_picks import _assert_sendable
    for segment in SEGMENTS:
        card = sp.ai_pick_bubble(_facts(), 1, 1, None, ai_source=segment, lead_note=True)
        _assert_sendable(card, "股票推薦：只供參考｜AI 相關股推薦", "股票推薦：只供參考｜")
        assert f"核心名單（{segment}）" in json.dumps(card, ensure_ascii=False)
    plain = json.dumps(sp.ai_pick_bubble(_facts(), 1, 1, None), ensure_ascii=False)  # 沒給名單日期也不會寫成核心名單
    assert "凱基台灣AI50 持股" in plain and "核心名單（00952）" not in plain


def test_ai_cards_fit_the_large_print_page(monkeypatch):
    import card_pages
    facts = [_facts(str(1101 + n)) for n in range(3)]
    for ai in (_state(facts), _state([]), _state(t=None)):
        card = _reply(monkeypatch, facts, ai=ai).flex
        url = "https://demo.trycloudflare.com/card/" + "A" * 22
        paged = card_pages.with_page_button(card, url)
        assert card_pages.fits_line_limits(paged)
        html = card_pages.render_html(card, "股票推薦")
        assert "AI 相關股推薦" in html and "<script" not in html


def test_handle_reads_memory_only_and_never_waits_for_the_state_lock(monkeypatch):
    facts = _facts()
    _reply(monkeypatch, [facts], ai=_state([facts]))

    def fail(*args, **kwargs):
        raise AssertionError("回覆不應讀檔或抓資料")

    monkeypatch.setattr(sp, "_read_json_file", fail)
    monkeypatch.setattr(sp, "_fetch_chart", fail)
    monkeypatch.setattr(sp, "_state_file_lock", fail)
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with sp._lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(2)
    try:
        def ask():
            assert sp.handle("").alt_text.startswith("股票推薦：只供參考｜")
            sp._block_yahoo()  # 單檔查詢撞到 429 的路徑也不能等 _lock
            done.set()

        asker = threading.Thread(target=ask, daemon=True)
        asker.start()
        assert done.wait(3), "回覆被背景拿著的 _lock 卡住了"
    finally:
        release.set()
        holder.join(2)


# ── AI 狀態獨立驗證，不能覆蓋較新結果 ───────────────────────────────────────
def test_state_round_trip_uses_prefixed_private_file():
    payload = _state([_facts()])
    assert sp._commit_ai(payload) is True
    path = sp.STATE_DIR / "stock_picks_tw_ai.json"
    assert path.exists() and path.stat().st_mode & 0o777 == 0o600
    assert path.name.startswith("stock_picks_tw") and sp.AI_HISTORY_NAME.startswith("stock_picks_tw")
    loaded = sp.load_ai_file()
    assert loaded["t"] == T.isoformat() and loaded["picks"][0]["c"] == "2330" and loaded["picks"][0]["ai_source"] == "00952"
    assert sp._ai_state == loaded
    assert not list(sp.STATE_DIR.glob(".*.tmp"))  # 原子寫入：沒有留下暫存檔


def test_stale_ai_temp_files_are_swept_with_the_main_ones(monkeypatch):
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    old = sp.STATE_DIR / ".stock_picks_tw_ai.json.1.2.3.tmp"
    old.write_text("x")
    import os
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    sp._sweep_stale_tmp()
    assert not old.exists()


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(schema=999),
    lambda p: p.update(t="2099-01-01"),
    lambda p: p.update(t="2026-10-8"),
    lambda p: p.update(fail_count=-1),
    lambda p: p.update(fail_count=True),
    lambda p: p["picks"][0].update(ai_source="買進建議"),
    lambda p: p["picks"][0].update(ai_source="AI 伺服器 "),
    lambda p: p["picks"][0].update(c="２３３０"),
    lambda p: p["picks"][0].update(c="9103"),
    lambda p: p["picks"][0].update(n="測試-DR"),
    lambda p: p["picks"][0].update(d="2026-10-07"),
    lambda p: p["picks"][0].update(rv=[2026, 8, -1.0, 30.0]),
    lambda p: p["picks"][0].update(tr=None),
    lambda p: p["picks"][0].update(m="tpex"),
    lambda p: p["universe"].update(source="unknown"),
    lambda p: p["universe"].update(codes=["2330"] * 2),
    lambda p: p["universe"].update(codes=["１１０１"]),
    lambda p: p["universe"].update(etf_codes=[]),
    lambda p: p["universe"].update(codes=["1101"]),  # 入選的代號必須在名單裡
    lambda p: p.update(picks=p["picks"] * 4),
    lambda p: p.update(picks=p["picks"] * 2),
    lambda p: p["funnel"].update(trend=2),
    lambda p: p["funnel"].update(cheap=0),
    lambda p: p["funnel"].update(universe=2),  # 漏斗由大到小沒錯，但第一格不等於名單的檔數
    lambda p: p.update(computed_at=-1),
    lambda p: p.update(last_try="yesterday"),
    lambda p: p.update(core_version="x" * 65),
    lambda p: p.update(core_as_of="2099-01-01"),
    lambda p: p["universe"].update(as_of="2099-01-01"),
    lambda p: p["universe"].update(fetched_day="2099-01-01"),
    lambda p: p["universe"].update(codes=[str(1000 + n) for n in range(121)]),
    lambda p: p.update(holdings=dict(codes=["２０００"] + [str(2001 + n) for n in range(49)], as_of="2026-10-08", fetched_day="2026-10-08")),
    lambda p: p.update(holdings=dict(codes=[str(2000 + n) for n in range(50)], as_of="2099-01-01", fetched_day="2026-10-08")),
    lambda p: p.update(holdings=dict(codes=[str(2000 + n) for n in range(50)], as_of="2026-10-08", fetched_day="2099-01-01")),
    lambda p: p["funnel"].update(universe=121),
    lambda p: p.update(holdings=dict(codes=[str(2000 + n) for n in range(61)], as_of="2026-10-08", fetched_day="2026-10-08")),
    lambda p: p.update(holdings=dict(codes=["1101"] * 45, as_of="2026-10-08", fetched_day="2026-10-08")),
    lambda p: p.update(holdings=dict(codes=[str(2000 + n) for n in range(39)], as_of="2026-10-08", fetched_day="2026-10-08")),
    lambda p: p.update(fetch_budget=dict(day="2026-10-24", attempts=4, next_try=0.0)),
    lambda p: p.update(fetch_budget=dict(day="2026-10-25", attempts=1, next_try=0.0)),
])
def test_corrupt_state_rejected(mutation):
    payload = _state([_facts()])
    mutation(payload)
    with pytest.raises((ValueError, TypeError)):
        sp.ai_state_from_json(payload)


def test_pick_count_must_equal_the_last_funnel_cell():
    payload = _state([_facts("2330"), _facts("1101")])
    payload["funnel"]["trend"] = 1  # 2,2,2,1：由大到小沒錯，但入選兩檔、漏斗最後一格卻寫 1
    with pytest.raises(ValueError):
        sp.ai_state_from_json(payload)
    payload["funnel"]["trend"] = 2
    assert len(sp.ai_state_from_json(payload)["picks"]) == 2


def test_duplicate_picks_are_rejected_even_when_the_counts_agree():
    payload = _state([_facts("2330"), _facts("1101")])
    assert len(sp.ai_state_from_json(payload)["picks"]) == 2
    payload["picks"] = [payload["picks"][0], payload["picks"][0]]  # 同一檔寫兩次；名單與漏斗都說 2 檔
    with pytest.raises(ValueError):
        sp.ai_state_from_json(payload)


def test_at_most_three_distinct_picks_are_accepted():
    three = [_facts(str(1101 + n)) for n in range(3)]
    assert len(sp.ai_state_from_json(_state(three))["picks"]) == 3
    with pytest.raises(ValueError):  # 四檔都合格、名單與漏斗也彼此一致：只有「最多 3 檔」擋得住
        sp.ai_state_from_json(_state([*three, _facts("1104")]))


@pytest.mark.parametrize("count", [40, 60])
def test_holdings_boundaries_are_accepted(count):
    payload = _state([_facts()])
    payload["holdings"] = dict(codes=[str(2000 + n) for n in range(count)], as_of="2026-10-08", fetched_day="2026-10-08")
    assert sp.ai_state_from_json(payload)["holdings"]["codes"][0] == "2000"


def test_no_result_state_must_be_empty():
    payload = _state(t=None)
    sp.ai_state_from_json(payload)  # 還沒有結果：合法
    payload["picks"] = [dict(sp._facts_json(_facts()), ai_source="00952")]
    with pytest.raises(ValueError):
        sp.ai_state_from_json(payload)
    # 沒有推薦、但名單與漏斗有數字（其他欄位彼此一致）：不是「還沒有結果」，是壞檔——零檔必須有 T
    payload = _state(t=None)
    payload["universe"]["codes"] = ["1101"]
    payload["funnel"]["universe"] = 1
    with pytest.raises(ValueError):
        sp.ai_state_from_json(payload)


def test_a_future_t_is_rejected_even_without_picks():
    payload = _state([])  # 零檔：沒有入選資料的日期可以拿來互相對照，只剩 T 本身的檢查
    sp.ai_state_from_json(payload)
    for future in ("2026-10-25", "2099-01-01"):
        payload["t"] = future
        with pytest.raises(ValueError):
            sp.ai_state_from_json(payload)


def test_future_timestamps_are_clamped_but_future_t_is_not():
    payload = _state([_facts()])
    payload["computed_at"] = payload["last_try"] = time.time() + 100000
    checked = sp.ai_state_from_json(payload)
    assert checked["computed_at"] <= time.time() and checked["last_try"] <= time.time()
    payload["fetch_budget"] = dict(day="2026-10-24", attempts=1, next_try=time.time() + 10**9)
    assert sp.ai_state_from_json(payload)["fetch_budget"]["next_try"] <= time.time() + 86400 + 1


def test_broken_file_load_is_safe_and_status_read_only(monkeypatch):
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = sp.STATE_DIR / "stock_picks_tw_ai.json"
    path.write_text("{broken", encoding="utf-8")
    assert sp.load_ai_file() is None
    monkeypatch.setattr(sp, "_loaded", False)
    before = {p.name: p.read_bytes() for p in sp.STATE_DIR.iterdir()}
    status = sp.status_summary()
    assert status["ai_status"] == "pending" and status["ai_t"] is None and "status" in status and "data_date" in status
    assert {p.name: p.read_bytes() for p in sp.STATE_DIR.iterdir()} == before
    sp._ensure_loaded()  # 壞檔不能讓 /股票 拋例外，主快照照常載入
    assert sp._ai_state is None and sp._loaded


@pytest.mark.parametrize("payload", [b"[]", b'{"schema":1}', b"\xff\xfe", b"x" * (sp.STATE_MAX_BYTES + 1)])
def test_odd_ai_files_never_raise(payload):
    sp.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (sp.STATE_DIR / "stock_picks_tw_ai.json").write_bytes(payload)
    assert sp.load_ai_file() is None


def test_missing_leaf_module_does_not_break_loading_or_replies(monkeypatch):
    facts = _facts()
    assert sp._commit_ai(_state([facts]))
    monkeypatch.setitem(sys.modules, "stock_picks_ai_source", None)  # 葉節點模組缺檔：import 會失敗
    assert sp.load_ai_file() is None  # 讀檔需要固定分類清單：當作沒有，不丟例外
    monkeypatch.setattr(sp, "_loaded", False)
    sp._ensure_loaded()
    assert sp._loaded and sp._ai_state is None
    assert "計算中" in _text(_reply(monkeypatch, [facts], ai=None))  # /股票 照常回覆
    assert sp.status_summary()["ai_status"] == "pending"
    with pytest.raises(ImportError):
        sp._ai_core()  # AI 段自己當失敗（由 _run 接住），不影響主 worker


def test_commit_never_regresses_but_same_t_can_be_replaced():
    payload = _state([_facts()])
    assert sp._commit_ai(payload)
    older = _state([replace(_facts(), data_date=T - timedelta(days=1))], t=T - timedelta(days=1))
    assert not sp._commit_ai(older)
    assert sp.load_ai_file()["t"] == T.isoformat() and sp._ai_state["t"] == T.isoformat()
    replacement = _state([_facts("1101")])
    replacement["core_version"] = "test-v2"
    assert sp._commit_ai(replacement)
    assert sp.load_ai_file()["picks"][0]["c"] == "1101"
    newer = _state([replace(_facts(), data_date=T + timedelta(days=1))], t=T + timedelta(days=1))
    assert sp._commit_ai(newer) and sp.load_ai_file()["t"] == (T + timedelta(days=1)).isoformat()


def test_a_no_result_record_never_overwrites_a_real_one():
    assert sp._commit_ai(_state([_facts()]))
    assert not sp._commit_ai(_state(t=None, fail_count=5))
    assert sp.load_ai_file()["picks"][0]["c"] == "2330" and sp._ai_state["picks"]


def test_unwritable_state_reports_failure_without_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(sp, "_write_json_file", boom)
    assert sp._commit_ai(_state([_facts()])) is False
    assert sp._ai_state is None


def test_ensure_loaded_restores_ai_memory_from_disk(monkeypatch):
    payload = _state([_facts()], fail_count=2)
    payload["holdings"] = dict(codes=[str(2000 + n) for n in range(50)], as_of="2026-10-07", fetched_day="2026-10-24")
    payload["fetch_budget"] = dict(day="2026-10-24", attempts=2, next_try=time.time() + 3000)
    assert sp._commit_ai(payload)
    for name, value in (("_ai_state", None), ("_ai_next_try", 0.0), ("_ai_holdings", None), ("_ai_fetch_budget", {})):
        monkeypatch.setattr(sp, name, value)
    monkeypatch.setattr(sp, "_loaded", False)
    sp._ensure_loaded()
    assert sp._ai_state["t"] == T.isoformat() and sp._ai_state["fail_count"] == 2
    assert sp._ai_next_try == pytest.approx(sp._ai_state["last_try"] + sp.AI_RETRY_S)  # 重啟後失敗的節流還在
    assert sp._ai_holdings["fetched_day"] == "2026-10-24" and len(sp._ai_holdings["codes"]) == 50
    assert sp._ai_fetch_budget["attempts"] == 2  # 今天抓 00952 的帳不會因為重啟歸零


def test_switch_off_does_not_even_read_the_ai_file(monkeypatch):
    assert sp._commit_ai(_state([_facts()]))
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    monkeypatch.setattr(sp, "_ai_state", None)
    monkeypatch.setattr(sp, "_loaded", False)
    sp._ensure_loaded()
    assert sp._ai_state is None
    assert sp.status_summary()["ai_status"] == "disabled"


# ── 推薦紀錄：每個交易日一行，帶 rule ───────────────────────────────────────
def _history_lines():
    path = sp.STATE_DIR / "stock_picks_tw_ai_history.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_history_once_per_day_with_rule_and_never_touches_the_legacy_file():
    payload = _state([_facts()])
    sp._append_ai_history(payload)
    sp._append_ai_history(payload)  # 同一天已經有了：不重寫
    changed = _state([_facts("1101")])
    sp._append_ai_history(changed)  # 同一天換了推薦也不改寫第一筆
    next_day = _state([_facts("1101", data_date=T + timedelta(days=1))], t=T + timedelta(days=1))
    sp._append_ai_history(next_day)
    lines = _history_lines()
    assert [row["t"] for row in lines] == [T.isoformat(), (T + timedelta(days=1)).isoformat()]
    assert lines[0]["rule"] == "ai_v1" and lines[0]["picks"][0] == {
        "code": "2330", "name": "股2330", "close": 100.0, "ai_source": "00952", "p60": 0}
    assert lines[0]["source"] == "00952" and lines[0]["core_version"] == "test-v1"  # 同一天重算（名單、核心換了）時對得上是哪一份
    assert lines[1]["picks"][0]["code"] == "1101"
    assert not (sp.STATE_DIR / "stock_picks_history.jsonl").exists()
    sp._append_ai_history(_state(t=None))  # 還沒有結果的狀態不記
    assert len(_history_lines()) == 2


def test_history_records_a_day_without_picks():
    sp._append_ai_history(_state([]))
    assert _history_lines()[0]["picks"] == []


def test_startup_backfills_a_missing_history_line(monkeypatch):
    assert sp._commit_ai(_state([_facts()]))
    path = sp.STATE_DIR / "stock_picks_tw_ai_history.jsonl"
    assert not path.exists()  # 狀態已落盤、紀錄還沒寫就重啟
    monkeypatch.setattr(sp, "_loaded", False)
    monkeypatch.setattr(sp, "_ai_state", None)
    stop = threading.Event()
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(sp, "_tick", lambda _: stop.set() or 0.0)
    sp._run(stop, threading.Event())
    assert [row["t"] for row in _history_lines()] == [T.isoformat()]


# ── status_summary：新欄位，舊欄位不變 ───────────────────────────────────────
def test_status_summary_ai_fields(monkeypatch):
    status = sp.status_summary()
    assert status["ai_status"] == "pending" and status["ai_t"] is None and status["ai_fail_count"] == 0
    assert status["ai_universe_source"] is None and status["ai_core_as_of"] is None and status["ai_core_status"] is None
    assert {"data_date", "seen_date", "fail_count", "last_fail", "hours_since_check", "backoff_minutes",
            "waiting", "lag_minutes", "status"} <= set(status)  # 舊欄位照舊
    monkeypatch.setattr(sp, "_ai_state", _state([_facts()], source="cached", universe_as_of=T - timedelta(days=2)))
    ok = sp.status_summary()
    assert (ok["ai_status"], ok["ai_t"], ok["ai_universe_source"]) == ("ok", T.isoformat(), "cached")
    assert ok["ai_universe_as_of"] == (T - timedelta(days=2)).isoformat() and ok["ai_core_as_of"] == T.isoformat()
    assert ok["ai_core_status"] == "ok"
    monkeypatch.setattr(sp, "_ai_state", _state([_facts()], fail_count=3))
    failed = sp.status_summary()
    assert failed["ai_status"] == "failed" and failed["ai_fail_count"] == 3
    bad_core = _state([_facts()])
    bad_core.update(core_version="invalid", core_as_of=None)
    monkeypatch.setattr(sp, "_ai_state", bad_core)
    assert sp.status_summary()["ai_core_status"] == "invalid"
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    assert sp.status_summary()["ai_status"] == "disabled"


def test_status_summary_from_an_unloaded_process_reads_the_file(monkeypatch):
    assert sp._commit_ai(_state([_facts()], source="core_only", ai_source="AI 伺服器"))
    monkeypatch.setattr(sp, "_loaded", False)
    monkeypatch.setattr(sp, "_ai_state", None)
    status = sp.status_summary()
    assert status["ai_status"] == "ok" and status["ai_universe_source"] == "core_only"
    assert sp._ai_state is None and not sp._loaded  # 唯讀：不載入、不改執行期狀態


# ── 開關 LINE_BOT_STOCK_PICKS_AI：照 card_pages._OFF 解析，模組載入時讀 ──────────
def test_flag_parsing_matches_card_pages():
    import card_pages
    assert sp._AI_OFF == card_pages._OFF
    for value in (None, "", "1", "true", "yes", "on", " 1 ", "2", "enabled"):
        assert sp._ai_flag(value) is True
    for value in ("0", "false", "no", "off", "OFF", " False ", "No"):
        assert sp._ai_flag(value) is False
    for value in ("0", "OFF", "1", "x", " no "):  # 和 card_pages.enabled() 的寫法逐字一致
        assert sp._ai_flag(value) == (value.strip().lower() not in card_pages._OFF)


@pytest.mark.parametrize("env,expected", [({}, "True"), ({"LINE_BOT_STOCK_PICKS_AI": "0"}, "False"),
                                          ({"LINE_BOT_STOCK_PICKS_AI": "off"}, "False"),
                                          ({"LINE_BOT_STOCK_PICKS_AI": "1"}, "True")])
def test_ai_enabled_is_read_from_the_environment_at_import(env, expected):
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); import stock_picks; print(stock_picks.AI_ENABLED)"
    import os
    environment = {k: v for k, v in os.environ.items() if k != "LINE_BOT_STOCK_PICKS_AI"}
    environment.update(env)
    out = subprocess.run([sys.executable, "-I", "-c", code], env=environment, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected


# ── 計畫 C 事後審查修正（2026-10-28） ────────────────────────────────────────
def test_skipped_candidates_are_validated_and_optional():
    payload = _state([_facts()])
    payload["universe"]["codes"] = ["2330", "1101"]
    payload["funnel"]["universe"] = 2
    sp.ai_state_from_json(payload)  # 舊檔沒有 skipped 這個鍵：照常載入
    payload["skipped"] = {"1101": "chart_unavailable"}
    assert sp.ai_state_from_json(payload)["skipped"] == {"1101": "chart_unavailable"}
    payload["skipped"] = {"1101": "previous_day_missing", "2330": "unknown"}
    assert len(sp.ai_state_from_json(payload)["skipped"]) == 2
    too_many = {str(1000 + n): "unknown" for n in range(61)}
    for bad in ({"1101": "買進"}, {"1101": "bars_short"}, {"9999": "chart_unavailable"}, {"１１０１": "unknown"},
                ["1101"], {"1101": None}, too_many):
        payload["skipped"] = bad
        with pytest.raises((ValueError, TypeError)):
            sp.ai_state_from_json(payload)


def test_a_rule_change_keeps_the_old_file_readable_without_rechecking_old_picks():
    """GP1 N3：門檻調整（規則換版）後，舊檔裡當時合格的入選者不用新門檻重驗；同一規則的檔仍整份驗證。"""
    payload = _state([_facts(revenue=sp.Revenue(2026, 8, 10.0, 12.0))])  # 累計 12%：現行的 15% 門檻不合格
    with pytest.raises(ValueError):
        sp.ai_state_from_json(payload)
    payload["rule"] = "ai_v0"
    assert sp.ai_state_from_json(payload)["picks"][0]["c"] == "2330"
    payload["picks"][0]["c"] = "9103"  # 其他驗證（91 開頭的 TDR）照舊
    with pytest.raises(ValueError):
        sp.ai_state_from_json(payload)


@pytest.mark.parametrize("env,dotenv,expected", [
    (None, None, "pending"),  # 沒設環境變數、沒有 .env：預設開
    ("0", None, "disabled"),
    ("off", "LINE_BOT_STOCK_PICKS_AI=1\n", "disabled"),  # 環境變數優先（main.py 用 load_dotenv(override=False)）
    ("1", "LINE_BOT_STOCK_PICKS_AI=0\n", "pending"),
    (None, "LINE_BOT_STOCK_PICKS_AI=0\n", "disabled"),  # 維護指令另開的程序沒有載入 .env：自己讀
    (None, "# 備註\nOTHER=1\nexport LINE_BOT_STOCK_PICKS_AI='off'\n", "disabled"),
    (None, 'LINE_BOT_STOCK_PICKS_AI="no"  # 先關\n', "disabled"),
    (None, "LINE_BOT_STOCK_PICKS_AI=0 # 先關掉\n", "disabled"),
    (None, "LINE_BOT_STOCK_PICKS_AI=0\nLINE_BOT_STOCK_PICKS_AI=1\n", "pending"),  # 最後一次設定算數
    (None, "LINE_BOT_STOCK_PICKS_AI=0#x\n", "pending"),  # 和 python-dotenv 一樣：# 前面沒有空白不是註解
    (None, "OTHER=0\nNOT_LINE_BOT_STOCK_PICKS_AI=0\n# LINE_BOT_STOCK_PICKS_AI=0\n", "pending"),
    (None, "LINE_BOT_STOCK_PICKS_AI=\n", "pending"),  # 空值＝沒設（照 _ai_flag）
])
def test_status_reads_the_flag_like_the_bot_even_when_dotenv_was_not_loaded(monkeypatch, tmp_path, env, dotenv, expected):
    """GP2 #4：每日維護與部署 smoke 的程序沒有載入 .env；只看環境變數的話永遠讀到「開」，ai_status 不會寫 disabled。"""
    env_file = tmp_path / ".env"
    if dotenv is not None:
        env_file.write_text(dotenv, encoding="utf-8")
    monkeypatch.setattr(sp, "ENV_FILE", env_file)
    if env is not None:
        monkeypatch.setenv("LINE_BOT_STOCK_PICKS_AI", env)
    assert sp.status_summary()["ai_status"] == expected
    assert sp.status_summary()["ai_status"] == sp._ai_status_fields(None)["ai_status"]


def test_unreadable_or_oversized_dotenv_means_the_default(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(sp, "ENV_FILE", env_file)
    env_file.write_bytes(b"\xff\xfeLINE_BOT_STOCK_PICKS_AI=0\n")
    assert sp.status_summary()["ai_status"] == "pending"
    env_file.write_text("X=" + "a" * (300 * 1024) + "\nLINE_BOT_STOCK_PICKS_AI=0\n", encoding="utf-8")
    assert sp.status_summary()["ai_status"] == "pending"
    monkeypatch.setattr(sp, "ENV_FILE", tmp_path)  # 是目錄：讀不了
    assert sp.status_summary()["ai_status"] == "pending"


def test_module_flag_off_is_disabled_whatever_the_environment_says(monkeypatch):
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    monkeypatch.setenv("LINE_BOT_STOCK_PICKS_AI", "1")
    assert sp.status_summary()["ai_status"] == "disabled"


def test_status_says_when_the_leaf_module_is_missing_and_lists_skipped(monkeypatch):
    status = sp.status_summary()
    assert status["ai_import_ok"] is True and status["ai_skipped"] == []
    payload = _state([_facts()])
    payload["universe"]["codes"] = ["2330", "1101"]
    payload["funnel"]["universe"] = 2
    payload["skipped"] = {"1101": "chart_unavailable"}
    monkeypatch.setattr(sp, "_ai_state", payload)
    assert sp.status_summary()["ai_skipped"] == ["1101"]
    monkeypatch.setitem(sys.modules, "stock_picks_ai_source", None)  # 葉節點模組缺檔：狀態寫不進去，卡片只會寫「計算中」
    assert sp.status_summary()["ai_import_ok"] is False
