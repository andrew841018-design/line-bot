"""美股推薦與推薦成績的卡片自我檢查（2026-10-28）：送出前自己先驗，沒過就不接到台股卡組後面。

main 送卡時是 altText＋整組卡片一起驗（main._validated_flex_card_message）：美股一個字沒過，台股卡會一起
被換成固定句；空字串 validator 會放行，但 LINE 回 400、`_reply` 失敗時不補固定句，整組都送不出去。
所以美股的每張卡在接上去之前，先用和 main 一樣嚴、再多幾項的規則驗一次：
- 節點型別只收 carousel／bubble／box／text／button／separator／filler，沒有 uri／url／data／altUri 鍵；
- action 只收 message，label 1–20 字、text 1–300 字；
- 每個字都不是空的、沒有控制字元（Cc、Cf）、沒有「@」「＠」，而且原樣通過 output_validator；
- box 的 contents 是非空的 list；
- linebot SDK 的 FlexContainer.from_dict 讀得進去（只做 from_dict，和 main 一樣；SDK 載不到就略過這項）。

不 import main（main 會 import stock_picks）。
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any, Optional

import output_validator

logger = logging.getLogger("stock_card_check")

NODE_TYPES = frozenset({"carousel", "bubble", "box", "text", "button", "separator", "filler"})
FORBIDDEN_KEYS = frozenset({"uri", "url", "data", "altUri"})
LABEL_MAX = 20
ACTION_TEXT_MAX = 300
ALT_TEXT_MAX = 400  # LINE 上限 1500；這個 repo 的卡片 altText 都壓在 400 以內


class CardRejected(ValueError):
    """卡片沒過自我檢查；只帶原因種類（不帶卡片上的字）。"""


def _text_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        return False
    if "@" in value or "＠" in value:
        return False
    result = output_validator.validate_outbound_text(value)
    return bool(result.ok) and result.text == value


def _walk(node: Any, texts: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, texts)
        return
    if not isinstance(node, dict):
        return
    if FORBIDDEN_KEYS & set(node):
        raise CardRejected("forbidden_key")
    node_type = node.get("type")
    if node_type is not None and node_type not in NODE_TYPES:
        raise CardRejected("node_type")
    if node_type == "box":
        contents = node.get("contents")
        if not isinstance(contents, list) or not contents:
            raise CardRejected("empty_box")
    if node_type == "text":
        texts.append(node.get("text"))
    action = node.get("action")
    if action is not None:
        if not isinstance(action, dict) or action.get("type") != "message":
            raise CardRejected("action")
        label, text = action.get("label"), action.get("text")
        if not isinstance(label, str) or not 0 < len(label) <= LABEL_MAX:
            raise CardRejected("label")
        if not isinstance(text, str) or not 0 < len(text) <= ACTION_TEXT_MAX:
            raise CardRejected("action_text")
        texts += [label, text]
    for key, value in node.items():
        if key != "action":
            _walk(value, texts)


def check(card: Any, alt_text: Optional[str] = None) -> None:
    """沒過就丟 CardRejected（只帶種類）。"""
    if not isinstance(card, dict) or card.get("type") not in ("bubble", "carousel"):
        raise CardRejected("root")
    texts: list[str] = []
    _walk(card, texts)
    if alt_text is not None:
        if len(alt_text) > ALT_TEXT_MAX:
            raise CardRejected("alt_too_long")
        texts.append(alt_text)
    if not texts or not all(_text_ok(t) for t in texts):
        raise CardRejected("text")
    try:
        from linebot.v3.messaging import FlexContainer  # type: ignore[import-untyped]
    except Exception:  # SDK 載不到（只有某些測試環境）：其他檢查照做
        return
    try:
        FlexContainer.from_dict(card)
    except Exception:
        raise CardRejected("sdk") from None


def card_ok(card: Any, alt_text: Optional[str] = None) -> bool:
    """check() 的布林版；沒過記一行種類。"""
    try:
        check(card, alt_text)
    except CardRejected as exc:
        logger.warning("stock card rejected kind=%s", exc)
        return False
    return True
