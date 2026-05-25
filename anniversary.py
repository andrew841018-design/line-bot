"""Anniversary extractor + reminder — 家族永久紀念日.

- 歷史 sweep: 一次性 read 全 raw_messages，Gemini 抽生日 / 結婚 / 紀念日
- 增量 sweep: 每月 1 號 launchd 跑 last 30d
- D-7 / D-1 / D-day 推播 reminder，dedup by year

Andrew 2026-05-25 directive (Feature D).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402

from config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_TW = ZoneInfo("Asia/Taipei")
_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()

GROUP_ID = os.environ.get("LINE_ALLOWED_GROUP_ID") or os.environ.get(
    "ALLOWED_GROUP_ID", ""
)
_PUSH_URL = "https://api.line.me/v2/bot/message/push"

ANNIVERSARY_TYPES: tuple[str, ...] = ("birthday", "wedding", "memorial", "other")
REMINDER_OFFSETS: tuple[int, ...] = (7, 1, 0)  # D-7 / D-1 / D-day


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH, isolation_level=None, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS anniversaries (
                anniversary_id     TEXT PRIMARY KEY,
                group_id           TEXT NOT NULL,
                person             TEXT NOT NULL,
                anniversary_type   TEXT NOT NULL,
                month              INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
                day                INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31),
                source_msg_id      TEXT,
                source_text        TEXT,
                created_at         INTEGER NOT NULL
            )
            """
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_anniv_dedup "
            "ON anniversaries(group_id, person, anniversary_type, month, day)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_anniv_month_day "
            "ON anniversaries(group_id, month, day)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS anniversary_reminders_sent (
                anniversary_id  TEXT NOT NULL,
                year            INTEGER NOT NULL,
                offset_days     INTEGER NOT NULL,
                sent_at         INTEGER NOT NULL,
                PRIMARY KEY (anniversary_id, year, offset_days)
            )
            """
        )


def insert_anniversary(
    group_id: str,
    person: str,
    anniversary_type: str,
    month: int,
    day: int,
    source_msg_id: str | None = None,
    source_text: str | None = None,
) -> str:
    """Insert OR IGNORE — 同 group+person+type+month+day dedup。Returns id or '' if dedup."""
    if anniversary_type not in ANNIVERSARY_TYPES:
        anniversary_type = "other"
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    aid = uuid.uuid4().hex
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO anniversaries "
            "(anniversary_id, group_id, person, anniversary_type, month, day, "
            "source_msg_id, source_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                aid, group_id, person, anniversary_type, month, day,
                source_msg_id, source_text, int(time.time() * 1000),
            ),
        )
        return aid if cur.rowcount > 0 else ""


def get_anniversaries_on(
    group_id: str, month: int, day: int
) -> list[dict]:
    """Get all anniversaries falling on month/day."""
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM anniversaries WHERE group_id = ? AND month = ? AND day = ?",
            (group_id, month, day),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_reminded(anniversary_id: str, year: int, offset_days: int) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO anniversary_reminders_sent "
            "(anniversary_id, year, offset_days, sent_at) VALUES (?, ?, ?, ?)",
            (anniversary_id, year, offset_days, int(time.time() * 1000)),
        )


def already_reminded(anniversary_id: str, year: int, offset_days: int) -> bool:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT 1 FROM anniversary_reminders_sent "
            "WHERE anniversary_id = ? AND year = ? AND offset_days = ?",
            (anniversary_id, year, offset_days),
        ).fetchone()
    return row is not None


def extract_from_text(text: str) -> list[dict]:
    """Gemini 從文字抽 anniversaries. Returns list of dict (may be empty).

    Each dict: {person, anniversary_type, month, day}
    """
    if not text or not text.strip():
        return []
    from google.genai import types
    import gemini_client

    prompt = (
        "從以下訊息抽家族紀念日（生日/結婚/忌日/其他）。\n"
        "**不要抓**：純閒聊、計劃、商業活動。只抓真實永久紀念日。\n\n"
        "回 JSON list（無就回 []）:\n"
        '[{"person": "媽媽", "anniversary_type": "birthday", "month": 6, "day": 4}, ...]\n\n'
        "anniversary_type 限 'birthday' / 'wedding' / 'memorial' / 'other'\n"
        "person 用稱謂或人名（媽媽 / 黃將修 / 妹妹）\n\n"
        f"【訊息】\n{text[:1500]}\n"
    )
    try:
        resp = gemini_client._client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                temperature=0.1,
            ),
        )
        raw = (resp.text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            person = str(item.get("person", "")).strip()[:30]
            atype = item.get("anniversary_type", "other")
            if atype not in ANNIVERSARY_TYPES:
                atype = "other"
            try:
                month = int(item.get("month", 0))
                day = int(item.get("day", 0))
            except (TypeError, ValueError):
                continue
            if not (person and 1 <= month <= 12 and 1 <= day <= 31):
                continue
            out.append({
                "person": person, "anniversary_type": atype,
                "month": month, "day": day,
            })
        return out
    except Exception as e:
        logger.warning("anniversary extract failed: %s", e)
        return []


def run_sweep(group_id: str, since_days: int = 0) -> int:
    """
    Sweep raw_messages 抓 anniversaries.

    since_days=0 → 全歷史 sweep (one-shot)
    since_days=N → last N days only (incremental cron)

    Returns count of new anniversaries inserted.
    """
    import memory
    where = "group_id = ?"
    params: list = [group_id]
    if since_days > 0:
        since_ts = int((time.time() - since_days * 24 * 3600) * 1000)
        where += " AND created_at >= ?"
        params.append(since_ts)

    with memory._conn() as c:
        rows = c.execute(
            f"SELECT message_id, text FROM raw_messages WHERE {where} "
            f"ORDER BY created_at ASC",
            params,
        ).fetchall()

    n_new = 0
    for msg_id, text in rows:
        if not text or len(text) < 5:
            continue
        extracted = extract_from_text(text)
        for ann in extracted:
            aid = insert_anniversary(
                group_id=group_id,
                person=ann["person"],
                anniversary_type=ann["anniversary_type"],
                month=ann["month"],
                day=ann["day"],
                source_msg_id=msg_id,
                source_text=text[:500],
            )
            if aid:
                n_new += 1
                logger.info(
                    "new anniversary: %s %s/%s (%s)",
                    ann["person"], ann["month"], ann["day"], aid,
                )
    return n_new


def _get_token() -> str:
    try:
        import line_token_refresh
        return line_token_refresh.get_line_token() or os.environ.get(
            "LINE_CHANNEL_ACCESS_TOKEN", ""
        )
    except Exception:
        return os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def _push(text: str) -> bool:
    token = _get_token()
    if not token or not GROUP_ID:
        logger.error("missing TOKEN or GROUP_ID; skip push")
        return False
    try:
        resp = requests.post(
            _PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": GROUP_ID, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.warning("LINE push exception: %s", e)
        return False


_TYPE_LABEL = {
    "birthday": "生日 🎂",
    "wedding": "結婚紀念日 💍",
    "memorial": "忌日 🕯️",
    "other": "紀念日",
}
_OFFSET_LABEL = {7: "1 週後", 1: "明天", 0: "今天"}


def _format_anniversary(ann: dict, offset: int) -> str:
    when = _OFFSET_LABEL.get(offset, f"{offset} 天後")
    type_label = _TYPE_LABEL.get(ann["anniversary_type"], "紀念日")
    return (
        f"🔔 {when}是 {ann['person']} 的{type_label}\n"
        f"📅 {ann['month']}/{ann['day']}"
    )


def push_today_reminders(group_id: str) -> int:
    """Daily launchd 跑：check D-7 / D-1 / D-day anniversaries → push."""
    today = datetime.now(_TW).date()
    year = today.year
    n_sent = 0
    for offset in REMINDER_OFFSETS:
        from datetime import timedelta
        target = today + timedelta(days=offset)
        anns = get_anniversaries_on(group_id, target.month, target.day)
        for ann in anns:
            if already_reminded(ann["anniversary_id"], year, offset):
                continue
            text = _format_anniversary(ann, offset)
            if _push(text):
                mark_reminded(ann["anniversary_id"], year, offset)
                n_sent += 1
                logger.info(
                    "anniversary reminder sent: %s (offset=%d)",
                    ann["person"], offset,
                )
    return n_sent


def main_extractor() -> int:
    """Launchd entrypoint: monthly incremental sweep."""
    if not GROUP_ID:
        logger.error("GROUP_ID 未設定")
        return 1
    init_db()
    n = run_sweep(GROUP_ID, since_days=30)
    logger.info("anniversary sweep done: %d new", n)
    return 0


def main_reminder() -> int:
    """Launchd entrypoint: daily reminder check."""
    if not GROUP_ID:
        logger.error("GROUP_ID 未設定")
        return 1
    init_db()
    n = push_today_reminders(GROUP_ID)
    logger.info("anniversary reminders sent: %d", n)
    return 0


# 允許 import 時自動建表
init_db()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "reminder"
    if mode == "extract":
        sys.exit(main_extractor())
    elif mode == "extract-historical":
        if not GROUP_ID:
            logger.error("GROUP_ID 未設定")
            sys.exit(1)
        init_db()
        n = run_sweep(GROUP_ID, since_days=0)
        logger.info("historical sweep done: %d new", n)
        sys.exit(0)
    else:
        sys.exit(main_reminder())
