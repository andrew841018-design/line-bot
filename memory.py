"""
SQLite-backed 對話 context + 長期記憶 + 過濾器規則。

本地 Mac 部署用，stdlib sqlite3 無需額外服務（取代原本的 Upstash Redis）。Schema：

    context       (group_id, seq, role, text)             LIST-like，最近 N 輪對話
    facts         (group_id, fact)                         SET-like，長期事實（去重）
    counters      (group_id, msg_count)                    每群組訊息計數器
    raw_messages  (group_id, message_id, user_id, text)    所有看過的原始訊息，供 quote 回查
    filter_rules  (group_id, rule_id, kind, pattern, ...)  過濾器規則（skip / must_answer）
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from config import settings

_DB_PATH = Path(settings.sqlite_path)
if _DB_PATH.parent and str(_DB_PATH.parent) not in ("", "."):
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# sqlite3 在多 thread 寫入時需要 serialize，用一個全域 lock 最單純
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    # check_same_thread=False：uvicorn 會從不同 worker thread 呼進來
    # isolation_level=None：autocommit，我們用 context manager 的 lock 控制一致性
    conn = sqlite3.connect(_DB_PATH, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS context (
                group_id TEXT NOT NULL,
                seq      INTEGER NOT NULL,
                role     TEXT NOT NULL,
                text     TEXT NOT NULL,
                PRIMARY KEY (group_id, seq)
            );
            CREATE TABLE IF NOT EXISTS facts (
                group_id TEXT NOT NULL,
                fact     TEXT NOT NULL,
                PRIMARY KEY (group_id, fact)
            );
            CREATE TABLE IF NOT EXISTS counters (
                group_id  TEXT PRIMARY KEY,
                msg_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS raw_messages (
                group_id    TEXT NOT NULL,
                message_id  TEXT NOT NULL,
                user_id     TEXT,
                text        TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (group_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_raw_messages_time
                ON raw_messages(group_id, created_at);
            CREATE TABLE IF NOT EXISTS filter_rules (
                group_id   TEXT NOT NULL,
                rule_id    INTEGER NOT NULL,
                kind       TEXT NOT NULL,  -- 'skip' | 'must_answer'
                pattern    TEXT NOT NULL,
                source     TEXT NOT NULL,  -- 'user' | 'learned'
                created_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, rule_id)
            );
            CREATE TABLE IF NOT EXISTS rule_drafts (
                group_id   TEXT NOT NULL,
                draft_id   INTEGER NOT NULL,
                kind       TEXT NOT NULL,  -- 'skip' | 'must_answer'
                pattern    TEXT NOT NULL,
                reason     TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, draft_id)
            );
            CREATE TABLE IF NOT EXISTS persona_notes (
                group_id   TEXT NOT NULL,
                note_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT NOT NULL,  -- 'example' | 'correction'
                scenario   TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_persona_notes_group
                ON persona_notes(group_id, kind);
            """
        )


_init_db()


# ── Context（短期對話歷史）────────────────────────────────────────────────────

def append_turn(group_id: str, role: str, text: str) -> None:
    """role: 'user' | 'bot'。超過 context_rounds*2 筆會自動截掉最舊的。"""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM context WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        next_seq = row[0] + 1
        c.execute(
            "INSERT INTO context(group_id, seq, role, text) VALUES (?, ?, ?, ?)",
            (group_id, next_seq, role, text),
        )
        keep = settings.context_rounds * 2
        c.execute(
            "DELETE FROM context WHERE group_id = ? AND seq <= ?",
            (group_id, next_seq - keep),
        )


def get_context(group_id: str) -> list[tuple[str, str]]:
    """回傳 [(role, text), ...]，舊→新。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT role, text FROM context WHERE group_id = ? ORDER BY seq ASC",
            (group_id,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


# ── Facts（長期記憶）──────────────────────────────────────────────────────────

def add_fact(group_id: str, fact: str) -> bool:
    """回傳是否真的新增（False 代表重複或空字串）。"""
    fact = fact.strip()
    if not fact:
        return False
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO facts(group_id, fact) VALUES (?, ?)",
            (group_id, fact),
        )
        return cur.rowcount > 0


def remove_fact(group_id: str, fact_substring: str) -> int:
    """刪除所有「包含該子字串」的事實，回傳刪幾筆。"""
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM facts WHERE group_id = ? AND fact LIKE ?",
            (group_id, f"%{fact_substring}%"),
        )
        return cur.rowcount


def list_facts(group_id: str) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT fact FROM facts WHERE group_id = ? ORDER BY fact",
            (group_id,),
        ).fetchall()
        return [r[0] for r in rows]


def clear_facts(group_id: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM facts WHERE group_id = ?", (group_id,))
        return cur.rowcount


def top_facts(group_id: str) -> list[str]:
    """給 prompt 注入用，取前 max_facts_in_prompt 條。"""
    return list_facts(group_id)[: settings.max_facts_in_prompt]


# ── 計數器（決定何時觸發事實抽取）──────────────────────────────────────────

_RAW_MESSAGE_KEEP = 2000  # 每群組保留最近 N 筆原始訊息（給 quote-reply 查詢用）


def log_raw_message(
    group_id: str, message_id: str, user_id: str | None, text: str
) -> None:
    """記錄原始訊息，供之後 quote-reply 時查詢。超過 _RAW_MESSAGE_KEEP 筆自動汰舊。"""
    if not message_id or not text:
        return
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO raw_messages"
            "(group_id, message_id, user_id, text, created_at) "
            "VALUES (?, ?, ?, ?, strftime('%s','now'))",
            (group_id, message_id, user_id, text),
        )
        # 汰舊：只保留最近 _RAW_MESSAGE_KEEP 筆
        c.execute(
            "DELETE FROM raw_messages WHERE group_id = ? AND message_id NOT IN "
            "(SELECT message_id FROM raw_messages WHERE group_id = ? "
            " ORDER BY created_at DESC LIMIT ?)",
            (group_id, group_id, _RAW_MESSAGE_KEEP),
        )


def get_raw_message(
    group_id: str, message_id: str
) -> tuple[str | None, str] | None:
    """查原始訊息。回傳 (user_id, text) 或 None。"""
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, text FROM raw_messages "
            "WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
        if row:
            return (row[0], row[1])
        return None


def bump_and_should_extract(group_id: str) -> bool:
    """每呼叫一次 +1；每 fact_extract_every 次回傳一次 True。"""
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO counters(group_id, msg_count) VALUES (?, 1) "
            "ON CONFLICT(group_id) DO UPDATE SET msg_count = msg_count + 1",
            (group_id,),
        )
        row = c.execute(
            "SELECT msg_count FROM counters WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        return row[0] % settings.fact_extract_every == 0


def get_recent_raw_messages(
    group_id: str, limit: int = 10
) -> list[tuple[str, str | None, str, int]]:
    """取最近 N 筆原始訊息（新→舊→再反轉成舊→新）。

    回傳 [(message_id, user_id, text, created_at), ...]，順序為舊→新，
    給 burst classifier / look-back 用。
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT message_id, user_id, text, created_at FROM raw_messages "
            "WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
    return list(reversed([(r[0], r[1], r[2], r[3]) for r in rows]))


def get_last_bot_reply(group_id: str) -> tuple[str, str] | None:
    """拿最近一則 bot 自己發過的訊息，回傳 (message_id, text) 或 None。
    給 /閉嘴 指令用，用於找出「上一則要被糾正的 bot 回覆」。"""
    with _conn() as c:
        row = c.execute(
            "SELECT message_id, text FROM raw_messages "
            "WHERE group_id = ? AND user_id = '__bot__' "
            "ORDER BY created_at DESC LIMIT 1",
            (group_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


# ── Filter rules（過濾器的學習結果）────────────────────────────────────────

def add_filter_rule(
    group_id: str, kind: str, pattern: str, source: str = "user"
) -> int:
    """新增規則，回傳分配到的 rule_id。kind: 'skip' | 'must_answer'。"""
    assert kind in ("skip", "must_answer")
    assert source in ("user", "learned")
    pattern = pattern.strip()
    if not pattern:
        return 0
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(rule_id), 0) FROM filter_rules WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        next_id = row[0] + 1
        c.execute(
            "INSERT INTO filter_rules"
            "(group_id, rule_id, kind, pattern, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, strftime('%s','now'))",
            (group_id, next_id, kind, pattern, source),
        )
        return next_id


def list_filter_rules(group_id: str) -> list[dict]:
    """回傳所有規則（舊→新），每筆是 {rule_id, kind, pattern, source}。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT rule_id, kind, pattern, source FROM filter_rules "
            "WHERE group_id = ? ORDER BY rule_id ASC",
            (group_id,),
        ).fetchall()
    return [
        {"rule_id": r[0], "kind": r[1], "pattern": r[2], "source": r[3]}
        for r in rows
    ]


def delete_filter_rule(group_id: str, rule_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM filter_rules WHERE group_id = ? AND rule_id = ?",
            (group_id, rule_id),
        )
        return cur.rowcount > 0


def clear_filter_rules(group_id: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM filter_rules WHERE group_id = ?", (group_id,)
        )
        return cur.rowcount


# ── Rule drafts（Layer 3 週期性自我檢討的候選規則）──────────────────────────

def add_rule_draft(
    group_id: str, kind: str, pattern: str, reason: str = ""
) -> int:
    """新增一筆 draft，回傳 draft_id。kind: 'skip' | 'must_answer'。"""
    assert kind in ("skip", "must_answer")
    pattern = pattern.strip()
    if not pattern:
        return 0
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COALESCE(MAX(draft_id), 0) FROM rule_drafts WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        next_id = row[0] + 1
        c.execute(
            "INSERT INTO rule_drafts"
            "(group_id, draft_id, kind, pattern, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, strftime('%s','now'))",
            (group_id, next_id, kind, pattern, reason.strip()),
        )
        return next_id


def list_rule_drafts(group_id: str) -> list[dict]:
    """回傳所有 draft（舊→新），每筆 {draft_id, kind, pattern, reason}。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT draft_id, kind, pattern, reason FROM rule_drafts "
            "WHERE group_id = ? ORDER BY draft_id ASC",
            (group_id,),
        ).fetchall()
    return [
        {"draft_id": r[0], "kind": r[1], "pattern": r[2], "reason": r[3]}
        for r in rows
    ]


def get_rule_draft(group_id: str, draft_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT draft_id, kind, pattern, reason FROM rule_drafts "
            "WHERE group_id = ? AND draft_id = ?",
            (group_id, draft_id),
        ).fetchone()
    if not row:
        return None
    return {"draft_id": row[0], "kind": row[1], "pattern": row[2], "reason": row[3]}


def clear_rule_drafts(group_id: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM rule_drafts WHERE group_id = ?", (group_id,)
        )
        return cur.rowcount


def delete_rule_draft(group_id: str, draft_id: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM rule_drafts WHERE group_id = ? AND draft_id = ?",
            (group_id, draft_id),
        )
        return cur.rowcount > 0


def get_messages_since(
    group_id: str, since_ts: int, exclude_bot: bool = True
) -> list[tuple[str, str | None, str, int]]:
    """取 since_ts（unix 秒）之後的原始訊息，舊→新。給 Layer 3 週期性檢討用。

    回傳 [(message_id, user_id, text, created_at), ...]。
    exclude_bot=True 時會過濾掉 user_id='__bot__' 的 bot 自貼訊息。
    """
    with _conn() as c:
        if exclude_bot:
            rows = c.execute(
                "SELECT message_id, user_id, text, created_at FROM raw_messages "
                "WHERE group_id = ? AND created_at >= ? "
                "  AND (user_id IS NULL OR user_id != '__bot__') "
                "ORDER BY created_at ASC",
                (group_id, since_ts),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT message_id, user_id, text, created_at FROM raw_messages "
                "WHERE group_id = ? AND created_at >= ? "
                "ORDER BY created_at ASC",
                (group_id, since_ts),
            ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


# ── Persona Notes（人設範例 + 糾正記憶）──────────────────────────────────────

_PERSONA_NOTE_CAP = 20  # 每個 group 每種 kind 最多保留幾筆（先進先出）


def add_persona_note(
    group_id: str, kind: str, scenario: str, content: str
) -> int | None:
    """新增一筆 persona note。kind='example'|'correction'。超過上限自動淘汰最舊的。"""
    import time
    now = int(time.time())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO persona_notes(group_id, kind, scenario, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, kind, scenario, content, now),
        )
        note_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        # 淘汰舊的
        c.execute(
            "DELETE FROM persona_notes WHERE note_id IN ("
            "  SELECT note_id FROM persona_notes "
            "  WHERE group_id = ? AND kind = ? "
            "  ORDER BY created_at DESC LIMIT -1 OFFSET ?"
            ")",
            (group_id, kind, _PERSONA_NOTE_CAP),
        )
        return note_id


def list_persona_notes(
    group_id: str, kind: str | None = None
) -> list[dict]:
    """取出 persona notes。kind=None 取全部，否則只取指定種類。"""
    with _conn() as c:
        if kind:
            rows = c.execute(
                "SELECT note_id, kind, scenario, content, created_at "
                "FROM persona_notes WHERE group_id = ? AND kind = ? "
                "ORDER BY created_at ASC",
                (group_id, kind),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT note_id, kind, scenario, content, created_at "
                "FROM persona_notes WHERE group_id = ? "
                "ORDER BY created_at ASC",
                (group_id,),
            ).fetchall()
    return [
        {"note_id": r[0], "kind": r[1], "scenario": r[2],
         "content": r[3], "created_at": r[4]}
        for r in rows
    ]
