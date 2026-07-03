"""Phase 2B.1 — Golden tests pinning chat() pre-refactor behavior.

These tests are the safety net for the gemini_core.py extraction +
rag_graph.py wrapping refactor (Phase 2B sub-phases 2-6). They mock
external deps (Gemini SDK, embedding_recall, stock_quote) and assert
the exact contracts of chat() under current implementation.

After each Phase 2B sub-phase commit, these tests MUST stay green
byte-identically. Any drift = behavior change → halt + investigate.

Scenarios:
1. _NEWS_CASE_RE pattern pinned (catch accidental regex drift during extraction)
2. semantic_retrieve / retrieve_case_pairs call gates (text length, NEWS_CASE match)
3. `or None` semantics: empty retrieve list → None passed to _build_config
4. Happy path returns string
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Set dummy env BEFORE any line_bot import (pydantic-settings reads .env at import time)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

# Make line_bot/ importable from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gemini_client as gc  # noqa: E402


# ---------- 1. Regex drift guard ----------

def test_news_case_regex_pattern_pinned():
    """Pin the exact NEWS_CASE regex pattern (catches accidental edit during extraction)."""
    expected_pattern = (
        r"新聞|報導|案例|個案|研究|論文|文章|貼文|"
        r"保險|保單|投保|理賠|"
        r"投資|股市|理財|"
        r"醫療|醫生|醫院|疫苗|藥物|手術|症狀|病|健康|養生|"
        r"教育|升學|考試|教改|"
        r"法律|法案|法條|判決|訴訟|"
        r"消費|商品|品牌|"
        r"房地產|房市|房價|租屋|"
        r"AI|人工智慧|詐騙|騙局|"
        r"分析|評論|心得|解析"
    )
    assert gc._NEWS_CASE_RE.pattern == expected_pattern


def test_news_case_regex_matches_known_keywords():
    samples = ["新聞報導", "保險理賠", "投資建議", "醫療諮詢", "房價分析", "AI 詐騙"]
    for s in samples:
        assert gc._NEWS_CASE_RE.search(s), f"Should match: {s!r}"


def test_news_case_regex_skips_casual_chat():
    samples = ["你好", "謝謝", "今天天氣不錯", "晚安"]
    for s in samples:
        assert not gc._NEWS_CASE_RE.search(s), f"Should NOT match: {s!r}"


def test_system_instruction_injects_runtime_taipei_date(monkeypatch):
    """Main Gemini chat must not rely on stale model/training-year context."""
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 7, 2, 10, 30, 45)
            return base.replace(tzinfo=tz) if tz else base

    monkeypatch.setattr(gc, "datetime", FixedDateTime)

    out = gc._build_system_instruction(facts=[], persona_notes=[])

    assert "即時時間基準" in out
    assert "目前台灣時間：2026-07-02 10:30:45" in out
    assert "今天日期：2026年7月2日" in out
    assert "不可沿用訓練資料、舊對話或舊回答中的年份" in out


# ---------- Mock fixtures ----------

class _MockResponse:
    """Minimal mock for Gemini response object — mirrors fields chat() reads."""
    def __init__(self, text="這是個測試回覆。"):
        self.text = text
        self.candidates = []
        self.usage_metadata = MagicMock(
            prompt_token_count=10,
            candidates_token_count=20,
            total_token_count=30,
            thoughts_token_count=0,
        )
        self.grounding_metadata = None


def _mock_chat_session(reply_text="這是個測試回覆。"):
    sess = MagicMock()
    sess.send_message.return_value = _MockResponse(text=reply_text)
    return sess


# ---------- Phase 2B.4 — flag parametrization fixture ----------
#
# Parity contract (§3 chain consensus across codex+gemini+GP1+GP2):
#   USE_RAG_GRAPH=OFF and USE_RAG_GRAPH=ON must produce byte-identical
#   observable behavior under the same mocks for every gate test below.
#
# Why monkeypatch the settings object (not os.environ): pydantic-settings
# reads .env at import time (config.py:54 instantiates Settings once).
# Mutating os.environ post-import has no effect on settings.use_rag_graph.

@pytest.fixture(params=[False, True], ids=["inline", "rag_graph"])
def chat_mode(request, monkeypatch):
    monkeypatch.setattr(gc.settings, "use_rag_graph", request.param)
    if request.param:
        import rag_graph
        rag_graph.get_graph.cache_clear()  # fresh compile per parametrized run
    return request.param


# ---------- 2-5. Retrieve call gates + or-None semantics ----------

@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs")
@patch("embedding_recall.retrieve")
@patch("gemini_client._client")
def test_retrieve_skipped_for_short_text(mock_client, mock_retrieve, mock_case, _stock, chat_mode):
    """user_text strip length < 4 → both retrieves NOT called (both modes)."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="嗨嗨",  # len=2 after strip
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    mock_retrieve.assert_not_called()
    mock_case.assert_not_called()


@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs")
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._client")
def test_retrieve_skipped_when_no_group_id(mock_client, mock_retrieve, mock_case, _stock, chat_mode):
    """group_id=None → semantic recall entirely skipped (both modes)."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id=None,
    )
    mock_retrieve.assert_not_called()
    mock_case.assert_not_called()


@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs")
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._client")
def test_retrieve_called_for_normal_non_news_text(mock_client, mock_retrieve, mock_case, _stock, chat_mode):
    """Normal text (len>=4, no NEWS_CASE) → retrieve called, case_pairs NOT (both modes)."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    mock_retrieve.assert_called_once()
    mock_case.assert_not_called()


@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=[])
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._client")
def test_retrieve_case_pairs_called_for_news_case_text(mock_client, mock_retrieve, mock_case, _stock, chat_mode):
    """NEWS_CASE text → BOTH retrieves called (both modes)."""
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="這個保險方案值得買嗎",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    mock_retrieve.assert_called_once()
    mock_case.assert_called_once()


@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=[])
@patch("embedding_recall.retrieve", return_value=[])
@patch("gemini_client._build_config")
@patch("gemini_client._client")
def test_empty_recall_list_becomes_none(mock_client, mock_build, _ret, _case, _stock, chat_mode):
    """Current `or None` semantics: empty list → None passed to _build_config (both modes)."""
    mock_client.chats.create.return_value = _mock_chat_session()
    mock_build.return_value = MagicMock()
    gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    assert mock_build.called, "_build_config should be invoked"
    call_kwargs = mock_build.call_args.kwargs
    assert call_kwargs.get("recall_hits") is None, \
        f"Expected None (or-None semantics), got {call_kwargs.get('recall_hits')!r}"
    assert call_kwargs.get("case_hits") is None, \
        f"Expected None (or-None semantics for non-NEWS path), got {call_kwargs.get('case_hits')!r}"


# ---------- 6. Happy path smoke ----------

@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=None)
@patch("embedding_recall.retrieve", return_value=None)
@patch("gemini_client._client")
def test_chat_returns_string_on_happy_path(mock_client, _ret, _case, _stock, chat_mode):
    mock_client.chats.create.return_value = _mock_chat_session(reply_text="這是個測試回覆。")
    result = gc.chat(
        user_input="今天晚餐吃什麼好呢",
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    assert isinstance(result, str)
    assert len(result) > 0


# ---------- Phase 2B.4 §3 chain — parity-only divergence checks ----------

@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs")
@patch("embedding_recall.retrieve")
@patch("gemini_client._client")
def test_short_news_case_text_does_not_call_case_pairs(mock_client, mock_retrieve, mock_case, _stock, chat_mode):
    """BLOCKER 1 fix verification: short NEWS_CASE text ("AI", len=2) MUST skip
    case_retrieve in both modes. Pre-fix, graph route would have called
    retrieve_case_pairs while inline would not — parity break.
    """
    mock_client.chats.create.return_value = _mock_chat_session()
    gc.chat(
        user_input="AI",  # len=2 + NEWS_CASE match
        context=[],
        facts=[],
        group_id="C_test_group",
    )
    mock_retrieve.assert_not_called()
    mock_case.assert_not_called()


@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=None)
@patch("embedding_recall.retrieve", return_value=None)
@patch("gemini_client._run")
def test_non_fallback_exception_propagates(mock_run, _ret, _case, _stock, chat_mode):
    """GP1 NIT verification: non-503/non-429 exception in _run must bubble
    unchanged from both inline path and graph path.
    """
    mock_run.side_effect = RuntimeError("permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        gc.chat(
            user_input="今天晚餐吃什麼好呢",
            context=[],
            facts=[],
            group_id="C_test_group",
        )


@patch("stock_quote.get_contextual_quotes_text", return_value="")
@patch("embedding_recall.retrieve_case_pairs", return_value=None)
@patch("embedding_recall.retrieve", return_value=None)
@patch("gemini_client._client")
def test_multimodal_user_input_passes_through_unchanged(mock_client, _ret, _case, _stock, chat_mode):
    """Phase 2B.4 post-impl GP1 IMPORTANT: a list[str | Part-like] user_input
    must reach send_message as the SAME object (no coercion to str)
    in both inline and graph modes — multimodal contract preserved.
    """
    sess = _mock_chat_session()
    mock_client.chats.create.return_value = sess
    part_like = MagicMock()
    part_like.text = "AI 詐騙新聞解析"
    user_input = ["請看這張圖片分析", part_like]
    gc.chat(user_input=user_input, context=[], facts=[], group_id="C_test_group")
    # First send_message call is the original user_input (subsequent calls are
    # quality_gate / Chinese-rewrite retries with string prompts).
    first_call = sess.send_message.call_args_list[0].args
    assert first_call[0] is user_input, "first send_message must receive original user_input"


def test_run_kwargs_byte_identical_across_modes(monkeypatch):
    """Phase 2B.4 post-impl GP1 IMPORTANT: the true parity contract is that
    `_run` receives identical (model, kwargs) in both modes for the same
    input. Capture both, assert equality.
    """
    captured: dict[str, list] = {"inline": [], "graph": []}

    def _capture_for(mode):
        def fn(model, **kwargs):
            captured[mode].append((model, kwargs))
            return "reply"
        return fn

    def run_one(mode_name, flag):
        monkeypatch.setattr(gc.settings, "use_rag_graph", flag)
        if flag:
            import rag_graph
            rag_graph.get_graph.cache_clear()
        with patch("gemini_client._run", side_effect=_capture_for(mode_name)), \
             patch("embedding_recall.retrieve", return_value=[{"text": "past hit"}]), \
             patch("embedding_recall.retrieve_case_pairs", return_value=[{"u": "u1", "b": "b1"}]), \
             patch("stock_quote.get_contextual_quotes_text", return_value=""):
            gc.chat(
                user_input="保險方案分析心得",  # len>=4 + NEWS_CASE → both retrieves fire
                context=[("user", "hi"), ("assistant", "hello")],
                facts=["fact1", "fact2"],
                persona_notes=[{"note": "n1"}],
                group_id="C_test_group",
            )

    run_one("inline", False)
    run_one("graph", True)

    assert len(captured["inline"]) == 1, f"inline _run not called: {captured}"
    assert len(captured["graph"]) == 1, f"graph _run not called: {captured}"
    inline_model, inline_kwargs = captured["inline"][0]
    graph_model, graph_kwargs = captured["graph"][0]
    assert inline_model == graph_model, f"model differs: {inline_model!r} vs {graph_model!r}"
    assert inline_kwargs == graph_kwargs, \
        f"kwargs diverge:\n  inline: {inline_kwargs}\n  graph:  {graph_kwargs}"


@patch("stock_quote.get_contextual_quotes_text", return_value="【市場報價｜測試】\nNVDA: 180.00")
@patch("embedding_recall.retrieve_case_pairs", return_value=None)
@patch("embedding_recall.retrieve", return_value=None)
@patch("gemini_client._run")
def test_contextual_stock_quote_receives_context_and_injects_facts(mock_run, _ret, _case, mock_stock, chat_mode):
    """Stock quote prefetch must see recent context, not only current text."""
    mock_run.return_value = "reply"
    ctx = [("user", "我覺得 NVDA 會再突破"), ("assistant", "要看財報")]

    gc.chat(
        user_input="現在多少？",
        context=ctx,
        facts=["base_fact"],
        group_id="C_test_group",
    )

    assert mock_stock.call_count == 1
    assert mock_stock.call_args.args[0] == "現在多少？"
    assert mock_stock.call_args.kwargs["context"] == ctx
    assert mock_run.call_count == 1
    run_facts = mock_run.call_args.kwargs["facts"]
    assert any("【市場報價｜測試】" in f for f in run_facts)
