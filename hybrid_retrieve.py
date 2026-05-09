"""Hybrid (dense + sparse) retrieval for the LINE bot RAG.

Combines:
* Dense top-N from `rag_retriever` (cosine over MiniLM / bge-m3 / TF-IDF
  embeddings stored in SQLite).
* Sparse top-N from `bm25_index.BM25Index` over the same corpus.

Fusion is a weighted sum on z-score-normalized scores:

    final = alpha * z(dense) + (1 - alpha) * z(bm25)

with `alpha=0.5` as a balanced default. Z-score normalization is applied
PER QUERY, PER MODALITY: each modality's pool is mapped to mean=0 / std=1
before mixing, so a high-magnitude raw cosine doesn't drown a perfectly
good (but smaller-magnitude) BM25 score and vice versa.

This module is 100% local. It does NOT call any cloud API. The dense
side reuses `rag_retriever`'s existing local pipeline (which itself
falls back gracefully when cloud is unreachable).

Why hybrid:
* Dense embeddings are smooth — they catch paraphrase / semantic match
  but miss exact rare-token hits ("BRK.B", a Chinese name, an
  obscure acronym).
* BM25 is the inverse — exact terms shine, paraphrase falls flat.
* Weighted z-score fusion is the simplest move that consistently beats
  either alone (see Lin et al., "A Replication Study of Dense Passage
  Retrieval" 2021 §4.3 for a deeper take). We keep it elementary so
  it's fast to debug and easy to tune.

Public surface:
    hybrid_search(query, k=3, alpha=0.5, ...) -> list[dict]

Hit shape (mirrors rag_retriever.retrieve):
    {
      "message_id": str,
      "group_id":  str,
      "text":      str,
      "similarity": float,    # raw dense cosine (or 0.0 if not in dense pool)
      "bm25_score": float,    # raw BM25 score (or 0.0 if not in sparse pool)
      "fused_score": float,   # alpha * z(dense) + (1-alpha) * z(bm25)
      "created_at": int,
    }

`hybrid_search` is built so that:
    alpha == 1.0  -> behaves like pure dense (BM25 weight zero)
    alpha == 0.0  -> behaves like pure BM25 (dense weight zero)

…this lets the caller A/B easily and lets tests assert each end of the
spectrum without mocking either side.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from bm25_index import BM25Index

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_DEFAULT_DB = os.environ.get("LINE_BOT_DB", str(_HERE / "line_bot.db"))


# ── normalization helpers ──────────────────────────────────────────────────


def _zscore(scores: dict[Any, float]) -> dict[Any, float]:
    """Map {key: raw_score} -> {key: z_score}.

    Empty / single-element dicts return all zeros (no signal).
    Constant-score dicts (std == 0) also return all zeros — there's
    no meaningful ordering to preserve, so contributing 0 to fusion
    is the right call.
    """
    if not scores:
        return {}
    vals = np.array(list(scores.values()), dtype=np.float64)
    if vals.size <= 1:
        return {k: 0.0 for k in scores}
    mu = float(vals.mean())
    sigma = float(vals.std())
    if sigma == 0.0:
        return {k: 0.0 for k in scores}
    return {k: (v - mu) / sigma for k, v in scores.items()}


# ── dense pool fetch ───────────────────────────────────────────────────────


def _fetch_corpus(
    db_path: str,
    group_id: str | None = None,
) -> list[dict]:
    """Pull every (message_id, group_id, text, created_at) row.

    Returns rows in DB-order (no ranking). Used to align BM25 indices
    with the dense pool — the BM25 index is built over `corpus[i].text`
    so retrieval index `i` maps 1:1 back to a row.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        if group_id:
            cur.execute(
                "SELECT message_id, group_id, text, created_at "
                "FROM embeddings WHERE group_id = ? ORDER BY created_at",
                (group_id,),
            )
        else:
            cur.execute(
                "SELECT message_id, group_id, text, created_at "
                "FROM embeddings ORDER BY created_at"
            )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "message_id": r[0],
            "group_id": r[1],
            "text": r[2],
            "created_at": r[3],
        }
        for r in rows
    ]


def _dense_pool(
    query: str,
    group_id: str | None,
    pool_size: int,
    db_path: str,
) -> list[dict]:
    """Delegate to rag_retriever.retrieve for the dense top-N.

    We import rag_retriever lazily so this module remains testable
    without the full embedding stack — tests stub `_dense_pool` instead.

    `min_similarity=0.0` forces rag_retriever to return its full pool
    (we re-rank ourselves after fusion).
    """
    import rag_retriever  # lazy import — heavy embedding deps live there

    return rag_retriever.retrieve(
        query,
        k=pool_size,
        group_id=group_id,
        min_similarity=0.0,
        db_path=db_path,
    )


# ── public API ─────────────────────────────────────────────────────────────


def hybrid_search(
    query: str,
    k: int = 3,
    alpha: float = 0.5,
    *,
    group_id: str | None = None,
    pool_size: int = 20,
    db_path: str | None = None,
    corpus: list[dict] | None = None,
    dense_pool: list[dict] | None = None,
    bm25_index: BM25Index | None = None,
) -> list[dict]:
    """Hybrid (dense + sparse) retrieval.

    Args:
        query: Natural-language query string.
        k: Number of final hits to return.
        alpha: Dense weight in [0, 1]. 0 = pure BM25, 1 = pure dense.
        group_id: Optional group-scoping. Forwarded to both pools.
        pool_size: Top-N pulled from each modality before fusion. Bigger
            pool = better recall, slower. 20 is a fine default for our
            corpus size (low thousands).
        db_path: Override the SQLite path (used in tests).
        corpus: Optional pre-loaded corpus rows (for tests/perf). Each
            row must have message_id / group_id / text / created_at.
        dense_pool: Optional pre-computed dense top-N (for tests). Each
            element must have message_id, text, similarity (cosine).
        bm25_index: Optional pre-built BM25Index aligned with `corpus`.
            If not given, we build one on the fly from `corpus`.

    Returns:
        Up to `k` hit dicts ordered by `fused_score` desc.
    """
    if not query or not query.strip():
        return []
    if k <= 0:
        return []
    alpha = max(0.0, min(1.0, float(alpha)))

    db_path = db_path or _DEFAULT_DB

    # 1. Get the corpus (for BM25). Caller can pass it in to avoid
    #    re-querying SQLite on every call.
    if corpus is None:
        corpus = _fetch_corpus(db_path, group_id=group_id)
    if not corpus:
        return []

    # message_id -> corpus row
    by_id: dict[str, dict] = {row["message_id"]: row for row in corpus}

    # 2. Dense pool. We do NOT short-circuit when alpha==0 because the
    #    dense path is also the canonical source of message-level
    #    metadata (created_at etc) — but we DO skip the heavy embed
    #    call and treat all dense scores as 0 in that case.
    if alpha > 0.0:
        if dense_pool is None:
            try:
                dense_pool = _dense_pool(query, group_id, pool_size, db_path)
            except Exception as e:
                logger.warning("dense retrieval failed (%s); falling back to BM25-only", e)
                dense_pool = []
    else:
        dense_pool = []

    dense_scores: dict[str, float] = {}
    for hit in dense_pool or []:
        mid = hit.get("message_id")
        if mid is None:
            continue
        dense_scores[mid] = float(hit.get("similarity", 0.0))
        # Refresh by_id with dense-side metadata when corpus didn't have it
        # (tests that pass dense_pool but no corpus rely on this path).
        by_id.setdefault(mid, hit)

    # 3. Sparse pool via BM25.
    if alpha < 1.0:
        if bm25_index is None:
            bm25_index = BM25Index()
            bm25_index.build([row["text"] or "" for row in corpus])
        # Use a generous pool — argpartition under the hood is cheap.
        sparse_top = bm25_index.query(query, k=max(pool_size, k))
    else:
        sparse_top = []

    sparse_scores: dict[str, float] = {}
    for idx, score in sparse_top:
        if 0 <= idx < len(corpus):
            mid = corpus[idx]["message_id"]
            sparse_scores[mid] = float(score)

    # 4. Z-score normalize each modality's pool (per query).
    z_dense = _zscore(dense_scores)
    z_bm25 = _zscore(sparse_scores)

    # 5. Fuse. Union of doc ids that appeared in either pool.
    all_ids = set(z_dense) | set(z_bm25)
    if not all_ids:
        return []

    fused: list[dict[str, Any]] = []
    for mid in all_ids:
        zd = z_dense.get(mid, 0.0)
        zb = z_bm25.get(mid, 0.0)
        fused_score = alpha * zd + (1.0 - alpha) * zb

        meta = by_id.get(mid, {})
        fused.append(
            {
                "message_id": mid,
                "group_id": meta.get("group_id", ""),
                "text": meta.get("text", ""),
                "similarity": dense_scores.get(mid, 0.0),
                "bm25_score": sparse_scores.get(mid, 0.0),
                "fused_score": float(fused_score),
                "created_at": meta.get("created_at", 0),
            }
        )

    fused.sort(key=lambda h: h["fused_score"], reverse=True)
    return fused[:k]


# ── manual demo ────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover — demo only
    # Showcase: 5 fake docs, query "今天天氣如何", weighted fusion.
    fake_corpus = [
        {"message_id": "1", "group_id": "G", "text": "今天天氣很好,適合出去玩", "created_at": 1},
        {"message_id": "2", "group_id": "G", "text": "明天可能會下雨,記得帶傘", "created_at": 2},
        {"message_id": "3", "group_id": "G", "text": "AAPL 股價今天上漲 2%", "created_at": 3},
        {"message_id": "4", "group_id": "G", "text": "週末家庭聚餐在內湖餐廳", "created_at": 4},
        {"message_id": "5", "group_id": "G", "text": "提醒下週一要去看牙醫", "created_at": 5},
    ]
    # No dense pool -> alpha=0 = pure BM25
    print("=== alpha=0.0 (pure BM25) ===")
    for h in hybrid_search("今天天氣如何", k=3, alpha=0.0, corpus=fake_corpus, dense_pool=[]):
        print(f"  fused={h['fused_score']:+.3f} bm25={h['bm25_score']:.3f}  {h['text']}")
