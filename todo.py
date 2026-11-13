"""TODO list — 自動追蹤家族成員 first-person commitment.

- 抓「我下週要交報告」「我下個月開始減肥」等 first-person commitment
- 每天 launchd 推 due_today + overdue 提醒

Andrew 2026-05-25 directive (Feature E).
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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

from config import settings  # noqa: E402
from sqlite_security import connect_private_sqlite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_TW = ZoneInfo("Asia/Taipei")
_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()



def _get_target_group_ids() -> list[str]:
    """ALLOWED_GROUP_IDS (csv, multi-group) → list；legacy ALLOWED_GROUP_ID / LINE_ALLOWED_GROUP_ID
    → single-element list；都未設 → []. Inline helper（不 import settings）保留 launchd
    process-isolation：每個 cron job 獨立 env，pydantic-settings 失敗不會跨 script crash。"""
    raw = os.environ.get("ALLOWED_GROUP_IDS", "")
    if raw:
        seen: set[str] = set()
        out: list[str] = []
        for token in raw.split(","):
            gid = token.strip()
            if gid and gid not in seen:
                seen.add(gid)
                out.append(gid)
        return out
    single = (
        os.environ.get("LINE_ALLOWED_GROUP_ID", "")
        or os.environ.get("ALLOWED_GROUP_ID", "")
    ).strip()
    return [single] if single else []

TODO_STATUSES: tuple[str, ...] = ("pending", "completed", "cancelled")
MAX_OVERDUE_DAYS_TO_REMIND = 7  # 超過 7 天 overdue 不再提醒（避免 noise）


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _conn() -> sqlite3.Connection:
    c = connect_private_sqlite(
        _DB_PATH,
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                todo_id           TEXT PRIMARY KEY,
                group_id          TEXT NOT NULL,
                sender_user_id    TEXT,
                task              TEXT NOT NULL,
                due_date          TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                source_msg_id     TEXT,
                source_text       TEXT,
                created_at        INTEGER NOT NULL,
                completed_at      INTEGER,
                last_reminded_at  INTEGER
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_todos_due "
            "ON todos(group_id, status, due_date)"
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_dedup "
            "ON todos(group_id, sender_user_id, task, due_date) "
            "WHERE status='pending'"
        )


def insert_todo(
    group_id: str,
    task: str,
    sender_user_id: str | None = None,
    due_date: str | None = None,
    source_msg_id: str | None = None,
    source_text: str | None = None,
) -> str:
    """Insert OR IGNORE dedup by group+sender+task+due_date. Returns id or ''."""
    if not task or not task.strip():
        return ""
    tid = uuid.uuid4().hex
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO todos "
            "(todo_id, group_id, sender_user_id, task, due_date, status, "
            "source_msg_id, source_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                tid, group_id, sender_user_id, task.strip()[:200],
                due_date, source_msg_id,
                source_text[:500] if source_text else None,
                int(time.time() * 1000),
            ),
        )
        return tid if cur.rowcount > 0 else ""


def complete_todo(todo_id: str) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE todos SET status='completed', completed_at=? "
            "WHERE todo_id=? AND status='pending'",
            (int(time.time() * 1000), todo_id),
        )
        return cur.rowcount > 0


def cancel_todo(todo_id: str) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE todos SET status='cancelled' "
            "WHERE todo_id=? AND status='pending'",
            (todo_id,),
        )
        return cur.rowcount > 0


def list_due_today(group_id: str) -> list[dict]:
    """Due date is today (TW timezone)."""
    today_iso = datetime.now(_TW).date().isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM todos WHERE group_id=? AND status='pending' "
            "AND due_date=? ORDER BY created_at ASC",
            (group_id, today_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def list_overdue(group_id: str, max_days: int = MAX_OVERDUE_DAYS_TO_REMIND) -> list[dict]:
    """Overdue within last `max_days` days (avoid noise from very-old todos)."""
    today_d = datetime.now(_TW).date()
    today_iso = today_d.isoformat()
    cutoff_iso = (today_d - timedelta(days=max_days)).isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM todos WHERE group_id=? AND status='pending' "
            "AND due_date IS NOT NULL "
            "AND due_date < ? AND due_date >= ? "
            "ORDER BY due_date ASC",
            (group_id, today_iso, cutoff_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def list_pending(
    group_id: str,
    limit: int = 20,
    due_date: str | None = None,
) -> list[dict]:
    """List pending todos for immediate query replies."""
    limit = max(1, min(int(limit), 50))
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        if due_date:
            rows = c.execute(
                "SELECT * FROM todos WHERE group_id=? AND status='pending' "
                "AND due_date=? ORDER BY created_at ASC LIMIT ?",
                (group_id, due_date, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM todos WHERE group_id=? AND status='pending' "
                "ORDER BY due_date IS NULL, due_date ASC, created_at ASC LIMIT ?",
                (group_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def mark_reminded(todo_id: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE todos SET last_reminded_at=? WHERE todo_id=?",
            (int(time.time() * 1000), todo_id),
        )


def extract_from_text(text: str, today_iso: str | None = None) -> list[dict]:
    """Gemini 抽 first-person commitment todos.

    Returns list of {task, due_date (YYYY-MM-DD or null), sender_hint}.
    """
    if not text or not text.strip():
        return []
    from google.genai import types
    import gemini_client

    if today_iso is None:
        today_iso = datetime.now(_TW).date().isoformat()

    prompt = (
        "從以下訊息抽「first-person commitment」(我/自己 + 動詞 + 將/要/打算 + 任務)。\n"
        "**抓**：「我下週要交報告」「我下個月開始減肥」「我答應幫忙接送」\n"
        "**不抓**：純閒聊、別人的事、過去發生、純詢問、純感想\n\n"
        f"今天是 {today_iso}。模糊日期換算成 YYYY-MM-DD (下週=+7~13、下個月=+30 之類)\n\n"
        "回 JSON list（無就 []）:\n"
        '[{"task": "交報告", "due_date": "2026-06-01", "sender_hint": "我"}]\n\n'
        "task: 動詞+受詞 12 字內\n"
        "due_date: YYYY-MM-DD 或 null\n"
        "sender_hint: 「我」或具體稱謂\n\n"
        f"【訊息】\n{text[:1000]}\n"
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
            task = str(item.get("task", "")).strip()[:100]
            if not task:
                continue
            due = item.get("due_date")
            if due is not None:
                due = str(due).strip()
                try:
                    datetime.strptime(due, "%Y-%m-%d")
                except ValueError:
                    due = None
            sender = str(item.get("sender_hint", "")).strip()[:30]
            out.append({"task": task, "due_date": due, "sender_hint": sender})
        return out
    except Exception as e:
        logger.warning("todo extract failed: %s", e)
        return []


def _get_token() -> str:
    from line_push_client import line_access_token

    return line_access_token()


def _push(group_id: str, text: str, targets=(), *, retry_key: str) -> bool:
    token = _get_token()
    if not token or not group_id:
        logger.error("missing TOKEN or group_id; skip push")
        return False
    try:
        # Looked up when the push runs, not at import: an update that lands halfway must not stop the
        # 08:00 job before it starts.
        import line_push_client

        return bool(
            line_push_client.try_push_text_with_mentions(
                group_id, text, targets, retry_key=retry_key, timeout=10, source="todo"
            )
        )
    except Exception as exc:
        logger.warning("todo push failed error_type=%s", type(exc).__name__)
        return False


def _owner_line(todo_row: dict) -> tuple[str, tuple[str, str] | None]:
    """「媽媽 繳學費」: whose todo, by family name, then the task (Andrew 2026-10-07: 主詞放前面; the end of a
    user id told nobody who it was).  Also ``(user_id, name)`` when the line shows the owner's name:
    2026-11-12 「提到人就真 @」 @s them, decided in the same pass that prints the line.  The name is cleaned
    the way the printer cleans it (a leading @／＠ dropped)."""
    import line_mentions
    import reminder_overview

    user_id = str(todo_row.get("sender_user_id") or "")
    owner = line_mentions.alias_for_user_id(user_id) or ""
    line = reminder_overview.subject_first(str(todo_row.get("task") or ""), [owner] if owner else [])
    name = owner.strip().lstrip("@＠").strip()
    return line, ((user_id, name) if name and name in line else None)


def _format_reminder_and_owners(
    todos_due: list[dict], todos_overdue: list[dict]
) -> tuple[str, list[tuple[str, str]]]:
    """The daily reminder text, and the owners it names, in the order they appear."""
    lines = ["📋 今日 TODO 提醒"]
    owners: list[tuple[str, str]] = []
    if todos_due:
        lines.append("\n🔔 今天到期：")
        for t in todos_due:
            line, owner = _owner_line(t)
            lines.append(f"- {line}")
            if owner:
                owners.append(owner)
    if todos_overdue:
        lines.append("\n⚠️ 已逾期 (7 天內):")
        for t in todos_overdue:
            line, owner = _owner_line(t)
            lines.append(f"- {line} (due {t['due_date']})")
            if owner:
                owners.append(owner)
    return "\n".join(lines), owners


def _format_reminder(todos_due: list[dict], todos_overdue: list[dict]) -> str:
    """Compose daily reminder message."""
    return _format_reminder_and_owners(todos_due, todos_overdue)[0]


def _owner_targets(owners) -> list:
    """「提到人就真 @」：@ 名字有印出來的 owner。出錯就不 @，不能讓 @ 擋掉這則提醒。"""
    try:
        import line_mentions

        return line_mentions.user_mention_targets(owners)
    except Exception as exc:
        logger.warning("todo mention targets failed error_type=%s", type(exc).__name__)
        return []


def push_daily_reminders(group_id: str) -> int:
    due = list_due_today(group_id)
    overdue = list_overdue(group_id)
    if not due and not overdue:
        logger.info("no due or overdue todos today, skip push group=%s", group_id)
        return 0
    text, owners = _format_reminder_and_owners(due, overdue)
    # TODO(2026-11-13 deferred): TODO 沒有先搶列的去重。主 key 的 textV2 被拒收、改送純文字之後，同一天重跑會再送
    # 一次（LINE 沒收過主 key）。現在 TODO 是 0 筆（抽取抓不到資料，見 _sweep_one_group），先接受。
    retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"todo_daily:{group_id}:{datetime.now(_TW).date().isoformat()}"))
    if _push(group_id, text, _owner_targets(owners), retry_key=retry_key):
        for t in due + overdue:
            mark_reminded(t["todo_id"])
        logger.info(
            "daily todo reminder sent group=%s due=%d overdue=%d",
            group_id, len(due), len(overdue),
        )
        return len(due) + len(overdue)
    return 0


def _sweep_one_group(group_id: str) -> int:
    """Sweep last 7 days raw_messages of a single group for todo extraction."""
    import memory
    # TODO(2026-11-13 deferred, Andrew 決定要不要修): raw_messages.created_at 是秒（memory.log_raw_message 寫
    # strftime('%s')），這裡拿毫秒比，一筆都抓不到。修好之後 08:00 的 TODO 提醒就會開始 @ 人。
    since_ts = int((time.time() - 7 * 24 * 3600) * 1000)
    with memory._conn() as c:
        rows = c.execute(
            "SELECT message_id, user_id, text, created_at FROM raw_messages "
            "WHERE group_id=? AND created_at>=? ORDER BY created_at ASC",
            (group_id, since_ts),
        ).fetchall()
    n_new = 0
    for msg_id, user_id, text, _ts in rows:
        if not text or len(text) < 5:
            continue
        extracted = extract_from_text(text)
        for todo in extracted:
            tid = insert_todo(
                group_id=group_id,
                task=todo["task"],
                sender_user_id=user_id,
                due_date=todo.get("due_date"),
                source_msg_id=msg_id,
                source_text=text,
            )
            if tid:
                n_new += 1
                logger.info(
                    "new todo group=%s: %s (due=%s)",
                    group_id, todo["task"], todo.get("due_date"),
                )
    return n_new


def main_extractor_sweep() -> int:
    """Launchd: incremental sweep last 7 days raw_messages for todos across all groups."""
    gids = _get_target_group_ids()
    if not gids:
        logger.error("沒有任何 target group_id（ALLOWED_GROUP_IDS / ALLOWED_GROUP_ID 都未設）")
        return 1
    init_db()
    total = 0
    for gid in gids:
        n = _sweep_one_group(gid)
        total += n
        logger.info("todo sweep done group=%s: %d new", gid, n)
    logger.info("todo sweep total: %d new across %d group(s)", total, len(gids))
    return 0


def main_reminder() -> int:
    """Launchd: daily reminder push across all groups."""
    gids = _get_target_group_ids()
    if not gids:
        logger.error("沒有任何 target group_id（ALLOWED_GROUP_IDS / ALLOWED_GROUP_ID 都未設）")
        return 1
    init_db()
    for gid in gids:
        push_daily_reminders(gid)
    return 0


init_db()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "reminder"
    if mode == "extract":
        sys.exit(main_extractor_sweep())
    else:
        sys.exit(main_reminder())
