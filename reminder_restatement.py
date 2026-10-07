"""Source-bound generic reminder corrections and lossless preparation details."""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import memory

TW = ZoneInfo("Asia/Taipei")
OLD_KIND = "restated_generic_old"


def preserve_transit_details(text: str, result: dict) -> dict:
    """Keep linked transit legs as content without changing schedule fields.

    This enriches an accepted extraction; it never grants reminder intent.
    The complete source keeps connecting clocks, routes, intervals and negation.
    """
    import reminder_intent

    source = text.strip()
    clocks = set(re.findall(r"(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?!\d)", source))
    if (
        not source
        or len(source) > 500
        or len(clocks) < 2
        or not re.search(
            r"(?:銜接|轉乘|轉搭|換乘|接)\s*(?:\d|公車|客運|高鐵|火車|列車|捷運)", source
        )
        or not re.search(r"公車|客運|高鐵|火車|列車|捷運|巴士|國道.{0,8}線", source)
        or re.search(r"[?？]|如果|假如|可能|也許", source)
        or reminder_intent.is_obvious_noncommittal_source(source)
    ):
        return result
    return dict(result, action=source)


def complete_result(text: str, result: dict, today: date) -> dict:
    """Keep explicit preparation clauses; compute one weekday without a model."""
    result = dict(result)
    weekdays = list(
        re.finditer(
            r"(?P<week>下下|下|本|這)?(?:個)?(?:星期|週|周|禮拜)([一二三四五六日天])",
            text,
        )
    )
    if len(weekdays) == 1 and not re.search(
        r"\d+月\d+|\d+/\d+|\d{4}-\d+|[\d一二三四五六七八九十]+[日號]|今天|明天|後天|明日|今晚|明晚",
        text,
    ):
        match = weekdays[0]
        index = "一二三四五六日天".index(match.group(2))
        index = min(index, 6)
        prefix = match.group("week")
        if prefix:
            offset = {"下下": 14, "下": 7, "本": 0, "這": 0}[prefix]
            target = (
                today - timedelta(days=today.weekday()) + timedelta(days=offset + index)
            )
        else:
            target = today + timedelta(days=(index - today.weekday()) % 7)
        result.update(year=target.year, month=target.month, day=target.day)
    clause = re.search(r"[^，,。；;\n]*(?:需要|必須|記得|要|需)帶[^。；;\n]*", text)
    if clause and clause[0] not in str(result.get("action", "")):
        result["action"] = f"{result.get('action', '')}，{clause[0]}"
    return preserve_transit_details(text, result)


def reconcile(
    target: dict,
    cancelled: list[dict],
    *,
    action: str,
    remind_at: int,
    source_text: str,
    keep_old_source: bool = False,
) -> str:
    """CAS one target plus exact obsolete rows; never send or broaden identity."""
    group, user = target["group_id"], target["user_id"]
    snapshots = [target, *cancelled]
    if len({r["reminder_id"] for r in snapshots}) != len(snapshots):
        return "conflict"
    with memory._lock, memory._conn() as c:
        c.execute("BEGIN IMMEDIATE")
        for expected in snapshots:
            row = c.execute(
                "SELECT * FROM reminders WHERE reminder_id=?",
                (expected["reminder_id"],),
            ).fetchone()
            if row is None:
                return "conflict"
            names = [r[1] for r in c.execute("PRAGMA table_info(reminders)")]
            live = dict(zip(names, row))
            if any(live[k] != expected[k] for k in names if k in expected):
                return "conflict"
            if (
                live["group_id"] != group
                or live["user_id"] != user
                or live["status"] != "pending"
                or live["source_kind"]
                or live["source_ref"]
            ):
                return "conflict"
            if (
                memory._semantic_delivery_claim_conn(
                    c,
                    group_id=group,
                    reminder_id=live["reminder_id"],
                    action=live["action"],
                    remind_at=live["remind_at"],
                )
                is not None
            ):
                return "busy"
            queued = c.execute(
                "SELECT 1 FROM pending_reminder_extract WHERE group_id=? "
                "AND user_id=? AND text=? AND status='processing'",
                (group, user, live["source_text"]),
            ).fetchone()
            if queued:
                return "busy"
            outbox = c.execute(
                "SELECT 1 FROM reminder_confirmation_outbox AS o WHERE o.group_id=? "
                "AND o.status IN ('pending','processing') AND (o.source_ref IN ("
                "SELECT message_id FROM raw_messages WHERE group_id=? AND user_id=? AND text=?) "
                "OR o.source_ref IN (SELECT 'pending_reminder:' || pending_id FROM pending_reminder_extract "
                "WHERE group_id=? AND user_id=? AND text=?)) LIMIT 1",
                (
                    group,
                    group,
                    user,
                    live["source_text"],
                    group,
                    user,
                    live["source_text"],
                ),
            ).fetchone()
            if outbox:
                return "busy"
        if (
            not cancelled
            and target["action"] == action
            and target["remind_at"] == remind_at
            and target["source_text"] == source_text
        ):
            return "unchanged"
        if keep_old_source and target["source_text"] != source_text:
            c.execute(
                "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,status,"
                "source_text,source_kind,source_ref) VALUES(?,?,?,?,?,'cancelled',?,?,?)",
                (
                    group,
                    user,
                    target["action"],
                    target["remind_at"],
                    int(time.time()),
                    target["source_text"],
                    OLD_KIND,
                    str(target["reminder_id"]),
                ),
            )
        for old in cancelled:
            c.execute(
                "UPDATE reminders SET status='cancelled',source_kind=?,source_ref=? WHERE reminder_id=?",
                (OLD_KIND, str(target["reminder_id"]), old["reminder_id"]),
            )
        for old in snapshots:
            c.execute(
                "UPDATE pending_reminder_extract SET status='dropped',claimed_at=0,claim_token='' "
                "WHERE group_id=? AND user_id=? AND text=? AND status='pending'",
                (group, user, old["source_text"]),
            )
        reset = ""
        if target["remind_at"] != remind_at:
            reset = ",last_pushed_at=0,weekly_count=0,last_weekly_at=0," + ",".join(
                f"{key}=0"
                for key in memory._REMINDER_PUSH_FLAG_COLUMNS
                if key not in {"last_pushed_at", "weekly_count", "last_weekly_at"}
            )
        c.execute(
            "UPDATE reminders SET action=?,remind_at=?,source_text=?,time_kind='clock',"
            "merged_details='[]'"
            + reset
            + " WHERE reminder_id=?",
            (action, remind_at, source_text, target["reminder_id"]),
        )
        return "updated"


def correction(
    text: str, group: str, user: str, message_id: str, quoted_id: str = ""
) -> dict | None:
    """Only a single affirmative dated restatement with provable human context."""
    if not user or user == "__bot__" or not message_id or len(text) > 500:
        return None
    if not re.match(r"^\s*(?:我是|其實我是|正確是|應該是)\d", text):
        return None
    if re.search(r"[?？]|如果|可能|不確定|不是|不要|取消|另外|再一次", text):
        return None
    import calendar_regex

    now = int(time.time())
    with memory._conn() as c:
        current = c.execute(
            "SELECT created_at,user_id FROM raw_messages WHERE group_id=? AND message_id=?",
            (group, message_id),
        ).fetchone()
        if not current or current[1] != user or not 0 <= now - current[0] <= 3600:
            return None
        dates = list(re.finditer(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日號]?", text))
        place = re.search(r"在([^，,。；;\n]{3,50})(?:[，,]|$)", text)
        clock = calendar_regex._first_time_in_text(text)
        if (
            len(dates) != 1
            or place is None
            or not clock
            or re.search(r"\d+[/\-]\d+|今天|明天|後天", text)
            or len(list(calendar_regex._TIME_IN_TEXT.finditer(text))) != 1
            or calendar_regex._TIME_RANGE_IN_TEXT.search(text)
        ):
            return None
        match = dates[0]
        try:
            year = int(match[1] or datetime.fromtimestamp(current[0], TW).year)
            target_date = date(year, int(match[2]), int(match[3]))
            stamp = int(
                datetime.fromisoformat(f"{target_date}T{clock}")
                .replace(tzinfo=TW)
                .timestamp()
            )
        except ValueError:
            return None
        location = place[1]
        if stamp <= now:
            return None

        def norm(value: str) -> str:
            return re.sub(r"飯店|酒店|[\s，,。]", "", value)

        rows = c.execute(
            "SELECT * FROM reminders WHERE group_id=? AND user_id=? AND status='pending' "
            "AND source_kind='' AND source_ref=''",
            (group, user),
        ).fetchall()
        names = [r[1] for r in c.execute("PRAGMA table_info(reminders)")]
        candidates = []
        matching_place = False
        for row in rows:
            candidate = dict(zip(names, row))
            if norm(location) not in norm(candidate["source_text"]):
                continue
            matching_place = True
            if (
                datetime.fromtimestamp(candidate["remind_at"], TW).strftime("%H:%M")
                != clock
            ):
                continue
            sources = c.execute(
                "SELECT message_id,created_at FROM raw_messages WHERE group_id=? AND user_id=? AND text=?",
                (group, user, candidate["source_text"]),
            ).fetchall()
            if not any(
                (s[0] == quoted_id if quoted_id else 0 <= current[0] - s[1] <= 3600)
                for s in sources
            ):
                continue
            candidates.append(candidate)
    if not candidates:
        return {"status": "ambiguous"} if quoted_id or matching_place else None
    if len(candidates) != 1:
        return {"status": "ambiguous"}
    target = candidates[0]
    # Preserve the source body verbatim, including all post-comma requirements.
    body = re.sub(r"^\s*(?:我是|其實我是|正確是|應該是)", "", text)
    status = reconcile(
        target, [], action=body, remind_at=stamp, source_text=text, keep_old_source=True
    )
    return {"status": status, "reminder_id": target["reminder_id"]}
