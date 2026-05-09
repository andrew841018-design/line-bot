"""
tests/test_local_nli.py — pure-mock tests for local_nli.

Tests cover:
    1. verify_claim: high entailment via mocked pipeline
    2. verify_claim: contradiction (low entailment) via mocked pipeline
    3. verify_claim: pipeline raises -> falls back to cosine
    4. verify_claim: pipeline + ST both fail -> noop returns 1.0
    5. verify_claim: empty inputs -> 1.0
    6. score_response: aggregates per-claim scores correctly
    7. score_response: low_score_claims captures < 0.5 sentences
    8. score_response: empty response / sources -> 1.0
    9. split_into_claims: punctuation + newline boundaries
   10. split_into_claims: filters too-short fragments
   11. score_response: dispatches each claim to best source
   12. _resolve_active_backend: reflects pipeline / cosine / noop tier

NO real model loading — every transformer / sentence-transformer call
is monkey-patched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure project root on sys.path (tests/ is a sibling).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import local_nli  # noqa: E402


# ── shared fixtures / fakes ───────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_local_nli_state():
    """每個 test 前後都清掉 lazy-loaded state，避免 cross-test bleed。"""
    local_nli._reset_state_for_tests()
    yield
    local_nli._reset_state_for_tests()


class _FakePipeline:
    """模仿 transformers.pipeline 的 callable + 可控 entailment 分。"""

    def __init__(self, entail_score: float, contradict_score: float | None = None):
        self.entail_score = entail_score
        self.contradict_score = (
            contradict_score
            if contradict_score is not None
            else 1.0 - entail_score
        )
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        sequence: str,
        candidate_labels: list[str] | None = None,
        hypothesis_template: str | None = None,
        multi_label: bool = False,
    ) -> dict[str, Any]:
        self.calls.append({
            "sequence": sequence,
            "candidate_labels": candidate_labels,
            "hypothesis_template": hypothesis_template,
        })
        return {
            "labels": [local_nli.ENTAILMENT_LABEL, local_nli.CONTRADICTION_LABEL],
            "scores": [self.entail_score, self.contradict_score],
            "sequence": sequence,
        }


class _FakeRaisingPipeline:
    """每次 call 都丟 exception。"""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated pipeline failure")


class _FakeSTModel:
    """模仿 SentenceTransformer.encode：回固定 cosine 強度的向量。"""

    def __init__(self, sim_value: float = 0.8):
        self.sim_value = sim_value

    def encode(
        self,
        texts: list[str],
        convert_to_numpy: bool = True,
        normalize_embeddings: bool = True,
        show_progress_bar: bool = False,
        **_: Any,
    ) -> Any:
        import numpy as np

        # 第 0 個 vec 固定 [1, 0]；其他 vec 設成讓 dot = sim_value
        # 目標：np.dot(v0, vi) = sim_value
        n = len(texts)
        out = np.zeros((n, 2), dtype=float)
        out[0] = np.array([1.0, 0.0])
        s = self.sim_value
        # vi = [s, sqrt(1-s^2)] → norm = 1 → dot(v0, vi) = s
        import math

        complement = math.sqrt(max(0.0, 1.0 - s * s))
        for i in range(1, n):
            out[i] = np.array([s, complement])
        return out


# ── 1. high-entailment 走 NLI pipeline ─────────────────────────────────────
def test_verify_claim_high_entailment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePipeline(entail_score=0.92)
    monkeypatch.setattr(
        local_nli, "_get_pipeline", lambda: (fake, local_nli.BACKEND_NLI_PRIMARY)
    )

    score = local_nli.verify_claim(
        "今天台北下雨。", "氣象局說今天台北會下雨"
    )
    assert score == pytest.approx(0.92, abs=0.001)
    assert len(fake.calls) == 1
    assert fake.calls[0]["candidate_labels"] == [
        local_nli.ENTAILMENT_LABEL,
        local_nli.CONTRADICTION_LABEL,
    ]


# ── 2. contradiction → 低分 ───────────────────────────────────────────────
def test_verify_claim_contradiction_low(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePipeline(entail_score=0.08, contradict_score=0.92)
    monkeypatch.setattr(
        local_nli, "_get_pipeline", lambda: (fake, local_nli.BACKEND_NLI_PRIMARY)
    )

    score = local_nli.verify_claim(
        "今天台北放晴。", "氣象局說今天台北會下雨"
    )
    assert score < 0.2


# ── 3. pipeline 失敗 → cosine fallback ─────────────────────────────────────
def test_verify_claim_falls_back_to_cosine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raising = _FakeRaisingPipeline()
    monkeypatch.setattr(
        local_nli,
        "_get_pipeline",
        lambda: (raising, local_nli.BACKEND_NLI_PRIMARY),
    )
    monkeypatch.setattr(local_nli, "_get_st", lambda: _FakeSTModel(sim_value=0.73))

    score = local_nli.verify_claim("a claim", "a source")
    # cosine fake 設定 0.73
    assert score == pytest.approx(0.73, abs=0.01)


# ── 4. pipeline + ST 都壞 → 1.0 (noop) ─────────────────────────────────────
def test_verify_claim_all_fail_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_nli, "_get_pipeline", lambda: None)
    monkeypatch.setattr(local_nli, "_get_st", lambda: None)

    score = local_nli.verify_claim("any claim", "any source")
    assert score == 1.0


# ── 5. 空輸入 → 1.0 ────────────────────────────────────────────────────────
def test_verify_claim_empty_inputs() -> None:
    assert local_nli.verify_claim("", "source") == 1.0
    assert local_nli.verify_claim("claim", "") == 1.0
    assert local_nli.verify_claim("", "") == 1.0


# ── 6. score_response 平均聚合 ────────────────────────────────────────────
def test_score_response_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePipeline(entail_score=0.80)
    monkeypatch.setattr(
        local_nli, "_get_pipeline", lambda: (fake, local_nli.BACKEND_NLI_PRIMARY)
    )
    # source select 用單一 source，避免走 ST
    result = local_nli.score_response(
        "第一句明天會下雨。第二句後天放晴。",
        ["氣象局報告：明天下雨後天放晴"],
    )
    assert result["score_avg"] == pytest.approx(0.80, abs=0.01)
    assert result["low_score_claims"] == []
    assert len(result["verified"]) == 2
    assert result["backend"] == local_nli.BACKEND_NLI_PRIMARY


# ── 7. low_score_claims 收 < 0.5 ──────────────────────────────────────────
def test_score_response_collects_low_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 第一句 0.9，第二句 0.2 → 平均 0.55，第二句進 low_score
    call_count = {"i": 0}
    scores_seq = [0.9, 0.2]

    class _SeqPipeline:
        def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            i = call_count["i"]
            call_count["i"] += 1
            es = scores_seq[i % len(scores_seq)]
            return {
                "labels": [
                    local_nli.ENTAILMENT_LABEL,
                    local_nli.CONTRADICTION_LABEL,
                ],
                "scores": [es, 1.0 - es],
                "sequence": args[0] if args else "",
            }

    monkeypatch.setattr(
        local_nli,
        "_get_pipeline",
        lambda: (_SeqPipeline(), local_nli.BACKEND_NLI_PRIMARY),
    )

    result = local_nli.score_response(
        "好句子明天下雨。壞句子月亮是綠色的。",
        ["明天下雨"],
    )
    assert result["score_avg"] == pytest.approx(0.55, abs=0.05)
    assert len(result["low_score_claims"]) == 1
    bad_claim, bad_score = result["low_score_claims"][0]
    assert "綠色" in bad_claim
    assert bad_score < 0.5


# ── 8. 空 response / sources → 1.0 ────────────────────────────────────────
def test_score_response_empty_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        local_nli,
        "_get_pipeline",
        lambda: (_FakePipeline(0.9), local_nli.BACKEND_NLI_PRIMARY),
    )
    r1 = local_nli.score_response("", ["src"])
    assert r1["score_avg"] == 1.0
    assert r1["verified"] == []

    r2 = local_nli.score_response("一些句子。", [])
    assert r2["score_avg"] == 1.0
    assert r2["verified"] == []


# ── 9. split: 中英標點 + 換行 ─────────────────────────────────────────────
def test_split_into_claims_boundaries() -> None:
    text = "第一句話啊。第二句呢？第三句喔！\n第四句沒標點"
    out = local_nli.split_into_claims(text)
    assert any("第一句話" in s for s in out)
    assert any("第二句" in s for s in out)
    assert any("第三句" in s for s in out)
    assert any("第四句" in s for s in out)
    assert len(out) == 4


# ── 10. split: 過濾過短片段 ──────────────────────────────────────────────
def test_split_into_claims_filters_short() -> None:
    # "好。" "嗯" 都太短應該被過濾，"明天去台北" 留下
    text = "好。嗯。明天去台北。"
    out = local_nli.split_into_claims(text)
    assert out == ["明天去台北。"]


# ── 11. score_response: 多 source → 用 ST 挑最相關 ────────────────────────
def test_score_response_picks_best_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多 source 時應該叫 _best_source_for_claim 挑一個。"""
    fake = _FakePipeline(entail_score=0.7)
    monkeypatch.setattr(
        local_nli, "_get_pipeline", lambda: (fake, local_nli.BACKEND_NLI_PRIMARY)
    )

    # 直接 patch _best_source_for_claim 確認它被呼到
    calls: list[tuple[str, list[str]]] = []

    def _spy(claim: str, sources: list[str]) -> tuple[str, int]:
        calls.append((claim, list(sources)))
        return sources[0], 0

    monkeypatch.setattr(local_nli, "_best_source_for_claim", _spy)

    result = local_nli.score_response(
        "第一個事實在這裡。第二個事實也在這裡。",
        ["source A", "source B", "source C"],
    )
    assert len(calls) == 2  # 每句都挑一次
    assert result["score_avg"] == pytest.approx(0.7, abs=0.01)


# ── 12. _resolve_active_backend tier reporting ────────────────────────────
def test_resolve_active_backend_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    # tier 1: pipeline 載得到 → primary
    fake = _FakePipeline(0.5)
    monkeypatch.setattr(
        local_nli, "_get_pipeline", lambda: (fake, local_nli.BACKEND_NLI_PRIMARY)
    )
    assert local_nli._resolve_active_backend() == local_nli.BACKEND_NLI_PRIMARY

    # tier 2: 沒 pipeline，但有 ST → cosine
    monkeypatch.setattr(local_nli, "_get_pipeline", lambda: None)
    monkeypatch.setattr(local_nli, "_get_st", lambda: _FakeSTModel())
    assert local_nli._resolve_active_backend() == local_nli.BACKEND_COSINE

    # tier 3: 都沒 → noop
    monkeypatch.setattr(local_nli, "_get_st", lambda: None)
    assert local_nli._resolve_active_backend() == local_nli.BACKEND_NOOP
