"""Group poll helper for family LINE chat.

This module handles short-lived availability polls such as:
    爸爸想知道，今天晚上有誰可以去吃凱薩

It is deterministic and SQLite-backed so poll creation and vote updates do not
depend on LLM quota.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings

_DB_PATH = Path(settings.sqlite_path)
_ALIASES_PATH = Path(__file__).parent / "user_aliases.json"
_lock = threading.Lock()

ACTIVE_WINDOW_HOURS = 48
POLL_STATUS_OPEN = "open"
POLL_STATUS_CLOSED = "closed"
CHOICE_YES = "yes"
CHOICE_NO = "no"
CHOICE_MAYBE = "maybe"
CHOICES = (CHOICE_YES, CHOICE_NO, CHOICE_MAYBE)

_CHOICE_LABELS = {
    CHOICE_YES: "可以",
    CHOICE_NO: "不行",
    CHOICE_MAYBE: "不確定",
}

_DEFAULT_FAMILY_ALIASES = {
    "爸爸": "爸爸",
    "爸": "爸爸",
    "老爸": "爸爸",
    "媽媽": "媽媽",
    "媽咪": "媽媽",
    "老媽": "媽媽",
    "黃聖雅": "黃聖雅",
    "聖雅": "黃聖雅",
    "妹妹": "黃聖雅",
    "黃聖穎": "黃聖穎",
    "聖穎": "黃聖穎",
}

_POLL_REQUEST_RE = re.compile(
    r"("
    r"(?:想知道|想問|問一下|統計一下|調查一下|確認一下).{0,30}"
    r"(?:有誰|誰|幾個人|多少人|大家).{0,10}"
    r"(?:可以|能不能|能|要不要|方便)"
    r"|"
    r"(?:有誰|誰|幾個人|多少人|大家).{0,10}"
    r"(?:可以|能不能|能|要不要|方便).{1,40}"
    r")"
)
_POLL_INTENT_WORD_RE = re.compile(r"民調|投票|統計一下|調查一下")
_QUESTION_PREFIX_RE = re.compile(
    r"^(?:(?:爸爸|媽媽|黃聖雅|黃聖穎|聖雅|聖穎|妹妹)\s*)?"
    r"(?:想知道|想問|問一下|統計一下|調查一下|確認一下)"
    r"[，,：:\s]*"
)
_NO_RE = re.compile(
    r"不行|不可以|不能|沒辦法|無法|不去|不吃|不要|不方便|pass|不了"
)
_MAYBE_RE = re.compile(
    r"不確定|不知道|再看看|看情況|可能|應該|大概|也許|maybe|待確認"
)
_YES_RE = re.compile(
    r"(?<!不)(?:可以|方便|ok|OK|Ok|\+1|加一|"
    r"會去|要去|我去|我也去|我吃|我也吃|我要|我也要|"
    r"爸爸要|媽媽要|聖雅要|聖穎要|爸爸去|媽媽去|聖雅去|聖穎去|"
    r"好啊|好喔|好啦|^好$|^可$)"
)
_VOTE_SEGMENT_SPLIT_RE = re.compile(r"[，,、。；;\n]+")
_QUESTION_MARK_RE = re.compile(r"[嗎嘛?？]\s*$")
_COUNT_RE = re.compile(r"(兩|二|2|三|3|四|4|五|5)\s*(?:個|人)?")
_COMMAND_PREFIXES = ("/民調", "/投票")
_STATUS_COMMANDS = ("/民調", "/投票", "/民調狀態", "/看民調", "/投票狀態")
_NUDGE_COMMANDS = ("/催民調", "/提醒民調")
_CLOSE_COMMANDS = ("/關閉民調", "/結束民調", "/取消民調")
_EXPLICIT_CREATE_RE = re.compile(
    r"^(?:(?:(?:請|麻煩|可以)?\s*幫(?:我|我們|大家)?\s*)|(?:麻煩你?|請你?)\s*)?"
    r"(?:(?:做|開|建立|建|發起|弄)\s*)?"
    r"(?:一個|個|一下|下)?\s*(?:民調|投票)(?:\s*[:：,，]\s*|\s+|$)(.*)$"
)
_EXPLICIT_NON_VOTE_RE = re.compile(
    r"幫我|幫忙|請問|查|搜尋|分析|解釋|怎麼|為什麼|可不可以|能不能"
)
_POLL_CLOSE_WORD_RE = re.compile(r"關掉|關閉|結束|取消|停掉|停止")


@dataclass(frozen=True)
class VoteTarget:
    key: str
    alias: str


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS family_polls (
                poll_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        TEXT NOT NULL,
                question        TEXT NOT NULL,
                topic           TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'open',
                created_by_user TEXT NOT NULL DEFAULT '',
                created_by_alias TEXT NOT NULL DEFAULT '',
                source_msg_id   TEXT NOT NULL DEFAULT '',
                source_text     TEXT,
                created_at      INTEGER NOT NULL,
                closed_at       INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_family_polls_active
                ON family_polls(group_id, status, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_family_polls_source
                ON family_polls(group_id, source_msg_id)
                WHERE source_msg_id != '';

            CREATE TABLE IF NOT EXISTS family_poll_votes (
                poll_id       INTEGER NOT NULL,
                voter_key     TEXT NOT NULL,
                voter_alias   TEXT NOT NULL,
                choice        TEXT NOT NULL,
                source_msg_id TEXT NOT NULL DEFAULT '',
                source_text   TEXT,
                updated_at    INTEGER NOT NULL,
                PRIMARY KEY (poll_id, voter_key)
            );
            CREATE INDEX IF NOT EXISTS idx_family_poll_votes_poll
                ON family_poll_votes(poll_id, choice);
            """
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_aliases() -> dict[str, str]:
    if not _ALIASES_PATH.exists():
        return {}
    try:
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if str(v).strip()}
    except Exception:
        return {}
    return {}


def alias_from_user_id(user_id: str | None) -> str:
    if not user_id:
        return ""
    return _load_aliases().get(user_id, "")


def _known_family_names() -> list[str]:
    names = set(_DEFAULT_FAMILY_ALIASES.values())
    names.update(_load_aliases().values())
    return sorted(names)


def _alias_patterns() -> list[tuple[str, str]]:
    mapping = dict(_DEFAULT_FAMILY_ALIASES)
    for alias in _load_aliases().values():
        mapping.setdefault(alias, alias)
    return sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True)


def _target_for_alias(alias: str) -> VoteTarget:
    return VoteTarget(key=f"alias:{alias}", alias=alias)


def _clean_sender_alias(sender_alias: str | None) -> str:
    alias = (sender_alias or "").strip()
    return alias if alias and alias not in {"某人"} else ""


def _target_for_user(
    user_id: str | None,
    *,
    sender_alias: str | None = None,
) -> VoteTarget | None:
    alias = alias_from_user_id(user_id)
    if alias:
        return _target_for_alias(alias)
    alias = _clean_sender_alias(sender_alias) or "群組成員"
    if not user_id:
        return _target_for_alias(alias)
    return VoteTarget(key=f"user:{user_id}", alias=alias)


def _normalize_question(text: str) -> str:
    s = (text or "").strip()
    s = _QUESTION_PREFIX_RE.sub("", s).strip(" ，,。")
    if not s:
        s = (text or "").strip()
    if not s.endswith(("?", "？")):
        s += "？"
    return s[:160]


def _topic_from_question(question: str) -> str:
    s = question.strip(" ?？。")
    for marker in ("去吃", "吃", "參加", "去"):
        idx = s.find(marker)
        if idx >= 0 and idx + len(marker) < len(s):
            topic = s[idx + len(marker) :].strip(" ，,。?？")
            if topic:
                return topic[:60]
    s = re.sub(r"^(?:今天|今晚|今天晚上|明天|明天晚上|這週|週末|下週)\s*", "", s)
    return s[:60]


def looks_like_poll_request(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < 4 or len(s) > 200:
        return False
    if _POLL_REQUEST_RE.search(s):
        return True
    return bool(_POLL_INTENT_WORD_RE.search(s) and ("可以" in s or "要不要" in s))


def create_poll(
    group_id: str,
    question: str,
    *,
    user_id: str | None = None,
    sender_alias: str | None = None,
    source_msg_id: str = "",
    source_text: str | None = None,
) -> dict:
    question = _normalize_question(question)
    topic = _topic_from_question(question)
    now = _now_ms()
    created_by_alias = alias_from_user_id(user_id) or _clean_sender_alias(sender_alias)
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO family_polls "
            "(group_id, question, topic, status, created_by_user, created_by_alias, "
            "source_msg_id, source_text, created_at) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (
                group_id,
                question,
                topic,
                user_id or "",
                created_by_alias,
                source_msg_id or "",
                (source_text or "")[:500] or None,
                now,
            ),
        )
        if cur.rowcount > 0:
            poll_id = int(cur.lastrowid)
        elif source_msg_id:
            row = c.execute(
                "SELECT poll_id FROM family_polls "
                "WHERE group_id=? AND source_msg_id=?",
                (group_id, source_msg_id),
            ).fetchone()
            poll_id = int(row[0]) if row else 0
        else:
            poll_id = 0
    return get_poll(poll_id) if poll_id else get_active_poll(group_id)


def try_create_poll_from_text(
    group_id: str,
    text: str,
    *,
    user_id: str | None = None,
    sender_alias: str | None = None,
    source_msg_id: str = "",
) -> dict | None:
    if not looks_like_poll_request(text):
        return None
    question = _normalize_question(text)
    return create_poll(
        group_id,
        question,
        user_id=user_id,
        sender_alias=sender_alias,
        source_msg_id=source_msg_id,
        source_text=text,
    )


def get_poll(poll_id: int) -> dict | None:
    if not poll_id:
        return None
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM family_polls WHERE poll_id=?",
            (poll_id,),
        ).fetchone()
    return dict(row) if row else None


def get_active_poll(group_id: str) -> dict | None:
    cutoff = _now_ms() - ACTIVE_WINDOW_HOURS * 3600 * 1000
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM family_polls "
            "WHERE group_id=? AND status='open' AND created_at>=? "
            "ORDER BY created_at DESC, poll_id DESC LIMIT 1",
            (group_id, cutoff),
        ).fetchone()
    return dict(row) if row else None


def close_active_poll(group_id: str) -> dict | None:
    poll = get_active_poll(group_id)
    if not poll:
        return None
    with _lock, _conn() as c:
        c.execute(
            "UPDATE family_polls SET status='closed', closed_at=? WHERE poll_id=?",
            (_now_ms(), poll["poll_id"]),
        )
    closed = get_poll(int(poll["poll_id"]))
    return closed or poll


def _choice_from_text(text: str) -> str | None:
    s = (text or "").strip()
    if not s or len(s) > 120:
        return None
    if _QUESTION_MARK_RE.search(s):
        return None
    if _NO_RE.search(s):
        return CHOICE_NO
    if _MAYBE_RE.search(s):
        return CHOICE_MAYBE
    if _YES_RE.search(s):
        return CHOICE_YES
    return None


def _targets_from_text(
    text: str,
    user_id: str | None,
    *,
    sender_alias: str | None = None,
) -> list[VoteTarget]:
    found: list[VoteTarget] = []
    seen: set[str] = set()
    for raw, canonical in _alias_patterns():
        if raw and raw in text and canonical not in seen:
            target = _target_for_alias(canonical)
            found.append(target)
            seen.add(canonical)
    sender = _target_for_user(user_id, sender_alias=sender_alias)
    has_first_person = "我" in text or "偶" in text
    if sender and (has_first_person or not found):
        if sender.key not in {t.key for t in found}:
            found.append(sender)
    return found


def _ambiguous_group_count(text: str) -> int:
    if not ("我們" in text or "兩個" in text or "2個" in text or "2人" in text):
        return 0
    m = _COUNT_RE.search(text)
    if not m:
        return 0
    raw = m.group(1)
    return {"兩": 2, "二": 2, "2": 2, "三": 3, "3": 3, "四": 4, "4": 4, "五": 5, "5": 5}.get(raw, 0)


def record_vote_from_text(
    group_id: str,
    text: str,
    *,
    user_id: str | None = None,
    sender_alias: str | None = None,
    source_msg_id: str = "",
) -> dict | None:
    poll = get_active_poll(group_id)
    if not poll:
        return None
    updates: list[tuple[VoteTarget, str]] = []
    segments = [seg.strip() for seg in _VOTE_SEGMENT_SPLIT_RE.split(text) if seg.strip()]
    for segment in segments or [text]:
        choice = _choice_from_text(segment)
        if choice is None:
            continue
        targets = _targets_from_text(segment, user_id, sender_alias=sender_alias)
        for target in targets:
            updates.append((target, choice))
    if not updates:
        choice = _choice_from_text(text)
        targets = (
            _targets_from_text(text, user_id, sender_alias=sender_alias)
            if choice
            else []
        )
        updates = [(target, choice) for target in targets if choice]
    if not updates:
        return None
    now = _now_ms()
    with _lock, _conn() as c:
        for target, choice in updates:
            c.execute(
                "INSERT INTO family_poll_votes "
                "(poll_id, voter_key, voter_alias, choice, source_msg_id, source_text, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(poll_id, voter_key) DO UPDATE SET "
                "voter_alias=excluded.voter_alias, choice=excluded.choice, "
                "source_msg_id=excluded.source_msg_id, source_text=excluded.source_text, "
                "updated_at=excluded.updated_at",
                (
                    poll["poll_id"],
                    target.key,
                    target.alias,
                    choice,
                    source_msg_id or "",
                    (text or "")[:500] or None,
                    now,
                ),
            )
    return {
        "poll": poll,
        "updates": [(target.alias, choice) for target, choice in updates],
        "choice": updates[0][1],
        "targets": [target.alias for target, _ in updates],
        "ambiguous_count": _ambiguous_group_count(text),
    }


def _votes_for_poll(poll_id: int) -> list[dict]:
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM family_poll_votes WHERE poll_id=? ORDER BY updated_at ASC",
            (poll_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def summarize_poll(poll: dict) -> dict[str, list[str]]:
    votes = _votes_for_poll(int(poll["poll_id"]))
    out = {choice: [] for choice in CHOICES}
    for vote in votes:
        choice = vote.get("choice")
        if choice in out:
            out[choice].append(vote.get("voter_alias") or vote.get("voter_key") or "")
    return out


def _names_text(names: list[str]) -> str:
    clean = [n for n in names if n]
    return "、".join(clean) if clean else "0"


def _pending_names(summary: dict[str, list[str]]) -> list[str]:
    voted = set()
    for names in summary.values():
        voted.update(names)
    return [name for name in _known_family_names() if name not in voted]


def format_summary(poll: dict, *, include_pending: bool = True) -> str:
    summary = summarize_poll(poll)
    yes = summary[CHOICE_YES]
    no = summary[CHOICE_NO]
    maybe = summary[CHOICE_MAYBE]
    lines = [
        f"民調：{poll['question']}",
        f"可以 {len(yes)} 人：{_names_text(yes)}",
        f"不行 {len(no)} 人：{_names_text(no)}",
        f"不確定 {len(maybe)} 人：{_names_text(maybe)}",
    ]
    if include_pending:
        pending = _pending_names(summary)
        if pending:
            lines.append(f"尚未回覆：{_names_text(pending)}")
    return "\n".join(lines)


def format_created_reply(poll: dict) -> str:
    return (
        f"@all 民調開好了：{poll['question']}\n"
        "請直接回「可以 / 不行 / 不確定」。也可以回「媽媽可以、爸爸不行」。\n\n"
        f"{format_summary(poll)}"
    )


def format_vote_reply(result: dict) -> str:
    poll = get_poll(int(result["poll"]["poll_id"])) or result["poll"]
    updates = result.get("updates") or [
        (alias, result["choice"]) for alias in result.get("targets", [])
    ]
    update_text = "\n".join(
        f"{alias} → {_CHOICE_LABELS.get(choice, choice)}"
        for alias, choice in updates
    )
    lines = [update_text, format_summary(poll)]
    if result.get("ambiguous_count", 0) > len(result["targets"]):
        lines.append("如果你是幫多個人回，請直接寫名字，例如「爸爸和媽媽可以」。")
    return "\n\n".join(lines)


def format_nudge_reply(poll: dict) -> str:
    return (
        f"@all 民調更新：{poll['question']}\n"
        "還沒回的人請直接回「可以 / 不行 / 不確定」。\n\n"
        f"{format_summary(poll)}"
    )


def handle_command(
    group_id: str,
    text: str,
    *,
    user_id: str | None = None,
    sender_alias: str | None = None,
    source_msg_id: str = "",
) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    for prefix in _COMMAND_PREFIXES:
        if t.startswith(prefix + " "):
            question = t[len(prefix) :].strip()
            if not question:
                return f"用法：{prefix} <要統計的事情>"
            poll = create_poll(
                group_id,
                question,
                user_id=user_id,
                sender_alias=sender_alias,
                source_msg_id=source_msg_id,
                source_text=text,
            )
            return format_created_reply(poll)
    if t in _STATUS_COMMANDS:
        poll = get_active_poll(group_id)
        if not poll:
            return "目前沒有進行中的民調。用法：/民調 <要統計的事情>"
        return format_summary(poll)
    if t in _NUDGE_COMMANDS:
        poll = get_active_poll(group_id)
        if not poll:
            return "目前沒有進行中的民調。"
        return format_nudge_reply(poll)
    if any(t.startswith(prefix) for prefix in _CLOSE_COMMANDS):
        poll = close_active_poll(group_id)
        if not poll:
            return "目前沒有進行中的民調可以關閉。"
        return "已關閉民調。\n" + format_summary(poll, include_pending=False)
    return None


def _parse_explicit_create_request(text: str) -> tuple[bool, str]:
    m = _EXPLICIT_CREATE_RE.match((text or "").strip())
    if not m:
        return False, ""
    return True, (m.group(1) or "").strip(" ：:,，")


def _looks_like_close_request(text: str) -> bool:
    s = (text or "").strip()
    if not s or len(s) > 50:
        return False
    return bool(("民調" in s or "投票" in s) and _POLL_CLOSE_WORD_RE.search(s))


def _looks_like_explicit_vote(text: str) -> bool:
    s = (text or "").strip()
    if not s or len(s) > 60 or _EXPLICIT_NON_VOTE_RE.search(s):
        return False
    return _choice_from_text(s) is not None


def handle_explicit_message(
    group_id: str,
    text: str,
    *,
    user_id: str | None = None,
    sender_alias: str | None = None,
    source_msg_id: str = "",
) -> str | None:
    command_reply = handle_command(
        group_id,
        text,
        user_id=user_id,
        sender_alias=sender_alias,
        source_msg_id=source_msg_id,
    )
    if command_reply is not None:
        return command_reply

    is_create_request, question = _parse_explicit_create_request(text)
    if is_create_request:
        if not question:
            return "要開民調請這樣說：幫我做民調：<要統計的事情>"
        poll = create_poll(
            group_id,
            question,
            user_id=user_id,
            sender_alias=sender_alias,
            source_msg_id=source_msg_id,
            source_text=text,
        )
        return format_created_reply(poll)

    if _looks_like_close_request(text):
        poll = close_active_poll(group_id)
        if not poll:
            return "目前沒有進行中的民調可以關閉。"
        return "已關閉民調。\n" + format_summary(poll, include_pending=False)

    if not _looks_like_explicit_vote(text):
        return None

    result = record_vote_from_text(
        group_id,
        text,
        user_id=user_id,
        sender_alias=sender_alias,
        source_msg_id=source_msg_id,
    )
    if result:
        return format_vote_reply(result)
    return None


def handle_natural_message(
    group_id: str,
    text: str,
    *,
    user_id: str | None = None,
    sender_alias: str | None = None,
    source_msg_id: str = "",
) -> str | None:
    poll = try_create_poll_from_text(
        group_id,
        text,
        user_id=user_id,
        sender_alias=sender_alias,
        source_msg_id=source_msg_id,
    )
    if poll:
        return format_created_reply(poll)
    result = record_vote_from_text(
        group_id,
        text,
        user_id=user_id,
        sender_alias=sender_alias,
        source_msg_id=source_msg_id,
    )
    if result:
        return format_vote_reply(result)
    return None


def clear_group(group_id: str) -> int:
    with _lock, _conn() as c:
        poll_ids = [
            r[0]
            for r in c.execute(
                "SELECT poll_id FROM family_polls WHERE group_id=?",
                (group_id,),
            ).fetchall()
        ]
        deleted_votes = 0
        for poll_id in poll_ids:
            cur = c.execute("DELETE FROM family_poll_votes WHERE poll_id=?", (poll_id,))
            deleted_votes += cur.rowcount
        cur = c.execute("DELETE FROM family_polls WHERE group_id=?", (group_id,))
        return cur.rowcount + deleted_votes


init_db()
