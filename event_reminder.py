"""家族行事曆 — 多 offset 活動提醒推播（launchd 每天 07:00 觸發）。

每次跑時依序處理 calendar_db.REMINDER_OFFSETS，每個 event 在每個 offset
只推一次（per-offset reminded_Xd 欄位 idempotency 保證）。

Backward-compat: 舊 reminded_at 欄位保留，主流程已不再讀（user 2026-05-21 directive）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from requests.adapters import HTTPAdapter  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402

import calendar_db  # noqa: E402
import line_mentions  # noqa: E402
from line_push_client import (  # noqa: E402
    LinePushError,
    line_access_token,
    push_messages,
    validate_push_text,
)

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


def _get_token() -> str:
    """優先 line_token_cache.json，fallback .env long-lived token（向後相容）。"""
    return line_access_token()


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
_UID_RE = re.compile(r"[UCR][0-9A-Fa-f]{32}")

POST_OK = "ok"
POST_REJECT = "definite_reject"
POST_TRANSIENT = "transient_or_unknown"


def _redact_line_ids(text: str) -> str:
    return _UID_RE.sub(lambda m: f"{m.group(0)[0]}***", text or "")


def _retry_key_for_event(e: dict, offset: int, suffix: str = "main") -> str:
    seed = f"line_bot:event_reminder:{GROUP_ID}:{e.get('event_id')}:{offset}:{suffix}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _post_result(
    messages: list,
    retry_key: str | None = None,
    reminder_refs: list[dict | None] | None = None,
) -> str:
    """共用底層推播：組好的 messages list → LINE push API。
    token / GROUP_ID 缺失或非 200/409 回分類結果；失敗 log 先遮蔽 LINE id 再截斷。
    （_push / _push_mention 共用，遮蔽邏輯只寫一處 — GP2 DRY review）"""
    token = _get_token()
    if not token or not GROUP_ID:
        logger.error("missing TOKEN or GROUP_ID; skip push")
        return POST_REJECT
    try:
        sent_message_ids: list[str] = []
        push_messages(
            GROUP_ID,
            messages,
            retry_key=retry_key or str(uuid.uuid4()),
            session=_session,
            timeout=10,
            ok_statuses=(200, 409),
            fallback_token=token,
            sent_message_ids=sent_message_ids,
        )
        try:
            import memory

            for idx, message_id in enumerate(sent_message_ids):
                message = messages[idx] if idx < len(messages) else {}
                text = (
                    str(message.get("text") or "")
                    if isinstance(message, dict)
                    else ""
                )
                if text:
                    memory.log_raw_message(
                        GROUP_ID,
                        message_id,
                        "__bot__",
                        text,
                    )
                    reference = (
                        reminder_refs[idx]
                        if reminder_refs is not None and idx < len(reminder_refs)
                        else None
                    )
                    if reference:
                        memory.log_sent_reminder_reference(
                            GROUP_ID,
                            message_id,
                            reminder_id=reference.get("reminder_id"),
                            source_kind=str(reference.get("source_kind") or ""),
                            source_ref=str(reference.get("source_ref") or ""),
                        )
        except Exception as archive_error:
            # The LINE request already succeeded. Never convert an archival
            # failure into a retry that would duplicate the reminder.
            logger.error(
                "event reminder delivered but sent-message archive failed: %s",
                str(archive_error)[:200],
            )
        return POST_OK
    except LinePushError as e:
        body = _redact_line_ids(e.response_text or str(e))[:300]
        logger.warning(
            "LINE push failed %s: %s",
            e.status_code or "request",
            body,
        )
        if e.status_code is None or e.status_code == 429 or e.status_code >= 500:
            return POST_TRANSIENT
        return POST_REJECT


def _post(messages: list, retry_key: str | None = None) -> bool:
    return _post_result(messages, retry_key=retry_key) == POST_OK


def _push_result(
    text: str,
    retry_key: str | None = None,
    reminder_ref: dict | None = None,
) -> str:
    return _post_result(
        [{"type": "text", "text": text}],
        retry_key=retry_key,
        reminder_refs=[reminder_ref],
    )


def _push(text: str, retry_key: str | None = None) -> bool:
    return _push_result(text, retry_key=retry_key) == POST_OK


def _push_mention_result(
    text_template: str,
    placeholder: str,
    user_id: str,
    retry_key: str | None = None,
    reminder_ref: dict | None = None,
) -> str:
    """textV2 + substitution 真 @mention 推播（會跳通知給被標記者）。
    text_template 內用半形 {placeholder} 當佔位符；勿放其他非佔位符的半形 {}
    （LINE 會把半形 {} 內的字當佔位符解析）。"""
    return _post_result(
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
        ],
        retry_key=retry_key,
        reminder_refs=[reminder_ref],
    )


def _push_mention(
    text_template: str, placeholder: str, user_id: str, retry_key: str | None = None
) -> bool:
    return (
        _push_mention_result(text_template, placeholder, user_id, retry_key=retry_key)
        == POST_OK
    )


def _load_one_off_config() -> dict | None:
    raw = os.environ.get("EVENT_REMINDER_ONE_OFF_JSON")
    if raw:
        try:
            data = json.loads(raw)
        except Exception as e:
            logger.warning("invalid EVENT_REMINDER_ONE_OFF_JSON: %s", e)
            return None
    elif os.environ.get("REMINDER_ASK_EVENT_IDS"):
        data = {
            "event_ids": os.environ.get("REMINDER_ASK_EVENT_IDS", ""),
            "offset": os.environ.get("REMINDER_ASK_OFFSET", 1),
            "placeholder": os.environ.get("REMINDER_ASK_PLACEHOLDER", "target"),
            "mention_text": os.environ.get("REMINDER_ASK_TEXT", ""),
            "plain_text": os.environ.get("REMINDER_ASK_TEXT_PLAIN", ""),
            "user_id": os.environ.get("REMINDER_ASK_USER_ID", ""),
        }
    else:
        path = Path(
            os.environ.get(
                "EVENT_REMINDER_PRIVATE_CONFIG",
                str(Path(__file__).parent / "event_reminder_private.json"),
            )
        )
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("invalid event reminder private config %s: %s", path, e)
            return None
    if not isinstance(data, dict):
        return None
    event_ids = data.get("event_ids") or data.get("event_id") or []
    if isinstance(event_ids, str):
        event_ids = [x.strip() for x in event_ids.split(",") if x.strip()]
    event_ids = {str(x) for x in event_ids if x}
    if not event_ids:
        return None
    user_id = data.get("user_id") or ""
    user_id_env = data.get("user_id_env")
    if user_id_env and not user_id:
        user_id = os.environ.get(str(user_id_env), "")
    if not user_id:
        user_id = os.environ.get("EVENT_REMINDER_ONE_OFF_USER_ID", "")
    mention_text = str(data.get("mention_text") or data.get("text") or "")
    plain_text = str(data.get("plain_text") or "")
    placeholder = str(data.get("placeholder") or "target")
    if mention_text and not plain_text:
        plain_text = mention_text.replace(f"{{{placeholder}}}", placeholder)
    if not (mention_text or plain_text):
        return None
    try:
        offset = int(data.get("offset", 1))
    except (TypeError, ValueError):
        offset = 1
    return {
        "event_ids": event_ids,
        "offset": offset,
        "placeholder": placeholder,
        "mention_text": mention_text,
        "plain_text": plain_text,
        "user_id": str(user_id or ""),
    }


def should_skip_standard_reminder(e: dict, offset: int) -> bool:
    cfg = _load_one_off_config()
    return bool(cfg and e.get("event_id") in cfg["event_ids"])


def build_reminder_message_spec(
    e: dict, offset: int, allow_mention: bool = True
) -> dict | None:
    """Return a delivery spec for launchd and piggyback reminder paths."""
    reminder_ref = {
        "source_kind": calendar_db.EVENT_REMINDER_SOURCE_KIND,
        "source_ref": str(e.get("event_id") or ""),
    }
    cfg = _load_one_off_config()
    if cfg and e.get("event_id") in cfg["event_ids"]:
        if offset != cfg["offset"]:
            return None
        plain_text = validate_push_text(
            cfg["plain_text"], source="event_reminder"
        )
        if not plain_text.strip():
            return None
        if allow_mention and cfg["mention_text"] and cfg["user_id"]:
            mention_text = validate_push_text(
                cfg["mention_text"], source="event_reminder"
            )
            if not mention_text.strip():
                return None
            if mention_text != cfg["mention_text"]:
                return {"kind": "text", "text": mention_text, **reminder_ref}
            return {
                "kind": "mention",
                "text": mention_text,
                "placeholder": cfg["placeholder"],
                "user_id": cfg["user_id"],
                "fallback_text": plain_text,
                **reminder_ref,
            }
        if plain_text:
            return {"kind": "text", "text": plain_text, **reminder_ref}
        return None
    text = _format_event(e, offset)
    safe_text = validate_push_text(text, source="event_reminder")
    if safe_text != text:
        if not safe_text.strip():
            return None
        return {"kind": "text", "text": safe_text, **reminder_ref}
    targets = line_mentions.event_mention_targets(e)
    plain_labels = line_mentions.event_plain_labels(e)
    if targets and allow_mention:
        message = line_mentions.text_v2_dict(text, targets, plain_labels)
        fallback_text = validate_push_text(
            line_mentions.text_with_plain_mentions(text, targets, plain_labels),
            source="event_reminder",
        )
        if not fallback_text.strip():
            return None
        if fallback_text != line_mentions.text_with_plain_mentions(
            text, targets, plain_labels
        ):
            return {"kind": "text", "text": fallback_text, **reminder_ref}
        return {
            "kind": "textV2",
            "message": message,
            "text": message["text"],
            "fallback_text": fallback_text,
            **reminder_ref,
        }
    if plain_labels:
        plain_text = validate_push_text(
            line_mentions.text_with_plain_mentions(text, targets, plain_labels),
            source="event_reminder",
        )
        if not plain_text.strip():
            return None
        return {
            "kind": "text",
            "text": plain_text,
            **reminder_ref,
        }
    return {"kind": "text", "text": text, **reminder_ref}


def _send_reminder_message_spec(
    spec: dict, retry_key: str | None = None, fallback_retry_key: str | None = None
) -> str:
    reminder_ref = {
        "source_kind": str(spec.get("source_kind") or ""),
        "source_ref": str(spec.get("source_ref") or ""),
    }
    if spec.get("kind") == "textV2":
        result = _post_result(
            [spec["message"]],
            retry_key=retry_key,
            reminder_refs=[reminder_ref],
        )
        fallback_text = spec.get("fallback_text")
        if result == POST_REJECT and fallback_text:
            return _push_result(
                fallback_text,
                retry_key=fallback_retry_key,
                reminder_ref=reminder_ref,
            )
        return result
    if spec.get("kind") == "mention":
        result = _push_mention_result(
            spec["text"],
            spec["placeholder"],
            spec["user_id"],
            retry_key=retry_key,
            reminder_ref=reminder_ref,
        )
        if result == POST_OK:
            return result
        fallback_text = spec.get("fallback_text")
        if result == POST_REJECT and fallback_text:
            return _push_result(
                fallback_text,
                retry_key=fallback_retry_key,
                reminder_ref=reminder_ref,
            )
        return result
    return _push_result(
        spec["text"],
        retry_key=retry_key,
        reminder_ref=reminder_ref,
    )


def sdk_message_from_spec(spec: dict):
    """Convert reminder delivery spec into LINE SDK message object for reply_token paths."""
    from linebot.v3.messaging import TextMessage  # type: ignore[import-untyped]

    if spec.get("kind") == "textV2":
        fallback_text = str(spec.get("fallback_text") or spec.get("text") or "")
        safe_text = validate_push_text(fallback_text, source="event_reminder_sdk")
        if not safe_text.strip():
            return None
        if safe_text != fallback_text:
            return TextMessage(text=safe_text[:5000])
        return line_mentions.sdk_message_from_text_v2_dict(spec["message"])
    if spec.get("kind") == "mention":
        fallback_text = str(spec.get("fallback_text") or spec.get("text") or "")
        safe_text = validate_push_text(fallback_text, source="event_reminder_sdk")
        if not safe_text.strip():
            return None
        if safe_text != fallback_text:
            return TextMessage(text=safe_text[:5000])
        message = {
            "type": "textV2",
            "text": spec["text"],
            "substitution": {
                spec["placeholder"]: {
                    "type": "mention",
                    "mentionee": {"type": "user", "userId": spec["user_id"]},
                }
            },
        }
        return line_mentions.sdk_message_from_text_v2_dict(message)
    safe_text = validate_push_text(
        str(spec.get("text") or ""), source="event_reminder_sdk"
    )
    if not safe_text.strip():
        return None
    return TextMessage(text=safe_text[:5000])


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
            spec = build_reminder_message_spec(e, offset, allow_mention=True)
            if spec is None:
                continue
            # Cancellation can race with list_due_for_reminder(). Recheck the
            # durable tombstone immediately before the external LINE request.
            try:
                import memory

                if memory.is_reminder_source_cancelled(
                    GROUP_ID,
                    calendar_db.EVENT_REMINDER_SOURCE_KIND,
                    str(e.get("event_id") or ""),
                ):
                    logger.info(
                        "skip cancelled event reminder before push: %s",
                        e.get("event_id"),
                    )
                    continue
            except Exception as guard_error:
                # Fail closed: an unavailable cancellation store must not risk
                # pushing something the user may just have cancelled.
                logger.warning(
                    "event reminder cancellation guard failed; skip %s: %s",
                    e.get("event_id"),
                    str(guard_error)[:160],
                )
                continue
            claim = memory.claim_calendar_reminder_delivery(
                GROUP_ID,
                calendar_db.EVENT_REMINDER_SOURCE_KIND,
                str(e.get("event_id") or ""),
                offset,
                expected_title=str(e.get("title") or ""),
                expected_event_date=str(e.get("event_date") or ""),
                expected_event_time=e.get("event_time"),
                expected_location=str(e.get("location") or ""),
                expected_participants=str(e.get("participants") or "[]"),
                transport="push",
            )
            if claim is None:
                logger.info(
                    "skip unclaimed event reminder before push: %s T-%d",
                    e.get("event_id"),
                    offset,
                )
                continue
            try:
                result = _send_reminder_message_spec(
                    spec,
                    retry_key=str(claim["retry_key"]),
                    fallback_retry_key=str(claim["fallback_retry_key"]),
                )
            except Exception:
                memory.release_reminder_delivery_claim(claim)
                raise
            if result == POST_OK:
                finalized = memory.finalize_calendar_reminder_delivery(claim)
                if not finalized:
                    logger.error(
                        "event delivery accepted but claim finalization failed "
                        "(T-%d, %s)",
                        offset,
                        e["event_id"],
                    )
                total_sent += 1
                logger.info(
                    "reminder sent (T-%d): %s '%s' on %s",
                    offset, e["event_id"], e["title"], e["event_date"],
                )
            else:
                memory.release_reminder_delivery_claim(claim)
                # push 失敗不 mark — 下次 launchd 跑會 retry（at-least-once 保證）
                logger.warning(
                    "reminder push failed (T-%d, %s): %s",
                    offset, result, e["event_id"]
                )

    logger.info("done: %d/%d reminders sent", total_sent, total_due)
    return 0


if __name__ == "__main__":
    sys.exit(main())
