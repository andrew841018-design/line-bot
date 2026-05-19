"""Phase 2B.3 — tests for rag_graph module.

Covers:
1. Graph compiles + caches via lru_cache
2. Mermaid export
3. Node behavior (extract_text, semantic_retrieve, case_retrieve)
4. Conditional edge routing (NEWS_CASE_RE)
5. Checkpointer prohibition contract (§3 chain GP2 #2 mandate)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed — Phase 2B.3 dep")

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_graph  # noqa: E402


# ---------- Graph compile + caching ----------

def test_get_graph_caches():
    """lru_cache returns same compiled instance."""
    rag_graph.get_graph.cache_clear()
    g1 = rag_graph.get_graph()
    g2 = rag_graph.get_graph()
    assert g1 is g2


def test_graph_has_expected_nodes():
    """All 4 nodes registered."""
    rag_graph.get_graph.cache_clear()
    g = rag_graph.get_graph()
    nodes = set(g.get_graph().nodes)
    expected = {"extract_text", "semantic_retrieve", "case_retrieve", "generate"}
    assert expected.issubset(nodes), f"Missing nodes: {expected - nodes}"


def test_export_mermaid_returns_valid_markdown():
    """Mermaid export contains all node names + flowchart syntax."""
    md = rag_graph.export_mermaid()
    assert isinstance(md, str)
    for node in ("extract_text", "semantic_retrieve", "case_retrieve", "generate"):
        assert node in md, f"{node} missing from mermaid output"


# ---------- Node behavior ----------

def test_node_semantic_retrieve_no_group_id():
    state = {"user_input": "hi", "user_text": "hi", "group_id": None}
    assert rag_graph._node_semantic_retrieve(state) == {"recall_hits": None}


def test_node_semantic_retrieve_short_text():
    state = {"user_input": "嗨", "user_text": "嗨", "group_id": "C_test"}
    assert rag_graph._node_semantic_retrieve(state) == {"recall_hits": None}


def test_node_semantic_retrieve_strip_only_whitespace():
    state = {"user_input": "   ", "user_text": "   ", "group_id": "C_test"}
    assert rag_graph._node_semantic_retrieve(state) == {"recall_hits": None}


@patch("embedding_recall.retrieve", return_value=[])
def test_node_semantic_retrieve_empty_becomes_none(mock_retrieve):
    """Preserve `or None` semantics: empty list → None (GP backfill S3)."""
    state = {"user_input": "今天晚餐", "user_text": "今天晚餐吃什麼好呢", "group_id": "C_test"}
    result = rag_graph._node_semantic_retrieve(state)
    assert result == {"recall_hits": None}
    mock_retrieve.assert_called_once_with("C_test", "今天晚餐吃什麼好呢")


@patch("embedding_recall.retrieve", return_value=[{"text": "past"}])
def test_node_semantic_retrieve_returns_hits(mock_retrieve):
    state = {"user_input": "今天晚餐", "user_text": "今天晚餐吃什麼好呢", "group_id": "C_test"}
    result = rag_graph._node_semantic_retrieve(state)
    assert result == {"recall_hits": [{"text": "past"}]}


@patch("embedding_recall.retrieve", side_effect=RuntimeError("network down"))
def test_node_semantic_retrieve_silent_fail(mock_retrieve):
    """Silent-fail invariant (GP1 #5)."""
    state = {"user_input": "今天", "user_text": "今天晚餐吃什麼好呢", "group_id": "C_test"}
    assert rag_graph._node_semantic_retrieve(state) == {"recall_hits": None}


# ---------- Conditional edge routing ----------

def test_route_after_semantic_news_case():
    assert rag_graph._route_after_semantic({"user_text": "這個保險方案值得買嗎"}) == "case_retrieve"


def test_route_after_semantic_non_news_case():
    assert rag_graph._route_after_semantic({"user_text": "今天晚餐吃什麼好呢"}) == "generate"


def test_route_after_semantic_empty_text():
    assert rag_graph._route_after_semantic({"user_text": ""}) == "generate"


def test_route_after_semantic_missing_text_key():
    assert rag_graph._route_after_semantic({}) == "generate"


# ---------- Checkpointer prohibition contract ----------

def test_state_has_no_chat_session_field():
    """chat_session deliberately NOT in state — lives inside _run.

    Per §3 chain GP1 #7: stateful SDK chat_session is non-picklable;
    keeping it out of state enforces the no-checkpointer contract.
    """
    fields = rag_graph.RagState.__annotations__
    assert "chat_session" not in fields, \
        "chat_session must NOT be in RagState (non-picklable; would break checkpointer)"


def test_module_docstring_warns_no_checkpointer():
    """Module docstring must explicitly forbid checkpointer (§3 chain GP2 #2)."""
    assert "DO NOT add checkpointer" in rag_graph.__doc__


# ---------- Phase 2B.4 — _node_case_retrieve defensive length guard ----------

def test_node_case_retrieve_short_text():
    """Defensive guard inside the node (Phase 2B.4 §3 chain GP1+GP2): even if
    a future edge bypasses _route_after_semantic, short text must NOT call
    retrieve_case_pairs.
    """
    state = {"user_input": "AI", "user_text": "AI", "group_id": "C_test"}
    assert rag_graph._node_case_retrieve(state) == {"case_hits": None}


def test_node_case_retrieve_no_group_id():
    state = {"user_input": "保險方案分析", "user_text": "保險方案分析", "group_id": None}
    assert rag_graph._node_case_retrieve(state) == {"case_hits": None}


# ---------- Phase 2B.4 — _route_after_semantic length guard (BLOCKER 1 fix) ----------

def test_route_after_semantic_short_news_case_skips():
    """BLOCKER 1 fix: short NEWS_CASE text ("AI" len=2) must route to generate,
    not case_retrieve — mirror inline gate at gemini_client.py:1231.
    """
    assert rag_graph._route_after_semantic({"user_text": "AI"}) == "generate"
    assert rag_graph._route_after_semantic({"user_text": "病"}) == "generate"


def test_route_after_semantic_long_news_case_routes():
    """Sanity: full-length NEWS_CASE still routes to case_retrieve."""
    assert rag_graph._route_after_semantic({"user_text": "這個保險方案值得買嗎"}) == "case_retrieve"


# ---------- Phase 2B.4 — _node_generate 503/429 fallback inside node (BLOCKER 2 fix) ----------

def test_node_generate_503_falls_back_to_lite_with_same_kwargs():
    """BLOCKER 2 fix: 503 on main model triggers ONE retry with lite model
    using the EXACT same run_kwargs (recall_hits / case_hits / facts /
    context already resolved in state). Retrieval nodes must NOT re-run.
    """
    from unittest.mock import patch as _patch
    base_state = {
        "user_input": "hi there",
        "context": [("user", "old")],
        "facts": ["fact1"],
        "persona_notes": None,
        "recall_hits": [{"text": "past"}],
        "case_hits": None,
        "group_id": "C_test",
        "model": "gemini-2.0-flash",
    }
    calls = []

    def fake_run(model, **kwargs):
        calls.append((model, kwargs))
        if model == "gemini-2.0-flash":
            raise RuntimeError("503 service unavailable")
        return "lite reply"

    with _patch("gemini_client._run", side_effect=fake_run):
        result = rag_graph._node_generate(base_state)

    assert result == {"response": "lite reply"}
    assert len(calls) == 2, f"expected exactly 2 _run calls (main + lite), got {len(calls)}"
    main_model, main_kwargs = calls[0]
    lite_model, lite_kwargs = calls[1]
    assert main_model == "gemini-2.0-flash"
    assert lite_model != main_model
    # CRITICAL: lite retry must receive byte-identical resolved kwargs
    assert main_kwargs == lite_kwargs, "lite retry must reuse same kwargs (no re-retrieval)"
    assert main_kwargs["recall_hits"] == [{"text": "past"}]


def test_node_generate_429_perday_falls_back():
    from unittest.mock import patch as _patch
    state = {
        "user_input": "hi", "context": [], "facts": [], "persona_notes": None,
        "recall_hits": None, "case_hits": None, "group_id": None,
        "model": "gemini-2.0-flash",
    }

    def fake_run(model, **kwargs):
        if model == "gemini-2.0-flash":
            raise RuntimeError("429 RESOURCE_EXHAUSTED PerDay free_tier_requests")
        return "lite reply"

    with _patch("gemini_client._run", side_effect=fake_run):
        result = rag_graph._node_generate(state)
    assert result == {"response": "lite reply"}


def test_node_generate_non_fallback_exception_propagates():
    """Non-503/non-429 exception must propagate unchanged (no spurious fallback)."""
    from unittest.mock import patch as _patch
    state = {
        "user_input": "hi", "context": [], "facts": [], "persona_notes": None,
        "recall_hits": None, "case_hits": None, "group_id": None,
        "model": "gemini-2.0-flash",
    }
    with _patch("gemini_client._run", side_effect=RuntimeError("permission denied")):
        with pytest.raises(RuntimeError, match="permission denied"):
            rag_graph._node_generate(state)


def test_node_generate_503_when_already_lite_does_not_recurse():
    """If model is already lite and it 503s, do NOT fall back (avoid infinite recursion)."""
    from unittest.mock import patch as _patch
    from config import settings
    state = {
        "user_input": "hi", "context": [], "facts": [], "persona_notes": None,
        "recall_hits": None, "case_hits": None, "group_id": None,
        "model": settings.gemini_light_model,
    }
    with _patch("gemini_client._run", side_effect=RuntimeError("503 unavailable")):
        with pytest.raises(RuntimeError, match="503"):
            rag_graph._node_generate(state)
