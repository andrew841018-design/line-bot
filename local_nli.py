"""
local_nli — 100% 本機 NLI / entailment 驗證

設計目的：取代 HF Inference API 的雲端 NLI。下載一次 model（HuggingFace
cache）後，後續呼叫永遠走本機 transformers pipeline，不打雲端 API。

三層 fallback（任何一層 raise 都不阻塞 caller）：

    1. transformers `pipeline("zero-shot-classification")`
       - 主：MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli (~440MB, 多語可用)
       - 退：MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7
              (~560MB, 繁中強)
    2. sentence-transformers cosine similarity
       - 用 rag_retriever 已經載過的 paraphrase-multilingual-MiniLM-L12-v2
       - 把 cosine 當 entailment proxy（相似度 ≈ 是否被支持）
    3. 完全失敗 → 回傳 1.0（"通過"），等同關閉 grounding check
       — 不阻塞使用者拿到答案。

兩個對外 entry point：
    - verify_claim(claim, source) -> float    [0, 1] entailment 機率
    - score_response(response, sources) -> dict
        {"score_avg": float,
         "low_score_claims": [(claim, score), ...],
         "verified": [(claim, score), ...],
         "backend": str}

Lazy-load：所有 heavy import（transformers / torch / sentence_transformers）
都在第一次呼叫 `verify_claim` / `score_response` 時才發生，import 此 module
本身不會載 model。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ── Model 設定 ────────────────────────────────────────────────────────────
PRIMARY_NLI_MODEL = "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"
FALLBACK_NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"
ST_FALLBACK_MODEL = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

# zero-shot-classification 的兩條候選 label：「被支持」vs「不被支持」
# pipeline 回傳機率分布，我們只取 "entailment" 的分。
ENTAILMENT_LABEL = "entailment"
CONTRADICTION_LABEL = "contradiction"
CANDIDATE_LABELS = [ENTAILMENT_LABEL, CONTRADICTION_LABEL]

# 切句正則：句號 / 問號 / 驚嘆號 / 換行（中英都吃）。
# 用 re.split 保留結尾標點不會生出空字串太多。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。．！？!?\.])\s*|\n+")

# 最少句子長度（過濾「好。」「OK」這種沒內容的）
# 中文一字算 1 char，所以設 4：「不知道。」會被擋，「明天去台北」(5) 會留。
_MIN_CLAIM_CHARS = 4

# 三種 backend tag（debug / observability 用）
BACKEND_NLI_PRIMARY = "nli_primary"
BACKEND_NLI_FALLBACK = "nli_fallback"
BACKEND_COSINE = "cosine"
BACKEND_NOOP = "noop"

# ── 內部 lazy state ────────────────────────────────────────────────────────
_pipeline_lock = threading.Lock()
_pipeline: Any = None
_pipeline_backend: str | None = None  # 已載成功的 backend tag
_pipeline_load_failed: bool = False  # 失敗過就不再重試 NLI 載入

_st_lock = threading.Lock()
_st_model: Any = None
_st_load_failed: bool = False


# ── Pipeline lazy loader ──────────────────────────────────────────────────
def _try_load_pipeline() -> tuple[Any, str] | None:
    """嘗試載 NLI pipeline。先試 primary，失敗再試 fallback。

    回 (pipeline, backend_tag) 或 None。
    """
    try:
        from transformers import pipeline as hf_pipeline  # type: ignore
    except Exception as e:
        logger.info("local_nli: transformers unavailable: %s", e)
        return None

    for model_name, tag in (
        (PRIMARY_NLI_MODEL, BACKEND_NLI_PRIMARY),
        (FALLBACK_NLI_MODEL, BACKEND_NLI_FALLBACK),
    ):
        try:
            pipe = hf_pipeline(
                "zero-shot-classification",
                model=model_name,
                device=-1,  # CPU only — 跑 LINE bot 沒 GPU
            )
            logger.info("local_nli: loaded NLI model %s (%s)", model_name, tag)
            return pipe, tag
        except Exception as e:
            logger.warning(
                "local_nli: failed to load %s: %s — try next", model_name, e
            )
            continue

    logger.info("local_nli: all NLI models failed to load")
    return None


def _get_pipeline() -> tuple[Any, str] | None:
    """Lazy-init NLI pipeline. 失敗一次就不再重試。"""
    global _pipeline, _pipeline_backend, _pipeline_load_failed
    if _pipeline is not None and _pipeline_backend is not None:
        return _pipeline, _pipeline_backend
    if _pipeline_load_failed:
        return None
    with _pipeline_lock:
        # double-check after acquiring lock
        if _pipeline is not None and _pipeline_backend is not None:
            return _pipeline, _pipeline_backend
        if _pipeline_load_failed:
            return None
        loaded = _try_load_pipeline()
        if loaded is None:
            _pipeline_load_failed = True
            return None
        _pipeline, _pipeline_backend = loaded
        return _pipeline, _pipeline_backend


# ── ST cosine fallback ────────────────────────────────────────────────────
def _try_load_st() -> Any | None:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as e:
        logger.info("local_nli: sentence-transformers unavailable: %s", e)
        return None
    try:
        return SentenceTransformer(ST_FALLBACK_MODEL, device="cpu")
    except Exception as e:
        logger.warning("local_nli: failed to load %s: %s", ST_FALLBACK_MODEL, e)
        return None


def _get_st() -> Any | None:
    global _st_model, _st_load_failed
    if _st_model is not None:
        return _st_model
    if _st_load_failed:
        return None
    with _st_lock:
        if _st_model is not None:
            return _st_model
        if _st_load_failed:
            return None
        m = _try_load_st()
        if m is None:
            _st_load_failed = True
            return None
        _st_model = m
        return _st_model


def _cosine_similarity(claim: str, source: str) -> float | None:
    """用 ST encode 兩段文字，回 cosine similarity ∈ [-1, 1] → clamp 到 [0, 1]。

    當作 entailment proxy（很粗，只在 NLI 不可用時兜底）。
    回 None 表示連 ST 也壞了。
    """
    model = _get_st()
    if model is None:
        return None
    try:
        import numpy as np  # type: ignore

        vecs = model.encode(
            [claim, source],
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        sim = float(np.dot(vecs[0], vecs[1]))
        # cosine ∈ [-1, 1]，把 [-1, 0] clamp 到 0；正向部分視為 entailment 強度
        return max(0.0, min(1.0, sim))
    except Exception as e:
        logger.warning("local_nli: cosine compute failed: %s", e)
        return None


# ── 公開：verify_claim ────────────────────────────────────────────────────
def verify_claim(claim: str, source: str) -> float:
    """驗 claim 是否被 source 支持。回 [0, 1] 的 entailment 分。

    優先順序：NLI pipeline → cosine similarity → 1.0（當作通過、不阻塞）。

    任何 exception 都吞下不往外丟（grounding check 不能讓主流程死）。
    """
    if not claim or not source:
        # 空輸入沒法驗 → 視為通過
        return 1.0

    # ── Layer 1：NLI pipeline ──
    pipe_result = _get_pipeline()
    if pipe_result is not None:
        pipe, _tag = pipe_result
        try:
            out = pipe(
                source,  # premise（源頭）
                candidate_labels=CANDIDATE_LABELS,
                hypothesis_template="這段話支持以下說法：{}。它的具體內容是：" + claim,
                multi_label=False,
            )
            # transformers 回 {"labels": [...], "scores": [...], "sequence": ...}
            labels = out.get("labels", []) if isinstance(out, dict) else []
            scores = out.get("scores", []) if isinstance(out, dict) else []
            for lbl, sc in zip(labels, scores):
                if lbl == ENTAILMENT_LABEL:
                    return float(max(0.0, min(1.0, sc)))
            # 沒 entailment label 就退 cosine
        except Exception as e:
            logger.warning("local_nli: pipeline call failed: %s — try cosine", e)

    # ── Layer 2：cosine similarity ──
    cos = _cosine_similarity(claim, source)
    if cos is not None:
        return cos

    # ── Layer 3：noop（不阻塞）──
    logger.info("local_nli: all backends failed, returning 1.0 (noop)")
    return 1.0


# ── 公開：score_response ──────────────────────────────────────────────────
def split_into_claims(text: str) -> list[str]:
    """把 response 切成 sentence-level claims。

    切點：句號 / 問號 / 驚嘆號 / 換行。過濾過短的句子。
    """
    if not text:
        return []
    pieces = _SENTENCE_SPLIT_RE.split(text)
    out: list[str] = []
    for p in pieces:
        s = (p or "").strip()
        if not s:
            continue
        if len(s) < _MIN_CLAIM_CHARS:
            continue
        out.append(s)
    return out


def _best_source_for_claim(
    claim: str, sources: list[str]
) -> tuple[str, int]:
    """回 (best_source_text, idx)。

    用 ST cosine 挑最相關的 source；如果 ST 不可用，回 sources[0]。
    """
    if not sources:
        return "", -1
    if len(sources) == 1:
        return sources[0], 0

    model = _get_st()
    if model is None:
        return sources[0], 0
    try:
        import numpy as np  # type: ignore

        vecs = model.encode(
            [claim] + list(sources),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        claim_v = vecs[0]
        sims = vecs[1:] @ claim_v
        idx = int(sims.argmax())
        return sources[idx], idx
    except Exception as e:
        logger.warning("local_nli: best-source select failed: %s", e)
        return sources[0], 0


def score_response(
    response: str, sources: list[str]
) -> dict[str, Any]:
    """對整段 response 算 grounding score。

    流程：
      1. 切句 → claims
      2. 每句挑最相關 source
      3. 對每句跑 verify_claim
      4. 算平均、收集低分句

    回傳：
      {
        "score_avg": float ∈ [0, 1]，沒任何 claim 時 = 1.0,
        "low_score_claims": [(claim, score)]  # < 0.5 的句子,
        "verified": [(claim, score)]          # 全部驗過的句子,
        "backend": "nli_primary" | "nli_fallback" | "cosine" | "noop",
      }
    """
    backend = _resolve_active_backend()

    claims = split_into_claims(response)
    if not claims or not sources:
        return {
            "score_avg": 1.0,
            "low_score_claims": [],
            "verified": [],
            "backend": backend,
        }

    verified: list[tuple[str, float]] = []
    low_score: list[tuple[str, float]] = []

    for claim in claims:
        src, _ = _best_source_for_claim(claim, sources)
        if not src:
            continue
        try:
            score = verify_claim(claim, src)
        except Exception as e:
            logger.warning("local_nli: verify_claim threw, skipping: %s", e)
            continue
        verified.append((claim, score))
        if score < 0.5:
            low_score.append((claim, score))

    if not verified:
        return {
            "score_avg": 1.0,
            "low_score_claims": [],
            "verified": [],
            "backend": backend,
        }

    score_avg = sum(s for _, s in verified) / len(verified)
    return {
        "score_avg": float(score_avg),
        "low_score_claims": low_score,
        "verified": verified,
        "backend": backend,
    }


def _resolve_active_backend() -> str:
    """回目前實際 active 的 backend tag（給 caller debug）。

    注意：這個會 force-load pipeline / ST，是「探查」用。caller 真正
    的決策還是看 score_avg。
    """
    pipe = _get_pipeline()
    if pipe is not None:
        return pipe[1]
    if _get_st() is not None:
        return BACKEND_COSINE
    return BACKEND_NOOP


# ── 測試用 reset hook ────────────────────────────────────────────────────
def _reset_state_for_tests() -> None:
    """pytest 用：清掉所有 lazy state，下次呼叫重新走 fallback 邏輯。"""
    global _pipeline, _pipeline_backend, _pipeline_load_failed
    global _st_model, _st_load_failed
    _pipeline = None
    _pipeline_backend = None
    _pipeline_load_failed = False
    _st_model = None
    _st_load_failed = False
