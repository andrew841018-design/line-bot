"""finance_view_extractor — prefilter + Gemini extract 測試。

mock Gemini，避免實際打 API（quota 緊）。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import finance_view_extractor


def test_prefilter_skips_non_finance():
    assert not finance_view_extractor.is_finance_burst("")
    assert not finance_view_extractor.is_finance_burst("今天午餐吃什麼")
    assert not finance_view_extractor.is_finance_burst("晚上幾點到家")


def test_prefilter_catches_finance_keywords():
    assert finance_view_extractor.is_finance_burst("我覺得 0050 會漲")
    assert finance_view_extractor.is_finance_burst("半導體要修正了")
    assert finance_view_extractor.is_finance_burst("Fed 年底前不會升息")
    assert finance_view_extractor.is_finance_burst("台積電目標價 800")
    assert finance_view_extractor.is_finance_burst("NVDA 看多")


def test_extract_returns_empty_when_no_finance_keyword():
    # prefilter 不通過 → 不打 Gemini（quota 保護）
    assert finance_view_extractor.extract("今天天氣很好") == []


def test_extract_parses_gemini_json(monkeypatch):
    """mock Gemini 回 JSON array，verify _normalize 跟 _calc_expires_at。"""
    fake_payload = json.dumps([
        {
            "symbol_type": "ticker",
            "ticker": "0050.TW",
            "macro_topic": None,
            "direction": "bull",
            "time_frame": "mid",
            "horizon_days": 90,
            "target_price": 180.0,
            "target_pct": None,
            "confidence": "high",
            "condition_text": None,
            "speaker_hint": "媽媽",
            "raw_quote": "0050 會漲到 180",
        }
    ])

    fake_resp = MagicMock()
    fake_resp.text = fake_payload
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_resp
    monkeypatch.setattr(finance_view_extractor.gemini_client, "_client", fake_client)

    result = finance_view_extractor.extract("我覺得 0050 會漲到 180")
    assert len(result) == 1
    v = result[0]
    assert v["ticker"] == "0050.TW"
    assert v["direction"] == "bull"
    assert v["target_price"] == 180.0
    assert v["confidence"] == "high"
    assert v["speaker_hint"] == "媽媽"
    assert v["horizon_days"] == 90
    assert v["expires_at"] is not None  # 90 天後有日期


def test_normalize_drops_invalid_enum_values():
    bad = {
        "symbol_type": "garbage",
        "direction": "rocket",
        "time_frame": "century",
        "confidence": "ultra",
        "horizon_days": "abc",
    }
    out = finance_view_extractor._normalize(bad)
    assert out["symbol_type"] == "ticker"  # 預設
    assert out["direction"] is None
    assert out["time_frame"] is None
    assert out["confidence"] is None
    assert out["horizon_days"] is None


def test_normalize_keeps_valid_values():
    good = {
        "symbol_type": "macro",
        "ticker": None,
        "macro_topic": "Fed 升息",
        "direction": "bear",
        "time_frame": "long",
        "horizon_days": 180,
        "confidence": "mid",
        "raw_quote": "Fed 年底會升息",
    }
    out = finance_view_extractor._normalize(good)
    assert out["symbol_type"] == "macro"
    assert out["ticker"] is None
    assert out["macro_topic"] == "Fed 升息"
    assert out["direction"] == "bear"
    assert out["time_frame"] == "long"
    assert out["horizon_days"] == 180
    assert out["confidence"] == "mid"
    assert out["expires_at"] is not None
