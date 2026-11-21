"""TODO／紀念日抽取的數字（2026-11-13，給每日維護）：唯讀、只印數字 JSON。

在正式目錄用 ``.venv/bin/python jobs/family_extract_status.py`` 跑。DB 用 ``mode=ro`` 開，不 import
todo／anniversary（它們 import 時會建表），只讀白名單欄位，不印任何家人文字、稱呼、id。

輸出：每個 job 的游標狀態（family_extract.status）、TODO 與候選的筆數，以及 ``alerts``：一串固定代碼，
每日維護照代碼寫成一句話。exit code：0 完成；2 不在正式目錄（沒有 .env 或 DB）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

_TW = ZoneInfo("Asia/Taipei")
_ENV_KEYS = (
    "SQLITE_PATH", "ALLOWED_GROUP_IDS", "LINE_ALLOWED_GROUP_ID", "ALLOWED_GROUP_ID", "FAMILY_GROUP_ID",
    "LINE_BOT_TODO_MODE", "LINE_BOT_ANNIVERSARY_EXTRACT",
)


def _mode(raw: str | None, *, allowed: tuple[str, ...]) -> str:
    """The jobs' own reading (family_extract.parse_mode): unset or on → on, a typo → off."""
    value = str(raw or "").strip().lower()
    if value in ("", "on", "1", "true", "yes"):
        return "on"
    if value in ("off", "0", "false", "no"):
        return "off"
    return value if value in allowed else "off"


def _env(base: Path) -> dict[str, str] | None:
    """The jobs' own reading of .env (python-dotenv: comments, quotes and ``export`` handled), only
    the keys this report needs."""
    path = base / ".env"
    if not path.is_file():
        return None
    from dotenv import dotenv_values

    values = dotenv_values(path)
    return {k: str(values.get(k) or "").strip() for k in _ENV_KEYS if values.get(k) is not None}


def _family_group(env: dict[str, str]) -> str:
    """config.Settings.family_group_id (FAMILY_GROUP_ID, else the legacy ALLOWED_GROUP_ID), kept only
    when it is one of the allowed groups, as the jobs themselves decide."""
    allowed = [g.strip() for g in env.get("ALLOWED_GROUP_IDS", "").split(",") if g.strip()]
    if not allowed:
        single = (env.get("LINE_ALLOWED_GROUP_ID") or env.get("ALLOWED_GROUP_ID") or "").strip()
        allowed = [single] if single else []
    family = (env.get("FAMILY_GROUP_ID") or env.get("ALLOWED_GROUP_ID") or "").strip()
    return family if family and family in allowed else ""


def _count(conn, sql: str, params: tuple = ()) -> int | None:
    """One integer, or None when the table is missing or the value is NULL (MAX of no rows)."""
    try:
        value = conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error:
        return None
    return None if value is None else int(value)


_NAME_RE = re.compile(r"[a-z_]{1,40}", re.ASCII)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_RUN_KEYS = ("mode", "read", "sent", "requests", "answered", "new", "stopped", "gap_s", "skipped", "finished_at")
_PUSH_KEYS = (
    "mode", "date", "due", "expired", "unsent", "fold_structural", "fold_queue", "fold_semantic",
    "fold_error", "pushed", "lines", "would_push", "would_fold", "push_ok", "mark_error", "error",
)


def _clean(summary: dict, keys: tuple[str, ...]) -> dict:
    """Numbers, booleans and a few fixed words only: whatever else a summary holds is dropped."""
    out: dict = {}
    for key in keys:
        value = summary.get(key) if isinstance(summary, dict) else None
        if isinstance(value, (bool, int)):
            out[key] = value
        elif key == "skipped" and isinstance(value, dict):
            out[key] = {
                k: v for k, v in value.items()
                if isinstance(k, str) and _NAME_RE.fullmatch(k) and isinstance(v, int) and not isinstance(v, bool)
            }
        elif key == "mode" and value in ("on", "shadow", "off"):
            out[key] = value
        elif key in ("stopped", "error") and isinstance(value, str) and re.fullmatch(r"[A-Za-z_]{0,60}", value, re.ASCII):
            out[key] = value
        elif key == "date" and isinstance(value, str) and _DATE_RE.fullmatch(value):
            out[key] = value
    return out


def collect(base: Path = BASE, *, now: float | None = None) -> dict | None:
    import family_extract

    env = _env(base)
    if env is None:
        return None
    db_path = Path(env.get("SQLITE_PATH") or "line_bot.db")
    if not db_path.is_absolute():
        db_path = base / db_path
    if not db_path.is_file():
        return None
    now = time.time() if now is None else float(now)
    group = _family_group(env)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        newest = _count(conn, "SELECT MAX(created_at) FROM raw_messages WHERE group_id=?", (group,)) if group else None
        jobs = {}
        for name in ("todo", "anniversary"):
            row = None
            try:
                row = conn.execute(
                    "SELECT created_at, message_id FROM extract_cursors WHERE name=? AND group_id=?", (name, group)
                ).fetchone()
            except sqlite3.Error:
                row = None
            unread = None
            if row is not None:
                unread = _count(
                    conn,
                    "SELECT COUNT(*) FROM raw_messages WHERE group_id=? "
                    "AND (created_at > ? OR (created_at = ? AND message_id > ?))",
                    (group, int(row[0]), int(row[0]), str(row[1])),
                )
            job = family_extract.status(conn, name, group, now=now, newest_raw=newest, unread=unread)
            if job.get("ran"):
                job["last_run"] = _clean(job.get("last_run") or {}, _RUN_KEYS)
                job["last_push"] = _clean(job.get("last_push") or {}, _PUSH_KEYS)
            jobs[name] = job
        today = datetime.fromtimestamp(now, _TW).date()
        week_ago = (today - timedelta(days=7)).isoformat()
        todo_counts = {
            "pending_upcoming": _count(
                conn, "SELECT COUNT(*) FROM todos WHERE status='pending' AND due_date >= ?", (today.isoformat(),)
            ),
            "shadow": _count(conn, "SELECT COUNT(*) FROM todos WHERE status='shadow'"),
            # Context only, not an alert: switching off → on expires the rows that fell due meanwhile.
            "expired_never_pushed_7d": _count(
                conn,
                "SELECT COUNT(*) FROM todos WHERE status='expired' AND last_reminded_at IS NULL AND due_date >= ?",
                (week_ago,),
            ),
        }
        anniversary_counts = {
            status: _count(conn, "SELECT COUNT(*) FROM anniversary_candidates WHERE status=?", (status,))
            for status in ("pending", "confirmed", "rejected")
        }
        anniversary_counts["anniversaries"] = _count(conn, "SELECT COUNT(*) FROM anniversaries")
        # From the sent ledger, not last_push: a second run the same day rewrites last_push.
        day_start_ms = int(datetime(today.year, today.month, today.day, tzinfo=_TW).timestamp() * 1000)
        anniversary_counts["pushed_today"] = _count(
            conn, "SELECT COUNT(*) FROM anniversary_reminders_sent WHERE sent_at >= ?", (day_start_ms,)
        )
    finally:
        conn.close()
    modes = {
        "todo": _mode(env.get("LINE_BOT_TODO_MODE"), allowed=("on", "shadow", "off")),
        "anniversary": _mode(env.get("LINE_BOT_ANNIVERSARY_EXTRACT"), allowed=("on", "off")),
    }
    return {
        "family_group_set": bool(group),
        "modes": modes,
        "jobs": jobs,
        "todo": todo_counts,
        "anniversary": anniversary_counts,
        "alerts": alerts(jobs, anniversary_counts, modes, now=now),
    }


def _since_h(now: float | None, ts) -> float | None:
    if now is None or isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
        return None
    return max(0.0, (now - ts) / 3600)


def _after_0810(now: float | None) -> datetime | None:
    """Today 08:00 (Taipei) once it is 08:10 or later, else None: both jobs run at 08:00."""
    if now is None:
        return None
    here = datetime.fromtimestamp(now, _TW)
    run_at = here.replace(hour=8, minute=0, second=0, microsecond=0)
    return run_at if here >= run_at + timedelta(minutes=10) else None


def _push_not_today(push: dict, now: float | None) -> bool:
    """A push that ran before but left no summary today (2026-11-21 F1): a lock took the push half, or the
    save of its summary.  A push that never ran yet (the deploy day) raises nothing here."""
    run_at = _after_0810(now)
    return run_at is not None and bool(push.get("date")) and push.get("date") != run_at.date().isoformat()


def alerts(jobs: dict, anniversary_counts: dict, modes: dict, *, now: float | None = None) -> list[str]:
    """Fixed codes; the daily maintenance prompt turns each into one sentence.  A job switched off
    raises nothing (its numbers simply stop moving)."""
    out: list[str] = []
    for name, job in jobs.items():
        if modes.get(name) == "off":
            continue
        if not job.get("ran") or not job.get("last_run"):  # the push may create the row first
            out.append(f"{name}_never_ran")
            continue
        last = job.get("last_run") or {}
        skipped = last.get("skipped") or {}
        if (job.get("no_answer_h") or 0) >= 24:  # two daily runs in a row without an answer
            out.append(f"{name}_no_answer_2d")
        if job.get("failures") or skipped.get("given_up") or skipped.get("blocked"):
            out.append(f"{name}_bad_answers")
        if last.get("gap_s"):
            out.append(f"{name}_gap")
        if (job.get("behind_h") or 0) > 48:
            out.append(f"{name}_behind_48h")
        if last.get("stopped") == "db_busy":
            out.append(f"{name}_db_busy")
        # A run that got to its end (finished_at), not a batch that moved the cursor (updated_at): a lock can
        # cut a run off after its first batch (2026-11-21 F1).  A row without finished_at falls back.
        since_run_h = _since_h(now, last.get("finished_at"))
        if since_run_h is None:
            since_run_h = job.get("since_run_h")
        if (since_run_h or 0) > 26:
            out.append(f"{name}_not_run_26h")
        # Both jobs run at 08:00: by 08:10 today's run should have finished, even when a lock kept it from
        # recording why it stopped (REV_1 I-1).
        run_at = _after_0810(now)
        if run_at is not None and since_run_h is not None and since_run_h * 3600 > (now - run_at.timestamp()) + 360:
            out.append(f"{name}_not_run_today")
    push = (jobs.get("todo") or {}).get("last_push") or {}
    if modes.get("todo") != "off" and (jobs.get("todo") or {}).get("ran") and _push_not_today(push, now):
        out.append("todo_push_not_today")
    if modes.get("todo") == "on" and push.get("mode") == "on":
        if push.get("push_ok") is False:
            out.append("todo_push_failed")
        if push.get("fold_error"):
            out.append("todo_fold_error")
        if push.get("mark_error"):
            out.append("todo_mark_error")
    anniversary_push = (jobs.get("anniversary") or {}).get("last_push") or {}
    if anniversary_push.get("push_ok") is False:  # the push runs whatever the extraction switch says
        out.append("anniversary_push_failed")
    if (jobs.get("anniversary") or {}).get("ran") and _push_not_today(anniversary_push, now):
        out.append("anniversary_push_not_today")
    if anniversary_counts.get("pending"):
        out.append("anniversary_candidates_pending")
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        data = collect()
    except Exception as exc:  # the report still gets one line, never a traceback with paths
        print(json.dumps({"error": "status_failed", "type": type(exc).__name__}))
        return 1
    if data is None:
        print(json.dumps({"error": "not_production_folder"}))
        return 2
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
