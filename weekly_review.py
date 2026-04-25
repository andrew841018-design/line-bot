"""
Layer 3 的 CLI 入口 — 週期性檢討群組對話、產生候選規則、推送到 LINE。

用法（本機 cron / launchd）：
    python weekly_review.py                 # 用 settings.allowed_group_id, 7 天
    python weekly_review.py --days 14       # 改抓 14 天
    python weekly_review.py --dry-run       # 只印不推
    python weekly_review.py --group-id Cxxx # 指定群組

免費額度考量：
- Gemini: 走 flash-lite（1000 RPD），一週一次根本吃不完
- LINE Push: 免費方案 500 則/月，一週一次 = 約 4-5 則/月，夠

成功後使用者可以在群組回 /採用 1 2 / /採用 全部 / /採用 無 來升級 drafts。
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

import review
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("weekly_review")


def _push_to_line(group_id: str, text: str) -> None:
    cfg = Configuration(access_token=settings.line_channel_access_token)
    # LINE 單則上限 5000,留點 margin
    text = text[:4900]
    with ApiClient(cfg) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=group_id,
                messages=[TextMessage(text=text)],
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Layer 3 週期性自我檢討")
    parser.add_argument(
        "--group-id",
        default=settings.allowed_group_id,
        help="目標群組 ID（預設讀 .env 的 ALLOWED_GROUP_ID）",
    )
    parser.add_argument("--days", type=int, default=7, help="回看天數（預設 7）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只印報告不推 LINE；drafts 仍會寫進 DB",
    )
    args = parser.parse_args()

    group_id = (args.group_id or "").strip()
    if not group_id:
        print(
            "ERROR: 沒有 group_id。請在 .env 設定 ALLOWED_GROUP_ID 或用 --group-id 指定。",
            file=sys.stderr,
        )
        return 2

    logger.info("running weekly review for group=%s days=%d", group_id, args.days)
    report_text, drafts = review.run_weekly_review(group_id, days=args.days)

    print(report_text)
    print(f"\n[CLI] drafts_added={len(drafts)}", file=sys.stderr)

    if args.dry_run:
        logger.info("dry-run, 不推 LINE")
        return 0

    try:
        _push_to_line(group_id, report_text)
        logger.info("已推送到 LINE group=%s", group_id)
    except Exception as e:
        logger.exception("push_message 失敗: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
