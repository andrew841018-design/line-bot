"""C3 精簡版背景規則評估（jobs/stock_picks_review_rules.py，PLAN_C3 v2 §6）的測試。

- 引擎 vs bot：直接呼叫真的 bot 函式（ai_candidates、ai_eligible、ai_sort_key、choose_us_picks），不 mock；引擎的輸入一律走
  解析（_facts_json → JSON → _facts_from、名稱換成空字串），和真實資料同一條路。
- 黃金邊界（r3 對 r2 I-4 的補法，計畫原本放 bot 的測試檔，這裡改放 job 這邊）：bot 的門檻剛好在選單的數字上。
- 凍結格式：手寫、固定的正式庫格式（不用 _facts_json 現做），bot 收緊驗證、舊資料讀不進來時先紅（實作後審查 r2 I-2）。
- 解析、對帳、選版、走法（對帳不一致與試驗版本判定不了的天、不偷看）、日曆守門、週報這一段的寫法與一層一層的退法。
  走勢 no:／skipped:／unknown 的跨模組測試（真的
  _maybe_fill_ai＋spool）在 test_stock_picks_review_hook.py；週報的段落位置、出錯只換一行、整份 5,000 字在
  test_stock_picks_review_returns.py 與 test_stock_picks_review.py。
合成資料、tmp DB、不連網。
"""

from __future__ import annotations

import json
import types
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

import stock_picks as sp
import stock_picks_us as us
from jobs import stock_picks_review as runner
from jobs import stock_picks_review_db as rdb
from jobs import stock_picks_review_pool as pool
from jobs import stock_picks_review_returns as returns
from jobs import stock_picks_review_rules as rules
from jobs import stock_picks_review_variants as variants

TPE = ZoneInfo("Asia/Taipei")
AI, US = pool.FAMILY_AI, pool.FAMILY_US
DAY = date(2026, 11, 2)
T = DAY.isoformat()
GOOD = sp.Trend(close=100.0, ma60=100.0, ma240=90.0, p60=0)
MENU_MSG = "回測 C3 的選單要跟著改"


def weekdays(first: str, last: str) -> list[str]:
    day, end, out = date.fromisoformat(first), date.fromisoformat(last), []
    while day <= end:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


CAL = weekdays("2026-09-01", "2027-12-31")


# ── 合成的列 ────────────────────────────────────────────────────────────────
def ai_facts(code: str, *, yoy=10.0, cum=40.0, pe=10.0, median=20.0, trend=GOOD, kind=sp.STOCK, day=DAY) -> sp.Facts:
    stock = kind == sp.STOCK
    return sp.Facts(code=code, name=f"股{code}", kind=kind, data_date=day, trend=trend,
                    revenue=sp.Revenue(2026, 9, yoy, cum) if stock else None, pe=pe if stock else None,
                    pe_median=median if stock else None, inst_net=1000 if stock else None, market=sp.TWSE)


def ai_member(facts: sp.Facts, label: Optional[str] = "pool", *, source: Optional[str] = "AI 伺服器",
              status: str = pool.MEMBER, picked: bool = False, rank=None, values: Optional[dict] = None) -> rules.Member:
    """bot 寫 spool 的寫法：values＝_facts_json（走勢不知道的沒有 tr）＋ai_source；trend_json 的 tr 和 values 一樣。"""
    if label not in ("pool", "ok"):
        facts = replace(facts, trend=None)
    vals = dict(sp._facts_json(facts)) if values is None else values
    if source is not None:
        vals["ai_source"] = source
    return rules.Member(facts.code, status, picked, rank, json.dumps({"trend": label, "tr": vals.get("tr")}),
                        json.dumps(vals, ensure_ascii=False))


def us_facts(symbol: str, p60: int = 0, *, close=100.0, ma240=90.0, growth=50, profit=(0, 0), weight=1.0,
             fund=us.FUND_OK, kind=us.STOCK, trend=True) -> us.UsFacts:
    q = date(2026, 9, 30)
    return us.UsFacts(symbol, "", kind, DAY, us.UsTrend(close=close, ma60=close, ma240=ma240, p60=p60) if trend else None,
                      growth=None if growth is None else us.Growth(q, growth),
                      profit=None if profit is None else us.Profit(q, *profit), pe=us.Pe(us.PE_OK, 252), fund=fund,
                      weight=weight, gray="" if trend else "missing")


def us_member(facts: us.UsFacts, *, picked: bool = False, rank=None, status: str = pool.MEMBER) -> rules.Member:
    vals = us._facts_json(facts)
    trend = {"trend": "pool" if facts.trend is not None else "unknown", "tr": vals["tr"]}
    return rules.Member(facts.symbol, status, picked, rank, json.dumps(trend), json.dumps(vals))


def settle(family: str, members: list, t: str = T) -> list:
    """picked、rank 照底規則填（walk、統計的測試用；對帳本身另有測試）。"""
    sel = rules.parse_and_choose(family, rules.BASES[family], members, t)
    assert sel.picks is not None, sel
    rank = {code: i for i, code in enumerate(sel.candidates, start=1)}
    return [m._replace(picked=m.code in sel.picks, rank=rank.get(m.code)) for m in members]


# ── 引擎 vs bot（不 mock） ─────────────────────────────────────────────────────
def _ai_cases() -> list[tuple[str, dict]]:
    """邊界：_tenths 的 .05、累計 15.0／14.9、本益比 5.0／4.9／4.95、本益比＝中位數、p60 剛好 100／101、在年線下、ETF、
    單月年增是 None 的普通股（引擎不能把 None 交給 _tenths：那會在逐列的 try 外面丟 TypeError，r4 N-7）。"""
    cases = [
        ("2301", {}), ("2302", {"yoy": 0.05}), ("2303", {"yoy": 0.04}), ("2304", {"cum": 15.0}), ("2305", {"cum": 14.9}),
        ("2306", {"cum": 14.95}), ("2307", {"cum": 14.94}), ("2308", {"pe": 5.0}), ("2309", {"pe": 4.9}),
        ("2310", {"pe": 4.95}), ("2311", {"pe": 20.0}), ("2312", {"pe": 20.1}), ("2313", {"pe": None}),
        ("2314", {"trend": sp.Trend(100.0, 90.0, 90.0, 100)}), ("2315", {"trend": sp.Trend(100.0, 90.0, 90.0, 101)}),
        ("2316", {"trend": sp.Trend(89.0, 89.0, 90.0, 0)}), ("2317", {"kind": sp.ETF}), ("2318", {"cum": 300.0, "pe": 7.0}),
        ("2319", {"cum": 300.0, "pe": 7.0, "trend": sp.Trend(100.0, 80.0, 90.0, 250)}), ("2320", {"cum": 41.0}),
        ("2321", {"yoy": None}),
    ]
    return cases


def test_ai_engine_matches_bot_candidates_order_and_walk():
    cases = _ai_cases()
    listed = {}
    for code, kw in cases:
        f = ai_facts(code, **{k: v for k, v in kw.items() if k != "trend"})
        listed[code] = sp.ListedRow(f.name, f.kind, "01" if f.kind == sp.STOCK else "", 100.0, 0.0, f.pe, f.revenue, 1000)
    snap = sp.Snapshot(t=DAY, calendar=(), computed_at=0.0, market=None, universe=(), picks=(), pool={},
                       listed=listed, medians={"01": 20.0}, otc=None, missed=None)
    sources = {code: "AI 伺服器" for code, _ in cases}
    sources["2320"] = ""  # 來源是空字串：條件 1 不成立（同 bot 的 bool(ai_source)）
    candidates, _ = sp.ai_candidates(snap, list(listed), sources)
    trends = {code: kw.get("trend", GOOD) for code, kw in cases}
    want = [f.code for f in candidates if sp.ai_eligible(replace(f, trend=trends[f.code]), sources[f.code])]
    members = [ai_member(replace(sp._facts_for(code, listed[code], DAY, None, snap.medians), trend=trends[code]),
                         source=sources[code]) for code, _ in cases]
    got = rules.parse_and_choose(AI, variants.AI_BASE, members, T)
    assert got.reason is None and list(got.candidates) == [f.code for f in candidates]
    assert list(got.picks) == want[:sp.MAX_PICKS]
    # 邊界一個一個看（_tenths 的 .05 進位、等號）
    assert {"2302", "2304", "2306", "2308", "2310", "2311"} <= set(got.candidates)
    assert not {"2303", "2305", "2307", "2309", "2312", "2313", "2317", "2320", "2321"} & set(got.candidates)
    # 走勢：p60 剛好 100 可以、101 不行、在年線下不行（全部條件都過的才進 want）
    everyone = rules.parse_and_choose(AI, {**variants.AI_BASE, "max_picks": 99}, members, T)
    assert list(everyone.picks) == want and "2314" in want and not {"2315", "2316", "2319"} & set(want)
    # 排序：累計年增 ÷ 本益比大的先（2318：300 ÷ 7）
    assert got.candidates[:2] == ("2318", "2319") and got.picks[0] == "2318"  # 同分照代號；2319 的走勢不合格


def test_us_engine_matches_bot_choose_us_picks():
    pool_facts = [
        us_facts("AAA", 0, weight=5.0), us_facts("AAB", 0, weight=None), us_facts("AAC", 50), us_facts("AAD", -50),
        us_facts("AAE", 51), us_facts("AAF", -51), us_facts("AAG", 10, growth=0), us_facts("AAH", 10, growth=None),
        us_facts("AAI", 10, profit=(1, 0)), us_facts("AAJ", 10, profit=(0, 1)), us_facts("AAK", 10, fund=us.FUND_UNAVAILABLE),
        us_facts("AAL", 10, kind=us.ETF, fund=us.FUND_NA, growth=None, profit=None), us_facts("AAM", 0, trend=False),
        us_facts("AAN", 3, close=1000.2, ma240=1000.4),  # 台股精度算在年線上，美股精度不算
        us_facts("AAO", 3, close=1000.4, ma240=1000.4), us_facts("BRK.B", 3, weight=0.5), us_facts("AAP", -3, weight=0.5),
    ]
    bot = [f.symbol for f in us.choose_us_picks(pool_facts, limit=len(pool_facts))]
    got = rules.parse_and_choose(US, variants.US_BASE, [us_member(f) for f in pool_facts], T)
    assert got.reason is None and list(got.candidates) == bot and list(got.picks) == bot[:us.MAX_PICKS]
    assert "AAN" not in bot and "AAO" in bot and {"AAC", "AAD"} <= set(bot) and not {"AAE", "AAF"} & set(bot)
    assert bot[:2] == ["AAA", "AAB"]  # |p60| 同分：權重大的先，權重 None 當 0


def test_trend_precision_is_cents_for_us_and_display_value_for_tw():
    close, ma240 = 1000.2, 1000.4
    assert sp.Trend(close, close, ma240, 0).above_year is True
    assert us.UsTrend(close, close, ma240, 0).above_year is False
    member = us_member(us_facts("AAN", 0, close=close, ma240=ma240))
    assert rules.parse_and_choose(US, variants.US_BASE, [member], T).candidates == ()


# ── 黃金邊界：bot 的門檻剛好在選單的數字上 ─────────────────────────────────────────
def _ai_checks(**kw) -> dict:
    return {c.key: c.state for c in sp.ai_checks_of(ai_facts("2330", **kw), "AI 伺服器")}


def test_golden_boundaries_bot_thresholds_equal_the_menu():
    base, ubase = variants.AI_BASE, variants.US_BASE
    cum = base["growth_cum_yoy_tenths_min"]
    assert _ai_checks(cum=cum / 10)[sp.GROWTH] is True and _ai_checks(cum=(cum - 1) / 10)[sp.GROWTH] is False, MENU_MSG
    gt = base["growth_month_yoy_tenths_gt"]
    assert _ai_checks(yoy=(gt + 1) / 10)[sp.GROWTH] is True and _ai_checks(yoy=gt / 10)[sp.GROWTH] is False, MENU_MSG
    pe = base["pe_tenths_min"]
    assert _ai_checks(pe=pe / 10)[sp.CHEAP] is True and _ai_checks(pe=(pe - 1) / 10)[sp.CHEAP] is False, MENU_MSG
    assert base["pe_vs_median_pct_max"] == 100, MENU_MSG  # bot：本益比 ≤ 中位數（兩邊都取到 0.1）
    assert _ai_checks(pe=20.0, median=20.0)[sp.CHEAP] is True and _ai_checks(pe=20.1, median=20.0)[sp.CHEAP] is False
    p60 = base["p60_permille_max"]
    assert _ai_checks(trend=sp.Trend(100.0, 90.0, 90.0, p60))["trend"] is True, MENU_MSG
    assert _ai_checks(trend=sp.Trend(100.0, 90.0, 90.0, p60 + 1))["trend"] is False, MENU_MSG
    assert base["use_pe"] is True and base["above_year"] is True and base["sort"] == "cum_yoy_over_pe", MENU_MSG
    band = ubase["band_permille"]
    for value, ok in ((band, True), (-band, True), (band + 1, False), (-band - 1, False)):
        assert us.is_us_pick_candidate(us_facts("AAA", value)) is ok, MENU_MSG
    assert sp.MAX_PICKS == base["max_picks"] and us.MAX_PICKS == ubase["max_picks"], MENU_MSG
    assert sp.NEAR_PERMILLE == band and ubase["use_growth"] and ubase["use_profit"], MENU_MSG
    assert sp.AI_RULE == base["rule"] and us.RULE == ubase["rule"], MENU_MSG


# ── 參數 ──────────────────────────────────────────────────────────────────────
def test_every_menu_variant_has_valid_params_and_bad_params_fail_closed():
    for v in variants.VARIANTS:
        assert rules.params_ok(v["family"], variants.params_of(v["id"])), v["id"]
    member = ai_member(ai_facts("2301"))
    for bad in ({**variants.AI_BASE, "extra": 1}, {k: v for k, v in variants.AI_BASE.items() if k != "sort"},
                {**variants.AI_BASE, "above_year": False}, {**variants.AI_BASE, "max_picks": True},
                {**variants.AI_BASE, "sort": "market_cap"}, {**variants.AI_BASE, "use_pe": 1},
                {**variants.AI_BASE, "max_picks": 0}, variants.US_BASE):
        assert rules.parse_and_choose(AI, bad, [member], T) == rules.Selection(None, (), rules.BAD_PARAMS)
    assert rules.parse_and_choose(US, {**variants.US_BASE, "above_year": False}, [], T).reason == rules.BAD_PARAMS


# ── 解析 ──────────────────────────────────────────────────────────────────────
def test_names_are_blanked_before_parsing():
    """名稱和規則無關：bot 收緊名稱規則（或名稱本來就有怪字）時，舊資料不會整批不可判定（r3 I-5）。"""
    facts = ai_facts("2301")
    odd = {**sp._facts_json(facts), "n": "壞名字!!@@<>" * 5}
    assert rules.parse_and_choose(AI, variants.AI_BASE, [ai_member(facts, values=odd)], T).picks == ("2301",)
    blank = {**sp._facts_json(facts), "n": ""}
    assert rules.parse_and_choose(AI, variants.AI_BASE, [ai_member(facts, values=blank)], T).picks == ("2301",)
    uvals = {**us._facts_json(us_facts("AAA")), "n": "Bad‮name"}
    member = rules.Member("AAA", pool.MEMBER, False, None, json.dumps({"trend": "pool", "tr": uvals["tr"]}),
                          json.dumps(uvals))
    assert rules.parse_and_choose(US, variants.US_BASE, [member], T).picks == ("AAA",)


def _bad_ai_members() -> dict[str, rules.Member]:
    good = ai_member(ai_facts("2302"))
    vals = sp._facts_json(ai_facts("2301"))
    tr = vals["tr"]
    return {
        "overflow": ai_member(ai_facts("2301"), values={**vals, "pe": 10 ** 400}),  # float() 丟 OverflowError
        "nan": rules.Member("2301", pool.MEMBER, False, None, json.dumps({"trend": "pool", "tr": tr}),
                            json.dumps({**vals, "pe": float("nan")})),
        "type": ai_member(ai_facts("2301"), values={**vals, "pe": "10"}),
        "not_dict": good._replace(code="2301", values_json="[1, 2]"),
        "trend_not_dict": good._replace(code="2302", trend_json="5"),
        "bad_json": good._replace(code="2302", values_json="{"),
        "deep": good._replace(code="2302", values_json="[" * 100000 + "]" * 100000),  # RecursionError
        "code": ai_member(ai_facts("2301"), values={**vals, "c": "2309"}),  # values 的代號≠列的 code
        "day": ai_member(ai_facts("2301", day=date(2026, 10, 30))),  # values 的 d≠t
        "tr": good._replace(trend_json=json.dumps({"trend": "pool", "tr": [101.0, 100.0, 90.0, 0]})),  # 兩邊的 tr 不同
        "tr_unknown": good._replace(trend_json=json.dumps({"trend": "unknown", "tr": tr})),  # 不知道走勢卻有 tr
        # 看不懂的標記，兩邊都沒有 tr（帶 tr 的話會先被 tr 那條擋下，測不到標記的白名單，r1 N-3）
        "label": ai_member(ai_facts("2302"), "unchecked")._replace(
            trend_json=json.dumps({"trend": "maybe", "tr": None})),
        # 少了白名單時會被當成 no:（確定不選）默默跳過
        "label_no": ai_member(ai_facts("2302"), "unchecked")._replace(
            trend_json=json.dumps({"trend": "no:壞 原因!!", "tr": None}, ensure_ascii=False)),
    }


# bot 的 _facts_from 丟的例外：類別跟著 bot 走，只斷言記成某個例外類別（C3 第 2 輪審查 測試 F7）。舊資料讀不讀得進來由
# 凍結格式那支測試負責。
BOT_ERRORS = {"overflow", "nan", "type"}
BAD_AI_ERRORS = {"not_dict": rules.INCONSISTENT,
                 "trend_not_dict": rules.INCONSISTENT, "bad_json": "JSONDecodeError", "deep": "RecursionError",
                 "code": rules.INCONSISTENT, "day": rules.INCONSISTENT, "tr": rules.INCONSISTENT,
                 "tr_unknown": rules.INCONSISTENT, "label": rules.INCONSISTENT, "label_no": rules.INCONSISTENT}


@pytest.mark.parametrize("case", list(_bad_ai_members()))
def test_any_bad_row_makes_the_whole_version_undecidable(case):
    """解析失敗或沒過一致性檢查的一列不跳過（不然候選會和 bot 不同）：整版 bad_values，不丟例外；細項是例外類別或
    inconsistent（r2 I-2）。"""
    members = [ai_member(ai_facts("2303")), _bad_ai_members()[case], ai_member(ai_facts("2304"))]
    version, error = rules.parse(AI, members, T)
    assert version is None
    if case in BOT_ERRORS:
        assert error not in (None, rules.INCONSISTENT) and error.isidentifier()
    else:
        assert error == BAD_AI_ERRORS[case]
    assert rules.parse_and_choose(AI, variants.AI_BASE, members, T) == rules.Selection(None, (), rules.BAD_VALUES)


def test_us_code_mismatch_and_a_bad_t_only_make_that_version_undecidable():
    good = us_member(us_facts("AAA"))
    vals = us._facts_json(us_facts("AAB"))
    other = rules.Member("AAC", pool.MEMBER, False, None, json.dumps({"trend": "pool", "tr": vals["tr"]}),
                         json.dumps(vals))  # values 的代號≠列的 code
    assert rules.parse(US, [good, other], T) == (None, rules.INCONSISTENT)
    assert rules.parse(US, [good], "2026-11-2") == (None, "ValueError")  # t 不是日期：逐列的 try 接住（r3 n-4）
    bad_t = rules.parse_and_choose(US, variants.US_BASE, [good], "2026-11-2")
    assert bad_t == rules.Selection(None, (), rules.BAD_VALUES)


# 正式庫 11/20 的格式（鍵、型別、null 的寫法），手寫、固定、值是合成的：不用 _facts_json 現做，bot 之後收緊驗證、讓
# 舊資料讀不進來時這裡先紅（C3 每週從 d0 重算，舊樣本會一次全部變成判定不了；實作後審查 r2 I-2）。
FROZEN_AI_ROWS = (  # (代號, 走勢標記, values_json, trend_json)
    ("2301", "pool", '{"ai_source":"AI 伺服器","c":"2301","cap":null,"d":"2026-11-02","in":-12345,"k":"stock","m":"twse",'
     '"n":"測試一","pe":12.5,"pm":20.25,"rv":[2026,10,35.123456789,40.987654321],"tr":[105.5,100.25,90.125,52]}',
     '{"tr":[105.5,100.25,90.125,52],"trend":"pool"}'),
    ("2302", "ok", '{"ai_source":"散熱","c":"2302","cap":null,"d":"2026-11-02","in":678,"k":"stock","m":"twse",'
     '"n":"測試二","pe":30.5,"pm":19.75,"rv":[2026,10,-3.5,21.0],"tr":[88.0,90.5,95.25,-28]}',
     '{"tr":[88.0,90.5,95.25,-28],"trend":"ok"}'),
    ("2303", "unchecked", '{"ai_source":"core","c":"2303","cap":null,"d":"2026-11-02","in":0,"k":"stock","m":"twse",'
     '"n":"測試三","pe":8.25,"pm":14.0,"rv":[2026,10,12.0,18.5],"tr":null}', '{"tr":null,"trend":"unchecked"}'),
    ("2304", None, '{"ai_source":"IC 設計","c":"2304","cap":null,"d":"2026-11-02","in":-1,"k":"stock","m":"twse",'
     '"n":"測試四","pe":15.0,"pm":15.0,"rv":[2026,10,1.5,2.5],"tr":null}', '{"tr":null,"trend":null}'),
)
FROZEN_US_ROWS = (  # (代號, values_json, trend_json)：成長／獲利／本益比有值或是 null 的寫法
    ("AAA", '{"f":"ok","g":["2026-09-30",117],"k":"stock","n":"TEST CORP","p":["2026-09-30",0,0],"pe":["ok",400],'
     '"s":"AAA","tr":[349.21,338.1265833333334,296.73439583333334,33],"w":8.134997,"x":""}',
     '{"tr":[349.21,338.1265833333334,296.73439583333334,33],"trend":"pool"}'),
    ("AAB", '{"f":"ok","g":null,"k":"stock","n":"TEST CORP B","p":null,"pe":["none",null],"s":"AAB",'
     '"tr":[50.5,49.75,45.0,-12],"w":0.5,"x":""}', '{"tr":[50.5,49.75,45.0,-12],"trend":"pool"}'),
    ("AAC", '{"f":"ok","g":["2026-06-30",-40],"k":"stock","n":"TEST CORP C","p":["2026-09-30",2,1],"pe":["loss",null],'
     '"s":"AAC","tr":[10.0,10.5,11.0,-48],"w":0.02,"x":""}', '{"tr":[10.0,10.5,11.0,-48],"trend":"pool"}'),
)


def test_frozen_database_formats_still_parse():
    ai = [rules.Member(code, pool.MEMBER, False, None, trend, values) for code, _, values, trend in FROZEN_AI_ROWS]
    parsed, error = rules.parse(AI, ai, T)
    assert error is None and [p.label for p in parsed] == [label for _, label, _, _ in FROZEN_AI_ROWS]
    first = parsed[0].facts
    assert (first.pe, first.pe_median, first.revenue.cum_yoy, first.trend.p60) == (12.5, 20.25, 40.987654321, 52)
    assert parsed[0].ai_source == "AI 伺服器" and parsed[2].facts.trend is None and parsed[3].facts.trend is None
    usa = [rules.Member(code, pool.MEMBER, False, None, trend, values) for code, values, trend in FROZEN_US_ROWS]
    parsed, error = rules.parse(US, usa, T)
    assert error is None and [p.facts.symbol for p in parsed] == ["AAA", "AAB", "AAC"]
    assert parsed[1].facts.growth is None and parsed[1].facts.profit is None and parsed[0].facts.trend.p60 == 33
    assert rules.parse_and_choose(US, variants.US_BASE, usa, T).reason is None


def test_frozen_and_excluded_rows_are_not_parsed():
    junk = rules.Member("2399", pool.FROZEN, False, None, None, "not json")
    excluded = rules.Member("2398", "excluded:not_listed", False, None, json.dumps({"trend": None, "tr": None}),
                            json.dumps({"ai_source": "x"}))
    sel = rules.parse_and_choose(AI, variants.AI_BASE, [ai_member(ai_facts("2301")), junk, excluded], T)
    assert sel.picks == ("2301",) and sel.candidates == ("2301",)


def test_frozen_us_rows_are_not_candidates():
    """冷凍的美股列（status frozen，C2b §4.5 之後才會出現）不解析、不進候選，就算它本來排第一（r2 N-9）。"""
    best = us_member(us_facts("AAA", 0), status=pool.FROZEN)
    junk = rules.Member("AAZ", pool.FROZEN, False, None, None, "not json")
    sel = rules.parse_and_choose(US, variants.US_BASE, [best, junk, us_member(us_facts("AAB", 5))], T)
    assert sel.picks == ("AAB",) and sel.candidates == ("AAB",)


# ── 台股 AI 的走法（_maybe_fill_ai） ───────────────────────────────────────────
def _walk(labels: list, *, max_picks: int = 3, trend_none: tuple = ()) -> rules.Selection:
    members = []
    for i, label in enumerate(labels):
        code = str(2301 + i)
        facts = ai_facts(code, cum=90.0 - i, trend=None if code in trend_none else GOOD)
        members.append(ai_member(facts, label))
    return rules.parse_and_choose(AI, {**variants.AI_BASE, "max_picks": max_picks}, members, T)


def test_walk_skips_no_and_skipped_and_stops_at_unknown_before_filling():
    sel = _walk(["pool", "no:bars_short", "skipped:chart_unavailable", "ok", "unchecked", "pool", None])
    assert sel.picks is None and sel.reason == rules.UNKNOWN_TREND  # 2305 unchecked：第 3 檔之前遇到未知
    assert sel.candidates == tuple(str(2301 + i) for i in range(7))
    sel = _walk(["pool", "no:bars_short", "ok", "skipped:previous_day_missing", "pool", "unknown", "unchecked", None])
    assert sel.picks == ("2301", "2303", "2305") and sel.reason is None  # 湊滿以後的未知不影響
    assert _walk(["pool", "unknown"]).reason == rules.UNKNOWN_TREND
    assert _walk(["pool", None]).reason == rules.UNKNOWN_TREND  # null（bot 沒看它的走勢）也是未知
    assert _walk(["pool", "no:other", "skipped:unknown"]).picks == ("2301",)  # 候選都走完：不到 3 檔也可判定


def test_known_label_without_trend_is_not_eligible():
    """pool／ok 但走勢是 None（兩邊都沒有 tr）：同 bot，算不合格，不是不可判定（r1 nit 10）。"""
    sel = _walk(["ok", "pool", "pool"], trend_none=("2301",))
    assert sel.picks == ("2302", "2303")


def test_cum_yoy_sort_and_empty_source():
    a = ai_member(ai_facts("2301", cum=30.0, pe=5.0))  # 30 ÷ 5 ＝ 6
    b = ai_member(ai_facts("2302", cum=40.0, pe=10.0))  # 40 ÷ 10 ＝ 4
    c = ai_member(ai_facts("2303", cum=40.0, pe=10.0), source="")  # 空字串：條件 1 不成立
    d = ai_member(ai_facts("2304", cum=40.0, pe=10.0), source=None)  # 沒有 ai_source
    assert rules.parse_and_choose(AI, variants.AI_BASE, [a, b, c, d], T).candidates == ("2301", "2302")
    assert rules.parse_and_choose(AI, variants.params_of("ai_sortg"), [a, b, c, d], T).candidates == ("2302", "2301")


def test_pe_variants_use_integer_ratio():
    """ai_pe12：_tenths(pe)×100 ≤ _tenths(中位數)×120（整數）；ai_nope 不看本益比（沒有本益比的也進來）。"""
    at = ai_member(ai_facts("2301", pe=24.0, median=20.0))
    over = ai_member(ai_facts("2302", pe=24.1, median=20.0))
    none = ai_member(ai_facts("2303", pe=None))
    assert rules.parse_and_choose(AI, variants.params_of("ai_pe12"), [at, over, none], T).candidates == ("2301",)
    nope = rules.parse_and_choose(AI, variants.params_of("ai_nope"), [at, over, none], T)
    assert set(nope.candidates) == {"2301", "2302", "2303"}
    assert rules.parse_and_choose(AI, variants.AI_BASE, [at, over, none], T).candidates == ()


def test_threshold_and_switch_variants_only_change_their_own_condition():
    """目前真的在產生樣本的試驗版本（ai_g10、ai_g25、us_nogrowth、us_noprofit）各自只改它那一個條件：讀錯參數或開關失效時，
    它們會悄悄和現行一樣（實作後審查 r1 I-2）。"""
    facts = [us_facts("AAA", 0), us_facts("AAB", 1, growth=-5), us_facts("AAC", 2, profit=(1, 0)), us_facts("AAD", 3)]
    members = [us_member(f) for f in facts]
    assert rules.parse_and_choose(US, variants.US_BASE, members, T).picks == ("AAA", "AAD")
    assert rules.parse_and_choose(US, variants.params_of("us_nogrowth"), members, T).picks == ("AAA", "AAB", "AAD")
    assert rules.parse_and_choose(US, variants.params_of("us_noprofit"), members, T).picks == ("AAA", "AAC", "AAD")
    ai = [ai_member(ai_facts("2301", cum=30.0)), ai_member(ai_facts("2302", cum=20.0)),
          ai_member(ai_facts("2303", cum=12.0))]
    assert rules.parse_and_choose(AI, variants.AI_BASE, ai, T).picks == ("2301", "2302")
    assert rules.parse_and_choose(AI, variants.params_of("ai_g25"), ai, T).picks == ("2301",)
    assert rules.parse_and_choose(AI, variants.params_of("ai_g10"), ai, T).picks == ("2301", "2302", "2303")


# ── 對帳 ─────────────────────────────────────────────────────────────────────
def test_parity_compares_set_full_candidate_list_ranks_and_picks_match():
    members = settle(AI, [ai_member(ai_facts(str(2301 + i), cum=90.0 - i)) for i in range(5)])
    base = rules.parse_and_choose(AI, variants.AI_BASE, members, T)
    assert rules.parity_ok(AI, base, members, None)
    swapped = [m._replace(rank={1: 2, 2: 1}.get(m.rank, m.rank)) for m in members]  # 集合一樣、名次不同
    assert not rules.parity_ok(AI, base, swapped, None)
    other = [m._replace(picked=m.code in ("2301", "2302", "2305")) for m in members]  # 集合不同
    assert not rules.parity_ok(AI, base, other, None)
    tail = [m._replace(rank=None) if m.code == "2305" else m for m in members]  # 整串候選少一檔
    assert not rules.parity_ok(AI, base, tail, None)
    frozen = members + [rules.Member("2399", pool.FROZEN, False, 6, None, None)]  # 非成員帶名次也算不一致
    assert not rules.parity_ok(AI, base, frozen, None)
    odd = [m._replace(rank=str(m.rank)) if m.code == "2301" else m for m in members]
    assert not rules.parity_ok(AI, base, odd, None)
    byte_code = members + [rules.Member(b"2399", pool.FROZEN, False, 1, None, None)]  # 代號不是 str、名次重複（r2 N-1）
    assert not rules.parity_ok(AI, base, byte_code, None)  # 不讓 sorted 丟 TypeError（那會讓整段算不出來）
    umembers = settle(US, [us_member(us_facts(s, i)) for i, s in enumerate(("AAA", "AAB", "AAC", "AAD"))])
    ubase = rules.parse_and_choose(US, variants.US_BASE, umembers, T)
    assert rules.parity_ok(US, ubase, umembers, {"picks_match": True})
    for meta in ({"picks_match": False}, {}, None, {"picks_match": 1}):
        assert not rules.parity_ok(US, ubase, umembers, meta)


@pytest.mark.parametrize("module, name, value, changed", [
    pytest.param(sp, "MAX_PICKS", 4, AI, id="tw_max_picks"),
    pytest.param(sp, "NEAR_PERMILLE", 40, US, id="near_permille"),
    pytest.param(us, "MAX_PICKS", 2, US, id="us_max_picks"),
])
def test_bot_constant_drift_skips_the_whole_family(monkeypatch, tmp_path, module, name, value, changed):
    world = World(tmp_path)
    assert not rules.bot_rule_changed(AI) and not rules.bot_rule_changed(US)
    monkeypatch.setattr(module, name, value)
    assert [rules.bot_rule_changed(family) for family in (AI, US)] == [changed == AI, changed == US]
    results = [rules.evaluate_family(world.conn, family, CAL, with_returns=True) for family in (AI, US)]
    assert [r.state == rules.BOT_RULE_CHANGED for r in results] == [changed == AI, changed == US]
    lines = rules.format_lines(results, DAY)
    assert f"・{pool.FAMILY_BY_KEY[changed].name}：bot 的選股常數（最多幾檔、季線範圍）和選單不一樣，這週不算" in lines
    assert "ready" not in rules.numbers_of(results)  # 檢查點寫「這週算不出來」，不只拿另一族的數字（r3 n-6）


# ── DB 世界 ───────────────────────────────────────────────────────────────────
def at(t: str, hour: int = 18) -> float:
    d = date.fromisoformat(t)
    return datetime(d.year, d.month, d.day, hour, tzinfo=TPE).timestamp()


class World:
    """tmp 的 review.sqlite（真的 migration）：日曆、池子版本、成員報酬都直接寫。"""

    def __init__(self, tmp_path: Path, days=CAL):
        self.conn = rdb.open_db(tmp_path / "review.sqlite")
        for market in ("tw", "us"):
            symbol = rdb.CALENDAR_SYMBOLS[market][0]
            self.conn.executemany("INSERT INTO prices (m, code, d, close, src, seen_at) VALUES (?, ?, ?, 100.0, ?, 0)",
                                  [(market, symbol, d, market) for d in days])

    def version(self, family: str, t: str, members: list, *, rule: Optional[str] = None, computed_at=None, dv: int = 1,
                meta: Optional[dict] = None) -> tuple:
        m = "tw" if family == AI else "us"
        rule = rule or rules.BASES[family]["rule"]
        computed_at = at(t) if computed_at is None else computed_at
        if meta is None:
            meta = {"picks_match": True} if family == US else {}
        key = (m, rule, t, round(computed_at), dv)
        self.conn.execute(
            "INSERT INTO pool_days (m, rule, t, ver_s, derive_version, computed_at, origin, n_members, n_picks, meta_json,"
            " logged_at) VALUES (?, ?, ?, ?, ?, ?, 'simple', ?, ?, ?, 0)",
            (*key, computed_at, len(members), sum(x.picked for x in members), json.dumps(meta)))
        self.conn.executemany(
            "INSERT INTO pool_rows (m, rule, t, ver_s, derive_version, code, name, c0, picked, rank, status, trend_json,"
            " flags_json, values_json) VALUES (?, ?, ?, ?, ?, ?, '毒字串', 100.0, ?, ?, ?, ?, '{}', ?)",
            [(*key, x.code, 1 if x.picked else 0, x.rank, x.status, x.trend_json, x.values_json) for x in members])
        return key

    def returns(self, family: str, key: tuple, results: dict, h: int = rules.HOLD_H) -> None:
        """results：{代號: r 或跳過原因（字串）}。"""
        rows = []
        for code, value in results.items():
            r, skip = (None, value) if isinstance(value, str) else (value, None)
            rows.append((*key[:3], family, key[3], key[4], code, h, returns.SUPPORTED_LEDGER_VERSION,
                         returns.CALC_VERSION, r, None if skip else 0.0, skip, None, 0.0))
        self.conn.executemany(
            "INSERT INTO member_returns (m, rule, t, family, ver_s, derive_version, code, h, ledger_version, calc_version,"
            " r, b, skip, inputs_json, frozen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def evaluate(self, family: str = US, calendar=CAL) -> rules.FamilyResult:
        return rules.evaluate_family(self.conn, family, calendar, with_returns=True)


# 美股的兩種天（只有 us_band3 會和現行不同：底規則照 |p60| 排，前 3 名有一檔 |p60| > 3%）
FILLER = [f"AB{c}" for c in "CDEFGH"]  # 不是任何規則的候選（|p60| 3%＞8%）：給股票池湊成員數


def us_day(kind: str) -> list:
    if kind == "differ":  # 現行 {AAA, AAB, AAX}；us_band3 {AAA, AAB}
        facts = [us_facts("AAA", 0), us_facts("AAB", 5), us_facts("AAX", 40), us_facts("AAD", 45)]
    elif kind == "tie":  # 兩邊都是 {AAA, AAB, AAC}
        facts = [us_facts("AAA", 0), us_facts("AAB", 5), us_facts("AAC", 10), us_facts("AAX", 40)]
    elif kind == "zero":  # 現行 {AAA, AAB}；us_band3 0 檔
        facts = [us_facts("AAA", 35), us_facts("AAB", 40)]
    else:
        raise AssertionError(kind)
    return settle(US, [us_member(f) for f in facts + [us_facts(s, 300) for s in FILLER]])


def band3(result: rules.FamilyResult) -> rules.VariantStats:
    return next(s for s in result.variants if s.id == "us_band3")


def test_ties_are_not_sampled_and_the_next_sample_waits_for_th(tmp_path):
    days = weekdays("2026-10-30", "2026-12-31")
    th = rules.exit_day(CAL, "2026-11-02")
    assert th == CAL[CAL.index("2026-11-03") + rules.HOLD_H]  # E＝11/3，再 20 個交易日（同 perf.evaluate 的 T_h）
    (tmp_path / "a").mkdir()
    world = World(tmp_path / "a")
    for t in days:
        world.version(US, t, us_day("tie" if t == "2026-10-30" or t >= th else "differ"))
    stats = band3(world.evaluate())
    assert stats.samples == ["2026-11-02"]  # 10/30 平局不取樣；th 之前的分歧不取樣；th 起都平局
    (tmp_path / "b").mkdir()
    world2 = World(tmp_path / "b")
    for t in days:
        world2.version(US, t, us_day("differ" if t >= "2026-11-02" else "tie"))
    stats = band3(world2.evaluate())
    assert stats.samples[:2] == ["2026-11-02", th]  # t 剛好等於 th 算新的一期
    assert stats.samples[2] == rules.exit_day(CAL, th)
    result = world2.evaluate()
    assert result.days[rules.USABLE] == len(days) and result.differ_days == len(days) - 1
    assert (stats.k, stats.n_days) == (len(days) - 1, len(days)) and stats.pending == len(stats.samples)


def test_last_sample_stops_the_walk_when_th_is_beyond_the_calendar(tmp_path):
    short = weekdays("2026-09-01", "2026-11-20")
    world = World(tmp_path, short)
    for t in weekdays("2026-11-02", "2026-11-20"):
        world.version(US, t, us_day("differ"))
    result = world.evaluate(calendar=short)
    stats = band3(result)
    assert rules.exit_day(short, "2026-11-02") is None and stats.samples == ["2026-11-02"]
    assert stats.k == stats.n_days == 14 and result.days[rules.WAIT_E] == 1  # 11/20：日曆上還沒有 E，先等


def test_parity_mismatch_day_is_counted_and_never_sampled(tmp_path):
    """對帳不一致的那天（主要驗收；實作後審查 r1 I-1、r3 I-2）：記 parity，整族不取樣，也不算進 k/N 與每族的
    「選得不一樣」；數字與資料行照實寫。"""
    world = World(tmp_path)
    good = us_day("differ")
    bad = [m._replace(picked=not m.picked) if m.code in ("AAX", "AAD") else m for m in good]  # 記下的 picked 集合不同
    world.version(US, T, bad)
    world.version(US, "2026-11-03", good)
    result = world.evaluate()
    assert result.days == Counter({rules.USABLE: 1, rules.PARITY: 1}) and result.differ_days == 1
    stats = band3(result)
    assert (stats.samples, stats.k, stats.n_days) == (["2026-11-03"], 1, 1)
    lines, numbers = rules.report_lines(types.SimpleNamespace(today=DAY), world.conn, {"tw": CAL, "us": CAL})
    assert (numbers["checked_days"], numbers["parity_ok"], numbers["parity_mismatch"]) == (2, 1, 1)
    assert lines[-1] == "・資料：現行規則重算和 bot 記下的，比了 2 天、一致 1 天；作廢 0 期"


def _ai_day(t: str, fourth: str, picked: Optional[set] = None) -> list:
    """台股 AI 的一天：底規則選 2301、2302、2303（2302 比季線高 8%：ai_p60_5 不要它，得走到第 4 檔）。第 4 檔的走勢標記是
    fourth：unchecked（未知）→ ai_p60_5 判定不了；pool → ai_p60_5 選 2301、2303、2304。給了 picked 就照它填（對帳不一致）。"""
    d = date.fromisoformat(t)
    members = settle(AI, [ai_member(ai_facts("2301", cum=90.0, day=d)),
                          ai_member(ai_facts("2302", cum=89.0, day=d, trend=sp.Trend(100.0, 90.0, 90.0, 80))),
                          ai_member(ai_facts("2303", cum=88.0, day=d)),
                          ai_member(ai_facts("2304", cum=87.0, day=d), fourth)], t)
    return members if picked is None else [m._replace(picked=m.code in picked) for m in members]


def test_undecidable_variant_days_count_in_neither_its_k_n_nor_the_family_k(tmp_path):
    """試驗版本判定不了的天（真實的台股 AI 幾乎每週都有）：不取樣、不算進它的 k/N，也不算進每族的「選得不一樣」；
    對帳不一致的天整族都不取樣（實作後審查 r1 I-1／N-1、r3 I-2、r4 I-1）。"""
    world = World(tmp_path)
    d1, d2, d3 = "2026-11-02", "2026-11-03", "2026-11-04"
    world.version(AI, d1, _ai_day(d1, "unchecked"))  # 只有 ai_p60_5 判定不了（判定得了的話會選得不一樣）
    world.version(AI, d2, _ai_day(d2, "pool", picked={"2301", "2302", "2304"}))  # 記下的和底規則不一樣：對帳不一致
    world.version(AI, d3, _ai_day(d3, "pool"))  # ai_p60_5 判定得了、選得不一樣
    result = world.evaluate(AI)
    assert result.days == Counter({rules.USABLE: 2, rules.PARITY: 1}) and result.differ_days == 1
    stats = {s.id: s for s in result.variants}
    p5 = stats.pop("ai_p60_5")
    assert (p5.samples, p5.k, p5.n_days) == ([d3], 1, 1)
    assert all((s.samples, s.k, s.n_days) == ([], 0, 2) for s in stats.values())  # 其他的兩天都和現行一樣
    lines, numbers = rules.report_lines(types.SimpleNamespace(today=DAY), world.conn, {"tw": CAL, "us": CAL})
    assert (numbers["parity_mismatch"], numbers["undecided"], numbers["pending"]) == (1, 0, 1)
    assert lines[1].endswith("，合計另有 1 期等 20 日成績；2 天裡有 1 天和現行選得不一樣")
    assert lines[-1] == "・資料：現行規則重算和 bot 記下的，比了 3 天、一致 2 天；作廢 0 期；判定不了的試驗版本不算分歧"


def test_same_set_in_another_order_is_not_a_difference(tmp_path):
    """ai_sortg 只換排序：選到同一組、只是順序不同，不算分歧、不取樣（比的是集合；實作後審查 r1 I-2）。"""
    members = [ai_member(ai_facts("2301", cum=30.0, pe=5.0)), ai_member(ai_facts("2302", cum=40.0, pe=10.0)),
               ai_member(ai_facts("2303", cum=35.0, pe=10.0))]
    base = rules.parse_and_choose(AI, variants.AI_BASE, members, T)
    sortg = rules.parse_and_choose(AI, variants.params_of("ai_sortg"), members, T)
    assert set(base.picks) == set(sortg.picks) and base.picks != sortg.picks
    world = World(tmp_path)
    world.version(AI, T, settle(AI, members))
    result = world.evaluate(AI)
    stats = next(s for s in result.variants if s.id == "ai_sortg")
    assert (stats.samples, stats.k, stats.n_days) == ([], 0, 1) and result.differ_days == 0


def _world_with_returns(tmp_path: Path, mode: str) -> tuple:
    """同一組池子紀錄，成員報酬三種狀態：none（還沒算）、random（亂填）、old_dv（只有舊的 derive_version 齊全）。"""
    world = World(tmp_path)
    keys = []
    for t in weekdays("2026-11-02", "2027-01-29"):
        kind = "differ" if date.fromisoformat(t).day % 3 else "tie"
        members = us_day(kind)
        if mode == "old_dv":
            world.version(US, t, members, dv=1)
            keys.append((world.version(US, t, members, dv=2), members))
        else:
            keys.append((world.version(US, t, members), members))
    for (key, members), seed in zip(keys, range(10 ** 6)):
        if mode == "random":
            world.returns(US, key, {m.code: (seed % 7 - 3) / 100 if (seed + i) % 5 else "missing"
                                    for i, m in enumerate(members)})
        elif mode == "old_dv":
            world.returns(US, key[:4] + (1,), {m.code: 0.01 * i for i, m in enumerate(members)})
    return world


def test_sample_days_do_not_depend_on_member_returns(tmp_path):
    """不偷看（r4 I-B）：把成員報酬（r、skip，所以 p 也跟著變）全部換掉、拿掉、或只在舊的 derive_version 齊全，樣本日都一樣；
    舊的 dv 齊全時，那一期先等（引擎讀的 dv 和 r 的不同，r4 對 r3 n-4 的回應）。"""
    out = {}
    for mode in ("none", "random", "old_dv"):
        (tmp_path / mode).mkdir()
        result = _world_with_returns(tmp_path / mode, mode).evaluate()
        out[mode] = {s.id: s.samples for s in result.variants}
        if mode != "random":
            assert all(s.pending == len(s.samples) and not s.diffs and not s.void for s in result.variants), mode
    assert out["none"] == out["random"] == out["old_dv"] and len(out["none"]["us_band3"]) >= 3


@pytest.mark.parametrize("case, want", [
    ("comparable", rules.COMPARABLE), ("pending", rules.PENDING), ("zero", rules.ZERO_PICKS),
    ("zero_pending", rules.ZERO_PICKS),  # 0 檔＋還沒有報酬：0 檔在 t 當下就知道，排在等成績前面（r1 N-2、r4 I-1）
    ("pool_missing", rules.POOL_MISSING), ("no_returns", rules.NO_RETURNS), ("same_valued", rules.SAME_VALUED),
])
def test_each_outcome_of_a_sample(tmp_path, case, want):
    world = World(tmp_path)
    members = us_day("zero" if case.startswith("zero") else "differ")
    key = world.version(US, T, members)
    valued = {m.code: 0.01 for m in members}
    valued.update(AAA=0.01, AAB=0.02, AAX=0.10)
    if case == "pool_missing":  # 有值的不到 8 成
        valued.update({code: "missing" for code in FILLER[:3]})
    elif case == "no_returns":  # us_band3 的兩檔都沒有報酬（股票池還有 8 成）
        valued.update(AAA="range", AAB="jump")
    elif case == "same_valued":  # 只差的那一檔（AAX）沒有報酬：兩邊有報酬的都是 {AAA, AAB}
        valued.update(AAX="quarantine")
    if case not in ("pending", "zero_pending"):
        world.returns(US, key, valued)
    stats = band3(world.evaluate())
    assert stats.samples == [T]
    if want == rules.COMPARABLE:
        assert stats.diffs == [rules._mean([0.01, 0.02]) - rules._mean([0.01, 0.02, 0.10])] and stats.better == 0
    elif want == rules.PENDING:
        assert stats.pending == 1 and not stats.void and not stats.diffs
    else:
        assert stats.void == Counter({want: 1}) and not stats.diffs and not stats.pending


def test_fsum_mean_of_the_same_set_in_another_order_is_exactly_zero():
    values = [0.1, 0.2, 0.3]
    assert rules._mean(values) - rules._mean(list(reversed(values))) == 0.0
    assert (0.1 + 0.2 + 0.3) / 3 - (0.3 + 0.2 + 0.1) / 3 != 0.0  # 照順序加會有誤差（r3 對 nit 1 的實測）


def test_undecided_days_are_counted_by_reason_and_the_section_still_renders(tmp_path):
    """底規則判定不了的天（r2 I-2、r3 n-1、n-4）：資料讀不懂（例外類別或 inconsistent）和走勢未知分開數，資料行補一句
    「其中資料讀不懂 M 天」；美股的 t 壞了也只讓那一版判定不了。其他天與整段照常（不丟例外）。"""
    world = World(tmp_path)
    world.version(US, T, us_day("differ"))
    bad = us_day("tie")
    bad[0] = bad[0]._replace(values_json=json.dumps({**json.loads(bad[0].values_json), "w": 10 ** 400}))
    world.version(US, "2026-11-03", bad)  # 超大整數：OverflowError
    world.version(US, "2026-11-2", us_day("tie"), computed_at=at(T))  # t 不是日期：ValueError
    world.version(AI, T, [ai_member(ai_facts("2301", cum=90.0)), ai_member(ai_facts("2302", cum=89.0), "unchecked"),
                          ai_member(ai_facts("2303", cum=88.0))])  # 第 2 檔就遇到未知：走勢未知
    result = world.evaluate()
    assert result.days == Counter({rules.USABLE: 1, rules.UNDECIDED: 2})
    assert result.bad_values_types == Counter({"OverflowError": 1, "ValueError": 1})
    lines, numbers = rules.report_lines(types.SimpleNamespace(today=DAY), world.conn, {"tw": CAL, "us": CAL})
    assert lines[-1] == ("・資料：現行規則重算和 bot 記下的，比了 1 天、一致 1 天；現行規則判定不了 3 天，"
                         "其中資料讀不懂 2 天；作廢 0 期")
    assert (numbers["undecided"], numbers["undecided_bad_values"], numbers["undecided_unknown_trend"]) == (3, 2, 1)
    assert numbers["bad_values_types"] == {"OverflowError": 1, "ValueError": 1}
    assert runner.sanitize(numbers) == numbers  # 例外類別的鍵過得了 last_run 的 sanitize


# ── 選版 ─────────────────────────────────────────────────────────────────────
def test_c3_uses_the_last_base_rule_version_before_e_close_and_counts_rule_mismatch(tmp_path):
    world = World(tmp_path)
    good = settle(AI, [ai_member(ai_facts(str(2301 + i), cum=90.0 - i)) for i in range(5)])
    wrong = [m._replace(picked=m.code in ("2304", "2305")) for m in good]  # 對帳一定不一致的列
    world.version(AI, T, wrong, computed_at=at(T, 10))  # 較早的版本（不用）
    world.version(AI, T, good, computed_at=at(T, 12))  # E 收盤前最後一版
    world.version(AI, T, wrong, computed_at=at("2026-11-04", 9))  # E 收盤以後才算出來：不算
    world.version(AI, T, wrong, rule="ai_v2", computed_at=at(T, 13))  # 別的規則（最後一版）：過濾掉
    world.version(AI, "2026-11-03", good, rule="ai_v2")  # 那天只有別的規則
    world.version(AI, "2026-11-04", good, computed_at=at("2026-11-06", 9))  # 只有 E 收盤以後的版本
    result = world.evaluate(AI)
    assert result.days == Counter({rules.USABLE: 1, rules.RULE_MISMATCH: 1, rules.NO_VERSION: 1})
    assert result.switched is False  # 最新的一版（11/4）是 ai_v1
    world.version(AI, "2026-11-05", good, rule="ai_zz")
    result = world.evaluate(AI)
    assert result.switched and result.days[rules.RULE_MISMATCH] == 2
    lines = rules.format_lines([result, world.evaluate(US)], DAY)
    assert lines[1].startswith("・台股 AI 相關股：現行規則已換成別的版本，背景比較停在 ai_v1（要新選單）；7 個試驗版本")
    assert "；只有別的規則的紀錄 2 天" in lines[-1]


def test_section_only_has_menu_constants_numbers_and_dates(tmp_path):
    """週報這一段不放股票代號、名稱或任何 DB 的字串（r2 n-1）：名稱、怪規則名、代號都不會出現。"""
    world = World(tmp_path)
    days = weekdays("2026-11-02", "2027-06-30")
    for i, t in enumerate(days):
        members = us_day("differ" if i % 2 else "tie")
        key = world.version(US, t, members)
        world.returns(US, key, {m.code: 0.01 * (j + i % 4) for j, m in enumerate(members)})
        world.version(AI, t, settle(AI, [ai_member(ai_facts(str(8761 + j), cum=90.0 - j, day=date.fromisoformat(t)))
                                         for j in range(4)], t))
    for table in ("pool_days", "pool_rows"):  # 最後一天的 AI 換成怪規則名（DB 的字串）
        world.conn.execute(f"UPDATE {table} SET rule = 'ai_zz9' WHERE m = 'tw' AND t = ?", (days[-1],))
    ctx = types.SimpleNamespace(today=date(2027, 7, 4))
    lines, numbers = rules.report_lines(ctx, world.conn, {"tw": CAL, "us": CAL})
    text = "\n".join(lines)
    for word in ("毒", "ai_zz9", "AAA", "AAX", "ABC", "876"):
        assert word not in text, word
    assert "us_band3（離季線 ±3%）：" in text and numbers["ready"] == 1 and numbers["parity_ok"] == numbers["checked_days"]
    assert "現行規則已換成別的版本，背景比較停在 ai_v1（要新選單）" in text and "；只有別的規則的紀錄 1 天" in text
    assert len(text) <= rules.MAX_CHARS
    assert numbers.pop("bad_values_types") == {}  # {例外類別: 天數}；其他都是數字
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in numbers.values())


# ── 日曆守門 ───────────────────────────────────────────────────────────────────
def test_start_day_out_of_the_price_calendar_writes_only_one_sentence(tmp_path):
    world = World(tmp_path)
    world.version(US, T, us_day("differ"))
    late = weekdays("2026-10-30", "2027-03-31")  # 日曆裡沒有 10/29 或更早的日子：d0 可能已經掉出去
    result = world.evaluate(US, late)
    assert result.state == rules.OUT_OF_RANGE
    lines = rules.format_lines([world.evaluate(AI), result], DAY)
    assert lines == [rules.HEAD, "・起算日已超出價格庫範圍，背景規則要改成存樣本日"]
    ctx = types.SimpleNamespace(today=DAY)
    got, numbers = rules.report_lines(ctx, world.conn, {"tw": CAL, "us": late})
    assert got == lines and numbers["out_of_range"] == 1
    assert "ready" not in numbers  # 檢查點寫「這週算不出來」，不只拿台股 AI 的數字（r3 n-6、r1 N-9）


def test_calendar_whose_first_day_is_created_still_has_the_start_day(tmp_path):
    """日曆第一天剛好是 10/29（CREATED）：d0 確定是 10/30，不是掉出日曆（守門只擋日曆上沒有 ≤ 10/29 的日子；r3 I-2）。"""
    cal = weekdays("2026-10-29", "2027-03-31")
    world = World(tmp_path, cal)
    world.version(US, "2026-10-29", us_day("differ"))  # CREATED 當天：不是樣本外，不算
    world.version(US, "2026-10-30", us_day("differ"))
    result = world.evaluate(US, cal)
    assert result.state == rules.OK and result.drop_day == date(2028, 9, 29)
    assert result.days == Counter({rules.USABLE: 1}) and band3(result).samples == ["2026-10-30"]


def test_sixty_days_before_the_start_day_drops_out_the_data_line_warns(tmp_path):
    world = World(tmp_path)
    results = [world.evaluate(AI), world.evaluate(US)]
    drop = date(2026, 10, 29) + timedelta(days=rdb.PRICE_KEEP_DAYS + 1)  # 10/29 是日曆上最後一個不晚於 CREATED 的日子
    assert drop == date(2028, 9, 29) and {r.drop_day for r in results} == {drop}
    warn = rules.format_lines(results, drop - timedelta(days=rules.WARN_DAYS))[-1]
    assert warn.endswith("；約 9/29 起要改成存樣本日")
    assert "存樣本日" not in rules.format_lines(results, drop - timedelta(days=rules.WARN_DAYS + 1))[-1]
    # 價格庫照 PRICE_KEEP_DAYS 刪：drop 那天 10/29 剛好被刪掉（prices_stage 的 keep_from）
    assert (drop - timedelta(days=rdb.PRICE_KEEP_DAYS)).isoformat() > "2026-10-29"
    assert (drop - timedelta(days=rdb.PRICE_KEEP_DAYS + 1)).isoformat() == "2026-10-29"


def test_no_calendar_and_no_start_day_yet(tmp_path):
    world = World(tmp_path)
    world.version(US, T, us_day("differ"))
    empty = world.evaluate(US, ())
    assert empty.state == rules.NO_CALENDAR and rules._family_line(empty) == "・美股：價格庫還沒有交易日，這週不算"
    early = world.evaluate(US, weekdays("2026-09-01", "2026-10-29"))  # 日曆上還沒有起算日（10/30）
    assert early.state == rules.OK and not early.days and early.drop_day == date(2028, 9, 29)
    assert rules._family_line(early) == "・美股：4 個試驗版本，每個可比 0 期（要 6 期）；還沒有可以比的天"
    assert "ready" not in rules.numbers_of([world.evaluate(AI), empty])  # 沒有日曆的那族沒有正常算
    assert rules.numbers_of([world.evaluate(AI), early])["ready"] == 0
    # ready 不見時數字要說得出原因：沒有日曆也有自己的鍵（C3 第 2 輪審查 正確性 F-4）
    assert rules.numbers_of([world.evaluate(AI), empty])["no_calendar"] == 1
    assert rules.numbers_of([world.evaluate(AI), early])["no_calendar"] == 0


# ── 時間保險 ───────────────────────────────────────────────────────────────────
def test_out_of_time_writes_one_line(tmp_path):
    world = World(tmp_path)
    world.version(US, T, us_day("differ"))
    ticks = iter([0.0] + [31.0] * 1000)
    ctx = types.SimpleNamespace(today=DAY)
    lines, numbers = rules.report_lines(ctx, world.conn, {"tw": CAL, "us": CAL}, clock=lambda: next(ticks))
    assert lines == ["【背景規則】這週算不完（超過 30 秒），其他內容照常"] and numbers == {"computed": 0, "out_of_time": 1}


def test_member_returns_with_old_columns_raises_schema_error(tmp_path):
    world = World(tmp_path)
    world.conn.execute("DROP TABLE member_returns")
    world.conn.execute("CREATE TABLE member_returns (m TEXT, code TEXT, PRIMARY KEY (m, code))")
    with pytest.raises(rdb.SchemaError):
        rules.report_lines(types.SimpleNamespace(today=DAY), world.conn, {"tw": CAL, "us": CAL})


# ── 週報這一段的寫法 ─────────────────────────────────────────────────────────────
def _stats(vid: str, n: int, mean: float, k: int, n_days: int, **kw) -> rules.VariantStats:
    desc = next(v["desc"] for v in variants.VARIANTS if v["id"] == vid)
    return rules.VariantStats(vid, desc, samples=["2027-01-04"] * n, diffs=[mean] * n, k=k, n_days=n_days, **kw)


def test_ready_variants_get_at_most_two_lines_sorted_by_share_times_mean():
    ai = rules.FamilyResult(AI, variants=[
        _stats("ai_g10", 8, 0.024, 9, 40), _stats("ai_g25", 6, -0.05, 2, 40), _stats("ai_pe12", 7, 0.01, 30, 40),
        _stats("ai_nope", 3, 0.2, 40, 40), _stats("ai_p60_5", 0, 0.0, 0, 40),
        _stats("ai_p60_20", 9, 0.0, 1, 40), _stats("ai_sortg", 0, 0.0, 0, 0)], days=Counter({rules.USABLE: 40}),
        differ_days=12)
    us_result = rules.FamilyResult(US, variants=[_stats(v["id"], 0, 0.0, 0, 40) for v in variants.VARIANTS
                                                 if v["family"] == US], days=Counter({rules.USABLE: 40}))
    lines = rules.format_lines([ai, us_result], DAY)
    # 寫法不像日期（r4 I-2）：「40 天裡有 12 天」「其中 7 期較好」，不寫 12/40、7/7
    assert lines[1] == "・台股 AI 相關股：7 個試驗版本，每個可比 0–9 期（要 6 期）；40 天裡有 12 天和現行選得不一樣"
    assert lines[2] == ("　ai_pe12（本益比 ≤ 1.2 × 同產業中位數）：7 期平均比現行多 1.0%，其中 7 期較好；"
                        "能判定的 40 天裡 30 天選得不一樣")  # 0.75×1%
    assert lines[3] == ("　ai_g10（累計營收年增 ≥ 10%）：8 期平均比現行多 2.4%，其中 8 期較好；"
                        "能判定的 40 天裡 9 天選得不一樣")  # 0.225×2.4%
    assert lines[4] == "　另外 2 個試驗版本也有 6 期以上可比樣本"  # ai_g25（0.05×5%＝0.25%）、ai_p60_20（0）
    assert lines[5] == "・美股：4 個試驗版本，每個可比 0 期（要 6 期）；40 天裡有 0 天和現行選得不一樣"
    # ai_sortg 能判定的天（0）比可以比的天（40）少：資料行寫明判定不了的不算分歧（r1 N-6）
    assert lines[6] == "・資料：現行規則重算和 bot 記下的，比了 80 天、一致 80 天；作廢 0 期；判定不了的試驗版本不算分歧"
    assert rules.numbers_of([ai, us_result])["ready"] == 4
    assert "比現行少 5.0%" in rules._detail_lines(rules.FamilyResult(AI, variants=[_stats("ai_g25", 6, -0.05, 2, 40)]))[0]
    assert "和現行差不多（0.0%）" in rules._detail_lines(rules.FamilyResult(AI, variants=[_stats("ai_g25", 6, 0.0, 2, 40)]))[0]


def worst_results() -> list:
    """每一種可選的寫法同時出現、數字都取最長的：換了規則、上萬期等成績、每種作廢、判定不了（含資料讀不懂）、規則不同、
    預告、「判定不了的試驗版本不算分歧」、每族兩行試驗版本＋「另外」。"""
    def family(key: str) -> rules.FamilyResult:
        menu = [v for v in variants.VARIANTS if v["family"] == key]
        big = 9_999_999
        stats = [rules.VariantStats(v["id"], v["desc"], diffs=[-9.87654] * 9999, pending=big, k=big, n_days=big,
                                    void=Counter({k: big for k in rules.VOID_ORDER})) for v in menu]
        stats[0].diffs = [-0.001] * 6
        stats[-1].k = stats[-1].n_days = big - 1  # 有一天判定不了：資料行可以多寫一句
        return rules.FamilyResult(key, variants=stats, switched=True, drop_day=date(2030, 12, 31), differ_days=big,
                                  days=Counter({rules.USABLE: big, rules.PARITY: big, rules.UNDECIDED: big,
                                                rules.RULE_MISMATCH: big}), undecided=Counter({rules.BAD_VALUES: big}))
    return [family(AI), family(US)]


def _layers(results: list, today: date) -> list[list[str]]:
    """format_lines 依序試的寫法：細節全部／每族第 1 行（其餘收成「另外 N 個」）／不列（各自先試資料行加那一句、再試不加）
    → 只留資料行 → 一句。"""
    data = rules._data_line(results, today)
    out = []
    for limit in (rules.MAX_DETAIL, 1, 0):
        body = [rules.HEAD] + [x for r in results
                               for x in (rules._family_line(r), *(rules._detail_lines(r, limit) if limit else ()))]
        out += [body + [data + rules.UNDECIDABLE_NOTE], body + [data]]
    return out + [[rules.HEAD, data], [rules.HEAD, rules.TOO_LONG_TEXT]]


def test_section_never_exceeds_550_chars_and_falls_back_to_one_line_per_family():
    results = worst_results()
    full = [rules.HEAD] + [x for r in results for x in (rules._family_line(r), *rules._detail_lines(r))]
    assert len("\n".join(full)) > rules.MAX_CHARS  # 這種寫法放不下
    lines = rules.format_lines(results, date(2030, 12, 1))
    assert len("\n".join(lines)) <= rules.MAX_CHARS
    assert lines[0] == rules.HEAD and len(lines) == 4 and not any(x.startswith(rules.INDENT) for x in lines)
    assert lines[1].startswith("・台股 AI 相關股：現行規則已換成別的版本") and lines[2].startswith("・美股：")
    assert lines[3].startswith("・資料：") and "；約 12/31 起要改成存樣本日" in lines[3]
    assert all(word in lines[3] for word in [*rules._VOID_WORDS.values(), "其中資料讀不懂 19999998 天"])


def _realistic_results() -> tuple[list, date]:
    """約 2028 的量級：兩族各 3 個樣本夠的試驗版本、三位數的天數、換了規則、預告。"""
    def family(key: str, switched: bool) -> rules.FamilyResult:
        stats = [rules.VariantStats(v["id"], v["desc"], diffs=[0.0123 * (i + 1)] * (14 - i if i < 3 else 3), pending=1,
                                    k=100 + 10 * i, n_days=450 - i)
                 for i, v in enumerate(v for v in variants.VARIANTS if v["family"] == key)]
        return rules.FamilyResult(key, variants=stats, switched=switched, drop_day=date(2028, 9, 29), differ_days=230,
                                  days=Counter({rules.USABLE: 470, rules.UNDECIDED: 12}))
    return [family(AI, True), family(US, False)], date(2028, 8, 6)


def test_a_long_realistic_section_keeps_the_first_detail_line_of_each_family():
    """約 2028 的量級：全部細節放不下時，每族還留第 1 行，不是一次退到每族一行（r4 I-3）。"""
    results, today = _realistic_results()
    lines = rules.format_lines(results, today)
    assert len("\n".join(_layers(results, today)[1])) > rules.MAX_CHARS >= len("\n".join(lines))  # 全部細節放不下
    assert [x.startswith(rules.INDENT) for x in lines] == [False, False, True, True, False, True, True, False]
    assert lines[2].startswith("　ai_pe12（") and lines[5].startswith("　us_nogrowth（")  # 各族 |k/N × 平均差| 最大的
    assert lines[-1].endswith("；約 9/29 起要改成存樣本日；判定不了的試驗版本不算分歧")


def test_middle_layer_still_counts_the_other_ready_versions():
    """每族只留第 1 行細節時，其餘樣本夠的試驗版本收成「另外 N 個」：不然段落看起來只有 1 個樣本夠，和同一份週報檢查點的
    「樣本夠 6 期的試驗版本 n 個」對不起來（C3 第 2 輪審查 正確性 F-3）。"""
    results, today = _realistic_results()
    others = [x for x in rules.format_lines(results, today) if "另外" in x]
    assert others == [f"{rules.INDENT}另外 2 個試驗版本也有 6 期以上可比樣本"] * 2


def test_each_fallback_layer_is_used_only_when_the_longer_ones_do_not_fit(monkeypatch):
    """放不下就一層一層退，不一次退到底（r4 I-3、r3 n-3）：上限剛好是某一層的長度時就用那一層；最後一層是一句話，不是只剩
    標題（看起來像沒東西要報）。"""
    results, today = worst_results(), date(2030, 12, 1)
    layers = _layers(results, today)
    sizes = [len("\n".join(lines)) for lines in layers]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes)  # 由長到短
    middle = layers[2]  # 每族只留第 1 行細節，其餘收成「另外 N 個」（C3 第 2 輪審查 正確性 F-3）
    n_ai, n_us = (sum(v["family"] == key for v in variants.VARIANTS) for key in (AI, US))
    assert [x.startswith(rules.INDENT) for x in middle] == [False, False, True, True, False, True, True, False]
    assert middle[3] == f"{rules.INDENT}另外 {n_ai - 1} 個試驗版本也有 6 期以上可比樣本"
    assert middle[6] == f"{rules.INDENT}另外 {n_us - 1} 個試驗版本也有 6 期以上可比樣本"
    assert middle[-1].endswith(rules.UNDECIDABLE_NOTE)
    for lines, size in zip(layers, sizes):
        monkeypatch.setattr(rules, "MAX_CHARS", size)
        assert rules.format_lines(results, today) == lines
    monkeypatch.setattr(rules, "MAX_CHARS", 10)
    assert rules.format_lines(results, today) == [rules.HEAD, "・這週的數字太長放不下"]
