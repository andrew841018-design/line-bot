"""Pure-local LLM router — Gemini quota 爆時 graceful degrade，零雲端 LLM 依賴。

Tier 1: Gemini main（line_bot 預設）— main.py _llm_chat 走這條
Tier 2: local LLM (Qwen2.5-14B-Instruct via mlx) — 自由問答 + 自家 LoRA
Tier 3: RAG retrieval（過去對話相似 retrieve）— 沒 LLM 也能答 facts
Tier 4: lite_reply（規則式 fallback）— 最後 safety net
"""
from __future__ import annotations

import logging

logger = logging.getLogger("llm_router")


def _gemini_quota_exhausted() -> bool:
    """讀 main._quota_exhausted（既有）。失敗回 False（讓 router 試 Gemini）。"""
    try:
        import main as _main
        return bool(_main._quota_exhausted())
    except Exception as e:
        logger.warning("read gemini quota state failed: %s", e)
        return False


def _maybe_local_ensemble(user_input, context) -> str | None:
    """高價值 query → 本機 14B 多次採樣 + judge / self-consistency / ToT。

    觸發條件命中才走（避免一律慢 30+ 秒）；module 不可用 → 回 None 走下層。
    """
    text = user_input if isinstance(user_input, str) else _extract_text(user_input)
    try:
        import local_ensemble as le
        if le.should_tot(text):
            return le.tree_of_thoughts(text) or None
        if le.should_self_consistency(text):
            return le.self_consistency(text) or None
        if le.should_ensemble(text):
            return le.local_ensemble_chat(text) or None
    except ImportError:
        return None
    except Exception as e:
        logger.warning("local_ensemble path failed: %s", e)
    return None


def _try_local_llm(user_input, context) -> str | None:
    """Tier 2 — local LLM。

    優先走 ReAct agent (agent_loop.Agent) 讓 14B 級 local LLM 能 call tools，
    若 agent 不可用 / 失敗 / 回應過短 → graceful degrade 到原生 local_llm.chat()。
    """
    text = user_input if isinstance(user_input, str) else _extract_text(user_input)

    # 先試 ReAct agent（給 local LLM 工具能力）
    try:
        from agent_loop import Agent
        agent = Agent()
        result = agent.run(text, context=context)
        if result and len(result.strip()) > 5:
            return result
    except ImportError:
        logger.info("agent_loop 不可用，直接走 local_llm.chat")
    except Exception as e:
        logger.warning("agent_loop fallback failed: %s, degrade 到 local_llm.chat", e)

    # 退路：直接 local_llm.chat（原本邏輯）
    try:
        from local_llm import chat as local_chat
        result = local_chat(text, context=context)
        if result and len(result.strip()) > 5:
            return result
    except ImportError:
        logger.info("local_llm 未安裝（fallback 不可用）")
    except Exception as e:
        logger.warning("local_llm fallback failed: %s", e)
    return None


def _try_rag(user_input) -> str | None:
    """Tier 3"""
    try:
        from rag_retriever import retrieve, format_rag_response
        text = user_input if isinstance(user_input, str) else _extract_text(user_input)
        hits = retrieve(text, k=3)
        return format_rag_response(text, hits)
    except ImportError:
        logger.info("rag_retriever 未建（fallback 不可用）")
    except Exception as e:
        logger.warning("RAG fallback failed: %s", e)
    return None


def _try_lite_reply(user_input, context) -> str | None:
    """Tier 4"""
    try:
        from lite_reply import lite_reply
        text = user_input if isinstance(user_input, str) else _extract_text(user_input)
        return lite_reply(text, context=context)
    except ImportError:
        return None
    except Exception as e:
        logger.warning("lite_reply fallback failed: %s", e)
    return None


def _extract_text(user_input) -> str:
    """從 MessageInput（str | Part | list）抽 text"""
    if isinstance(user_input, str):
        return user_input
    try:
        from gemini_client import _extract_text as _ge
        return _ge(user_input)
    except Exception:
        return str(user_input)[:500]


def fallback_chat(
    user_input,
    context: list = None,
    facts: list = None,
    persona_notes: list = None,
    group_id: str | None = None,
) -> str:
    """Quota 爆時的 waterfall。回字串（空 = 全敗）。"""
    context = context or []

    # Tier 1.5: 高價值 query 走 ensemble（觸發詞才打）
    out = _maybe_local_ensemble(user_input, context)
    if out:
        return out

    # Tier 2: local LLM
    out = _try_local_llm(user_input, context)
    if out:
        return out

    # Tier 3: RAG
    out = _try_rag(user_input)
    if out:
        return out

    # Tier 4: lite_reply
    out = _try_lite_reply(user_input, context)
    if out:
        return out

    return ""


def smart_chat(
    user_input,
    context: list,
    facts: list,
    persona_notes: list = None,
    group_id: str | None = None,
) -> str:
    """主入口。Gemini 沒爆 → main，爆了 → fallback waterfall（純本機）。

    這個函式是 main._llm_chat 的後續演進版（更詳細的 fallback chain）。
    """
    if _gemini_quota_exhausted():
        return fallback_chat(
            user_input,
            context=context,
            facts=facts,
            persona_notes=persona_notes,
            group_id=group_id,
        )

    # Gemini main path
    try:
        import gemini_client
        return gemini_client.chat(
            user_input, context, facts,
            persona_notes=persona_notes, group_id=group_id,
        )
    except Exception as e:
        logger.warning("Gemini main failed, falling back: %s", e)
        return fallback_chat(
            user_input,
            context=context,
            facts=facts,
            persona_notes=persona_notes,
            group_id=group_id,
        )
