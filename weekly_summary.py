"""週摘要推播 — launchd 每週日 20:00 TW 觸發。

從 raw_messages 取過去 7 天 bot 的回應，請 Gemini 整理成一則摘要推播給群組。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests

import family_interest
import gemini_client
import memory

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _push(text: str) -> None:
    requests.post(
        _PUSH_URL,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": GROUP_ID, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=10,
    )


def main() -> None:
    if not GROUP_ID or not TOKEN:
        print("ERR: LINE_ALLOWED_GROUP_ID or LINE_CHANNEL_ACCESS_TOKEN not set")
        return

    since_ts = int(time.time()) - 7 * 86400
    all_msgs = memory.get_messages_since(GROUP_ID, since_ts, exclude_bot=False)

    # 只取 bot 的回應
    bot_replies = [text for _, uid, text, _ in all_msgs if uid == "__bot__"]

    if not bot_replies:
        print("本週沒有 bot 回應，跳過摘要推播")
        return

    # 每次最多取最近 20 則，避免塞爆 prompt
    sample = bot_replies[-20:]
    joined = "\n---\n".join(sample)

    prompt = (
        "以下是 LINE 群組 bot 咪寶這週所有的回應。"
        "請用繁體中文、溫柔可愛的語氣，幫我整理成一則「本週查核/分析摘要」，"
        "讓群組成員知道這週咪寶查了哪些重要的事、有哪些假訊息被揭穿。"
        "格式：條列重點（3~5 點），不超過 200 字，結尾一句溫馨收尾。\n\n"
        f"{joined}"
    )

    # bot 摘要（Gemini 失敗就跳過，但不影響家族熱話）
    try:
        summary = gemini_client.chat(prompt, [], [], None)
        push_text = f"📋 本週咪寶摘要\n\n{summary}"
        _push(push_text)
        print(f"週摘要已推播 ({len(bot_replies)} 則回應，取最近 {len(sample)} 則)")
    except Exception as e:
        print(f"ERR Gemini bot 摘要 (跳過，繼續家族熱話): {e}")

    # 家族熱話週報（per Q5=B）— 偵測 4 主成員過去 30 天興趣 + 對應新聞
    # 不依賴 Gemini，純 lexicon + RSS，所以 Gemini 爆 quota 不影響
    try:
        family_text = family_interest.render_summary(GROUP_ID, days=30)
        if family_text:
            _push(family_text[:4900])
            print(f"家族熱話週報已推播（{len(family_text)} 字）")
        else:
            print("家族熱話無偵測到主題（過去 30 天訊息不足）")
    except Exception as e:
        print(f"ERR 家族熱話: {e}")


if __name__ == "__main__":
    main()
