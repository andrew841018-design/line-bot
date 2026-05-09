"""
local_reranker.py — 100% 本機 cross-encoder reranker。

用 ``sentence_transformers.CrossEncoder`` 取代雲端 Cohere/Jina rerank API。
Model 第一次會從 HuggingFace 下載到本機 cache（~/.cache/huggingface/）；
之後永遠純本機跑、不打雲端。

匹配 ``rag_retriever`` 既有 ``reranker`` module 介面：
    rerank(query, candidates, top_k=3) -> list[(text, score)]

設計守則
--------
1. **Lazy load**：模組 import 階段不載 model，避免啟動成本（模型 80~280MB，
   load 上 GPU/MPS 需要 1~3 秒）。第一次 ``rerank()`` 才觸發 load。
2. **Graceful degrade**：CrossEncoder load 失敗 / predict 失敗 → 回原 ranking
   ``[(c, 0.0) for c in candidates[:top_k]]``，確保 caller (rag_retriever) 不會
   因為 reranker 爆掉就完全失去 RAG 結果。
3. **Batch chunk**：候選 > ``_BATCH_THRESHOLD`` 時切片跑（避免 MPS/CPU 記憶體
   爆掉）。每批跑完 score 拼回。
4. **無雲端 call**：完全沒有 ``requests`` / API key 邏輯。Model 走 HuggingFace
   transformers 的 cache，下載一次後純離線。
"""

from __future__ import annotations

import logging
import threading
from typing import List, Tuple

logger = logging.getLogger(__name__)

# 主模型：MS-MARCO MiniLM L-6（~80 MB）。多語可用，英中混合 query 表現夠。
_PRIMARY_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# 退路模型：BGE reranker base（~280 MB）。中文最強，但較大。
_FALLBACK_MODEL = "BAAI/bge-reranker-base"

# 候選 > 此值就 chunk 跑，避免一次塞太多 pair 給 cross-encoder。
_BATCH_THRESHOLD = 100
# 每批最多幾筆送 predict（cross-encoder 有自己的內部 batch_size，這層只是
# 把巨量輸入切塊餵進去）。
_CHUNK_SIZE = 64

# Lazy-load 的全域 model handle 與 lock。lock 防止多 thread 同時觸發 load。
_model = None  # type: ignore[var-annotated]
_model_load_failed = False
_load_lock = threading.Lock()


def _get_model():  # pragma: no cover - I/O heavy, exercised by integration smoke
    """Lazy load CrossEncoder. 失敗會記錄並回 None（caller fallback 用）。"""
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed:
        return None

    with _load_lock:
        # Double-check 進 lock 後是否已經被別 thread 載完。
        if _model is not None:
            return _model
        if _model_load_failed:
            return None

        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            logger.warning(
                "sentence_transformers 不可用，local rerank 停用：%s", e
            )
            _model_load_failed = True
            return None

        for model_name in (_PRIMARY_MODEL, _FALLBACK_MODEL):
            try:
                logger.info("local_reranker: loading CrossEncoder %s", model_name)
                _model = CrossEncoder(model_name)
                logger.info("local_reranker: %s ready", model_name)
                return _model
            except Exception as e:
                logger.warning(
                    "local_reranker: load %s failed: %s", model_name, e
                )

        # 兩個 model 都 load 不起來。
        _model_load_failed = True
        return None


def _chunked_predict(model, pairs: List[Tuple[str, str]]) -> List[float]:
    """把 pairs 切成 _CHUNK_SIZE 批跑 predict，把結果串起來。"""
    if len(pairs) <= _BATCH_THRESHOLD:
        return [float(s) for s in model.predict(pairs)]

    scores: List[float] = []
    for i in range(0, len(pairs), _CHUNK_SIZE):
        chunk = pairs[i : i + _CHUNK_SIZE]
        chunk_scores = model.predict(chunk)
        scores.extend(float(s) for s in chunk_scores)
    return scores


def _fallback_ranking(
    candidates: List[str], top_k: int
) -> List[Tuple[str, float]]:
    """Reranker 失敗時的退路：保持原順序、score 給 0.0。"""
    if not candidates:
        return []
    cleaned = [c for c in candidates if c and c.strip()]
    return [(c, 0.0) for c in cleaned[: max(0, top_k)]]


def rerank(
    query: str,
    candidates: List[str],
    top_k: int = 3,
) -> List[Tuple[str, float]]:
    """Rerank candidates against query with a local cross-encoder.

    Parameters
    ----------
    query : str
        The user query text.
    candidates : list[str]
        Candidate texts to rerank (e.g. hybrid retrieve top-N pool).
    top_k : int
        How many top results to return. Default 3.

    Returns
    -------
    list[(text, score)]
        Top-k candidates by cross-encoder relevance score (higher = better).
        Returns ``[]`` if candidates is empty. Falls back to original
        ordering with score 0.0 if model load / predict fails so the
        caller never loses RAG entirely.
    """
    if not candidates:
        return []
    if not query or not query.strip():
        return _fallback_ranking(candidates, top_k)

    # 去掉空白 / None 候選。Caller 已經 dedup 過，這裡不再 dedup。
    cleaned = [c for c in candidates if c and c.strip()]
    if not cleaned:
        return []
    if top_k <= 0:
        return []

    model = _get_model()
    if model is None:
        return _fallback_ranking(cleaned, top_k)

    pairs: List[Tuple[str, str]] = [(query, c) for c in cleaned]

    try:
        scores = _chunked_predict(model, pairs)
    except Exception as e:
        logger.warning(
            "local_reranker: predict failed (%s), fallback to original order", e
        )
        return _fallback_ranking(cleaned, top_k)

    if len(scores) != len(cleaned):
        # 不太可能，但保險：對不上就 fallback。
        logger.warning(
            "local_reranker: score length %d != candidate length %d",
            len(scores),
            len(cleaned),
        )
        return _fallback_ranking(cleaned, top_k)

    ranked = sorted(
        zip(cleaned, scores), key=lambda x: x[1], reverse=True
    )
    return ranked[:top_k]


__all__ = ["rerank"]
