"""背景規則選單（jobs/stock_picks_review_variants.py）：A 段就寫死，created＝2026-10-29（PLAN v5 §4.5、§8）。

雜湊釘住內容：C3 只用 created 之後的資料比（樣本外），選單之後被改掉會讓比較失真。要改選單就另寫一份、另記日期；
真的要改這份，這個測試會失敗，提醒先回頭看計畫。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from jobs import stock_picks_review_variants as variants

PINNED_SHA256 = "b9129bba90c31f5e519b4756ba8cc8c3a474d531699ccbb6a23d0af789e5e555"


def test_menu_content_is_pinned():
    text = json.dumps(variants.menu(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert text == variants.canonical_json()
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == variants.menu_sha256() == PINNED_SHA256


def test_menu_lists_the_planned_variants():
    assert variants.CREATED == "2026-10-29"
    ids = [v["id"] for v in variants.VARIANTS]
    assert ids == ["ai_g10", "ai_g25", "ai_pe12", "ai_nope", "ai_p60_5", "ai_p60_20", "ai_sortg",
                   "us_band3", "us_band8", "us_nogrowth", "us_noprofit"]
    for variant in variants.VARIANTS:
        base = variants.AI_BASE if variant["family"] == "tw_ai" else variants.US_BASE
        assert len(variant["overrides"]) == 1 and set(variant["overrides"]) <= set(base)  # 只改底規則的一個參數
        assert variant["id"].startswith("ai_" if variant["family"] == "tw_ai" else "us_")


@pytest.mark.parametrize("variant_id, key, value", [
    ("ai_g10", "growth_cum_yoy_tenths_min", 100), ("ai_g25", "growth_cum_yoy_tenths_min", 250),
    ("ai_pe12", "pe_vs_median_pct_max", 120), ("ai_nope", "use_pe", False),
    ("ai_p60_5", "p60_permille_max", 50), ("ai_p60_20", "p60_permille_max", 200), ("ai_sortg", "sort", "cum_yoy"),
    ("us_band3", "band_permille", 30), ("us_band8", "band_permille", 80),
    ("us_nogrowth", "use_growth", False), ("us_noprofit", "use_profit", False),
])
def test_params_are_base_plus_one_override(variant_id, key, value):
    params = variants.params_of(variant_id)
    assert params[key] == value
    base = variants.AI_BASE if variant_id.startswith("ai_") else variants.US_BASE
    assert {k: v for k, v in params.items() if k != key} == {k: v for k, v in base.items() if k != key}


def test_base_rules_match_the_live_thresholds():
    """底規則和 bot 現行的門檻一致（stock_picks.ai_checks_of、stock_picks.NEAR_PERMILLE）。"""
    import stock_picks as sp

    assert variants.AI_BASE["growth_cum_yoy_tenths_min"] == 150 and variants.AI_BASE["p60_permille_max"] == 100
    assert variants.AI_BASE["pe_tenths_min"] == 50 and variants.AI_BASE["max_picks"] == sp.MAX_PICKS
    assert variants.US_BASE["band_permille"] == sp.NEAR_PERMILLE
    facts = sp.Facts(code="2330", name="台積電", kind=sp.STOCK, data_date=__import__("datetime").date(2026, 10, 27),
                     trend=sp.Trend(100.0, 100.0, 90.0, 100), revenue=sp.Revenue(2026, 9, 0.1, 15.0), pe=5.0,
                     pe_median=5.0)
    assert sp.ai_eligible(facts, "00952")  # 剛好在底規則的每個邊界上
    assert not sp.ai_eligible(sp.Facts(**{**facts.__dict__, "trend": sp.Trend(100.0, 100.0, 90.0, 101)}), "00952")
    assert not sp.ai_eligible(sp.Facts(**{**facts.__dict__, "revenue": sp.Revenue(2026, 9, 0.1, 14.9)}), "00952")
