"""rag_graph — LangGraph orchestration of line_bot RAG flow.

Phase 2B.3 of Phase 2 refactor (§3 chain approved). Wraps existing top-level
functions (embedding_recall.retrieve / retrieve_case_pairs, gemini_client._run)
as a LangGraph for visualization + traceability.

Behavior contract: identical to gemini_client.chat() pre-Phase 2B.4 wire.
Used only when USE_RAG_GRAPH env is set (Phase 2B.4 feature flag).

Phase 2B.5: model is passed via `config={"configurable": {"model": ...}}`
to `invoke()`, NOT via state. langgraph propagates `config` to EVERY node
whose signature accepts it — treat as sensitive: do NOT log `config`
from any node (would leak future tenant_id / billing keys).

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

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from gemini_core import MIN_RECALL_LEN, _NEWS_CASE_RE, _RunKwargs

logger = logging.getLogger(__name__)


class RagInput(TypedDict, total=False):
    """Phase 2B.6: caller-set subset of RagState used by
    `gemini_client._chat_via_graph` to build the invoke input.

    Documentation-only boundary (per Phase 2B.6 GP2 NIT): TypedDict has
    no runtime isolation — the single trusted construction site is
    `gemini_client._chat_via_graph`. If a future contributor adds a
    sensitive field (e.g., tenant_id) to RagState (node-set output),
    do NOT extend RagInput to include it — keep the input surface
    minimal and bounded.
    """
    group_id: Optional[str]
    user_input: object  # str | types.Part | list (multi-modal)
    context: list
    facts: list
    persona_notes: Optional[list]


class RagState(RagInput, total=False):
    """Full mutable state flowing through the graph nodes — inherits the
    5 caller-set fields from RagInput plus 5 node-derived / output fields.

    Per §3 chain GP1 #6: last-write-wins default merging is sufficient since
    each field has a single writer node (no Annotated reducer needed).

    Phase 2B.5: `model` removed from state — now passed via RunnableConfig.
    Phase 2B.6: split into RagInput (caller-set) + this (node-set superset).
    """
    # Cached / derived
    user_text: str
    # Retrieved
    recall_hits: Optional[list]
    case_hits: Optional[list]
    # Output
    response: str
    # Reserved for Phase 2B.7+ LangSmith integration (per GP2 #10)
    trace_id: Optional[str]


def _node_extract_text(state: RagState) -> dict:
    """Extract plain user_text from user_input (multi-modal Part list → text-only)."""
    from gemini_client import _extract_text
    return {"user_text": _extract_text(state["user_input"])}


def _node_semantic_retrieve(state: RagState) -> dict:
    """Silent-fail recall of past user messages, scoped to group_id.

    Per §3 chain GP1 #5: replicate exactly the existing len>=4 + group_id guards.
    """
    gid = state.get("group_id")
    if not gid:
        return {"recall_hits": None}
    user_text = state.get("user_text", "")
    if not user_text or len(user_text.strip()) < MIN_RECALL_LEN:
        return {"recall_hits": None}
    try:
        import embedding_recall
        hits = embedding_recall.retrieve(gid, user_text) or None
        return {"recall_hits": hits}
    except Exception as e:
        logger.warning("semantic_retrieve failed: %s", e)
        return {"recall_hits": None}


def _node_case_retrieve(state: RagState) -> dict:
    """Silent-fail retrieve of similar (user_msg, bot_reply) pairs for few-shot.

    Phase 2B.4 post-impl GP3 finding 8a: mirror the inline gate FULLY —
    `group_id` AND `len(strip) >= MIN_RECALL_LEN` AND `_NEWS_CASE_RE.search`.
    The router `_route_after_semantic` enforces all three, but defense-in-depth
    here prevents parity break if a future edge bypasses the router.
    """
    gid = state.get("group_id")
    if not gid:
        return {"case_hits": None}
    user_text = state.get("user_text", "")
    if not user_text or len(user_text.strip()) < MIN_RECALL_LEN:
        return {"case_hits": None}
    if not _NEWS_CASE_RE.search(user_text):
        return {"case_hits": None}
    try:
        import embedding_recall
        hits = embedding_recall.retrieve_case_pairs(gid, user_text) or None
        return {"case_hits": hits}
    except Exception as e:
        logger.warning("case_retrieve failed: %s", e)
        return {"case_hits": None}


def _route_after_semantic(state: RagState) -> str:
    """Conditional edge: short-text OR non-NEWS_CASE → skip to generate;
    otherwise → case_retrieve.

    Phase 2B.4 §3 chain (codex+gemini+GP2 BLOCKER consensus): mirror the exact
    inline gate in `chat()` — both `len(strip) >= MIN_RECALL_LEN` AND
    `_NEWS_CASE_RE.search` must hold to call case_pairs. Short NEWS_CASE
    text (e.g., "AI", "病") must skip case_retrieve to preserve parity.
    """
    user_text = state.get("user_text", "")
    if user_text and len(user_text.strip()) >= MIN_RECALL_LEN and _NEWS_CASE_RE.search(user_text):
        return "case_retrieve"
    return "generate"


def _node_generate(state: RagState, config: Optional[RunnableConfig] = None) -> dict:
    """Invoke Gemini model via top-level _run (3-attempt retry + Chinese
    rewrite + quality_gate stay node-internal per GP1 #2).

    Phase 2B.5 (codex+gemini+GP2 §3 chain): model is read ONLY from
    `config["configurable"]["model"]` — NOT from state. Allowlist enforced
    fail-closed: anything other than the caller's active main/lite model
    raises ValueError. The annotation
    `Optional[RunnableConfig]` (NOT `RunnableConfig | None`) is mandatory:
    `from __future__ import annotations` would stringify the union and
    defeat langgraph 1.2.0's signature introspection that injects config
    (verified at .venv/.../langgraph/_internal/_runnable.py:147,320,373).

    Phase 2B.4 §3 chain (codex+gemini+GP2 BLOCKER consensus): 503 /
    429-PerDay fallback to lite model lives HERE inside the node, NOT
    wrapping the whole graph invoke. Re-uses already-resolved
    recall_hits/case_hits without re-traversing retrieval nodes. Mirrors
    gemini_client.chat() inline fallback byte-for-byte.

    DO NOT log `config` from this node — it is propagated to all nodes
    and may carry sensitive future fields (tenant_id, API keys).
    """
    from config import settings as config_settings
    from gemini_client import _run, settings as caller_settings
    configurable = (config or {}).get("configurable") or {}
    main_model = getattr(caller_settings, "gemini_model", config_settings.gemini_model)
    lite_model = getattr(
        caller_settings, "gemini_light_model", config_settings.gemini_light_model
    )
    allowed_models = {main_model, lite_model}
    model = configurable.get("model") or main_model
    if model not in allowed_models:
        raise ValueError(f"Disallowed model: {model!r}")
    run_kwargs: _RunKwargs = {
        "user_input": state["user_input"],
        "context": state.get("context", []),
        "facts": state.get("facts", []),
        "persona_notes": state.get("persona_notes"),
        "recall_hits": state.get("recall_hits"),
        "case_hits": state.get("case_hits"),
        "group_id": state.get("group_id"),
    }
    # Fallback intentionally in-node (Phase 2B.4 post-impl GP3 Q3): graph-level
    # conditional edge would require shuttling run_kwargs through state or
    # re-traversing retrieval — both break parity with the inline path. See
    # test_node_generate_503_falls_back_to_lite_with_same_kwargs.
    try:
        response = _run(model, **run_kwargs)
    except Exception as e:
        err = str(e)
        is_503 = "503" in err
        is_429_perday = ("429" in err or "RESOURCE_EXHAUSTED" in err) and (
            "PerDay" in err or "free_tier_requests" in err
        )
        if (is_503 or is_429_perday) and model != lite_model:
            reason = "503" if is_503 else "429 daily quota"
            logger.warning(
                "rag_graph generate: gemini main model %s exhausted, fallback to %s",
                reason, lite_model,
            )
            # lite_model is in allowlist by construction; no re-check needed.
            response = _run(lite_model, **run_kwargs)
        else:
            raise
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
    # Per §3 chain GP2 #2 + Phase 2B.4 post-impl codex NIT: explicit
    # `checkpointer=None` to match the module docstring contract and
    # document intent at the call site (default is also None, but
    # explicit prevents a future contributor from adding one by habit).
    return g.compile(checkpointer=None)


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
