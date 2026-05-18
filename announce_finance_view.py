"""家族財經觀點追蹤器上線通知 — 一次性 ad-hoc 推播。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from line_token_refresh import get_line_token  # noqa: E402

TOKEN = get_line_token() or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)
URL = "https://api.line.me/v2/bot/message/push"

MSG = """📈 家族財經觀點追蹤器上線

從現在起，咪寶會自動記錄大家在群裡聊到的具體標的 + 方向（例「0050 會漲到 180」「Fed 不會升息」），到期後比對 yfinance 驗證對錯。

查詢指令：
• /觀點 — 看全家最近觀點
• /觀點 媽媽 — 看特定家人觀點
• /觀點 0050 — 看特定標的觀點

每週日 20:00 會推一份「家族財經觀點回顧」，統計命中 / 落空 / 待驗。"""


def main() -> int:
    if not TOKEN or not GROUP_ID:
        print("ERR: env not set")
        return 1
    resp = requests.post(
        URL,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": GROUP_ID, "messages": [{"type": "text", "text": MSG[:5000]}]},
        timeout=10,
    )
    print(resp.status_code, resp.text[:300])
    return 0 if resp.ok else 1


if __name__ == "__main__":
    sys.exit(main())
