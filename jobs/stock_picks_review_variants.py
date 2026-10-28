"""股票推薦回測的背景規則選單（PLAN v5 §4.5、§8；2026-10-29 在 A 段就寫死）。

created＝2026-10-29：C3 只拿這天之後的資料比（樣本外），所以選單現在就釘住，C3 晚上線也不影響。
內容的 sha256 由 test_stock_picks_review_variants.py 釘住；要改選單就另寫一份、另記日期，不改這份。
這裡只有資料，評估在 C3。

底規則照 bot 現行的條件（數字都用卡片顯示的精度比，和 stock_picks.ai_checks_of、stock_picks_us 一樣）：
- AI 相關股（ai_v1）：單月營收年增（一位小數）> 0、1 月到這個月的累計年增 ≥ 15.0%；
  5.0 ≤ 本益比 ≤ 同產業中位數；在年線之上、比季線高 ≤ 10.0%；
  依「累計營收年增 ÷ 本益比」大到小排、同分照代號，最多 3 檔。
- 美股（us_v1）：在年線之上、離季線 ±5.0% 以內；最近一季營收比去年同季高、近四季每季都賺錢（ETF 只看走勢）；
  依（|季線乖離|、SPY 權重大到小、代號）排，最多 3 檔。
變體＝在底規則上只改一個參數（overrides）。
"""

from __future__ import annotations

import hashlib
import json

CREATED = "2026-10-29"
MENU_VERSION = 1

AI_BASE = {
    "rule": "ai_v1",
    "growth_month_yoy_tenths_gt": 0,  # 單月年增 ×10 要 > 0
    "growth_cum_yoy_tenths_min": 150,  # 累計年增 ×10 要 ≥ 150（15.0%）
    "use_pe": True,
    "pe_tenths_min": 50,  # 本益比 ×10 要 ≥ 50（5.0 倍）
    "pe_vs_median_pct_max": 100,  # 本益比 ≤ 同產業中位數 ×100%
    "above_year": True,
    "p60_permille_max": 100,  # 比季線高 ≤ 10.0%
    "sort": "cum_yoy_over_pe",
    "max_picks": 3,
}

US_BASE = {
    "rule": "us_v1",
    "above_year": True,
    "band_permille": 50,  # |季線乖離| ≤ 5.0%
    "use_growth": True,
    "use_profit": True,
    "sort": "abs_p60_weight_symbol",
    "max_picks": 3,
}

VARIANTS = (
    {"id": "ai_g10", "family": "tw_ai", "desc": "累計營收年增 ≥ 10%", "overrides": {"growth_cum_yoy_tenths_min": 100}},
    {"id": "ai_g25", "family": "tw_ai", "desc": "累計營收年增 ≥ 25%", "overrides": {"growth_cum_yoy_tenths_min": 250}},
    {"id": "ai_pe12", "family": "tw_ai", "desc": "本益比 ≤ 1.2 × 同產業中位數", "overrides": {"pe_vs_median_pct_max": 120}},
    {"id": "ai_nope", "family": "tw_ai", "desc": "拿掉本益比條件", "overrides": {"use_pe": False}},
    {"id": "ai_p60_5", "family": "tw_ai", "desc": "比季線高 ≤ 5%", "overrides": {"p60_permille_max": 50}},
    {"id": "ai_p60_20", "family": "tw_ai", "desc": "比季線高 ≤ 20%", "overrides": {"p60_permille_max": 200}},
    {"id": "ai_sortg", "family": "tw_ai", "desc": "只用累計營收年增排序", "overrides": {"sort": "cum_yoy"}},
    {"id": "us_band3", "family": "us", "desc": "離季線 ±3%", "overrides": {"band_permille": 30}},
    {"id": "us_band8", "family": "us", "desc": "離季線 ±8%", "overrides": {"band_permille": 80}},
    {"id": "us_nogrowth", "family": "us", "desc": "拿掉成長條件", "overrides": {"use_growth": False}},
    {"id": "us_noprofit", "family": "us", "desc": "拿掉獲利條件", "overrides": {"use_profit": False}},
)


def menu() -> dict:
    return {"created": CREATED, "version": MENU_VERSION, "bases": {"tw_ai": AI_BASE, "us": US_BASE},
            "variants": list(VARIANTS)}


def canonical_json() -> str:
    return json.dumps(menu(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def menu_sha256() -> str:
    return hashlib.sha256(canonical_json().encode("utf-8")).hexdigest()


def params_of(variant_id: str) -> dict:
    """某個變體的完整參數（底規則＋overrides）；C3 用。"""
    for variant in VARIANTS:
        if variant["id"] == variant_id:
            base = AI_BASE if variant["family"] == "tw_ai" else US_BASE
            return {**base, **variant["overrides"]}
    raise KeyError(variant_id)
