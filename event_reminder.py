"""家族行事曆 — T-3 / T-2 / T-1 三天提醒推播（launchd 每天 07:00 觸發）。

每次跑時依序處理 3 種 offset (3 天後 / 後天 / 明天)，每個 event 在每個 offset
只推一次（per-offset reminded_Xd 欄位 idempotency 保證）。

Backward-compat: 舊 reminded_at 欄位保留，主流程已不再讀（user 2026-05-21 directive）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402

import calendar_db  # noqa: E402

# 2026-05-29 加 retry 防 transient 5xx 一棒打死（GP2#2）
# Retry 重試 500/502/503/504；429 monthly quota 不重試（HTTPAdapter 預設只 retry 表列 status）
_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST"],
        )
    ),
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 2026-05-27 multi-group: event_reminder 仍是 family-only launchd job（不擴推 mom 群）。
# 優先讀顯式 FAMILY_GROUP_ID；legacy LINE_ALLOWED_GROUP_ID / ALLOWED_GROUP_ID 作 fallback。
GROUP_ID = (
    os.environ.get("FAMILY_GROUP_ID")
    or os.environ.get("LINE_ALLOWED_GROUP_ID")
    or os.environ.get("ALLOWED_GROUP_ID", "")
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


# 2026-05-29 防 PII 外洩：LINE 失敗 response 理論上只 echo 屬性路徑、不 echo 值，
# 但仍 defense-in-depth 把任何 LINE userId（U + 32 hex）遮成 U***（GP2 review 1b）。
_UID_RE = re.compile(r"U[0-9a-f]{32}")


def _post(messages: list) -> bool:
    """共用底層推播：組好的 messages list → LINE push API。
    token / GROUP_ID 缺失或非 200 回 False；失敗 log 遮蔽 userId 防 PII 外洩。
    （_push / _push_mention 共用，遮蔽邏輯只寫一處 — GP2 DRY review）"""
    token = _get_token()
    if not token or not GROUP_ID:
        logger.error("missing TOKEN or GROUP_ID; skip push")
        return False
    try:
        resp = _session.post(
            _PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": GROUP_ID, "messages": messages},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning(
            "LINE push failed %d: %s",
            resp.status_code,
            _UID_RE.sub("U***", resp.text[:300]),
        )
        return False
    except Exception as e:
        logger.warning("LINE push exception: %s", e)
        return False


def _push(text: str) -> bool:
    return _post([{"type": "text", "text": text}])


def _push_mention(text_template: str, placeholder: str, user_id: str) -> bool:
    """textV2 + substitution 真 @mention 推播（會跳通知給被標記者）。
    text_template 內用半形 {placeholder} 當佔位符；勿放其他非佔位符的半形 {}
    （LINE 會把半形 {} 內的字當佔位符解析）。"""
    return _post(
        [
            {
                "type": "textV2",
                "text": text_template,
                "substitution": {
                    placeholder: {
                        "type": "mention",
                        "mentionee": {"type": "user", "userId": user_id},
                    }
                },
            }
        ]
    )


# ── 2026-05-29 one-off（user directive）──────────────────────────────────────
# 「看醫生外科」6/02 這筆事件 DB 時間 08:00 與其他記錄 13:00 兜不攏 → 不自己猜，
# T-1（6/01）@媽媽 直接問本人確認。她回覆後寫回正確時間 + 移除這整段（含 .env MOM_USER_ID）。
# 只在 offset==1 問一次：避免 T-3/T-2/T-1 三 offset 各問一次（每 offset idempotency 獨立，GP1 Q3）。
# 其他 offset 對這筆事件直接 skip — 不發可能是錯誤時間的標準提醒。
# ⚠️ 單一推播視窗：只在 6/01(T-1) 推一次；6/02 是 offset 0、不在 REMINDER_OFFSETS → 不會重列。
#    若 6/01 推失敗（如 429）無自動 retry（下方 L217「at-least-once」註解對此 one-off 不成立）→ 需人工 6/01 兜底確認。
_ASK_MOM_EVENT_IDS = {"a653382f7a494f509f21c5e8c8462b19"}
_ASK_MOM_OFFSET = 1
_ASK_MOM_PLACEHOLDER = "mom"
_ASK_MOM_TEXT = (
    "{mom}\n"
    "提醒一下～6/2(二) 要看外科 陳晉興醫師（外科8診）\n"
    "是早上 8:00 還是下午 1:00 呀？跟我說我幫你記正確 🙏"
)
# mention 投遞失敗 / MOM_USER_ID 未設 → fallback 純文字（仍把問題送出，不 silent drop，GP2 #6）
_ASK_MOM_TEXT_PLAIN = (
    "媽媽\n"
    "提醒一下～6/2(二) 要看外科 陳晉興醫師（外科8診）\n"
    "是早上 8:00 還是下午 1:00 呀？跟我說我幫你記正確 🙏"
)


def main() -> int:
    if not GROUP_ID:
        logger.error("ALLOWED_GROUP_ID 未設定，無法推播")
        return 1

    total_sent = 0
    total_due = 0
    for offset in calendar_db.REMINDER_OFFSETS:
        events = calendar_db.list_due_for_reminder(GROUP_ID, days_ahead=offset)
        if not events:
            logger.info("no events due for T-%d reminder", offset)
            continue
        total_due += len(events)
        for e in events:
            if e["event_id"] in _ASK_MOM_EVENT_IDS:
                # one-off：只在 T-1 @媽媽 問時間一次；其他 offset 對這筆事件 skip
                if offset != _ASK_MOM_OFFSET:
                    continue
                mom_id = os.environ.get("MOM_USER_ID")
                pushed = (
                    _push_mention(_ASK_MOM_TEXT, _ASK_MOM_PLACEHOLDER, mom_id)
                    if mom_id
                    else False
                )
                if not pushed:
                    # mention 失敗（userId 未設 / 非 200）→ fallback 純文字，至少送達
                    pushed = _push(_ASK_MOM_TEXT_PLAIN)
            else:
                pushed = _push(_format_event(e, offset))
            if pushed:
                calendar_db.mark_reminded(e["event_id"], offset, GROUP_ID)
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
