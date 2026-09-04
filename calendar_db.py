"""家族行事曆 — events 表 CRUD。

events 表：burst flush 時 Gemini 抽出的家族活動（聚餐、出遊、就醫…）。
- status='active'：未取消，會被 7 天前提醒掃到
- status='cancelled'：被使用者口頭取消（"不去了 / 改期"）
- reminded_at：已推 7 天前提醒的時間（NULL = 還沒推），避免重推
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
import line_mentions
import reminder_intent

_TW = ZoneInfo("Asia/Taipei")


def _today_tw() -> date:
    return datetime.now(_TW).date()

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()

# 集中 source of truth — 加新 type 只動這一行（GP2 反饋：防 enum 漂移）
EVENT_TYPES: tuple[str, ...] = ("family_gathering", "personal_trip", "medical")
_DEFAULT_EVENT_TYPE = "family_gathering"

# Reminder offsets — multi-tier 提醒 (Andrew 2026-05-25 directive 加 30/7-day)
# 30 = 1 個月前 / 7 = 1 週前 / 3-2-1 = 倒數三天每天推 / 0 = 當天提醒
# 加新 offset 只動這一行 + schema migration 自動補對應 reminded_Xd 欄位
REMINDER_OFFSETS: tuple[int, ...] = (30, 7, 3, 2, 1, 0)
EVENT_REMINDER_SOURCE_KIND = "calendar_event"
_EVENT_REMINDER_URL_RE = re.compile(r"https?://[^\s\]\)；，。？！!?]+")
_EVENT_REMINDER_CODE_RE = re.compile(
    r"(?:驗證碼|認證碼|校驗碼|接機碼|確認碼|領車碼|出發碼|code|OTP|passcode)"
    r"\s*[:：]?\s*([A-Za-z0-9]{4,12})",
    re.IGNORECASE,
)
_BADMINTON_RE = re.compile(r"羽球")


def _extract_event_reference_info(raw_text: str | None) -> tuple[list[str], list[str]]:
    """從 LINE 原文訊息抓 URL 與驗證碼，供 reminder source_text 補充。"""
    if not raw_text:
        return [], []
    text = str(raw_text).strip()
    if not text:
        return [], []

    urls: list[str] = []
    for url in _EVENT_REMINDER_URL_RE.findall(text):
        cleaned = re.sub(r"[\]】。，,;；>)]$", "", url)
        if cleaned and cleaned not in urls:
            urls.append(cleaned)

    codes: list[str] = []
    for m in _EVENT_REMINDER_CODE_RE.finditer(text):
        code = (m.group(1) or "").strip()
        if code and code not in codes:
            codes.append(code)

    return urls, codes


def _event_participants(event: Mapping[str, object]) -> list[str]:
    """Normalize event participants from db/json/list payload into displayable names."""

    raw = event.get("participants", "[]")
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            loaded = json.loads(text)
        except Exception:
            return []
        values = loaded if isinstance(loaded, list) else []
    else:
        return []

    participants: list[str] = []
    for item in values:
        name = str(item).strip()
        if name:
            clean_name = _clean_participant_name(name)
            if clean_name:
                participants.append(clean_name)
    return participants


def _merge_mention_aliases(*buckets: list[str] | None) -> list[str]:
    out: list[str] = []
    for aliases in buckets:
        for alias in aliases or []:
            name = _clean_participant_name(str(alias))
            if not name or name in out:
                continue
            out.append(name)
    return out


def _raw_text_reminder_aliases(text: str) -> list[str]:
    """Only explicit raw mentions should add reminder mention targets.

    A family member name in the source text can be the event subject ("爸爸體檢"),
    not necessarily someone to tag. Keep explicit @mentions and broad family
    mentions such as "全家".
    """
    if not text:
        return []
    from line_mentions import aliases_mentioned_in_text

    explicit_mentions = " ".join(
        m.group(0).replace("＠", "@")
        for m in re.finditer(r"[@＠][A-Za-z0-9_\u4e00-\u9fff]{1,30}", text)
    )
    broad_mentions = " ".join(
        term for term in ("全家", "大家", "所有人", "全部", "全員", "@all", "everyone")
        if term in text
    )
    return aliases_mentioned_in_text(f"{explicit_mentions} {broad_mentions}".strip())


def _clean_participant_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned:
        return ""
    cleaned = cleaned.lstrip("@")
    for marker in ("(", "（"):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0].strip()
            break
    return cleaned


def _is_badminton_event(event: Mapping[str, object]) -> bool:
    haystack = " ".join(
        str(event.get(key, "") or "")
        for key in ("title", "location", "source_text")
    )
    return bool(_BADMINTON_RE.search(haystack))


def _uses_badminton_booking_workflow(event: Mapping[str, object]) -> bool:
    return _is_badminton_event(event) and _event_reminder_lead_days(event) > 0


def _format_month_day(event_date: str) -> str:
    try:
        _, month_s, day_s = str(event_date).split("-", 2)
        return f"{int(month_s)}/{int(day_s)}"
    except Exception:
        return str(event_date)


def _event_reminder_lead_days(event: Mapping[str, object]) -> int:
    raw = event.get("reminder_lead_days")
    if raw is not None and str(raw).strip() != "":
        try:
            days = int(raw)
        except (TypeError, ValueError):
            days = -1
        if 0 <= days <= 365:
            return days
    return 7 if _is_badminton_event(event) else 0


def _event_reminder_time(event: Mapping[str, object]) -> object:
    if _uses_badminton_booking_workflow(event):
        return "18:00"
    return event.get("event_time")


def _reminded_column(offset: int) -> str:
    """offset → reminded_Xd column name。Whitelist 驗 offset 防 SQL injection (column 不能 bind param)。"""
    if offset not in REMINDER_OFFSETS:
        raise ValueError(f"invalid offset {offset}; must be in {REMINDER_OFFSETS}")
    return f"reminded_{offset}d"


def _validate_event_type(et: str | None) -> str:
    """白名單驗證 — invalid → default。"""
    return et if et in EVENT_TYPES else _DEFAULT_EVENT_TYPE


# LIKE wildcard escape — escape 順序：先 \ 再 _ / %（順序錯會 double-escape）
def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")


def _ensure_memory_db_path() -> None:
    """Keep memory._DB_PATH 對齊 calendar_db._DB_PATH（避免 test env drift）。"""
    import memory

    if str(memory._DB_PATH) != str(_DB_PATH):
        memory._DB_PATH = _DB_PATH
        memory._init_db()


def _event_reminder_payload(event: Mapping[str, object]) -> tuple[str, str]:
    title = str(event.get("title", "") or "").strip()
    participants = _event_participants(event)
    if _uses_badminton_booking_workflow(event):
        event_date = str(event.get("event_date", "") or "").strip()
        event_time = str(event.get("event_time", "") or "").strip()
        responsible = [p for p in participants if p and p != "全家"]
        prefix = f"{'、'.join(responsible)}負責" if responsible else ""
        action = f"{prefix}預約{_format_month_day(event_date)}打羽球場地"
        source_parts = [f"活動：{title or '打羽球'}"]
        if event_date:
            source_parts.append(f"活動日期：{event_date}")
        if event_time:
            source_parts.append(f"活動時間：{event_time}")
        if responsible:
            source_parts.append("預約負責人：" + "、".join(responsible))
        elif participants:
            source_parts.append("預約標注：" + "、".join(participants))
        source_parts.append("提醒規則：活動前 7 天預約場地")
        return action, "；".join([p for p in source_parts if p])

    action = title or "家族行事曆提醒"

    source_parts = [title]
    location = str(event.get("location", "") or "").strip()
    if location:
        source_parts.append(f"地點：{location}")
    event_time = str(event.get("event_time", "") or "").strip()
    if event_time:
        source_parts.append(f"時間：{event_time}")
    if participants:
        source_parts.append("參加人：" + "、".join(participants))
    source_msg_id = str(event.get("source_msg_id") or "").strip()
    group_id = str(event.get("group_id") or "").strip()
    location_has_reference = "接送網址" in location or "驗證碼" in location
    if source_msg_id and group_id and not location_has_reference:
        try:
            import memory

            _ensure_memory_db_path()
            raw = memory.get_raw_message(group_id, source_msg_id)
            if raw and raw[1]:
                urls, codes = _extract_event_reference_info(raw[1])
                if urls:
                    source_parts.append(f"接送網址：{'、'.join(urls)}")
                if codes:
                    source_parts.append(f"驗證碼：{'、'.join(codes)}")
        except Exception:
            pass
    return action, "；".join([p for p in source_parts if p])


def _event_to_remind_at(
    event_date: str,
    event_time: str | None,
    *,
    days_before: int = 0,
) -> int | None:
    if not event_date:
        return None
    try:
        if event_time:
            hour, minute = map(int, str(event_time).strip().split(":", 1))
        else:
            hour, minute = 0, 0
        y, m, d = (int(x) for x in str(event_date).split("-"))
        dt = datetime(y, m, d, hour, minute, tzinfo=_TW)
        if days_before:
            dt = dt - timedelta(days=days_before)
        return int(dt.timestamp())
    except Exception:
        return None


def _upsert_event_reminder(
    event: Mapping[str, object],
    *,
    preserve_existing: bool = False,
    synchronize_pending: bool = False,
) -> bool:
    if not event:
        return False
    days_before = _event_reminder_lead_days(event)
    remind_at = _event_to_remind_at(
        str(event.get("event_date", "")),
        _event_reminder_time(event),  # type: ignore[arg-type]
        days_before=days_before,
    )
    if remind_at is None:
        return False
    action, source_text = _event_reminder_payload(event)
    import memory

    group_id = str(event.get("group_id", "") or "").strip()
    source_msg_id = str(event.get("source_msg_id", "") or "").strip()
    raw_text = ""
    reminder_user_id = ""
    if source_msg_id and group_id:
        try:
            _ensure_memory_db_path()
            raw = memory.get_raw_message(group_id, source_msg_id)
            if raw and raw[0]:
                reminder_user_id = str(raw[0]).strip()
            if raw and raw[1]:
                raw_text = str(raw[1] or "")
        except Exception:
            pass

    participant_aliases = _event_participants(event)
    raw_aliases = _raw_text_reminder_aliases(raw_text)
    if _is_badminton_event(event) and participant_aliases:
        mention_aliases = participant_aliases
    else:
        mention_aliases = _merge_mention_aliases(participant_aliases, raw_aliases)

    _ensure_memory_db_path()
    try:
        if synchronize_pending:
            reminder_id = memory.synchronize_pending_reminder_for_source(
                group_id=group_id,
                user_id=reminder_user_id,
                action=action,
                remind_at=remind_at,
                source_kind=EVENT_REMINDER_SOURCE_KIND,
                source_ref=str(event.get("event_id", "")),
                source_text=source_text,
                mention_aliases=mention_aliases,
                require_active_calendar_event=True,
            )
        elif preserve_existing:
            if not hasattr(memory, "ensure_reminder_for_source"):
                return False
            reminder_id = memory.ensure_reminder_for_source(
                group_id=str(event.get("group_id", "")),
                user_id=reminder_user_id,
                action=action,
                remind_at=remind_at,
                source_kind=EVENT_REMINDER_SOURCE_KIND,
                source_ref=str(event.get("event_id", "")),
                source_text=source_text,
                mention_aliases=mention_aliases,
            )
        elif hasattr(memory, "upsert_reminder_for_source_any_status"):
            reminder_id = memory.upsert_reminder_for_source_any_status(
                group_id=str(event.get("group_id", "")),
                user_id=reminder_user_id,
                action=action,
                remind_at=remind_at,
                source_kind=EVENT_REMINDER_SOURCE_KIND,
                source_ref=str(event.get("event_id", "")),
                source_text=source_text,
                mention_aliases=mention_aliases,
            )
        else:
            reminder_id = memory.upsert_reminder_for_source(
                group_id=str(event.get("group_id", "")),
                user_id=reminder_user_id,
                action=action,
                remind_at=remind_at,
                source_kind=EVENT_REMINDER_SOURCE_KIND,
                source_ref=str(event.get("event_id", "")),
                source_text=source_text,
                mention_aliases=mention_aliases,
            )
        return reminder_id is not None
    except Exception:
        return False


def ensure_event_reminder_mirror(event: Mapping[str, object]) -> bool:
    """Safely backfill one event source without reviving terminal reminders."""

    return _upsert_event_reminder(event, preserve_existing=True)


def synchronize_pending_event_reminder_mirror(
    event: Mapping[str, object],
) -> bool:
    """Create or refresh one pending mirror without reviving terminal rows."""

    return _upsert_event_reminder(event, synchronize_pending=True)


def ensure_active_event_reminder_mirrors(group_id: str | None = None) -> int:
    """Insert missing reminder mirrors for active legacy calendar events."""

    _ensure_memory_db_path()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        if group_id:
            rows = c.execute(
                "SELECT * FROM events WHERE group_id=? AND status='active'",
                (group_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM events WHERE status='active'",
            ).fetchall()
    return sum(ensure_event_reminder_mirror(dict(row)) for row in rows)


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(
        _DB_PATH,
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id       TEXT PRIMARY KEY,
                group_id       TEXT NOT NULL,
                title          TEXT NOT NULL,
                event_date     TEXT NOT NULL,
                event_time     TEXT,
                location       TEXT,
                participants   TEXT,
                source_msg_id  TEXT,
                status         TEXT NOT NULL DEFAULT 'active',
                created_at     INTEGER NOT NULL,
                reminded_at    INTEGER,
                reminder_lead_days INTEGER
            )
            """
        )
        # PRAGMA pre-check + conditional ALTER（GP2 反饋：跨 process race window 收窄）
        # event_reminder.py 在 launchd job 跑時也 import calendar_db → init_db，
        # 跟 uvicorn worker 並行。PRAGMA + ALTER 在 WAL 下窗口小，加上 BUSY 容忍。
        cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
        if "event_type" not in cols:
            try:
                c.execute(
                    "ALTER TABLE events ADD COLUMN event_type "
                    "TEXT NOT NULL DEFAULT 'family_gathering'"
                )
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                # duplicate column = 另一 process 剛剛 ALTER 過，OK
                # database is locked = 暫時 contention，下一輪 import 會補
                if "duplicate column" not in msg and "database is locked" not in msg:
                    raise
            # 重新 verify（如果是 lock 沒做成功，下一輪 init_db 會處理）
            cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
        assert "event_type" in cols, "event_type migration failed"
        if "reminder_lead_days" not in cols:
            try:
                c.execute("ALTER TABLE events ADD COLUMN reminder_lead_days INTEGER")
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" not in msg and "database is locked" not in msg:
                    raise
            cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
        assert "reminder_lead_days" in cols, "reminder_lead_days migration failed"

        # Reminder offsets migration — 每個 offset 一個 timestamp 欄位（NULL=未推）
        # 同 event_type PRAGMA pre-check + duplicate-column tolerance (codex critical)
        for off in REMINDER_OFFSETS:
            col_name = f"reminded_{off}d"
            if col_name not in cols:
                try:
                    c.execute(f"ALTER TABLE events ADD COLUMN {col_name} INTEGER")
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "duplicate column" not in msg and "database is locked" not in msg:
                        raise
                cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
            assert col_name in cols, f"{col_name} migration failed"
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_active "
            "ON events(group_id, status, event_date)"
        )
        # Migrate the old date-only key once. The transaction keeps the legacy
        # uniqueness constraint live until the replacement can be created.
        desired_index_sql = (
            "CREATE UNIQUE INDEX idx_events_dedup "
            "ON events(group_id, title, event_date, COALESCE(event_time, '')) "
            "WHERE status='active'"
        )
        existing_index = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_events_dedup'"
        ).fetchone()
        def normalize_index_sql(sql: object) -> str:
            return "".join(str(sql or "").lower().split())

        if existing_index is None:
            c.execute(desired_index_sql)
        elif normalize_index_sql(existing_index[0]) != normalize_index_sql(
            desired_index_sql
        ):
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("DROP INDEX idx_events_dedup")
                c.execute(desired_index_sql)
            except Exception:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")


def _semantic_event_score(event: Mapping[str, object]) -> tuple[int, int, int]:
    title = reminder_intent.normalize_text(event.get("title"))
    return (
        int(bool(str(event.get("source_msg_id") or "").strip())),
        int(bool(str(event.get("location") or "").strip())),
        len(title),
    )


def _semantic_event_candidates_conn(
    c: sqlite3.Connection,
    incoming: Mapping[str, object],
) -> list[sqlite3.Row]:
    key = reminder_intent.event_semantic_key(incoming.get("title"))
    if not key:
        return []
    rows = c.execute(
        "SELECT * FROM events WHERE group_id=? AND event_date=? "
        "AND status='active' ORDER BY created_at, event_id",
        (
            str(incoming.get("group_id") or ""),
            str(incoming.get("event_date") or ""),
        ),
    ).fetchall()
    incoming_source = str(incoming.get("source_msg_id") or "").strip()
    incoming_location = reminder_intent.normalize_text(incoming.get("location"))
    incoming_participants = set(_event_participants(incoming))
    candidates: list[sqlite3.Row] = []
    for row in rows:
        current = dict(row)
        if not reminder_intent.event_titles_are_semantically_compatible(
            current.get("title"),
            incoming.get("title"),
        ):
            continue
        current_source = str(current.get("source_msg_id") or "").strip()
        if incoming_source and current_source and incoming_source != current_source:
            continue
        if str(current.get("event_type") or "") != str(
            incoming.get("event_type") or ""
        ):
            same_source = bool(incoming_source) and incoming_source == current_source
            one_sourceful = bool(incoming_source) != bool(current_source)
            if not (same_source or one_sourceful):
                continue
        current_location = reminder_intent.normalize_text(current.get("location"))
        if (
            incoming_location
            and current_location
            and incoming_location != current_location
        ):
            continue
        current_participants = set(_event_participants(current))
        if (
            incoming_participants
            and current_participants
            and incoming_participants.isdisjoint(current_participants)
        ):
            continue
        if not reminder_intent.schedules_are_compatible(
            current.get("event_time"),
            current.get("title"),
            incoming.get("event_time"),
            incoming.get("title"),
        ):
            continue
        candidates.append(row)
    return candidates


def _semantic_desired_event(
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
) -> dict[str, object]:
    desired = dict(existing)
    existing_source = str(existing.get("source_msg_id") or "").strip()
    incoming_source = str(incoming.get("source_msg_id") or "").strip()
    if incoming_source and not existing_source:
        for key in (
            "title",
            "event_time",
            "location",
            "participants",
            "source_msg_id",
            "event_type",
        ):
            desired[key] = incoming.get(key)
        return desired
    if existing_source and not incoming_source:
        return desired

    if _semantic_event_score(incoming) > _semantic_event_score(existing):
        desired["title"] = incoming.get("title")
    existing_location = str(existing.get("location") or "").strip()
    incoming_location = str(incoming.get("location") or "").strip()
    if incoming_location and len(incoming_location) > len(existing_location):
        desired["location"] = incoming_location
    merged_participants = _merge_mention_aliases(
        _event_participants(existing),
        _event_participants(incoming),
    )
    desired["participants"] = json.dumps(merged_participants, ensure_ascii=False)
    if not existing_source and incoming_source:
        desired["source_msg_id"] = incoming_source
    return desired


def _merge_semantic_event_conn(
    c: sqlite3.Connection,
    existing_row: sqlite3.Row,
    incoming: Mapping[str, object],
) -> tuple[str, str]:
    existing = dict(existing_row)
    event_id = str(existing["event_id"])
    desired = _semantic_desired_event(existing, incoming)
    changed_fields = (
        "title",
        "event_time",
        "location",
        "participants",
        "source_msg_id",
        "event_type",
    )
    if not any(desired.get(key) != existing.get(key) for key in changed_fields):
        return event_id, "duplicate"

    mirror_rows = c.execute(
        "SELECT * FROM reminders WHERE group_id=? AND source_kind=? "
        "AND source_ref=? ORDER BY reminder_id",
        (
            str(existing["group_id"]),
            EVENT_REMINDER_SOURCE_KIND,
            event_id,
        ),
    ).fetchall()
    if len(mirror_rows) != 1 or str(mirror_rows[0]["status"]) != "pending":
        return event_id, "blocked"
    mirror = mirror_rows[0]
    stale_before = int(time.time()) - 15 * 60
    live_claim = c.execute(
        "SELECT 1 FROM reminder_delivery_claims "
        "WHERE group_id=? AND state='sending' AND claimed_at>=? AND ("
        "(delivery_kind='calendar' AND subject_ref=?) OR "
        "(delivery_kind='natural' AND subject_ref=?) OR "
        "(source_kind=? AND source_ref=?)) LIMIT 1",
        (
            str(existing["group_id"]),
            stale_before,
            event_id,
            str(mirror["reminder_id"]),
            EVENT_REMINDER_SOURCE_KIND,
            event_id,
        ),
    ).fetchone()
    if live_claim is not None:
        return event_id, "busy"
    c.execute(
        "UPDATE reminder_delivery_claims SET state='uncertain' "
        "WHERE group_id=? AND state='sending' AND claimed_at<? AND ("
        "(delivery_kind='calendar' AND subject_ref=?) OR "
        "(delivery_kind='natural' AND subject_ref=?) OR "
        "(source_kind=? AND source_ref=?))",
        (
            str(existing["group_id"]),
            stale_before,
            event_id,
            str(mirror["reminder_id"]),
            EVENT_REMINDER_SOURCE_KIND,
            event_id,
        ),
    )

    schedule_changed = desired.get("event_time") != existing.get("event_time")
    action, remind_at, source_text = _corrected_event_payload_conn(c, desired)
    existing_aliases: list[str] = []
    try:
        loaded_aliases = json.loads(str(mirror["mention_aliases"] or "[]"))
        if isinstance(loaded_aliases, list):
            existing_aliases = [str(alias) for alias in loaded_aliases]
    except Exception:
        pass
    mention_aliases = json.dumps(
        _merge_mention_aliases(
            existing_aliases,
            _event_participants(desired),
        ),
        ensure_ascii=False,
    )
    reminder_schedule_changed = remind_at != int(mirror["remind_at"])

    event_reset_sql = (
        ", reminded_at=NULL, reminded_30d=NULL, reminded_7d=NULL, "
        "reminded_3d=NULL, reminded_2d=NULL, reminded_1d=NULL, reminded_0d=NULL"
        if schedule_changed
        else ""
    )
    updated_event = c.execute(
        "UPDATE events SET title=?, event_time=?, location=?, participants=?, "
        f"source_msg_id=?, event_type=?{event_reset_sql} "
        "WHERE group_id=? AND event_id=? AND status='active'",
        (
            desired.get("title"),
            desired.get("event_time"),
            desired.get("location"),
            desired.get("participants"),
            desired.get("source_msg_id"),
            desired.get("event_type"),
            str(existing["group_id"]),
            event_id,
        ),
    )
    if updated_event.rowcount != 1:
        raise RuntimeError("semantic event compare-and-set failed")

    reminder_reset_sql = (
        ", last_pushed_at=0, weekly_count=0, last_weekly_at=0, "
        "pushed_3d=0, pushed_1d=0, pushed_4hr=0, pushed_2hr=0, "
        "pushed_1hr=0, pushed_now=0"
        if reminder_schedule_changed
        else ""
    )
    updated_reminder = c.execute(
        "UPDATE reminders SET action=?, remind_at=?, source_text=?, "
        f"mention_aliases=?{reminder_reset_sql} "
        "WHERE group_id=? AND reminder_id=? AND status='pending' "
        "AND source_kind=? AND source_ref=?",
        (
            action,
            remind_at,
            source_text,
            mention_aliases,
            str(existing["group_id"]),
            int(mirror["reminder_id"]),
            EVENT_REMINDER_SOURCE_KIND,
            event_id,
        ),
    )
    if updated_reminder.rowcount != 1:
        raise RuntimeError("semantic reminder compare-and-set failed")
    return event_id, "merged"


def _insert_new_event_reminder_conn(
    c: sqlite3.Connection,
    event: Mapping[str, object],
) -> int:
    action, remind_at, source_text = _corrected_event_payload_conn(c, event)
    group_id = str(event.get("group_id") or "").strip()
    event_id = str(event.get("event_id") or "").strip()
    if not group_id or not event_id:
        raise ValueError("calendar event mirror has no source identity")

    source_msg_id = str(event.get("source_msg_id") or "").strip()
    reminder_user_id = ""
    raw_text = ""
    if source_msg_id:
        raw = c.execute(
            "SELECT user_id, text FROM raw_messages "
            "WHERE group_id=? AND message_id=?",
            (group_id, source_msg_id),
        ).fetchone()
        if raw is not None:
            reminder_user_id = str(raw[0] or "").strip()
            raw_text = str(raw[1] or "")

    participant_aliases = _event_participants(event)
    raw_aliases = _raw_text_reminder_aliases(raw_text)
    if _is_badminton_event(event) and participant_aliases:
        mention_aliases = participant_aliases
    else:
        mention_aliases = _merge_mention_aliases(participant_aliases, raw_aliases)
    cursor = c.execute(
        "INSERT INTO reminders("
        "group_id, user_id, action, remind_at, created_at, status, "
        "source_kind, source_ref, source_text, mention_aliases"
        ") VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (
            group_id,
            reminder_user_id,
            action,
            remind_at,
            int(time.time()),
            EVENT_REMINDER_SOURCE_KIND,
            event_id,
            source_text,
            json.dumps(mention_aliases, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def insert_event_with_outcome(
    group_id: str,
    title: str,
    event_date: str,
    event_time: str | None = None,
    location: str | None = None,
    participants: list[str] | None = None,
    source_msg_id: str | None = None,
    event_type: str = 'family_gathering',
) -> tuple[str, str]:
    event_id = uuid.uuid4().hex
    parts_json = json.dumps(participants or [], ensure_ascii=False)
    et = _validate_event_type(event_type)
    incoming: dict[str, object] = {
        "event_id": event_id,
        "group_id": group_id,
        "title": title,
        "event_date": event_date,
        "event_time": event_time,
        "location": location,
        "participants": parts_json,
        "source_msg_id": source_msg_id,
        "status": "active",
        "event_type": et,
    }
    try:
        _ensure_memory_db_path()
        with _lock, _conn() as c:
            c.row_factory = sqlite3.Row
            c.execute("BEGIN IMMEDIATE")
            candidates = _semantic_event_candidates_conn(c, incoming)
            if len(candidates) > 1:
                return "", "ambiguous"
            if len(candidates) == 1:
                return _merge_semantic_event_conn(c, candidates[0], incoming)
            cur = c.execute(
                "INSERT OR IGNORE INTO events (event_id, group_id, title, "
                "event_date, event_time, location, participants, source_msg_id, "
                "status, created_at, event_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    event_id,
                    group_id,
                    title,
                    event_date,
                    event_time,
                    location,
                    parts_json,
                    source_msg_id,
                    int(time.time() * 1000),
                    et,
                ),
            )
            if cur.rowcount == 0:
                exact = c.execute(
                    "SELECT event_id, source_msg_id FROM events "
                    "WHERE group_id=? AND title=? "
                    "AND event_date=? AND COALESCE(event_time, '')=COALESCE(?, '') "
                    "AND status='active'",
                    (group_id, title, event_date, event_time),
                ).fetchone()
                if exact:
                    existing_source = str(exact[1] or "").strip()
                    incoming_source = str(source_msg_id or "").strip()
                    if (
                        existing_source
                        and incoming_source
                        and existing_source != incoming_source
                    ):
                        return "", "conflict"
                    return str(exact[0]), "duplicate"
                return "", "conflict"
            _insert_new_event_reminder_conn(c, incoming)
    except (sqlite3.Error, OSError, RuntimeError, ValueError):
        return "", "unavailable"
    try:
        import memory

        memory.delete_duplicate_pending_reminders(group_id)
    except Exception:
        pass
    return event_id, "created"


def insert_event(
    group_id: str,
    title: str,
    event_date: str,
    event_time: str | None = None,
    location: str | None = None,
    participants: list[str] | None = None,
    source_msg_id: str | None = None,
    event_type: str = "family_gathering",
) -> str:
    """Backward-compatible insert API: only newly created events return an id."""

    event_id, outcome = insert_event_with_outcome(
        group_id=group_id,
        title=title,
        event_date=event_date,
        event_time=event_time,
        location=location,
        participants=participants,
        source_msg_id=source_msg_id,
        event_type=event_type,
    )
    if outcome == "created":
        return event_id
    if outcome in {"duplicate", "merged", "conflict", "ambiguous", "busy", "blocked"}:
        return ""
    raise RuntimeError(f"calendar event write failed: {outcome}")


def sync_active_events_to_reminders(group_id: str | None = None) -> int:
    """回填 events → reminders（缺漏補齊）。"""
    _ensure_memory_db_path()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        if group_id:
            rows = c.execute(
                "SELECT event_id, group_id, title, event_date, event_time, "
                "location, participants, status, source_msg_id, reminder_lead_days FROM events "
                "WHERE group_id = ? AND status='active'",
                (group_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT event_id, group_id, title, event_date, event_time, "
                "location, participants, status, source_msg_id, reminder_lead_days FROM events "
                "WHERE status='active'",
            ).fetchall()

    synced = 0
    for row in rows:
        event = dict(row)
        if _upsert_event_reminder(event):
            synced += 1

    try:
        import memory

        with _lock, _conn() as c:
            if group_id:
                active_refs = [
                    str(r[0])
                    for r in c.execute(
                        "SELECT event_id FROM events "
                        "WHERE group_id = ? AND status='active'",
                        (group_id,),
                    ).fetchall()
                    if r[0]
                ]
            else:
                active_refs = [
                    str(r[0])
                    for r in c.execute(
                        "SELECT event_id FROM events WHERE status='active'"
                    ).fetchall()
                    if r[0]
                ]
            memory.delete_pending_reminders_by_source(
                source_kind=EVENT_REMINDER_SOURCE_KIND,
                keep_source_refs=active_refs,
                group_id=group_id,
            )
            memory.delete_duplicate_pending_reminders(group_id=group_id)
    except Exception:
        pass

    return synced


def find_active_event(
    group_id: str,
    keyword: str | None = None,
    near_date: str | None = None,
    event_type: str | None = None,
) -> dict | None:
    """Find one unambiguous active event for cancellation or correction.

    A supplied keyword or date is a required selector. If it matches zero or
    multiple rows, fail closed instead of falling back to the newest event.

    TODO (GP1+GP2 Phase 6): event_type filter 已加但 caller `/取消活動 <keyword>`
    (main.py:_cancel_calendar_event) 目前**沒**傳入。Cross-type collision 風險 medium：
    user 同時有 medical + family_gathering 同含「胃鏡」keyword → 誤取消最新一筆。
    後續若實際遇到衝突，可在 cancel command parser 推斷 type 並傳入。
    """
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        if event_type is not None:
            rows = c.execute(
                "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
                "AND event_date >= date('now', '-1 day') AND event_type = ? "
                "ORDER BY created_at DESC",
                (group_id, event_type),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
                "AND event_date >= date('now', '-1 day') ORDER BY created_at DESC",
                (group_id,),
            ).fetchall()
    if not rows:
        return None
    if not keyword and not near_date:
        return None
    matched = list(rows)
    if keyword:
        matched = [
            r
            for r in matched
            if keyword in (r["title"] or "")
            or keyword in (r["location"] or "")
        ]
    if near_date:
        matched = [r for r in matched if r["event_date"] == near_date]
    return dict(matched[0]) if len(matched) == 1 else None


def find_active_events_by_source_message(
    group_id: str,
    source_msg_id: str,
) -> list[dict]:
    """Return active events bound to one exact group-scoped LINE message."""

    if not group_id or not source_msg_id:
        return []
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id=? AND source_msg_id=? "
            "AND status='active' ORDER BY created_at, event_id",
            (group_id, source_msg_id),
        ).fetchall()
    return [dict(row) for row in rows]


def find_events_by_source_message(
    group_id: str,
    source_msg_id: str,
) -> list[dict]:
    """Return every event status bound to one exact source message."""

    if not group_id or not source_msg_id:
        return []
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id=? AND source_msg_id=? "
            "ORDER BY created_at, event_id",
            (group_id, source_msg_id),
        ).fetchall()
    return [dict(row) for row in rows]


def find_unbound_active_events_by_schedule(
    group_id: str,
    *,
    event_date: str,
    event_time: str | None,
    event_type: str,
) -> list[dict]:
    """Return unbound active candidates for conservative legacy reconciliation."""

    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id=? AND event_date=? "
            "AND COALESCE(event_time, '')=COALESCE(?, '') AND event_type=? "
            "AND status='active' AND COALESCE(source_msg_id, '')='' "
            "ORDER BY created_at, event_id",
            (group_id, event_date, event_time, event_type),
        ).fetchall()
    return [dict(row) for row in rows]


def get_active_event_by_id(group_id: str, event_id: str) -> dict | None:
    """Return one exact active event in the requested group."""

    if not group_id or not event_id:
        return None
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM events WHERE group_id=? AND event_id=? "
            "AND status='active'",
            (group_id, event_id),
        ).fetchone()
    return dict(row) if row is not None else None


def _resolve_quoted_event_id_conn(
    c: sqlite3.Connection,
    group_id: str,
    quoted_message_id: str,
) -> tuple[str, str | None]:
    resolved_ids = {
        str(row[0])
        for row in c.execute(
            "SELECT event_id FROM events "
            "WHERE group_id=? AND source_msg_id=?",
            (group_id, quoted_message_id),
        ).fetchall()
        if row[0]
    }

    sent_ref = c.execute(
        "SELECT reminder_id, source_kind, source_ref "
        "FROM sent_reminder_refs WHERE group_id=? AND message_id=?",
        (group_id, quoted_message_id),
    ).fetchone()
    if sent_ref is not None:
        reminder_id = int(sent_ref[0]) if sent_ref[0] is not None else None
        declared_kind = str(sent_ref[1] or "")
        declared_ref = str(sent_ref[2] or "")
        sent_ids: set[str] = set()

        if declared_kind or declared_ref:
            if declared_kind != EVENT_REMINDER_SOURCE_KIND or not declared_ref:
                return "not_found", None
            declared_event = c.execute(
                "SELECT 1 FROM events WHERE group_id=? AND event_id=?",
                (group_id, declared_ref),
            ).fetchone()
            if declared_event is None:
                return "not_found", None
            sent_ids.add(declared_ref)

        if reminder_id is not None:
            reminder_source = c.execute(
                "SELECT source_kind, source_ref FROM reminders "
                "WHERE group_id=? AND reminder_id=?",
                (group_id, reminder_id),
            ).fetchone()
            if reminder_source is None:
                return "not_found", None
            reminder_kind = str(reminder_source[0] or "")
            reminder_ref = str(reminder_source[1] or "")
            if (
                reminder_kind != EVENT_REMINDER_SOURCE_KIND
                or not reminder_ref
            ):
                return "not_found", None
            if declared_ref and reminder_ref != declared_ref:
                return "ambiguous", None
            reminder_event = c.execute(
                "SELECT 1 FROM events WHERE group_id=? AND event_id=?",
                (group_id, reminder_ref),
            ).fetchone()
            if reminder_event is None:
                return "not_found", None
            sent_ids.add(reminder_ref)

        if not sent_ids:
            return "not_found", None
        resolved_ids.update(sent_ids)

    if not resolved_ids:
        return "not_found", None
    if len(resolved_ids) != 1:
        return "ambiguous", None
    return "resolved", next(iter(resolved_ids))


def resolve_quoted_event_identity(
    group_id: str,
    quoted_message_id: str,
) -> tuple[str, str | None]:
    """Resolve an inbound source or sent reminder to one group-scoped event."""

    if not group_id or not quoted_message_id:
        return "not_found", None
    with _lock, _conn() as c:
        return _resolve_quoted_event_id_conn(c, group_id, quoted_message_id)


def _corrected_event_payload_conn(
    c: sqlite3.Connection,
    event: Mapping[str, object],
) -> tuple[str, int, str]:
    days_before = _event_reminder_lead_days(event)
    remind_at = _event_to_remind_at(
        str(event.get("event_date", "")),
        _event_reminder_time(event),  # type: ignore[arg-type]
        days_before=days_before,
    )
    if remind_at is None:
        raise ValueError("corrected event has no valid reminder timestamp")

    title = str(event.get("title", "") or "").strip()
    participants = _event_participants(event)
    if _uses_badminton_booking_workflow(event):
        event_date = str(event.get("event_date", "") or "").strip()
        event_time = str(event.get("event_time", "") or "").strip()
        responsible = [p for p in participants if p and p != "全家"]
        prefix = f"{'、'.join(responsible)}負責" if responsible else ""
        action = f"{prefix}預約{_format_month_day(event_date)}打羽球場地"
        source_parts = [f"活動：{title or '打羽球'}"]
        if event_date:
            source_parts.append(f"活動日期：{event_date}")
        if event_time:
            source_parts.append(f"活動時間：{event_time}")
        if responsible:
            source_parts.append("預約負責人：" + "、".join(responsible))
        elif participants:
            source_parts.append("預約標注：" + "、".join(participants))
        source_parts.append("提醒規則：活動前 7 天預約場地")
        return action, remind_at, "；".join(source_parts)

    action = title or "家族行事曆提醒"
    source_parts = [title]
    location = str(event.get("location", "") or "").strip()
    if location:
        source_parts.append(f"地點：{location}")
    event_time = str(event.get("event_time", "") or "").strip()
    if event_time:
        source_parts.append(f"時間：{event_time}")
    if participants:
        source_parts.append("參加人：" + "、".join(participants))

    source_msg_id = str(event.get("source_msg_id") or "").strip()
    group_id = str(event.get("group_id") or "").strip()
    if source_msg_id and group_id and "接送網址" not in location and "驗證碼" not in location:
        raw = c.execute(
            "SELECT text FROM raw_messages WHERE group_id=? AND message_id=?",
            (group_id, source_msg_id),
        ).fetchone()
        if raw and raw[0]:
            urls, codes = _extract_event_reference_info(str(raw[0]))
            if urls:
                source_parts.append(f"接送網址：{'、'.join(urls)}")
            if codes:
                source_parts.append(f"驗證碼：{'、'.join(codes)}")
    return action, remind_at, "；".join(part for part in source_parts if part)


def _resolve_corrected_date(
    current_date: str,
    new_date: str | None,
    new_month_day: tuple[int, int] | None,
) -> str:
    current = datetime.strptime(current_date, "%Y-%m-%d").date()
    if new_date:
        return datetime.strptime(new_date, "%Y-%m-%d").date().isoformat()
    if new_month_day is None:
        return current.isoformat()

    month, day = (int(new_month_day[0]), int(new_month_day[1]))
    candidates: list[date] = []
    for year in (current.year - 1, current.year, current.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise ValueError("invalid corrected month/day")
    return min(
        candidates,
        key=lambda candidate: (
            abs((candidate - current).days),
            candidate < current,
        ),
    ).isoformat()


def _corrected_title_participants(title: str | None) -> list[str]:
    text = str(title or "")
    if "全家" in text:
        return ["全家"]
    aliases = (
        ("媽媽", "媽媽"),
        ("爸爸", "爸爸"),
        ("姊姊", "姊姊"),
        ("姐姐", "姊姊"),
        ("妹妹", "妹妹"),
        ("弟弟", "弟弟"),
        ("哥哥", "哥哥"),
        ("爺爺", "爺爺"),
        ("奶奶", "奶奶"),
        *line_mentions.configured_family_alias_mapping(include_short=True).items(),
    )
    participants: list[str] = []
    for alias, normalized in aliases:
        if alias in text and normalized not in participants:
            participants.append(normalized)
    return participants


def correct_quoted_event_and_reminder(
    group_id: str,
    quoted_message_id: str,
    *,
    new_date: str | None,
    new_month_day: tuple[int, int] | None,
    new_time: str | None,
    new_title: str | None,
    _resolved_event_id: str | None = None,
) -> dict:
    """Atomically correct one exact quoted event and its pending mirror."""

    if not group_id or (not quoted_message_id and not _resolved_event_id):
        return {"status": "not_found"}
    normalized_title = (
        re.sub(r"[\x00-\x1f\x7f]+", " ", str(new_title or "")).strip()
        if new_title is not None
        else None
    )
    if normalized_title is not None:
        normalized_title = re.sub(r"\s+", " ", normalized_title)
        if not normalized_title or len(normalized_title) > 80:
            return {"status": "invalid"}
    if new_time is not None:
        try:
            datetime.strptime(new_time, "%H:%M")
        except ValueError:
            return {"status": "invalid"}
    if (
        new_date is None
        and new_month_day is None
        and new_time is None
        and normalized_title is None
    ):
        return {"status": "invalid"}

    try:
        with _lock, _conn() as c:
            c.row_factory = sqlite3.Row
            c.execute("BEGIN IMMEDIATE")
            if _resolved_event_id:
                event_id = str(_resolved_event_id)
            else:
                identity_status, event_id = _resolve_quoted_event_id_conn(
                    c,
                    group_id,
                    quoted_message_id,
                )
                if identity_status != "resolved" or event_id is None:
                    return {"status": identity_status}

            event_row = c.execute(
                "SELECT * FROM events WHERE group_id=? AND event_id=?",
                (group_id, event_id),
            ).fetchone()
            if event_row is None:
                return {"status": "not_found"}
            if str(event_row["status"]) != "active":
                return {"status": "terminal"}

            mirror_rows = c.execute(
                "SELECT * FROM reminders WHERE group_id=? "
                "AND source_kind=? AND source_ref=? ORDER BY reminder_id",
                (group_id, EVENT_REMINDER_SOURCE_KIND, event_id),
            ).fetchall()
            if not mirror_rows:
                return {"status": "mirror_missing"}
            if len(mirror_rows) != 1:
                return {"status": "ambiguous"}
            reminder_row = mirror_rows[0]
            if str(reminder_row["status"]) != "pending":
                return {"status": "terminal"}

            live_claim = c.execute(
                "SELECT 1 FROM reminder_delivery_claims "
                "WHERE group_id=? AND state='sending' AND claimed_at>=? AND ("
                "(delivery_kind='calendar' AND subject_ref=?) OR "
                "(delivery_kind='natural' AND subject_ref=?) OR "
                "(source_kind=? AND source_ref=?)) LIMIT 1",
                (
                    group_id,
                    int(time.time()) - 15 * 60,
                    event_id,
                    str(reminder_row["reminder_id"]),
                    EVENT_REMINDER_SOURCE_KIND,
                    event_id,
                ),
            ).fetchone()
            if live_claim is not None:
                return {"status": "busy"}
            c.execute(
                "UPDATE reminder_delivery_claims SET state='uncertain' "
                "WHERE group_id=? AND state='sending' AND claimed_at<? AND ("
                "(delivery_kind='calendar' AND subject_ref=?) OR "
                "(delivery_kind='natural' AND subject_ref=?) OR "
                "(source_kind=? AND source_ref=?))",
                (
                    group_id,
                    int(time.time()) - 15 * 60,
                    event_id,
                    str(reminder_row["reminder_id"]),
                    EVENT_REMINDER_SOURCE_KIND,
                    event_id,
                ),
            )

            desired = dict(event_row)
            desired["event_date"] = _resolve_corrected_date(
                str(event_row["event_date"]),
                new_date,
                new_month_day,
            )
            desired["event_time"] = (
                new_time
                if new_time is not None
                else event_row["event_time"]
            )
            desired["title"] = (
                normalized_title
                if normalized_title is not None
                else str(event_row["title"])
            )
            corrected_participants = _corrected_title_participants(
                normalized_title
            )
            desired_mentions = str(reminder_row["mention_aliases"] or "[]")
            if normalized_title is not None:
                desired["participants"] = json.dumps(
                    corrected_participants,
                    ensure_ascii=False,
                )
                desired_mentions = json.dumps(
                    corrected_participants,
                    ensure_ascii=False,
                )
            action, remind_at, source_text = _corrected_event_payload_conn(
                c,
                desired,
            )

            event_changed = any(
                desired[key] != event_row[key]
                for key in ("title", "event_date", "event_time", "participants")
            )
            schedule_changed = any(
                desired[key] != event_row[key]
                for key in ("event_date", "event_time")
            )
            reminder_schedule_changed = (
                remind_at != int(reminder_row["remind_at"])
            )
            reminder_changed = any(
                (
                    action != str(reminder_row["action"]),
                    remind_at != int(reminder_row["remind_at"]),
                    source_text != str(reminder_row["source_text"] or ""),
                    desired_mentions
                    != str(reminder_row["mention_aliases"] or "[]"),
                )
            )
            if event_changed:
                if schedule_changed:
                    event_update = c.execute(
                        "UPDATE events SET title=?, event_date=?, event_time=?, "
                        "participants=?, reminded_at=NULL, "
                        "reminded_30d=NULL, reminded_7d=NULL, "
                        "reminded_3d=NULL, reminded_2d=NULL, "
                        "reminded_1d=NULL, reminded_0d=NULL "
                        "WHERE group_id=? AND event_id=? AND status='active'",
                        (
                            desired["title"],
                            desired["event_date"],
                            desired["event_time"],
                            desired["participants"],
                            group_id,
                            event_id,
                        ),
                    )
                else:
                    event_update = c.execute(
                        "UPDATE events SET title=?, event_date=?, event_time=?, "
                        "participants=? "
                        "WHERE group_id=? AND event_id=? AND status='active'",
                        (
                            desired["title"],
                            desired["event_date"],
                            desired["event_time"],
                            desired["participants"],
                            group_id,
                            event_id,
                        ),
                    )
                if event_update.rowcount != 1:
                    raise RuntimeError("quoted event compare-and-set failed")
            if reminder_changed:
                if reminder_schedule_changed:
                    reminder_update = c.execute(
                        "UPDATE reminders SET action=?, remind_at=?, "
                        "source_text=?, mention_aliases=?, last_pushed_at=0, "
                        "weekly_count=0, last_weekly_at=0, pushed_3d=0, "
                        "pushed_1d=0, pushed_4hr=0, pushed_2hr=0, "
                        "pushed_1hr=0, pushed_now=0 "
                        "WHERE group_id=? AND reminder_id=? AND status='pending' "
                        "AND source_kind=? AND source_ref=?",
                        (
                            action,
                            remind_at,
                            source_text,
                            desired_mentions,
                            group_id,
                            int(reminder_row["reminder_id"]),
                            EVENT_REMINDER_SOURCE_KIND,
                            event_id,
                        ),
                    )
                else:
                    reminder_update = c.execute(
                        "UPDATE reminders SET action=?, remind_at=?, "
                        "source_text=?, mention_aliases=? "
                        "WHERE group_id=? AND reminder_id=? AND status='pending' "
                        "AND source_kind=? AND source_ref=?",
                        (
                            action,
                            remind_at,
                            source_text,
                            desired_mentions,
                            group_id,
                            int(reminder_row["reminder_id"]),
                            EVENT_REMINDER_SOURCE_KIND,
                            event_id,
                        ),
                    )
                if reminder_update.rowcount != 1:
                    raise RuntimeError("quoted reminder compare-and-set failed")

            persisted_event = c.execute(
                "SELECT * FROM events WHERE group_id=? AND event_id=?",
                (group_id, event_id),
            ).fetchone()
            persisted_reminder = c.execute(
                "SELECT * FROM reminders WHERE group_id=? AND reminder_id=?",
                (group_id, int(reminder_row["reminder_id"])),
            ).fetchone()
            if persisted_event is None or persisted_reminder is None:
                raise RuntimeError("quoted correction persisted row missing")
            return {
                "status": (
                    "updated"
                    if event_changed or reminder_changed
                    else "unchanged"
                ),
                "event": dict(persisted_event),
                "reminder": dict(persisted_reminder),
            }
    except sqlite3.IntegrityError as exc:
        if "unique constraint failed" in str(exc).lower():
            return {"status": "conflict"}
        return {"status": "unavailable"}
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        return {"status": "unavailable"}


def correct_event_and_reminder_by_id(
    group_id: str,
    event_id: str,
    *,
    new_date: str | None,
    new_time: str | None,
    new_title: str | None,
) -> dict:
    """Atomically correct an already-resolved event and its exact mirror."""

    return correct_quoted_event_and_reminder(
        group_id,
        "",
        new_date=new_date,
        new_month_day=None,
        new_time=new_time,
        new_title=new_title,
        _resolved_event_id=event_id,
    )


def bind_event_source_message(
    group_id: str,
    event_id: str,
    source_msg_id: str,
) -> bool:
    """Bind a legacy event to its source without overwriting another source."""

    if not group_id or not event_id or not source_msg_id:
        return False
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT source_msg_id FROM events WHERE group_id=? AND event_id=? "
            "AND status='active'",
            (group_id, event_id),
        ).fetchone()
        if row is None:
            return False
        current = str(row[0] or "").strip()
        if current:
            return current == source_msg_id
        cur = c.execute(
            "UPDATE events SET source_msg_id=? WHERE group_id=? AND event_id=? "
            "AND status='active' AND COALESCE(source_msg_id, '')=''",
            (source_msg_id, group_id, event_id),
        )
        return cur.rowcount == 1


def find_active_events_exact(
    group_id: str,
    *,
    title: str,
    event_date: str,
    event_time: str | None,
) -> list[dict]:
    """Return every normalized-exact active event for a reminder reference.

    Titles use the same NFKC/whitespace/casefold contract as natural-reminder
    cancellation. Returning every match lets the caller fail closed when raw
    titles differ but normalize to the same user-visible value.
    """

    import reminder_cancel

    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id=? AND status='active'",
            (group_id,),
        ).fetchall()
    normalized_title = reminder_cancel.normalize_action(title)
    target_schedule = _event_schedule_minute_key(event_date, event_time)
    if not normalized_title or target_schedule is None:
        return []
    return [
        dict(row)
        for row in rows
        if reminder_cancel.normalize_action(row["title"]) == normalized_title
        and _event_schedule_minute_key(
            row["event_date"],
            row["event_time"],
        )
        == target_schedule
    ]


def _event_time_minute_key(value: object) -> tuple[int, int] | None:
    """Normalize stored ``H:MM``/``HH:MM`` values to one calendar minute."""

    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def _event_schedule_minute_key(
    event_date: object,
    event_time: object,
) -> tuple[int, int, int, int | None, int | None] | None:
    """Normalize a stored event schedule without trusting string padding."""

    date_parts = str(event_date or "").strip().split("-")
    if len(date_parts) != 3:
        return None
    try:
        year, month, day = (int(part) for part in date_parts)
        date(year, month, day)
    except (TypeError, ValueError):
        return None
    raw_time = str(event_time or "").strip()
    if not raw_time:
        return year, month, day, None, None
    time_key = _event_time_minute_key(raw_time)
    if time_key is None:
        return None
    return year, month, day, time_key[0], time_key[1]


def find_active_event_exact(
    group_id: str,
    *,
    title: str,
    event_date: str,
    event_time: str | None,
) -> dict | None:
    """Return one normalized-exact active event, or ``None`` if ambiguous."""

    rows = find_active_events_exact(
        group_id,
        title=title,
        event_date=event_date,
        event_time=event_time,
    )
    if len(rows) != 1:
        return None
    return rows[0]


def cancel_event(event_id: str) -> bool:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT group_id FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        cur = c.execute(
            "UPDATE events SET status = 'cancelled' WHERE event_id = ? AND status = 'active'",
            (event_id,),
        )
        ok = cur.rowcount > 0
    if ok and row:
        import memory

        try:
            _ensure_memory_db_path()
            memory.mark_reminder_done_for_source(
                row[0], EVENT_REMINDER_SOURCE_KIND, event_id
            )
        except Exception:
            pass
        return True
    return False


def update_event_schedule(
    event_id: str,
    new_date: str,
    new_time: str | None = None,
    title: str | None = None,
) -> bool:
    """Reschedule event；reset 所有 reminded 欄位（codex critical：rescheduled event
    必須重推所有 offset，否則之前推過的 flag 會壓住新提醒）。
    """
    title = (title or "").strip() or None
    updated = False
    event: dict[str, object] | None = None
    with _lock, _conn() as c:
        try:
            cur = c.execute(
                "UPDATE events SET title = COALESCE(?, title), event_date = ?, "
                "event_time = COALESCE(?, event_time), "
                "reminded_at = NULL, "
                "reminded_30d = NULL, reminded_7d = NULL, "
                "reminded_3d = NULL, reminded_2d = NULL, reminded_1d = NULL, "
                "reminded_0d = NULL "
                "WHERE event_id = ?",
                (title, new_date, new_time, event_id),
            )
        except sqlite3.IntegrityError:
            return False
        if cur.rowcount == 0:
            return False
        row = c.execute(
            "SELECT event_id, group_id, title, event_date, event_time, "
            "location, participants, source_msg_id, reminder_lead_days "
            "FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row:
            event = {
                "event_id": row[0],
                "group_id": row[1],
                "title": row[2],
                "event_date": row[3],
                "event_time": row[4],
                "location": row[5],
                "participants": row[6],
                "source_msg_id": row[7],
                "reminder_lead_days": row[8],
            }
            updated = True
    if updated and event:
        _upsert_event_reminder(event)
    return updated


def update_event_date(event_id: str, new_date: str, new_time: str | None = None) -> bool:
    return update_event_schedule(event_id, new_date, new_time)


def update_event_reminder_lead_days(event_id: str, lead_days: int | None) -> bool:
    normalized_days = None
    if lead_days is not None:
        try:
            normalized_days = int(lead_days)
        except (TypeError, ValueError):
            return False
        if not 0 <= normalized_days <= 365:
            return False
    event: dict[str, object] | None = None
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE events SET reminder_lead_days = ? WHERE event_id = ?",
            (normalized_days, event_id),
        )
        if cur.rowcount == 0:
            return False
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT event_id, group_id, title, event_date, event_time, "
            "location, participants, source_msg_id, reminder_lead_days "
            "FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row:
            event = dict(row)
    if event:
        _upsert_event_reminder(event)
    return True


def list_upcoming(group_id: str, days: int = 30) -> list[dict]:
    # 用 TW timezone today（避免 UTC host 跨日誤判 — codex/GP1 反饋）
    today_d = _today_tw()
    today = today_d.isoformat()
    until = (today_d + timedelta(days=days)).isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
            "AND event_date BETWEEN ? AND ? ORDER BY event_date, event_time",
            (group_id, today, until),
        ).fetchall()
    return [dict(r) for r in rows]


def list_past(group_id: str, days: int = 30) -> list[dict]:
    """過去 N 天的 active events（含 cancelled 的不在裡面）。最新日期優先。"""
    today_d = _today_tw()
    since = (today_d - timedelta(days=days)).isoformat()
    today_iso = today_d.isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
            "AND event_date >= ? AND event_date < ? "
            "ORDER BY event_date DESC, event_time DESC",
            (group_id, since, today_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def search_by_keyword(
    group_id: str, keywords: list[str], limit: int = 5
) -> list[dict]:
    """模糊比對 title / location / participants。bound params + LIKE escape。

    回傳排序：未來事件（接近今天優先）+ 過去事件（最新優先），用 Python union 兩段。
    GP1 反饋：純 DESC 會把遠未來放最前，user 問「什麼時候 X」通常想知道下一次。
    """
    if not keywords:
        return []
    today_iso = _today_tw().isoformat()
    # 每個 keyword 對 3 個欄位各跑一次 LIKE，OR 連起來
    conditions = []
    params: list = [group_id]
    for kw in keywords:
        pat = f"%{_escape_like(kw)}%"
        conditions.append(
            "(title LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\' "
            "OR participants LIKE ? ESCAPE '\\')"
        )
        params.extend([pat, pat, pat])
    where = " OR ".join(conditions)
    sql_future = (
        "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date >= ? AND ({where}) "
        "ORDER BY event_date ASC, event_time ASC LIMIT ?"
    )
    sql_past = (
        "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date < ? AND ({where}) "
        "ORDER BY event_date DESC, event_time DESC LIMIT ?"
    )
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        future_rows = c.execute(
            sql_future, [group_id, today_iso, *params[1:], limit]
        ).fetchall()
        past_rows = c.execute(
            sql_past, [group_id, today_iso, *params[1:], limit]
        ).fetchall()
    combined = [dict(r) for r in future_rows] + [dict(r) for r in past_rows]
    return combined[:limit]


def search_by_title_phrase(
    group_id: str, verb_noun_pairs: list[tuple[str, str]], limit: int = 5
) -> list[dict]:
    """Verb+noun phrase search on title column with `%verb%noun%` LIKE pattern.

    Example: pairs=[("拿","蛋糕")] → title LIKE '%拿%蛋糕%'
    Matches: '拿蛋糕' / '拿爸爸生日蛋糕' / '拿一個蛋糕'
    Skips: '蛋糕拿來' (wrong order), '蛋糕' (no verb)

    Same ordering rule as search_by_keyword: future ASC + past DESC.
    """
    if not verb_noun_pairs:
        return []
    today_iso = _today_tw().isoformat()
    conditions = []
    pattern_params: list = []
    for v, n in verb_noun_pairs:
        conditions.append("title LIKE ? ESCAPE '\\'")
        pattern_params.append(f"%{_escape_like(v)}%{_escape_like(n)}%")
    where = " OR ".join(conditions)
    sql_future = (
        f"SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date >= ? AND ({where}) "
        "ORDER BY event_date ASC, event_time ASC LIMIT ?"
    )
    sql_past = (
        f"SELECT * FROM events WHERE group_id = ? AND status = 'active' "
        f"AND event_date < ? AND ({where}) "
        "ORDER BY event_date DESC, event_time DESC LIMIT ?"
    )
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        future_rows = c.execute(
            sql_future, [group_id, today_iso, *pattern_params, limit]
        ).fetchall()
        past_rows = c.execute(
            sql_past, [group_id, today_iso, *pattern_params, limit]
        ).fetchall()
    combined = [dict(r) for r in future_rows] + [dict(r) for r in past_rows]
    return combined[:limit]


def list_due_for_reminder(group_id: str, days_ahead: int = 7) -> list[dict]:
    """回傳指定 group_id 中 event_date = today + days_ahead 且該 offset 的 reminded 欄位
    IS NULL 的 active events。

    days_ahead in REMINDER_OFFSETS (30/7/3/2/1/0) → 查對應 reminded_Xd column
    days_ahead = 其他值（如 legacy）→ 走舊 `reminded_at` graveyard column（向後相容）

    group_id 從 2026-05-27 multi-group 起 required —— 兩位 reviewer 都警告若漏 filter，
    multi-group 下 piggyback drain 路徑會把家族群 events 推到 mom 群 reply_token（isolation breach）。
    """
    target = (_today_tw() + timedelta(days=days_ahead)).isoformat()
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        if days_ahead in REMINDER_OFFSETS:
            col = _reminded_column(days_ahead)
            # column name 已被 _reminded_column whitelist 驗過 (SQL injection 防護)
            rows = c.execute(
                f"SELECT * FROM events WHERE group_id = ? AND status = 'active' "
                f"AND event_date = ? AND {col} IS NULL",
                (group_id, target),
            ).fetchall()
        else:
            # legacy path（保留 reminded_at 欄位向後相容） — group_id filter 同樣加上
            rows = c.execute(
                "SELECT * FROM events WHERE group_id = ? AND status = 'active' "
                "AND event_date = ? AND reminded_at IS NULL",
                (group_id, target),
            ).fetchall()
    events = [dict(r) for r in rows]
    if not events:
        return []

    # A user may cancel the reminder while keeping the calendar event active.
    # The cancelled source row is a durable tombstone shared by both reminder
    # senders; do not let event_reminder.py bypass that preference.
    import memory

    _ensure_memory_db_path()
    events = [
        event
        for event in events
        if ensure_event_reminder_mirror(event)
    ]
    return [
        event
        for event in events
        if not memory.is_reminder_source_cancelled(
            group_id,
            EVENT_REMINDER_SOURCE_KIND,
            str(event.get("event_id") or ""),
        )
    ]


def mark_reminded(event_id: str, days_ahead: int, group_id: str) -> None:
    """標記 offset 的 reminded 欄位為 now timestamp。group_id 從 2026-05-27 起 required
    作為防禦深度（event_id 是 PK 全表唯一，理論上單 WHERE 不會誤判，但 reviewer 兩位都建議
    強制 group_id 防 caller 漏傳）。

    days_ahead in REMINDER_OFFSETS → 寫 reminded_Xd
    其他值 → 寫 legacy reminded_at（向後相容）
    """
    ts = int(time.time() * 1000)
    with _lock, _conn() as c:
        if days_ahead in REMINDER_OFFSETS:
            col = _reminded_column(days_ahead)
            c.execute(
                f"UPDATE events SET {col} = ? WHERE event_id = ? AND group_id = ?",
                (ts, event_id, group_id),
            )
        else:
            c.execute(
                "UPDATE events SET reminded_at = ? WHERE event_id = ? AND group_id = ?",
                (ts, event_id, group_id),
            )


# 允許 import 時自動建表（跟 memory.py 同模式）
init_db()
