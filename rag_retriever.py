"""Local RAG retriever for the LINE bot — 純本機，零雲端 embedding。

Pipeline:
    raw_messages (line_bot.db)
      -> embed (sentence-transformers → TF-IDF fallback)
      -> embeddings table (SQLite BLOB storage)
      -> retrieve(query) cosine-similarity scan in numpy

Design notes / constraints:

1. **No new services.** All storage stays in the existing `line_bot.db`
   (SQLite). No pgvector, no external vector DB, no FAISS / Annoy index.
2. **Local-only embedding.** Primary backend is local
   sentence-transformers (`paraphrase-multilingual-MiniLM-L12-v2`,
   384 dims, multilingual incl. Chinese). No cloud HF Inference API.
3. **TF-IDF fallback only.** If torch / sentence-transformers is not
   importable, we fall back to a TF-IDF vectorizer fitted on the corpus
   (`sklearn.feature_extraction.text.TfidfVectorizer`). Both backends
   share the bytes-in/bytes-out contract and `retrieve()` works via
   cosine similarity.
4. **Lazy imports.** Heavy deps (torch, sentence_transformers, sklearn)
   are imported only on first use, so module import stays cheap
   (millisecond) for `lite_reply` on the hot path.
5. **Pure-python scan.** No sqlite-vec / sqlite-vss extension. We pull
   all embeddings into numpy and dot-product. With ~1k–10k messages
   this is fine; revisit if corpus grows past ~50k.

Backfill is one-shot (idempotent via `INSERT OR IGNORE`). Re-running
only embeds rows that aren't already in the `embeddings` table.

Public surface:
    backfill_embeddings(batch_size=100, max_records=None) -> (processed, total)
    embed_one(text: str) -> bytes
    retrieve(query, k=5, group_id=None, min_similarity=0.5) -> list[dict]
    format_rag_response(query, hits) -> str | None
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("LINE_BOT_DB", str(_HERE / "line_bot.db"))

# Local sentence-transformers model. Multilingual MiniLM is the lightest
# multilingual model that handles Chinese reasonably; it loads fast and
# fits well on a 32GB Mac that's also running 14B LLMs.
ST_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
ST_FULL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
ST_DIM = 384

# Module-level singletons (populated on first use).
_st_model: Any = None
_tfidf_vectorizer: Any = None
_backend: str | None = None  # "st" | "tfidf"

# Track the model_name + dim attached to the most recent _encode call so
# backfill / retrieve can persist + filter on it.
_last_model_name: str | None = None
_last_dim: int | None = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    message_id TEXT PRIMARY KEY,
    group_id   TEXT NOT NULL,
    text       TEXT NOT NULL,
    embedding  BLOB NOT NULL,
    backend    TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    model_name TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_embeddings_group ON embeddings(group_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_model
    ON embeddings(group_id, model_name);
"""


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.executescript(_SCHEMA_SQL)
    # Migrate older DBs that pre-date the model_name column.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(embeddings)").fetchall()]
    if "model_name" not in cols:
        conn.execute(
            "ALTER TABLE embeddings ADD COLUMN model_name TEXT NOT NULL DEFAULT ''"
        )
    return conn


# ---------------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------------


def _try_load_sentence_transformer() -> Any | None:
    """Try to import + load the multilingual ST model. Return None on fail."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as e:
        logger.info("sentence-transformers unavailable: %s", e)
        return None
    try:
        # CPU only; this is a small model so no MPS/CUDA needed.
        model = SentenceTransformer(ST_MODEL_NAME, device="cpu")
        return model
    except Exception as e:
        logger.warning("Failed to load %s: %s", ST_MODEL_NAME, e)
        return None


def _build_tfidf_from_corpus(conn: sqlite3.Connection) -> Any:
    """Fit a TfidfVectorizer on all raw_messages text.

    Used as the fallback backend when sentence-transformers / torch
    install isn't available. Uses char n-grams so it works for Chinese
    without needing a tokenizer.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore

    cur = conn.execute(
        "SELECT text FROM raw_messages WHERE text IS NOT NULL AND length(text) > 0"
    )
    docs = [row[0] for row in cur.fetchall()]
    if not docs:
        # Avoid empty-vocab failure: seed with single dummy doc.
        docs = ["占位符"]
    vec = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        max_features=20000,
        lowercase=True,
    )
    vec.fit(docs)
    return vec


def _get_embedder() -> tuple[str, Any]:
    """Lazy-init the embedder. Returns (backend_name, embedder_object).

    backend_name is "st" or "tfidf". Embedder is either an
    SentenceTransformer model or a fitted TfidfVectorizer.
    """
    global _st_model, _tfidf_vectorizer, _backend
    if _backend is not None:
        if _backend == "st":
            return _backend, _st_model
        return _backend, _tfidf_vectorizer

    model = _try_load_sentence_transformer()
    if model is not None:
        _st_model = model
        _backend = "st"
        logger.info("RAG backend: sentence-transformers (%s)", ST_MODEL_NAME)
        return _backend, _st_model

    # Fallback: TF-IDF.
    conn = _connect()
    try:
        vec = _build_tfidf_from_corpus(conn)
    finally:
        conn.close()
    _tfidf_vectorizer = vec
    _backend = "tfidf"
    logger.info("RAG backend: TF-IDF fallback (no torch / ST)")
    return _backend, _tfidf_vectorizer


def _l2_normalize(arr: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def _encode_local(texts: list[str]) -> tuple[str, np.ndarray, str, int]:
    """Local-only encode.

    Returns (backend_tag, normalized_array, model_name, dim).
    """
    backend, embedder = _get_embedder()
    if backend == "st":
        arr = embedder.encode(
            texts,
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        out = arr.astype(np.float32, copy=False)
        return backend, out, ST_FULL_NAME, int(out.shape[1])
    # tfidf
    sparse = embedder.transform(texts)
    arr = sparse.toarray().astype(np.float32, copy=False)
    norm = _l2_normalize(arr)
    return backend, norm, "tfidf-char-2-4", int(norm.shape[1])


def _encode(texts: list[str]) -> np.ndarray:
    """Encode batch of texts -> 2D float32 numpy array (rows = texts).

    Backwards-compat shim: returns just the array. The backend tag,
    model_name, and dim of the last call are recorded in module globals
    `_backend`, `_last_model_name`, `_last_dim` for callers that need
    them (backfill, retrieve).
    """
    global _backend, _last_model_name, _last_dim
    backend, arr, model_name, dim = _encode_local(texts)
    _backend = backend
    _last_model_name = model_name
    _last_dim = dim
    return arr


def embed_one(text: str) -> bytes:
    """Encode a single text and return float32 bytes (for SQLite BLOB)."""
    arr = _encode([text])[0]
    return arr.tobytes()


def _bytes_to_vec(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def backfill_embeddings(
    batch_size: int = 100,
    max_records: int | None = None,
    db_path: str | None = None,
    overwrite: bool = False,
    target_model: str | None = None,
) -> tuple[int, int]:
    """Embed every `raw_messages` row not already in `embeddings`.

    Args:
        batch_size: Encode this many texts per forward pass.
        max_records: Stop after embedding this many new rows (None = all).
        db_path: Override DB path (used by tests).
        overwrite: If True, also re-embed rows whose stored ``model_name``
            differs from ``target_model`` (or every row when no target).
        target_model: Model to migrate TO when ``overwrite=True``. Only
            rows whose stored ``model_name`` differs are re-embedded.

    Returns:
        (processed, total) where ``processed`` is the number of rows
        newly embedded or re-embedded in this call and ``total`` is the
        count of rows in ``embeddings`` after this call.
    """
    conn = _connect(db_path)
    cur = conn.cursor()

    if overwrite and target_model:
        cur.execute(
            """
            SELECT r.message_id, r.group_id, r.text, r.created_at
            FROM raw_messages r
            LEFT JOIN embeddings e USING (message_id)
            WHERE r.text IS NOT NULL AND length(r.text) > 0
              AND (e.model_name IS NULL OR e.model_name = ''
                   OR e.model_name != ?)
            ORDER BY r.created_at
            """,
            (target_model,),
        )
    elif overwrite:
        cur.execute(
            """
            SELECT r.message_id, r.group_id, r.text, r.created_at
            FROM raw_messages r
            WHERE r.text IS NOT NULL AND length(r.text) > 0
            ORDER BY r.created_at
            """
        )
    else:
        cur.execute(
            """
            SELECT r.message_id, r.group_id, r.text, r.created_at
            FROM raw_messages r
            LEFT JOIN embeddings e USING (message_id)
            WHERE e.message_id IS NULL
              AND r.text IS NOT NULL
              AND length(r.text) > 0
            ORDER BY r.created_at
            """
        )

    pending = cur.fetchall()
    if max_records is not None:
        pending = pending[:max_records]

    processed = 0
    for i in range(0, len(pending), batch_size):
        chunk = pending[i : i + batch_size]
        texts = [row[2] for row in chunk]
        backend, vectors, model_name, dim = _encode_local(texts)
        rows: list[tuple[Any, ...]] = []
        for (mid, gid, text, created_at), vec in zip(chunk, vectors):
            rows.append(
                (
                    mid,
                    gid,
                    text,
                    vec.astype(np.float32).tobytes(),
                    backend,
                    dim,
                    created_at,
                    model_name,
                )
            )
        if overwrite:
            cur.executemany(
                """
                INSERT INTO embeddings
                    (message_id, group_id, text, embedding,
                     backend, dim, created_at, model_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    embedding=excluded.embedding,
                    backend=excluded.backend,
                    dim=excluded.dim,
                    model_name=excluded.model_name
                """,
                rows,
            )
        else:
            cur.executemany(
                """
                INSERT OR IGNORE INTO embeddings
                    (message_id, group_id, text, embedding,
                     backend, dim, created_at, model_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        conn.commit()
        processed += len(rows)

    cur.execute("SELECT COUNT(*) FROM embeddings")
    total = cur.fetchone()[0]
    conn.close()
    return processed, total


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def retrieve(
    query: str,
    k: int = 5,
    group_id: str | None = None,
    min_similarity: float = 0.5,
    db_path: str | None = None,
) -> list[dict]:
    """Return up to `k` most-similar messages whose cosine sim >= min_similarity.

    Pipeline: pure local sentence-transformers (or TF-IDF fallback) +
    cosine similarity scan over the stored corpus. No reranker, no
    HyDE / multi-hop, no cloud.

    Each hit is a dict: {message_id, text, similarity, created_at,
    group_id}.
    """
    if not query or not query.strip():
        return []

    # 2026-05-09 整合：優先試 hybrid_retrieve（BM25 + dense + cross-encoder rerank）
    # 純本機，無雲端 API。失敗 graceful 退回原 cosine-only 路徑。
    if os.environ.get("RAG_HYBRID_DISABLED") != "1":
        try:
            import hybrid_retrieve  # local module
            hybrid_hits = hybrid_retrieve.hybrid_search(
                query, k=k * 3,  # 抓多一點給 reranker
                alpha=0.5,
                group_id=group_id,
                db_path=db_path or DB_PATH,
            )
            if hybrid_hits:
                # Cross-encoder rerank
                try:
                    import local_reranker
                    candidates = [h.get("text", "") for h in hybrid_hits]
                    ranked = local_reranker.rerank(query, candidates, top_k=k)
                    text_to_hit = {h.get("text", ""): h for h in hybrid_hits}
                    out = []
                    for text, _score in ranked:
                        h = text_to_hit.get(text)
                        if h and h.get("similarity", 0) >= min_similarity * 0.5:
                            out.append(h)
                    if out:
                        return out[:k]
                except (ImportError, Exception) as e:
                    logger.info("local_reranker not available, use hybrid order: %s", e)
                # 沒 rerank → 直接用 hybrid 結果 top-k
                return [h for h in hybrid_hits[:k] if h.get("similarity", 0) >= min_similarity * 0.5]
        except ImportError:
            pass
        except Exception as e:
            logger.warning("hybrid_retrieve failed, fallback cosine: %s", e)

    # Embed the query locally. We pull back the ACTUAL model_name + dim
    # so we can filter the stored corpus to rows produced by the same
    # model. Mixing dims across models in one matrix scan would crash.
    backend, qmat, model_name, dim_q = _encode_local([query])
    qvec = qmat[0]

    conn = _connect(db_path)
    cur = conn.cursor()

    # Prefer model_name match. Old rows (pre-migration, model_name='')
    # were produced before we tracked it; fall back to (backend, dim)
    # match for them so we don't lose the historical corpus.
    if group_id:
        cur.execute(
            "SELECT message_id, group_id, text, embedding, dim, created_at, "
            "       model_name "
            "FROM embeddings "
            "WHERE group_id = ? AND ("
            "  model_name = ? "
            "  OR (model_name = '' AND backend = ? AND dim = ?))",
            (group_id, model_name, backend, dim_q),
        )
    else:
        cur.execute(
            "SELECT message_id, group_id, text, embedding, dim, created_at, "
            "       model_name "
            "FROM embeddings "
            "WHERE model_name = ? "
            "   OR (model_name = '' AND backend = ? AND dim = ?)",
            (model_name, backend, dim_q),
        )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return []

    dims = {row[4] for row in rows}
    if len(dims) != 1:
        # Mixed-dim corpus shouldn't happen once model_name filter is on,
        # but guard so we never compute on a ragged matrix.
        logger.warning("retrieve: mixed dims in embeddings (%s); skipping", dims)
        return []
    dim = dims.pop()
    if dim != dim_q:
        logger.warning(
            "retrieve: query dim %d != stored dim %d (model %r); skipping",
            dim_q,
            dim,
            model_name,
        )
        return []

    matrix = np.stack([_bytes_to_vec(row[3], dim) for row in rows]).astype(np.float32)

    # Rows are stored normalized; dot = cosine similarity.
    sims = matrix @ qvec
    if sims.size == 0:
        return []

    k_target = max(k, 1)
    # Top-k by cosine similarity, filtered by min_similarity.
    top_idx = np.argsort(-sims)[:k_target]
    hits: list[dict[str, Any]] = []
    for idx in top_idx:
        sim = float(sims[idx])
        if sim < min_similarity:
            continue
        mid, gid, text, _blob, _dim, created_at, _model = rows[int(idx)]
        hits.append(
            {
                "message_id": mid,
                "group_id": gid,
                "text": text,
                "similarity": sim,
                "created_at": created_at,
            }
        )
    return hits


def format_rag_response(query: str, hits: list[dict]) -> str | None:
    """Wrap top-k similar messages into a casual reply. None if no hits."""
    if not hits:
        return None
    lines = ["我之前看過類似的訊息："]
    for h in hits[:3]:
        snippet = (h["text"] or "").replace("\n", " ").strip()
        if len(snippet) > 60:
            snippet = snippet[:60] + "…"
        lines.append(f"・「{snippet}」(相似度 {h['similarity']:.2f})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def _smoke() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(f"DB: {DB_PATH}")
    backend, _ = _get_embedder()
    print(f"backend: {backend}")

    t0 = time.time()
    processed, total = backfill_embeddings(batch_size=32, max_records=100)
    dt = time.time() - t0
    print(f"backfill: processed={processed} total_in_table={total} elapsed={dt:.2f}s")

    for q in ["股票", "天氣", "拜託幫我"]:
        print(f"\n--- query: {q!r} ---")
        hits = retrieve(q, k=3, min_similarity=0.0)  # show even low-sim for inspection
        if not hits:
            print("  (no hits)")
            continue
        for h in hits:
            text = h["text"].replace("\n", " ")
            if len(text) > 80:
                text = text[:80] + "…"
            print(f"  sim={h['similarity']:.3f} | {text}")
        formatted = format_rag_response(q, hits)
        if formatted:
            print("formatted reply:")
            for line in formatted.splitlines():
                print(f"  {line}")


if __name__ == "__main__":
    _smoke()
