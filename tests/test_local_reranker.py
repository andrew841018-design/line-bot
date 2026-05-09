"""
tests/test_local_reranker.py — pure-mock unit tests for local_reranker.

We never download the real cross-encoder here; ``CrossEncoder`` is patched
to a fake that returns deterministic scores. This keeps the suite offline
and fast.
"""

from __future__ import annotations

import importlib
import sys
from typing import List, Sequence

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeCrossEncoder:
    """Returns fixed scores by candidate-string lookup.

    Constructed via ``_FakeCrossEncoder.with_scores({...})`` per-test so we
    can assert ranking deterministically.
    """

    score_table: dict[str, float] = {}
    instances_created: list[str] = []

    def __init__(self, model_name: str, *args, **kwargs):
        self.model_name = model_name
        type(self).instances_created.append(model_name)

    def predict(self, pairs: Sequence[tuple[str, str]]) -> List[float]:
        return [self.score_table.get(c, 0.0) for (_q, c) in pairs]


def _reload_reranker(monkeypatch, fake_class=None, raise_on_load=False):
    """Force a clean reload of local_reranker with optional CrossEncoder patch."""
    sys.modules.pop("local_reranker", None)
    import local_reranker  # noqa: F401  (fresh import)

    importlib.reload(local_reranker)

    # Reset module-level singletons explicitly (paranoia).
    local_reranker._model = None
    local_reranker._model_load_failed = False

    if fake_class is not None:
        # Patch sentence_transformers.CrossEncoder so _get_model picks it up.
        import sentence_transformers as st_mod

        if raise_on_load:

            def _boom(*a, **kw):
                raise RuntimeError("synthetic load failure")

            monkeypatch.setattr(st_mod, "CrossEncoder", _boom)
        else:
            monkeypatch.setattr(st_mod, "CrossEncoder", fake_class)

    return local_reranker


@pytest.fixture(autouse=True)
def _reset_fake_state():
    _FakeCrossEncoder.score_table = {}
    _FakeCrossEncoder.instances_created = []
    yield
    _FakeCrossEncoder.score_table = {}
    _FakeCrossEncoder.instances_created = []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_candidates_returns_empty(monkeypatch):
    """(c) candidates 空 → 回 []，不嘗試 load model。"""
    lr = _reload_reranker(monkeypatch, fake_class=_FakeCrossEncoder)
    out = lr.rerank("hello", [], top_k=3)
    assert out == []
    assert _FakeCrossEncoder.instances_created == []  # lazy: never instantiated


def test_basic_ranking_correct(monkeypatch):
    """(a) ranking 按 score 由高到低。"""
    _FakeCrossEncoder.score_table = {
        "apple": 0.1,
        "banana": 0.9,
        "cherry": 0.5,
    }
    lr = _reload_reranker(monkeypatch, fake_class=_FakeCrossEncoder)
    out = lr.rerank("fruit?", ["apple", "banana", "cherry"], top_k=3)
    assert [text for text, _ in out] == ["banana", "cherry", "apple"]
    assert out[0][1] == pytest.approx(0.9)
    assert out[1][1] == pytest.approx(0.5)
    assert out[2][1] == pytest.approx(0.1)


def test_top_k_truncation(monkeypatch):
    """top_k=2 只回 2 筆且仍是分數最高的兩個。"""
    _FakeCrossEncoder.score_table = {"a": 0.2, "b": 0.8, "c": 0.5, "d": 0.99}
    lr = _reload_reranker(monkeypatch, fake_class=_FakeCrossEncoder)
    out = lr.rerank("q", ["a", "b", "c", "d"], top_k=2)
    assert [t for t, _ in out] == ["d", "b"]
    assert len(out) == 2


def test_lazy_load_only_on_first_call(monkeypatch):
    """(b) lazy load：import 後沒打過 rerank → CrossEncoder 沒被 instantiate。"""
    lr = _reload_reranker(monkeypatch, fake_class=_FakeCrossEncoder)
    # 重點：reload 完還沒呼叫 rerank。
    assert _FakeCrossEncoder.instances_created == []
    assert lr._model is None  # 尚未 load

    _FakeCrossEncoder.score_table = {"x": 0.7}
    lr.rerank("q", ["x"], top_k=1)
    assert len(_FakeCrossEncoder.instances_created) == 1
    assert lr._model is not None


def test_model_loaded_only_once(monkeypatch):
    """連續多次 rerank 不應重複 load model。"""
    _FakeCrossEncoder.score_table = {"a": 0.5, "b": 0.6}
    lr = _reload_reranker(monkeypatch, fake_class=_FakeCrossEncoder)
    lr.rerank("q1", ["a", "b"], top_k=2)
    lr.rerank("q2", ["a", "b"], top_k=2)
    lr.rerank("q3", ["a", "b"], top_k=2)
    assert len(_FakeCrossEncoder.instances_created) == 1


def test_model_load_failure_graceful(monkeypatch):
    """(d) model load 失敗 → 回原 ranking，不爆。"""
    lr = _reload_reranker(
        monkeypatch, fake_class=_FakeCrossEncoder, raise_on_load=True
    )
    out = lr.rerank("q", ["alpha", "beta", "gamma"], top_k=2)
    # 兩個 model 都失敗 → fallback 到原順序、score 0.0。
    assert out == [("alpha", 0.0), ("beta", 0.0)]
    assert lr._model_load_failed is True
    # 第二次再叫 → 不該再嘗試 load（已 cached failure）。
    out2 = lr.rerank("q", ["alpha"], top_k=1)
    assert out2 == [("alpha", 0.0)]


def test_predict_failure_falls_back(monkeypatch):
    """predict 階段爆掉 → 回原 ranking。"""

    class _BoomingEncoder(_FakeCrossEncoder):
        def predict(self, pairs):
            raise RuntimeError("OOM-ish boom")

    lr = _reload_reranker(monkeypatch, fake_class=_BoomingEncoder)
    out = lr.rerank("q", ["one", "two", "three"], top_k=2)
    assert out == [("one", 0.0), ("two", 0.0)]


def test_chunking_for_large_candidate_pool(monkeypatch):
    """候選 > _BATCH_THRESHOLD → predict 被多次呼叫，score 數量正確。"""

    call_log: list[int] = []

    class _ChunkSpy(_FakeCrossEncoder):
        def predict(self, pairs):
            call_log.append(len(pairs))
            # score = index in pair list, 確保唯一可排序
            return [
                float(_FakeCrossEncoder.score_table.get(c, 0.0)) for (_q, c) in pairs
            ]

    cands = [f"doc-{i}" for i in range(150)]
    _FakeCrossEncoder.score_table = {c: float(i) for i, c in enumerate(cands)}
    lr = _reload_reranker(monkeypatch, fake_class=_ChunkSpy)

    out = lr.rerank("q", cands, top_k=5)
    # 應該有多次 predict（150 / 64 = 3 chunks）。
    assert len(call_log) >= 2
    assert sum(call_log) == 150
    # 最高分是 doc-149。
    assert out[0][0] == "doc-149"
    assert len(out) == 5


def test_blank_query_returns_fallback(monkeypatch):
    """空 query → 不打 cross-encoder，直接 fallback 原順序。"""
    lr = _reload_reranker(monkeypatch, fake_class=_FakeCrossEncoder)
    out = lr.rerank("   ", ["a", "b", "c"], top_k=2)
    assert out == [("a", 0.0), ("b", 0.0)]
    # 空 query 應該不觸發 model load。
    assert _FakeCrossEncoder.instances_created == []


def test_filters_blank_candidates(monkeypatch):
    """空字串 / 空白候選會被過濾，不送進 predict。"""
    seen_pairs: list[tuple[str, str]] = []

    class _PairSpy(_FakeCrossEncoder):
        def predict(self, pairs):
            seen_pairs.extend(pairs)
            return [_FakeCrossEncoder.score_table.get(c, 0.0) for (_q, c) in pairs]

    _FakeCrossEncoder.score_table = {"real": 0.9, "another": 0.3}
    lr = _reload_reranker(monkeypatch, fake_class=_PairSpy)

    out = lr.rerank("q", ["real", "", "   ", None, "another"], top_k=3)  # type: ignore[list-item]
    assert [t for t, _ in out] == ["real", "another"]
    # predict 看到的只有 2 個非空候選。
    assert len(seen_pairs) == 2
    assert {p[1] for p in seen_pairs} == {"real", "another"}


def test_top_k_zero_returns_empty(monkeypatch):
    """top_k=0 → 回 []。"""
    _FakeCrossEncoder.score_table = {"a": 0.5}
    lr = _reload_reranker(monkeypatch, fake_class=_FakeCrossEncoder)
    out = lr.rerank("q", ["a", "b"], top_k=0)
    assert out == []
