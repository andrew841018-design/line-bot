"""
掃描 pending_feedback.json，用 Gemini 判讀評語，更新 persona，推播改進摘要。
由 launchd 在 02:00（有 quota 時）和 15:00（quota 復活後）觸發。

Gemini 429 時不清空 json，讓下一個排程重試。
"""
from __future__ import annotations

import argparse
import logging
import sys

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

import feedback_collector
import gemini_client
import memory
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("process_feedback")


def _is_quota_error(e: Exception) -> bool:
    s = str(e)
    return ("429" in s or "RESOURCE_EXHAUSTED" in s) and (
        "PerDay" in s or "free_tier_requests" in s
    )


def _push(group_id: str, text: str) -> None:
    cfg = Configuration(access_token=settings.line_channel_access_token)
    with ApiClient(cfg) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=group_id,
                messages=[TextMessage(text=text[:4900])],
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="回饋處理（掃描 + 改進 + 推播）")
    parser.add_argument("--group-id", default=settings.allowed_group_id)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    group_id = (args.group_id or "").strip()
    if not group_id:
        print("ERROR: 沒有 group_id，請設定 ALLOWED_GROUP_ID", file=sys.stderr)
        return 2

    pending = feedback_collector.load_pending()
    if not pending:
        logger.info("pending_feedback.json 為空，跳過")
        return 0

    logger.info("pending 訊息數：%d，開始 Gemini 掃描", len(pending))

    # Step 1：Gemini 判斷哪些是評語
    try:
        feedback_items = gemini_client.scan_feedback_messages(pending)
    except Exception as e:
        if _is_quota_error(e):
            logger.warning("Gemini quota 已用完，保留 pending json 等待下次觸發")
            return 0  # 不清空，等 15:00 重試
        logger.exception("scan_feedback_messages 失敗: %s", e)
        return 1

    if not feedback_items:
        logger.info("未找到有效評語，清空 pending")
        feedback_collector.clear_pending()
        return 0

    logger.info("找到 %d 條評語", len(feedback_items))

    # Step 2：生成改進摘要 + corrections
    try:
        result = gemini_client.generate_improvement_push(feedback_items)
    except Exception as e:
        if _is_quota_error(e):
            logger.warning("Gemini quota 已用完（generate），保留 pending json")
            return 0
        logger.exception("generate_improvement_push 失敗: %s", e)
        return 1

    push_message = result.get("push_message", "")
    corrections = result.get("corrections") or []

    # Step 3：更新 persona notes
    for rule in corrections:
        rule = rule.strip()
        if rule:
            memory.add_persona_note(group_id, "correction", "每日回饋", rule)
            logger.info("[Feedback] persona correction saved: %s", rule[:60])

    # Step 4：推播改進摘要
    if push_message and not args.dry_run:
        try:
            _push(group_id, push_message)
            logger.info("改進摘要已推播到 group=%s", group_id)
        except Exception as e:
            logger.exception("推播失敗: %s", e)
    elif args.dry_run:
        print("[dry-run] push_message:", push_message)
        print("[dry-run] corrections:", corrections)

    # Step 5：清空 pending
    feedback_collector.clear_pending()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
