"""LINE bot 累積更新公告自動推播（launchd 每天觸發）。

設計：
- 讀 `pending_line_push.txt`（accumulated 更新內容草稿）
- 試 LINE push：
    - 200 → 清空 pending 檔 + Discord DM 通知 Andrew「終於推出去了」
    - 429（月配額爆）→ 留 pending 檔，明天自動重試
    - 其他錯誤 → log 警告，留 pending 檔
- 沒 pending 檔 / 空檔案 → no-op 直接離開
- 每天跑一次（09:00），月初 quota 重置那天會自動成功
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE))

PENDING_FILE = BASE / "pending_line_push.txt"
GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)


def _get_line_token() -> str:
    """v3 stateless 優先，fallback long-lived。"""
    try:
        from line_token_refresh import get_line_token
        return get_line_token()
    except Exception:
        return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def _notify_discord(text: str) -> None:
    """成功/失敗都通知 Andrew Discord DM（不影響主流程）。"""
    try:
        from notify_discord import send_dm
        send_dm(text)
    except Exception:
        pass


def main() -> int:
    if not PENDING_FILE.exists():
        print("[line_bot_update_push] no pending file, skip")
        return 0
    msg = PENDING_FILE.read_text().strip()
    if not msg:
        print("[line_bot_update_push] pending file empty, skip")
        return 0
    if not GROUP_ID:
        print("[line_bot_update_push] LINE_ALLOWED_GROUP_ID 未設定", file=sys.stderr)
        return 1
    token = _get_line_token()
    if not token:
        print("[line_bot_update_push] no LINE token", file=sys.stderr)
        return 1
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": GROUP_ID, "messages": [{"type": "text", "text": msg[:4900]}]},
            timeout=15,
        )
    except Exception as e:
        print(f"[line_bot_update_push] request exception: {e}", file=sys.stderr)
        return 1

    if r.status_code == 200:
        # 成功 → 清空 pending、通知 Andrew
        PENDING_FILE.unlink(missing_ok=True)
        head = msg.split("\n", 1)[0][:80]
        _notify_discord(
            f"✅ LINE 群更新公告已推出：\n首行：{head}\n（pending 檔已清空）"
        )
        print(f"[line_bot_update_push] OK pushed, cleared {PENDING_FILE.name}")
        return 0
    if r.status_code == 429:
        # 月配額仍爆，明天再試
        print(f"[line_bot_update_push] 429 monthly quota exhausted, retry tomorrow")
        return 0  # 不算錯誤
    # 其他狀態
    print(
        f"[line_bot_update_push] HTTP {r.status_code}: {r.text[:200]}",
        file=sys.stderr,
    )
    _notify_discord(
        f"⚠️ LINE 群更新公告 push 失敗 HTTP {r.status_code}：{r.text[:200]}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
