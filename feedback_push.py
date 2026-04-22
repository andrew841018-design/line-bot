"""
每日 20:00 推播回饋問題 — 由 launchd 觸發。

usage:
    python feedback_push.py
    python feedback_push.py --dry-run
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
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("feedback_push")

_QUESTION = "今天有哪裡可以改進的地方嗎？\n（對我說話方式、回應內容、任何感覺都可以說喔）"


def _push(group_id: str, text: str) -> None:
    cfg = Configuration(access_token=settings.line_channel_access_token)
    with ApiClient(cfg) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=group_id, messages=[TextMessage(text=text)])
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="每日回饋推播")
    parser.add_argument("--group-id", default=settings.allowed_group_id)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    group_id = (args.group_id or "").strip()
    if not group_id:
        print("ERROR: 沒有 group_id，請設定 ALLOWED_GROUP_ID", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"[dry-run] would push: {_QUESTION}")
        feedback_collector.record_push_time()
        return 0

    try:
        _push(group_id, _QUESTION)
        feedback_collector.record_push_time()
        logger.info("feedback question pushed to group=%s", group_id)
    except Exception as e:
        logger.exception("push failed: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
