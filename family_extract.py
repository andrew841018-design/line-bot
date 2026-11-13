"""Read each family message once, in batches, for the TODO and anniversary jobs (2026-11-13).

Andrew 11/13 chose 「B：兩個都開」 (ops/state/line_bot_extractor_sweep_20261113/).  Before this, both
jobs compared raw_messages.created_at (seconds) with a millisecond cutoff and read nothing; fixed
alone, they would have sent every message to gemini-2.5-flash, the reply model's free requests.

- A cursor per job and group, (created_at, message_id) as the daily memory job keeps it, in the
  job's own DB (``extract_cursors``).  Each answered batch is written and the cursor moved in one
  transaction, with a compare-and-set on the cursor: two runs that start from the same cursor never
  both write.
- Only messages a job's own prefilter keeps are sent, in batches whose lines carry their own Taipei
  date, so 「明天」 counts from the day it was said.  The others are passed over, never re-read.
  Unsent (收回) messages are recorded in ``extract_unsent`` and never sent.
- Only ``gemini_client.side_task_last_tier``: the last tier's own quota and its reserve for replies;
  nothing is booked against the 2.5 models.  A ``response_schema`` keeps the answer an array.
- A batch moves on once settled: answered, blocked (the same prompt would be blocked again), or a
  bad answer ``max_failures`` times in a row.  No answer at all (quota, network) keeps the cursor.
- Logs and the stored summaries hold counts only: never family text, names or ids.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TW = ZoneInfo("Asia/Taipei")
_WEEKDAYS = "一二三四五六日"

READ_LIMIT = 2000  # every message a group keeps (memory._RAW_MESSAGE_KEEP)
SETTLE_SEC = 120  # rows this young may still have a same-second neighbour on the way
BATCH_MAX_MESSAGES = 50
BATCH_MAX_CHARS = 12_000
MODEL_TIMEOUT_MS = 60_000  # HttpOptions.timeout is in milliseconds (calendar_extractor uses 20_000)
MODEL_MAX_OUTPUT_TOKENS = 4096
UNSENT_KEEP_SEC = 30 * 86400

# jobs/daily_memory.py keeps the same two: media placeholders and long numbers stay out of prompts.
_PLACEHOLDER_RE = re.compile(r"\[(?:圖片|影片|音訊|檔案: [^\n]*|[A-Za-z]*MessageContent)\]")
_LONG_DIGITS_RE = re.compile(r"\d[\d\s-]{7,}\d")
_DATE_LIKE_RE = re.compile(r"(?<!\d)\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}(?!\d)")
_BLOCKED_FINISH = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "BLOCKED"})

_ON = frozenset({"", "on", "1", "true", "yes"})
_OFF = frozenset({"off", "0", "false", "no"})

CURSOR_DDL = (
    "CREATE TABLE IF NOT EXISTS extract_cursors ("
    " name TEXT NOT NULL,"
    " group_id TEXT NOT NULL,"
    " created_at INTEGER NOT NULL,"  # raw_messages.created_at: seconds
    " message_id TEXT NOT NULL,"
    " updated_at INTEGER NOT NULL,"  # seconds, like every time in this table
    " failures INTEGER NOT NULL DEFAULT 0,"
    " failure_key TEXT NOT NULL DEFAULT '',"
    " no_answer_since INTEGER NOT NULL DEFAULT 0,"
    " last_success_at INTEGER NOT NULL DEFAULT 0,"  # the last run that read everything it saw
    " last_run TEXT NOT NULL DEFAULT '{}',"  # counts only
    " last_push TEXT NOT NULL DEFAULT '{}',"  # counts only (the TODO push)
    " PRIMARY KEY (name, group_id))"
)
UNSENT_DDL = (
    "CREATE TABLE IF NOT EXISTS extract_unsent ("
    " group_id TEXT NOT NULL,"
    " message_id TEXT NOT NULL,"
    " unsent_at INTEGER NOT NULL,"  # seconds
    " PRIMARY KEY (group_id, message_id))"
)


def ensure_tables(conn) -> None:
    conn.execute(CURSOR_DDL)
    conn.execute(UNSENT_DDL)


def parse_mode(env_name: str, *, allowed: Sequence[str] = ("on", "off")) -> str:
    """Fail-closed switch: unset/on/1/true/yes = on; off/0/false/no = off; ``shadow`` where allowed;
    anything else is off, with a warning (read at call time)."""
    raw = os.environ.get(env_name, "").strip().lower()
    if raw in _ON:
        return "on"
    if raw in _OFF:
        return "off"
    if raw in allowed:
        return raw
    logger.warning("%s is not one of %s: treated as off", env_name, "/".join(allowed))
    return "off"


def tw_day(ts: float) -> date:
    return datetime.fromtimestamp(float(ts), _TW).date()


def tw_hour(ts: float) -> int:
    return datetime.fromtimestamp(float(ts), _TW).hour


@dataclass(frozen=True)
class Message:
    created_at: int
    message_id: str
    user_id: str | None
    text: str

    @property
    def day(self) -> date:
        return tw_day(self.created_at)


@dataclass
class RunResult:
    """Counts only: safe to log and to keep for the daily maintenance report."""

    mode: str = ""
    read: int = 0
    sent: int = 0
    requests: int = 0
    answered: int = 0
    new: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    stopped: str = ""
    gap_s: int = 0

    def skip(self, reason: str, n: int = 1) -> None:
        if n:
            self.skipped[reason] = self.skipped.get(reason, 0) + n

    def log_fields(self) -> str:
        skipped = ",".join(f"{k}={v}" for k, v in sorted(self.skipped.items())) or "-"
        return (
            f"mode={self.mode or '-'} read={self.read} sent={self.sent} requests={self.requests} "
            f"answered={self.answered} new={self.new} skipped={skipped} "
            f"stopped={self.stopped or '-'} gap_s={self.gap_s}"
        )


@dataclass
class Feature:
    """What one job adds to the shared reader.

    ``prepare`` validates the answer and does the read-only lookups in other modules' tables
    (outside the transaction).  ``write`` runs inside the batch transaction and may only use the
    connection it is given: it must drop rows whose message already produced data or was unsent."""

    name: str
    label: str
    prefilter: Callable[[Message, RunResult], bool]
    build_prompt: Callable[[Sequence[Message]], str]
    item_properties: dict
    required: tuple
    prepare: Callable[[Sequence[Message], list, RunResult], list]
    write: Callable[[object, list, RunResult], None]
    produced: Callable[[object, str, Sequence[str]], set]
    initial_cursor: Callable[[float], tuple[int, str]]
    max_requests: int
    max_failures: int
    message_max_chars: int = 500
    retry_wait_s: float = 0.0


def is_family_text(msg: Message) -> bool:
    """Words a family member typed: not the bot's replies, commands, media placeholders or links."""
    text = str(msg.text or "").strip()
    if not text or str(msg.user_id or "") == "__bot__":
        return False
    if text.startswith(("/", "／")) or _PLACEHOLDER_RE.fullmatch(text):
        return False
    import fact_filters

    return bool(fact_filters.LINK_RE.sub("", text).strip())


def scrub(text: str) -> str:
    """Links and long numbers out (as the daily memory job), dates such as 1956-06-04 kept."""
    import fact_filters

    kept: list[str] = []

    def hold(match: re.Match) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    out = _DATE_LIKE_RE.sub(hold, fact_filters.LINK_RE.sub("[連結]", str(text or "")))
    out = _LONG_DIGITS_RE.sub("[號碼]", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], out)


def format_batch(batch: Sequence[Message], max_chars: int = 500) -> str:
    """One JSON object per line: ``{"i": 3, "date": "2026-11-12", "weekday": "週四", "time": "21:05",
    "text": "…"}``.  No names, no ids; a message cannot break out of its own string."""
    lines = []
    for i, msg in enumerate(batch, 1):
        when = datetime.fromtimestamp(msg.created_at, _TW)
        text = " ".join(scrub(msg.text).split())[:max_chars]
        lines.append(json.dumps(
            {"i": i, "date": when.date().isoformat(), "weekday": f"週{_WEEKDAYS[when.weekday()]}",
             "time": f"{when:%H:%M}", "text": text},
            ensure_ascii=False,
        ))
    return "\n".join(lines)


def split_batches(msgs: Sequence[Message], max_chars_each: int = 500) -> list[list[Message]]:
    """Batches in message order; the limits are read at call time (tests shrink them)."""
    batches: list[list[Message]] = []
    current: list[Message] = []
    size = 0
    for msg in msgs:
        n = min(len(msg.text or ""), max_chars_each) + 40
        if current and (len(current) >= BATCH_MAX_MESSAGES or size + n > BATCH_MAX_CHARS):
            batches.append(current)
            current, size = [], 0
        current.append(msg)
        size += n
    if current:
        batches.append(current)
    return batches


def item_message(item, batch: Sequence[Message]) -> Message | None:
    """The batch message an answer item points at (``i`` is 1-based), or None."""
    if not isinstance(item, dict):
        return None
    i = item.get("i")
    if isinstance(i, bool) or not isinstance(i, int) or not 1 <= i <= len(batch):
        return None
    return batch[i - 1]


def _finish_name(resp) -> str:
    try:
        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            feedback = getattr(resp, "prompt_feedback", None)
            return "BLOCKED" if getattr(feedback, "block_reason", None) else "NO_CANDIDATE"
        finish = getattr(candidates[0], "finish_reason", None)
        return str(getattr(finish, "name", finish) or "")
    except Exception:
        return ""


def ask(prompt: str, feature: Feature) -> tuple[str, list]:
    """One last-tier request: ("ok", items) | ("no_answer", []) | ("blocked", []) | ("bad", [])."""
    import gemini_client
    from google.genai import types

    schema = {
        "type": "ARRAY",
        "items": {"type": "OBJECT", "properties": feature.item_properties, "required": list(feature.required)},
    }
    resp = gemini_client.side_task_last_tier(
        prompt,
        types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            temperature=0.1,
            max_output_tokens=MODEL_MAX_OUTPUT_TOKENS,
            http_options=types.HttpOptions(timeout=MODEL_TIMEOUT_MS),
        ),
        label=feature.label,
    )
    if resp is None:
        return "no_answer", []
    finish = _finish_name(resp)
    if finish in _BLOCKED_FINISH:
        return "blocked", []
    text = getattr(resp, "text", None)
    if finish != "STOP" or not text:
        return "bad", []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "bad", []
    return ("ok", data) if isinstance(data, list) else ("bad", [])


def unsent_ids(conn, group_id: str, message_ids: Sequence[str]) -> set[str]:
    ids = [m for m in message_ids if m]
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT message_id FROM extract_unsent WHERE group_id=? AND message_id IN ({marks})",
        (group_id, *ids),
    ).fetchall()
    return {str(r[0]) for r in rows}


def record_unsent(conn, group_id: str, message_id: str, now: float) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO extract_unsent(group_id, message_id, unsent_at) VALUES (?, ?, ?)",
        (group_id, message_id, int(now)),
    )
    conn.execute("DELETE FROM extract_unsent WHERE unsent_at < ?", (int(now) - UNSENT_KEEP_SEC,))


def _load(conn, name: str, group_id: str) -> dict | None:
    conn_row = conn.execute(
        "SELECT created_at, message_id, failures, failure_key, no_answer_since "
        "FROM extract_cursors WHERE name=? AND group_id=?",
        (name, group_id),
    ).fetchone()
    if conn_row is None:
        return None
    return dict(zip(("created_at", "message_id", "failures", "failure_key", "no_answer_since"), conn_row))


def save_last_push(conn_factory: Callable, name: str, group_id: str, summary: dict) -> None:
    with conn_factory() as c:
        ensure_tables(c)
        c.execute(
            "UPDATE extract_cursors SET last_push=? WHERE name=? AND group_id=?",
            (json.dumps(summary, sort_keys=True), name, group_id),
        )


def run(feature: Feature, group_id: str, conn_factory: Callable, *, now: float | None = None, mode: str = "on") -> RunResult:
    """Read the messages after this job's cursor and write what the model found, batch by batch.

    A locked database (another process holds it past the timeout) ends the run as ``db_busy``: not a
    failure of the batch, the cursor stays and the next run goes on."""
    import sqlite3

    try:
        return _run(feature, group_id, conn_factory, now=now, mode=mode)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        result = RunResult(mode=mode, stopped="db_busy")
        logger.warning("%s %s", feature.label, result.log_fields())
        return result


def _run(feature: Feature, group_id: str, conn_factory: Callable, *, now: float | None, mode: str) -> RunResult:
    import gemini_client
    import memory

    result = RunResult(mode=mode)
    now = time.time() if now is None else float(now)
    with conn_factory() as c:
        ensure_tables(c)
        start = feature.initial_cursor(now)
        c.execute(
            "INSERT OR IGNORE INTO extract_cursors(name, group_id, created_at, message_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (feature.name, group_id, int(start[0]), str(start[1]), int(now)),
        )
        state = _load(c, feature.name, group_id)
    cursor = (int(state["created_at"]), str(state["message_id"]))
    failures, failure_key = int(state["failures"]), str(state["failure_key"])
    no_answer_since = int(state["no_answer_since"])

    def finish_run(reason: str = "") -> RunResult:
        result.stopped = reason
        quiet = reason in ("no_answer", "not_ready")
        with conn_factory() as c:
            c.execute(
                "UPDATE extract_cursors SET updated_at=?, failures=?, failure_key=?, no_answer_since=?, "
                "last_run=?, last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END "
                "WHERE name=? AND group_id=?",
                (
                    int(now), failures, failure_key,
                    (no_answer_since or int(now)) if quiet else 0,
                    json.dumps(asdict(result), sort_keys=True),
                    0 if reason else 1, int(now), feature.name, group_id,
                ),
            )
        logger.info("%s %s", feature.label, result.log_fields())
        return result

    def advance(to: Message, rows: list) -> bool:
        """Write ``rows`` and move the cursor in one transaction; False when another run moved it."""
        nonlocal cursor, failures, failure_key, no_answer_since
        with conn_factory() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                if rows:
                    feature.write(c, rows, result)
                cur = c.execute(
                    "UPDATE extract_cursors SET created_at=?, message_id=?, updated_at=?, failures=0, "
                    "failure_key='', no_answer_since=0 "
                    "WHERE name=? AND group_id=? AND created_at=? AND message_id=?",
                    (int(to.created_at), str(to.message_id), int(now), feature.name, group_id,
                     cursor[0], cursor[1]),
                )
                if cur.rowcount != 1:
                    c.execute("ROLLBACK")
                    return False
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        cursor = (int(to.created_at), str(to.message_id))
        failures, failure_key, no_answer_since = 0, "", 0
        return True

    count, oldest = memory.raw_messages_stats(group_id)
    if cursor[0] > 0 and count >= READ_LIMIT and oldest is not None and oldest > cursor[0]:
        result.gap_s = int(oldest - cursor[0])
    rows = memory.raw_messages_after(group_id, cursor[0], cursor[1], int(now) - SETTLE_SEC, READ_LIMIT)
    msgs = [Message(int(r[0]), str(r[1]), r[2], str(r[3] or "")) for r in rows]
    result.read = len(msgs)
    with conn_factory() as c:
        gone = unsent_ids(c, group_id, [m.message_id for m in msgs])
        done = feature.produced(c, group_id, [m.message_id for m in msgs])
    chosen: list[Message] = []
    for msg in msgs:
        if not is_family_text(msg):
            result.skip("not_family_text")
        elif msg.message_id in gone:
            result.skip("unsent")
        elif msg.message_id in done:
            result.skip("produced_before")
        elif feature.prefilter(msg, result):
            chosen.append(msg)
    for batch in split_batches(chosen, feature.message_max_chars):
        if result.requests >= feature.max_requests:
            return finish_run("request_cap")
        if not gemini_client.side_task_last_tier_ready():
            return finish_run("not_ready")
        result.requests += 1
        status, items = ask(feature.build_prompt(batch), feature)
        if status == "no_answer" and feature.retry_wait_s > 0 and result.requests < feature.max_requests:
            time.sleep(feature.retry_wait_s)  # one more try after the side task's 60-second backoff
            if gemini_client.side_task_last_tier_ready():
                result.requests += 1
                status, items = ask(feature.build_prompt(batch), feature)
        if status == "no_answer":
            return finish_run("no_answer")
        prepared: list = []
        if status == "bad":
            key = batch[0].message_id
            failures = failures + 1 if key == failure_key else 1
            failure_key = key
            if failures < feature.max_failures:
                result.skip("bad_answer")
                return finish_run("bad_answer")
            result.skip("given_up", len(batch))
        elif status == "blocked":
            result.skip("blocked", len(batch))
        else:
            result.answered += 1
            result.sent += len(batch)
            prepared = feature.prepare(batch, items, result)
        if not advance(batch[-1], prepared):
            return finish_run("raced")
    if msgs and (cursor[0], cursor[1]) != (msgs[-1].created_at, msgs[-1].message_id):
        if not advance(msgs[-1], []):
            return finish_run("raced")
    return finish_run()


def status(conn, name: str, group_id: str, *, now: float, newest_raw: int | None, unread: int | None) -> dict:
    """Numbers only, from one cursor row (``conn`` may be a read-only connection)."""
    try:
        row = conn.execute(
            "SELECT created_at, updated_at, failures, no_answer_since, last_success_at, last_run, last_push "
            "FROM extract_cursors WHERE name=? AND group_id=?",
            (name, group_id),
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return {"ran": False}

    def hours(ts) -> float | None:
        return round(max(0.0, now - int(ts)) / 3600, 1) if ts else None

    def parsed(text) -> dict:
        try:
            value = json.loads(text or "{}")
            return value if isinstance(value, dict) else {}
        except ValueError:
            return {}

    return {
        "ran": True,
        "behind_h": round(max(0, int(newest_raw) - int(row[0])) / 3600, 1) if newest_raw and row[0] else 0.0,
        "unread": unread,
        "since_run_h": hours(row[1]),
        "failures": int(row[2]),
        "no_answer_h": hours(row[3]),
        "since_success_h": hours(row[4]),
        "last_run": parsed(row[5]),
        "last_push": parsed(row[6]),
    }
