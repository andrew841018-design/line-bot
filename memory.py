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

import hashlib
import json
import re
import sqlite3
import threading
import time as _time
import unicodedata
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import reminder_intent
from config import settings

_DB_PATH = Path(settings.sqlite_path)
if _DB_PATH.parent and str(_DB_PATH.parent) not in ("", "."):
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# sqlite3 在多 thread 寫入時需要 serialize，用一個全域 lock 最單純
_lock = threading.Lock()
_EMBED_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedding-index")
_EMBED_INFLIGHT = threading.BoundedSemaphore(32)


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _conn() -> sqlite3.Connection:
    # check_same_thread=False：uvicorn 會從不同 worker thread 呼進來
    # isolation_level=None：autocommit，我們用 context manager 的 lock 控制一致性
    conn = sqlite3.connect(
        _DB_PATH,
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # I3 fix (2026-05-30): 跨 process 寫同一 db（uvicorn handler thread + 獨立 cron
    # process 如 reminder_push.py）時，沒 busy_timeout 會立刻 raise "database is locked"。
    # 設 5s 讓 writer 等鎖釋放而非直接炸。
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_pending_source_unique_index(c: sqlite3.Connection) -> bool:
    index_sql = (
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_reminders_pending_source_unique "
        "ON reminders(group_id, source_kind, source_ref) "
        "WHERE status='pending' AND source_kind<>'' AND source_ref<>''"
    )
    started_transaction = not c.in_transaction
    if started_transaction:
        c.execute("BEGIN IMMEDIATE")
    try:
        duplicate_source = c.execute(
            "SELECT 1 FROM reminders WHERE status='pending' "
            "AND source_kind<>'' AND source_ref<>'' "
            "GROUP BY group_id, source_kind, source_ref HAVING COUNT(*)>1 "
            "LIMIT 1"
        ).fetchone()
        if duplicate_source is not None:
            if started_transaction:
                c.execute("ROLLBACK")
            raise RuntimeError(
                "pending reminder source identity is not unique"
            )
        c.execute(index_sql)
        if started_transaction:
            c.execute("COMMIT")
        return True
    except Exception:
        if started_transaction and c.in_transaction:
            c.execute("ROLLBACK")
        raise


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
            CREATE TABLE IF NOT EXISTS sent_reminder_refs (
                group_id    TEXT NOT NULL,
                message_id  TEXT NOT NULL,
                reminder_id INTEGER,
                source_kind TEXT NOT NULL DEFAULT '',
                source_ref  TEXT NOT NULL DEFAULT '',
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (group_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS inbound_events (
                group_id    TEXT NOT NULL,
                message_id  TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY (group_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_inbound_events_updated
                ON inbound_events(updated_at);
            CREATE TABLE IF NOT EXISTS raw_message_meta (
                group_id    TEXT NOT NULL,
                message_id  TEXT NOT NULL,
                media_type  TEXT NOT NULL DEFAULT '',
                mime_type   TEXT NOT NULL DEFAULT '',
                file_name   TEXT NOT NULL DEFAULT '',
                media_path  TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY (group_id, message_id)
            );
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
                created_at INTEGER NOT NULL,
                source     TEXT NOT NULL DEFAULT 'rule_violation'
                    -- 'rule_violation' (黑名單詞觸發) | 'organic' (user 真實糾正)
            );
            CREATE INDEX IF NOT EXISTS idx_persona_notes_group
                ON persona_notes(group_id, kind);
            CREATE TABLE IF NOT EXISTS fact_check_cache (
                group_id   TEXT NOT NULL,
                text_hash  TEXT NOT NULL,
                result     TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, text_hash)
            );
            CREATE TABLE IF NOT EXISTS reminders (
                reminder_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        TEXT NOT NULL,
                user_id         TEXT NOT NULL DEFAULT '',
                action          TEXT NOT NULL,
                remind_at       INTEGER NOT NULL,
                created_at      INTEGER NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                source_kind     TEXT NOT NULL DEFAULT '',
                source_ref      TEXT NOT NULL DEFAULT '',
                source_text     TEXT,
                mention_aliases TEXT NOT NULL DEFAULT '[]',
                last_pushed_at  INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_remind_at
                ON reminders(group_id, status, remind_at);
            CREATE TABLE IF NOT EXISTS reminder_delivery_claims (
                group_id       TEXT NOT NULL,
                delivery_kind  TEXT NOT NULL,
                subject_ref    TEXT NOT NULL,
                occurrence     TEXT NOT NULL,
                source_kind    TEXT NOT NULL DEFAULT '',
                source_ref     TEXT NOT NULL DEFAULT '',
                transport      TEXT NOT NULL,
                state          TEXT NOT NULL DEFAULT 'sending',
                claim_token    TEXT NOT NULL,
                retry_key      TEXT NOT NULL,
                fallback_retry_key TEXT NOT NULL DEFAULT '',
                claimed_at     INTEGER NOT NULL,
                PRIMARY KEY (
                    group_id, delivery_kind, subject_ref, occurrence
                )
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_delivery_subject
                ON reminder_delivery_claims(
                    group_id, delivery_kind, subject_ref, state
                );
            CREATE INDEX IF NOT EXISTS idx_reminder_delivery_source
                ON reminder_delivery_claims(
                    group_id, source_kind, source_ref, state
                );
            CREATE TABLE IF NOT EXISTS pending_reminder_extract (
                pending_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id     TEXT NOT NULL,
                user_id      TEXT NOT NULL DEFAULT '',
                message_id   TEXT,
                text         TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                retries      INTEGER NOT NULL DEFAULT 0,
                claimed_at   INTEGER NOT NULL DEFAULT 0,
                claim_token  TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_pending_reminder_status
                ON pending_reminder_extract(status, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_reminder_msgid
                ON pending_reminder_extract(message_id) WHERE message_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS reminder_confirmation_outbox (
                confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        TEXT NOT NULL,
                source_ref      TEXT NOT NULL,
                text            TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                claimed_at      INTEGER NOT NULL DEFAULT 0,
                claim_token     TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(group_id, source_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_confirmation_pending
                ON reminder_confirmation_outbox(group_id, status, created_at);
            CREATE TABLE IF NOT EXISTS kg_triples (
                group_id    TEXT NOT NULL,
                subject     TEXT NOT NULL,
                relation    TEXT NOT NULL,
                object      TEXT NOT NULL,
                source_text TEXT,
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (group_id, subject, relation, object)
            );
            CREATE INDEX IF NOT EXISTS idx_kg_triples_subject
                ON kg_triples(group_id, subject);
            CREATE INDEX IF NOT EXISTS idx_kg_triples_relation
                ON kg_triples(group_id, relation);
            CREATE TABLE IF NOT EXISTS media_cache (
                cache_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id       TEXT NOT NULL,
                media_type     TEXT NOT NULL,
                sha256         TEXT NOT NULL,
                description    TEXT,
                last_reply     TEXT NOT NULL,
                first_seen_at  INTEGER NOT NULL,
                last_seen_at   INTEGER NOT NULL,
                seen_count     INTEGER NOT NULL DEFAULT 1,
                UNIQUE (group_id, media_type, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_media_cache_lookup
                ON media_cache(group_id, media_type, sha256);
            CREATE TABLE IF NOT EXISTS embeddings (
                message_id TEXT PRIMARY KEY,
                group_id   TEXT NOT NULL,
                text       TEXT NOT NULL,
                embedding  BLOB NOT NULL,
                backend    TEXT NOT NULL,
                dim        INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                model_name TEXT NOT NULL DEFAULT '',
                is_bot     INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_embeddings_group
                ON embeddings(group_id);
            """
        )
        cols = {
            row[1]
            for row in c.execute("PRAGMA table_info(pending_reminder_extract)").fetchall()
        }
        if "claimed_at" not in cols:
            c.execute(
                "ALTER TABLE pending_reminder_extract "
                "ADD COLUMN claimed_at INTEGER NOT NULL DEFAULT 0"
            )
        if "claim_token" not in cols:
            c.execute(
                "ALTER TABLE pending_reminder_extract "
                "ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
            )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_reminder_claimed "
            "ON pending_reminder_extract(status, claimed_at)"
        )
        outbox_cols = {
            row[1]
            for row in c.execute(
                "PRAGMA table_info(reminder_confirmation_outbox)"
            ).fetchall()
        }
        if "claim_token" not in outbox_cols:
            c.execute(
                "ALTER TABLE reminder_confirmation_outbox "
                "ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
            )
        # kg_triples schema migration: ALTER TABLE 自動補 column
        # 2026-05-08 新增：純本機 knowledge graph 萃取
        kg = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='kg_triples'"
        ).fetchone()
        if kg:
            kgcols = [
                r[1] for r in c.execute("PRAGMA table_info(kg_triples)").fetchall()
            ]
            if "source_text" not in kgcols:
                c.execute(
                    "ALTER TABLE kg_triples ADD COLUMN source_text TEXT"
                )
            if "created_at" not in kgcols:
                c.execute(
                    "ALTER TABLE kg_triples ADD COLUMN created_at "
                    "INTEGER NOT NULL DEFAULT 0"
                )
        # reminders schema migration: add stage flag columns
        rcols = [r[1] for r in c.execute("PRAGMA table_info(reminders)").fetchall()]
        for col in (
            "last_pushed_at", "weekly_count", "last_weekly_at",
            "pushed_3d", "pushed_1d",
            "pushed_4hr", "pushed_2hr", "pushed_1hr", "pushed_now",
        ):
            if col not in rcols:
                c.execute(f"ALTER TABLE reminders ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        for col in ("source_kind", "source_ref"):
            if col not in rcols:
                c.execute(
                    f"ALTER TABLE reminders ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        if "mention_aliases" not in rcols:
            c.execute(
                "ALTER TABLE reminders ADD COLUMN mention_aliases "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        _ensure_pending_source_unique_index(c)
        # persona_notes schema migration: add source column if missing
        # 2026-05-08：區分 'rule_violation'（既有黑名單觸發）vs 'organic'（user 真實糾正）
        pn_cols = [r[1] for r in c.execute("PRAGMA table_info(persona_notes)").fetchall()]
        if "source" not in pn_cols:
            c.execute(
                "ALTER TABLE persona_notes ADD COLUMN source TEXT NOT NULL "
                "DEFAULT 'rule_violation'"
            )
        # facts schema migration: add user_id column if missing
        cols = [r[1] for r in c.execute("PRAGMA table_info(facts)").fetchall()]
        if "user_id" not in cols:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS facts_new (
                    group_id TEXT NOT NULL,
                    user_id  TEXT NOT NULL DEFAULT '',
                    fact     TEXT NOT NULL,
                    PRIMARY KEY (group_id, user_id, fact)
                );
                INSERT OR IGNORE INTO facts_new (group_id, user_id, fact)
                    SELECT group_id, '', fact FROM facts;
                DROP TABLE facts;
                ALTER TABLE facts_new RENAME TO facts;
            """)

        # embeddings schema migration: ensure model_name column exists.
        # 2026-05-08: bge-m3 (1024 dim) / e5-large (1024) / MiniLM-L12 (384)
        # all coexist; we tag every row with the producing model so
        # retrieve() can filter to the same model as the active query
        # embedding (mixing dims would break the matrix scan).
        # 2026-05-19: add is_bot column for fast bot_only filter in
        # embedding_recall.retrieve() (avoid JOIN on raw_messages per round).
        ec = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='embeddings'"
        ).fetchone()
        if ec:
            ecols = [
                r[1] for r in c.execute("PRAGMA table_info(embeddings)").fetchall()
            ]
            if "model_name" not in ecols:
                c.execute(
                    "ALTER TABLE embeddings ADD COLUMN model_name TEXT NOT NULL "
                    "DEFAULT ''"
                )
            if "dim" not in ecols:
                c.execute(
                    "ALTER TABLE embeddings ADD COLUMN dim INTEGER NOT NULL "
                    "DEFAULT 0"
                )
            if "is_bot" not in ecols:
                c.execute(
                    "ALTER TABLE embeddings ADD COLUMN is_bot INTEGER NOT NULL "
                    "DEFAULT 0"
                )
            # Index lets retrieve() narrow to (group_id, model_name) cheaply
            # once we fan out across multiple models.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_embeddings_model "
                "ON embeddings(group_id, model_name)"
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


def add_fact(group_id: str, fact: str, user_id: str = "") -> bool:
    """回傳是否真的新增（False 代表重複或空字串）。user_id='' 代表群組層級。"""
    fact = fact.strip()
    if not fact:
        return False
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO facts(group_id, user_id, fact) VALUES (?, ?, ?)",
            (group_id, user_id or "", fact),
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


def list_facts(group_id: str, user_id: str | None = None) -> list[str]:
    """user_id=None 取全部；否則取該 user 的專屬事實 + 群組層級（user_id=''）事實。"""
    with _conn() as c:
        if user_id is None:
            rows = c.execute(
                "SELECT fact FROM facts WHERE group_id = ? ORDER BY fact",
                (group_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT fact FROM facts WHERE group_id = ? AND (user_id = ? OR user_id = '') ORDER BY fact",
                (group_id, user_id),
            ).fetchall()
        return [r[0] for r in rows]


def clear_facts(group_id: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM facts WHERE group_id = ?", (group_id,))
        return cur.rowcount


def top_facts(group_id: str, user_id: str | None = None) -> list[str]:
    """給 prompt 注入用，取前 max_facts_in_prompt 條。"""
    return list_facts(group_id, user_id)[: settings.max_facts_in_prompt]


# ── 謠言快取 ───────────────────────────────────────────────────────────────────

_CACHE_TTL_DAYS = 7
_NON_ALNUM = re.compile(r"[^\w]", re.UNICODE)


def _cache_key(text: str) -> str:
    normalized = _NON_ALNUM.sub("", text.lower().strip())
    return hashlib.sha256(normalized.encode()).hexdigest()


def check_fact_cache(group_id: str, text: str) -> str | None:
    """查快取，若命中且未過期回傳 cached result，否則回 None。"""
    if len(text.strip()) < 80:
        return None
    key = _cache_key(text)
    now = int(_time.time())
    with _conn() as c:
        row = c.execute(
            "SELECT result FROM fact_check_cache WHERE group_id = ? AND text_hash = ? AND expires_at > ?",
            (group_id, key, now),
        ).fetchone()
    return row[0] if row else None


def store_fact_cache(group_id: str, text: str, result: str) -> None:
    """存入快取，TTL = _CACHE_TTL_DAYS 天。"""
    if len(text.strip()) < 80:
        return
    key = _cache_key(text)
    now = int(_time.time())
    expires = now + _CACHE_TTL_DAYS * 86400
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO fact_check_cache(group_id, text_hash, result, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, key, result, now, expires),
        )


# ── 計數器（決定何時觸發事實抽取）──────────────────────────────────────────

_RAW_MESSAGE_KEEP = 2000  # 每群組保留最近 N 筆原始訊息（給 quote-reply 查詢用）

# raw_messages is written before routing, so it cannot prove that LINE got a
# reply. Keep a short processing lease for concurrent redeliveries, then allow
# a later delivery to retry an event that never reached a successful reply.
_INBOUND_PROCESSING_LEASE_SECONDS = 30
_INBOUND_EVENT_RETENTION_SECONDS = 14 * 86400


def begin_inbound_event(group_id: str, message_id: str) -> str:
    """Claim an inbound event: ``new``, ``processing``, ``retry`` or ``replied``."""
    if not group_id or not message_id:
        return "new"
    now = int(_time.time())
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM inbound_events WHERE updated_at < ?",
            (now - _INBOUND_EVENT_RETENTION_SECONDS,),
        )
        row = c.execute(
            "SELECT status, updated_at FROM inbound_events "
            "WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO inbound_events "
                "(group_id, message_id, status, created_at, updated_at) "
                "VALUES (?, ?, 'processing', ?, ?)",
                (group_id, message_id, now, now),
            )
            return "new"
        status, updated_at = row
        if status == "replied":
            return "replied"
        if now - int(updated_at) < _INBOUND_PROCESSING_LEASE_SECONDS:
            return "processing"
        c.execute(
            "UPDATE inbound_events SET status = 'processing', updated_at = ? "
            "WHERE group_id = ? AND message_id = ?",
            (now, group_id, message_id),
        )
        return "retry"


def mark_inbound_event_replied(group_id: str, message_id: str) -> None:
    """Record that LINE accepted a reply for an inbound event."""
    if not group_id or not message_id:
        return
    now = int(_time.time())
    with _lock, _conn() as c:
        c.execute(
            "UPDATE inbound_events SET status = 'replied', updated_at = ? "
            "WHERE group_id = ? AND message_id = ?",
            (now, group_id, message_id),
        )


def log_raw_message(
    group_id: str, message_id: str, user_id: str | None, text: str
) -> None:
    """記錄原始訊息，供之後 quote-reply 時查詢。超過 _RAW_MESSAGE_KEEP 筆自動汰舊。

    2026-05-19: 加 semantic embedding hook — 寫完 raw_messages 後同步呼
    embedding_recall.index_message。內部 try/except，失敗只 log 不阻塞主流程。
    ~50ms ST inference，被 Gemini 回覆耗時（>2s）淹沒，webhook 延遲影響可忽略。
    """
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
        c.execute(
            "DELETE FROM sent_reminder_refs WHERE group_id = ? "
            "AND message_id NOT IN "
            "(SELECT message_id FROM raw_messages WHERE group_id = ?)",
            (group_id, group_id),
        )
    # Embedding hook — async fire-and-forget with bounded in-flight work.
    try:
        import embedding_recall as _embedding_recall

        _index_message = _embedding_recall.index_message
        _index_db_path = _embedding_recall._DB_PATH
    except Exception:
        _index_message = None
        _index_db_path = None

    def _bg_index() -> None:
        try:
            if _index_message is not None:
                _index_message(
                    message_id,
                    group_id,
                    text,
                    is_bot=(user_id == "__bot__"),
                    db_path=_index_db_path,
                )
        except Exception:
            pass
        finally:
            _EMBED_INFLIGHT.release()

    if not _EMBED_INFLIGHT.acquire(blocking=False):
        return
    try:
        _EMBED_EXECUTOR.submit(_bg_index)
    except Exception:
        _EMBED_INFLIGHT.release()


def get_raw_message(group_id: str, message_id: str) -> tuple[str | None, str] | None:
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


def get_raw_message_record(group_id: str, message_id: str) -> dict | None:
    """Return one exact group-scoped raw message with its original timestamp."""

    if not group_id or not message_id:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT group_id, message_id, user_id, text, created_at "
            "FROM raw_messages WHERE group_id=? AND message_id=?",
            (group_id, message_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "group_id": str(row[0]),
        "message_id": str(row[1]),
        "user_id": str(row[2] or ""),
        "text": str(row[3] or ""),
        "created_at": int(row[4]),
    }


def log_sent_reminder_reference(
    group_id: str,
    message_id: str,
    *,
    reminder_id: int | None = None,
    source_kind: str = "",
    source_ref: str = "",
) -> bool:
    """Bind an accepted outbound LINE message to its durable reminder identity."""

    source_kind = str(source_kind or "").strip()
    source_ref = str(source_ref or "").strip()
    normalized_reminder_id = int(reminder_id) if reminder_id is not None else None
    if (
        not group_id
        or not message_id
        or (
            normalized_reminder_id is None
            and (not source_kind or not source_ref)
        )
    ):
        return False
    with _lock, _conn() as c:
        if normalized_reminder_id is not None and (
            not source_kind or not source_ref
        ):
            reminder_source = c.execute(
                "SELECT source_kind, source_ref FROM reminders "
                "WHERE group_id=? AND reminder_id=?",
                (group_id, normalized_reminder_id),
            ).fetchone()
            if reminder_source is not None:
                source_kind = source_kind or str(reminder_source[0] or "")
                source_ref = source_ref or str(reminder_source[1] or "")
        cursor = c.execute(
            "INSERT OR REPLACE INTO sent_reminder_refs("
            "group_id, message_id, reminder_id, source_kind, source_ref, created_at"
            ") VALUES (?, ?, ?, ?, ?, strftime('%s','now'))",
            (
                group_id,
                message_id,
                normalized_reminder_id,
                source_kind,
                source_ref,
            ),
        )
    return cursor.rowcount == 1


def get_sent_reminder_reference(
    group_id: str,
    message_id: str,
) -> dict | None:
    """Return the group-scoped reminder identity attached after LINE accepted it."""

    if not group_id or not message_id:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT reminder_id, source_kind, source_ref "
            "FROM sent_reminder_refs WHERE group_id=? AND message_id=?",
            (group_id, message_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "reminder_id": int(row[0]) if row[0] is not None else None,
        "source_kind": str(row[1] or ""),
        "source_ref": str(row[2] or ""),
    }


def log_raw_message_meta(
    group_id: str,
    message_id: str,
    *,
    media_type: str = "",
    mime_type: str = "",
    file_name: str = "",
    media_path: str = "",
    description: str = "",
) -> None:
    """Attach retrievable metadata to a raw LINE message for quote handling."""
    if not group_id or not message_id:
        return
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO raw_message_meta
                (group_id, message_id, media_type, mime_type, file_name,
                 media_path, description, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(group_id, message_id) DO UPDATE SET
                media_type = COALESCE(NULLIF(excluded.media_type, ''), media_type),
                mime_type = COALESCE(NULLIF(excluded.mime_type, ''), mime_type),
                file_name = COALESCE(NULLIF(excluded.file_name, ''), file_name),
                media_path = COALESCE(NULLIF(excluded.media_path, ''), media_path),
                description = COALESCE(NULLIF(excluded.description, ''), description),
                updated_at = excluded.updated_at
            """,
            (
                group_id,
                message_id,
                media_type or "",
                mime_type or "",
                file_name or "",
                media_path or "",
                (description or "")[:4000],
            ),
        )


def get_raw_message_meta(group_id: str, message_id: str) -> dict | None:
    """Return quote/media metadata for a raw message, if available."""
    if not group_id or not message_id:
        return None
    with _conn() as c:
        row = c.execute(
            """
            SELECT media_type, mime_type, file_name, media_path, description, updated_at
            FROM raw_message_meta
            WHERE group_id = ? AND message_id = ?
            """,
            (group_id, message_id),
        ).fetchone()
    if not row:
        return None
    return {
        "media_type": row[0],
        "mime_type": row[1],
        "file_name": row[2],
        "media_path": row[3],
        "description": row[4],
        "updated_at": row[5],
    }


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


def search_raw_messages(
    group_id: str,
    query: str,
    *,
    limit: int = 5,
    exclude_bot: bool = True,
) -> list[tuple[str, str | None, str, int]]:
    """Keyword search over retained raw LINE messages, newest first.

    The search is intentionally local and group-scoped: split the user query
    into terms and require every term to appear in the message text.
    """
    def _search_terms(q: str) -> list[str]:
        raw_terms = [
            t.strip()
            for t in re.split(r"\s+", q or "")
            if len(t.strip()) >= 2
        ]
        if len(raw_terms) != 1:
            return raw_terms
        only = raw_terms[0]
        if len(only) <= 4 or not re.search(r"[\u4e00-\u9fff]", only):
            return raw_terms
        parts = [
            p.strip()
            for p in re.split(
                r"(?:去|回|的|關於|有關|日期|時間|對話紀錄|聊天紀錄|聊天記錄|歷史訊息)",
                only,
            )
            if len(p.strip()) >= 2
        ]
        return parts or raw_terms

    terms = _search_terms(query)
    if not group_id or not terms:
        return []
    limit = max(1, min(int(limit or 5), 20))

    def _like_pattern(term: str) -> str:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    where = ["group_id = ?"]
    params: list[object] = [group_id]
    if exclude_bot:
        where.append("(user_id IS NULL OR user_id != '__bot__')")
    for term in terms[:5]:
        where.append("text LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(term))
    sql = (
        "SELECT message_id, user_id, text, created_at FROM raw_messages "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT ?"
    )
    params.append(limit)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


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
        {"rule_id": r[0], "kind": r[1], "pattern": r[2], "source": r[3]} for r in rows
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
        cur = c.execute("DELETE FROM filter_rules WHERE group_id = ?", (group_id,))
        return cur.rowcount


# ── Rule drafts（Layer 3 週期性自我檢討的候選規則）──────────────────────────


def add_rule_draft(group_id: str, kind: str, pattern: str, reason: str = "") -> int:
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
        {"draft_id": r[0], "kind": r[1], "pattern": r[2], "reason": r[3]} for r in rows
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
        cur = c.execute("DELETE FROM rule_drafts WHERE group_id = ?", (group_id,))
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

_PERSONA_NOTE_CAP = 50  # 每個 group 每種 kind 最多保留幾筆（先進先出）
# 2026-05-07 從 20 調 50：corrections 是 quality 違規累積學習，數量太少會 lose history


def add_persona_note(
    group_id: str,
    kind: str,
    scenario: str,
    content: str,
    source: str = "rule_violation",
) -> int | None:
    """新增一筆 persona note。

    - kind='example'|'correction'
    - source='rule_violation'（黑名單詞觸發、_violates_quality）|'organic'（user 真實糾正）
    超過上限自動淘汰最舊的。
    """
    import time

    now = int(time.time())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO persona_notes"
            "(group_id, kind, scenario, content, created_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, kind, scenario, content, now, source),
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


def add_organic_correction(
    group_id: str,
    prev_user_msg: str,
    prev_bot_msg: str,
    correction_msg: str,
    summary: str = "",
) -> int | None:
    """User 真實糾正 → 寫進 persona_notes（kind='correction', source='organic'）。

    把上一輪 user 訊息 + bot 回覆 + 這次糾正三者拼起來存。如果有 summary
    （Gemini 抽出的「具體做錯什麼」一句話），會放在 content 最前面。

    回 note_id；任何步驟失敗回 None（不阻塞主流程）。
    """
    try:
        prev_user = (prev_user_msg or "").strip()[:300]
        prev_bot = (prev_bot_msg or "").strip()[:300]
        correction = (correction_msg or "").strip()[:300]
        summary_clean = (summary or "").strip()[:200]

        if summary_clean:
            content = (
                f"教訓：{summary_clean}\n"
                f"user 原問：{prev_user}\n"
                f"咪寶當時答：{prev_bot}\n"
                f"user 糾正：{correction}"
            )
        else:
            content = (
                f"user 原問：{prev_user}\n"
                f"咪寶當時答：{prev_bot}\n"
                f"user 糾正：{correction}"
            )
        return add_persona_note(
            group_id, "correction", "使用者主動糾正", content, source="organic"
        )
    except Exception:
        return None


def list_persona_notes(group_id: str, kind: str | None = None) -> list[dict]:
    """取出 persona notes。kind=None 取全部，否則只取指定種類。

    回傳每筆含 source 欄位（'rule_violation' | 'organic'）。
    """
    with _conn() as c:
        if kind:
            rows = c.execute(
                "SELECT note_id, kind, scenario, content, created_at, source "
                "FROM persona_notes WHERE group_id = ? AND kind = ? "
                "ORDER BY created_at ASC",
                (group_id, kind),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT note_id, kind, scenario, content, created_at, source "
                "FROM persona_notes WHERE group_id = ? "
                "ORDER BY created_at ASC",
                (group_id,),
            ).fetchall()
    return [
        {
            "note_id": r[0],
            "kind": r[1],
            "scenario": r[2],
            "content": r[3],
            "created_at": r[4],
            "source": r[5] or "rule_violation",
        }
        for r in rows
    ]


# ── Reminders（自動偵測時間性事項，2026-05-08 加）────────────────────────────


def _add_reminder_with_outcome_conn(
    c: sqlite3.Connection,
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_text: str,
    mentions: list[str],
    now: int,
) -> tuple[int, str]:
    mentions_json = json.dumps(mentions, ensure_ascii=False)
    existing = c.execute(
        "SELECT reminder_id, COALESCE(mention_aliases, '[]') FROM reminders "
        "WHERE group_id = ? AND action = ? AND status = 'pending' "
        "AND ABS(remind_at - ?) < 3600",
        (group_id, action, remind_at),
    ).fetchone()
    if existing:
        merged_mentions = _merge_mention_aliases_json(existing[1], mentions)
        if merged_mentions != existing[1]:
            c.execute(
                "UPDATE reminders SET mention_aliases = ? WHERE reminder_id = ?",
                (merged_mentions, existing[0]),
            )
        return int(existing[0]), "duplicate"
    weak_nearby = c.execute(
        "SELECT reminder_id, action, COALESCE(source_text, ''), "
        "COALESCE(mention_aliases, '[]'), COALESCE(source_kind, ''), "
        "COALESCE(source_ref, '') "
        "FROM reminders WHERE group_id=? AND status='pending' "
        "AND ABS(remind_at - ?) < 60 ORDER BY reminder_id",
        (group_id, remind_at),
    ).fetchall()
    incoming_is_weak = reminder_intent.is_weak_reminder_action(action)
    strong_rows = [
        row
        for row in weak_nearby
        if not reminder_intent.is_weak_reminder_action(row[1])
    ]
    weak_rows = [
        row
        for row in weak_nearby
        if reminder_intent.is_weak_reminder_action(row[1])
        and not str(row[4] or "")
        and not str(row[5] or "")
    ]
    if incoming_is_weak and len(strong_rows) == 1:
        strong = strong_rows[0]
        return int(strong[0]), "duplicate"
    if not incoming_is_weak and len(weak_rows) == 1 and not strong_rows:
        weak = weak_rows[0]
        live_claim = c.execute(
            "SELECT 1 FROM reminder_delivery_claims "
            "WHERE group_id=? AND delivery_kind='natural' AND subject_ref=? "
            "AND state IN ('sending', 'uncertain') LIMIT 1",
            (group_id, str(weak[0])),
        ).fetchone()
        if live_claim is not None:
            return int(weak[0]), "duplicate"
        merged_mentions = _merge_mention_aliases_json(weak[3], mentions)
        c.execute(
            "UPDATE reminders SET user_id=?, action=?, source_text=?, "
            "mention_aliases=? WHERE reminder_id=? AND status='pending'",
            (
                user_id or "",
                action,
                source_text,
                merged_mentions,
                int(weak[0]),
            ),
        )
        return int(weak[0]), "merged"
    nearby = c.execute(
        "SELECT reminder_id, action, COALESCE(source_text, ''), "
        "COALESCE(mention_aliases, '[]') "
        "FROM reminders WHERE group_id = ? AND status = 'pending' "
        "AND ABS(remind_at - ?) < 1800",
        (group_id, remind_at),
    ).fetchall()
    for existing_id, existing_action, existing_source, existing_mentions in nearby:
        merged_action = _merge_reminder_action(existing_action, action)
        if not merged_action:
            continue
        merged_source = _merge_reminder_source(existing_source, source_text)
        merged_mentions = _merge_mention_aliases_json(existing_mentions, mentions)
        c.execute(
            "UPDATE reminders SET action = ?, source_text = ?, mention_aliases = ? "
            "WHERE reminder_id = ?",
            (merged_action, merged_source, merged_mentions, existing_id),
        )
        return int(existing_id), "merged"
    c.execute(
        "INSERT INTO reminders(group_id, user_id, action, remind_at, "
        "created_at, status, source_kind, source_ref, source_text, mention_aliases) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (
            group_id, user_id, action, remind_at, now,
            "", "", source_text, mentions_json
        ),
    )
    reminder_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    return reminder_id, "created"


def add_reminder_with_outcome(
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_text: str = "",
    mention_aliases: list[str] | None = None,
) -> tuple[int, str]:
    """Atomically add or merge a reminder and return its write outcome.

    Outcome is one of ``created``, ``duplicate``, or ``merged``. The explicit
    immediate transaction serializes the dedupe read/write across processes.
    """
    now = int(_time.time())
    action = _normalize_reminder_text(action)
    source_text = _normalize_reminder_text(source_text)
    mentions = _normalize_mention_aliases(mention_aliases)
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        return _add_reminder_with_outcome_conn(
            c,
            group_id,
            user_id,
            action,
            remind_at,
            source_text,
            mentions,
            now,
        )


def add_reminder(
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_text: str = "",
    mention_aliases: list[str] | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
) -> int | None:
    """新增 reminder。remind_at = epoch seconds。

    去重：同 group_id + 同 action + 24h 內 remind_at 差 < 1h → 跳過（避免同訊息被多次抽）。
    回 reminder_id；完全重複時回 None。
    """
    if source_kind and source_ref:
        return upsert_reminder_for_source(
            group_id=group_id,
            user_id=user_id,
            action=action,
            remind_at=remind_at,
            source_kind=source_kind,
            source_ref=source_ref,
            source_text=source_text,
            mention_aliases=mention_aliases,
        )

    reminder_id, outcome = add_reminder_with_outcome(
        group_id=group_id,
        user_id=user_id,
        action=action,
        remind_at=remind_at,
        source_text=source_text,
        mention_aliases=mention_aliases,
    )
    return None if outcome == "duplicate" else reminder_id


def _get_reminder_conn(c: sqlite3.Connection, reminder_id: int) -> dict | None:
    row = c.execute(
        "SELECT reminder_id, group_id, user_id, action, remind_at, status, "
        "source_kind, source_ref, source_text, mention_aliases "
        "FROM reminders WHERE reminder_id=?",
        (int(reminder_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "reminder_id": int(row[0]),
        "group_id": str(row[1]),
        "user_id": str(row[2]),
        "action": str(row[3]),
        "remind_at": int(row[4]),
        "status": str(row[5]),
        "source_kind": str(row[6] or ""),
        "source_ref": str(row[7] or ""),
        "source_text": str(row[8] or ""),
        "mention_aliases": _load_mention_aliases(row[9]),
    }


def get_reminder(reminder_id: int) -> dict | None:
    """Return the canonical persisted reminder row used for acknowledgements."""
    with _conn() as c:
        return _get_reminder_conn(c, reminder_id)


def list_reminder_cancellation_candidates(
    group_id: str,
    include_cancelled: bool = False,
    include_terminal: bool = False,
) -> list[dict]:
    """Pure group-scoped candidate read for deterministic cancellation.

    Unlike the user-facing pending-reminder list, this intentionally performs
    no deduplication and applies no time cutoff. Cancelled rows are optional so
    callers can recognize an idempotent repeat. Done/expired rows are optional
    ambiguity evidence for historical quoted messages that predate durable
    outbound identity bindings; they are never normal cancellation targets.
    """
    statuses = ["pending"]
    if include_cancelled:
        statuses.append("cancelled")
    if include_terminal:
        statuses.extend(("done", "expired"))
    placeholders = ",".join("?" for _ in statuses)
    with _conn() as c:
        rows = c.execute(
            "SELECT reminder_id, group_id, user_id, action, remind_at, status, "
            "source_kind, source_ref, source_text, mention_aliases "
            f"FROM reminders WHERE group_id = ? AND status IN ({placeholders}) "
            "ORDER BY remind_at, reminder_id",
            (group_id, *statuses),
        ).fetchall()
    return [
        {
            "reminder_id": int(row[0]),
            "group_id": str(row[1]),
            "user_id": str(row[2]),
            "action": str(row[3]),
            "remind_at": int(row[4]),
            "status": str(row[5]),
            "source_kind": str(row[6] or ""),
            "source_ref": str(row[7] or ""),
            "source_text": str(row[8] or ""),
            "mention_aliases": _load_mention_aliases(row[9]),
        }
        for row in rows
    ]


def list_reminder_source_cancellation_candidates(
    group_id: str,
    source_kind: str,
    source_ref: str,
) -> list[dict]:
    """Read exact source-linked rows, including done rows that can tombstone.

    A calendar event may have later event notifications after its natural
    reminder row reached ``done``. Only this source-scoped path exposes those
    rows for cancellation; generic reminder cancellation remains pending-only.
    """

    if not group_id or not source_kind or not source_ref:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT reminder_id, group_id, user_id, action, remind_at, status, "
            "source_kind, source_ref, source_text, mention_aliases "
            "FROM reminders WHERE group_id=? AND source_kind=? AND source_ref=? "
            "AND status IN ('pending', 'done', 'expired', 'cancelled') "
            "ORDER BY reminder_id",
            (group_id, source_kind, source_ref),
        ).fetchall()
    return [
        {
            "reminder_id": int(row[0]),
            "group_id": str(row[1]),
            "user_id": str(row[2]),
            "action": str(row[3]),
            "remind_at": int(row[4]),
            "status": str(row[5]),
            "source_kind": str(row[6] or ""),
            "source_ref": str(row[7] or ""),
            "source_text": str(row[8] or ""),
            "mention_aliases": _load_mention_aliases(row[9]),
        }
        for row in rows
    ]


def _semantic_delivery_claim_conn(
    c: sqlite3.Connection,
    *,
    group_id: str,
    reminder_id: int,
    action: str,
    remind_at: int,
    source_kind: str = "",
    source_ref: str = "",
    include_semantic: bool = True,
    restrict_to_source_cluster: bool = False,
) -> sqlite3.Row | tuple | None:
    """Find an in-flight claim for one reminder or its dedupe-equivalent row."""

    direct = c.execute(
        "SELECT state FROM reminder_delivery_claims AS claim "
        "WHERE claim.group_id=? "
        "AND claim.state IN ('sending', 'uncertain') AND ("
        "(claim.delivery_kind='natural' AND claim.subject_ref=?) "
        "OR (?<>'' AND ?<>'' "
        "AND claim.source_kind=? AND claim.source_ref=?)"
        ") ORDER BY CASE claim.state WHEN 'uncertain' THEN 0 ELSE 1 END LIMIT 1",
        (
            group_id,
            str(int(reminder_id)),
            source_kind,
            source_ref,
            source_kind,
            source_ref,
        ),
    ).fetchone()
    if direct is not None or not include_semantic:
        return direct

    semantic_rows = c.execute(
        "SELECT claim.state, peer.action, peer.source_kind, peer.source_ref "
        "FROM reminder_delivery_claims AS claim "
        "JOIN reminders AS peer "
        "ON claim.delivery_kind='natural' "
        "AND peer.reminder_id=CAST(claim.subject_ref AS INTEGER) "
        "WHERE claim.group_id=? "
        "AND claim.state IN ('sending', 'uncertain') "
        "AND peer.group_id=? AND ABS(peer.remind_at - ?) <= 60 "
        "ORDER BY CASE claim.state WHEN 'uncertain' THEN 0 ELSE 1 END",
        (group_id, group_id, int(remind_at)),
    ).fetchall()
    normalized_action = _reminder_equivalence_key(action)
    return next(
        (
            row
            for row in semantic_rows
            if _reminder_equivalence_key(row[1]) == normalized_action
            and (
                not restrict_to_source_cluster
                or (not str(row[2] or "") and not str(row[3] or ""))
                or (
                    str(row[2] or "") == source_kind
                    and str(row[3] or "") == source_ref
                )
            )
        ),
        None,
    )


def _cancel_semantic_pending_duplicates_conn(
    c: sqlite3.Connection,
    *,
    group_id: str,
    action: str,
    remind_at: int,
    source_kind: str = "",
    source_ref: str = "",
    restrict_to_source_cluster: bool = False,
) -> None:
    """Tombstone rows that the canonical deduper treats as one reminder."""

    rows = c.execute(
        "SELECT reminder_id, action, source_kind, source_ref FROM reminders "
        "WHERE group_id=? AND status='pending' "
        "AND ABS(remind_at - ?) <= 60",
        (group_id, int(remind_at)),
    ).fetchall()
    normalized_action = _reminder_equivalence_key(action)
    matching_rows = [
        row
        for row in rows
        if _reminder_equivalence_key(row[1]) == normalized_action
    ]
    if restrict_to_source_cluster:
        matching_rows = [
            row
            for row in matching_rows
            if (not str(row[2] or "") and not str(row[3] or ""))
            or (
                str(row[2] or "") == source_kind
                and str(row[3] or "") == source_ref
            )
        ]
    else:
        source_identities = {
            (str(row[2] or ""), str(row[3] or ""))
            for row in matching_rows
            if str(row[2] or "") and str(row[3] or "")
        }
        if len(source_identities) > 1:
            # A source-less reminder cannot be assigned to one of several
            # durable calendar identities. Cancel only source-less peers.
            matching_rows = [
                row
                for row in matching_rows
                if not str(row[2] or "") and not str(row[3] or "")
            ]
    reminder_ids = [int(row[0]) for row in matching_rows]
    if not reminder_ids:
        return
    placeholders = ",".join(["?"] * len(reminder_ids))
    c.execute(
        "UPDATE reminders SET status='cancelled' "
        f"WHERE reminder_id IN ({placeholders}) AND status='pending'",
        reminder_ids,
    )


def cancel_pending_reminder(
    group_id: str,
    reminder_id: int,
    expected_action: str,
    expected_remind_at: int,
) -> dict | None:
    """Atomically change one exact current-group pending reminder to cancelled."""
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        identity = c.execute(
            "SELECT source_kind, source_ref FROM reminders "
            "WHERE group_id=? AND reminder_id=?",
            (group_id, int(reminder_id)),
        ).fetchone()
        source_kind = str(identity[0] or "") if identity is not None else ""
        source_ref = str(identity[1] or "") if identity is not None else ""
        delivery = _semantic_delivery_claim_conn(
            c,
            group_id=group_id,
            reminder_id=int(reminder_id),
            action=str(expected_action),
            remind_at=int(expected_remind_at),
            source_kind=source_kind,
            source_ref=source_ref,
        )
        cursor = c.execute(
            "UPDATE reminders SET status='cancelled' "
            "WHERE group_id=? AND reminder_id=? AND status='pending' "
            "AND action=? AND remind_at=?",
            (
                group_id,
                int(reminder_id),
                str(expected_action),
                int(expected_remind_at),
            ),
        )
        if cursor.rowcount != 1:
            return None
        _cancel_semantic_pending_duplicates_conn(
            c,
            group_id=group_id,
            action=str(expected_action),
            remind_at=int(expected_remind_at),
            source_kind=source_kind,
            source_ref=source_ref,
            restrict_to_source_cluster=bool(source_kind and source_ref),
        )
        row = _get_reminder_conn(c, reminder_id)
        if row is not None and delivery is not None:
            row["_delivery_in_flight"] = True
            row["_delivery_state"] = str(delivery[0] or "sending")
        return row


def cancel_reminder_for_source(
    group_id: str,
    reminder_id: int,
    expected_action: str,
    expected_remind_at: int,
    source_kind: str,
    source_ref: str,
    expected_status: str,
) -> dict | None:
    """Atomically persist a source cancellation tombstone.

    ``done``/``expired`` are accepted only here because a source-backed
    calendar event can still have later event notifications. All identifiers
    and the prior status are compare-and-set conditions, so this cannot widen
    generic cancellation.
    """

    if (
        expected_status not in {"pending", "done", "expired"}
        or not group_id
        or not source_kind
        or not source_ref
    ):
        return None
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        delivery = _semantic_delivery_claim_conn(
            c,
            group_id=group_id,
            reminder_id=int(reminder_id),
            action=str(expected_action),
            remind_at=int(expected_remind_at),
            source_kind=source_kind,
            source_ref=source_ref,
            include_semantic=(expected_status == "pending"),
            restrict_to_source_cluster=True,
        )
        cursor = c.execute(
            "UPDATE reminders SET status='cancelled' "
            "WHERE group_id=? AND reminder_id=? AND status=? "
            "AND action=? AND remind_at=? AND source_kind=? AND source_ref=?",
            (
                group_id,
                int(reminder_id),
                expected_status,
                str(expected_action),
                int(expected_remind_at),
                source_kind,
                source_ref,
            ),
        )
        if cursor.rowcount != 1:
            return None
        if expected_status == "pending":
            _cancel_semantic_pending_duplicates_conn(
                c,
                group_id=group_id,
                action=str(expected_action),
                remind_at=int(expected_remind_at),
                source_kind=source_kind,
                source_ref=source_ref,
                restrict_to_source_cluster=True,
            )
        row = _get_reminder_conn(c, reminder_id)
        if row is not None and delivery is not None:
            row["_delivery_in_flight"] = True
            row["_delivery_state"] = str(delivery[0] or "sending")
        return row


_NATURAL_DELIVERY_STAGES = {
    "weekly",
    "3d",
    "1d",
    "4hr",
    "2hr",
    "1hr",
    "now",
}
_CALENDAR_DELIVERY_OFFSETS = {30, 7, 3, 2, 1, 0}
_REMINDER_DELIVERY_STALE_SECONDS = 15 * 60


def _delivery_retry_key(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _prepare_delivery_occurrence_conn(
    c: sqlite3.Connection,
    *,
    group_id: str,
    delivery_kind: str,
    subject_ref: str,
    occurrence: str,
    transport: str,
) -> bool:
    """Fence a live/uncertain claim and safely recycle stale push claims."""

    existing = c.execute(
        "SELECT state, transport, claimed_at FROM reminder_delivery_claims "
        "WHERE group_id=? AND delivery_kind=? AND subject_ref=? "
        "AND occurrence=?",
        (group_id, delivery_kind, subject_ref, occurrence),
    ).fetchone()
    if existing is None:
        return True
    state = str(existing[0] or "")
    existing_transport = str(existing[1] or "")
    claimed_at = int(existing[2] or 0)
    stale = claimed_at < int(_time.time()) - _REMINDER_DELIVERY_STALE_SECONDS
    if state != "sending" or not stale:
        return False
    if existing_transport == "reply":
        c.execute(
            "UPDATE reminder_delivery_claims SET state='uncertain' "
            "WHERE group_id=? AND delivery_kind=? AND subject_ref=? "
            "AND occurrence=? AND state='sending'",
            (group_id, delivery_kind, subject_ref, occurrence),
        )
        return False
    if existing_transport != "push" or transport != "push":
        return False
    c.execute(
        "DELETE FROM reminder_delivery_claims "
        "WHERE group_id=? AND delivery_kind=? AND subject_ref=? "
        "AND occurrence=? AND state='sending'",
        (group_id, delivery_kind, subject_ref, occurrence),
    )
    return True


def claim_natural_reminder_delivery(
    group_id: str,
    reminder_id: int,
    stage: str,
    *,
    expected_action: str,
    expected_remind_at: int,
    expected_weekly_count: int = 0,
    expected_user_id: str | None = None,
    expected_source_kind: str | None = None,
    expected_source_ref: str | None = None,
    expected_source_text: str | None = None,
    expected_mention_aliases: list[str] | None = None,
    transport: str,
) -> dict | None:
    """Atomically authorize one natural-reminder delivery occurrence."""

    if (
        not group_id
        or stage not in _NATURAL_DELIVERY_STAGES
        or transport not in {"push", "reply"}
    ):
        return None
    reminder_id = int(reminder_id)
    expected_remind_at = int(expected_remind_at)
    expected_weekly_count = int(expected_weekly_count)
    occurrence = (
        f"weekly:{expected_weekly_count}"
        if stage == "weekly"
        else stage
    )
    token = uuid.uuid4().hex
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT action, remind_at, weekly_count, source_kind, source_ref, "
            "pushed_3d, pushed_1d, pushed_4hr, pushed_2hr, pushed_1hr, "
            "pushed_now, user_id, source_text, mention_aliases FROM reminders "
            "WHERE group_id=? AND reminder_id=? AND status='pending'",
            (group_id, reminder_id),
        ).fetchone()
        if row is None:
            return None
        if str(row[0]) != str(expected_action) or int(row[1]) != expected_remind_at:
            return None
        if expected_user_id is not None and str(row[11] or "") != str(
            expected_user_id
        ):
            return None
        if expected_source_kind is not None and str(row[3] or "") != str(
            expected_source_kind
        ):
            return None
        if expected_source_ref is not None and str(row[4] or "") != str(
            expected_source_ref
        ):
            return None
        if expected_source_text is not None and str(row[12] or "") != str(
            expected_source_text
        ):
            return None
        if expected_mention_aliases is not None and _load_mention_aliases(
            row[13]
        ) != _normalize_mention_aliases(expected_mention_aliases):
            return None
        if stage == "weekly":
            if int(row[2] or 0) != expected_weekly_count:
                return None
        else:
            flag_index = {
                "3d": 5,
                "1d": 6,
                "4hr": 7,
                "2hr": 8,
                "1hr": 9,
                "now": 10,
            }[stage]
            if int(row[flag_index] or 0):
                return None
        duplicate_deliveries = c.execute(
            "SELECT claim.subject_ref, claim.occurrence, peer.action "
            "FROM reminder_delivery_claims AS claim "
            "JOIN reminders AS peer "
            "ON peer.reminder_id=CAST(claim.subject_ref AS INTEGER) "
            "WHERE claim.group_id=? AND claim.delivery_kind='natural' "
            "AND ((?='weekly' AND claim.occurrence LIKE 'weekly:%') "
            "OR (?<>'weekly' AND claim.occurrence=?)) "
            "AND claim.state IN ('sending', 'uncertain') "
            "AND peer.group_id=? "
            "AND ABS(peer.remind_at - ?) <= 60",
            (
                group_id,
                stage,
                stage,
                occurrence,
                group_id,
                expected_remind_at,
            ),
        ).fetchall()
        normalized_action = _reminder_equivalence_key(expected_action)
        for duplicate_delivery in duplicate_deliveries:
            peer_id = str(duplicate_delivery[0] or "")
            peer_occurrence = str(duplicate_delivery[1] or "")
            peer_action = _reminder_equivalence_key(duplicate_delivery[2])
            if peer_action != normalized_action:
                continue
            if peer_id != str(reminder_id) or (
                stage == "weekly" and peer_occurrence != occurrence
            ):
                return None
        retry_key = _delivery_retry_key(
            "line_bot:natural:"
            f"{group_id}:{reminder_id}:{occurrence}:"
            f"{expected_remind_at}:{expected_action}"
        )
        if not _prepare_delivery_occurrence_conn(
            c,
            group_id=group_id,
            delivery_kind="natural",
            subject_ref=str(reminder_id),
            occurrence=occurrence,
            transport=transport,
        ):
            return None
        try:
            c.execute(
                "INSERT INTO reminder_delivery_claims("
                "group_id, delivery_kind, subject_ref, occurrence, "
                "source_kind, source_ref, transport, state, claim_token, "
                "retry_key, fallback_retry_key, claimed_at"
                ") VALUES (?, 'natural', ?, ?, ?, ?, ?, 'sending', ?, ?, '', ?)",
                (
                    group_id,
                    str(reminder_id),
                    occurrence,
                    str(row[3] or ""),
                    str(row[4] or ""),
                    transport,
                    token,
                    retry_key,
                    int(_time.time()),
                ),
            )
        except sqlite3.IntegrityError:
            return None
    return {
        "group_id": group_id,
        "delivery_kind": "natural",
        "subject_ref": str(reminder_id),
        "occurrence": occurrence,
        "claim_token": token,
        "retry_key": retry_key,
        "fallback_retry_key": "",
        "reminder_id": reminder_id,
        "stage": stage,
        "expected_action": str(expected_action),
        "expected_remind_at": expected_remind_at,
    }


def claim_calendar_reminder_delivery(
    group_id: str,
    source_kind: str,
    source_ref: str,
    offset: int,
    *,
    expected_title: str,
    expected_event_date: str,
    expected_event_time: str | None,
    expected_location: str,
    expected_participants: str,
    transport: str,
) -> dict | None:
    """Atomically authorize one calendar-event reminder offset."""

    offset = int(offset)
    if (
        not group_id
        or not source_kind
        or not source_ref
        or offset not in _CALENDAR_DELIVERY_OFFSETS
        or transport not in {"push", "reply"}
    ):
        return None
    column = f"reminded_{offset}d"
    occurrence = f"calendar:{offset}"
    token = uuid.uuid4().hex
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        source_row = c.execute(
            "SELECT 1 FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=? "
            "AND status IN ('pending', 'done', 'expired') LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
        cancelled = c.execute(
            "SELECT 1 FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=? "
            "AND status='cancelled' LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
        if source_row is None or cancelled is not None:
            return None
        try:
            event = c.execute(
                f"SELECT title, event_date, COALESCE(event_time, ''), "
                f"COALESCE(location, ''), COALESCE(participants, '[]') "
                f"FROM events WHERE group_id=? AND event_id=? "
                f"AND status='active' AND {column} IS NULL "
                "AND title=? AND event_date=? "
                "AND COALESCE(event_time, '')=? "
                "AND COALESCE(location, '')=? "
                "AND COALESCE(participants, '[]')=?",
                (
                    group_id,
                    source_ref,
                    str(expected_title),
                    str(expected_event_date),
                    str(expected_event_time or ""),
                    str(expected_location or ""),
                    str(expected_participants or "[]"),
                ),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if event is None:
            return None
        retry_key = _delivery_retry_key(
            f"line_bot:calendar:{group_id}:{source_ref}:{offset}:main"
        )
        fallback_retry_key = _delivery_retry_key(
            f"line_bot:calendar:{group_id}:{source_ref}:{offset}:fallback"
        )
        if not _prepare_delivery_occurrence_conn(
            c,
            group_id=group_id,
            delivery_kind="calendar",
            subject_ref=source_ref,
            occurrence=occurrence,
            transport=transport,
        ):
            return None
        try:
            c.execute(
                "INSERT INTO reminder_delivery_claims("
                "group_id, delivery_kind, subject_ref, occurrence, "
                "source_kind, source_ref, transport, state, claim_token, "
                "retry_key, fallback_retry_key, claimed_at"
                ") VALUES (?, 'calendar', ?, ?, ?, ?, ?, 'sending', ?, ?, ?, ?)",
                (
                    group_id,
                    source_ref,
                    occurrence,
                    source_kind,
                    source_ref,
                    transport,
                    token,
                    retry_key,
                    fallback_retry_key,
                    int(_time.time()),
                ),
            )
        except sqlite3.IntegrityError:
            return None
    return {
        "group_id": group_id,
        "delivery_kind": "calendar",
        "subject_ref": source_ref,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "occurrence": occurrence,
        "claim_token": token,
        "retry_key": retry_key,
        "fallback_retry_key": fallback_retry_key,
        "offset": offset,
        "expected_title": str(event[0]),
        "expected_event_date": str(event[1]),
        "expected_event_time": str(event[2] or ""),
        "expected_location": str(event[3] or ""),
        "expected_participants": str(event[4] or "[]"),
    }


def _delivery_claim_where(claim: dict) -> tuple[tuple[object, ...], str]:
    params = (
        str(claim.get("group_id") or ""),
        str(claim.get("delivery_kind") or ""),
        str(claim.get("subject_ref") or ""),
        str(claim.get("occurrence") or ""),
        str(claim.get("claim_token") or ""),
    )
    where = (
        "group_id=? AND delivery_kind=? AND subject_ref=? "
        "AND occurrence=? AND claim_token=?"
    )
    return params, where


def release_reminder_delivery_claim(claim: dict) -> bool:
    """Release a definitively failed LINE delivery claim."""

    params, where = _delivery_claim_where(claim)
    with _lock, _conn() as c:
        cursor = c.execute(
            f"DELETE FROM reminder_delivery_claims WHERE {where}",
            params,
        )
    return cursor.rowcount == 1


def release_reminder_delivery_claims(claims: list[dict]) -> int:
    return sum(release_reminder_delivery_claim(claim) for claim in claims)


def mark_reminder_delivery_claim_uncertain(claim: dict) -> bool:
    """Fence an ambiguously accepted reply so another sender cannot retry it."""

    params, where = _delivery_claim_where(claim)
    with _lock, _conn() as c:
        cursor = c.execute(
            "UPDATE reminder_delivery_claims SET state='uncertain' "
            f"WHERE {where} AND state='sending'",
            params,
        )
    return cursor.rowcount == 1


def mark_reminder_delivery_claims_uncertain(claims: list[dict]) -> int:
    return sum(mark_reminder_delivery_claim_uncertain(claim) for claim in claims)


def finalize_natural_reminder_delivery(claim: dict) -> bool:
    """Mark one claimed natural stage without overwriting cancellation."""

    stage = str(claim.get("stage") or "")
    if stage not in _NATURAL_DELIVERY_STAGES:
        return False
    params, where = _delivery_claim_where(claim)
    now = int(_time.time())
    reminder_id = int(claim.get("reminder_id") or 0)
    group_id = str(claim.get("group_id") or "")
    action = str(claim.get("expected_action") or "")
    remind_at = int(claim.get("expected_remind_at") or 0)
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        owned = c.execute(
            f"SELECT 1 FROM reminder_delivery_claims WHERE {where} "
            "AND state='sending'",
            params,
        ).fetchone()
        if owned is None:
            return False
        if stage == "weekly":
            cursor = c.execute(
                "UPDATE reminders SET weekly_count=weekly_count+1, "
                "last_weekly_at=?, last_pushed_at=? "
                "WHERE group_id=? AND reminder_id=? AND action=? AND remind_at=? "
                "AND status IN ('pending', 'cancelled')",
                (now, now, group_id, reminder_id, action, remind_at),
            )
        elif stage == "now":
            cursor = c.execute(
                "UPDATE reminders SET pushed_now=1, last_pushed_at=?, "
                "status=CASE WHEN status='pending' THEN 'done' ELSE status END "
                "WHERE group_id=? AND reminder_id=? AND action=? AND remind_at=? "
                "AND status IN ('pending', 'cancelled')",
                (now, group_id, reminder_id, action, remind_at),
            )
        else:
            column = f"pushed_{stage}"
            cursor = c.execute(
                f"UPDATE reminders SET {column}=1, last_pushed_at=? "
                f"WHERE group_id=? AND reminder_id=? AND action=? AND remind_at=? "
                f"AND status IN ('pending', 'cancelled')",
                (now, group_id, reminder_id, action, remind_at),
            )
        c.execute(
            f"DELETE FROM reminder_delivery_claims WHERE {where}",
            params,
        )
        return cursor.rowcount == 1


def finalize_calendar_reminder_delivery(claim: dict) -> bool:
    """Mark one claimed event offset, then release its delivery fence."""

    offset = int(claim.get("offset") or -1)
    if offset not in _CALENDAR_DELIVERY_OFFSETS:
        return False
    params, where = _delivery_claim_where(claim)
    column = f"reminded_{offset}d"
    now_ms = int(_time.time() * 1000)
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        owned = c.execute(
            f"SELECT 1 FROM reminder_delivery_claims WHERE {where} "
            "AND state='sending'",
            params,
        ).fetchone()
        if owned is None:
            return False
        cursor = c.execute(
            f"UPDATE events SET {column}=? "
            f"WHERE group_id=? AND event_id=? AND title=? AND event_date=? "
            f"AND COALESCE(event_time, '')=? AND {column} IS NULL",
            (
                now_ms,
                str(claim.get("group_id") or ""),
                str(claim.get("source_ref") or ""),
                str(claim.get("expected_title") or ""),
                str(claim.get("expected_event_date") or ""),
                str(claim.get("expected_event_time") or ""),
            ),
        )
        c.execute(
            f"DELETE FROM reminder_delivery_claims WHERE {where}",
            params,
        )
        return cursor.rowcount == 1


def is_reminder_pending(group_id: str, reminder_id: int) -> bool:
    """Return whether the exact current-group reminder is still deliverable."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM reminders "
            "WHERE group_id=? AND reminder_id=? AND status='pending' LIMIT 1",
            (group_id, int(reminder_id)),
        ).fetchone()
    return row is not None


def is_reminder_source_cancelled(
    group_id: str,
    source_kind: str,
    source_ref: str,
) -> bool:
    """Return whether a source-linked reminder has a cancellation tombstone."""
    if not source_kind or not source_ref:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=? "
            "AND status='cancelled' LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
    return row is not None


def complete_pending_reminder_with_confirmation(
    pending_id: int,
    pending_claim_token: str,
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_text: str,
    mention_aliases: list[str] | None,
    confirmation_factory: Callable[[str, dict], str],
) -> tuple[int, str, dict]:
    """Atomically persist a drained reminder, acknowledgement, and terminal state."""
    now = int(_time.time())
    action = _normalize_reminder_text(action)
    source_text = _normalize_reminder_text(source_text)
    mentions = _normalize_mention_aliases(mention_aliases)
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        pending = c.execute(
            "SELECT status, group_id, claim_token FROM pending_reminder_extract "
            "WHERE pending_id=?",
            (int(pending_id),),
        ).fetchone()
        if (
            pending is None
            or pending[0] != "processing"
            or pending[1] != group_id
            or pending[2] != pending_claim_token
        ):
            raise RuntimeError("pending reminder claim is no longer owned")
        reminder_id, outcome = _add_reminder_with_outcome_conn(
            c,
            group_id,
            user_id,
            action,
            remind_at,
            source_text,
            mentions,
            now,
        )
        persisted = _get_reminder_conn(c, reminder_id)
        if persisted is None:
            raise RuntimeError("persisted reminder row is missing")
        confirmation_text = str(confirmation_factory(outcome, persisted) or "").strip()
        if not confirmation_text:
            raise RuntimeError("reminder confirmation text is empty")
        c.execute(
            "INSERT OR IGNORE INTO reminder_confirmation_outbox"
            "(group_id, source_ref, text, created_at, claimed_at, claim_token, status) "
            "VALUES (?, ?, ?, ?, 0, '', 'pending')",
            (
                group_id,
                f"pending_reminder:{int(pending_id)}",
                confirmation_text,
                now,
            ),
        )
        completed = c.execute(
            "UPDATE pending_reminder_extract "
            "SET status='done', claimed_at=0, claim_token='' "
            "WHERE pending_id=? AND status='processing' AND group_id=? "
            "AND claim_token=?",
            (int(pending_id), group_id, pending_claim_token),
        )
        if completed.rowcount != 1:
            raise RuntimeError("pending reminder completion lost its claim")
        return reminder_id, outcome, persisted


def upsert_reminder_for_source(
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_kind: str,
    source_ref: str,
    source_text: str = "",
    mention_aliases: list[str] | None = None,
) -> int | None:
    """依照 source_kind/source_ref upsert pending reminder。

    同一來源只保留一筆 pending reminder，改期/內容更新會更新原紀錄而非新增新紀錄。
    回 reminder_id；重複抽取時回 None 像傳統 add_reminder 不同，因為更新已完成。
    """
    import time
    now = int(time.time())
    action = _normalize_reminder_text(action)
    source_text = _normalize_reminder_text(source_text)
    mentions = _normalize_mention_aliases(mention_aliases)
    mentions_json = json.dumps(mentions, ensure_ascii=False)
    reminder_id: int
    with _lock, _conn() as c:
        existing = c.execute(
            "SELECT reminder_id FROM reminders "
            "WHERE group_id = ? AND status = 'pending' "
            "AND source_kind = ? AND source_ref = ?",
            (group_id, source_kind, source_ref),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE reminders SET "
                "action = ?, remind_at = ?, user_id = ?, source_text = ?, "
                "mention_aliases = ?, source_kind = ?, source_ref = ?, "
                "last_pushed_at = 0, weekly_count = 0, last_weekly_at = 0, "
                "pushed_3d = 0, pushed_1d = 0, "
                "pushed_4hr = 0, pushed_2hr = 0, pushed_1hr = 0, pushed_now = 0, "
                "created_at = ? "
                "WHERE reminder_id = ?",
                (
                    action, remind_at, user_id, source_text, mentions_json,
                    source_kind, source_ref, now, existing[0],
                ),
            )
            reminder_id = existing[0]
        else:
            c.execute(
                "INSERT INTO reminders(group_id, user_id, action, remind_at, "
                "created_at, status, source_kind, source_ref, source_text, mention_aliases) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    group_id, user_id, action, remind_at,
                    now, source_kind, source_ref, source_text, mentions_json,
                ),
            )
            reminder_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    delete_duplicate_pending_reminders(group_id)
    if get_reminder(reminder_id) is not None:
        return reminder_id
    with _conn() as c:
        survivor = c.execute(
            "SELECT reminder_id FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=? "
            "ORDER BY reminder_id LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
    return int(survivor[0]) if survivor is not None else None


def ensure_reminder_for_source(
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_kind: str,
    source_ref: str,
    source_text: str = "",
    mention_aliases: list[str] | None = None,
) -> int | None:
    """Insert a missing source mirror without reviving an existing tombstone.

    This is intentionally different from event update/sync upserts: any
    existing source identity, including ``done``, ``expired`` or ``cancelled``,
    is preserved byte-for-byte. It is safe to use as a legacy backfill before
    delivery or cancellation.
    """

    group_id = str(group_id or "").strip()
    source_kind = str(source_kind or "").strip()
    source_ref = str(source_ref or "").strip()
    action = _normalize_reminder_text(action)
    if not group_id or not source_kind or not source_ref or not action:
        return None

    now = int(_time.time())
    source_text = _normalize_reminder_text(source_text)
    mentions = _normalize_mention_aliases(mention_aliases)
    mentions_json = json.dumps(mentions, ensure_ascii=False)
    created = False
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute(
            "SELECT reminder_id FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=? "
            "ORDER BY CASE status "
            "WHEN 'cancelled' THEN 0 WHEN 'pending' THEN 1 "
            "WHEN 'done' THEN 2 WHEN 'expired' THEN 3 ELSE 4 END, reminder_id "
            "LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        cursor = c.execute(
            "INSERT INTO reminders("
            "group_id, user_id, action, remind_at, created_at, status, "
            "source_kind, source_ref, source_text, mention_aliases"
            ") VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (
                group_id,
                str(user_id or ""),
                action,
                int(remind_at),
                now,
                source_kind,
                source_ref,
                source_text,
                mentions_json,
            ),
        )
        reminder_id = int(cursor.lastrowid)
        created = True

    if created:
        delete_duplicate_pending_reminders(group_id)
        row = get_reminder(reminder_id)
        if row is not None:
            return reminder_id
        # Dedup may have preferred an equivalent source-backed row created by
        # another process. Resolve the durable source identity after the merge.
        with _conn() as c:
            row = c.execute(
                "SELECT reminder_id FROM reminders "
                "WHERE group_id=? AND source_kind=? AND source_ref=? "
                "ORDER BY reminder_id LIMIT 1",
                (group_id, source_kind, source_ref),
            ).fetchone()
        return int(row[0]) if row is not None else None
    return reminder_id


def synchronize_pending_reminder_for_source(
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_kind: str,
    source_ref: str,
    source_text: str = "",
    mention_aliases: list[str] | None = None,
    *,
    require_active_calendar_event: bool = False,
) -> int | None:
    """Atomically create or refresh one pending source mirror.

    Existing delivery counters/flags are preserved. Terminal source rows and
    inactive calendar events are durable tombstones and are never revived.
    """

    group_id = str(group_id or "").strip()
    source_kind = str(source_kind or "").strip()
    source_ref = str(source_ref or "").strip()
    action = _normalize_reminder_text(action)
    if not group_id or not source_kind or not source_ref or not action:
        return None
    source_text = _normalize_reminder_text(source_text)
    mentions_json = json.dumps(
        _normalize_mention_aliases(mention_aliases),
        ensure_ascii=False,
    )
    now = int(_time.time())
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if require_active_calendar_event:
            event = c.execute(
                "SELECT 1 FROM events WHERE group_id=? AND event_id=? "
                "AND status='active' LIMIT 1",
                (group_id, source_ref),
            ).fetchone()
            if event is None:
                return None
        rows = c.execute(
            "SELECT reminder_id, status FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=? "
            "ORDER BY reminder_id",
            (group_id, source_kind, source_ref),
        ).fetchall()
        if len(rows) > 1:
            return None
        if rows:
            reminder_id, status = int(rows[0][0]), str(rows[0][1])
            if status != "pending":
                return None
            c.execute(
                "UPDATE reminders SET user_id=?, action=?, remind_at=?, "
                "source_text=?, mention_aliases=? "
                "WHERE reminder_id=? AND status='pending'",
                (
                    str(user_id or ""),
                    action,
                    int(remind_at),
                    source_text,
                    mentions_json,
                    reminder_id,
                ),
            )
        else:
            cursor = c.execute(
                "INSERT INTO reminders("
                "group_id, user_id, action, remind_at, created_at, status, "
                "source_kind, source_ref, source_text, mention_aliases"
                ") VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    group_id,
                    str(user_id or ""),
                    action,
                    int(remind_at),
                    now,
                    source_kind,
                    source_ref,
                    source_text,
                    mentions_json,
                ),
            )
            reminder_id = int(cursor.lastrowid)

    delete_duplicate_pending_reminders(group_id)
    with _conn() as c:
        survivor = c.execute(
            "SELECT reminder_id FROM reminders WHERE group_id=? "
            "AND source_kind=? AND source_ref=? AND status='pending' "
            "ORDER BY reminder_id LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
    return int(survivor[0]) if survivor is not None else None


def upsert_reminder_for_source_any_status(
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_kind: str,
    source_ref: str,
    source_text: str = "",
    mention_aliases: list[str] | None = None,
) -> int | None:
    """依 source_kind/source_ref upsert reminder。

    差異在於：若已有相同來源但已是 done/expired，會改回 pending 並更新內容；
    cancelled 是 durable tombstone，不得由同步流程改回 pending。
    這主要用在「events 與 reminders 強一致」的資料修補流程。
    """
    import time
    now = int(time.time())
    action = _normalize_reminder_text(action)
    source_text = _normalize_reminder_text(source_text)
    mentions = _normalize_mention_aliases(mention_aliases)
    mentions_json = json.dumps(mentions, ensure_ascii=False)
    reminder_id: int

    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cancelled = c.execute(
            "SELECT reminder_id FROM reminders "
            "WHERE group_id = ? AND source_kind = ? AND source_ref = ? "
            "AND status = 'cancelled' ORDER BY reminder_id LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
        if cancelled:
            return int(cancelled[0])

        row = c.execute(
            "SELECT reminder_id, status FROM reminders "
            "WHERE group_id = ? AND source_kind = ? AND source_ref = ? "
            "ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, reminder_id "
            "LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()

        if row:
            reminder_id = row[0]
            c.execute(
                "UPDATE reminders SET "
                "action = ?, remind_at = ?, user_id = ?, source_text = ?, "
                "mention_aliases = ?, source_kind = ?, source_ref = ?, "
                "status = 'pending', last_pushed_at = 0, weekly_count = 0, "
                "last_weekly_at = 0, pushed_3d = 0, pushed_1d = 0, "
                "pushed_4hr = 0, pushed_2hr = 0, pushed_1hr = 0, pushed_now = 0, "
                "created_at = ? "
                "WHERE reminder_id = ?",
                (
                    action,
                    remind_at,
                    user_id,
                    source_text,
                    mentions_json,
                    source_kind,
                    source_ref,
                    now,
                    reminder_id,
                ),
            )
        else:
            c.execute(
                "INSERT INTO reminders(group_id, user_id, action, remind_at, "
                "created_at, status, source_kind, source_ref, source_text, mention_aliases) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    group_id,
                    user_id,
                    action,
                    remind_at,
                    now,
                    source_kind,
                    source_ref,
                    source_text,
                    mentions_json,
                ),
            )
            reminder_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    delete_duplicate_pending_reminders(group_id)
    if get_reminder(reminder_id) is not None:
        return reminder_id
    with _conn() as c:
        survivor = c.execute(
            "SELECT reminder_id FROM reminders "
            "WHERE group_id=? AND source_kind=? AND source_ref=? "
            "ORDER BY reminder_id LIMIT 1",
            (group_id, source_kind, source_ref),
        ).fetchone()
    return int(survivor[0]) if survivor is not None else None


def mark_reminder_done_for_source(
    group_id: str, source_kind: str, source_ref: str
) -> bool:
    """依照來源標記 pending reminder 完成。"""
    with _lock, _conn() as c:
        cursor = c.execute(
            "UPDATE reminders SET status='done' "
            "WHERE group_id = ? AND status='pending' "
            "AND source_kind = ? AND source_ref = ?",
            (group_id, source_kind, source_ref),
        )
        return cursor.rowcount > 0


def delete_pending_reminders_by_source(
    source_kind: str,
    keep_source_refs: list[str] | None = None,
    group_id: str | None = None,
) -> int:
    """刪除指定 source_kind 的 pending reminders，保留 keep_source_refs。

    用途：events 與 reminders 同步時，將已不存在的事件對應提醒清掉。
    """
    keep = [str(ref) for ref in (keep_source_refs or []) if str(ref)]
    with _lock, _conn() as c:
        if keep:
            placeholders = ",".join(["?"] * len(keep))
            if group_id is not None:
                sql = (
                    "DELETE FROM reminders "
                    "WHERE status='pending' AND source_kind = ? "
                    "AND group_id = ? AND source_ref NOT IN (" + placeholders + ")"
                )
                params = [source_kind, group_id, *keep]
            else:
                sql = (
                    "DELETE FROM reminders "
                    "WHERE status='pending' AND source_kind = ? "
                    "AND source_ref NOT IN (" + placeholders + ")"
                )
                params = [source_kind, *keep]
        else:
            if group_id is not None:
                sql = (
                    "DELETE FROM reminders "
                    "WHERE status='pending' AND source_kind = ? AND group_id = ?"
                )
                params = [source_kind, group_id]
            else:
                sql = (
                    "DELETE FROM reminders "
                    "WHERE status='pending' AND source_kind = ?"
                )
                params = [source_kind]

        cursor = c.execute(sql, params)
        return cursor.rowcount


def _normalize_mention_aliases(aliases: list[str] | None) -> list[str]:
    out: list[str] = []
    for alias in aliases or []:
        value = str(alias or "").strip().lstrip("@").strip()
        if value and value not in out:
            out.append(value)
    return out


def _load_mention_aliases(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except Exception:
        return []
    if not isinstance(loaded, list):
        return []
    return _normalize_mention_aliases([str(item) for item in loaded])


def _merge_mention_aliases_json(existing_json: str, new_aliases: list[str]) -> str:
    merged = _normalize_mention_aliases([
        *_load_mention_aliases(existing_json),
        *new_aliases,
    ])
    return json.dumps(merged, ensure_ascii=False)


def _normalize_reminder_text(text: str | None) -> str:
    """Normalize common ASR/OCR slips before reminder dedup/merge."""
    if not text:
        return ""
    out = str(text).strip()
    out = re.sub(r"嗎[？?]\s*那", "嗎哪", out)
    replacements = {
        "茶几": "查經",
        "雞腿腿飯": "雞腿飯",
    }
    for old, new in replacements.items():
        out = out.replace(old, new)
    return out


def _reminder_equivalence_key(text: str | None) -> str:
    """Canonical key shared by cancellation, claims, and reminder dedupe."""

    normalized = unicodedata.normalize("NFKC", _normalize_reminder_text(text))
    return " ".join(normalized.split()).casefold()


def _reminder_topic(action: str) -> str:
    text = _normalize_reminder_text(action)
    if "嗎哪小組" in text:
        return "mana_group"
    return ""


def _merge_reminder_action(existing_action: str, new_action: str) -> str | None:
    existing = _normalize_reminder_text(existing_action)
    new = _normalize_reminder_text(new_action)
    topic = _reminder_topic(existing)
    if not topic or topic != _reminder_topic(new):
        return None
    if topic == "mana_group":
        return _merge_mana_group_action(existing, new)
    return new if len(new) > len(existing) else existing


def _merge_mana_group_action(existing: str, new: str) -> str:
    combined = f"{existing} {new}"
    prefix = "媽媽行程：" if "媽媽" in combined else ""
    details: list[str] = []
    if "教會4樓" in combined and "教會4樓" not in details:
        details.append("教會4樓")
    duration = re.search(r"\d{1,2}:\d{2}\s*[-~－—]\s*\d{1,2}:\d{2}", combined)
    if duration:
        details.append(duration.group(0).replace(" ", ""))
    suffix = f"（{'，'.join(details)}）" if details else ""
    return f"{prefix}嗎哪小組查經{suffix}"


def _merge_reminder_source(existing_source: str, new_source: str) -> str:
    parts = []
    for value in (existing_source, new_source):
        value = _normalize_reminder_text(value)
        if value and value not in parts:
            parts.append(value)
    return "\n---\n".join(parts)[:800]


_REMINDER_PUSH_FLAG_COLUMNS: tuple[str, ...] = (
    "last_pushed_at",
    "weekly_count",
    "last_weekly_at",
    "pushed_3d",
    "pushed_1d",
    "pushed_4hr",
    "pushed_2hr",
    "pushed_1hr",
    "pushed_now",
)


def _reminder_source_priority(row: sqlite3.Row) -> int:
    source_kind = str(row["source_kind"] or "")
    source_ref = str(row["source_ref"] or "")
    if source_kind == "calendar_event" and source_ref:
        return 0
    if source_kind and source_ref:
        return 1
    return 2


def _best_duplicate_reminder(rows: list[sqlite3.Row]) -> sqlite3.Row:
    return min(
        rows,
        key=lambda row: (
            _reminder_source_priority(row),
            int(row["reminder_id"]),
        ),
    )


def _merged_duplicate_source_text(keep: sqlite3.Row, rows: list[sqlite3.Row]) -> str:
    keep_source = _normalize_reminder_text(keep["source_text"])
    if str(keep["source_kind"] or "") == "calendar_event":
        if keep_source:
            return keep_source
        for row in rows:
            source = _normalize_reminder_text(row["source_text"])
            if source:
                return source
        return ""

    merged = ""
    for row in rows:
        merged = _merge_reminder_source(merged, str(row["source_text"] or ""))
    return merged


def delete_duplicate_pending_reminders(
    group_id: str | None = None,
    remind_at_tolerance_seconds: int = 60,
) -> int:
    """Delete duplicate pending reminders and merge lightweight metadata.

    Duplicate means same group, same normalized action, and near-identical
    remind_at. Source-backed reminders, especially calendar events, are kept
    over generic extracted rows because event sync can regenerate them.
    Same-priority ties keep the lowest reminder_id. Mention aliases are unioned,
    and push-stage flags use the max value so already-sent stages are preserved.
    """
    tolerance = max(0, int(remind_at_tolerance_seconds))
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.row_factory = sqlite3.Row
        if group_id is not None:
            rows = c.execute(
                "SELECT reminder_id, group_id, user_id, action, remind_at, "
                "created_at, source_kind, source_ref, source_text, "
                "last_pushed_at, weekly_count, last_weekly_at, pushed_3d, "
                "pushed_1d, pushed_4hr, pushed_2hr, pushed_1hr, pushed_now, "
                "mention_aliases "
                "FROM reminders WHERE status='pending' AND group_id=? "
                "ORDER BY group_id, action, remind_at, reminder_id",
                (group_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT reminder_id, group_id, user_id, action, remind_at, "
                "created_at, source_kind, source_ref, source_text, "
                "last_pushed_at, weekly_count, last_weekly_at, pushed_3d, "
                "pushed_1d, pushed_4hr, pushed_2hr, pushed_1hr, pushed_now, "
                "mention_aliases "
                "FROM reminders WHERE status='pending' "
                "ORDER BY group_id, action, remind_at, reminder_id",
            ).fetchall()
        rows = sorted(
            rows,
            key=lambda row: (
                str(row["group_id"] or ""),
                _reminder_equivalence_key(row["action"]),
                int(row["remind_at"] or 0),
                int(row["reminder_id"]),
            ),
        )

        def flush(cluster: list[sqlite3.Row]) -> int:
            if len(cluster) < 2:
                return 0

            source_identities = {
                (
                    str(row["source_kind"] or ""),
                    str(row["source_ref"] or ""),
                )
                for row in cluster
                if str(row["source_kind"] or "")
                and str(row["source_ref"] or "")
            }
            if len(source_identities) > 1:
                # Equal-looking calendar events are still separate durable
                # identities. A generic row cannot be assigned to either one
                # without guessing, so preserve the whole ambiguous cluster.
                return 0

            cluster_ids = [int(row["reminder_id"]) for row in cluster]
            claim_placeholders = ",".join(["?"] * len(cluster_ids))
            protected_claim_rows = c.execute(
                "SELECT DISTINCT subject_ref, occurrence "
                "FROM reminder_delivery_claims "
                "WHERE delivery_kind='natural' "
                "AND state IN ('sending', 'uncertain') "
                f"AND subject_ref IN ({claim_placeholders})",
                [str(reminder_id) for reminder_id in cluster_ids],
            ).fetchall()
            protected_ids = {int(row[0]) for row in protected_claim_rows}
            protected_occurrences = {
                str(row[1] or "") for row in protected_claim_rows
            }
            if len(protected_ids) > 1:
                # This should be unreachable after semantic claim fencing, but
                # old databases may already contain conflicting live claims.
                # Preserve every identity rather than guess which accepted.
                return 0

            preferred = _best_duplicate_reminder(cluster)
            if protected_ids:
                protected_id = next(iter(protected_ids))
                keep = next(
                    row
                    for row in cluster
                    if int(row["reminder_id"]) == protected_id
                )
            else:
                keep = preferred
            keep_id = int(keep["reminder_id"])
            duplicate_ids = [
                int(row["reminder_id"])
                for row in cluster
                if int(row["reminder_id"]) != keep_id
            ]
            if not duplicate_ids:
                return 0

            merge_order = [
                keep,
                *[
                    row for row in cluster
                    if int(row["reminder_id"]) != keep_id
                ],
            ]
            merged_aliases: list[str] = []
            for row in merge_order:
                merged_aliases.extend(_load_mention_aliases(row["mention_aliases"]))
            merged_aliases_json = json.dumps(
                _normalize_mention_aliases(merged_aliases),
                ensure_ascii=False,
            )
            merged_source = _merged_duplicate_source_text(keep, cluster)
            source_kind = str(keep["source_kind"] or "")
            source_ref = str(keep["source_ref"] or "")
            if not source_kind or not source_ref:
                source_kind = str(preferred["source_kind"] or "")
                source_ref = str(preferred["source_ref"] or "")
            user_id = str(keep["user_id"] or "")
            if not user_id:
                user_id = next(
                    (str(row["user_id"]) for row in cluster if row["user_id"]),
                    "",
                )
            created_at = min(int(row["created_at"] or 0) for row in cluster)
            push_values = {
                col: max(int(row[col] or 0) for row in cluster)
                for col in _REMINDER_PUSH_FLAG_COLUMNS
            }
            if any(
                occurrence.startswith("weekly:")
                for occurrence in protected_occurrences
            ):
                # The active weekly claim owns this exact counter/retry-key
                # occurrence. Merging a reset/advanced duplicate counter would
                # strand stale-claim recovery on a different occurrence.
                push_values["weekly_count"] = int(keep["weekly_count"] or 0)

            placeholders = ",".join(["?"] * len(duplicate_ids))
            c.execute(
                "UPDATE sent_reminder_refs SET reminder_id=? "
                f"WHERE reminder_id IN ({placeholders})",
                (keep_id, *duplicate_ids),
            )
            if source_kind and source_ref:
                c.execute(
                    "UPDATE sent_reminder_refs SET "
                    "source_kind=CASE WHEN source_kind='' THEN ? ELSE source_kind END, "
                    "source_ref=CASE WHEN source_ref='' THEN ? ELSE source_ref END "
                    "WHERE reminder_id=?",
                    (source_kind, source_ref, keep_id),
                )
                c.execute(
                    "UPDATE reminder_delivery_claims SET "
                    "source_kind=CASE WHEN source_kind='' THEN ? ELSE source_kind END, "
                    "source_ref=CASE WHEN source_ref='' THEN ? ELSE source_ref END "
                    "WHERE delivery_kind='natural' AND subject_ref=? "
                    "AND state IN ('sending', 'uncertain')",
                    (source_kind, source_ref, str(keep_id)),
                )
            cursor = c.execute(
                f"DELETE FROM reminders WHERE reminder_id IN ({placeholders})",
                duplicate_ids,
            )
            c.execute(
                "UPDATE reminders SET user_id=?, source_kind=?, source_ref=?, "
                "source_text=?, mention_aliases=?, created_at=?, "
                "last_pushed_at=?, weekly_count=?, last_weekly_at=?, "
                "pushed_3d=?, pushed_1d=?, pushed_4hr=?, pushed_2hr=?, "
                "pushed_1hr=?, pushed_now=? WHERE reminder_id=?",
                (
                    user_id,
                    source_kind,
                    source_ref,
                    merged_source,
                    merged_aliases_json,
                    created_at,
                    push_values["last_pushed_at"],
                    push_values["weekly_count"],
                    push_values["last_weekly_at"],
                    push_values["pushed_3d"],
                    push_values["pushed_1d"],
                    push_values["pushed_4hr"],
                    push_values["pushed_2hr"],
                    push_values["pushed_1hr"],
                    push_values["pushed_now"],
                    keep_id,
                ),
            )
            return cursor.rowcount

        deleted = 0
        cluster: list[sqlite3.Row] = []
        cluster_key: tuple[str, str] | None = None
        cluster_start_ts = 0

        for row in rows:
            action = _reminder_equivalence_key(row["action"])
            if not action:
                deleted += flush(cluster)
                cluster = []
                cluster_key = None
                continue

            key = (str(row["group_id"] or ""), action)
            row_ts = int(row["remind_at"] or 0)
            if (
                cluster
                and cluster_key == key
                and abs(row_ts - cluster_start_ts) <= tolerance
            ):
                cluster.append(row)
                continue

            deleted += flush(cluster)
            cluster = [row]
            cluster_key = key
            cluster_start_ts = row_ts

        deleted += flush(cluster)
        return deleted


# ── Reminder creation confirmations ──────────────────────────────────────────


def enqueue_reminder_confirmation(
    group_id: str,
    source_ref: str,
    text: str,
) -> int | None:
    """Persist one idempotent reminder acknowledgement for later piggyback."""
    import time

    group_id = str(group_id or "").strip()
    source_ref = str(source_ref or "").strip()
    text = str(text or "").strip()
    if not group_id or not source_ref or not text:
        return None
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO reminder_confirmation_outbox"
            "(group_id, source_ref, text, created_at, claimed_at, status) "
            "VALUES (?, ?, ?, ?, 0, 'pending')",
            (group_id, source_ref, text, int(time.time())),
        )
        if cur.rowcount == 0:
            row = c.execute(
                "SELECT confirmation_id FROM reminder_confirmation_outbox "
                "WHERE group_id=? AND source_ref=?",
                (group_id, source_ref),
            ).fetchone()
            return int(row[0]) if row else None
        return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def claim_reminder_confirmations(
    group_id: str,
    limit: int = 4,
    stale_after_sec: int = 600,
) -> list[dict]:
    """Claim pending acknowledgements so concurrent replies cannot double-send."""
    if not group_id or limit <= 0:
        return []
    now = int(_time.time())
    cutoff = now - max(1, int(stale_after_sec))
    claimed: list[dict] = []
    with _lock, _conn() as c:
        c.execute(
            "UPDATE reminder_confirmation_outbox "
            "SET status='pending', claimed_at=0, claim_token='' "
            "WHERE group_id=? AND status='sending' AND claimed_at < ?",
            (group_id, cutoff),
        )
        rows = c.execute(
            "SELECT confirmation_id, source_ref, text, created_at "
            "FROM reminder_confirmation_outbox "
            "WHERE group_id=? AND status='pending' "
            "ORDER BY created_at, confirmation_id LIMIT ?",
            (group_id, int(limit)),
        ).fetchall()
        for confirmation_id, source_ref, text, created_at in rows:
            claim_token = uuid.uuid4().hex
            cur = c.execute(
                "UPDATE reminder_confirmation_outbox "
                "SET status='sending', claimed_at=?, claim_token=? "
                "WHERE confirmation_id=? AND group_id=? AND status='pending'",
                (now, claim_token, confirmation_id, group_id),
            )
            if cur.rowcount == 1:
                claimed.append(
                    {
                        "confirmation_id": int(confirmation_id),
                        "source_ref": str(source_ref),
                        "text": str(text),
                        "created_at": int(created_at),
                        "claim_token": claim_token,
                    }
                )
    return claimed


def release_reminder_confirmations(
    group_id: str,
    claims: list[tuple[int, str]],
) -> int:
    """Return claimed acknowledgements to pending after a failed LINE reply."""
    normalized = [
        (int(confirmation_id), str(claim_token))
        for confirmation_id, claim_token in claims
        if int(confirmation_id) > 0 and str(claim_token)
    ]
    if not group_id or not normalized:
        return 0
    released = 0
    with _lock, _conn() as c:
        for confirmation_id, claim_token in normalized:
            cur = c.execute(
                "UPDATE reminder_confirmation_outbox "
                "SET status='pending', claimed_at=0, claim_token='' "
                "WHERE group_id=? AND status='sending' "
                "AND confirmation_id=? AND claim_token=?",
                (group_id, confirmation_id, claim_token),
            )
            released += int(cur.rowcount)
    return released


def delete_sent_reminder_confirmations(
    group_id: str,
    claims: list[tuple[int, str]],
) -> int:
    """Delete acknowledgements only after LINE accepted the piggyback reply."""
    normalized = [
        (int(confirmation_id), str(claim_token))
        for confirmation_id, claim_token in claims
        if int(confirmation_id) > 0 and str(claim_token)
    ]
    if not group_id or not normalized:
        return 0
    deleted = 0
    with _lock, _conn() as c:
        for confirmation_id, claim_token in normalized:
            cur = c.execute(
                "DELETE FROM reminder_confirmation_outbox "
                "WHERE group_id=? AND status='sending' "
                "AND confirmation_id=? AND claim_token=?",
                (group_id, confirmation_id, claim_token),
            )
            deleted += int(cur.rowcount)
    return deleted


# ── Pending reminder extract（quota 爆時入隊、恢復後補抽，2026-05-30 加）──────────
# forward-only：只存「當下 Gemini 不可用而無法抽取」的訊息。drain 從不重掃
# raw_messages（否則重抽已 backfill 的舊訊息 → Gemini action 字串與手寫不同 →
# 繞過 add_reminder 去重 → 製造重複）。


def enqueue_pending_reminder(
    group_id: str,
    user_id: str,
    text: str,
    message_id: str | None = None,
) -> int | None:
    """quota 爆時把含日期+時間 hint 的訊息入隊。INSERT OR IGNORE（partial unique
    message_id 去重）。回 pending_id；重複/失敗回 None。"""
    import time
    now = int(time.time())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO pending_reminder_extract"
            "(group_id, user_id, message_id, text, created_at, retries, status) "
            "VALUES (?, ?, ?, ?, ?, 0, 'pending')",
            (group_id, user_id, message_id, text, now),
        )
        if cur.rowcount == 0:
            return None
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_pending_reminder_extract_by_message(
    group_id: str,
    message_id: str,
) -> dict | None:
    """Return the exact group/message extraction row regardless of status."""

    if not group_id or not message_id:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT pending_id, group_id, user_id, message_id, text, created_at, "
            "retries, claimed_at, claim_token, status "
            "FROM pending_reminder_extract WHERE group_id=? AND message_id=?",
            (group_id, message_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "pending_id": int(row[0]),
        "group_id": str(row[1]),
        "user_id": str(row[2] or ""),
        "message_id": str(row[3] or ""),
        "text": str(row[4] or ""),
        "created_at": int(row[5]),
        "retries": int(row[6]),
        "claimed_at": int(row[7] or 0),
        "claim_token": str(row[8] or ""),
        "status": str(row[9]),
    }


def reclaim_stale_pending_reminders(
    max_processing_age_sec: int = 600, group_id: str | None = None
) -> int:
    """Reset processing rows whose worker likely died before release/mark."""
    now = int(_time.time())
    cutoff = now - max_processing_age_sec
    with _lock, _conn() as c:
        if group_id is not None:
            cur = c.execute(
                "UPDATE pending_reminder_extract "
                "SET retries = retries + 1, status='pending', claimed_at=0, claim_token='' "
                "WHERE status='processing' AND claimed_at > 0 "
                "AND claimed_at < ? AND group_id = ?",
                (cutoff, group_id),
            )
        else:
            cur = c.execute(
                "UPDATE pending_reminder_extract "
                "SET retries = retries + 1, status='pending', claimed_at=0, claim_token='' "
                "WHERE status='processing' AND claimed_at > 0 AND claimed_at < ?",
                (cutoff,),
            )
        return cur.rowcount


def list_pending_reminder_retries(group_id: str, limit: int = 5) -> list[dict]:
    """取該 group 待重抽的 pending（status='pending'，舊→新），上限 limit。"""
    reclaim_stale_pending_reminders(group_id=group_id)
    with _conn() as c:
        rows = c.execute(
            "SELECT pending_id, group_id, user_id, message_id, text, created_at, retries "
            "FROM pending_reminder_extract "
            "WHERE group_id = ? AND status = 'pending' "
            "ORDER BY created_at LIMIT ?",
            (group_id, limit),
        ).fetchall()
    return [
        {
            "pending_id": r[0], "group_id": r[1], "user_id": r[2],
            "message_id": r[3], "text": r[4], "created_at": r[5], "retries": r[6],
        }
        for r in rows
    ]


def claim_pending_reminder(pending_id: int) -> str | None:
    """Atomic claim：status pending→processing。成功回 owner token。
    防 piggyback drain 與 cron worker 同時抽同一筆 → 重複 reminder。"""
    claim_token = uuid.uuid4().hex
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract "
            "SET status='processing', claimed_at=?, claim_token=? "
            "WHERE pending_id = ? AND status = 'pending'",
            (int(_time.time()), claim_token, pending_id),
        )
        return claim_token if cur.rowcount == 1 else None


def mark_pending_reminder(pending_id: int, status: str, claim_token: str) -> bool:
    """標記結果：'done'（已抽存）/'dropped'（非提醒、過期）。"""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract "
            "SET status = ?, claimed_at=0, claim_token='' "
            "WHERE pending_id = ? AND status='processing' AND claim_token=?",
            (status, pending_id, claim_token),
        )
        return cur.rowcount == 1


def complete_dropped_pending_reminder(pending_id: int) -> bool:
    """Promote one explicitly repaired dropped extraction to done."""

    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract "
            "SET status='done', claimed_at=0, claim_token='' "
            "WHERE pending_id=? AND status='dropped'",
            (pending_id,),
        )
        return cur.rowcount == 1


def release_pending_reminder(pending_id: int, claim_token: str) -> bool:
    """重抽又撞 quota：retries+1 並退回 'pending' 等下輪 drain。"""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract "
            "SET retries = retries + 1, status='pending', claimed_at=0, claim_token='' "
            "WHERE pending_id = ? AND status='processing' AND claim_token=?",
            (pending_id, claim_token),
        )
        return cur.rowcount == 1


def drop_stale_pending_reminders(max_age_sec: int, group_id: str | None = None) -> int:
    """清超齡 pending（created_at < now-max_age）→ status='dropped'。回清掉筆數。
    不寫任何 plaintext DLQ 檔（PII 只留 DB）。"""
    import time
    cutoff = int(time.time()) - max_age_sec
    with _lock, _conn() as c:
        if group_id is not None:
            cur = c.execute(
                "UPDATE pending_reminder_extract SET status='dropped' "
                "WHERE status IN ('pending','processing') AND created_at < ? "
                "AND group_id = ?",
                (cutoff, group_id),
            )
        else:
            cur = c.execute(
                "UPDATE pending_reminder_extract SET status='dropped' "
                "WHERE status IN ('pending','processing') AND created_at < ?",
                (cutoff,),
            )
        return cur.rowcount


def list_pending_reminder_groups() -> list[str]:
    """回有待重抽 pending 的 distinct group_id（cron backstop 用）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT group_id FROM pending_reminder_extract "
            "WHERE status = 'pending'"
        ).fetchall()
    return [r[0] for r in rows]


def list_pending_reminders(
    group_id: str | None = None,
    within_seconds: int | None = None,
) -> list[dict]:
    """列出 pending reminders。
    - group_id None → 全部 group
    - within_seconds None → 全部未過期；給數字 → 只取「現在 - 1day ~ 現在 + within_seconds」內
    """
    import time
    delete_duplicate_pending_reminders(group_id)
    now = int(time.time())
    with _conn() as c:
        if within_seconds is not None:
            lo = now - 86400  # 包含過去 24h（可能 user 還沒 mark done）
            hi = now + within_seconds
            if group_id:
                rows = c.execute(
                    "SELECT reminder_id, group_id, user_id, action, remind_at, "
                    "created_at, source_kind, source_ref, source_text, mention_aliases "
                    "FROM reminders "
                    "WHERE status='pending' AND group_id=? AND remind_at BETWEEN ? AND ? "
                    "ORDER BY remind_at",
                    (group_id, lo, hi),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT reminder_id, group_id, user_id, action, remind_at, "
                    "created_at, source_kind, source_ref, source_text, mention_aliases "
                    "FROM reminders "
                    "WHERE status='pending' AND remind_at BETWEEN ? AND ? "
                    "ORDER BY remind_at",
                    (lo, hi),
                ).fetchall()
        else:
            if group_id:
                rows = c.execute(
                    "SELECT reminder_id, group_id, user_id, action, remind_at, "
                    "created_at, source_kind, source_ref, source_text, mention_aliases "
                    "FROM reminders "
                    "WHERE status='pending' AND group_id=? AND remind_at >= ? "
                    "ORDER BY remind_at",
                    (group_id, now - 86400),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT reminder_id, group_id, user_id, action, remind_at, "
                    "created_at, source_kind, source_ref, source_text, mention_aliases "
                    "FROM reminders "
                    "WHERE status='pending' AND remind_at >= ? "
                    "ORDER BY remind_at",
                    (now - 86400,),
                ).fetchall()
    return [
        {
            "reminder_id": r[0],
            "group_id": r[1],
            "user_id": r[2],
            "action": r[3],
            "remind_at": r[4],
            "created_at": r[5],
            "source_kind": r[6] or "",
            "source_ref": r[7] or "",
            "source_text": r[8] or "",
            "mention_aliases": _load_mention_aliases(r[9]),
        }
        for r in rows
    ]


def mark_reminder_done(reminder_id: int) -> bool:
    """標記 reminder 完成。"""
    with _lock, _conn() as c:
        cursor = c.execute(
            "UPDATE reminders SET status='done' WHERE reminder_id=?",
            (reminder_id,),
        )
        return cursor.rowcount > 0


def update_reminder_schedule(
    reminder_id: int,
    remind_at: int,
    source_text: str | None = None,
    action: str | None = None,
) -> bool:
    """Update a pending reminder's schedule and reset push-stage flags."""
    action = _normalize_reminder_text(action) if action is not None else None
    source_text = (
        _normalize_reminder_text(source_text) if source_text is not None else None
    )
    with _lock, _conn() as c:
        if action is not None:
            cur = c.execute(
                "UPDATE reminders SET action = ?, remind_at = ?, "
                "source_text = COALESCE(?, source_text), "
                "last_pushed_at = 0, weekly_count = 0, last_weekly_at = 0, "
                "pushed_3d = 0, pushed_1d = 0, pushed_4hr = 0, "
                "pushed_2hr = 0, pushed_1hr = 0, pushed_now = 0 "
                "WHERE reminder_id = ? AND status = 'pending'",
                (action, remind_at, source_text, reminder_id),
            )
        else:
            cur = c.execute(
                "UPDATE reminders SET remind_at = ?, "
                "source_text = COALESCE(?, source_text), "
                "last_pushed_at = 0, weekly_count = 0, last_weekly_at = 0, "
                "pushed_3d = 0, pushed_1d = 0, pushed_4hr = 0, "
                "pushed_2hr = 0, pushed_1hr = 0, pushed_now = 0 "
                "WHERE reminder_id = ? AND status = 'pending'",
                (remind_at, source_text, reminder_id),
            )
        return cur.rowcount > 0


def delete_stale_pending_reminders(
    grace_seconds: int = 3600,
    group_id: str | None = None,
) -> int:
    """Clean pending reminders that are clearly past their due window.

    `reminder_push` still has a ±15 minute "now" stage. A 1-hour default grace
    keeps that path intact. Generic rows are deleted. Source-linked rows become
    ``expired`` instead: later calendar offsets still need that durable row so
    a user can cancel future notifications without deleting the calendar event.
    """
    import time

    cutoff = int(time.time()) - max(0, int(grace_seconds))
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if group_id is not None:
            expired = c.execute(
                "UPDATE reminders SET status='expired' "
                "WHERE status='pending' AND group_id=? AND remind_at < ? "
                "AND source_kind<>'' AND source_ref<>''",
                (group_id, cutoff),
            ).rowcount
            deleted = c.execute(
                "DELETE FROM reminders "
                "WHERE status='pending' AND group_id=? AND remind_at < ? "
                "AND (source_kind='' OR source_ref='')",
                (group_id, cutoff),
            ).rowcount
        else:
            expired = c.execute(
                "UPDATE reminders SET status='expired' "
                "WHERE status='pending' AND remind_at < ? "
                "AND source_kind<>'' AND source_ref<>''",
                (cutoff,),
            ).rowcount
            deleted = c.execute(
                "DELETE FROM reminders "
                "WHERE status='pending' AND remind_at < ? "
                "AND (source_kind='' OR source_ref='')",
                (cutoff,),
            ).rowcount
        return int(expired) + int(deleted)


def expire_old_reminders(threshold_seconds: int = 86400 * 3) -> int:
    """把過期超過 threshold（預設 3 天）的 pending reminder 標記 expired。回標記筆數。"""
    import time
    cutoff = int(time.time()) - threshold_seconds
    with _lock, _conn() as c:
        cursor = c.execute(
            "UPDATE reminders SET status='expired' "
            "WHERE status='pending' AND remind_at < ?",
            (cutoff,),
        )
        return cursor.rowcount


def list_pending_reminders_full(group_id: str | None = None) -> list[dict]:
    """完整版 list — 含所有 stage flag 給 reminder_push.py 用。"""
    import time
    delete_duplicate_pending_reminders(group_id)
    now = int(time.time())
    with _conn() as c:
        if group_id:
            rows = c.execute(
                "SELECT reminder_id, group_id, user_id, action, remind_at, "
                "created_at, source_kind, source_ref, source_text, "
                "last_pushed_at, weekly_count, "
                "last_weekly_at, pushed_3d, pushed_1d, "
                "pushed_4hr, pushed_2hr, pushed_1hr, pushed_now, "
                "mention_aliases "
                "FROM reminders WHERE status='pending' AND group_id=? AND remind_at >= ? "
                "ORDER BY remind_at",
                (group_id, now - 86400),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT reminder_id, group_id, user_id, action, remind_at, "
                "created_at, source_kind, source_ref, source_text, "
                "last_pushed_at, weekly_count, "
                "last_weekly_at, pushed_3d, pushed_1d, "
                "pushed_4hr, pushed_2hr, pushed_1hr, pushed_now, "
                "mention_aliases "
                "FROM reminders WHERE status='pending' AND remind_at >= ? "
                "ORDER BY remind_at",
                (now - 86400,),
            ).fetchall()
    return [
        {
            "reminder_id": r[0], "group_id": r[1], "user_id": r[2],
            "action": r[3], "remind_at": r[4], "created_at": r[5],
            "source_kind": r[6] or "", "source_ref": r[7] or "",
            "source_text": r[8] or "",
            "last_pushed_at": r[9], "weekly_count": r[10],
            "last_weekly_at": r[11], "pushed_3d": r[12], "pushed_1d": r[13],
            "pushed_4hr": r[14], "pushed_2hr": r[15],
            "pushed_1hr": r[16], "pushed_now": r[17],
            "mention_aliases": _load_mention_aliases(r[18]),
        }
        for r in rows
    ]


def mark_reminder_pushed(reminder_id: int, stage: str) -> bool:
    """把 reminder 在某 stage push 過的 flag 打開。

    stage 可選：
      - 'weekly' → weekly_count += 1, last_weekly_at = now, last_pushed_at = now
      - '3d' / '1d' / '4hr' / '2hr' / '1hr' / 'now' → 對應 flag = 1, last_pushed_at = now
      - 'now' 額外把 status 標為 'done'
    """
    import time
    now = int(time.time())
    with _lock, _conn() as c:
        if stage == "weekly":
            cursor = c.execute(
                "UPDATE reminders SET weekly_count = weekly_count + 1, "
                "last_weekly_at = ?, last_pushed_at = ? "
                "WHERE reminder_id = ? AND status='pending'",
                (now, now, reminder_id),
            )
        elif stage in ("3d", "1d", "4hr", "2hr", "1hr"):
            col = f"pushed_{stage}"
            cursor = c.execute(
                f"UPDATE reminders SET {col} = 1, last_pushed_at = ? "
                f"WHERE reminder_id = ? AND status='pending'",
                (now, reminder_id),
            )
        elif stage == "now":
            cursor = c.execute(
                "UPDATE reminders SET pushed_now = 1, last_pushed_at = ?, "
                "status = 'done' WHERE reminder_id = ? AND status='pending'",
                (now, reminder_id),
            )
        else:
            return False
        return cursor.rowcount == 1


# ── Media cache（圖片 / 影片 byte-exact dedup，Phase 1 from §3 chain）────────


def compute_sha256(data: bytes) -> str:
    """SHA-256 hex digest，給 media_cache lookup key 用。"""
    return hashlib.sha256(data).hexdigest()


def lookup_media_cache(
    group_id: str,
    media_type: str,
    sha256_hex: str,
) -> dict | None:
    """查 media_cache。命中回 dict（cache_id / description / last_reply /
    first_seen_at / last_seen_at / seen_count），miss 回 None。

    PK 含 group_id 防 cross-group leak（同 sha 在不同 group 是獨立 cache）。
    """
    if not group_id or not media_type or not sha256_hex:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT cache_id, description, last_reply, first_seen_at, "
            "last_seen_at, seen_count "
            "FROM media_cache WHERE group_id = ? AND media_type = ? AND sha256 = ?",
            (group_id, media_type, sha256_hex),
        ).fetchone()
    if not row:
        return None
    return {
        "cache_id": row[0],
        "description": row[1],
        "last_reply": row[2],
        "first_seen_at": row[3],
        "last_seen_at": row[4],
        "seen_count": row[5],
    }


def insert_media_cache(
    group_id: str,
    media_type: str,
    sha256_hex: str,
    description: str | None,
    reply: str,
) -> int | None:
    """寫一筆 media_cache。回 cache_id；重複 sha / 空 reply 回 None。

    Quality gate (Phase 1)：reply 空字串 / 純 whitespace 拒絕寫入（防永久空 reply）。
    INSERT OR IGNORE：同 (group_id, media_type, sha) 已存在不覆寫。

    Phase 1.5 deferred (per advisor family-bot threat model):
      - cache_version column 沒加：改 _CORE_PROMPT / vision model / v4 pipeline 後
        要手動 `DELETE FROM media_cache;` invalidate 舊 row（沒自動失效機制）
      - expires_at TTL：對齊 fact_check_cache 7d，但 byte-exact 圖實測再決定
      - In-flight dedup：同 sha 多 thread 同時跑 v4，family 5 人 rare race accept
      - source_msg_id：debug 追溯用，YAGNI Phase 1
    """
    if not group_id or not media_type or not sha256_hex:
        return None
    if not reply or not reply.strip():
        return None
    now = int(_time.time())
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO media_cache"
            "(group_id, media_type, sha256, description, last_reply, "
            "first_seen_at, last_seen_at, seen_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (group_id, media_type, sha256_hex, description, reply, now, now),
        )
        if cur.rowcount == 0:
            return None
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def delete_media_cache(cache_id: int) -> None:
    """Delete one media_cache row, used to invalidate stale cache formats."""
    if not cache_id:
        return
    with _lock, _conn() as c:
        c.execute("DELETE FROM media_cache WHERE cache_id = ?", (cache_id,))


def bump_media_cache_seen(cache_id: int) -> None:
    """命中 cache 後 seen_count +1、last_seen_at = now。caller 顯式 call。"""
    if not cache_id:
        return
    now = int(_time.time())
    with _lock, _conn() as c:
        c.execute(
            "UPDATE media_cache SET seen_count = seen_count + 1, "
            "last_seen_at = ? WHERE cache_id = ?",
            (now, cache_id),
        )
