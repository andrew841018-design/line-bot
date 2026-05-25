"""家族行事曆 — T-3 / T-2 / T-1 三天提醒推播（launchd 每天 07:00 觸發）。

每次跑時依序處理 3 種 offset (3 天後 / 後天 / 明天)，每個 event 在每個 offset
只推一次（per-offset reminded_Xd 欄位 idempotency 保證）。

Backward-compat: 舊 reminded_at 欄位保留，主流程已不再讀（user 2026-05-21 directive）。
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
_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _get_token() -> str:
    """優先 line_token_cache.json (token_refresh job 每 10 分鐘 refresh)，
    fallback .env long-lived token（向後相容）。"""
    try:
        import line_token_refresh
        return line_token_refresh.get_line_token() or os.environ.get(
            "LINE_CHANNEL_ACCESS_TOKEN", ""
        )
    except Exception:
        return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


_OFFSET_LABEL: dict[int, str] = {
    -2: "前天",
    -1: "昨天",
    0: "今天",
    1: "明天",
    2: "後天",
    3: "3 天後",
    7: "1 週後",
    30: "1 個月後",
}


def _format_event(e: dict, offset: int = 7) -> str:
    time_part = f" {e['event_time']}" if e["event_time"] else ""
    loc_part = f"\n📍 {e['location']}" if e["location"] else ""
    try:
        parts = json.loads(e["participants"] or "[]")
    except Exception:
        parts = []
    ppl_part = f"\n👥 {'、'.join(parts)}" if parts else ""
    label = _OFFSET_LABEL.get(offset)
    if label is None:
        label = f"{abs(offset)} 天前" if offset < 0 else f"{offset} 天後"
    return (
        f"🔔 **{label}活動提醒**\n"
        f"📅 {e['event_date']}{time_part}\n"
        f"🎯 {e['title']}{loc_part}{ppl_part}"
    )


def _push(text: str) -> bool:
    token = _get_token()
    if not token or not GROUP_ID:
        logger.error("missing TOKEN or GROUP_ID; skip push")
        return False
    try:
        resp = requests.post(
            _PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
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

    total_sent = 0
    total_due = 0
    for offset in calendar_db.REMINDER_OFFSETS:
        events = calendar_db.list_due_for_reminder(days_ahead=offset)
        if not events:
            logger.info("no events due for T-%d reminder", offset)
            continue
        total_due += len(events)
        for e in events:
            text = _format_event(e, offset)
            if _push(text):
                calendar_db.mark_reminded(e["event_id"], offset)
                total_sent += 1
                logger.info(
                    "reminder sent (T-%d): %s '%s' on %s",
                    offset, e["event_id"], e["title"], e["event_date"],
                )
            else:
                # push 失敗不 mark — 下次 launchd 跑會 retry（at-least-once 保證）
                logger.warning(
                    "reminder push failed (T-%d): %s", offset, e["event_id"]
                )

    logger.info("done: %d/%d reminders sent", total_sent, total_due)
    return 0


if __name__ == "__main__":
    sys.exit(main())
