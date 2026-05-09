"""Unit tests for llm_router 4-tier waterfall (2026-05-03).

驗證：
  Tier 2 (local_llm)：available 命中 → 回 LLM 字串；ImportError → graceful degrade；
                       runtime error → graceful degrade；短 (<6) 回應視為無效
  Tier 3 (RAG)：retrieve + format_rag_response 正常 → 回字串；ImportError → degrade
  Tier 4 (lite_reply)：available 命中 → 回字串；ImportError / runtime → degrade
  fallback_chat waterfall：依序試 → 第一個非空字串就回；全敗回 ""
  smart_chat：quota 沒爆走 gemini；爆了走 fallback_chat
"""
from __future__ import annotations

import builtins
import sys
import types
from unittest import mock

import pytest

import llm_router


# ─── Tier 2: local LLM ─────────────────────────────────────────────────────


def test_try_local_llm_returns_response_when_valid(monkeypatch):
    """local_llm.chat 回有效（>5 chars）→ 回字串。"""
    fake = types.ModuleType("local_llm")
    fake.chat = lambda text, context=None: "這是 local LLM 的回答內容"
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    out = llm_router._try_local_llm("Hello", [])
    assert out == "這是 local LLM 的回答內容"


def test_try_local_llm_import_error_graceful(monkeypatch):
    """local_llm 不存在（ImportError）→ 回 None，不 raise。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "local_llm":
            raise ImportError("simulated absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "local_llm", raising=False)

    out = llm_router._try_local_llm("anything", [])
    assert out is None


def test_try_local_llm_runtime_error_graceful(monkeypatch):
    """local_llm.chat 拋 exception → 回 None。"""
    fake = types.ModuleType("local_llm")

    def boom(*a, **kw):
        raise RuntimeError("boom")

    fake.chat = boom
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    assert llm_router._try_local_llm("anything", []) is None


def test_try_local_llm_short_response_rejected(monkeypatch):
    """local_llm 回 <= 5 chars → 視為無效，回 None。"""
    fake = types.ModuleType("local_llm")
    fake.chat = lambda text, context=None: "hi"  # too short
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    assert llm_router._try_local_llm("anything", []) is None


def test_try_local_llm_extracts_text_from_non_string(monkeypatch):
    """user_input 非字串 → 用 _extract_text 抽出文字傳給 local LLM。"""
    fake = types.ModuleType("local_llm")
    captured: dict = {}

    def chat(text, context=None):
        captured["text"] = text
        return "local LLM gave a long enough reply"

    fake.chat = chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    # Mock _extract_text to confirm it gets called
    monkeypatch.setattr(llm_router, "_extract_text", lambda x: "extracted-text")
    out = llm_router._try_local_llm(["some", "list", "input"], [])
    assert captured["text"] == "extracted-text"
    assert out == "local LLM gave a long enough reply"


# ─── Tier 3: RAG ─────────────────────────────────────────────────────────


def test_try_rag_returns_formatted_string(monkeypatch):
    """rag_retriever 命中 → 回 format_rag_response 的字串。"""
    fake = types.ModuleType("rag_retriever")
    fake.retrieve = lambda text, k=3: [{"text": "前面的相似訊息", "similarity": 0.85}]
    fake.format_rag_response = lambda q, hits: "我之前看過類似的訊息：\n・「...」"
    monkeypatch.setitem(sys.modules, "rag_retriever", fake)

    out = llm_router._try_rag("某問題")
    assert out is not None
    assert "類似" in out


def test_try_rag_import_error_graceful(monkeypatch):
    """rag_retriever 沒裝（ImportError）→ 回 None。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "rag_retriever":
            raise ImportError("absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "rag_retriever", raising=False)

    assert llm_router._try_rag("anything") is None


def test_try_rag_runtime_error_graceful(monkeypatch):
    """rag_retriever.retrieve 拋 exception → 回 None。"""
    fake = types.ModuleType("rag_retriever")

    def boom(*a, **kw):
        raise RuntimeError("DB locked")

    fake.retrieve = boom
    fake.format_rag_response = lambda q, hits: "should not reach"
    monkeypatch.setitem(sys.modules, "rag_retriever", fake)

    assert llm_router._try_rag("anything") is None


def test_try_rag_no_hits_returns_none(monkeypatch):
    """retrieve 沒命中 → format_rag_response 回 None → tier 3 回 None。"""
    fake = types.ModuleType("rag_retriever")
    fake.retrieve = lambda text, k=3: []
    fake.format_rag_response = lambda q, hits: None
    monkeypatch.setitem(sys.modules, "rag_retriever", fake)

    assert llm_router._try_rag("anything") is None


# ─── Tier 4: lite_reply ──────────────────────────────────────────────────


def test_try_lite_reply_returns_response(monkeypatch):
    """lite_reply 命中 → 回字串。"""
    fake = types.ModuleType("lite_reply")
    fake.lite_reply = lambda text, context=None: "lite reply 命中"
    monkeypatch.setitem(sys.modules, "lite_reply", fake)

    assert llm_router._try_lite_reply("anything", []) == "lite reply 命中"


def test_try_lite_reply_import_error_graceful(monkeypatch):
    """lite_reply 沒裝（ImportError）→ 回 None。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "lite_reply":
            raise ImportError("absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "lite_reply", raising=False)

    assert llm_router._try_lite_reply("anything", []) is None


def test_try_lite_reply_runtime_error_graceful(monkeypatch):
    """lite_reply 拋 exception → 回 None。"""
    fake = types.ModuleType("lite_reply")

    def boom(*a, **kw):
        raise RuntimeError("boom")

    fake.lite_reply = boom
    monkeypatch.setitem(sys.modules, "lite_reply", fake)

    assert llm_router._try_lite_reply("anything", []) is None


# ─── Waterfall: fallback_chat ───────────────────────────────────────────


def test_fallback_chat_tier2_hits_first(monkeypatch):
    """Tier 2 命中 → 後面 Tier 3/4 不該被叫。"""
    monkeypatch.setattr(llm_router, "_try_local_llm", lambda u, c: "local-llm-hit")
    monkeypatch.setattr(
        llm_router, "_try_rag",
        lambda u: pytest.fail("Tier 3 不該被叫"),
    )
    monkeypatch.setattr(
        llm_router, "_try_lite_reply",
        lambda u, c: pytest.fail("Tier 4 不該被叫"),
    )
    assert llm_router.fallback_chat("query") == "local-llm-hit"


def test_fallback_chat_tier3_when_tier2_misses(monkeypatch):
    """Tier 2 None → Tier 3 命中 → 不該再叫 Tier 4。"""
    monkeypatch.setattr(llm_router, "_try_local_llm", lambda u, c: None)
    monkeypatch.setattr(llm_router, "_try_rag", lambda u: "rag-hit")
    monkeypatch.setattr(
        llm_router, "_try_lite_reply",
        lambda u, c: pytest.fail("Tier 4 不該被叫"),
    )
    assert llm_router.fallback_chat("query") == "rag-hit"


def test_fallback_chat_tier4_when_others_miss(monkeypatch):
    """Tier 2/3 None → Tier 4 命中。"""
    monkeypatch.setattr(llm_router, "_try_local_llm", lambda u, c: None)
    monkeypatch.setattr(llm_router, "_try_rag", lambda u: None)
    monkeypatch.setattr(llm_router, "_try_lite_reply", lambda u, c: "lite-hit")
    assert llm_router.fallback_chat("query") == "lite-hit"


def test_fallback_chat_all_fail_returns_empty_string(monkeypatch):
    """4 個 tier 全 None → 回空字串。"""
    monkeypatch.setattr(llm_router, "_try_local_llm", lambda u, c: None)
    monkeypatch.setattr(llm_router, "_try_rag", lambda u: None)
    monkeypatch.setattr(llm_router, "_try_lite_reply", lambda u, c: None)
    assert llm_router.fallback_chat("query") == ""


def test_fallback_chat_default_context_none(monkeypatch):
    """context=None 時不該 raise（預設成空 list）。"""
    captured = {}

    def stub_local(u, c):
        captured["context"] = c
        return "ok-result"

    monkeypatch.setattr(llm_router, "_try_local_llm", stub_local)
    out = llm_router.fallback_chat("query", context=None)
    assert out == "ok-result"
    assert captured["context"] == []


# ─── smart_chat ─────────────────────────────────────────────────────────


def test_smart_chat_uses_gemini_when_quota_ok(monkeypatch):
    """quota 沒爆 → 走 gemini_client.chat（不該走 fallback）。"""
    import main
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)

    fake_gemini = types.ModuleType("gemini_client")
    fake_gemini.chat = lambda u, ctx, facts, persona_notes=None, group_id=None: "gemini-out"
    monkeypatch.setitem(sys.modules, "gemini_client", fake_gemini)

    monkeypatch.setattr(
        llm_router, "fallback_chat",
        lambda *a, **kw: pytest.fail("不該走 fallback"),
    )

    out = llm_router.smart_chat("hello", [], [])
    assert out == "gemini-out"


def test_smart_chat_uses_fallback_when_quota_exhausted(monkeypatch):
    """quota 爆 → 走 fallback_chat 不碰 gemini。"""
    import main
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)

    fake_gemini = types.ModuleType("gemini_client")

    def explode(*a, **kw):
        pytest.fail("quota 爆時不該叫 gemini.chat")

    fake_gemini.chat = explode
    monkeypatch.setitem(sys.modules, "gemini_client", fake_gemini)

    monkeypatch.setattr(
        llm_router, "fallback_chat",
        lambda u, context=None, facts=None, persona_notes=None, group_id=None: "fb-out",
    )
    assert llm_router.smart_chat("hi", [], []) == "fb-out"


def test_smart_chat_falls_back_when_gemini_raises(monkeypatch):
    """quota 沒爆但 gemini.chat 噴 exception → 走 fallback。"""
    import main
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)

    fake_gemini = types.ModuleType("gemini_client")

    def boom(*a, **kw):
        raise RuntimeError("gemini 5xx")

    fake_gemini.chat = boom
    monkeypatch.setitem(sys.modules, "gemini_client", fake_gemini)

    monkeypatch.setattr(
        llm_router, "fallback_chat",
        lambda u, context=None, facts=None, persona_notes=None, group_id=None: "fallback-after-err",
    )
    assert llm_router.smart_chat("hi", [], []) == "fallback-after-err"


# ─── _extract_text ────────────────────────────────────────────────────────


def test_extract_text_string_passthrough():
    """str 輸入直接回。"""
    assert llm_router._extract_text("plain text") == "plain text"


def test_extract_text_uses_gemini_extract_when_available(monkeypatch):
    """非 str 輸入 → 委派 gemini_client._extract_text。"""
    fake = types.ModuleType("gemini_client")
    fake._extract_text = lambda x: "from-gemini-extract"
    monkeypatch.setitem(sys.modules, "gemini_client", fake)

    out = llm_router._extract_text(["list", "input"])
    assert out == "from-gemini-extract"


def test_extract_text_falls_back_to_str_on_failure(monkeypatch):
    """gemini_client 無法 import → 退回 str() 截斷 500。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "gemini_client":
            raise ImportError("absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "gemini_client", raising=False)

    out = llm_router._extract_text(["abc", 123])
    assert isinstance(out, str)
    assert len(out) <= 500
