"""家族行事曆 — 7 天前提醒推播（launchd 每天 07:00 觸發）。

掃 events 找 event_date = today + 7 且 status='active' 且 reminded_at IS NULL，
推播到家族 LINE 群，更新 reminded_at。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

import calendar_db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _format_event(e: dict) -> str:
    time_part = f" {e['event_time']}" if e["event_time"] else ""
    loc_part = f"\n📍 {e['location']}" if e["location"] else ""
    try:
        parts = json.loads(e["participants"] or "[]")
    except Exception:
        parts = []
    ppl_part = f"\n👥 {'、'.join(parts)}" if parts else ""
    return (
        f"🔔 **7 天後活動提醒**\n"
        f"📅 {e['event_date']}{time_part}\n"
        f"🎯 {e['title']}{loc_part}{ppl_part}"
    )


def _push(text: str) -> bool:
    if not TOKEN or not GROUP_ID:
        logger.error("missing TOKEN or GROUP_ID; skip push")
        return False
    try:
        resp = requests.post(
            _PUSH_URL,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
            json={"to": GROUP_ID, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("LINE push failed %d: %s", resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        logger.warning("LINE push exception: %s", e)
        return False


def main() -> int:
    if not GROUP_ID:
        logger.error("ALLOWED_GROUP_ID 未設定，無法推播")
        return 1

    events = calendar_db.list_due_for_reminder(days_ahead=7)
    if not events:
        logger.info("no events due for 7-day reminder")
        return 0

    sent = 0
    for e in events:
        # 只推屬於有設定 group_id 的活動（多群組未來擴充用，目前只一個家族群）
        text = _format_event(e)
        if _push(text):
            calendar_db.mark_reminded(e["event_id"])
            sent += 1
            logger.info("reminder sent: %s '%s' on %s", e["event_id"], e["title"], e["event_date"])
        else:
            logger.warning("reminder push failed: %s", e["event_id"])

    logger.info("done: %d/%d reminders sent", sent, len(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
