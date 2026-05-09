"""Tests for bm25_index.BM25Index.

Coverage targets:
* Chinese tokenization + retrieval (jieba path)
* English tokenization
* Empty corpus / empty query handling
* Persistence round-trip (save/load)
* Stopword filtering
* Top-k truncation
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# bootstrap env (對齊 conftest.py)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

# Ensure project root on sys.path so `import bm25_index` works regardless
# of where pytest is invoked from.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from bm25_index import BM25Index, tokenize  # noqa: E402


# ── tokenizer ──────────────────────────────────────────────────────────────


def test_tokenize_zh_basic():
    toks = tokenize("今天天氣很好")
    # jieba should split into something containing 今天 + 天氣
    assert any("天氣" in t or "天" in t for t in toks)
    assert "今天" in toks or "今" in toks


def test_tokenize_en_basic():
    toks = tokenize("The Apple stock rose today")
    # 'the' is a stopword; 'apple' / 'stock' / 'rose' should remain
    assert "apple" in toks
    assert "stock" in toks
    assert "the" not in toks


def test_tokenize_stopwords_filtered():
    toks = tokenize("我的天氣")
    # '我' and '的' should be filtered, '天氣' kept
    assert "我" not in toks
    assert "的" not in toks
    assert any("天氣" in t for t in toks)


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []


# ── BM25Index core ─────────────────────────────────────────────────────────


@pytest.fixture
def chinese_corpus():
    return [
        "今天天氣很好,適合出去玩",
        "明天可能會下雨,記得帶傘",
        "AAPL 股價今天上漲 2%",
        "週末家庭聚餐在內湖餐廳",
        "提醒下週一要去看牙醫",
    ]


def test_build_and_query_zh_weather(chinese_corpus):
    """中文 query 「今天天氣如何」應該抓到 doc 0 (今天天氣很好)。"""
    idx = BM25Index()
    idx.build(chinese_corpus)
    hits = idx.query("今天天氣如何", k=3)
    assert hits, "expected at least one hit"
    top_idx = hits[0][0]
    assert top_idx == 0, f"expected doc 0 (天氣) on top, got {top_idx}: {chinese_corpus[top_idx]}"
    # Score should be positive
    assert hits[0][1] > 0


def test_build_and_query_zh_dental(chinese_corpus):
    """Query 「看牙醫」 should rank doc 4 (看牙醫) on top.

    Note: jieba tokenizes both '看牙醫' (in doc 4) and the query as a
    single token, so this is a clean rare-token match. Querying just
    '牙醫' alone wouldn't match because jieba never splits the inner
    bigram out — that's a known segmentation quirk we accept.
    """
    idx = BM25Index()
    idx.build(chinese_corpus)
    hits = idx.query("看牙醫", k=3)
    assert hits
    assert hits[0][0] == 4
    assert hits[0][1] > 0


def test_build_and_query_en_ticker(chinese_corpus):
    """Query 'AAPL' should rank doc 2 (AAPL 股價) first (rare token)."""
    idx = BM25Index()
    idx.build(chinese_corpus)
    hits = idx.query("AAPL", k=3)
    assert hits
    assert hits[0][0] == 2


def test_query_topk_truncation(chinese_corpus):
    idx = BM25Index()
    idx.build(chinese_corpus)
    hits = idx.query("今天", k=2)
    assert len(hits) <= 2


def test_query_k_zero(chinese_corpus):
    idx = BM25Index()
    idx.build(chinese_corpus)
    assert idx.query("今天", k=0) == []


# ── edge cases ─────────────────────────────────────────────────────────────


def test_empty_corpus():
    idx = BM25Index()
    idx.build([])
    assert idx.query("anything", k=5) == []
    assert idx.corpus_size == 0


def test_corpus_with_empty_doc():
    """Empty docs in corpus shouldn't crash; their idx should not match."""
    idx = BM25Index()
    idx.build(["", "今天天氣很好", ""])
    hits = idx.query("天氣", k=3)
    assert hits
    # Top hit must be the only meaningful doc
    assert hits[0][0] == 1


def test_empty_query_returns_empty(chinese_corpus):
    idx = BM25Index()
    idx.build(chinese_corpus)
    assert idx.query("", k=3) == []
    assert idx.query("   ", k=3) == []


def test_query_with_only_stopwords(chinese_corpus):
    idx = BM25Index()
    idx.build(chinese_corpus)
    # All tokens are stopwords → empty token list → no hits
    out = idx.query("的 了 是", k=3)
    assert out == []


# ── persistence ────────────────────────────────────────────────────────────


def test_save_load_roundtrip(chinese_corpus):
    idx = BM25Index()
    idx.build(chinese_corpus)
    expected = idx.query("今天天氣如何", k=3)

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "bm25.pkl"
        idx.save(path)
        assert path.exists()

        loaded = BM25Index.load(path)
        actual = loaded.query("今天天氣如何", k=3)

    assert actual == expected
    assert loaded.corpus_size == len(chinese_corpus)


def test_save_load_empty_index():
    """Saving + loading an empty index should also work."""
    idx = BM25Index()
    idx.build([])
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "bm25_empty.pkl"
        idx.save(path)
        loaded = BM25Index.load(path)
    assert loaded.corpus_size == 0
    assert loaded.query("anything", k=3) == []


# ── ranking sanity ─────────────────────────────────────────────────────────


def test_rare_term_outranks_common(chinese_corpus):
    """Rare-token query should beat common-token query for specific docs.

    Doc 2 mentions "AAPL" (very rare). Even though doc 0 contains '今天'
    and doc 2 ALSO contains '今天', the AAPL token gives doc 2 a higher
    BM25 score for query 'AAPL 今天'.
    """
    idx = BM25Index()
    idx.build(chinese_corpus)
    hits = idx.query("AAPL 今天", k=5)
    assert hits[0][0] == 2
