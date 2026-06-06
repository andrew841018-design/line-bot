"""Unit tests for lite_reply two-stage refactor (2026-05-03).

驗證重構後的三件事：
  1. _try_local_llm 在 local_llm 沒裝時 graceful degrade → None
  2. Stage 1 寫死 handler 優先於 Stage 2 LLM（事實查詢 LLM 不準）
  3. Local LLM unavailable 時，未命中 Stage 1 的會退回 Stage 3 規則式
  4. Stage 1/2/3 在主路由的拆分結構正確
"""
from __future__ import annotations

import sys
import builtins

import pytest

import lite_reply


# ─── Stage 1/2/3 結構性檢查 ─────────────────────────────────────────────


def test_stage1_handlers_count():
    """Stage 1 應該有 8 個寫死 handler。"""
    assert len(lite_reply._STAGE1_HANDLERS) == 8
    names = [h.__name__ for h in lite_reply._STAGE1_HANDLERS]
    expected = {
        "_try_youtube_info",
        "_try_stock",
        "_try_forex",
        "_try_calculate",
        "_try_time_date",
        "_try_countdown",
        "_try_unit_convert",
        "_try_wiki_lookup",
    }
    assert set(names) == expected


def test_stage3_handlers_count():
    """Stage 3 fallback 應該有 3 個 handler。"""
    assert len(lite_reply._STAGE3_HANDLERS) == 3
    names = [h.__name__ for h in lite_reply._STAGE3_HANDLERS]
    expected = {"_try_url_summary", "_try_weather", "_try_google_snippet"}
    assert set(names) == expected


def test_stage1_and_stage3_disjoint():
    """同一個 handler 不能同時掛 Stage 1 跟 Stage 3。"""
    s1 = {h.__name__ for h in lite_reply._STAGE1_HANDLERS}
    s3 = {h.__name__ for h in lite_reply._STAGE3_HANDLERS}
    assert not (s1 & s3)


# ─── _try_local_llm graceful degrade ────────────────────────────────────


def test_try_local_llm_returns_none_on_import_error(monkeypatch):
    """local_llm 沒裝（ImportError）→ 回 None，不能 raise。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "local_llm":
            raise ImportError("No module named 'local_llm' (simulated)")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # 確保 sys.modules 沒緩存（否則 import 不會走 hook）
    monkeypatch.delitem(sys.modules, "local_llm", raising=False)

    out = lite_reply._try_local_llm("Hello world")
    assert out is None


def test_try_local_llm_returns_none_on_runtime_error(monkeypatch):
    """local_llm.chat 拋 exception → 回 None。"""
    fake_module = type(sys)("local_llm")

    def boom(*a, **kw):
        raise RuntimeError("LLM crashed")

    fake_module.chat = boom
    monkeypatch.setitem(sys.modules, "local_llm", fake_module)

    out = lite_reply._try_local_llm("Anything")
    assert out is None


def test_try_local_llm_returns_none_on_short_response(monkeypatch):
    """local_llm 回 < 6 chars 視為無效 → 回 None。"""
    fake_module = type(sys)("local_llm")
    fake_module.chat = lambda *a, **kw: "hi"  # too short
    monkeypatch.setitem(sys.modules, "local_llm", fake_module)

    out = lite_reply._try_local_llm("Anything")
    assert out is None


def test_try_local_llm_returns_response_when_valid(monkeypatch):
    """local_llm 回有效字串 → 加上 lite mode footer 回傳。"""
    fake_module = type(sys)("local_llm")
    fake_module.chat = lambda *a, **kw: "這是 LLM 自主生成的回應內容"
    monkeypatch.setitem(sys.modules, "local_llm", fake_module)

    out = lite_reply._try_local_llm("為什麼天空是藍的")
    assert out is not None
    assert "這是 LLM 自主生成的回應內容" in out
    assert "local LLM" in out


def test_try_local_llm_passes_context(monkeypatch):
    """context 參數有正確傳給底層 chat()。"""
    fake_module = type(sys)("local_llm")
    captured: dict = {}

    def fake_chat(text, context=None, *a, **kw):
        captured["text"] = text
        captured["context"] = context
        return "回應內容看起來夠長給通過"

    fake_module.chat = fake_chat
    monkeypatch.setitem(sys.modules, "local_llm", fake_module)

    ctx = [("user", "嗨"), ("bot", "你好")]
    lite_reply._try_local_llm("最近怎樣", context=ctx)
    assert captured["context"] == ctx
    assert captured["text"] == "最近怎樣"


# ─── Stage 1 優先於 Stage 2 LLM ─────────────────────────────────────────


def test_stage1_calculator_beats_llm(monkeypatch):
    """『1+1』命中 Stage 1 計算 handler，不會走 LLM。"""
    fake_module = type(sys)("local_llm")
    fake_module.chat = lambda *a, **kw: pytest.fail(
        "LLM should NOT be called when Stage 1 calculator hit"
    )
    monkeypatch.setitem(sys.modules, "local_llm", fake_module)

    out = lite_reply.lite_reply("1+1")
    assert out is not None
    assert "= 2" in out


def test_stage1_time_query_beats_llm(monkeypatch):
    """『現在幾點』命中 Stage 1 時間 handler，不會走 LLM。"""
    fake_module = type(sys)("local_llm")
    fake_module.chat = lambda *a, **kw: pytest.fail(
        "LLM should NOT be called when Stage 1 time hit"
    )
    monkeypatch.setitem(sys.modules, "local_llm", fake_module)

    out = lite_reply.lite_reply("現在幾點")
    assert out is not None
    assert "現在是" in out


# ─── Stage 2 → Stage 3 fallback chain ───────────────────────────────────


def test_stage2_unavailable_falls_back_to_stage3(monkeypatch):
    """LLM 不可用 + Stage 1 沒命中 → 走 Stage 3 fallback。

    這裡用一個會被 _try_url_summary 命中的 URL 輸入。
    """
    # 1. 模擬 local_llm 不存在
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "local_llm":
            raise ImportError("simulated absence")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "local_llm", raising=False)

    # 2. 模擬 _try_url_summary 命中（不真的對外 HTTP）
    monkeypatch.setattr(
        lite_reply,
        "_try_url_summary",
        lambda text: "📰 from stage3 fallback" if "http" in text else None,
    )
    # 重建 Stage 3 tuple，因為 monkeypatch 動的是模組屬性而非 tuple
    monkeypatch.setattr(
        lite_reply,
        "_STAGE3_HANDLERS",
        (lite_reply._try_url_summary, lite_reply._try_weather, lite_reply._try_google_snippet),
    )

    out = lite_reply.lite_reply("這個連結 https://example.com 是什麼意思啊")
    assert out is not None
    assert "stage3" in out


def test_lite_reply_returns_none_when_nothing_matches(monkeypatch):
    """所有 stage 都沒命中 → 回 None（不亂答）。"""
    # local_llm 不可用
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "local_llm":
            raise ImportError("simulated absence")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "local_llm", raising=False)

    # 所有 Stage 3 handler 都返 None
    monkeypatch.setattr(lite_reply, "_try_url_summary", lambda t: None)
    monkeypatch.setattr(lite_reply, "_try_weather", lambda t: None)
    monkeypatch.setattr(lite_reply, "_try_google_snippet", lambda t: None)
    monkeypatch.setattr(
        lite_reply,
        "_STAGE3_HANDLERS",
        (lite_reply._try_url_summary, lite_reply._try_weather, lite_reply._try_google_snippet),
    )

    # 給一個既不會被 Stage 1 命中、又沒 URL 的隨意句子
    out = lite_reply.lite_reply("zzzzz_nothing_to_match_here_zzzzz")
    assert out is None


# ─── input validation ───────────────────────────────────────────────────


def test_lite_reply_rejects_empty():
    assert lite_reply.lite_reply("") is None
    assert lite_reply.lite_reply("   ") is None


def test_lite_reply_rejects_too_long():
    """> 500 chars 直接拒絕（避免 prompt injection）。"""
    huge = "a" * 600
    assert lite_reply.lite_reply(huge) is None


def test_lite_reply_signature_accepts_context():
    """重構後 lite_reply 必須接受 context kwarg（不能 break）。"""
    # 不真的 call LLM；只確認簽名兼容
    out = lite_reply.lite_reply("1+1", context=[("u", "hi")])
    assert out is not None  # 1+1 命中 calculator


def test_lite_reply_stock_stage1_passes_context(monkeypatch):
    ctx = [("user", "我覺得 NVDA 會再突破")]
    seen = {}

    def fake_quotes(text, *, context=None, **kwargs):
        seen["text"] = text
        seen["context"] = context
        return "【市場報價｜測試】\nNVDA: 180.00"

    monkeypatch.setattr(
        lite_reply.stock_quote,
        "get_contextual_quotes_text",
        fake_quotes,
    )
    fake_nlp = type(sys)("chinese_nlp")
    fake_nlp.classify_intent = lambda text: {"intent": "general"}
    monkeypatch.setitem(sys.modules, "chinese_nlp", fake_nlp)

    out = lite_reply.lite_reply("現在多少？", context=ctx)

    assert out is not None
    assert "NVDA" in out
    assert seen == {"text": "現在多少？", "context": ctx}


def test_lite_reply_stock_intent_priority_passes_context(monkeypatch):
    ctx = [("user", "我覺得台積電會再突破")]
    seen = {}

    def fake_quotes(text, *, context=None, **kwargs):
        seen["text"] = text
        seen["context"] = context
        return "【市場報價｜測試】\n2330.TW: 2325.00"

    monkeypatch.setattr(
        lite_reply.stock_quote,
        "get_contextual_quotes_text",
        fake_quotes,
    )
    fake_nlp = type(sys)("chinese_nlp")
    fake_nlp.classify_intent = lambda text: {"intent": "stock"}
    monkeypatch.setitem(sys.modules, "chinese_nlp", fake_nlp)

    out = lite_reply.lite_reply("現在多少？", context=ctx)

    assert out is not None
    assert "2330.TW" in out
    assert seen == {"text": "現在多少？", "context": ctx}


def test_lite_reply_countdown_wins_over_contextual_stock(monkeypatch):
    ctx = [("user", "我覺得 NVDA 會再突破")]
    stock_called = False

    def fake_quotes(*args, **kwargs):
        nonlocal stock_called
        stock_called = True
        return "【市場報價｜測試】\nNVDA: 180.00"

    monkeypatch.setattr(
        lite_reply.stock_quote,
        "get_contextual_quotes_text",
        fake_quotes,
    )
    fake_nlp = type(sys)("chinese_nlp")
    fake_nlp.classify_intent = lambda text: {"intent": "general"}
    monkeypatch.setitem(sys.modules, "chinese_nlp", fake_nlp)

    out = lite_reply.lite_reply("距離 6/15 還有多少天？", context=ctx)

    assert out is not None
    assert "距離" in out
    assert not stock_called


# ─── Stage 1 handler 個別 smoke ─────────────────────────────────────────


def test_try_calculate_handler():
    assert "= 5" in lite_reply._try_calculate("2+3")


def test_try_time_date_handler():
    assert lite_reply._try_time_date("現在幾點") is not None
    assert lite_reply._try_time_date("今天日期") is not None
    assert lite_reply._try_time_date("無關文字") is None


def test_try_url_summary_returns_none_when_no_url():
    """沒 URL → 不該抓網頁。"""
    assert lite_reply._try_url_summary("一般文字沒 URL") is None


def test_try_weather_returns_none_when_no_weather_keyword():
    assert lite_reply._try_weather("打籃球") is None


def test_try_google_snippet_returns_none_when_no_question():
    """沒問句 keyword → 不該 scrape。"""
    assert lite_reply._try_google_snippet("普通陳述句") is None
