"""rag_graph — LangGraph orchestration of line_bot RAG flow.

Phase 2B.3 of Phase 2 refactor (§3 chain approved). Wraps existing top-level
functions (embedding_recall.retrieve / retrieve_case_pairs, gemini_client._run)
as a LangGraph for visualization + traceability.

Behavior contract: identical to gemini_client.chat() pre-Phase 2B.4 wire.
Used only when USE_RAG_GRAPH env is set (Phase 2B.4 feature flag).

DO NOT add checkpointer — state carries non-serializable Gemini SDK types
(genai.types.Part for multi-modal user_input; chat_session lives inside _run).
Distributed persistence / SqliteSaver / MemorySaver will silently break
on pickling and is enforced by `compile(checkpointer=None)` below.

Per §3 chain GP2 finding #2: this is a hard contract, not a recommendation.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)


class RagState(TypedDict, total=False):
    """Mutable state flowing through the graph nodes.

    Per §3 chain GP1 #6: last-write-wins default merging is sufficient since
    each field has a single writer node (no Annotated reducer needed).
    """
    # Inputs (set before invoke)
    group_id: Optional[str]
    user_input: object  # str | types.Part | list (multi-modal)
    context: list
    facts: list
    persona_notes: Optional[list]
    model: str
    # Cached / derived
    user_text: str
    # Retrieved
    recall_hits: Optional[list]
    case_hits: Optional[list]
    # Output
    response: str
    # Reserved for Phase 2B.5+ LangSmith integration (per GP2 #10)
    trace_id: Optional[str]


def _node_extract_text(state: RagState) -> dict:
    """Extract plain user_text from user_input (multi-modal Part list → text-only)."""
    from gemini_client import _extract_text
    return {"user_text": _extract_text(state["user_input"])}


def _node_semantic_retrieve(state: RagState) -> dict:
    """Silent-fail recall of past user messages, scoped to group_id.

    Per §3 chain GP1 #5: replicate exactly the existing len>=4 + group_id guards.
    """
    if not state.get("group_id"):
        return {"recall_hits": None}
    user_text = state.get("user_text", "")
    if not user_text or len(user_text.strip()) < 4:
        return {"recall_hits": None}
    try:
        import embedding_recall
        hits = embedding_recall.retrieve(state["group_id"], user_text) or None
        return {"recall_hits": hits}
    except Exception as e:
        logger.warning("semantic_retrieve failed: %s", e)
        return {"recall_hits": None}


def _node_case_retrieve(state: RagState) -> dict:
    """Silent-fail retrieve of similar (user_msg, bot_reply) pairs for few-shot."""
    if not state.get("group_id"):
        return {"case_hits": None}
    user_text = state.get("user_text", "")
    try:
        import embedding_recall
        hits = embedding_recall.retrieve_case_pairs(state["group_id"], user_text) or None
        return {"case_hits": hits}
    except Exception as e:
        logger.warning("case_retrieve failed: %s", e)
        return {"case_hits": None}


def _route_after_semantic(state: RagState) -> str:
    """Conditional edge: NEWS_CASE_RE.search → case_retrieve; else skip to generate.

    Per §3 chain GP1 #5: replicate the exact `_NEWS_CASE_RE.search(user_text)` check.
    """
    from gemini_core import _NEWS_CASE_RE
    user_text = state.get("user_text", "")
    if user_text and _NEWS_CASE_RE.search(user_text):
        return "case_retrieve"
    return "generate"


def _node_generate(state: RagState) -> dict:
    """Invoke Gemini model via top-level _run (which has internal 3-retry +
    Chinese rewrite + quality_gate per GP1 #2: retries stay node-internal).

    503/429-PerDay fallback to lite model is OUTSIDE the graph (in chat() caller)
    per GP1 #2: don't model fallback as graph edges since chat_session continuity
    matters for retries.
    """
    from config import settings
    from gemini_client import _run
    model = state.get("model") or settings.gemini_model
    response = _run(
        model,
        user_input=state["user_input"],
        context=state.get("context", []),
        facts=state.get("facts", []),
        persona_notes=state.get("persona_notes"),
        recall_hits=state.get("recall_hits"),
        case_hits=state.get("case_hits"),
        group_id=state.get("group_id"),
    )
    return {"response": response}


def _build_graph():
    """Build + compile the RAG graph. Checkpointer prohibited."""
    g = StateGraph(RagState)
    g.add_node("extract_text", _node_extract_text)
    g.add_node("semantic_retrieve", _node_semantic_retrieve)
    g.add_node("case_retrieve", _node_case_retrieve)
    g.add_node("generate", _node_generate)
    g.set_entry_point("extract_text")
    g.add_edge("extract_text", "semantic_retrieve")
    g.add_conditional_edges(
        "semantic_retrieve",
        _route_after_semantic,
        {"case_retrieve": "case_retrieve", "generate": "generate"},
    )
    g.add_edge("case_retrieve", "generate")
    g.add_edge("generate", END)
    # Per §3 chain GP2 #2: NO checkpointer; state has non-serializable Parts
    return g.compile()


@lru_cache(maxsize=1)
def get_graph():
    """Lazy compile (per §3 chain GP2 #7): avoids import-time crash blocking
    the 15 importers of gemini_client; cache clearable for tests via
    `get_graph.cache_clear()`.
    """
    return _build_graph()


def export_mermaid() -> str:
    """Export current graph as Mermaid markdown string for README / demo."""
    return get_graph().get_graph().draw_mermaid()
