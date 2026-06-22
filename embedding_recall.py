"""Local sentence-transformers based semantic recall for line_bot.

Added 2026-05-19. Replaces nothing — sits on top of the existing flow as a
retrieval-only layer. Generation stays Gemini (per 5/18 directive).

Two retrieval modes:
  - retrieve()           — top-K semantically similar past messages (any role)
  - retrieve_case_pairs() — top-K similar past user queries paired with the
                            bot's reply that followed (for case-style anchor)

The ST model is shared with `grounding_local._get_st_model()` so we don't
load two copies of paraphrase-multilingual-MiniLM-L12-v2 in RAM.

Vectors are L2-normalized at index time → cosine similarity is a pure dot
product at retrieve time. Stored as float32 BLOBs in the `embeddings` table
already declared in memory.py (schema migration adds `is_bot` column).

Privacy note: partitioning is group_id only, matching the rest of line_bot's
architecture. Within a group all members already see all messages, so RAG
does not add a new leak axis. If per-user isolation is later required, add
`user_id` to the WHERE clause in retrieve().
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time as _time
from pathlib import Path

import numpy as np

from config import settings

logger = logging.getLogger(__name__)

MODEL_TAG = "st-paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384

# Threshold calibrated for MiniLM-multilingual on short Chinese conversation.
# General-purpose multilingual MiniLM scores genuinely related Chinese pairs
# at ~0.35-0.50; 0.40 is the practical floor. Empirically tune up if false
# positives are too noisy — expose via settings later.
DEFAULT_THRESHOLD = 0.40
DEFAULT_TOP_K = 3

# Cap injected snippet length to control Gemini prompt token cost.
MAX_SNIPPET_CHARS = 200
MAX_BOT_REPLY_SNIPPET_CHARS = 400  # case anchors deserve a bit more room

# Skip indexing short / placeholder messages (carry no semantic content).
MIN_INDEX_CHARS = 10
PLACEHOLDER_TEXTS = frozenset({
    "[圖片]", "[影片]", "[音訊]", "[語音留言]",
    "[sticker]", "[stickers]", "[檔案]", "[file]",
})

_DB_PATH = Path(settings.sqlite_path)
_lock = threading.Lock()


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else _DB_PATH


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _db_path(db_path),
        isolation_level=None,
        check_same_thread=False,
        factory=_ClosingConnection,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_st_model():
    """Reuse grounding_local's singleton — one model copy in RAM."""
    try:
        from grounding_local import _get_st_model as _g
        return _g()
    except Exception as e:
        logger.warning("embedding_recall: ST model load failed: %s", e)
        return None


def _normalize(arr: np.ndarray) -> np.ndarray:
    """L2-normalize so cosine = dot product downstream."""
    norm = float(np.linalg.norm(arr))
    if norm < 1e-9:
        return arr
    return arr / norm


def embed(text: str) -> np.ndarray | None:
    """Return a (384,) float32 L2-normalized vector, or None if ST unavailable."""
    if not text or not text.strip():
        return None
    model = _get_st_model()
    if model is None:
        return None
    arr = model.encode(text, normalize_embeddings=False)
    arr = np.asarray(arr, dtype=np.float32)
    return _normalize(arr)


def _serialize(arr: np.ndarray) -> bytes:
    return arr.astype(np.float32).tobytes()


def _deserialize(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _should_index(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    if len(text) < MIN_INDEX_CHARS:
        return False
    if text in PLACEHOLDER_TEXTS:
        return False
    return True


def index_message(
    message_id: str,
    group_id: str,
    text: str,
    is_bot: bool = False,
    db_path: Path | str | None = None,
) -> bool:
    """Embed `text` and store. Returns True if a row was written.

    Skips: empty / < MIN_INDEX_CHARS / placeholder strings. Uses INSERT OR
    REPLACE so re-running the backfill or reindexing on model upgrade is
    idempotent (and old tfidf rows with same message_id get overwritten).
    Never raises — failures log and return False so the caller can fire-and-
    forget from a webhook handler.
    """
    if not message_id or not group_id:
        return False
    if not _should_index(text):
        return False
    try:
        vec = embed(text)
        if vec is None:
            return False
        blob = _serialize(vec)
        now = int(_time.time())
        with _lock, _conn(db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO embeddings"
                "(message_id, group_id, text, embedding, backend, dim, "
                " created_at, model_name, is_bot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id, group_id, text.strip(), blob,
                    "sentence-transformers", DIM, now, MODEL_TAG,
                    1 if is_bot else 0,
                ),
            )
        return True
    except Exception as e:
        logger.warning("index_message failed for %s: %s", message_id, e)
        return False


def retrieve(
    group_id: str,
    query: str,
    k: int = DEFAULT_TOP_K,
    threshold: float = DEFAULT_THRESHOLD,
    bot_only: bool = False,
) -> list[dict]:
    """Top-K semantic neighbors. Each hit:
        {"message_id", "text", "score", "is_bot", "created_at"}
    Score is cosine similarity in [-1, 1]. Snippets are truncated to
    MAX_SNIPPET_CHARS for prompt cost control. Returns [] on any failure.
    """
    if not query or not query.strip():
        return []
    q_vec = embed(query)
    if q_vec is None:
        return []
    try:
        with _conn() as c:
            if bot_only:
                rows = c.execute(
                    "SELECT message_id, text, embedding, is_bot, created_at "
                    "FROM embeddings "
                    "WHERE group_id = ? AND model_name = ? AND is_bot = 1",
                    (group_id, MODEL_TAG),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT message_id, text, embedding, is_bot, created_at "
                    "FROM embeddings "
                    "WHERE group_id = ? AND model_name = ?",
                    (group_id, MODEL_TAG),
                ).fetchall()
    except Exception as e:
        logger.warning("retrieve query failed: %s", e)
        return []
    if not rows:
        return []
    try:
        mat = np.stack([_deserialize(r[2]) for r in rows])  # (N, 384)
    except Exception as e:
        logger.warning("retrieve deserialize failed: %s", e)
        return []
    scores = mat @ q_vec  # both already L2-normalized → cosine
    # argsort descending, then threshold + truncate
    order = np.argsort(-scores)
    out: list[dict] = []
    for i in order:
        score = float(scores[int(i)])
        if score < threshold:
            break  # sorted desc, rest are lower
        text = rows[int(i)][1]
        if len(text) > MAX_SNIPPET_CHARS:
            text = text[:MAX_SNIPPET_CHARS] + "…"
        out.append({
            "message_id": rows[int(i)][0],
            "text": text,
            "score": score,
            "is_bot": bool(rows[int(i)][3]),
            "created_at": int(rows[int(i)][4]) if rows[int(i)][4] is not None else 0,
        })
        if len(out) >= k:
            break
    return out


def retrieve_case_pairs(
    group_id: str,
    query: str,
    k: int = DEFAULT_TOP_K,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict]:
    """Find similar past user queries and pair each with the bot reply that
    immediately followed in the same group. Returns:
        [{"user_query", "bot_reply", "score", "created_at"}, ...]
    Designed for the news/case rule pack: gives Gemini concrete examples of
    how 咪寶 has answered similar questions before.

    Why retrieve by user query (not bot reply): the bot's wording is often
    derivative of the user's framing; semantic match on the *question* is the
    sharper signal for "is this the same kind of ask".
    """
    if not query or not query.strip():
        return []
    # Pull more candidates than k since we'll filter by "has bot reply".
    candidates = retrieve(
        group_id, query, k=k * 3, threshold=threshold, bot_only=False,
    )
    user_only = [c for c in candidates if not c["is_bot"]]
    if not user_only:
        return []
    out: list[dict] = []
    try:
        with _conn() as c:
            for cand in user_only:
                row = c.execute(
                    "SELECT text, created_at FROM raw_messages "
                    "WHERE group_id = ? AND user_id = '__bot__' "
                    "  AND created_at >= ("
                    "    SELECT created_at FROM raw_messages "
                    "    WHERE group_id = ? AND message_id = ? LIMIT 1"
                    "  ) "
                    "ORDER BY created_at ASC LIMIT 1",
                    (group_id, group_id, cand["message_id"]),
                ).fetchone()
                if not row:
                    continue
                bot_text = (row[0] or "").strip()
                if not bot_text or bot_text in PLACEHOLDER_TEXTS:
                    continue
                if len(bot_text) < MIN_INDEX_CHARS:
                    continue
                if len(bot_text) > MAX_BOT_REPLY_SNIPPET_CHARS:
                    bot_text = bot_text[:MAX_BOT_REPLY_SNIPPET_CHARS] + "…"
                out.append({
                    "user_query": cand["text"],
                    "bot_reply": bot_text,
                    "score": cand["score"],
                    "created_at": cand["created_at"],
                })
                if len(out) >= k:
                    break
    except Exception as e:
        logger.warning("retrieve_case_pairs join failed: %s", e)
        return []
    return out


def _relative_time(epoch: int) -> str:
    """Render epoch as a coarse '3 天前 / 上週 / 2 小時前' string for prompt."""
    if not epoch:
        return ""
    now = int(_time.time())
    delta = now - epoch
    if delta < 60:
        return "剛剛"
    if delta < 3600:
        return f"{delta // 60} 分鐘前"
    if delta < 86400:
        return f"{delta // 3600} 小時前"
    if delta < 7 * 86400:
        return f"{delta // 86400} 天前"
    return f"{delta // 86400} 天前"


def format_recall_block(hits: list[dict]) -> str:
    """Render general recall hits as a system-instruction block. Empty in → empty out."""
    if not hits:
        return ""
    lines = ["【相關歷史對話 — 供你回想群組脈絡用，但不要硬塞進回覆】"]
    for h in hits:
        role = "咪寶之前" if h.get("is_bot") else "群組之前有人說"
        when = _relative_time(h.get("created_at", 0))
        prefix = f"{role}（{when}）" if when else role
        lines.append(f"  - {prefix}：{h['text']}")
    return "\n".join(lines)


def format_case_block(pairs: list[dict]) -> str:
    """Render case anchor pairs as a system-instruction block."""
    if not pairs:
        return ""
    lines = ["【類似 case 過去咪寶這樣回 — 風格錨點，不要逐字抄】"]
    for i, p in enumerate(pairs, 1):
        when = _relative_time(p.get("created_at", 0))
        lines.append(f"  --- case {i}（{when}） ---")
        lines.append(f"  user 當時問：{p['user_query']}")
        lines.append(f"  咪寶當時答：{p['bot_reply']}")
    return "\n".join(lines)
