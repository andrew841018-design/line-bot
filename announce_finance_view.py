"""家族財經觀點追蹤器上線通知 — 一次性 ad-hoc 推播。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

from line_push_client import LinePushError, line_access_token, push_text  # noqa: E402

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)

MSG = """📈 家族財經觀點追蹤器上線

從現在起，咪寶會自動記錄大家在群裡聊到的具體標的 + 方向（例「0050 會漲到 180」「Fed 不會升息」），到期後比對 yfinance 驗證對錯。

查詢指令：
• /觀點 — 看全家最近觀點
• /觀點 媽媽 — 看特定家人觀點
• /觀點 0050 — 看特定標的觀點

每週日 20:00 會推一份「家族財經觀點回顧」，統計命中 / 落空 / 待驗。"""


def main() -> int:
    token = line_access_token()
    if not token or not GROUP_ID:
        print("ERR: env not set")
        return 1
    try:
        push_text(GROUP_ID, MSG[:5000], timeout=10, fallback_token=token)
    except LinePushError as e:
        print(f"{e.status_code or 'ERR'} {e.response_text[:300]}")
        return 1
    print("200 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
