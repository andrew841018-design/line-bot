"""food_push.py — 每日推「今晚可煮 + 該補貨」到家族群（launchd 觸發）。

照 event_reminder.py 範式；import 其推播 / 遮蔽 / token 函式（GP2 E：DRY，不複製）。
- 純 text 推播，**不用** event_reminder._push_mention（會帶 userId 點名，違反 GP2 A 不點名）。
- 無可煮菜 + 無待買 → 不推（graceful-empty；呼應 gate 發現的稀疏資料，不對空表 fire）。
- GROUP_ID 沿用 event_reminder.GROUP_ID（FAMILY_GROUP_ID）。

部署（plist 在專案內，需手動安裝）：
    cp com.andrew.line-bot-food-push.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.andrew.line-bot-food-push.plist
"""
from __future__ import annotations

import datetime
import logging
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import event_reminder  # noqa: E402  — 重用 _push / token / 遮蔽（GP2 E DRY）
import food_db  # noqa: E402
import food_recipes  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("food_push")

GROUP_ID = event_reminder.GROUP_ID


def build_message() -> str:
    """組今日飲食推播文字。無可煮 + 無待買 → 回 ""（caller 不推）。只給全家層級、不點名。"""
    if not GROUP_ID:
        return ""
    inventory = food_db.query_inventory(GROUP_ID)
    prefs = food_db.query_prefs(GROUP_ID)
    shopping = food_db.query_shopping(GROUP_ID)

    parts: list[str] = []
    cook = food_recipes.format_suggestions(inventory, prefs.get("dislikes"))
    if cook:
        parts.append(cook)
    if shopping:
        parts.append("🛒 別忘了補貨：" + "、".join(shopping))
    return "\n\n".join(parts)


def _retry_key() -> str:
    """同一天重跑用同 key（LINE 端 dedup，避免 launchd 同日 retry 重推）。"""
    seed = f"line_bot:food_push:{GROUP_ID}:{datetime.date.today().isoformat()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def main() -> int:
    if not GROUP_ID:
        logger.error("FAMILY_GROUP_ID 未設定，無法推播")
        return 1
    msg = build_message()
    if not msg:
        logger.info("no food suggestions today; skip push (graceful-empty)")
        return 0
    ok = event_reminder._push(msg, retry_key=_retry_key())
    logger.info("food push %s", "sent" if ok else "failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
