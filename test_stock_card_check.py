"""stock_card_check（美股與成績卡的自我檢查，2026-10-28）的測試。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import stock_card_check as cc  # noqa: E402
import stock_picks as sp  # noqa: E402


def _bubble(*texts, footer=None):
    return sp._bubble("股票推薦：測試", None, [sp._text(t) for t in texts], footer or [sp._text("只供參考")])


def test_good_card_passes():
    card = sp.carousel([_bubble("✅ NVDA（NVIDIA CORP）", "收盤 186.23 美元（10/27 美國時間）")])
    assert cc.card_ok(card, "股票推薦：只供參考｜美股：NVDA")


@pytest.mark.parametrize(
    "mutate, kind",
    [
        (lambda c: c["contents"][0]["body"]["contents"].append(sp._text("")), "text"),
        (lambda c: c["contents"][0]["body"]["contents"].append(sp._text("a​b")), "text"),
        (lambda c: c["contents"][0]["body"]["contents"].append(sp._text("@all")), "text"),
        (lambda c: c["contents"][0]["body"]["contents"].append({"type": "span", "text": "x"}), "node_type"),
        (lambda c: c["contents"][0]["body"]["contents"].append({"type": "box", "layout": "vertical", "contents": []}), "empty_box"),
        (lambda c: c["contents"][0]["footer"]["contents"].append(
            {"type": "button", "action": {"type": "uri", "label": "x", "uri": "https://x"}}), "action"),
        (lambda c: c["contents"][0]["footer"]["contents"].append(
            {"type": "button", "action": {"type": "postback", "label": "x", "data": "x"}}), "action"),
        (lambda c: c["contents"][0]["body"].__setitem__("url", "https://x"), "forbidden_key"),
        (lambda c: c["contents"][0]["footer"]["contents"].append(sp._button("看" + "很" * 20 + "細節", "/股票 NVDA")), "label"),
    ],
)
def test_bad_cards_are_rejected(mutate, kind):
    card = sp.carousel([_bubble("✅ NVDA")])
    mutate(card)
    with pytest.raises(cc.CardRejected) as info:
        cc.check(card, "股票推薦：只供參考")
    assert str(info.value) == kind
    assert not cc.card_ok(card)


def test_alt_text_rules():
    card = _bubble("✅ NVDA")
    assert not cc.card_ok(card, "")
    assert not cc.card_ok(card, "x" * 401)
    assert cc.card_ok(card, "股票評估：只供參考｜NVDA")


def test_lists_match_main():
    """漂移測試：節點型別與禁用鍵要和 main 送卡前的白名單一樣（main 改了這裡要跟著改）。"""
    import main

    assert cc.NODE_TYPES == main._FLEX_CARD_NODE_TYPES
    assert cc.FORBIDDEN_KEYS == main._FLEX_CARD_FORBIDDEN_KEYS
