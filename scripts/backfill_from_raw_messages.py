#!/usr/bin/env python3
"""一次性回掃 raw_messages，把歷史訊息轉成 reminders / events。

用途
- 重新把 line_bot.db 裡保存的 raw_messages 回填到 reminders 與 events。
- 回填完成後再把 active events 同步到 reminders，避免事件和提醒脫節。
- 可選擇直接把已過期 pending reminders 標記 expired。

執行方式：
    cd /Users/andrew/Desktop/andrew/Data_engineer/line_bot
    .venv/bin/python3.11 -m scripts.backfill_from_raw_messages

重點
- 以「訊息級 idempotency」防重：同一則訊息對應到同一組事件/提醒，回掃時可重複執行。
- 不會讀 pending_reminder_extract；它專門給「drain 後向前補抽」使用。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


# 讓腳本可直接從 line_bot/ 目錄外執行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calendar_db
import calendar_regex
import memory
from config import settings


_REMINDER_DATE_HINT = re.compile(
    r"(\d+\s*月\s*\d+|\d+/\d+|\d+號|\d+日|"
    r"今天|今晚|明天|明日|明晚|後天|大後天|"
    r"(?:星期|週|周|禮拜)[一二三四五六日天]|"
    r"下\s*(週|周|星期|月)|這\s*(週|周|星期))"
)


def _normalize_text(raw: str | None, limit: int = 500) -> str:
    if not raw:
        return ""
    return str(raw).strip()[:limit]


def _iter_raw_messages(
    db_path: str,
    group_id: str | None = None,
    since_ts: int | None = None,
    limit: int = 0,
):
    """Yield raw message rows as (group_id, message_id, user_id, text, created_at)."""
    sql = (
        "SELECT group_id, message_id, user_id, text, created_at "
        "FROM raw_messages WHERE (user_id IS NULL OR user_id != '__bot__')"
    )
    params: list = []
    if group_id:
        sql += " AND group_id = ?"
        params.append(group_id)
    if since_ts is not None:
        sql += " AND created_at >= ?"
        params.append(int(since_ts))
    sql += " ORDER BY created_at ASC"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        for row in rows:
            yield (
                row[0],
                row[1],
                row[2],
                row[3],
                int(row[4]),
            )
    finally:
        conn.close()


def _calendar_regex_to_reminder_result(
    text: str,
    created_at_ts: int,
) -> tuple[dict | None, bool]:
    """用 calendar regex 回收日程類提醒；回傳 (result, parsed)

    說明：reminder 仍採 date hint gating，時間可空。若缺時間，預設為 00:00（用於 date-only 事件提醒）。
    """
    if not _REMINDER_DATE_HINT.search(text):
        return None, False

    today = datetime.fromtimestamp(created_at_ts).date()
    events = calendar_regex.extract_many_regex_only(text, today, require_time=False)
    if not events:
        return None, False

    # 僅取第一筆（reminder 是單筆存）並補齊 title / time fallback
    data = events[0]
    if not (data.get("has_event") and data.get("date") and data.get("title")):
        return None, False

    try:
        year_s, month_s, day_s = str(data["date"]).split("-", 2)
        time_val = data.get("time")
        if time_val:
            hour_s, minute_s = str(time_val).split(":", 1)
        else:
            hour_s, minute_s = "0", "0"
    except (TypeError, ValueError):
        return None, False

    action = _normalize_text(data.get("title"), limit=80)
    if not action:
        return None, False

    return {
        "action": action,
        "year": int(year_s),
        "month": int(month_s),
        "day": int(day_s),
        "hour": int(hour_s),
        "minute": int(minute_s),
    }, True


def _extract_reminder_from_text(
    text: str,
    created_at_ts: int,
) -> tuple[dict | None, bool]:
    """目前僅用 calendar_regex，回傳 (result, parsed)；parsed=False 代表未命中。"""
    return _calendar_regex_to_reminder_result(text, created_at_ts)


def _to_remind_at(record: dict, now_ts: int) -> int | None:
    try:
        dt = datetime(
            int(record["year"]), int(record["month"]), int(record["day"]),
            int(record["hour"]), int(record["minute"]),
        )
    except (TypeError, ValueError):
        return None
    remind_at = int(dt.timestamp())
    if remind_at < now_ts - 3600:
        return None
    return remind_at


def _is_event_row(text: str, created_at_ts: int) -> tuple[bool, list[dict]]:
    today = datetime.fromtimestamp(created_at_ts).date()
    events = calendar_regex.extract_many_regex_only(text, today, require_time=False)
    if not events:
        return False, []
    return True, events


def _upsert_event_from_match(group_id: str, message_id: str, item: dict) -> tuple[bool, bool]:
    """回傳 (inserted, parsed)；parsed=True 代表有有效事件能寫入。"""
    title = _normalize_text(item.get("title"), limit=120)
    event_date = str(item.get("date") or "").strip()
    if not (title and event_date):
        return False, False

    event_time = _normalize_text(item.get("time"), limit=8) or None
    location = _normalize_text(item.get("location"), limit=120) or None
    participants = item.get("participants") or []
    if not isinstance(participants, list):
        participants = []
    participants = [str(p).strip() for p in participants if str(p).strip()]
    event_type = str(item.get("event_type") or "family_gathering")

    eid = calendar_db.insert_event(
        group_id=group_id,
        title=title,
        event_date=event_date,
        event_time=event_time,
        location=location,
        participants=participants,
        source_msg_id=message_id,
        event_type=event_type,
    )
    return (bool(eid), True)


def run_backfill(
    *,
    db_path: str,
    group_id: str | None = None,
    since_ts: int | None = None,
    limit: int = 0,
    sync_events: bool = True,
    backfill_reminders: bool = True,
    skip_event_gemini: bool = True,
    expire_overdue: bool = False,
):
    del skip_event_gemini  # reserved for future (目前保留)；目前全程 regex-only

    stats = {
        "rows_scanned": 0,
        "event_msgs": 0,
        "event_rows_inserted": 0,
        "event_rows_deduped": 0,
        "reminder_candidates": 0,
        "reminders_inserted": 0,
        "reminders_skipped": 0,
    }

    now_ts = int(time.time())

    for gid, mid, uid, text, created_at in _iter_raw_messages(
        db_path, group_id=group_id, since_ts=since_ts, limit=limit,
    ):
        text = _normalize_text(text, limit=2000)
        if not text:
            continue

        stats["rows_scanned"] += 1

        # 先回補 events（含多事件）
        event_inserted = 0
        event_dedup = 0
        parsed_events = []
        if sync_events and _REMINDER_DATE_HINT.search(text):
            parsed, parsed_events = _is_event_row(text, created_at)
            if parsed:
                stats["event_msgs"] += 1
                for ev in parsed_events:
                    inserted, was_parsed = _upsert_event_from_match(gid, mid, ev)
                    if not was_parsed:
                        continue
                    if inserted:
                        event_inserted += 1
                    else:
                        event_dedup += 1
                stats["event_rows_inserted"] += event_inserted
                stats["event_rows_deduped"] += event_dedup

        # 再補 reminder（排除剛剛命中的 event message，避免事件 message 重複加提醒來源）
        if backfill_reminders and not parsed_events:
            reminder, parsed = _extract_reminder_from_text(text, created_at)
            if parsed:
                stats["reminder_candidates"] += 1
                remind_at = _to_remind_at(reminder, now_ts)
                if remind_at is None:
                    stats["reminders_skipped"] += 1
                else:
                    rid = memory.add_reminder(
                        gid, uid or "", reminder["action"], remind_at,
                        source_text=text[:200],
                    )
                    if rid:
                        stats["reminders_inserted"] += 1
                    else:
                        stats["reminders_skipped"] += 1

    # 事件與提醒同步：補齊所有 active events 對應 reminders
    synced_events = 0
    if sync_events:
        synced_events = calendar_db.sync_active_events_to_reminders(group_id)

    # 過期提醒可直接標記 expired（直接「刪除」指示）
    expired = 0
    if expire_overdue:
        expired = memory.expire_old_reminders(0)

    # 剩餘 pending reminders：用於回報
    if group_id:
        remaining = memory.list_pending_reminders(group_id=group_id)
    else:
        remaining = memory.list_pending_reminders()

    return {
        "stats": stats,
        "synced_events": synced_events,
        "expired": expired,
        "remaining_reminders": remaining,
    }


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill reminders/events from raw_messages",
    )
    parser.add_argument(
        "--group-id",
        dest="group_id",
        default=None,
        help="只處理指定群組；未指定則處理所有群組（回報會更完整）",
    )
    parser.add_argument(
        "--since-seconds-ago",
        type=int,
        default=0,
        help="只掃 this 秒前之後的訊息（0=全部）",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="只掃前 N 筆 raw_messages（0=全部）"
    )
    parser.add_argument(
        "--skip-events",
        action="store_true",
        help="不要回補 events，僅回補 reminders",
    )
    parser.add_argument(
        "--skip-reminders",
        action="store_true",
        help="不要回補 reminders，僅回補 events",
    )
    parser.add_argument(
        "--expire-overdue",
        action="store_true",
        help="把提醒時間早於現在的 pending reminders mark expired",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出會處理/會回補的數量，不實際寫入 DB",
    )

    args = parser.parse_args()

    since_ts = None
    if args.since_seconds_ago and args.since_seconds_ago > 0:
        since_ts = int(time.time()) - int(args.since_seconds_ago)

    print(f"DB: {settings.sqlite_path if not args.dry_run else settings.sqlite_path} (read-only if dry-run)")
    if args.dry_run:
        # 不實作 DB 重放；只用 SQL 統計，避免影響資料
        # 先做一輪 dry-run 檢查，不實際 insert
        print("Dry-run mode: no writes")

    if args.dry_run:
        # 直接統計會影響到的 raw rows / 匹配數
        scanned = 0
        event_msgs = 0
        reminder_candidates = 0
        for gid, mid, uid, text, created_at in _iter_raw_messages(
            settings.sqlite_path, group_id=args.group_id, since_ts=since_ts, limit=args.limit,
        ):
            del gid, mid, uid  # keep linter happy in static-only path
            text = _normalize_text(text, 2000)
            if not text:
                continue
            scanned += 1
            if _REMINDER_DATE_HINT.search(text):
                parsed, evs = _is_event_row(text, created_at)
                if parsed:
                    event_msgs += 1
                remind, parsed_r = _extract_reminder_from_text(text, created_at)
                if parsed_r and remind:
                    reminder_candidates += 1
        print("Dry-run results:")
        print(f"  scanned rows: {scanned}")
        print(f"  event-message matches: {event_msgs}")
        print(f"  reminder-candidate matches: {reminder_candidates}")
        return 0

    out = run_backfill(
        db_path=settings.sqlite_path,
        group_id=args.group_id,
        since_ts=since_ts,
        limit=args.limit,
        sync_events=not args.skip_events,
        backfill_reminders=not args.skip_reminders,
        expire_overdue=args.expire_overdue,
    )

    print("Backfill finished")
    for k, v in out["stats"].items():
        print(f"  {k}: {v}")
    print(f"  synced_events: {out['synced_events']}")
    print(f"  expired_reminders: {out['expired']}")
    print()

    remaining = out["remaining_reminders"]
    print(f"剩餘 pending 提醒（{len(remaining)} 筆）")
    for item in remaining:
        print(
            f"- {item['group_id']} | {_format_ts(item['remind_at'])} | "
            f"{item.get('action','')} | source_kind={item.get('source_kind','')} "
            f"source_ref={item.get('source_ref','')}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
