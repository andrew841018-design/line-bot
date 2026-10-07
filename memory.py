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
import os
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
from sqlite_security import connect_private_sqlite

_DB_PATH = Path(settings.sqlite_path)

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
    conn = connect_private_sqlite(
        _DB_PATH,
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # I3 fix (2026-05-30): 跨 process 寫同一 db（uvicorn handler thread + 獨立 cron
        # process 如 reminder_push.py）時，沒 busy_timeout 會立刻 raise "database is locked"。
        # 設 5s 讓 writer 等鎖釋放而非直接炸。
        conn.execute("PRAGMA busy_timeout=5000")
    except BaseException:
        # The caller never gets the connection, so its closing __exit__ never runs.
        conn.close()
        raise
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


def _add_column(c: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add one column; another process may be doing the same."""
    for attempt in range(8):
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" in message and attempt < 7:
                _time.sleep(0.05 * (attempt + 1))
                continue
            if "duplicate column" not in message:
                raise
        break
    columns = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        raise RuntimeError(f"{table}.{column} migration failed")


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
            CREATE TABLE IF NOT EXISTS raw_message_quotes (
                group_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                quoted_message_id TEXT NOT NULL,
                PRIMARY KEY (group_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS sent_reminder_refs (
                group_id    TEXT NOT NULL,
                message_id  TEXT NOT NULL,
                reminder_id INTEGER,
                source_kind TEXT NOT NULL DEFAULT '',
                source_ref  TEXT NOT NULL DEFAULT '',
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (group_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS reminder_reschedule_log (
                group_id        TEXT NOT NULL,
                message_id      TEXT NOT NULL,
                reminder_id     INTEGER NOT NULL,
                old_remind_at   INTEGER NOT NULL,
                new_remind_at   INTEGER NOT NULL,
                old_action_hash TEXT NOT NULL,
                new_action_hash TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                PRIMARY KEY (group_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_reschedule_log_created
                ON reminder_reschedule_log(created_at);
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
                source     TEXT NOT NULL DEFAULT 'rule_violation',
                    -- 'rule_violation' (黑名單詞觸發) | 'organic' (user 真實糾正)
                correction_linked INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_persona_notes_group
                ON persona_notes(group_id, kind);
            CREATE TABLE IF NOT EXISTS correction_rules (
                rule_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id         TEXT NOT NULL,
                canonical_key    TEXT NOT NULL,
                canonical_rule   TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'active',
                occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count >= 0),
                first_seen_at    INTEGER NOT NULL,
                last_seen_at     INTEGER NOT NULL,
                matcher_version  TEXT NOT NULL DEFAULT 'local-v1',
                semantic_context TEXT NOT NULL DEFAULT '',
                UNIQUE(group_id, canonical_key)
            );
            CREATE INDEX IF NOT EXISTS idx_correction_rules_group_recent
                ON correction_rules(group_id, status, last_seen_at DESC, rule_id DESC);
            CREATE INDEX IF NOT EXISTS idx_correction_rules_group_recurrence
                ON correction_rules(
                    group_id, status, occurrence_count DESC,
                    last_seen_at DESC, rule_id DESC
                );
            CREATE TABLE IF NOT EXISTS correction_observations (
                group_id          TEXT NOT NULL,
                observation_id    TEXT NOT NULL,
                note_id           INTEGER NOT NULL UNIQUE,
                source_message_id TEXT NOT NULL DEFAULT '',
                actor_key         TEXT NOT NULL DEFAULT '',
                scenario          TEXT NOT NULL,
                content           TEXT NOT NULL,
                candidate_rule    TEXT NOT NULL,
                decision          TEXT NOT NULL,
                canonical_rule_id INTEGER,
                match_score       REAL NOT NULL DEFAULT 0,
                matcher_version   TEXT NOT NULL DEFAULT 'local-v1',
                observed_at       INTEGER NOT NULL,
                PRIMARY KEY (group_id, observation_id)
            );
            CREATE INDEX IF NOT EXISTS idx_correction_observations_rule
                ON correction_observations(group_id, canonical_rule_id, observed_at);
            CREATE TABLE IF NOT EXISTS correction_rule_events (
                event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id         TEXT NOT NULL,
                action           TEXT NOT NULL,
                rule_id          INTEGER,
                observation_id   TEXT NOT NULL DEFAULT '',
                payload_json     TEXT NOT NULL DEFAULT '{}',
                reverts_event_id INTEGER,
                created_at       INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_correction_rule_single_undo
                ON correction_rule_events(group_id, reverts_event_id)
                WHERE reverts_event_id IS NOT NULL;
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
        # 2026-10-04: dropped rows close silently; the daily audit reads these.
        for col, ddl in (
            ("dropped_at", "dropped_at INTEGER NOT NULL DEFAULT 0"),
            ("drop_reason", "drop_reason TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in cols:
                _add_column(c, "pending_reminder_extract", col, ddl)
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
        # 2026-10-04: rows stored before this column existed replay as ungrounded.
        fc_cols = {r[1] for r in c.execute("PRAGMA table_info(fact_check_cache)").fetchall()}
        if "grounded" not in fc_cols:
            _add_column(
                c, "fact_check_cache", "grounded", "grounded INTEGER NOT NULL DEFAULT 0"
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
        # 2026-09-28 同事件合併：時間是否明確（NULL＝舊資料、固定）與吸收的說法。
        for col, ddl in (
            ("time_kind", "time_kind TEXT"),
            ("merged_details", "merged_details TEXT NOT NULL DEFAULT '[]'"),
        ):
            if col not in rcols:
                _add_column(c, "reminders", col, ddl)
        _ensure_pending_source_unique_index(c)
        # persona_notes schema migration: add source column if missing
        # 2026-05-08：區分 'rule_violation'（既有黑名單觸發）vs 'organic'（user 真實糾正）
        pn_cols = [r[1] for r in c.execute("PRAGMA table_info(persona_notes)").fetchall()]
        if "source" not in pn_cols:
            c.execute(
                "ALTER TABLE persona_notes ADD COLUMN source TEXT NOT NULL "
                "DEFAULT 'rule_violation'"
            )
        if "correction_linked" not in pn_cols:
            c.execute(
                "ALTER TABLE persona_notes ADD COLUMN correction_linked "
                "INTEGER NOT NULL DEFAULT 0"
            )
        # Idempotent crash repair: ALTER TABLE autocommits in some migration
        # paths, so startup must repair a flag left stale between ALTER/UPDATE.
        c.execute(
            "UPDATE persona_notes SET correction_linked=1 WHERE "
            "source='organic' AND correction_linked=0 AND EXISTS ("
            "SELECT 1 FROM correction_observations o "
            "WHERE o.note_id=persona_notes.note_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_persona_notes_prompt_projection "
            "ON persona_notes(group_id, kind, source, correction_linked, "
            "created_at, note_id)"
        )
        rule_cols = [
            r[1] for r in c.execute("PRAGMA table_info(correction_rules)").fetchall()
        ]
        if "semantic_context" not in rule_cols:
            c.execute(
                "ALTER TABLE correction_rules ADD COLUMN semantic_context "
                "TEXT NOT NULL DEFAULT ''"
            )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_correction_rules_semantic_repair "
            "ON correction_rules(rule_id) "
            "WHERE status='active' AND semantic_context=''"
        )
        # Crash/intermediate-schema repair: a rule may already have absorbed a
        # direction-bearing observation before this column existed.  Rebuild
        # empty state from immutable members before accepting new matches.
        from correction_memory import combine_semantic_contexts

        empty_semantic_rules = c.execute(
            "SELECT rule_id, group_id, canonical_rule FROM correction_rules "
            "WHERE status='active' AND semantic_context=''"
        ).fetchall()
        for semantic_rule_id, semantic_group_id, canonical_rule in empty_semantic_rules:
            member_texts = [
                row[0]
                for row in c.execute(
                    "SELECT candidate_rule FROM correction_observations "
                    "WHERE group_id=? AND canonical_rule_id=? "
                    "ORDER BY observed_at, observation_id",
                    (semantic_group_id, int(semantic_rule_id)),
                ).fetchall()
            ]
            rebuilt_semantic = combine_semantic_contexts(
                member_texts or [canonical_rule]
            )
            c.execute(
                "UPDATE correction_rules SET semantic_context=? "
                "WHERE rule_id=? AND group_id=? AND semantic_context=''",
                (
                    rebuilt_semantic or "none",
                    int(semantic_rule_id),
                    semantic_group_id,
                ),
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


class FactCacheHit(str):
    """A cached reply; ``grounded`` is True only for rows stored with search backing."""

    grounded: bool

    def __new__(cls, text: str, grounded: bool = False):
        hit = super().__new__(cls, text)
        hit.grounded = bool(grounded)
        return hit


def check_fact_cache(group_id: str, text: str) -> FactCacheHit | None:
    """查快取，若命中且未過期回傳 cached result（帶 grounded 標記），否則回 None。"""
    if len(text.strip()) < 80:
        return None
    key = _cache_key(text)
    now = int(_time.time())
    with _conn() as c:
        row = c.execute(
            "SELECT result, grounded FROM fact_check_cache "
            "WHERE group_id = ? AND text_hash = ? AND expires_at > ?",
            (group_id, key, now),
        ).fetchone()
    return FactCacheHit(row[0], grounded=bool(row[1])) if row else None


def store_fact_cache(group_id: str, text: str, result: str, grounded: bool = False) -> None:
    """存入快取，TTL = _CACHE_TTL_DAYS 天。

    2026-10-04: only replies backed by a search are cached (a made-up reply
    cached on 9/30 would otherwise replay for a week); they carry the
    ``grounded`` marker so a replay counts as backed.
    """
    if not grounded or len(text.strip()) < 80:
        return
    key = _cache_key(text)
    now = int(_time.time())
    expires = now + _CACHE_TTL_DAYS * 86400
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO fact_check_cache"
            "(group_id, text_hash, result, created_at, expires_at, grounded) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (group_id, key, result, now, expires),
        )


# ── 計數器（決定何時觸發事實抽取）──────────────────────────────────────────

_RAW_MESSAGE_KEEP = 2000  # 每群組保留最近 N 筆原始訊息（給 quote-reply 查詢用）

# raw_messages is written before routing, so it cannot prove that LINE got a
# reply. Keep a short processing lease for concurrent redeliveries, then allow
# a later delivery to retry an event that never reached a successful reply.
_INBOUND_PROCESSING_LEASE_SECONDS = 30
_INBOUND_MEDIA_PROCESSING_LEASE_SECONDS = 120
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
        if status in {"replied", "completed_no_reply"}:
            return status
        lease_seconds = (
            _INBOUND_MEDIA_PROCESSING_LEASE_SECONDS
            if status == "media_processing"
            else _INBOUND_PROCESSING_LEASE_SECONDS
        )
        if now - int(updated_at) < lease_seconds:
            return "processing"
        c.execute(
            "UPDATE inbound_events SET status = 'processing', updated_at = ? "
            "WHERE group_id = ? AND message_id = ?",
            (now, group_id, message_id),
        )
        return "retry"


def mark_inbound_event_media_processing(group_id: str, message_id: str) -> None:
    """Extend the durable processing claim while bounded media work is active."""
    if not group_id or not message_id:
        return
    now = int(_time.time())
    with _lock, _conn() as c:
        c.execute(
            "UPDATE inbound_events SET status = 'media_processing', updated_at = ? "
            "WHERE group_id = ? AND message_id = ? AND status != 'replied'",
            (now, group_id, message_id),
        )


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


def mark_inbound_events_replied(group_id: str, message_ids: list[str]) -> int:
    """Atomically mark every existing event covered by one accepted reply."""
    ids = list(
        dict.fromkeys(str(message_id) for message_id in message_ids if message_id)
    )
    if not group_id or not ids:
        return 0
    now = int(_time.time())
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            before = c.total_changes
            c.executemany(
                "UPDATE inbound_events SET status = 'replied', updated_at = ? "
                "WHERE group_id = ? AND message_id = ? "
                "AND status NOT IN ('replied', 'completed_no_reply')",
                [(now, group_id, message_id) for message_id in ids],
            )
            marked = c.total_changes - before
            c.execute("COMMIT")
            return marked
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


_TRANSIENT_OPEN_ERROR_RE = re.compile(r"unable to open database file|disk i/o error", re.IGNORECASE)
# One UPDATE per this many ids: in autocommit each statement may wait
# busy_timeout, so a burst waits for the lock once instead of once per message.
_COMPLETE_BATCH = 500


def mark_inbound_events_completed_no_reply(
    group_id: str, message_ids: list[str]
) -> int:
    """Durably close intentionally silent inbound events.

    This is distinct from ``replied``: it prevents redelivery/reply-gap false
    positives without claiming that LINE accepted an outbound message.
    """
    ids = list(
        dict.fromkeys(str(message_id) for message_id in message_ids if message_id)
    )
    if not group_id or not ids:
        return 0
    for attempt in range(2):
        now = int(_time.time())
        try:
            with _lock, _conn() as c:
                before = c.total_changes
                for start in range(0, len(ids), _COMPLETE_BATCH):
                    batch = ids[start:start + _COMPLETE_BATCH]
                    c.execute(
                        "UPDATE inbound_events SET status = 'completed_no_reply', updated_at = ? "
                        "WHERE group_id = ? AND status != 'replied' "
                        f"AND message_id IN ({','.join('?' * len(batch))})",
                        [now, group_id, *batch],
                    )
                return c.total_changes - before
        except sqlite3.OperationalError as exc:
            # One quick retry for a transient open/I-O failure, outside _lock.
            # "database is locked" already waited busy_timeout, so it is final.
            if attempt or not _TRANSIENT_OPEN_ERROR_RE.search(str(exc)):
                raise
            _time.sleep(0.2)
    return 0


def get_inbound_event_status(group_id: str, message_id: str) -> str | None:
    """Return the durable terminal/processing state without claiming the event."""
    if not group_id or not message_id:
        return None
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT status FROM inbound_events "
            "WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
    return str(row[0]) if row else None


def log_raw_message(
    group_id: str, message_id: str, user_id: str | None, text: str,
    *, quoted_message_id: str | None = None, index_for_recall: bool = True,
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
        if isinstance(quoted_message_id, str) and quoted_message_id.strip():
            c.execute(
                "INSERT OR REPLACE INTO raw_message_quotes VALUES (?, ?, ?)",
                (group_id, message_id, quoted_message_id.strip()),
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
        c.execute(
            "DELETE FROM raw_message_quotes WHERE group_id = ? "
            "AND message_id NOT IN (SELECT message_id FROM raw_messages WHERE group_id = ?)",
            (group_id, group_id),
        )
    if not index_for_recall:
        return
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


def raw_message_sender_near(
    group_id: str, at_ts: int, quote: str = "", window_sec: int = 180
) -> str | None:
    """The one family member who sent ``quote`` (or, failing that, the only one
    who wrote anything) in the ``window_sec`` before ``at_ts``; else None.

    Repairs finance views stored before 2026-10-07 without their speaker.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, text FROM raw_messages "
            "WHERE group_id = ? AND created_at BETWEEN ? AND ? "
            "AND user_id IS NOT NULL AND user_id NOT IN ('', '__bot__')",
            (group_id, int(at_ts) - int(window_sec), int(at_ts) + 5),
        ).fetchall()
    needle = "".join((quote or "").split())
    quoted = {uid for uid, text in rows if needle and needle in "".join((text or "").split())}
    candidates = quoted or {uid for uid, _text in rows}
    return next(iter(candidates)) if len(candidates) == 1 else None


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


def get_quoted_message_id(group_id: str, message_id: str) -> str | None:
    """Recover only the exact persisted edge in this group, never a recent guess."""
    if not isinstance(message_id, str) or not message_id:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT quoted_message_id FROM raw_message_quotes WHERE group_id=? AND message_id=?",
            (group_id, message_id),
        ).fetchone()
    return str(row[0]) if row else None


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


def get_contextual_reminder_source(
    group_id: str,
    current_message_id: str,
    *,
    max_age_sec: int = 180,
) -> dict | None:
    """Return the exact preceding same-sender human row for a follow-up.

    Ordering is anchored to the current row's ``(created_at, rowid)`` so two
    messages written in the same second remain deterministic.  One immediate
    bot acknowledgement may sit between the source and the command; another
    human row or more bot chatter makes the context ambiguous and fails closed.
    """

    if not group_id or not current_message_id or max_age_sec <= 0:
        return None
    with _conn() as c:
        current = c.execute(
            "SELECT rowid,user_id,text,created_at FROM raw_messages "
            "WHERE group_id=? AND message_id=?",
            (group_id, current_message_id),
        ).fetchone()
        if current is None or not str(current[1] or ""):
            return None
        rows = c.execute(
            "SELECT rowid,message_id,user_id,text,created_at FROM raw_messages "
            "WHERE group_id=? AND (created_at<? OR (created_at=? AND rowid<?)) "
            "ORDER BY created_at DESC,rowid DESC LIMIT 3",
            (group_id, int(current[3]), int(current[3]), int(current[0])),
        ).fetchall()
    bot_rows: list[str] = []
    source = None
    for row in rows:
        if str(row[2] or "") == "__bot__":
            bot_rows.append(str(row[3] or ""))
            if len(bot_rows) > 1:
                return None
            continue
        source = row
        break
    # This narrow follow-up is valid only after the preceding source was
    # durably acknowledged as a reminder.  Without that fence, its generic
    # extraction can race this four-slot batch and add a fifth legacy row.
    if (
        source is None
        or len(bot_rows) != 1
        or "已新增提醒" not in bot_rows[0]
    ):
        return None
    if str(source[2] or "") != str(current[1] or ""):
        return None
    age = int(current[3]) - int(source[4])
    if age < 0 or age > int(max_age_sec):
        return None
    return {
        "group_id": group_id,
        "message_id": str(source[1]),
        "user_id": str(source[2] or ""),
        "text": str(source[3] or ""),
        "created_at": int(source[4]),
        "current_message_id": current_message_id,
        "current_text": str(current[2] or ""),
        "current_created_at": int(current[3]),
    }


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

_PERSONA_NOTE_CAP = 50
# 每個 group/kind/source 最多保留 50 筆「未連結」note。Canonical organic
# audits 是不可變的 source of truth，不受 FIFO 清理；prompt 只讀投影。


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
        # Retention is source-local.  Canonical organic observations keep an
        # immutable persona-note audit; a burst of automated rule violations
        # must never delete those rows and leave dangling observation links.
        c.execute(
            "DELETE FROM persona_notes WHERE note_id IN ("
            "  SELECT p.note_id FROM persona_notes p "
            "  WHERE p.group_id = ? AND p.kind = ? AND p.source = ? "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM correction_observations o "
            "    WHERE o.note_id = p.note_id"
            "  ) "
            "  ORDER BY p.created_at DESC, p.note_id DESC LIMIT -1 OFFSET ?"
            ")",
            (group_id, kind, source, _PERSONA_NOTE_CAP),
        )
        return note_id


def add_organic_correction(
    group_id: str,
    prev_user_msg: str,
    prev_bot_msg: str,
    correction_msg: str,
    summary: str = "",
    observation_id: str = "",
    source_message_id: str = "",
    actor_key: str = "",
    observed_at: int | None = None,
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
        outcome = record_organic_correction_observation(
            group_id=group_id,
            observation_id=(
                observation_id
                or source_message_id
                or f"organic:{uuid.uuid4().hex}"
            ),
            scenario="使用者主動糾正",
            content=content,
            candidate_rule=summary_clean or correction,
            source_message_id=source_message_id,
            actor_key=actor_key,
            observed_at=observed_at,
        )
        return int(outcome["note_id"])
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


# ── Canonical organic correction projection ────────────────────────────────

_CORRECTION_MATCHER_VERSION = "local-v1"
_CORRECTION_DECISIONS = {"distinct", "equivalent", "ambiguous", "conflict"}


def _canonical_correction_enabled() -> bool:
    """Runtime rollback switch; env override is intentionally read per call."""
    raw = os.environ.get("CORRECTION_CANONICAL_MEMORY_ENABLED")
    if raw is None:
        return bool(
            getattr(settings, "correction_canonical_memory_enabled", True)
        )
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _correction_rule_rows_conn(c: sqlite3.Connection, group_id: str) -> list[dict]:
    rows = c.execute(
        "SELECT rule_id, canonical_rule, canonical_key, occurrence_count, "
        "first_seen_at, last_seen_at, matcher_version, semantic_context "
        "FROM correction_rules WHERE group_id=? AND status='active' "
        "ORDER BY last_seen_at DESC, rule_id DESC",
        (group_id,),
    ).fetchall()
    return [
        {
            "rule_id": int(r[0]),
            "canonical_rule": r[1],
            "canonical_key": r[2],
            "occurrence_count": int(r[3]),
            "first_seen_at": int(r[4]),
            "last_seen_at": int(r[5]),
            "matcher_version": r[6],
            "semantic_context": r[7] or "",
        }
        for r in rows
    ]


def _correction_rule_rows_for_prompt_conn(
    c: sqlite3.Connection,
    group_id: str,
) -> list[dict]:
    """Return a bounded union of recent and recurrent active rules."""
    rows = c.execute(
        "WITH selected(rule_id) AS ("
        " SELECT rule_id FROM ("
        "  SELECT rule_id FROM correction_rules "
        "  INDEXED BY idx_correction_rules_group_recent "
        "  WHERE group_id=? AND status='active' AND occurrence_count>0 "
        "  ORDER BY last_seen_at DESC, rule_id DESC LIMIT 10"
        " ) UNION "
        " SELECT rule_id FROM ("
        "  SELECT rule_id FROM correction_rules "
        "  INDEXED BY idx_correction_rules_group_recurrence "
        "  WHERE group_id=? AND status='active' AND occurrence_count>0 "
        "  ORDER BY occurrence_count DESC, last_seen_at DESC, rule_id DESC LIMIT 10"
        " )"
        ") "
        "SELECT r.rule_id, r.canonical_rule, r.canonical_key, "
        "r.occurrence_count, r.first_seen_at, r.last_seen_at, r.matcher_version, "
        "r.semantic_context "
        "FROM correction_rules r JOIN selected s ON s.rule_id=r.rule_id "
        "ORDER BY r.last_seen_at DESC, r.rule_id DESC",
        (group_id, group_id),
    ).fetchall()
    return [
        {
            "rule_id": int(r[0]),
            "canonical_rule": r[1],
            "canonical_key": r[2],
            "occurrence_count": int(r[3]),
            "first_seen_at": int(r[4]),
            "last_seen_at": int(r[5]),
            "matcher_version": r[6],
            "semantic_context": r[7] or "",
        }
        for r in rows
    ]


def _canonical_key(candidate: str, *, suffix: str = "") -> str:
    from correction_memory import normalize_rule

    normalized = normalize_rule(candidate)
    material = normalized if not suffix else f"{normalized}\0{suffix}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _insert_rule_conn(
    c: sqlite3.Connection,
    group_id: str,
    candidate: str,
    observed_at: int,
    *,
    key_suffix: str = "",
) -> int:
    from correction_memory import semantic_context

    key = _canonical_key(candidate, suffix=key_suffix)
    semantic = semantic_context(candidate) or "none"
    try:
        c.execute(
            "INSERT INTO correction_rules"
            "(group_id, canonical_key, canonical_rule, status, occurrence_count, "
            " first_seen_at, last_seen_at, matcher_version, semantic_context) "
            "VALUES (?, ?, ?, 'active', 0, ?, ?, ?, ?)",
            (
                group_id,
                key,
                candidate,
                observed_at,
                observed_at,
                _CORRECTION_MATCHER_VERSION,
                semantic,
            ),
        )
        return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    except sqlite3.IntegrityError:
        row = c.execute(
            "SELECT rule_id FROM correction_rules "
            "WHERE group_id=? AND canonical_key=?",
            (group_id, key),
        ).fetchone()
        if row is None:
            raise
        return int(row[0])


def _recompute_rule_conn(c: sqlite3.Connection, group_id: str, rule_id: int) -> None:
    row = c.execute(
        "SELECT COUNT(*), MIN(observed_at), MAX(observed_at) "
        "FROM correction_observations "
        "WHERE group_id=? AND canonical_rule_id=?",
        (group_id, rule_id),
    ).fetchone()
    count = int(row[0] or 0)
    if count == 0:
        c.execute(
            "UPDATE correction_rules SET occurrence_count=0, status='retired' "
            "WHERE group_id=? AND rule_id=?",
            (group_id, rule_id),
        )
        return
    from correction_memory import combine_semantic_contexts

    member_rows = c.execute(
        "SELECT candidate_rule FROM correction_observations "
        "WHERE group_id=? AND canonical_rule_id=? ORDER BY observed_at, observation_id",
        (group_id, rule_id),
    ).fetchall()
    semantic = combine_semantic_contexts(member[0] for member in member_rows) or "none"
    c.execute(
        "UPDATE correction_rules SET occurrence_count=?, first_seen_at=?, "
        "last_seen_at=?, status='active', semantic_context=? "
        "WHERE group_id=? AND rule_id=?",
        (count, int(row[1]), int(row[2]), semantic, group_id, rule_id),
    )


def _event_conn(
    c: sqlite3.Connection,
    group_id: str,
    action: str,
    *,
    rule_id: int | None = None,
    observation_id: str = "",
    payload: dict | None = None,
    reverts_event_id: int | None = None,
    created_at: int | None = None,
) -> int:
    c.execute(
        "INSERT INTO correction_rule_events"
        "(group_id, action, rule_id, observation_id, payload_json, "
        " reverts_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            group_id,
            action,
            rule_id,
            observation_id,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            reverts_event_id,
            int(created_at or _time.time()),
        ),
    )
    return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def _assign_correction_observation_conn(
    c: sqlite3.Connection,
    *,
    group_id: str,
    observation_id: str,
    note_id: int,
    scenario: str,
    content: str,
    candidate_rule: str,
    source_message_id: str,
    actor_key: str,
    observed_at: int,
    adjudicator=None,
) -> dict:
    from correction_memory import default_adjudicator, extract_candidate_rule

    candidate = extract_candidate_rule(content, candidate_rule)
    existing = _correction_rule_rows_conn(c, group_id)
    matcher = adjudicator or default_adjudicator
    try:
        raw_decision = matcher(existing, candidate)
    except Exception:
        raw_decision = {"decision": "ambiguous", "score": 0.0}
    decision = str((raw_decision or {}).get("decision", "ambiguous"))
    if decision not in _CORRECTION_DECISIONS:
        decision = "ambiguous"
    try:
        score = max(0.0, min(1.0, float((raw_decision or {}).get("score", 0.0))))
    except (TypeError, ValueError):
        score = 0.0
    match_reason = str((raw_decision or {}).get("reason", ""))[:240]

    rule_id: int | None = None
    if decision == "equivalent":
        try:
            proposed_id = int((raw_decision or {}).get("rule_id"))
        except (TypeError, ValueError):
            proposed_id = 0
        if proposed_id and any(r["rule_id"] == proposed_id for r in existing):
            rule_id = proposed_id
        else:
            decision = "ambiguous"
    elif decision == "distinct" and candidate:
        rule_id = _insert_rule_conn(c, group_id, candidate, observed_at)
        # An exact normalized key may already exist even when an injected
        # matcher said "distinct".  Treat the unique projection as equivalent.
        if any(r["rule_id"] == rule_id for r in existing):
            decision = "equivalent"
            score = max(score, 1.0)
    elif decision == "distinct":
        decision = "ambiguous"

    c.execute(
        "INSERT INTO correction_observations"
        "(group_id, observation_id, note_id, source_message_id, actor_key, "
        " scenario, content, candidate_rule, decision, canonical_rule_id, "
        " match_score, matcher_version, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            group_id,
            observation_id,
            note_id,
            source_message_id,
            actor_key,
            scenario,
            content,
            candidate,
            decision,
            rule_id,
            score,
            _CORRECTION_MATCHER_VERSION,
            observed_at,
        ),
    )
    c.execute(
        "UPDATE persona_notes SET correction_linked=1 "
        "WHERE note_id=? AND group_id=?",
        (note_id, group_id),
    )

    occurrence = 0
    status = decision
    if rule_id is not None:
        _recompute_rule_conn(c, group_id, rule_id)
        occurrence = int(
            c.execute(
                "SELECT occurrence_count FROM correction_rules "
                "WHERE group_id=? AND rule_id=?",
                (group_id, rule_id),
            ).fetchone()[0]
        )
        status = "new" if occurrence == 1 else "recurrent"
    _event_conn(
        c,
        group_id,
        "create" if status == "new" else ("merge" if status == "recurrent" else decision),
        rule_id=rule_id,
        observation_id=observation_id,
        payload={"decision": decision, "score": score, "reason": match_reason},
        created_at=observed_at,
    )
    return {
        "note_id": note_id,
        "observation_id": observation_id,
        "rule_id": rule_id,
        "status": status,
        "occurrence_count": occurrence,
        "is_recurrence": occurrence > 1,
    }


def record_organic_correction_observation(
    group_id: str,
    observation_id: str,
    scenario: str,
    content: str,
    candidate_rule: str = "",
    source_message_id: str = "",
    actor_key: str = "",
    observed_at: int | None = None,
    adjudicator=None,
) -> dict:
    """Append one raw audit and update its group-local canonical projection.

    ``observation_id`` is the transport idempotency key.  Matcher failure is
    fail-closed: the raw audit is still stored with an ``ambiguous`` decision.
    """
    group = (group_id or "").strip()
    observation = (observation_id or "").strip()
    if not group or not observation or len(observation) > 240:
        raise ValueError("group_id and bounded observation_id are required")
    scenario_clean = (scenario or "使用者主動糾正").strip()[:120]
    content_clean = (content or "").strip()[:1600]
    when = int(observed_at or _time.time())

    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            duplicate = c.execute(
                "SELECT note_id, canonical_rule_id, decision FROM "
                "correction_observations WHERE group_id=? AND observation_id=?",
                (group, observation),
            ).fetchone()
            if duplicate is not None:
                occurrence = 0
                if duplicate[1] is not None:
                    row = c.execute(
                        "SELECT occurrence_count FROM correction_rules "
                        "WHERE group_id=? AND rule_id=?",
                        (group, int(duplicate[1])),
                    ).fetchone()
                    occurrence = int(row[0]) if row else 0
                c.execute("COMMIT")
                return {
                    "note_id": int(duplicate[0]),
                    "observation_id": observation,
                    "rule_id": int(duplicate[1]) if duplicate[1] is not None else None,
                    "status": "duplicate",
                    "occurrence_count": occurrence,
                    "is_recurrence": occurrence > 1,
                }

            c.execute(
                "INSERT INTO persona_notes"
                "(group_id, kind, scenario, content, created_at, source) "
                "VALUES (?, 'correction', ?, ?, ?, 'organic')",
                (group, scenario_clean, content_clean, when),
            )
            note_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
            outcome = _assign_correction_observation_conn(
                c,
                group_id=group,
                observation_id=observation,
                note_id=note_id,
                scenario=scenario_clean,
                content=content_clean,
                candidate_rule=candidate_rule,
                source_message_id=(source_message_id or observation)[:240],
                actor_key=(actor_key or "")[:128],
                observed_at=when,
                adjudicator=adjudicator,
            )
            c.execute("COMMIT")
            return outcome
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


def list_organic_correction_audits(group_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT observation_id, note_id, source_message_id, actor_key, "
            "scenario, content, candidate_rule, decision, canonical_rule_id, "
            "match_score, matcher_version, observed_at "
            "FROM correction_observations WHERE group_id=? "
            "ORDER BY observed_at, observation_id",
            (group_id,),
        ).fetchall()
    return [
        {
            "observation_id": r[0],
            "note_id": int(r[1]),
            "source_message_id": r[2],
            "actor_key": r[3],
            "scenario": r[4],
            "content": r[5],
            "candidate_rule": r[6],
            "decision": r[7],
            "canonical_rule_id": int(r[8]) if r[8] is not None else None,
            "match_score": float(r[9]),
            "matcher_version": r[10],
            "observed_at": int(r[11]),
        }
        for r in rows
    ]


def list_canonical_organic_corrections(group_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT rule_id, canonical_rule, occurrence_count, first_seen_at, "
            "last_seen_at, matcher_version FROM correction_rules "
            "WHERE group_id=? AND status='active' AND occurrence_count>0 "
            "ORDER BY last_seen_at DESC, occurrence_count DESC, rule_id DESC",
            (group_id,),
        ).fetchall()
    return [
        {
            "rule_id": int(r[0]),
            "canonical_rule": r[1],
            "occurrence_count": int(r[2]),
            "recurrence_count": max(int(r[2]) - 1, 0),
            "is_recurrence": int(r[2]) > 1,
            "first_seen_at": int(r[3]),
            "last_seen_at": int(r[4]),
            "matcher_version": r[5],
        }
        for r in rows
    ]


def list_persona_notes_for_prompt(group_id: str) -> list[dict]:
    """Return legacy notes or a deduplicated canonical organic projection."""
    if not _canonical_correction_enabled():
        return list_persona_notes(group_id)

    # Raw notes, canonical rules, and observation links must come from one WAL
    # read snapshot.  Separate connections can straddle a backfill commit and
    # briefly omit a correction from both the legacy and canonical projections.
    with _conn() as c:
        c.execute("BEGIN")
        try:
            note_rows = []
            for kind, source, linked in (
                ("example", None, None),
                ("correction", "rule_violation", None),
                ("correction", "organic", 0),
            ):
                if source is None:
                    rows = c.execute(
                        "SELECT note_id, kind, scenario, content, created_at, source "
                        "FROM persona_notes WHERE group_id=? AND kind=? "
                        "ORDER BY created_at ASC, note_id ASC LIMIT ?",
                        (group_id, kind, _PERSONA_NOTE_CAP),
                    ).fetchall()
                elif linked is None:
                    rows = c.execute(
                        "SELECT note_id, kind, scenario, content, created_at, source "
                        "FROM persona_notes WHERE group_id=? AND kind=? "
                        "AND source<>'organic' "
                        "ORDER BY created_at ASC, note_id ASC LIMIT ?",
                        (group_id, kind, _PERSONA_NOTE_CAP),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT note_id, kind, scenario, content, created_at, source "
                        "FROM persona_notes WHERE group_id=? AND kind=? "
                        "AND source='organic' AND correction_linked=0 "
                        "ORDER BY created_at ASC, note_id ASC LIMIT ?",
                        (group_id, kind, _PERSONA_NOTE_CAP),
                    ).fetchall()
                note_rows.extend(rows)
            note_rows.sort(key=lambda row: (int(row[4]), int(row[0])))
            rule_rows = _correction_rule_rows_for_prompt_conn(c, group_id)
            c.execute("COMMIT")
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
    all_notes = [
        {
            "note_id": int(row[0]),
            "kind": row[1],
            "scenario": row[2],
            "content": row[3],
            "created_at": int(row[4]),
            "source": row[5] or "rule_violation",
        }
        for row in note_rows
    ]
    examples = [n for n in all_notes if n["kind"] == "example"]
    nonorganic = [
        n for n in all_notes
        if n["kind"] == "correction" and n.get("source") != "organic"
    ]
    canonical = [
        {
            "note_id": rule["rule_id"],
            "kind": "correction",
            "scenario": "使用者糾正（canonical）",
            "content": rule["canonical_rule"],
            "created_at": rule["last_seen_at"],
            "last_seen_at": rule["last_seen_at"],
            "source": "organic",
            "canonical_rule_id": rule["rule_id"],
            "occurrence_count": rule["occurrence_count"],
            "recurrence_count": rule["recurrence_count"],
            "is_recurrence": rule["is_recurrence"],
        }
        for rule in (
            {
                **row,
                "recurrence_count": max(int(row["occurrence_count"]) - 1, 0),
                "is_recurrence": int(row["occurrence_count"]) > 1,
            }
            for row in rule_rows
            if int(row["occurrence_count"]) > 0
        )
    ]
    # The snapshot query only returned still-unlinked organic notes, so rollout
    # loses no legacy rule without materializing the entire immutable audit log.
    legacy_organic = [
        n for n in all_notes
        if n["kind"] == "correction"
        and n.get("source") == "organic"
    ]
    return examples + canonical + legacy_organic + nonorganic


def backfill_organic_corrections(
    group_id: str | None = None,
    *,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict:
    """Project retained legacy organic notes without mutating their raw rows."""
    sql = (
        "SELECT p.note_id, p.group_id, p.scenario, p.content, p.created_at "
        "FROM persona_notes p LEFT JOIN correction_observations o "
        "ON o.note_id=p.note_id WHERE p.kind='correction' AND p.source='organic' "
        "AND o.note_id IS NULL"
    )
    params: list[object] = []
    if group_id:
        sql += " AND p.group_id=?"
        params.append(group_id)
    sql += " ORDER BY p.created_at, p.note_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    with _conn() as c:
        pending = c.execute(sql, tuple(params)).fetchall()
    if dry_run:
        return {"eligible": len(pending), "linked": 0, "unresolved": 0}

    linked = 0
    unresolved = 0
    for note_id, gid, scenario, content, created_at in pending:
        observation_id = f"legacy:{int(note_id)}"
        with _lock, _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                current = c.execute(
                    "SELECT p.scenario, p.content, p.created_at "
                    "FROM persona_notes p LEFT JOIN correction_observations o "
                    "ON o.note_id=p.note_id WHERE p.note_id=? AND p.group_id=? "
                    "AND p.kind='correction' AND p.source='organic' "
                    "AND o.note_id IS NULL",
                    (int(note_id), gid),
                ).fetchone()
                if current is None:
                    c.execute("COMMIT")
                    continue
                scenario, content, created_at = current
                outcome = _assign_correction_observation_conn(
                    c,
                    group_id=gid,
                    observation_id=observation_id,
                    note_id=int(note_id),
                    scenario=scenario,
                    content=content,
                    candidate_rule="",
                    source_message_id="",
                    actor_key="",
                    observed_at=int(created_at),
                )
                c.execute("COMMIT")
            except Exception:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        linked += 1
        if outcome["rule_id"] is None:
            unresolved += 1
    return {"eligible": len(pending), "linked": linked, "unresolved": unresolved}


def split_correction_rule(
    group_id: str,
    rule_id: int,
    observation_ids: list[str],
) -> int:
    """Move selected observations into a new canonical; raw audits stay intact."""
    selected = list(dict.fromkeys(x.strip() for x in observation_ids if x.strip()))
    if not selected:
        raise ValueError("at least one observation_id is required")
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            source = c.execute(
                "SELECT canonical_rule FROM correction_rules "
                "WHERE group_id=? AND rule_id=? AND status='active'",
                (group_id, int(rule_id)),
            ).fetchone()
            if source is None:
                raise ValueError("active source rule not found in group")
            marks = ",".join("?" for _ in selected)
            rows = c.execute(
                "SELECT observation_id, candidate_rule, observed_at "
                "FROM correction_observations WHERE group_id=? "
                "AND canonical_rule_id=? AND observation_id IN (" + marks + ")",
                (group_id, int(rule_id), *selected),
            ).fetchall()
            if len(rows) != len(selected):
                raise ValueError("one or more observations do not belong to source rule")
            rows_by_id = {r[0]: r for r in rows}
            first = min(rows, key=lambda r: (int(r[2]), r[0]))
            new_text = first[1] or source[0]
            split_token = uuid.uuid4().hex
            new_rule_id = _insert_rule_conn(
                c,
                group_id,
                new_text,
                int(first[2]),
                key_suffix=f"split:{split_token}",
            )
            c.execute(
                "UPDATE correction_observations SET canonical_rule_id=? "
                "WHERE group_id=? "
                "AND observation_id IN (" + marks + ")",
                (new_rule_id, group_id, *selected),
            )
            _recompute_rule_conn(c, group_id, int(rule_id))
            _recompute_rule_conn(c, group_id, new_rule_id)
            event_id = _event_conn(
                c,
                group_id,
                "split",
                rule_id=int(rule_id),
                payload={
                    "source_rule_id": int(rule_id),
                    "new_rule_id": new_rule_id,
                    "observation_ids": selected,
                    "previous": {
                        obs_id: {
                            "canonical_rule_id": int(rule_id),
                            "decision": "unchanged",
                        }
                        for obs_id in rows_by_id
                    },
                },
            )
            c.execute("COMMIT")
            return event_id
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


def undo_correction_rule_event(group_id: str, event_id: int) -> bool:
    """Undo one split exactly once and append an undo event."""
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT action, payload_json FROM correction_rule_events "
                "WHERE group_id=? AND event_id=?",
                (group_id, int(event_id)),
            ).fetchone()
            if row is None or row[0] != "split":
                c.execute("ROLLBACK")
                return False
            if c.execute(
                "SELECT 1 FROM correction_rule_events "
                "WHERE group_id=? AND reverts_event_id=?",
                (group_id, int(event_id)),
            ).fetchone():
                c.execute("ROLLBACK")
                return False
            payload = json.loads(row[1])
            source_rule_id = int(payload["source_rule_id"])
            new_rule_id = int(payload["new_rule_id"])
            observation_ids = list(payload["observation_ids"])
            marks = ",".join("?" for _ in observation_ids)
            moved = c.execute(
                "UPDATE correction_observations SET canonical_rule_id=? "
                "WHERE group_id=? "
                "AND canonical_rule_id=? AND observation_id IN (" + marks + ")",
                (source_rule_id, group_id, new_rule_id, *observation_ids),
            )
            if moved.rowcount != len(observation_ids):
                raise RuntimeError("split state drift; undo refused")
            _recompute_rule_conn(c, group_id, source_rule_id)
            _recompute_rule_conn(c, group_id, new_rule_id)
            _event_conn(
                c,
                group_id,
                "undo_split",
                rule_id=source_rule_id,
                payload={"restored_observation_ids": observation_ids},
                reverts_event_id=int(event_id),
            )
            c.execute("COMMIT")
            return True
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


# ── Reminders（自動偵測時間性事項，2026-05-08 加）────────────────────────────


_MERGED_DETAIL_LIMIT = 20
_MERGED_DETAIL_TEXT_LIMIT = 300
# Same-day stages: once one of these went out, the family has been told the
# time for today, so a later mention must not move it (day-level stages stay
# valid when the time moves within the same day).
_SAME_DAY_PUSH_COLUMNS: tuple[str, ...] = (
    "pushed_4hr",
    "pushed_2hr",
    "pushed_1hr",
    "pushed_now",
)


def _reminder_hhmm(remind_at: int) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(int(remind_at), ZoneInfo("Asia/Taipei")).strftime(
        "%H:%M"
    )


def _load_merged_details(raw: object) -> list[dict]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and item.get("key")]


def _merged_detail_fragment(action: str, source_text: str) -> dict:
    """One absorbed mention; the key makes a replayed message a no-op."""
    action = _normalize_reminder_text(action)
    text = _normalize_reminder_text(source_text)[:_MERGED_DETAIL_TEXT_LIMIT]
    digest = hashlib.sha1(
        f"{_reminder_equivalence_key(action)}\n{_reminder_equivalence_key(text)}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return {"key": digest, "action": action, "text": text}


def merged_detail_key(action: str, source_text: str) -> str:
    """The merged_details key a row's own words get when another row absorbs it."""
    return _merged_detail_fragment(action, source_text)["key"]


def _store_merged_detail_conn(
    c: sqlite3.Connection,
    reminder_id: int,
    raw_details: object,
    kept_action: str,
    kept_source: str,
    fragment: dict,
) -> bool:
    """Record an absorbed mention; False when it adds nothing or the list is full."""
    if fragment["key"] == _merged_detail_fragment(kept_action, kept_source)["key"]:
        return False
    fragments = _load_merged_details(raw_details)
    if len(fragments) >= _MERGED_DETAIL_LIMIT or any(
        item["key"] == fragment["key"] for item in fragments
    ):
        return False
    fragments.append(fragment)
    c.execute(
        "UPDATE reminders SET merged_details=? WHERE reminder_id=? AND status='pending'",
        (json.dumps(fragments, ensure_ascii=False), int(reminder_id)),
    )
    return True


def _delivery_in_flight_conn(
    c: sqlite3.Connection,
    group_id: str,
    reminder_id: int,
    action: str,
    remind_at: int,
) -> bool:
    return (
        _semantic_delivery_claim_conn(
            c,
            group_id=group_id,
            reminder_id=int(reminder_id),
            action=action,
            remind_at=int(remind_at),
        )
        is not None
    )


def _promote_time_kind_conn(
    c: sqlite3.Connection,
    reminder_id: int,
    stored_kind: str | None,
    stored_at: int,
    incoming_kind: str | None,
    incoming_hhmm: str,
) -> None:
    """Raise a kept reminder's time kind when a more specific mention confirms its time.

    Only upward (none < daypart < clock). Unknown kinds never promote, and a
    legacy row (NULL) is already fixed. Allowed during a delivery claim: the
    claim does not depend on this column.
    """
    if incoming_kind is None or stored_kind is None:
        return
    if reminder_intent.time_rank(incoming_kind) <= reminder_intent.time_rank(stored_kind):
        return
    if reminder_intent.time_is_confirmed_by(
        _reminder_hhmm(stored_at), incoming_kind, incoming_hhmm
    ):
        c.execute(
            "UPDATE reminders SET time_kind=? WHERE reminder_id=? AND status='pending'",
            (incoming_kind, int(reminder_id)),
        )


def _mention_sets_a_new_time(
    stored_kind: str | None,
    stored_at: int,
    incoming_kind: str | None,
    incoming_hhmm: str,
) -> bool:
    """A more specific mention that does not just confirm the stored time."""
    return (
        incoming_kind is not None
        and stored_kind is not None
        and reminder_intent.time_rank(incoming_kind) > reminder_intent.time_rank(stored_kind)
        and not reminder_intent.time_is_confirmed_by(
            _reminder_hhmm(stored_at), incoming_kind, incoming_hhmm
        )
    )


def _same_event_calendar_mirror_conn(
    c: sqlite3.Connection,
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    mentions: list[str],
    time_kind: str,
    now: int,
    day_start: int,
) -> int | None:
    """A pending same-day calendar mirror describing this event, read only."""
    rows = c.execute(
        "SELECT reminder_id, action, COALESCE(source_text, ''), "
        "COALESCE(mention_aliases, '[]'), remind_at, time_kind, "
        "COALESCE(merged_details, '[]'), COALESCE(user_id, '') "
        "FROM reminders WHERE group_id=? AND status='pending' "
        "AND source_kind='calendar_event' AND source_ref<>'' "
        "AND remind_at>=? AND remind_at<? AND remind_at>? ORDER BY reminder_id",
        (group_id, int(day_start), int(day_start) + 86400, int(now)),
    ).fetchall()
    incoming_hhmm = _reminder_hhmm(remind_at)
    for row in rows:
        if reminder_intent.has_reminder_offset_marker(row[1]):
            continue
        identity = [row[1], *(item.get("action") or "" for item in _load_merged_details(row[6]))]
        if reminder_intent.mention_matches_reminder(
            action,
            row[1],
            time_kind=time_kind,
            hhmm=incoming_hhmm,
            kept_kind=row[5],
            kept_hhmm=_reminder_hhmm(row[4]),
            kept_identity=identity,
            kept_source=row[2],
            mentions=mentions,
            kept_mentions=_load_mention_aliases(row[3]),
            same_author=bool(user_id) and row[7] == user_id,
        ):
            return int(row[0])
    return None


def _merge_same_event_conn(
    c: sqlite3.Connection,
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_text: str,
    mentions: list[str],
    time_kind: str,
    now: int,
    fragment: dict,
) -> tuple[int, str] | None:
    """Fold a repeated mention of one event into its pending reminder.

    Honor-or-insert: returns (reminder_id, outcome) only when the kept reminder
    will still remind at the time the new mention asks for; None means insert
    a new reminder as before.

    The kept reminder's wording (``action``) never changes here, even when the
    mention says more (fixR5a, GP1 r4 #1 #2): the calendar pairing reads the
    event title against it and quoted cancel / reschedule match the messages
    already sent by it.  The mention goes into merged_details, and the push
    and the receipt show a fuller wording on a 「細節：…」 line
    (reminder_push.fuller_detail_line).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = datetime.fromtimestamp(int(remind_at), ZoneInfo("Asia/Taipei"))
    start = int(
        datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    )
    rows = c.execute(
        "SELECT reminder_id, action, COALESCE(source_text, ''), "
        "COALESCE(mention_aliases, '[]'), remind_at, time_kind, "
        "COALESCE(merged_details, '[]'), pushed_4hr, pushed_2hr, pushed_1hr, pushed_now, "
        "COALESCE(user_id, '') "
        "FROM reminders WHERE group_id=? AND status='pending' "
        # 2026-10-04: a trip day created from a dated list (schedule_line) is
        # the same kind of user reminder; later mentions join it.
        "AND source_kind IN ('', 'contextual_create_once', 'schedule_line') "
        "AND remind_at>=? AND remind_at<? AND remind_at>? ORDER BY reminder_id",
        (group_id, start, start + 86400, int(now)),
    ).fetchall()
    incoming_hhmm = _reminder_hhmm(remind_at)
    candidates = []
    for row in rows:
        if reminder_intent.has_reminder_offset_marker(row[1]):
            continue
        kept_mentions = _load_mention_aliases(row[3])
        names = tuple(dict.fromkeys([*mentions, *kept_mentions]))
        identity = [row[1], *(item.get("action") or "" for item in _load_merged_details(row[6]))]
        stored_hhmm = _reminder_hhmm(row[4])
        same_author = bool(user_id) and row[11] == user_id
        # 2026-10-04: the same test the push-time fold uses, now with the
        # narrow same-author, same-clock loosening (e1/e3).  Only this way
        # round: a row that reads into the mention but not the other way
        # (fixC12's reverse reading) gets the mention as a row of its own,
        # because merged here it kept its older words (fixR4b, GP1 r3 #2).
        # The fold pairs the two at the next moment and keeps the row with
        # the most content; the receipt's own reply leaves both alone
        # (reminder_push keep_ids / same_event_ids).
        if not reminder_intent.mention_matches_reminder(
            action,
            row[1],
            time_kind=time_kind,
            hhmm=incoming_hhmm,
            kept_kind=row[5],
            kept_hhmm=stored_hhmm,
            kept_identity=identity,
            kept_source=row[2],
            mentions=mentions,
            kept_mentions=kept_mentions,
            same_author=same_author,
        ):
            continue
        candidates.append((row, names, identity, stored_hhmm, same_author))
    if len(candidates) > 1:
        # A refused move kept the vague row and added the new time; a later
        # mention belongs to the one row that already reminds at its time.
        keeping = [
            candidate
            for candidate in candidates
            if not _mention_sets_a_new_time(
                candidate[0][5], int(candidate[0][4]), time_kind, incoming_hhmm
            )
        ]
        if len(keeping) == 1:
            candidates = keeping
    if not candidates:
        # 2026-10-04 (P4 item 3, GP1 I11): only when no natural reminder
        # matched, a separate pass checks the same day's calendar mirrors.  A
        # match means the calendar already reminds about this event: report a
        # duplicate, insert nothing and leave the mirror as it is.
        mirror_id = _same_event_calendar_mirror_conn(
            c, group_id, user_id, action, remind_at, mentions, time_kind, now, start
        )
        if mirror_id is not None:
            return mirror_id, "duplicate"
        return None
    if len(candidates) != 1:
        return None

    row, names, identity, stored_hhmm, same_author = candidates[0]
    reminder_id = int(row[0])
    in_flight = _delivery_in_flight_conn(c, group_id, reminder_id, row[1], int(row[4]))
    merged_mentions = _merge_mention_aliases_json(row[3], mentions)
    wants_move = reminder_intent.time_rank(time_kind) > reminder_intent.time_rank(
        row[5]
    ) and not reminder_intent.time_is_confirmed_by(stored_hhmm, time_kind, incoming_hhmm)
    if wants_move:
        movable = (
            row[5] is not None
            and int(remind_at) > int(now)
            and not any(int(flag or 0) for flag in row[7:11])
            and not in_flight
            and reminder_intent.same_event_move_match(
                action, row[1], identity, names, same_author=same_author
            )
        )
        if not movable:
            return None
        _store_merged_detail_conn(c, reminder_id, row[6], row[1], row[2], fragment)
        c.execute(
            "UPDATE reminders SET remind_at=?, time_kind=?, mention_aliases=? "
            "WHERE reminder_id=? AND status='pending'",
            (
                int(remind_at),
                time_kind,
                merged_mentions,
                reminder_id,
            ),
        )
        return reminder_id, "merged"

    _promote_time_kind_conn(c, reminder_id, row[5], int(row[4]), time_kind, incoming_hhmm)
    if in_flight:
        return reminder_id, "duplicate"
    changed = _store_merged_detail_conn(c, reminder_id, row[6], row[1], row[2], fragment)
    if merged_mentions != row[3]:
        c.execute(
            "UPDATE reminders SET mention_aliases=? WHERE reminder_id=?",
            (merged_mentions, reminder_id),
        )
        changed = True
    return reminder_id, "merged" if changed else "duplicate"


def _add_reminder_with_outcome_conn(
    c: sqlite3.Connection,
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_text: str,
    mentions: list[str],
    now: int,
    time_kind: str | None = None,
) -> tuple[int, str]:
    mentions_json = json.dumps(mentions, ensure_ascii=False)
    incoming_hhmm = _reminder_hhmm(remind_at)
    fragment = _merged_detail_fragment(action, source_text)
    retired = c.execute(
        "SELECT reminder_id FROM reminders WHERE group_id=? AND user_id=? "
        "AND source_kind='restated_generic_old' AND status='cancelled' "
        "AND source_text=? AND source_text<>'' LIMIT 1",
        (group_id, user_id, source_text),
    ).fetchone()
    if retired:
        return int(retired[0]), "inactive"
    exact = c.execute(
        "SELECT reminder_id, COALESCE(mention_aliases, '[]'), remind_at, time_kind, "
        "COALESCE(merged_details, '[]'), COALESCE(source_text, '') FROM reminders "
        "WHERE group_id = ? AND action = ? AND status = 'pending' "
        "AND ABS(remind_at - ?) < 3600",
        (group_id, action, remind_at),
    ).fetchall()
    existing = exact[0] if exact else None
    if existing and _mention_sets_a_new_time(existing[3], int(existing[2]), time_kind, incoming_hhmm):
        # 「明天回診」then「明天11點15分回診」: let the same-event step move it
        # under its guards instead of calling it a duplicate at the default time,
        # unless a row already keeps this time (an earlier refused move inserted it).
        existing = next(
            (
                row
                for row in exact[1:]
                if not _mention_sets_a_new_time(row[3], int(row[2]), time_kind, incoming_hhmm)
                and reminder_intent.times_compatible(
                    row[3], _reminder_hhmm(row[2]), time_kind, incoming_hhmm
                )
            ),
            None,
        )
    if existing:
        merged_mentions = _merge_mention_aliases_json(existing[1], mentions)
        if merged_mentions != existing[1]:
            c.execute(
                "UPDATE reminders SET mention_aliases = ? WHERE reminder_id = ?",
                (merged_mentions, existing[0]),
            )
        _promote_time_kind_conn(
            c, int(existing[0]), existing[3], int(existing[2]), time_kind, incoming_hhmm
        )
        if reminder_intent.times_compatible(
            existing[3], _reminder_hhmm(existing[2]), time_kind, incoming_hhmm
        ) and not _delivery_in_flight_conn(
            c, group_id, int(existing[0]), action, int(existing[2])
        ):
            _store_merged_detail_conn(
                c, int(existing[0]), existing[4], action, existing[5], fragment
            )
        return int(existing[0]), "duplicate"
    weak_nearby = c.execute(
        "SELECT reminder_id, action, COALESCE(source_text, ''), "
        "COALESCE(mention_aliases, '[]'), COALESCE(source_kind, ''), "
        "COALESCE(source_ref, ''), remind_at, time_kind, "
        "COALESCE(merged_details, '[]') "
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
        _promote_time_kind_conn(
            c, int(strong[0]), strong[7], int(strong[6]), time_kind, incoming_hhmm
        )
        if not _delivery_in_flight_conn(c, group_id, int(strong[0]), strong[1], int(strong[6])):
            _store_merged_detail_conn(c, int(strong[0]), strong[8], strong[1], strong[2], fragment)
        return int(strong[0]), "duplicate"
    if not incoming_is_weak and len(weak_rows) == 1 and not strong_rows:
        weak = weak_rows[0]
        _promote_time_kind_conn(
            c, int(weak[0]), weak[7], int(weak[6]), time_kind, incoming_hhmm
        )
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
        # The weak row's own words are replaced; keep them as an absorbed detail.
        _store_merged_detail_conn(
            c,
            int(weak[0]),
            weak[8],
            action,
            source_text,
            _merged_detail_fragment(weak[1], weak[2]),
        )
        return int(weak[0]), "merged"
    nearby = c.execute(
        "SELECT reminder_id, action, COALESCE(source_text, ''), "
        "COALESCE(mention_aliases, '[]'), remind_at, time_kind, "
        "pushed_4hr, pushed_2hr, pushed_1hr, pushed_now "
        "FROM reminders WHERE group_id = ? AND status = 'pending' "
        "AND ABS(remind_at - ?) < 1800",
        (group_id, remind_at),
    ).fetchall()
    for (
        existing_id,
        existing_action,
        existing_source,
        existing_mentions,
        existing_at,
        existing_kind,
        *same_day_flags,
    ) in nearby:
        merged_action = _merge_reminder_action(existing_action, action)
        if not merged_action:
            continue
        takes_new_time = _mention_sets_a_new_time(
            existing_kind, int(existing_at), time_kind, incoming_hhmm
        )
        if takes_new_time and (
            int(remind_at) <= int(now) or any(int(flag or 0) for flag in same_day_flags)
        ):
            # a clock for a 嗎哪 reminder already announced today (or a time
            # already past): leave it as it is and add the new time
            continue
        _promote_time_kind_conn(
            c, int(existing_id), existing_kind, int(existing_at), time_kind, incoming_hhmm
        )
        if _delivery_in_flight_conn(
            c, group_id, int(existing_id), existing_action, int(existing_at)
        ):
            continue
        merged_source = _merge_reminder_source(existing_source, source_text)
        merged_mentions = _merge_mention_aliases_json(existing_mentions, mentions)
        if takes_new_time:
            # 「媽媽嗎哪小組查經」 then 「嗎哪小組查經 19:20」: one group, the stated time
            c.execute(
                "UPDATE reminders SET action = ?, source_text = ?, mention_aliases = ?, "
                "remind_at = ?, time_kind = ? WHERE reminder_id = ?",
                (
                    merged_action,
                    merged_source,
                    merged_mentions,
                    int(remind_at),
                    time_kind,
                    existing_id,
                ),
            )
            return int(existing_id), "merged"
        c.execute(
            "UPDATE reminders SET action = ?, source_text = ?, mention_aliases = ? "
            "WHERE reminder_id = ?",
            (merged_action, merged_source, merged_mentions, existing_id),
        )
        return int(existing_id), "merged"
    if time_kind is not None:
        same_event = _merge_same_event_conn(
            c,
            group_id,
            user_id,
            action,
            remind_at,
            source_text,
            mentions,
            time_kind,
            now,
            fragment,
        )
        if same_event is not None:
            return same_event
    c.execute(
        "INSERT INTO reminders(group_id, user_id, action, remind_at, "
        "created_at, status, source_kind, source_ref, source_text, mention_aliases, "
        "time_kind) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
        (
            group_id, user_id, action, remind_at, now,
            "", "", source_text, mentions_json, time_kind
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
    time_kind: str | None = None,
    *,
    source_kind: str = "",
    source_ref: str = "",
) -> tuple[int, str]:
    """Atomically add or merge a reminder and return its write outcome.

    Outcome is one of ``created``, ``duplicate``, ``merged`` or ``inactive``.
    The explicit immediate transaction serializes the dedupe read/write across
    processes.  With a source identity (``source_kind`` + ``source_ref``, e.g.
    one line of a schedule message) the same source is written at most once:
    a pending row answers ``duplicate`` and a cancelled/finished one
    ``inactive``, so a resend or a later quote never revives it.
    """
    now = int(_time.time())
    action = _normalize_reminder_text(action)
    source_text = _normalize_reminder_text(source_text)
    mentions = _normalize_mention_aliases(mention_aliases)
    source_kind = str(source_kind or "").strip()
    source_ref = str(source_ref or "").strip()
    keyed = bool(source_kind and source_ref)
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if keyed:
            known = c.execute(
                "SELECT reminder_id, status FROM reminders "
                "WHERE group_id=? AND source_kind=? AND source_ref=? "
                "ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, reminder_id "
                "LIMIT 1",
                (group_id, source_kind, source_ref),
            ).fetchone()
            if known is not None:
                return int(known[0]), (
                    "duplicate" if known[1] == "pending" else "inactive"
                )
        reminder_id, outcome = _add_reminder_with_outcome_conn(
            c,
            group_id,
            user_id,
            action,
            remind_at,
            source_text,
            mentions,
            now,
            time_kind,
        )
        if keyed and outcome == "created":
            c.execute(
                "UPDATE reminders SET source_kind=?, source_ref=? WHERE reminder_id=?",
                (source_kind, source_ref, reminder_id),
            )
        return reminder_id, outcome


def get_contextual_legacy_reminder(
    group_id: str,
    user_id: str,
    source_text: str,
) -> dict | None:
    """Return one exact source-less pending row eligible for batch CAS."""

    if not group_id or not user_id or not source_text:
        return None
    with _conn() as c:
        rows = c.execute(
            "SELECT reminder_id,group_id,user_id,action,remind_at,status,"
            "source_kind,source_ref,source_text,mention_aliases,"
            "last_pushed_at,weekly_count,last_weekly_at,pushed_3d,pushed_1d,"
            "pushed_4hr,pushed_2hr,pushed_1hr,pushed_now "
            "FROM reminders WHERE group_id=? AND user_id=? AND status='pending' "
            "AND source_kind='' AND source_ref='' AND source_text=? "
            "ORDER BY reminder_id LIMIT 2",
            (group_id, user_id, _normalize_reminder_text(source_text)),
        ).fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    keys = (
        "reminder_id", "group_id", "user_id", "action", "remind_at", "status",
        "source_kind", "source_ref", "source_text", "mention_aliases",
        "last_pushed_at", "weekly_count", "last_weekly_at", "pushed_3d",
        "pushed_1d", "pushed_4hr", "pushed_2hr", "pushed_1hr", "pushed_now",
    )
    result = dict(zip(keys, row))
    result["reminder_id"] = int(result["reminder_id"])
    result["remind_at"] = int(result["remind_at"])
    result["mention_aliases"] = _load_mention_aliases(result["mention_aliases"])
    for key in _REMINDER_PUSH_FLAG_COLUMNS:
        result[key] = int(result[key] or 0)
    return result


def complete_contextual_date_reminder_batch(
    *,
    group_id: str,
    user_id: str,
    plan: dict,
    pending_id: int | None = None,
    pending_claim_token: str | None = None,
    legacy_reminder_id: int | None = None,
) -> dict:
    """Atomically reconcile one four-slot contextual exact-date batch.

    This deliberately bypasses ordinary reminder near-time merging.  Stable
    source identities are the idempotency key; a partial or drifted batch is a
    conflict rather than something to guess through.
    """

    source_message_id = str(plan.get("source_message_id") or "")
    command_message_id = str(plan.get("command_message_id") or "")
    source_text = _normalize_reminder_text(plan.get("source_text"))
    command_text = _normalize_reminder_text(plan.get("command_text"))
    specs = list(plan.get("reminders") or [])
    if (
        not group_id
        or not user_id
        or not source_message_id
        or not command_message_id
        or not source_text
        or not command_text
        or len(specs) != 4
    ):
        raise ValueError("invalid contextual reminder batch")
    normalized_specs: list[dict] = []
    source_refs: set[str] = set()
    for spec in specs:
        action = _normalize_reminder_text(spec.get("action"))
        source_kind = str(spec.get("source_kind") or "").strip()
        source_ref = str(spec.get("source_ref") or "").strip()
        remind_at = int(spec.get("remind_at") or 0)
        mentions = _normalize_mention_aliases(spec.get("mention_aliases"))
        if (
            not action
            or remind_at <= 0
            or source_kind != "contextual_date_once"
            or not source_ref.startswith(f"{command_message_id}:")
            or source_ref in source_refs
        ):
            raise ValueError("invalid contextual reminder slot")
        source_refs.add(source_ref)
        normalized_specs.append(
            {
                "action": action,
                "remind_at": remind_at,
                "source_kind": source_kind,
                "source_ref": source_ref,
                "mention_aliases": mentions,
            }
        )
    expected_refs = {
        f"{command_message_id}:{slot}"
        for slot in ("lead:0", "same:0", "lead:1", "same:1")
    }
    if source_refs != expected_refs:
        raise ValueError("invalid contextual reminder slot set")
    if pending_id is not None and not pending_claim_token:
        raise ValueError("pending claim token is required")

    expected_legacy = plan.get("legacy_expected")
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        source_raw = c.execute(
            "SELECT user_id,text FROM raw_messages WHERE group_id=? AND message_id=?",
            (group_id, source_message_id),
        ).fetchone()
        command_raw = c.execute(
            "SELECT user_id,text FROM raw_messages WHERE group_id=? AND message_id=?",
            (group_id, command_message_id),
        ).fetchone()
        if (
            source_raw is None
            or command_raw is None
            or str(source_raw[0] or "") != user_id
            or str(command_raw[0] or "") != user_id
            or _normalize_reminder_text(source_raw[1]) != source_text
            or _normalize_reminder_text(command_raw[1]) != command_text
        ):
            raise RuntimeError("contextual reminder raw-message identity drift")
        has_events_table = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if has_events_table is not None:
            source_event = c.execute(
                "SELECT 1 FROM events WHERE group_id=? AND source_msg_id=? "
                "AND status='active' LIMIT 1",
                (group_id, source_message_id),
            ).fetchone()
            if source_event is not None:
                # A calendar event has its own mirror/sender ownership.  Do
                # not add four natural reminders beside it and create a fifth
                # independently deliverable identity.
                raise RuntimeError("contextual source is already calendar-bound")
        if pending_id is not None:
            pending = c.execute(
                "SELECT group_id,user_id,message_id,text,status,claim_token "
                "FROM pending_reminder_extract WHERE pending_id=?",
                (int(pending_id),),
            ).fetchone()
            if (
                pending is None
                or str(pending[0]) != group_id
                or str(pending[1] or "") != user_id
                or str(pending[2] or "") != command_message_id
                or _normalize_reminder_text(pending[3]) != command_text
                or str(pending[4]) != "processing"
                or str(pending[5] or "") != str(pending_claim_token)
            ):
                raise RuntimeError("contextual reminder pending claim drift")

        existing_rows = c.execute(
            "SELECT reminder_id,action,remind_at,status,source_kind,source_ref,"
            "source_text,mention_aliases FROM reminders WHERE group_id=? "
            "AND source_kind='contextual_date_once' AND source_ref IN (?,?,?,?)",
            (group_id, *[spec["source_ref"] for spec in normalized_specs]),
        ).fetchall()
        generic_source_rows = c.execute(
            "SELECT reminder_id FROM reminders WHERE group_id=? AND user_id=? "
            "AND status='pending' AND source_kind='' AND source_ref='' "
            "AND source_text=? ORDER BY reminder_id LIMIT 2",
            (group_id, user_id, source_text),
        ).fetchall()
        expected_by_ref = {spec["source_ref"]: spec for spec in normalized_specs}
        if existing_rows:
            if generic_source_rows:
                raise RuntimeError("contextual reminder duplicate has legacy residue")
            if len(existing_rows) != len(normalized_specs):
                raise RuntimeError("contextual reminder batch is partial")
            for row in existing_rows:
                expected = expected_by_ref.get(str(row[5] or ""))
                if (
                    expected is None
                    or str(row[1]) != expected["action"]
                    or int(row[2]) != expected["remind_at"]
                    or str(row[3]) != "pending"
                    or str(row[4]) != expected["source_kind"]
                    or _normalize_reminder_text(row[6]) != command_text
                    or _load_mention_aliases(row[7]) != expected["mention_aliases"]
                ):
                    raise RuntimeError("contextual reminder batch payload drift")
            if pending_id is not None:
                completed = c.execute(
                    "UPDATE pending_reminder_extract SET status='done',claimed_at=0,"
                    "claim_token='' WHERE pending_id=? AND status='processing' "
                    "AND claim_token=?",
                    (int(pending_id), str(pending_claim_token)),
                )
                if completed.rowcount != 1:
                    raise RuntimeError("contextual reminder pending completion drift")
            return {
                "outcome": "duplicate",
                "reminder_ids": sorted(int(row[0]) for row in existing_rows),
            }

        if legacy_reminder_id is None and generic_source_rows:
            raise RuntimeError("contextual legacy reminder is ambiguous")
        if legacy_reminder_id is not None and (
            len(generic_source_rows) != 1
            or int(generic_source_rows[0][0]) != int(legacy_reminder_id)
        ):
            raise RuntimeError("contextual legacy reminder identity drift")

        legacy_slot = next(
            spec for spec in normalized_specs if spec["source_ref"].endswith(":same:0")
        )
        reminder_ids: list[int] = []
        if legacy_reminder_id is not None:
            if not isinstance(expected_legacy, dict):
                raise RuntimeError("contextual legacy preimage is required")
            row = c.execute(
                "SELECT reminder_id,group_id,user_id,action,remind_at,status,"
                "source_kind,source_ref,source_text,mention_aliases,"
                "last_pushed_at,weekly_count,last_weekly_at,pushed_3d,pushed_1d,"
                "pushed_4hr,pushed_2hr,pushed_1hr,pushed_now "
                "FROM reminders WHERE reminder_id=?",
                (int(legacy_reminder_id),),
            ).fetchone()
            if row is None:
                raise RuntimeError("contextual legacy reminder is missing")
            actual = {
                "reminder_id": int(row[0]), "group_id": str(row[1]),
                "user_id": str(row[2] or ""), "action": str(row[3]),
                "remind_at": int(row[4]), "status": str(row[5]),
                "source_kind": str(row[6] or ""), "source_ref": str(row[7] or ""),
                "source_text": str(row[8] or ""),
                "mention_aliases": _load_mention_aliases(row[9]),
            }
            for index, key in enumerate(_REMINDER_PUSH_FLAG_COLUMNS, start=10):
                actual[key] = int(row[index] or 0)
            compare_keys = (
                "reminder_id", "group_id", "user_id", "action", "remind_at",
                "status", "source_kind", "source_ref", "source_text",
                "mention_aliases", *_REMINDER_PUSH_FLAG_COLUMNS,
            )
            if any(actual.get(key) != expected_legacy.get(key) for key in compare_keys):
                raise RuntimeError("contextual legacy reminder preimage drift")
            if (
                actual["group_id"] != group_id
                or actual["user_id"] != user_id
                or actual["status"] != "pending"
                or actual["remind_at"]
                != int(plan.get("legacy_expected_remind_at") or 0)
                or actual["source_kind"]
                or actual["source_ref"]
                or _normalize_reminder_text(actual["source_text"]) != source_text
                or any(actual[key] for key in _REMINDER_PUSH_FLAG_COLUMNS)
            ):
                raise RuntimeError("contextual legacy reminder is not safe to reconcile")
            protected = c.execute(
                "SELECT 1 FROM reminder_delivery_claims WHERE group_id=? "
                "AND delivery_kind='natural' AND subject_ref=? "
                "AND state IN ('sending','uncertain') LIMIT 1",
                (group_id, str(legacy_reminder_id)),
            ).fetchone()
            sent_ref = c.execute(
                "SELECT 1 FROM sent_reminder_refs WHERE group_id=? AND reminder_id=? LIMIT 1",
                (group_id, int(legacy_reminder_id)),
            ).fetchone()
            if protected is not None or sent_ref is not None:
                raise RuntimeError("contextual legacy reminder has delivery history")
            updated = c.execute(
                "UPDATE reminders SET action=?,remind_at=?,source_kind=?,source_ref=?,"
                "source_text=?,mention_aliases=? WHERE reminder_id=? AND status='pending'",
                (
                    legacy_slot["action"], legacy_slot["remind_at"],
                    legacy_slot["source_kind"], legacy_slot["source_ref"],
                    command_text,
                    json.dumps(legacy_slot["mention_aliases"], ensure_ascii=False),
                    int(legacy_reminder_id),
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("contextual legacy reminder update lost")
            reminder_ids.append(int(legacy_reminder_id))

        now = int(_time.time())
        for spec in normalized_specs:
            if legacy_reminder_id is not None and spec is legacy_slot:
                continue
            inserted = c.execute(
                "INSERT INTO reminders(group_id,user_id,action,remind_at,created_at,"
                "status,source_kind,source_ref,source_text,mention_aliases) "
                "VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                (
                    group_id, user_id, spec["action"], spec["remind_at"], now,
                    spec["source_kind"], spec["source_ref"], command_text,
                    json.dumps(spec["mention_aliases"], ensure_ascii=False),
                ),
            )
            if inserted.rowcount != 1:
                raise RuntimeError("contextual reminder insert failed")
            reminder_ids.append(int(c.execute("SELECT last_insert_rowid()").fetchone()[0]))
        if pending_id is not None:
            completed = c.execute(
                "UPDATE pending_reminder_extract SET status='done',claimed_at=0,"
                "claim_token='' WHERE pending_id=? AND status='processing' "
                "AND claim_token=?",
                (int(pending_id), str(pending_claim_token)),
            )
            if completed.rowcount != 1:
                raise RuntimeError("contextual reminder pending completion lost")
        return {"outcome": "created", "reminder_ids": sorted(reminder_ids)}


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
        "source_kind, source_ref, source_text, mention_aliases, time_kind, "
        "merged_details FROM reminders WHERE reminder_id=?",
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
        "time_kind": row[10],
        "merged_details": _load_merged_details(row[11]),
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


def cancel_unique_reminder_for_local_date(
    group_id: str,
    target_date: str,
    start_at: int,
    end_at: int,
) -> dict:
    """Atomically cancel one logical reminder target for a Taipei date.

    Generic reminders are selected by ``remind_at``. Calendar-backed reminders
    are selected by their source event's ``event_date`` so lead-time booking
    notifications cannot be mistaken for activities on the notification day.
    """

    target_date = str(target_date or "").strip()
    start_at = int(start_at)
    end_at = int(end_at)
    if not group_id or not target_date or end_at <= start_at:
        return {"status": "invalid", "count": 0, "reminder": None}

    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        generic_rows = c.execute(
            "SELECT reminder_id, action, remind_at, source_kind, source_ref "
            "FROM reminders WHERE group_id=? AND status='pending' "
            "AND remind_at>=? AND remind_at<? "
            "AND NOT (source_kind='calendar_event' "
            "AND COALESCE(source_ref, '')<>'') "
            "ORDER BY remind_at, reminder_id",
            (group_id, start_at, end_at),
        ).fetchall()

        event_groups: dict[str, list[tuple]] = {}
        has_events_table = c.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='events'"
        ).fetchone()
        if has_events_table is not None:
            event_rows = c.execute(
                "SELECT e.event_id, e.title, e.event_time, "
                "r.reminder_id, r.action, r.remind_at, "
                "r.status, r.source_kind, r.source_ref "
                "FROM events AS e LEFT JOIN reminders AS r "
                "ON r.group_id=e.group_id "
                "AND r.source_kind='calendar_event' "
                "AND r.source_ref=e.event_id "
                "WHERE e.group_id=? AND e.status='active' "
                "AND e.event_date=? "
                "ORDER BY e.event_id, r.reminder_id",
                (group_id, target_date),
            ).fetchall()
            for row in event_rows:
                event_groups.setdefault(str(row[0]), []).append(tuple(row))

        generic_clusters: list[list[tuple]] = []
        source_cluster_indexes: dict[tuple[str, str], int] = {}
        for raw_row in generic_rows:
            row = tuple(raw_row)
            source_kind = str(row[3] or "")
            source_ref = str(row[4] or "")
            if source_kind and source_ref:
                source_key = (source_kind, source_ref)
                cluster_index = source_cluster_indexes.get(source_key)
                if cluster_index is None:
                    source_cluster_indexes[source_key] = len(generic_clusters)
                    generic_clusters.append([row])
                else:
                    generic_clusters[cluster_index].append(row)
                continue

            normalized_action = _reminder_equivalence_key(row[1])
            cluster = next(
                (
                    candidate
                    for candidate in generic_clusters
                    if not str(candidate[0][3] or "")
                    and not str(candidate[0][4] or "")
                    and _reminder_equivalence_key(candidate[0][1])
                    == normalized_action
                    and abs(int(candidate[0][2]) - int(row[2])) <= 60
                ),
                None,
            )
            if cluster is None:
                generic_clusters.append([row])
            else:
                cluster.append(row)

        targets: list[tuple[str, object]] = [
            ("generic", tuple(cluster)) for cluster in generic_clusters
        ]
        cancelled_calendar_rows: list[tuple] = []
        for rows in event_groups.values():
            materialized = [row for row in rows if row[3] is not None]
            eligible = [
                row
                for row in materialized
                if str(row[6] or "") in {"pending", "done", "expired"}
            ]
            cancelled = [
                row for row in materialized if str(row[6] or "") == "cancelled"
            ]
            if eligible:
                targets.append(("calendar", tuple(eligible)))
            elif cancelled:
                cancelled_calendar_rows.append(cancelled[0])
            else:
                targets.append(("calendar_missing", rows[0]))

        if len(targets) > 1:
            return {
                "status": "ambiguous",
                "count": len(targets),
                "reminder": None,
            }
        if not targets:
            if len(cancelled_calendar_rows) == 1:
                reminder = _get_reminder_conn(
                    c,
                    int(cancelled_calendar_rows[0][3]),
                )
                return {
                    "status": "already_cancelled",
                    "count": 0,
                    "reminder": reminder,
                }
            if len(cancelled_calendar_rows) > 1:
                return {
                    "status": "ambiguous",
                    "count": len(cancelled_calendar_rows),
                    "reminder": None,
                }
            return {"status": "not_found", "count": 0, "reminder": None}

        target_kind, selected = targets[0]
        if target_kind == "generic":
            selected_rows = tuple(selected)
            representative = selected_rows[0]
            reminder_id = int(representative[0])
            action = str(representative[1])
            remind_at = int(representative[2])
            source_kind = str(representative[3] or "")
            source_ref = str(representative[4] or "")
        elif target_kind == "calendar":
            selected_rows = tuple(selected)
            representative = selected_rows[0]
            reminder_id = int(representative[3])
            action = str(representative[4])
            remind_at = int(representative[5])
            source_kind = str(representative[7] or "")
            source_ref = str(representative[8] or "")
        else:
            source_ref = str(selected[0] or "")
            action = str(selected[1] or "家族行事曆提醒")
            event_time = str(selected[2] or "").strip()
            hour = 0
            minute = 0
            if re.fullmatch(r"\d{1,2}:\d{2}", event_time):
                parsed_hour, parsed_minute = map(int, event_time.split(":"))
                if 0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59:
                    hour = parsed_hour
                    minute = parsed_minute
            remind_at = start_at + hour * 3600 + minute * 60
            cursor = c.execute(
                "INSERT INTO reminders("
                "group_id, user_id, action, remind_at, created_at, status, "
                "source_kind, source_ref, source_text, mention_aliases"
                ") VALUES (?, '', ?, ?, ?, 'cancelled', "
                "'calendar_event', ?, ?, '[]')",
                (
                    group_id,
                    action,
                    remind_at,
                    int(_time.time()),
                    source_ref,
                    action,
                ),
            )
            reminder = _get_reminder_conn(c, int(cursor.lastrowid))
            return {
                "status": "cancelled",
                "count": 1,
                "reminder": reminder,
            }

        delivery = _semantic_delivery_claim_conn(
            c,
            group_id=group_id,
            reminder_id=reminder_id,
            action=action,
            remind_at=remind_at,
            source_kind=source_kind,
            source_ref=source_ref,
            include_semantic=(target_kind == "generic"),
            restrict_to_source_cluster=bool(source_kind and source_ref),
        )
        if target_kind in {"generic", "calendar"}:
            reminder_id_index = 0 if target_kind == "generic" else 3
            reminder_ids = [
                int(row[reminder_id_index]) for row in selected_rows
            ]
            placeholders = ",".join(["?"] * len(reminder_ids))
            status_clause = (
                "status='pending'"
                if target_kind == "generic"
                else "status IN ('pending', 'done', 'expired')"
            )
            cursor = c.execute(
                "UPDATE reminders SET status='cancelled' "
                f"WHERE group_id=? AND {status_clause} "
                f"AND reminder_id IN ({placeholders})",
                [group_id, *reminder_ids],
            )
            expected_updates = len(reminder_ids)
        if cursor.rowcount != expected_updates:
            return {"status": "not_found", "count": 0, "reminder": None}
        reminder = _get_reminder_conn(c, reminder_id)
        if reminder is not None and delivery is not None:
            reminder["_delivery_in_flight"] = True
            reminder["_delivery_state"] = str(delivery[0] or "sending")
        return {
            "status": "cancelled" if reminder is not None else "not_found",
            "count": 1 if reminder is not None else 0,
            "reminder": reminder,
        }


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

    raw_offset = claim.get("offset")
    if raw_offset is None or isinstance(raw_offset, bool):
        return False
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        return False
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


def complete_pending_reminder(
    pending_id: int,
    pending_claim_token: str,
    group_id: str,
    user_id: str,
    action: str,
    remind_at: int,
    source_text: str,
    mention_aliases: list[str] | None,
    time_kind: str | None = None,
) -> tuple[int, str, dict]:
    """Atomically persist a drained reminder and close its queue row.

    No acknowledgement is queued (2026-10-04, 「不用確認」): the reminder's own
    next push is the family's signal.
    """
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
            time_kind,
        )
        persisted = _get_reminder_conn(c, reminder_id)
        if persisted is None:
            raise RuntimeError("persisted reminder row is missing")
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
    expected_calendar_event: dict | None = None,
) -> int | None:
    """Atomically create or refresh one pending source mirror.

    Existing delivery counters/flags are preserved. Terminal source rows and
    inactive calendar events are durable tombstones and are never revived.
    A supplied calendar snapshot must still match inside the write transaction.
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
        if require_active_calendar_event or expected_calendar_event is not None:
            event = c.execute(
                "SELECT title, event_date, event_time, location, participants, "
                "source_msg_id, event_type, reminder_lead_days, status "
                "FROM events WHERE group_id=? AND event_id=? "
                "AND status='active' LIMIT 1",
                (group_id, source_ref),
            ).fetchone()
            if event is None:
                return None
            if expected_calendar_event is not None:
                # Exclude delivery flags: a push does not change the payload.
                # Compare under the same write lock to fence stale repair jobs.
                fields = (
                    "title", "event_date", "event_time", "location", "participants",
                    "source_msg_id", "event_type", "reminder_lead_days", "status",
                )
                for field, current in zip(fields, event):
                    expected = expected_calendar_event.get(field)
                    if field == "participants":
                        try:
                            current = json.loads(current or "[]")
                            if isinstance(expected, str) or expected is None:
                                expected = json.loads(expected or "[]")
                        except (TypeError, ValueError):
                            return None
                    if current != expected:
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
                "mention_aliases, time_kind, merged_details "
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
                "mention_aliases, time_kind, merged_details "
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
            best_rank = max(
                reminder_intent.time_rank(row["time_kind"]) for row in cluster
            )
            time_kind = keep["time_kind"]
            if reminder_intent.time_rank(time_kind) < best_rank:
                time_kind = next(
                    row["time_kind"]
                    for row in merge_order
                    if reminder_intent.time_rank(row["time_kind"]) == best_rank
                )
            fragments: list[dict] = []
            for row in merge_order:
                for item in _load_merged_details(row["merged_details"]):
                    if all(item["key"] != kept["key"] for kept in fragments):
                        fragments.append(item)
            merged_details = json.dumps(fragments, ensure_ascii=False)
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
                "pushed_1hr=?, pushed_now=?, time_kind=?, merged_details=? "
                "WHERE reminder_id=?",
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
                    time_kind,
                    merged_details,
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


def get_pending_reminder_extract(pending_id: int) -> dict | None:
    """Return one exact pending-extraction row by durable id."""

    if int(pending_id) <= 0:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT pending_id,group_id,user_id,message_id,text,created_at,retries,"
            "claimed_at,claim_token,status FROM pending_reminder_extract "
            "WHERE pending_id=?",
            (int(pending_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "pending_id": int(row[0]), "group_id": str(row[1]),
        "user_id": str(row[2] or ""), "message_id": str(row[3] or ""),
        "text": str(row[4] or ""), "created_at": int(row[5]),
        "retries": int(row[6]), "claimed_at": int(row[7] or 0),
        "claim_token": str(row[8] or ""), "status": str(row[9]),
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


def list_pending_reminder_retries(
    group_id: str,
    limit: int = 5,
    *,
    after_created_at: int | None = None,
    after_pending_id: int | None = None,
) -> list[dict]:
    """取該 group 待重抽的 pending（status='pending'，舊→新）。

    ``after_*`` 是穩定的 keyset cursor；local-only backstop 用它跨頁尋找
    可本機解析的提醒，避免前一頁都是 Gemini-dependent rows 時永久餓死。
    """
    reclaim_stale_pending_reminders(group_id=group_id)
    with _conn() as c:
        if after_created_at is None or after_pending_id is None:
            rows = c.execute(
                "SELECT pending_id, group_id, user_id, message_id, text, "
                "created_at, retries FROM pending_reminder_extract "
                "WHERE group_id = ? AND status = 'pending' "
                "ORDER BY created_at, pending_id LIMIT ?",
                (group_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT pending_id, group_id, user_id, message_id, text, "
                "created_at, retries FROM pending_reminder_extract "
                "WHERE group_id = ? AND status = 'pending' "
                "AND (created_at > ? OR (created_at = ? AND pending_id > ?)) "
                "ORDER BY created_at, pending_id LIMIT ?",
                (
                    group_id,
                    int(after_created_at),
                    int(after_created_at),
                    int(after_pending_id),
                    limit,
                ),
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
    """Mark a successfully persisted extraction as done."""
    if status != "done":
        raise ValueError(
            "non-success terminal states require an explicit pending reminder API: "
            "drop_pending_reminder(pending_id, claim_token, group_id, reason)"
        )
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract "
            "SET status = ?, claimed_at=0, claim_token='' "
            "WHERE pending_id = ? AND status='processing' AND claim_token=?",
            (status, pending_id, claim_token),
        )
        return cur.rowcount == 1


# Why a queued extraction ended without a reminder.  A fixed set: the daily
# audit shows these to Andrew, so exception text never lands here.
PENDING_REMINDER_DROP_REASONS = frozenset(
    {
        "no_date",
        "model_null",
        "expired",
        "stale",
        "invalid_source",
        "cancelled_source",
    }
)


def drop_pending_reminder(
    pending_id: int,
    claim_token: str,
    group_id: str,
    reason: str,
) -> bool:
    """Close one claimed queue row silently (2026-10-04: no group receipt).

    Only the claim owner can drop it.  Returns False when the claim was lost.
    """

    if reason not in PENDING_REMINDER_DROP_REASONS:
        raise ValueError("unknown pending reminder drop reason")
    group_id = str(group_id or "").strip()
    claim_token = str(claim_token or "").strip()
    if not group_id or not claim_token:
        raise ValueError("pending reminder drop needs its group and claim token")
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract "
            "SET status='dropped', claimed_at=0, claim_token='', "
            "dropped_at=?, drop_reason=? "
            "WHERE pending_id=? AND group_id=? AND status='processing' "
            "AND claim_token=?",
            (int(_time.time()), reason, int(pending_id), group_id, claim_token),
        )
        return cur.rowcount == 1


def drop_pending_reminder_for_cancelled_source(
    pending_id: int,
    group_id: str,
    claim_token: str,
) -> bool:
    """Drop a queue mirror whose durable calendar source was cancelled."""

    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract "
            "SET status='dropped', claimed_at=0, claim_token='', "
            "dropped_at=?, drop_reason='cancelled_source' "
            "WHERE pending_id=? AND group_id=? AND status='processing' "
            "AND claim_token=?",
            (int(_time.time()), int(pending_id), str(group_id), str(claim_token)),
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


def drop_stale_pending_reminders(
    max_age_sec: int,
    group_id: str | None = None,
) -> int:
    """清超齡 pending（created_at < now-max_age）→ status='dropped'。回清掉筆數。
    不寫任何 plaintext DLQ 檔（PII 只留 DB），也不寫群組回執（2026-10-04）。"""
    import time

    cutoff = int(time.time()) - max_age_sec
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        event_columns = {
            str(row[1]) for row in c.execute("PRAGMA table_info(events)").fetchall()
        }
        source_guard = ""
        if {"group_id", "source_msg_id"}.issubset(event_columns):
            source_guard = (
                " AND (p.message_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM events AS e WHERE e.group_id=p.group_id "
                "AND e.source_msg_id=p.message_id))"
            )
        if group_id is not None:
            rows = c.execute(
                "SELECT p.pending_id, p.group_id FROM pending_reminder_extract AS p "
                "WHERE p.status IN ('pending','processing') AND p.created_at < ? "
                "AND p.group_id = ?" + source_guard,
                (cutoff, group_id),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT p.pending_id, p.group_id FROM pending_reminder_extract AS p "
                "WHERE p.status IN ('pending','processing') AND p.created_at < ?"
                + source_guard,
                (cutoff,),
            ).fetchall()
        now = int(time.time())
        dropped = 0
        for pending_id, row_group_id in rows:
            completed = c.execute(
                "UPDATE pending_reminder_extract "
                "SET status='dropped', claimed_at=0, claim_token='', "
                "dropped_at=?, drop_reason='stale' "
                "WHERE pending_id=? AND group_id=? "
                "AND status IN ('pending','processing') AND created_at < ?",
                (now, int(pending_id), str(row_group_id), cutoff),
            )
            if completed.rowcount != 1:
                raise RuntimeError("stale pending reminder drop lost its row")
            dropped += 1
        return dropped


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
                    "created_at, source_kind, source_ref, source_text, mention_aliases, "
                    "time_kind, merged_details "
                    "FROM reminders "
                    "WHERE status='pending' AND group_id=? AND remind_at BETWEEN ? AND ? "
                    "ORDER BY remind_at",
                    (group_id, lo, hi),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT reminder_id, group_id, user_id, action, remind_at, "
                    "created_at, source_kind, source_ref, source_text, mention_aliases, "
                    "time_kind, merged_details "
                    "FROM reminders "
                    "WHERE status='pending' AND remind_at BETWEEN ? AND ? "
                    "ORDER BY remind_at",
                    (lo, hi),
                ).fetchall()
        else:
            if group_id:
                rows = c.execute(
                    "SELECT reminder_id, group_id, user_id, action, remind_at, "
                    "created_at, source_kind, source_ref, source_text, mention_aliases, "
                    "time_kind, merged_details "
                    "FROM reminders "
                    "WHERE status='pending' AND group_id=? AND remind_at >= ? "
                    "ORDER BY remind_at",
                    (group_id, now - 86400),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT reminder_id, group_id, user_id, action, remind_at, "
                    "created_at, source_kind, source_ref, source_text, mention_aliases, "
                    "time_kind, merged_details "
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
            "time_kind": r[10],
            "merged_details": _load_merged_details(r[11]),
        }
        for r in rows
    ]


def list_generic_reminders_between(
    group_id: str,
    start_at: int,
    end_at: int,
    *,
    topic: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Pure read of non-cancelled natural reminders in ``[start_at, end_at)``.

    Calendar queries use this narrow reader as a fail-closed compatibility
    bridge for legacy rows that were saved as reminders instead of events.
    Unlike :func:`list_pending_reminders`, this function performs no cleanup,
    deduplication, or other database mutation.
    """

    normalized_group_id = str(group_id or "").strip()
    normalized_start = int(start_at)
    normalized_end = int(end_at)
    normalized_limit = max(1, min(int(limit), 200))
    if not normalized_group_id or normalized_end <= normalized_start:
        return []
    normalized_topic = str(topic or "").strip()
    topic_clause = ""
    parameters: list[object] = [
        normalized_group_id,
        normalized_start,
        normalized_end,
    ]
    if normalized_topic:
        escaped_topic = (
            normalized_topic.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        topic_clause = (
            "AND (action LIKE ? ESCAPE '\\' OR "
            "COALESCE(source_text, '') LIKE ? ESCAPE '\\') "
        )
        topic_pattern = f"%{escaped_topic}%"
        parameters.extend((topic_pattern, topic_pattern))
    parameters.append(normalized_limit + 1)
    with _conn() as c:
        rows = c.execute(
            "SELECT reminder_id, group_id, user_id, action, remind_at, "
            "created_at, status, source_kind, source_ref, source_text, mention_aliases "
            "FROM reminders "
            "WHERE status IN ('pending','done','expired') AND group_id=? "
            "AND remind_at>=? AND remind_at<? "
            "AND COALESCE(source_kind, '')='' "
            "AND COALESCE(source_ref, '')='' "
            f"{topic_clause}"
            "ORDER BY remind_at, reminder_id LIMIT ?",
            parameters,
        ).fetchall()
    if len(rows) > normalized_limit:
        raise RuntimeError("legacy reminder query result was truncated")
    return [
        {
            "reminder_id": int(row[0]),
            "group_id": str(row[1]),
            "user_id": str(row[2] or ""),
            "action": str(row[3] or ""),
            "remind_at": int(row[4]),
            "created_at": int(row[5]),
            "status": str(row[6] or ""),
            "source_kind": str(row[7] or ""),
            "source_ref": str(row[8] or ""),
            "source_text": str(row[9] or ""),
            "mention_aliases": _load_mention_aliases(row[10]),
        }
        for row in rows
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
                "UPDATE reminders SET action = ?, remind_at = ?, time_kind = 'clock', "
                "merged_details = '[]', "
                "source_text = COALESCE(?, source_text), "
                "last_pushed_at = 0, weekly_count = 0, last_weekly_at = 0, "
                "pushed_3d = 0, pushed_1d = 0, pushed_4hr = 0, "
                "pushed_2hr = 0, pushed_1hr = 0, pushed_now = 0 "
                "WHERE reminder_id = ? AND status = 'pending'",
                (action, remind_at, source_text, reminder_id),
            )
        else:
            cur = c.execute(
                "UPDATE reminders SET remind_at = ?, time_kind = 'clock', "
                "merged_details = '[]', "
                "source_text = COALESCE(?, source_text), "
                "last_pushed_at = 0, weekly_count = 0, last_weekly_at = 0, "
                "pushed_3d = 0, pushed_1d = 0, pushed_4hr = 0, "
                "pushed_2hr = 0, pushed_1hr = 0, pushed_now = 0 "
                "WHERE reminder_id = ? AND status = 'pending'",
                (remind_at, source_text, reminder_id),
            )
        return cur.rowcount > 0


# Quoted reschedules (2026-10-03): the log lets a redelivered webhook replay
# what its message already did instead of re-applying it over a later edit.
# It keeps only times and action hashes (receipts render from the live row)
# and follows the inbound_events retention.
_RESCHEDULE_LOG_RETENTION_SECONDS = _INBOUND_EVENT_RETENTION_SECONDS


def reminder_action_hash(action: str) -> str:
    return hashlib.sha256(str(action or "").encode("utf-8")).hexdigest()


def _get_reschedule_log_conn(
    c: sqlite3.Connection, group_id: str, message_id: str
) -> dict | None:
    row = c.execute(
        "SELECT reminder_id, old_remind_at, new_remind_at, old_action_hash, "
        "new_action_hash FROM reminder_reschedule_log "
        "WHERE group_id=? AND message_id=? AND created_at>=?",
        (group_id, message_id, int(_time.time()) - _RESCHEDULE_LOG_RETENTION_SECONDS),
    ).fetchone()
    if row is None:
        return None
    return {
        "reminder_id": int(row[0]),
        "old_remind_at": int(row[1]),
        "new_remind_at": int(row[2]),
        "old_action_hash": str(row[3]),
        "new_action_hash": str(row[4]),
    }


def get_reminder_reschedule_log(group_id: str, message_id: str) -> dict | None:
    """Return what an inbound message already did to a reminder, if anything."""
    if not group_id or not message_id:
        return None
    with _conn() as c:
        return _get_reschedule_log_conn(c, group_id, message_id)


def _reminder_source_pipeline_busy_conn(
    c: sqlite3.Connection, group_id: str, user_id: str, source_text: str
) -> bool:
    """Same in-flight checks as reminder_restatement.reconcile."""
    if not source_text:
        return False
    queued = c.execute(
        "SELECT 1 FROM pending_reminder_extract WHERE group_id=? AND user_id=? "
        "AND text=? AND status='processing' LIMIT 1",
        (group_id, user_id, source_text),
    ).fetchone()
    if queued is not None:
        return True
    outbox = c.execute(
        "SELECT 1 FROM reminder_confirmation_outbox AS o WHERE o.group_id=? "
        "AND o.status IN ('pending','processing') AND (o.source_ref IN ("
        "SELECT message_id FROM raw_messages WHERE group_id=? AND user_id=? AND text=?) "
        "OR o.source_ref IN (SELECT 'pending_reminder:' || pending_id "
        "FROM pending_reminder_extract WHERE group_id=? AND user_id=? AND text=?)) "
        "LIMIT 1",
        (group_id, group_id, user_id, source_text, group_id, user_id, source_text),
    ).fetchone()
    return outbox is not None


def _insert_reschedule_log_conn(
    c: sqlite3.Connection,
    group_id: str,
    message_id: str,
    reminder_id: int,
    previous: dict,
    new_action: str,
    new_remind_at: int,
) -> None:
    now = int(_time.time())
    c.execute(
        "DELETE FROM reminder_reschedule_log WHERE created_at < ?",
        (now - _RESCHEDULE_LOG_RETENTION_SECONDS,),
    )
    c.execute(
        "INSERT INTO reminder_reschedule_log(group_id, message_id, reminder_id, "
        "old_remind_at, new_remind_at, old_action_hash, new_action_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            group_id,
            message_id,
            int(reminder_id),
            int(previous["remind_at"]),
            int(new_remind_at),
            reminder_action_hash(str(previous["action"])),
            reminder_action_hash(new_action),
            now,
        ),
    )


def reschedule_generic_reminder(
    group_id: str,
    reminder_id: int,
    *,
    inbound_message_id: str,
    expected_action: str,
    expected_remind_at: int,
    new_remind_at: int,
    new_action: str,
) -> dict:
    """Atomically move one pending generic reminder for a quoted reschedule.

    ``status`` is one of updated, unchanged, replayed, not_found, not_generic,
    terminal, conflict, duplicate, collision, busy, delivery_uncertain or
    unavailable.  Refusals write nothing; updated/unchanged also record the
    inbound message so a redelivery replays it (``replayed``).  Any natural
    delivery claim (sending or uncertain) blocks the change: a sender may still
    deliver the old text, and an uncertain fence must not be dropped.
    A time change follows update_reminder_schedule: push stages reset,
    time_kind becomes 'clock' (a later vaguer mention cannot move it) and
    absorbed merged_details are cleared.
    """
    if not group_id or not inbound_message_id:
        return {"status": "unavailable"}
    try:
        reminder_id = int(reminder_id)
        new_remind_at = int(new_remind_at)
        new_action = str(new_action)
        with _lock, _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            logged = _get_reschedule_log_conn(c, group_id, inbound_message_id)
            if logged is not None:
                return {"status": "replayed", "log": logged}
            row = _get_reminder_conn(c, reminder_id)
            if row is None or row["group_id"] != group_id:
                return {"status": "not_found"}
            if row["source_kind"] or row["source_ref"]:
                return {"status": "not_generic"}
            if row["status"] != "pending":
                return {"status": "terminal"}
            if row["action"] != str(expected_action) or row["remind_at"] != int(
                expected_remind_at
            ):
                return {"status": "conflict"}
            claim_states = {
                str(state[0])
                for state in c.execute(
                    "SELECT state FROM reminder_delivery_claims WHERE group_id=? "
                    "AND delivery_kind='natural' AND subject_ref=? "
                    "AND state IN ('sending', 'uncertain')",
                    (group_id, str(reminder_id)),
                ).fetchall()
            }
            if "sending" in claim_states:
                return {"status": "busy"}
            if "uncertain" in claim_states:
                return {"status": "delivery_uncertain"}
            if _reminder_source_pipeline_busy_conn(
                c, group_id, row["user_id"], row["source_text"]
            ):
                return {"status": "busy"}
            peers = c.execute(
                "SELECT action, remind_at FROM reminders WHERE group_id=? "
                "AND status='pending' AND reminder_id<>?",
                (group_id, reminder_id),
            ).fetchall()
            old_key = _reminder_equivalence_key(row["action"])
            new_key = _reminder_equivalence_key(new_action)
            if any(
                _reminder_equivalence_key(peer[0]) == old_key
                and abs(int(peer[1]) - row["remind_at"]) <= 60
                for peer in peers
            ):
                return {"status": "duplicate"}
            time_changed = new_remind_at != row["remind_at"]
            if not time_changed and new_action == row["action"]:
                _insert_reschedule_log_conn(
                    c, group_id, inbound_message_id, reminder_id, row,
                    new_action, new_remind_at,
                )
                return {"status": "unchanged", "reminder": row, "previous": row}
            if any(
                _reminder_equivalence_key(peer[0]) in {old_key, new_key}
                and abs(int(peer[1]) - new_remind_at) <= 60
                for peer in peers
            ):
                return {"status": "collision"}
            reset = ""
            if time_changed:
                reset = ", " + ", ".join(
                    f"{column}=0" for column in _REMINDER_PUSH_FLAG_COLUMNS
                ) + ", time_kind='clock', merged_details='[]'"
            cursor = c.execute(
                "UPDATE reminders SET action=?, remind_at=?" + reset + " "
                "WHERE group_id=? AND reminder_id=? AND status='pending' "
                "AND action=? AND remind_at=? "
                "AND COALESCE(source_kind, '')='' AND COALESCE(source_ref, '')=''",
                (
                    new_action,
                    new_remind_at,
                    group_id,
                    reminder_id,
                    row["action"],
                    row["remind_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("reminder reschedule compare-and-set failed")
            if row["source_text"]:
                c.execute(
                    "UPDATE pending_reminder_extract SET status='dropped', "
                    "claimed_at=0, claim_token='' WHERE group_id=? AND user_id=? "
                    "AND text=? AND status='pending'",
                    (group_id, row["user_id"], row["source_text"]),
                )
            _insert_reschedule_log_conn(
                c, group_id, inbound_message_id, reminder_id, row,
                new_action, new_remind_at,
            )
            return {
                "status": "updated",
                "reminder": _get_reminder_conn(c, reminder_id),
                "previous": row,
            }
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return {"status": "unavailable"}


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
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = int(time.time())
    cutoff = now - max(0, int(grace_seconds))
    today_tw = datetime.fromtimestamp(now, ZoneInfo("Asia/Taipei")).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    contextual_cutoff = int(today_tw.timestamp())
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if group_id is not None:
            expired = c.execute(
                "UPDATE reminders SET status='expired' "
                "WHERE status='pending' AND group_id=? AND remind_at < ? "
                "AND source_kind<>'' AND source_ref<>'' "
                "AND NOT (source_kind='contextual_date_once' AND remind_at>=?)",
                (group_id, cutoff, contextual_cutoff),
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
                "AND source_kind<>'' AND source_ref<>'' "
                "AND NOT (source_kind='contextual_date_once' AND remind_at>=?)",
                (cutoff, contextual_cutoff),
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


def list_pending_reminders_full(
    group_id: str | None = None, *, dedupe: bool = True
) -> list[dict]:
    """完整版 list — 含所有 stage flag 給 reminder_push.py 用。

    ``dedupe=False`` skips the exact-duplicate cleanup so a caller can read
    without writing (reminder_push --dry-run).
    """
    import time
    if dedupe:
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
                "mention_aliases, time_kind, merged_details "
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
                "mention_aliases, time_kind, merged_details "
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
            "time_kind": r[19],
            "merged_details": _load_merged_details(r[20]),
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


def fold_same_event_reminders(primary: dict, peers: list[dict]) -> list[int]:
    """Fold pending rows of one event into ``primary`` before a push.

    2026-10-04 Andrew「同一件事只留一筆、階段照舊」(P4 item 1).  ``primary``
    and ``peers`` are the row snapshots reminder_push planned the fold from.
    In one immediate transaction each peer is re-checked: still pending,
    unchanged since the snapshot, not a calendar mirror or a 前一天／當天 pair,
    and with no delivery in flight.  Its words and absorbed details are added
    to the primary's merged_details (same fragments as the write-time merge),
    the peer is cancelled, and outbound messages bound to it are re-bound to
    the primary, so quoting an older push still reaches the reminder that
    remains.  A peer at the very same time also hands over the stages it
    already sent (and its last weekly notice), so the same notice is never
    sent twice for one moment; rows at other times hand over nothing, their
    stages were about another clock.  Nothing is pushed here.  Returns the
    folded ids.
    """
    group_id = str(primary.get("group_id") or "")
    primary_id = int(primary["reminder_id"])
    folded: list[int] = []
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        kept = c.execute(
            "SELECT action, remind_at, COALESCE(source_text, ''), "
            "COALESCE(merged_details, '[]'), COALESCE(source_kind, ''), "
            "COALESCE(source_ref, '') FROM reminders "
            "WHERE group_id=? AND reminder_id=? AND status='pending'",
            (group_id, primary_id),
        ).fetchone()
        if (
            kept is None
            or str(kept[0]) != str(primary.get("action") or "")
            or int(kept[1]) != int(primary["remind_at"])
            or kept[4] == "contextual_date_once"
            or (kept[4] == "calendar_event" and kept[5])
        ):
            return []
        fragments = _load_merged_details(kept[3])
        known_keys = {item["key"] for item in fragments}
        own_key = _merged_detail_fragment(str(kept[0]), str(kept[2]))["key"]
        sent_flags = [0, 0, 0, 0, 0]  # pushed_3d, 1d, 4hr, 2hr, 1hr
        last_weekly_at = 0
        for peer in peers:
            peer_id = int(peer["reminder_id"])
            if peer_id == primary_id or peer_id in folded:
                continue
            row = c.execute(
                "SELECT action, remind_at, COALESCE(source_text, ''), "
                "COALESCE(merged_details, '[]'), COALESCE(user_id, ''), "
                "COALESCE(mention_aliases, '[]'), time_kind, "
                "COALESCE(source_kind, ''), COALESCE(source_ref, ''), "
                "pushed_3d, pushed_1d, pushed_4hr, pushed_2hr, pushed_1hr, "
                "last_weekly_at FROM reminders "
                "WHERE group_id=? AND reminder_id=? AND status='pending'",
                (group_id, peer_id),
            ).fetchone()
            if row is None:
                continue
            if (
                str(row[0]) != str(peer.get("action") or "")
                or int(row[1]) != int(peer["remind_at"])
                or str(row[4]) != str(peer.get("user_id") or "")
                or _load_mention_aliases(row[5])
                != _normalize_mention_aliases(list(peer.get("mention_aliases") or []))
                or row[6] != peer.get("time_kind")
            ):
                continue  # changed since the fold was planned
            if row[7] == "contextual_date_once" or (row[7] == "calendar_event" and row[8]):
                continue
            if _delivery_in_flight_conn(c, group_id, peer_id, str(row[0]), int(row[1])):
                continue
            for fragment in (
                _merged_detail_fragment(str(row[0]), str(row[2])),
                *_load_merged_details(row[3]),
            ):
                if (
                    fragment["key"] == own_key
                    or fragment["key"] in known_keys
                    or len(fragments) >= _MERGED_DETAIL_LIMIT
                ):
                    continue
                fragments.append(fragment)
                known_keys.add(fragment["key"])
            if int(row[1]) == int(kept[1]):
                sent_flags = [max(a, int(b or 0)) for a, b in zip(sent_flags, row[9:14])]
                last_weekly_at = max(last_weekly_at, int(row[14] or 0))
            folded.append(peer_id)
        if not folded:
            return []
        c.execute(
            "UPDATE reminders SET merged_details=?, "
            "pushed_3d=MAX(pushed_3d, ?), pushed_1d=MAX(pushed_1d, ?), "
            "pushed_4hr=MAX(pushed_4hr, ?), pushed_2hr=MAX(pushed_2hr, ?), "
            "pushed_1hr=MAX(pushed_1hr, ?), last_weekly_at=MAX(last_weekly_at, ?) "
            "WHERE reminder_id=? AND status='pending'",
            (json.dumps(fragments, ensure_ascii=False), *sent_flags, last_weekly_at, primary_id),
        )
        marks = ",".join("?" for _ in folded)
        c.execute(
            "UPDATE reminders SET status='cancelled' "
            f"WHERE status='pending' AND reminder_id IN ({marks})",
            folded,
        )
        c.execute(
            "UPDATE sent_reminder_refs SET reminder_id=?, source_kind=?, source_ref=? "
            f"WHERE group_id=? AND reminder_id IN ({marks})",
            (primary_id, str(kept[4]), str(kept[5]), group_id, *folded),
        )
    return folded


# 2026-10-04: a creation receipt already tells the family about the reminder,
# so the stage open at that moment (or opening within these minutes) must not
# follow it as a second message.  "now" is the reminder itself and is never
# consumed.
CONSUME_OPEN_STAGE_HORIZON_SECONDS = 20 * 60


def consume_open_stages(reminder_ids, now: int | None = None) -> int:
    """Mark the stages a delivered creation receipt stood in for.

    For each pending reminder in ``reminder_ids``, every ladder stage that is
    open at ``now`` or opens within the next 20 minutes is recorded as sent:
    3d/1d/4hr/2hr/1hr set their flag, weekly only sets ``last_weekly_at`` (the
    weekly counter is untouched).  "now" is never marked.  Idempotent; returns
    how many stage marks were newly made.  Call it only after LINE accepted
    the receipt; silent creations (pending drains) must not call it, because
    the next stage push is then the family's only signal.
    """
    import reminder_stages

    ids = sorted({int(rid) for rid in (reminder_ids or []) if rid is not None})
    if not ids:
        return 0
    now = int(_time.time()) if now is None else int(now)
    placeholders = ",".join("?" for _ in ids)
    marked = 0
    with _lock, _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT reminder_id, remind_at, source_kind, source_ref, source_text, "
            "last_weekly_at, pushed_3d, pushed_1d, pushed_4hr, pushed_2hr, "
            "pushed_1hr, pushed_now FROM reminders "
            f"WHERE status='pending' AND reminder_id IN ({placeholders})",
            ids,
        ).fetchall()
        for row in rows:
            row = dict(row)
            stages = reminder_stages.stages_opening_within(
                row, now, CONSUME_OPEN_STAGE_HORIZON_SECONDS
            )
            for stage in stages:
                if stage not in reminder_stages.CONSUMABLE_STAGES:
                    continue
                if stage == "weekly":
                    if not reminder_stages.weekly_owed(row.get("last_weekly_at"), now):
                        continue
                    cursor = c.execute(
                        "UPDATE reminders SET last_weekly_at=? "
                        "WHERE reminder_id=? AND status='pending'",
                        (now, int(row["reminder_id"])),
                    )
                    row["last_weekly_at"] = now
                else:
                    column = reminder_stages.FLAG_COLUMNS[stage]
                    cursor = c.execute(
                        f"UPDATE reminders SET {column}=1 "
                        f"WHERE reminder_id=? AND status='pending' AND {column}=0",
                        (int(row["reminder_id"]),),
                    )
                marked += int(cursor.rowcount == 1)
    return marked


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
