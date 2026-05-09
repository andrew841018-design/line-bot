"""Tests for hybrid_retrieve.hybrid_search.

Coverage:
* alpha=0 -> behaves like pure BM25
* alpha=1 -> behaves like pure dense
* alpha=0.5 -> balanced fusion
* z-score normalization (each modality has mean 0)
* Empty / degenerate inputs
* Hit-shape contract
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from bm25_index import BM25Index  # noqa: E402
from hybrid_retrieve import _zscore, hybrid_search  # noqa: E402


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def fake_corpus():
    return [
        {"message_id": "1", "group_id": "G", "text": "今天天氣很好,適合出去玩", "created_at": 1},
        {"message_id": "2", "group_id": "G", "text": "明天可能會下雨,記得帶傘", "created_at": 2},
        {"message_id": "3", "group_id": "G", "text": "AAPL 股價今天上漲 2%", "created_at": 3},
        {"message_id": "4", "group_id": "G", "text": "週末家庭聚餐在內湖餐廳", "created_at": 4},
        {"message_id": "5", "group_id": "G", "text": "提醒下週一要去看牙醫", "created_at": 5},
    ]


@pytest.fixture
def prebuilt_bm25(fake_corpus):
    idx = BM25Index()
    idx.build([row["text"] for row in fake_corpus])
    return idx


# ── z-score normalization unit ─────────────────────────────────────────────


def test_zscore_empty():
    assert _zscore({}) == {}


def test_zscore_single_element():
    out = _zscore({"a": 5.0})
    assert out == {"a": 0.0}


def test_zscore_mean_zero_after_normalize():
    out = _zscore({"a": 1.0, "b": 2.0, "c": 3.0})
    vals = list(out.values())
    assert abs(sum(vals)) < 1e-9  # mean of z-scores is 0


def test_zscore_constant_returns_zeros():
    out = _zscore({"a": 7.0, "b": 7.0, "c": 7.0})
    assert all(v == 0.0 for v in out.values())


# ── alpha=0 -> pure BM25 ───────────────────────────────────────────────────


def test_alpha_zero_is_pure_bm25(fake_corpus, prebuilt_bm25):
    """alpha=0 → BM25 ranking dominates; weather doc must rank top."""
    hits = hybrid_search(
        "今天天氣如何",
        k=3,
        alpha=0.0,
        corpus=fake_corpus,
        dense_pool=[],
        bm25_index=prebuilt_bm25,
    )
    assert hits, "expected at least one hit"
    assert hits[0]["message_id"] == "1"
    assert hits[0]["bm25_score"] > 0
    # similarity field should be 0 since dense pool was empty
    assert hits[0]["similarity"] == 0.0


def test_alpha_zero_ignores_dense_signal(fake_corpus, prebuilt_bm25):
    """Even with a misleading dense pool, alpha=0 ranks by BM25 only."""
    # Dense says doc 4 is the best — but BM25 strongly disagrees for
    # "今天天氣". With alpha=0, BM25 must win.
    misleading_dense = [
        {"message_id": "4", "text": fake_corpus[3]["text"], "similarity": 0.99},
    ]
    hits = hybrid_search(
        "今天天氣如何",
        k=3,
        alpha=0.0,
        corpus=fake_corpus,
        dense_pool=misleading_dense,
        bm25_index=prebuilt_bm25,
    )
    assert hits[0]["message_id"] == "1"


# ── alpha=1 -> pure dense ──────────────────────────────────────────────────


def test_alpha_one_is_pure_dense(fake_corpus, prebuilt_bm25):
    """alpha=1.0 → fusion follows dense ordering exclusively."""
    dense_pool = [
        {"message_id": "4", "text": fake_corpus[3]["text"], "similarity": 0.95},
        {"message_id": "1", "text": fake_corpus[0]["text"], "similarity": 0.50},
    ]
    hits = hybrid_search(
        "今天天氣如何",
        k=3,
        alpha=1.0,
        corpus=fake_corpus,
        dense_pool=dense_pool,
        bm25_index=prebuilt_bm25,
    )
    assert hits, "expected hits"
    # Dense ranks 4 above 1 → fusion must agree with alpha=1.
    assert hits[0]["message_id"] == "4"
    # bm25 column should still be 0 because alpha=1 skipped sparse path.
    assert hits[0]["bm25_score"] == 0.0


# ── alpha=0.5 -> balanced fusion ───────────────────────────────────────────


def test_alpha_half_balances_signals(fake_corpus, prebuilt_bm25):
    """When dense + BM25 agree, balanced fusion picks the consensus doc."""
    dense_pool = [
        {"message_id": "1", "text": fake_corpus[0]["text"], "similarity": 0.85},
        {"message_id": "2", "text": fake_corpus[1]["text"], "similarity": 0.40},
    ]
    hits = hybrid_search(
        "今天天氣如何",
        k=3,
        alpha=0.5,
        corpus=fake_corpus,
        dense_pool=dense_pool,
        bm25_index=prebuilt_bm25,
    )
    assert hits[0]["message_id"] == "1"


def test_alpha_half_blends_when_dense_and_sparse_disagree(
    fake_corpus, prebuilt_bm25
):
    """If dense top is doc 4 and BM25 top is doc 1, alpha=0.5 should
    yield a fused score where doc 1's BM25 evidence is reflected.

    Concretely: doc 1 should have a higher fused score than doc 4
    because BM25 normalized z-score for doc 1 is much larger than
    dense-only doc 4 after both modalities are normalized.
    """
    dense_pool = [
        {"message_id": "4", "text": fake_corpus[3]["text"], "similarity": 0.60},
        {"message_id": "5", "text": fake_corpus[4]["text"], "similarity": 0.55},
    ]
    hits = hybrid_search(
        "今天天氣如何",
        k=5,
        alpha=0.5,
        corpus=fake_corpus,
        dense_pool=dense_pool,
        bm25_index=prebuilt_bm25,
    )
    by_id = {h["message_id"]: h for h in hits}
    assert "1" in by_id, "BM25-only doc 1 must appear in fused results"
    # Doc 1 gets BM25 boost; doc 4 gets dense boost. After z-score the
    # one with highest combined z wins. We assert doc 1 outranks doc 4
    # because BM25 alone picks doc 1 with a strong z-score.
    assert by_id["1"]["fused_score"] > by_id.get("4", {"fused_score": -1e9})[
        "fused_score"
    ]


# ── input validation / degenerate cases ────────────────────────────────────


def test_empty_query_returns_empty(fake_corpus, prebuilt_bm25):
    assert (
        hybrid_search(
            "",
            k=3,
            alpha=0.5,
            corpus=fake_corpus,
            dense_pool=[],
            bm25_index=prebuilt_bm25,
        )
        == []
    )


def test_k_zero_returns_empty(fake_corpus, prebuilt_bm25):
    assert (
        hybrid_search(
            "天氣",
            k=0,
            alpha=0.5,
            corpus=fake_corpus,
            dense_pool=[],
            bm25_index=prebuilt_bm25,
        )
        == []
    )


def test_empty_corpus_returns_empty(prebuilt_bm25):
    assert (
        hybrid_search(
            "天氣",
            k=3,
            alpha=0.5,
            corpus=[],
            dense_pool=[],
            bm25_index=prebuilt_bm25,
        )
        == []
    )


def test_alpha_clamped_to_unit_interval(fake_corpus, prebuilt_bm25):
    """alpha outside [0,1] should clamp, not crash."""
    hits_neg = hybrid_search(
        "天氣",
        k=3,
        alpha=-0.5,
        corpus=fake_corpus,
        dense_pool=[],
        bm25_index=prebuilt_bm25,
    )
    hits_zero = hybrid_search(
        "天氣",
        k=3,
        alpha=0.0,
        corpus=fake_corpus,
        dense_pool=[],
        bm25_index=prebuilt_bm25,
    )
    # Clamping alpha=-0.5 -> 0.0 gives identical ranking
    assert [h["message_id"] for h in hits_neg] == [h["message_id"] for h in hits_zero]


# ── hit-shape contract ─────────────────────────────────────────────────────


def test_hit_shape(fake_corpus, prebuilt_bm25):
    hits = hybrid_search(
        "今天天氣",
        k=2,
        alpha=0.5,
        corpus=fake_corpus,
        dense_pool=[
            {"message_id": "1", "text": fake_corpus[0]["text"], "similarity": 0.7},
        ],
        bm25_index=prebuilt_bm25,
    )
    assert hits
    h = hits[0]
    for key in (
        "message_id",
        "group_id",
        "text",
        "similarity",
        "bm25_score",
        "fused_score",
        "created_at",
    ):
        assert key in h, f"missing field {key}"
