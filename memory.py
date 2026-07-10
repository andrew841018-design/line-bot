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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
            CREATE TABLE IF NOT EXISTS pending_reminder_extract (
                pending_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id     TEXT NOT NULL,
                user_id      TEXT NOT NULL DEFAULT '',
                message_id   TEXT,
                text         TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                retries      INTEGER NOT NULL DEFAULT 0,
                claimed_at   INTEGER NOT NULL DEFAULT 0,
                status       TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_pending_reminder_status
                ON pending_reminder_extract(status, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_reminder_msgid
                ON pending_reminder_extract(message_id) WHERE message_id IS NOT NULL;
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
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_reminder_claimed "
            "ON pending_reminder_extract(status, claimed_at)"
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
    回 reminder_id；跳過時回 None。
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

    # Historical path: 無 source_kind/source_ref 時保持既有行為（去重）
    import time
    now = int(time.time())
    action = _normalize_reminder_text(action)
    source_text = _normalize_reminder_text(source_text)
    mentions = _normalize_mention_aliases(mention_aliases)
    mentions_json = json.dumps(mentions, ensure_ascii=False)
    with _lock, _conn() as c:
        # 去重
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
            return None
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
            return existing_id
        c.execute(
            "INSERT INTO reminders(group_id, user_id, action, remind_at, "
            "created_at, status, source_kind, source_ref, source_text, mention_aliases) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (
                group_id, user_id, action, remind_at, now,
                "", "", source_text, mentions_json
            ),
        )
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


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
    return reminder_id


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

    差異在於：若已有相同來源但已是 done/expired，會改回 pending 並更新內容。
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
    return reminder_id


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

        def flush(cluster: list[sqlite3.Row]) -> int:
            if len(cluster) < 2:
                return 0

            keep = _best_duplicate_reminder(cluster)
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

            c.execute(
                "UPDATE reminders SET user_id=?, source_text=?, mention_aliases=?, "
                "created_at=?, last_pushed_at=?, weekly_count=?, last_weekly_at=?, "
                "pushed_3d=?, pushed_1d=?, pushed_4hr=?, pushed_2hr=?, "
                "pushed_1hr=?, pushed_now=? WHERE reminder_id=?",
                (
                    user_id,
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

            placeholders = ",".join(["?"] * len(duplicate_ids))
            cursor = c.execute(
                f"DELETE FROM reminders WHERE reminder_id IN ({placeholders})",
                duplicate_ids,
            )
            return cursor.rowcount

        deleted = 0
        cluster: list[sqlite3.Row] = []
        cluster_key: tuple[str, str] | None = None
        cluster_start_ts = 0

        for row in rows:
            action = _normalize_reminder_text(row["action"])
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
                "SET retries = retries + 1, status='pending', claimed_at=0 "
                "WHERE status='processing' AND claimed_at > 0 "
                "AND claimed_at < ? AND group_id = ?",
                (cutoff, group_id),
            )
        else:
            cur = c.execute(
                "UPDATE pending_reminder_extract "
                "SET retries = retries + 1, status='pending', claimed_at=0 "
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


def claim_pending_reminder(pending_id: int) -> bool:
    """Atomic claim：status pending→processing。回 True=搶到（rowcount==1）。
    防 piggyback drain 與 cron worker 同時抽同一筆 → 重複 reminder。"""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE pending_reminder_extract SET status='processing', claimed_at=? "
            "WHERE pending_id = ? AND status = 'pending'",
            (int(_time.time()), pending_id),
        )
        return cur.rowcount == 1


def mark_pending_reminder(pending_id: int, status: str) -> None:
    """標記結果：'done'（已抽存）/'dropped'（非提醒、過期）。"""
    with _lock, _conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract SET status = ?, claimed_at=0 "
            "WHERE pending_id = ?",
            (status, pending_id),
        )


def release_pending_reminder(pending_id: int) -> None:
    """重抽又撞 quota：retries+1 並退回 'pending' 等下輪 drain。"""
    with _lock, _conn() as c:
        c.execute(
            "UPDATE pending_reminder_extract "
            "SET retries = retries + 1, status='pending', claimed_at=0 "
            "WHERE pending_id = ?",
            (pending_id,),
        )


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
    """Delete pending reminders that are clearly past their due window.

    `reminder_push` still has a ±15 minute "now" stage. A 1-hour default grace
    keeps that path intact while preventing stale pending rows from lingering
    for days.
    """
    import time

    cutoff = int(time.time()) - max(0, int(grace_seconds))
    with _lock, _conn() as c:
        if group_id is not None:
            cursor = c.execute(
                "DELETE FROM reminders "
                "WHERE status='pending' AND group_id=? AND remind_at < ?",
                (group_id, cutoff),
            )
        else:
            cursor = c.execute(
                "DELETE FROM reminders "
                "WHERE status='pending' AND remind_at < ?",
                (cutoff,),
            )
        return cursor.rowcount


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
            c.execute(
                "UPDATE reminders SET weekly_count = weekly_count + 1, "
                "last_weekly_at = ?, last_pushed_at = ? WHERE reminder_id = ?",
                (now, now, reminder_id),
            )
        elif stage in ("3d", "1d", "4hr", "2hr", "1hr"):
            col = f"pushed_{stage}"
            c.execute(
                f"UPDATE reminders SET {col} = 1, last_pushed_at = ? "
                f"WHERE reminder_id = ?",
                (now, reminder_id),
            )
        elif stage == "now":
            c.execute(
                "UPDATE reminders SET pushed_now = 1, last_pushed_at = ?, "
                "status = 'done' WHERE reminder_id = ?",
                (now, reminder_id),
            )
        else:
            return False
        return True


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
