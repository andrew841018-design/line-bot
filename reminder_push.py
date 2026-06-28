"""Reminder push — LINE 群組獨立提醒排程（2026-05-08 建立）。

每 15 分鐘 launchd 觸發。階梯式 push schedule（用戶要求 2026-05-08）：

  7-30 days：每週推一次（last_weekly_at >= 6.5 days ago）
  > 30 days：太遠，不主動推
  4-6 days：dead zone 不推
  ~ 3 days：推 1 次（pushed_3d = 1）
  ~ 1 day：推 1 次（pushed_1d = 1）
  4 hr 前：推 1 次（pushed_4hr = 1）
  2 hr 前：推 1 次（pushed_2hr = 1）
  1 hr 前：推 1 次（pushed_1hr = 1）
  到時：推 1 次 + mark done（pushed_now = 1, status = 'done'）

每階段都有 flag 防重複 push。launchd 每 15 分鐘跑一次提供精度 ±7.5 min。

跟 daily_briefing_discord 完全獨立（不走 Discord）。
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime

from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi, PushMessageRequest, TextMessage,
)

import memory
import line_mentions
from line_push_client import line_access_token
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("reminder_push")
STALE_PENDING_GRACE_SECONDS = 3600


def _line_access_token() -> str:
    return line_access_token()


def _push_to_group(
    group_id: str, text: str, max_retries: int = 3, message: object | None = None
) -> bool:
    """推到 LINE 群組。失敗回 False。

    2026-05-29 加 retry 防 transient 5xx（GP2#2）：
      - 429（monthly quota / rate limit）：不重試，立即 False
      - 其他 exception（5xx / network）：exponential backoff (1s/2s/4s) 重試
    """
    cfg = Configuration(access_token=_line_access_token())
    for attempt in range(max_retries):
        try:
            with ApiClient(cfg) as api_client:
                MessagingApi(api_client).push_message(
                    PushMessageRequest(
                        to=group_id,
                        messages=[message or TextMessage(text=text[:4900])],
                    )
                )
            return True
        except Exception as e:
            err_str = str(e)
            status = getattr(e, "status", None)
            if status == 429 or "429" in err_str:
                logger.warning("LINE push 429 quota (group=%s) — no retry", group_id)
                return False
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.info(
                    "LINE push fail (group=%s, attempt %d/%d), retry in %ds: %s",
                    group_id, attempt + 1, max_retries, wait, err_str[:120],
                )
                time.sleep(wait)
                continue
            logger.warning(
                "LINE push failed after %d attempts (group=%s): %s",
                max_retries, group_id, err_str[:120],
            )
            return False
    return False


def _decide_stage(r: dict, now: int) -> str | None:
    """判斷該 reminder 此刻該推哪個 stage，None = 不推。"""
    delta = r["remind_at"] - now
    days = delta / 86400
    hours = delta / 3600

    # 到時（-15min ~ +15min 之間，且 pushed_now 未推）
    if -0.25 <= hours <= 0.25 and not r["pushed_now"]:
        return "now"

    # 1 hr 前（窗口 0.5 ~ 1.5 hr）
    if 0.5 < hours <= 1.5 and not r["pushed_1hr"]:
        return "1hr"

    # 2 hr 前（窗口 1.5 ~ 2.5 hr）
    if 1.5 < hours <= 2.5 and not r["pushed_2hr"]:
        return "2hr"

    # 4 hr 前（窗口 3.5 ~ 4.5 hr）
    if 3.5 < hours <= 4.5 and not r["pushed_4hr"]:
        return "4hr"

    # 1 day 前（窗口 0.5 ~ 2 days，避開 4hr 前那段）
    # 條件：4.5 hr < hours, 0.5 days < days <= 2 days
    if hours > 4.5 and 0.5 < days <= 2 and not r["pushed_1d"]:
        return "1d"

    # 3 days 前（窗口 2 ~ 4 days）
    if 2 < days <= 4 and not r["pushed_3d"]:
        return "3d"

    # 4-7 days dead zone（不推）
    if 4 < days < 7:
        return None

    # 7-30 days：每週一次；超過 30 天太早，不主動推。
    if 7 <= days <= 30:
        last_weekly = r["last_weekly_at"]
        days_since = (now - last_weekly) / 86400 if last_weekly else 999
        if days_since >= 6.5:
            return "weekly"

    return None


_STAGE_LABELS = {
    "weekly": "（一週後）",
    "3d": "（3 天後）",
    "1d": "（明天）",
    "4hr": "（4 小時後）",
    "2hr": "（2 小時後）",
    "1hr": "（1 小時後）",
    "now": "（**現在 / 即將到時**）",
}
_DAY_BASED_STAGES = {"weekly", "3d", "1d"}


def _stage_label(stage: str, remind_at: int, now: int | None = None) -> str:
    """Return a human label based on the target calendar date, not the window."""
    if stage in _DAY_BASED_STAGES:
        now_ts = int(datetime.now().timestamp()) if now is None else now
        delta_days = (
            datetime.fromtimestamp(remind_at).date()
            - datetime.fromtimestamp(now_ts).date()
        ).days
        if delta_days == 0:
            return "（今天）"
        if delta_days == 1:
            return "（明天）"
        if delta_days == 2:
            return "（後天）"
        if delta_days == 7:
            return "（1 週後）"
        if delta_days == 30:
            return "（1 個月後）"
        if delta_days > 0:
            return f"（{delta_days} 天後）"
    return _STAGE_LABELS.get(stage, "")


def _participant_names(r: dict) -> list[str]:
    names = line_mentions.parse_participants(r.get("mention_aliases") or [])
    if line_mentions.is_all_participants(names):
        return ["全家"]
    return names


def _participant_plain_labels(r: dict) -> list[str]:
    names = line_mentions.parse_participants(r.get("mention_aliases") or [])
    if not names:
        return []
    if line_mentions.is_all_participants(names):
        return ["@all"]
    return [f"@{name}" for name in names if name]


def _format_push_text(r: dict, stage: str, now: int | None = None) -> str:
    dt = datetime.fromtimestamp(r["remind_at"])
    label = _stage_label(stage, r["remind_at"], now=now)
    body = f"⏰ 提醒{label}\n{dt.strftime('%Y-%m-%d %H:%M')} {r['action']}"
    participants = _participant_names(r)
    if participants:
        body += "\n參加人：" + "、".join(participants)
    return body


def _build_push_text_and_message(
    r: dict, stage: str, now: int | None = None
) -> tuple[str, object]:
    body = _format_push_text(r, stage, now=now)
    targets = line_mentions.reminder_actor_targets(
        str(r.get("user_id") or ""),
        r.get("mention_aliases") or [],
        str(r.get("action") or ""),
    )
    plain_labels = _participant_plain_labels(r) or None
    if not targets:
        text = line_mentions.text_with_plain_mentions(body, [], plain_labels)
        return text, TextMessage(text=text[:4900])
    message_dict = line_mentions.text_v2_dict(body, targets, plain_labels)
    return (
        line_mentions.text_with_plain_mentions(body, targets, plain_labels),
        line_mentions.sdk_message_from_text_v2_dict(message_dict),
    )


def _due_reminder_items(
    group_id: str | None = None,
    limit: int = 10_000,
    now: int | None = None,
) -> list[dict]:
    """Return reminders due for their next push stage without mutating DB."""
    if limit <= 0:
        return []
    now = int(datetime.now().timestamp()) if now is None else now
    due: list[dict] = []
    for r in memory.list_pending_reminders_full(group_id):
        stage = _decide_stage(r, now)
        if stage is None:
            continue
        text, message = _build_push_text_and_message(r, stage, now=now)
        due.append(
            {
                "reminder_id": r["reminder_id"],
                "group_id": r["group_id"],
                "stage": stage,
                "text": text,
                "message": message,
                "action": r["action"],
            }
        )
        if len(due) >= limit:
            break
    return due


def due_reminders_for_reply(
    group_id: str, limit: int = 4, now: int | None = None
) -> list[dict]:
    """Return due reminder_push entries that can piggyback on LINE reply_token.

    Caller must only mark these after reply_message succeeds.
    """
    if limit <= 0 or not group_id:
        return []
    return _due_reminder_items(group_id=group_id, limit=limit, now=now)


def mark_reminders_pushed(reminders: list[tuple[int, str]]) -> int:
    """Mark reminder_push entries after reply_message accepted them."""
    marked = 0
    for reminder_id, stage in reminders:
        if memory.mark_reminder_pushed(reminder_id, stage):
            marked += 1
    return marked


def push_reminders(dry_run: bool = False) -> int:
    """掃所有 pending reminder，依階梯式 stage 規則 push。

    回 push 成功的筆數。
    """
    deleted = memory.delete_stale_pending_reminders(
        grace_seconds=STALE_PENDING_GRACE_SECONDS
    )
    if deleted:
        logger.info("deleted %d stale pending reminders", deleted)

    sent = 0

    for item in _due_reminder_items():
        reminder_id = item["reminder_id"]
        group_id = item["group_id"]
        stage = item["stage"]
        text = item["text"]

        if dry_run:
            print(f"[DRY] rid={reminder_id} stage={stage} group={group_id}")
            print(f"      {text}")
            sent += 1
            continue

        ok = _push_to_group(group_id, text, message=item.get("message"))
        if ok:
            memory.mark_reminder_pushed(reminder_id, stage)
            logger.info(
                "pushed rid=%d stage=%s action=%r",
                reminder_id, stage, item["action"],
            )
            sent += 1
        else:
            logger.warning(
                "push failed rid=%d stage=%s, will retry next run",
                reminder_id, stage,
            )

    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="LINE 提醒推送（階梯式 schedule）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不真推 LINE，只印出來")
    args = parser.parse_args()

    sent = push_reminders(dry_run=args.dry_run)
    if sent:
        if args.dry_run:
            logger.info("reminder_push: %d 筆待推送（dry-run）", sent)
        else:
            logger.info("reminder_push: %d 筆已推送", sent)

    # 過期清理（每天 00:00 一次就夠）
    if datetime.now().hour == 0 and datetime.now().minute < 15:
        expired = memory.expire_old_reminders()
        if expired:
            logger.info("expired %d old reminders", expired)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
