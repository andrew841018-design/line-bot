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
import time

from linebot.v3.messaging import (
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

import feedback_collector
from line_push_client import line_access_token, line_configuration
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("feedback_push")

_QUESTION = (
    "今天有哪裡可以改進的地方嗎？\n（對我說話方式、回應內容、任何感覺都可以說喔）"
)


def _line_access_token() -> str:
    return line_access_token()


def _push(group_id: str, text: str, max_retries: int = 3) -> None:
    """推 feedback 問題到 LINE。

    2026-05-29 加 retry 防 transient 5xx（GP2#2）— feedback_push 每週只跑 1 次，
    一次 5xx 就 7 天空白；加 retry 把這個窗口收掉。
      - 429：raise（不重試，caller 看 monthly quota 不可解）
      - 5xx / network：exponential backoff (1s/2s/4s) 重試
    """
    cfg = line_configuration()
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            with ApiClient(cfg) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(to=group_id, messages=[TextMessage(text=text)])
                )
            return
        except Exception as e:
            err_str = str(e)
            status = getattr(e, "status", None)
            if status == 429 or "429" in err_str:
                raise
            last_exc = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(
                    "feedback_push fail (attempt %d/%d), retry in %ds: %s",
                    attempt + 1, max_retries, wait, err_str[:120],
                )
                time.sleep(wait)
                continue
    if last_exc is not None:
        raise last_exc


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
