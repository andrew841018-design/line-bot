"""speaker_turns — 對話紀錄裡「誰說的」怎麼寫（2026-10-10）。

只由程式寫：每則訊息第一行是「稱呼：」，同一則的其他行前面加全形空白，所以訊息內容裡
行首的「某某：」（轉貼的對話）不會被當成某某在說話；認不出是誰就寫 UNKNOWN_SPEAKER，
家人打的「咪寶：…」也不會像 bot 自己說的。

\\r、\\u2028 這類換行字元都用 str.splitlines() 切開、再用 \\n 接回：讀取端
（main._speakers_in_context）用同一種切法，換行字元不能拿來跳過縮排。

沒有其他 import：gemini_client、claude_client 也用得到，不必為此載入 memory（會開 DB）。
"""
from __future__ import annotations

UNKNOWN_SPEAKER = "（不確定是誰）"
CONTINUATION_INDENT = "\u3000"


def speaker_turn(label: str, text: str) -> str:
    """「稱呼：第一行\\n\\u3000第二行…」；沒有稱呼就用 UNKNOWN_SPEAKER。"""
    first, *rest = str(text or "").splitlines() or [""]
    lines = [f"{label or UNKNOWN_SPEAKER}：{first}", *(CONTINUATION_INDENT + line for line in rest)]
    return "\n".join(lines)


def spoken_text(turn: str) -> str:
    """speaker_turn 的反向：拿掉開頭的「稱呼：」和續行縮排，只留說的話。"""
    first, *rest = str(turn or "").splitlines() or [""]
    label, sep, body = first.partition("：")
    if sep and 0 < len(label) <= 20 and not any(ch.isspace() for ch in label):
        first = body
    return "\n".join([first, *(line.removeprefix(CONTINUATION_INDENT) for line in rest)])


def one_line(text: str) -> str:
    """各種換行收成一個空白：一條記憶只佔提示裡的一行，/記住 多行不能在事實區塊
    偽造【系統…】、（現在說話的是…）或別人的事實。"""
    return " ".join(part.strip() for part in str(text or "").splitlines() if part.strip())
