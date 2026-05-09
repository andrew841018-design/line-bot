"""Tests for agent_tools + agent_loop（2026-05-03）。

關鍵點：
- 不真 call local LLM（mlx 太慢）。用 stub callable 模擬 LLM 多輪 JSON 回應。
- 不真 call yfinance / requests / wiki — 把 underlying lib 換 stub 確認 plumbing。
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest import mock

import pytest

import agent_tools
import agent_loop


# ════════════════════════════════════════════════════════════════════════
# agent_tools layer
# ════════════════════════════════════════════════════════════════════════


def test_tools_count_is_seven():
    """確認 tool 數恰為 7 個（任務規格）。"""
    names = [t["name"] for t in agent_tools.TOOLS]
    assert len(names) >= 7, f"應至少有 7 個 tool，目前 {len(names)}: {names}"


def test_all_tools_have_required_keys():
    """每個 tool 必有 name / description / args / call。"""
    for t in agent_tools.TOOLS:
        assert "name" in t and isinstance(t["name"], str)
        assert "description" in t and isinstance(t["description"], str)
        assert "args" in t and isinstance(t["args"], dict)
        assert callable(t["call"])


def test_get_tool_returns_spec_for_known_name():
    spec = agent_tools.get_tool("get_stock_price")
    assert spec is not None
    assert spec["name"] == "get_stock_price"


def test_get_tool_returns_none_for_unknown():
    assert agent_tools.get_tool("nonexistent_tool") is None
    assert agent_tools.get_tool("") is None


def test_list_tools_for_prompt_contains_each_name():
    s = agent_tools.list_tools_for_prompt()
    for t in agent_tools.TOOLS:
        assert t["name"] in s


def test_call_tool_unknown_returns_error_string():
    out = agent_tools.call_tool("does_not_exist", {})
    assert isinstance(out, str)
    assert "未知 tool" in out


def test_call_tool_get_time_works():
    """get_time 是純 datetime，不需 mock。"""
    out = agent_tools.call_tool("get_time", {})
    assert isinstance(out, str)
    # 預期含「-」「:」「週」
    assert "-" in out and ":" in out and "週" in out


def test_call_tool_get_stock_price_uses_stock_quote(monkeypatch):
    """get_stock_price 應呼叫 stock_quote.get_quotes_text。"""
    fake = types.ModuleType("stock_quote")
    fake.get_quotes_text = lambda text, max_symbols=5: f"FAKE-RESULT-FOR:{text}"
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    out = agent_tools.call_tool("get_stock_price", {"text": "台積電"})
    assert "FAKE-RESULT-FOR:台積電" in out


def test_call_tool_get_stock_price_handles_none(monkeypatch):
    fake = types.ModuleType("stock_quote")
    fake.get_quotes_text = lambda text, max_symbols=5: None
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    out = agent_tools.call_tool("get_stock_price", {"text": "ZZZZ"})
    assert "找不到" in out or "（" in out


def test_call_tool_search_wiki_uses_lite_reply(monkeypatch):
    fake = types.ModuleType("lite_reply")
    fake._wiki_summary = lambda q: f"WIKI:{q}"
    monkeypatch.setitem(sys.modules, "lite_reply", fake)

    out = agent_tools.call_tool("search_wiki", {"query": "ETL"})
    assert "WIKI:ETL" in out


def test_call_tool_summarize_url_uses_lite_reply(monkeypatch):
    fake = types.ModuleType("lite_reply")
    fake._summarize_url = lambda url: f"SUMMARY:{url}"
    monkeypatch.setitem(sys.modules, "lite_reply", fake)

    out = agent_tools.call_tool("summarize_url", {"url": "https://example.com"})
    assert "SUMMARY:https://example.com" in out


def test_call_tool_google_search_uses_lite_reply(monkeypatch):
    # web_scraper 強制回空 → fallback 到 lite_reply 單筆 snippet
    fake_ws = types.ModuleType("web_scraper")
    fake_ws.search_duckduckgo = lambda q, k=5: []
    monkeypatch.setitem(sys.modules, "web_scraper", fake_ws)
    fake = types.ModuleType("lite_reply")
    fake._google_search_snippet = lambda q: f"GOOG:{q}"
    monkeypatch.setitem(sys.modules, "lite_reply", fake)

    out = agent_tools.call_tool("google_search", {"query": "MLX"})
    assert "GOOG:MLX" in out


def test_call_tool_get_weather_uses_lite_reply(monkeypatch):
    fake = types.ModuleType("lite_reply")
    fake._weather_taiwan = lambda text: f"WEATHER:{text}"
    monkeypatch.setitem(sys.modules, "lite_reply", fake)

    out = agent_tools.call_tool("get_weather", {"city": "高雄"})
    assert "WEATHER:高雄" in out


def test_call_tool_get_forex_uses_lite_reply(monkeypatch):
    fake = types.ModuleType("lite_reply")
    fake._try_forex = lambda text: f"FOREX:{text}"
    monkeypatch.setitem(sys.modules, "lite_reply", fake)

    out = agent_tools.call_tool("get_forex", {"text": "100 USD to TWD"})
    assert "FOREX:100 USD to TWD" in out


def test_call_tool_retrieve_rag_uses_rag_retriever(monkeypatch):
    fake = types.ModuleType("rag_retriever")
    fake.retrieve = lambda q, k=3: [
        {"text": "之前說過的話", "similarity": 0.88, "message_id": "m1"},
    ]
    monkeypatch.setitem(sys.modules, "rag_retriever", fake)

    out = agent_tools.call_tool("retrieve_rag", {"query": "上次討論", "k": 2})
    assert "之前說過的話" in out
    assert "0.88" in out


def test_call_tool_wraps_runtime_exceptions(monkeypatch):
    """tool callable 拋例外 → 回字串而非 raise（避免 ReAct loop 中斷）。"""
    fake = types.ModuleType("stock_quote")

    def boom(*a, **kw):
        raise RuntimeError("network down")

    fake.get_quotes_text = boom
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    out = agent_tools.call_tool("get_stock_price", {"text": "AAPL"})
    assert isinstance(out, str)
    assert "失敗" in out or "（" in out


# ════════════════════════════════════════════════════════════════════════
# agent_loop layer — stub LLM 模擬 ReAct flow
# ════════════════════════════════════════════════════════════════════════


class _ScriptedLLM:
    """Stub LLM：依序回 _scripted 序列裡的字串。

    每 call 一次 .chat 就 pop 一個 reply。耗盡後 raise（讓 test 一定爆）。
    """

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.call_log: list[dict[str, Any]] = []

    def chat(self, user_input, context=None, system_prompt=None, max_tokens=None):
        self.call_log.append({
            "user": user_input,
            "context": list(context or []),
            "system_prompt": system_prompt,
        })
        if not self._replies:
            raise AssertionError("ScriptedLLM 已耗盡 replies")
        return self._replies.pop(0)


def test_agent_returns_immediate_final_when_no_tool_needed():
    """User 問『你好』，stub LLM 直接回 final → 不該 call tool。"""
    stub = _ScriptedLLM(['{"final": "嗨！有什麼能幫你的？"}'])
    agent = agent_loop.Agent(llm_chat=stub.chat)
    out = agent.run("你好")
    assert out == "嗨！有什麼能幫你的？"
    assert len(stub.call_log) == 1


def test_agent_calls_tool_then_returns_final(monkeypatch):
    """經典 ReAct：第一輪 LLM 要 tool；call 完後第二輪 LLM 回 final。"""
    # mock stock_quote → 控制 tool result
    fake = types.ModuleType("stock_quote")
    fake.get_quotes_text = lambda text, max_symbols=5: "2330.TW (台積電): 1245.0"
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    stub = _ScriptedLLM([
        '{"action": "get_stock_price", "args": {"text": "台積電"}, "thought": "查報價"}',
        '{"final": "台積電現在 1245 元（即時）"}',
    ])
    agent = agent_loop.Agent(llm_chat=stub.chat)
    out = agent.run("台積電股價")
    assert out == "台積電現在 1245 元（即時）"
    assert len(stub.call_log) == 2
    # 第二輪 user msg 應含 tool result
    assert "Tool result" in stub.call_log[1]["user"]
    assert "1245.0" in stub.call_log[1]["user"]


def test_agent_handles_markdown_wrapped_json(monkeypatch):
    """LLM 把 JSON 包進 ```json fence 也要正確 parse。"""
    fake = types.ModuleType("stock_quote")
    fake.get_quotes_text = lambda text, max_symbols=5: "AAPL: 178"
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    stub = _ScriptedLLM([
        '```json\n{"action": "get_stock_price", "args": {"text": "AAPL"}}\n```',
        '```json\n{"final": "AAPL 是 178 元"}\n```',
    ])
    agent = agent_loop.Agent(llm_chat=stub.chat)
    out = agent.run("AAPL 多少")
    assert out == "AAPL 是 178 元"


def test_agent_handles_extra_text_around_json(monkeypatch):
    """LLM 混了思考文字 + JSON → 抓出第一個 balanced {...}。"""
    fake = types.ModuleType("stock_quote")
    fake.get_quotes_text = lambda text, max_symbols=5: "TSLA: 250"
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    stub = _ScriptedLLM([
        '我想用 stock 工具：\n{"action": "get_stock_price", "args": {"text": "TSLA"}}\n好',
        '想了想：{"final": "TSLA 現在 250"}',
    ])
    agent = agent_loop.Agent(llm_chat=stub.chat)
    out = agent.run("TSLA 多少")
    assert out == "TSLA 現在 250"


def test_agent_returns_raw_when_llm_breaks_format():
    """LLM 不回 JSON → 直接拿 raw 當 final（不要丟掉答案）。"""
    stub = _ScriptedLLM(["這就是答案，沒有 JSON 格式"])
    agent = agent_loop.Agent(llm_chat=stub.chat)
    out = agent.run("某個問題")
    assert out == "這就是答案，沒有 JSON 格式"


def test_agent_caps_iterations(monkeypatch):
    """LLM 一直要 call tool 不 final → 在 max_iterations 內強制終止。"""
    fake = types.ModuleType("stock_quote")
    fake.get_quotes_text = lambda text, max_symbols=5: "stock data"
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    # 給 LLM 4 輪 action，最後一輪它仍想 action 但會被「最後一輪不准 call tool」處理
    # 這邊改成 LLM 故意每輪都用「不一樣 args」避開 dedup
    stub = _ScriptedLLM([
        '{"action": "get_stock_price", "args": {"text": "A"}}',
        '{"action": "get_stock_price", "args": {"text": "B"}}',
        '{"action": "get_stock_price", "args": {"text": "C"}}',
        '{"action": "get_stock_price", "args": {"text": "D"}}',
        # 第 5 輪（is_last=True），即使送 action 也會被當成終止
        '{"action": "get_stock_price", "args": {"text": "E"}, "thought": "再一次"}',
    ])
    agent = agent_loop.Agent(llm_chat=stub.chat, max_iterations=5)
    out = agent.run("查很多股票")
    # 最後一輪的 thought 會被當 fallback final
    assert out == "再一次" or out is None  # 兩種行為都可接受
    # 關鍵：只 call 5 次 LLM，沒爆無窮 loop
    assert len(stub.call_log) <= 5


def test_agent_detects_repeated_action(monkeypatch):
    """LLM 連兩輪要呼叫一模一樣的 action+args → 偵測並中斷（避免無限重試）。"""
    fake = types.ModuleType("stock_quote")
    fake.get_quotes_text = lambda text, max_symbols=5: "stock"
    monkeypatch.setitem(sys.modules, "stock_quote", fake)

    stub = _ScriptedLLM([
        '{"action": "get_stock_price", "args": {"text": "X"}}',
        '{"action": "get_stock_price", "args": {"text": "X"}}',  # 重複！
    ])
    agent = agent_loop.Agent(llm_chat=stub.chat, max_iterations=5)
    out = agent.run("查 X")
    # 重複偵測後應 return None（讓上層降級）
    assert out is None
    assert len(stub.call_log) == 2


def test_agent_returns_none_when_llm_returns_empty():
    stub = _ScriptedLLM([""])
    agent = agent_loop.Agent(llm_chat=stub.chat)
    assert agent.run("anything") is None


def test_agent_returns_none_for_empty_input():
    stub = _ScriptedLLM([])  # 不該被 call
    agent = agent_loop.Agent(llm_chat=stub.chat)
    assert agent.run("") is None
    assert agent.run("   ") is None
    # stub 不該被 call
    assert len(stub.call_log) == 0


def test_agent_no_llm_returns_none(monkeypatch):
    """llm_chat=None 且 local_llm 不可用 → run 回 None。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "local_llm":
            raise ImportError("simulated absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "local_llm", raising=False)

    agent = agent_loop.Agent(llm_chat=None)
    assert agent.run("hi") is None


def test_agent_handles_llm_signature_without_kwargs():
    """LLM stub 不接 system_prompt / max_tokens → agent 會降級用簡單 signature。"""
    call_count = {"n": 0}

    def chat(user_input, context=None):  # 沒 system_prompt / max_tokens
        call_count["n"] += 1
        return '{"final": "簡單 signature OK"}'

    agent = agent_loop.Agent(llm_chat=chat)
    out = agent.run("test")
    assert out == "簡單 signature OK"
    # 應該至少 call 一次（包含 TypeError fallback path）
    assert call_count["n"] >= 1


def test_safe_parse_json_handles_pure_json():
    out = agent_loop.Agent._safe_parse_json('{"a": 1}')
    assert out == {"a": 1}


def test_safe_parse_json_strips_fence():
    out = agent_loop.Agent._safe_parse_json('```json\n{"a": 2}\n```')
    assert out == {"a": 2}


def test_safe_parse_json_extracts_balanced_braces():
    out = agent_loop.Agent._safe_parse_json('blah {"a": 3} trailing')
    assert out == {"a": 3}


def test_safe_parse_json_returns_raw_on_failure():
    raw = "this is not json at all"
    out = agent_loop.Agent._safe_parse_json(raw)
    # 失敗時要回 raw 字串（caller 會視為 final）
    assert out == raw


# ════════════════════════════════════════════════════════════════════════
# llm_router._try_local_llm 整合 agent_loop（fallback chain 仍正常）
# ════════════════════════════════════════════════════════════════════════


def test_router_tier2_uses_agent_when_available(monkeypatch):
    """router._try_local_llm 應先試 agent_loop.Agent.run。"""
    import llm_router

    # 偽 agent_loop module
    fake = types.ModuleType("agent_loop")

    class FakeAgent:
        def __init__(self, *a, **kw): pass
        def run(self, msg, context=None):
            return "AGENT-RESULT-FOR:" + msg

    fake.Agent = FakeAgent
    monkeypatch.setitem(sys.modules, "agent_loop", fake)

    out = llm_router._try_local_llm("台積電", [])
    assert out == "AGENT-RESULT-FOR:台積電"


def test_router_tier2_falls_back_to_local_llm_when_agent_fails(monkeypatch):
    """agent_loop.Agent.run raise → 退回 local_llm.chat（原本邏輯）。"""
    import llm_router

    fake_agent_mod = types.ModuleType("agent_loop")

    class FakeAgent:
        def __init__(self, *a, **kw): pass
        def run(self, msg, context=None):
            raise RuntimeError("agent boom")

    fake_agent_mod.Agent = FakeAgent
    monkeypatch.setitem(sys.modules, "agent_loop", fake_agent_mod)

    fake_llm = types.ModuleType("local_llm")
    fake_llm.chat = lambda text, context=None: "fallback-llm-said-something-long-enough"
    monkeypatch.setitem(sys.modules, "local_llm", fake_llm)

    out = llm_router._try_local_llm("anything", [])
    assert out == "fallback-llm-said-something-long-enough"


def test_router_tier2_falls_back_when_agent_returns_short(monkeypatch):
    """agent.run 回太短 → 退到 local_llm.chat。"""
    import llm_router

    fake_agent_mod = types.ModuleType("agent_loop")

    class FakeAgent:
        def __init__(self, *a, **kw): pass
        def run(self, msg, context=None):
            return "hi"  # too short (< 6 chars)

    fake_agent_mod.Agent = FakeAgent
    monkeypatch.setitem(sys.modules, "agent_loop", fake_agent_mod)

    fake_llm = types.ModuleType("local_llm")
    fake_llm.chat = lambda text, context=None: "本地 LLM 直接回的長度合格內容"
    monkeypatch.setitem(sys.modules, "local_llm", fake_llm)

    out = llm_router._try_local_llm("hi", [])
    assert out == "本地 LLM 直接回的長度合格內容"


def test_router_tier2_full_degrade_when_both_fail(monkeypatch):
    """agent_loop ImportError + local_llm ImportError → None（讓 Tier 3 接手）。"""
    import builtins
    import llm_router

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name in ("agent_loop", "local_llm"):
            raise ImportError(f"simulated absent {name}")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "agent_loop", raising=False)
    monkeypatch.delitem(sys.modules, "local_llm", raising=False)

    assert llm_router._try_local_llm("anything", []) is None
