"""咪寶選單：家族群組的 Flex 按鈕卡片（2026-10-05 加）。

群組聊天室不會顯示圖文選單（Rich menu），所以改成有人打「選單」時回一張
Flex 卡片。每顆按鈕都是 message action：點了等於點的人自己打出那串文字，
後面完全走既有的文字指令路由（靜音成員、結案紀錄、引用都照舊）。

卡片只用 reply token 回覆，不主動推播；內容全是固定字串，不含模型輸出。
"""

from __future__ import annotations

import unicodedata

ALT_TEXT = "咪寶選單：點開看按鈕"
TITLE = "咪寶選單"
SUBTITLE = "點按鈕＝幫你打出指令"

# (按鈕文字, 點了之後送出的文字)。送出的文字必須永遠接得到既有路由：
# 舊卡片會一直留在聊天紀錄裡可以點，改名或拿掉指令前先看
# test_flex_menu.SHIPPED_BUTTON_TEXTS。
BUTTONS: tuple[tuple[str, str], ...] = (
    ("📋 提醒清單", "/提醒清單"),
    ("📅 行事曆", "/行事曆"),
    ("🍽️ 外食推薦", "今晚吃什麼？"),
    ("💬 財經觀點", "/觀點"),
    ("❓ 全部指令", "/help"),
)

_TRIGGERS = frozenset({"選單", "/選單"})


def _normalize(text: str | None) -> str:
    # NFKC 把全形「／」「？」「！」轉成半形；結尾的問號、驚嘆號不影響意思。
    return unicodedata.normalize("NFKC", text or "").strip().rstrip("?!").strip()


def is_menu_request(text: str, addressed_text: str | None = None) -> bool:
    """整則只是「選單」（或去掉 @咪寶／咪寶 稱呼後只剩「選單」）才算。"""
    return _normalize(text) in _TRIGGERS or _normalize(addressed_text) in _TRIGGERS


def build_menu() -> dict:
    """卡片的 Flex JSON（bubble）。"""
    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": TITLE, "weight": "bold", "size": "lg"},
                {"type": "text", "text": SUBTITLE, "size": "xs", "color": "#888888"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {"type": "message", "label": label, "text": text},
                }
                for label, text in BUTTONS
            ],
        },
    }


def menu_message():
    """回傳可以直接放進 ReplyMessageRequest 的 FlexMessage。"""
    from linebot.v3.messaging import FlexContainer, FlexMessage  # type: ignore[import-untyped]

    return FlexMessage(alt_text=ALT_TEXT, contents=FlexContainer.from_dict(build_menu()))
