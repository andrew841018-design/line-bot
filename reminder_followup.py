"""Bounded reminder creation follow-ups; never interpret bot prose as a task."""

from __future__ import annotations

import re
import time

import memory
import reminder_intent


SOURCE_KIND = "contextual_create_once"
_WEEKEND = re.compile(r"(?:(?:這個|這|本|下個|下)?[週周]末)")
_EXACT_DATE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}|\d{1,2}/\d{1,2}|"
    r"今天|今晚|明天|明晚|明日|後天|大後天|"
    r"(?:星期|週|周|禮拜)[一二三四五六日天]"
)
_FOLLOWUP = re.compile(
    r"(?:(?:@?咪寶)[，,:：\s]*)?"
    r"(?:(?:這(?:個|則)?|那(?:個|則)?)?不是閒聊[，,。\s]*)?"
    r"(?:請|麻煩)?\s*(?:幫我)?\s*"
    r"(?:把(?:這|那)(?:個|則|件事)?\s*)?"
    r"(?:加入|加到|新增|建立)\s*(?:到)?\s*提醒(?:事項|清單)?"
    r"[。！!\s]*"
)
_UNCOMMITTED = re.compile(
    r"上(?:個)?[週周]末|昨天|前天|已經|整理完|整理好了|"
    r"可能|也許|如果|假如|不一定|打算看看|"
    r"(?:他|她|有人|朋友).{0,8}(?:說|提到)|[「『\"]|"
    r"不要|不用|不必|別整理|不(?:會|打算|想|要)?整理|沒有整理|曾經"
)


def is_creation_followup(text: str) -> bool:
    return bool(_FOLLOWUP.fullmatch(reminder_intent.normalize_text(text)))


def has_weekend(text: str) -> bool:
    """Weekend needs a date only when it precedes any concrete schedule.

    In '明天確認週末行程', weekend belongs to the task payload. A date
    mentioned after weekend may itself be payload, so keep that case unclear.
    """
    value = reminder_intent.normalize_text(text)
    weekend = _WEEKEND.search(value)
    if weekend is None:
        return False
    date = _EXACT_DATE.search(value)
    return date is None or weekend.start() < date.start()


def is_weekend_activity(text: str) -> bool:
    """A narrow affirmative housekeeping statement, not every weekend chat."""
    value = reminder_intent.normalize_text(text)
    return bool(
        len(value) <= 500
        and has_weekend(value)
        and re.search(r"整理\s*\S+", value)
        and not _UNCOMMITTED.search(value)
        and not reminder_intent.is_obvious_noncommittal_source(value)
        and not re.search(r"嗎|呢|[?？]|怎麼|如何|要不要|哪", value)
    )


def source_is_safe(text: str) -> bool:
    return bool(
        text
        and len(text) <= 500
        and not _UNCOMMITTED.search(text)
        and not reminder_intent.is_obvious_noncommittal_source(text)
        and not is_creation_followup(text)
    )


def clarification(source_text: str = "") -> str:
    if not source_text:
        return (
            "尚未新增提醒：無法唯一確認原本的事項。請傳送「提醒我＋完整日期＋事項」。"
        )
    if has_weekend(source_text):
        return (
            f"尚未新增提醒。事項：{source_text}\n"
            "「週末」尚未確定是哪一天，請指定週六或週日的完整日期。\n"
            "請傳送「提醒我＋完整日期＋事項」；未指定時間會依既有規則預設中午12點。"
        )
    return f"尚未新增提醒。事項：{source_text}\n請傳送「提醒我＋完整日期＋事項」，以確認提醒日期。"


def resolve_source(
    group_id: str,
    user_id: str,
    current_id: str,
    quoted_id: str = "",
    *,
    max_age: int = 180,
) -> dict | None:
    """Accept an exact human quote or an adjacent human across one bot reply.

    Bot quotes must be the immediately preceding row. Unquoted follow-ups use
    the same strict adjacency; no scanning past other people or bot chatter.
    """
    if not group_id or not user_id or not current_id or user_id == "__bot__":
        return None
    with memory._conn() as c:
        current = c.execute(
            "SELECT rowid,user_id,created_at FROM raw_messages WHERE group_id=? AND message_id=?",
            (group_id, current_id),
        ).fetchone()
        if current is None or current[1] != user_id:
            return None
        previous = c.execute(
            "SELECT rowid,message_id,user_id,text,created_at FROM raw_messages "
            "WHERE group_id=? AND (created_at<? OR (created_at=? AND rowid<?)) "
            "ORDER BY created_at DESC,rowid DESC LIMIT 3",
            (group_id, current[2], current[2], current[0]),
        ).fetchall()
        inferred = True
        if quoted_id:
            quoted = c.execute(
                "SELECT rowid,message_id,user_id,text,created_at FROM raw_messages "
                "WHERE group_id=? AND message_id=?",
                (group_id, quoted_id),
            ).fetchone()
            if quoted is None or (quoted[4], quoted[0]) >= (current[2], current[0]):
                return None
            if quoted[2] != "__bot__":
                source = quoted
                inferred = False
            else:
                if not previous or previous[0][1] != quoted_id:
                    return None
                source = previous[1] if len(previous) > 1 else None
        else:
            if not previous:
                return None
            source = previous[0]
            if source[2] == "__bot__":
                source = previous[1] if len(previous) > 1 else None
        if source is None or source[2] != user_id:
            return None
        if inferred:
            source_index = next(
                i for i, row in enumerate(previous) if row[1] == source[1]
            )
            older = previous[source_index + 1 :]
            if (
                older
                and older[0][2] != "__bot__"
                and int(current[2]) - int(older[0][4]) <= max_age
            ):
                return None
        if not 0 <= int(current[2]) - int(source[4]) <= max_age:
            return None
        if not 0 <= time.time() - int(current[2]) <= max_age:
            return None
    return {
        "message_id": source[1],
        "user_id": source[2],
        "text": source[3],
        "created_at": int(source[4]),
    }


def persist_source(
    group_id: str, source: dict, result: dict, remind_at: int
) -> tuple[int | None, str]:
    """One atomic source write; preserve terminal states and pending workers."""
    import json

    source = dict(source, text=memory._normalize_reminder_text(source["text"]))
    with memory._lock, memory._conn() as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT reminder_id,status FROM reminders WHERE group_id=? AND user_id=? "
            "AND ((source_kind=? AND source_ref=?) OR (source_text=? AND remind_at=? AND action=?)) "
            "ORDER BY CASE WHEN status='pending' THEN 1 ELSE 0 END,reminder_id LIMIT 1",
            (
                group_id,
                source["user_id"],
                SOURCE_KIND,
                source["message_id"],
                source["text"],
                remind_at,
                result["action"],
            ),
        ).fetchone()
        if existing:
            return int(existing[0]), "duplicate" if existing[
                1
            ] == "pending" else "inactive"
        events = c.execute(
            "SELECT event_id,status FROM events WHERE group_id=? AND source_msg_id=?",
            (group_id, source["message_id"]),
        ).fetchall()
        if events:
            if len(events) == 1 and events[0][1] == "active":
                mirror = c.execute(
                    "SELECT reminder_id,status FROM reminders WHERE group_id=? "
                    "AND source_kind='calendar_event' AND source_ref=?",
                    (group_id, events[0][0]),
                ).fetchone()
                if mirror and mirror[1] == "pending":
                    return int(mirror[0]), "duplicate"
            return None, "inactive"
        queued = c.execute(
            "SELECT pending_id,status FROM pending_reminder_extract "
            "WHERE group_id=? AND message_id=?",
            (group_id, source["message_id"]),
        ).fetchone()
        if queued and queued[1] == "processing":
            return None, "queued"  # a drain worker owns it right now
        if queued and queued[1] != "pending":
            return None, "inactive"
        if remind_at <= time.time():
            return None, "expired"
        if queued:
            # The source waits in the queue: this write settles it, in the same
            # transaction, so the later drain cannot add a second reminder.
            closed = c.execute(
                "UPDATE pending_reminder_extract SET status='done',claimed_at=0,"
                "claim_token='' WHERE pending_id=? AND status='pending'",
                (int(queued[0]),),
            )
            if closed.rowcount != 1:
                raise RuntimeError("queued follow-up source changed state")
        cursor = c.execute(
            "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
            "source_kind,source_ref,source_text,mention_aliases) VALUES (?,?,?,?,?,'pending',?,?,?,?)",
            (
                group_id,
                source["user_id"],
                result["action"],
                remind_at,
                int(time.time()),
                SOURCE_KIND,
                source["message_id"],
                source["text"],
                json.dumps(result.get("mention_aliases") or [], ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid), "created"
