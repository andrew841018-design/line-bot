"""tests/test_grounding_local.py — pure-mock + 1 real-integration tests
for grounding_local.

Coverage:
     1. _keyword_overlap: jaccard over jieba tokens
     2. _keyword_overlap: empty side -> 0.0
     3. _embedding_cosine: ST model returns sim_value
     4. _embedding_cosine: ST unavailable -> char bigram fallback
     5. _number_match: exact + tolerant equality
     6. _number_match: claim has digits not in source -> 0.0
     7. _number_match: source-only digits -> 1.0 (no conflict)
     8. _entity_match: shared org name -> high score
     9. _entity_match: empty claim ents -> 1.0
    10. extract_factual_claims: drops opinion sentences
    11. extract_factual_claims: keeps factual sentences
    12. split_into_claims: punctuation + newline boundaries
    13. check_claim: supported verdict via mocked high cosine
    14. check_claim: contradicts verdict via mocked very low signals
    15. check_claim: uncertain verdict (mid-range)
    16. check_claim: empty source -> uncertain
    17. score_response: aggregates per-claim, captures low scores
    18. score_response: empty response -> 1.0
    19. score_response: empty sources but has claims -> 0.0 + uncertain
    20. real-integration canary: "台積電 Q1 EPS 12 元" vs "...11.85" -> uncertain

Heavy ST loads are mocked except the canary (test 20).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure project root on sys.path (tests/ is a sibling).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import grounding_local as gl  # noqa: E402


# ── shared fixtures / fakes ───────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_grounding_state() -> Any:
    """每個 test 前後都清掉 lazy-loaded state，避免 cross-test bleed。"""
    gl._reset_state_for_tests()
    yield
    gl._reset_state_for_tests()


class _FakeSTModel:
    """模擬 SentenceTransformer.encode：兩段文字回固定 cosine sim_value。

    支援 score_response 的 best_source_for_claim：第一個是 query，後面是 sources。
    用 [s, sqrt(1-s^2)] 編碼讓 dot(v0, vi) = sim_value。
    """

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

        n = len(texts)
        out = np.zeros((n, 2), dtype=float)
        out[0] = np.array([1.0, 0.0])
        s = self.sim_value
        complement = math.sqrt(max(0.0, 1.0 - s * s))
        for i in range(1, n):
            out[i] = np.array([s, complement])
        return out


def _mock_st(monkeypatch: pytest.MonkeyPatch, sim_value: float) -> _FakeSTModel:
    """把 _get_st_model 換成 fake。"""
    fake = _FakeSTModel(sim_value)
    monkeypatch.setattr(gl, "_get_st_model", lambda: fake)
    return fake


# ── 1. keyword overlap basic ──────────────────────────────────────────────
def test_keyword_overlap_jaccard() -> None:
    # "明天" + "下雨" 是兩段共有；其他詞拆開。
    score = gl._keyword_overlap("明天會下雨", "明天下雨機率高")
    assert 0.0 < score <= 1.0


# ── 2. keyword overlap empty ──────────────────────────────────────────────
def test_keyword_overlap_empty_side_returns_zero() -> None:
    assert gl._keyword_overlap("", "anything") == 0.0
    assert gl._keyword_overlap("anything", "") == 0.0
    assert gl._keyword_overlap("", "") == 0.0


# ── 3. embedding cosine via mocked ST ─────────────────────────────────────
def test_embedding_cosine_uses_mocked_st(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_st(monkeypatch, sim_value=0.73)
    sim = gl._embedding_cosine("a claim", "a source")
    assert sim == pytest.approx(0.73, abs=0.01)


# ── 4. embedding cosine fallback when ST None ─────────────────────────────
def test_embedding_cosine_falls_back_when_st_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gl, "_get_st_model", lambda: None)
    # char bigram jaccard fallback. 兩段相同字串應該滿分。
    sim = gl._embedding_cosine("台積電", "台積電")
    assert sim == pytest.approx(1.0, abs=1e-9)


# ── 5. number match exact + tolerant ──────────────────────────────────────
def test_number_match_exact_and_tolerant() -> None:
    # 完全相等
    assert gl._number_match("EPS 12.5", "EPS 12.5") == 1.0
    # 1% 內視為一致：12.5 vs 12.55 -> 0.4% diff -> match
    assert gl._number_match("EPS 12.55", "EPS 12.5") == 1.0
    # 超出 1%：12 vs 11.85 -> 1.27% -> miss
    assert gl._number_match("EPS 12 元", "EPS 11.85") == 0.0


# ── 6. number match claim digits not in source -> 0.0 ─────────────────────
def test_number_match_claim_unmatched_returns_zero() -> None:
    # claim 提了 99，source 完全沒這個數字
    score = gl._number_match("利率調到 99%", "今日股市平淡")
    assert score == 0.0


# ── 7. number match source-only digits -> 1.0 ────────────────────────────
def test_number_match_source_only_returns_one() -> None:
    # claim 沒提數字 -> 不會跟 source 衝突 -> 視為一致
    score = gl._number_match("公司營運穩定", "Q3 EPS 4.21 元")
    assert score == 1.0


# ── 8. entity match shared org -> high ────────────────────────────────────
def test_entity_match_shared_org_high() -> None:
    score = gl._entity_match("台積電本季表現亮眼", "台積電 Q1 表現")
    assert score > 0.0  # 至少抓到台積電


# ── 9. entity match empty claim ents -> 1.0 ──────────────────────────────
def test_entity_match_empty_claim_returns_one() -> None:
    # claim 完全沒實體 -> 不會跟 source 衝突
    assert gl._entity_match("天氣很好", "台積電 Q1 EPS 11.85") == 1.0


# ── 10. extract_factual_claims drops opinions ────────────────────────────
def test_extract_factual_claims_drops_opinions() -> None:
    text = "我覺得這支股票會漲。台積電 Q1 EPS 12 元。也許明天會下雨。"
    out = gl.extract_factual_claims(text)
    # 「我覺得」「也許」會被剔
    assert any("台積電" in c for c in out)
    assert not any("我覺得" in c for c in out)
    assert not any("也許" in c for c in out)


# ── 11. extract_factual_claims keeps factual ─────────────────────────────
def test_extract_factual_claims_keeps_factual() -> None:
    text = "台積電 Q1 EPS 11.85 元。3 月 15 日召開股東會。"
    out = gl.extract_factual_claims(text)
    assert len(out) == 2


# ── 12. split_into_claims punctuation + newline ──────────────────────────
def test_split_into_claims_boundaries() -> None:
    text = "第一句話啊。第二句呢？第三句喔！\n第四句沒標點"
    out = gl.split_into_claims(text)
    assert len(out) == 4
    assert any("第一句" in s for s in out)
    assert any("第二句" in s for s in out)
    assert any("第三句" in s for s in out)
    assert any("第四句" in s for s in out)


# ── 13. check_claim supported via mocked high cosine ──────────────────────
def test_check_claim_supported_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_st(monkeypatch, sim_value=0.95)
    # claim ≈ source；keyword overlap 也高，加上 cosine 0.95 → score > 0.6
    r = gl.check_claim("台積電本季 EPS 11.85 元", "台積電本季 EPS 11.85 元")
    assert r["verdict"] == "supported"
    assert r["score"] > gl.SUPPORTED_THRESHOLD
    assert "keyword" in r["signals"]
    assert "cosine" in r["signals"]


# ── 14. check_claim contradicts via mocked low signals ────────────────────
def test_check_claim_contradicts_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_st(monkeypatch, sim_value=0.05)
    # 極低 cosine，加上 claim 提到 source 沒有的數字，總分 < 0.3
    r = gl.check_claim(
        "今天溫度 99 度", "台積電半導體季報"
    )
    assert r["verdict"] == "contradicts"
    assert r["score"] < gl.CONTRADICTS_THRESHOLD


# ── 15. check_claim uncertain (mid range) ─────────────────────────────────
def test_check_claim_uncertain_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_st(monkeypatch, sim_value=0.5)
    # mid cosine, 部分數字對得上 → 卡在 0.3 ~ 0.6 中間
    r = gl.check_claim("台積電 Q1 EPS 12 元", "台積電 Q1 EPS 11.85")
    assert r["verdict"] == "uncertain"
    assert gl.CONTRADICTS_THRESHOLD <= r["score"] <= gl.SUPPORTED_THRESHOLD


# ── 16. check_claim empty source -> uncertain ────────────────────────────
def test_check_claim_empty_source_returns_uncertain() -> None:
    r = gl.check_claim("台積電 Q1 EPS 12", "")
    assert r["verdict"] == "uncertain"
    assert r["score"] == 0.0


# ── 17. score_response aggregates + low_score_claims ─────────────────────
def test_score_response_aggregates_with_low_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有兩句 claim：一句明顯支持，一句明顯不支持。"""
    # cosine 用 fake，依輸入內容回不同分。我們改用 patch check_claim 直接控分。
    real_check = gl.check_claim
    call_n = {"i": 0}

    def fake_check(claim: str, source: str) -> dict[str, Any]:
        i = call_n["i"]
        call_n["i"] += 1
        # 第一次呼叫高分、第二次低分
        if i == 0:
            return {
                "score": 0.85,
                "signals": {
                    "keyword": 0.8,
                    "cosine": 0.9,
                    "number_match": 1.0,
                    "entity_match": 1.0,
                },
                "verdict": "supported",
            }
        return {
            "score": 0.2,
            "signals": {
                "keyword": 0.1,
                "cosine": 0.1,
                "number_match": 0.0,
                "entity_match": 0.0,
            },
            "verdict": "contradicts",
        }

    monkeypatch.setattr(gl, "check_claim", fake_check)
    monkeypatch.setattr(gl, "_best_source_for_claim", lambda c, s: (s[0], 0))

    result = gl.score_response(
        "第一句明天會下雨。第二句月亮是綠色的。",
        ["氣象局報告"],
    )
    assert result["score_avg"] == pytest.approx((0.85 + 0.2) / 2, abs=0.01)
    assert len(result["low_score_claims"]) == 1
    assert "綠色" in result["low_score_claims"][0]["claim"]
    assert result["low_score_claims"][0]["verdict"] == "contradicts"
    assert len(result["verified"]) == 2
    # restore not strictly needed thanks to monkeypatch context
    _ = real_check


# ── 18. score_response empty response -> 1.0 ──────────────────────────────
def test_score_response_empty_response_returns_one() -> None:
    r = gl.score_response("", ["src"])
    assert r["score_avg"] == 1.0
    assert r["verified"] == []
    assert r["low_score_claims"] == []


# ── 19. score_response empty sources but has claims -> 0.0 ────────────────
def test_score_response_empty_sources_returns_zero() -> None:
    r = gl.score_response("台積電 Q1 EPS 11.85 元。", [])
    assert r["score_avg"] == 0.0
    assert len(r["low_score_claims"]) >= 1
    assert all(c["verdict"] == "uncertain" for c in r["low_score_claims"])


# ── 20. real-integration canary (no mock) ────────────────────────────────
def test_real_integration_canary_eps_uncertain() -> None:
    """驗收 spec 指定的 sample：
        claim  = "台積電 Q1 EPS 12 元"
        source = "台積電 Q1 EPS 11.85"
    EPS 數字不一致 (12 vs 11.85，差 1.27%) → 應該落在 uncertain。

    這個 test 真的會 lazy-load sentence-transformers，所以比較重，但
    每個 session 只會載一次 model。
    """
    r = gl.check_claim("台積電 Q1 EPS 12 元", "台積電 Q1 EPS 11.85")
    assert r["verdict"] == "uncertain", (
        f"expected uncertain, got {r['verdict']} (score={r['score']:.3f}, "
        f"signals={r['signals']})"
    )
    # number_match 應該 miss，因為 12 ≠ 11.85 (>1% diff)
    assert r["signals"]["number_match"] == 0.0
