import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))


def send_dm(message: str) -> bool:
    token = os.getenv("DISCORD_BOT_TOKEN")
    user_id = os.getenv("DISCORD_USER_ID")
    if not token or not user_id:
        print("Discord DM 設定缺失")
        return False

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    # 建立 DM channel
    r = requests.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers=headers,
        json={"recipient_id": user_id},
        timeout=(5, 15),
    )
    if r.status_code != 200:
        print(f"建立 DM 失敗: {r.status_code}")
        return False

    channel_id = r.json()["id"]

    # 送訊息
    r = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=headers,
        json={"content": message},
        timeout=(5, 15),
    )
    if r.status_code != 200:
        print(f"送訊息失敗: {r.status_code}")
        return False

    print("Discord DM 送出成功")
    return True


def _today_pt() -> str:
    """Gemini 免費額度以 00:00 PT 重置，用 PT 日期當 per-day 去重 key。

    自帶算法（不 import gemini_client，避免把 google-genai/config 重依賴拉進
    被 health monitor import 的路徑）。zoneinfo 是 stdlib，py3.9+/3.13 皆有。
    """
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def notify_quota_pressure(project, state, detail=""):
    """Gemini quota 吃緊即時通知 Andrew（per-project-per-day 去重）。

    project: 哪個 GCP project 吃緊（如 "line_bot 共用 key" / "Cramer 獨立 key"）。
    state:   呼叫端的 dict，用 state["quota_alert_dates"] 去重。
             health monitor 傳它既有的 state（跟著 save_health_state 落盤，跨輪去重）；
             Cramer 傳 {}（每天一次 briefing，天然 per-day）。
    detail:  固定短語（如「已 fallback 英文標題」）。禁傳 raw exception（mention 注入/洩漏）。
    回傳 True = 真的推了（send_dm 成功且今天該 project 首次）。fail-soft：絕不拋例外。
    """
    try:
        today = _today_pt()
        dates = state.setdefault("quota_alert_dates", {})
        if dates.get(project) == today:
            return False  # 今天這個 project 已通知過 → 不洗版
        safe_detail = str(detail or "").replace("@everyone", "@ everyone").replace("@here", "@ here")
        msg = "📊 **Gemini 額度吃緊**：" + str(project) + " 今日已撞到免費額度上限"
        if safe_detail:
            msg += "（" + safe_detail + "）"
        msg += "，已自動降級／fallback；等 00:00 PT（台灣下午）重置。"
        if send_dm(msg):
            dates[project] = today
            return True
        return False  # send_dm 失敗 → 不記 state，下輪可重試
    except Exception as e:
        print("[quota_alert] notify 失敗:", e, file=sys.stderr)
        return False


if __name__ == "__main__":
    import sys

    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "測試提醒"
    sys.exit(0 if send_dm(msg) else 1)
