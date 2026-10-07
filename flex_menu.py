"""咪寶選單：家族群組叫得出來、用完會自己收起的按鈕列（2026-10-05 加，10-07 改）。

群組聊天室不會顯示圖文選單（Rich menu），所以有人打「選單」或只打「/」時回一則
短訊息，按鈕掛在 LINE Quick Reply 上：浮在輸入框上方，點了任一顆、或群組裡任何
人再傳一則訊息就自動收起（Andrew 2026-10-07：要的時候叫出來，不要的時候縮回去）。
原本的 Flex 卡片送出後會一直佔著聊天畫面，Flex 本身也沒有收合的做法。

點按鈕得到的回覆會再掛一次同樣的按鈕（main._reply 的 menu_buttons），連續查
好幾樣不用一直重打「選單」；有人聊別的就收起。LINE 的 bot 收不到「正在輸入」的
字，所以做不到打「/」當下就跳出候選清單，只能送出「/」再跳按鈕。

每顆按鈕都是 message action：點了等於點的人自己打出那串文字，後面完全走既有
的文字指令路由（靜音成員、結案紀錄、引用都照舊）。只放不用再補字、只讀不改的
指令；要補內容的（/記住 <內容>）、會通知全體或改資料的（/催民調、/關閉民調）
不放。

選單只用 reply token 回覆，不主動推播；內容全是固定字串，不含模型輸出。
"""

from __future__ import annotations

import unicodedata

# 選單訊息本身的文字：留在聊天紀錄裡的只有這一行，按鈕收起後不佔版面。
PROMPT_TEXT = "咪寶選單在下方👇 點按鈕＝幫你打出指令，聊別的就自動收起"

# (按鈕文字, 點了之後送出的文字)。Quick Reply 最多 13 顆、按鈕文字最多 20 字。
# 送出的文字必須永遠接得到既有路由：舊的 Flex 卡片會一直留在聊天紀錄裡可以點，
# 改名或拿掉指令前先看 test_flex_menu.SHIPPED_BUTTON_TEXTS。
BUTTONS: tuple[tuple[str, str], ...] = (
    ("📋 提醒清單", "/提醒清單"),
    ("📅 行事曆", "/行事曆"),
    ("🍽️ 外食推薦", "今晚吃什麼？"),
    ("🍳 今晚煮什麼", "/今晚煮什麼"),
    ("🛒 該買什麼", "/該買什麼"),
    ("🧊 家裡有什麼", "/家裡有什麼"),
    ("📊 民調結果", "/民調"),
    ("💬 財經觀點", "/觀點"),
    ("🧠 看記憶", "/看記憶"),
    ("🚦 過濾規則", "/規則"),
    ("❓ 全部指令", "/help"),
)

_TRIGGERS = frozenset({"選單", "/選單", "/"})


def _normalize(text: str | None) -> str:
    # NFKC 把全形「／」「？」「！」轉成半形；結尾的問號、驚嘆號不影響意思。
    return unicodedata.normalize("NFKC", text or "").strip().rstrip("?!").strip()


_BUTTON_TEXTS = frozenset(_normalize(text) for _label, text in BUTTONS)


def might_be_menu_request(text: str) -> bool:
    """便宜的前置檢查：大部分訊息不用再跑一次稱呼解析。"""
    return len(text) <= 64 and ("選單" in text or text.rstrip().endswith(("/", "／")))


def is_menu_request(text: str, addressed_text: str | None = None) -> bool:
    """整則只是「選單」或「/」（或去掉 @咪寶／咪寶 稱呼後只剩這些）才算。"""
    return _normalize(text) in _TRIGGERS or _normalize(addressed_text) in _TRIGGERS


def is_button_text(text: str | None) -> bool:
    """這則訊息就是某顆按鈕送出的指令（自己打的也算）。"""
    return _normalize(text) in _BUTTON_TEXTS


def quick_reply():
    """選單按鈕本身（LINE QuickReply），可以掛在任何一則訊息上。"""
    from linebot.v3.messaging import (  # type: ignore[import-untyped]
        MessageAction,
        QuickReply,
        QuickReplyItem,
    )

    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label=label, text=text))
            for label, text in BUTTONS
        ]
    )


def menu_message():
    """回傳可以直接放進 ReplyMessageRequest 的文字訊息，按鈕掛在 Quick Reply。

    LINE 只顯示一次回覆裡最後一則訊息的 Quick Reply，所以這則必須單獨送。
    """
    from linebot.v3.messaging import TextMessage  # type: ignore[import-untyped]

    return TextMessage(text=PROMPT_TEXT, quick_reply=quick_reply())
