import os
import sys
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

if os.getenv("LINE_BOT_DISABLE_DOTENV", "").lower() not in {"1", "true", "yes", "on"}:
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DISCORD_TIMEOUT = (3, 7)


@dataclass(frozen=True)
class DiscordDeliveryResult:
    status: str


def send_dm_result(message: str) -> DiscordDeliveryResult:
    """Send a DM and preserve whether a retry is known-safe or ambiguous."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    user_id = os.getenv("DISCORD_USER_ID")
    if not token or not user_id:
        print("Discord DM 設定缺失")
        return DiscordDeliveryResult("definite_failed")

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    # 建立 DM channel
    try:
        r = requests.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=headers,
            json={"recipient_id": user_id},
            timeout=DISCORD_TIMEOUT,
        )
    except requests.RequestException:
        print("建立 DM 失敗: network")
        return DiscordDeliveryResult("definite_failed")
    if r.status_code != 200:
        print(f"建立 DM 失敗: {r.status_code}")
        return DiscordDeliveryResult("definite_failed")

    channel_id = r.json()["id"]

    # 送訊息
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            json={"content": message},
            timeout=DISCORD_TIMEOUT,
        )
    except requests.RequestException:
        print("送訊息結果不明: network")
        return DiscordDeliveryResult("pending_unknown")
    if r.status_code != 200:
        print(f"送訊息失敗: {r.status_code}")
        if 500 <= r.status_code:
            return DiscordDeliveryResult("pending_unknown")
        return DiscordDeliveryResult("definite_failed")

    print("Discord DM 送出成功")
    return DiscordDeliveryResult("sent")


def send_dm(message: str) -> bool:
    """Backward-compatible boolean sender for existing callers."""
    return send_dm_result(message).status == "sent"


def _today_pt() -> str:
    """Gemini 免費額度以 00:00 PT 重置，用 PT 日期當 per-day 去重 key。

    自帶算法（不 import gemini_client，避免把 google-genai/config 重依賴拉進
    被 health monitor import 的路徑）。zoneinfo 是 stdlib，py3.9+/3.13 皆有。
    """
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def notify_quota_pressure(project, state, detail=""):
    """Deprecated no-op: Andrew disabled Discord quota-pressure alerts."""
    return False


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "測試提醒"
    sys.exit(0 if send_dm(msg) else 1)
