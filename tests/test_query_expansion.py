"""tests/test_query_expansion.py — finetune_query_expansion 單測

驗證項目
========
  1. expand_queries：mock local_llm 回乾淨 JSON → 正確 parse
  2. expand_queries：JSON 包 ```json fence → 仍能 parse
  3. expand_queries：local_llm 回 None → 觸發 fallback
  4. expand_queries：local_llm 回亂七八糟非 JSON → 觸發 fallback
  5. expand_queries：LLM 回不足 n 個 → 從 fallback 補滿
  6. expand_queries：desc 為空 → 直接走 fallback（不打 LLM）
  7. _fallback_queries：純規則式拿到專有名詞 → 套模板
  8. _fallback_queries：完全沒專有名詞 → 用 desc 前綴變體
  9. _parse_query_list：list 含非 str → 過濾乾淨
 10. expand_queries：去重 + 數量正確
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

# 對齊 conftest 的 env bootstrap
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import finetune_query_expansion as fqe  # noqa: E402


# ─── helper：mock local_llm.chat ──────────────────────────────────────────────
def _install_fake_local_llm(monkeypatch, response):
    """把 sys.modules['local_llm'] 換成 stub。response 可以是 str / None / Exception。"""
    calls = {"prompts": []}

    def fake_chat(prompt, **kwargs):
        calls["prompts"].append(prompt)
        if isinstance(response, Exception):
            raise response
        return response

    fake = types.ModuleType("local_llm")
    fake.chat = fake_chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    return calls


# ─── 1. clean JSON list 直 parse ────────────────────────────────────────────
def test_expand_queries_clean_json(monkeypatch):
    queries = [
        "馬斯克 SpaceX 2026",
        "SpaceX latest news",
        "Starship 反對意見",
        "Starship FAA 一手",
        "Starship 爭議",
        "Starship fact check",
    ]
    _install_fake_local_llm(monkeypatch, json.dumps(queries, ensure_ascii=False))

    out = fqe.expand_queries("馬斯克宣布 SpaceX 在德州測試 Starship", n=6)
    assert out == queries
    assert len(out) == 6


# ─── 2. JSON 包 markdown fence 仍可 parse ────────────────────────────────────
def test_expand_queries_with_markdown_fence(monkeypatch):
    queries = ["q1", "q2", "q3", "q4", "q5", "q6"]
    fenced = "```json\n" + json.dumps(queries) + "\n```"
    _install_fake_local_llm(monkeypatch, fenced)

    out = fqe.expand_queries("某個事件描述", n=6)
    assert out == queries


# ─── 3. local_llm 回 None → fallback ─────────────────────────────────────────
def test_expand_queries_none_response_triggers_fallback(monkeypatch):
    _install_fake_local_llm(monkeypatch, None)

    out = fqe.expand_queries("馬斯克 SpaceX 在德州", n=6)
    # fallback 永遠回 n 個
    assert len(out) == 6
    assert all(isinstance(q, str) and q.strip() for q in out)


# ─── 4. local_llm 回非 JSON → fallback ───────────────────────────────────────
def test_expand_queries_garbage_response_triggers_fallback(monkeypatch):
    _install_fake_local_llm(
        monkeypatch,
        "我幫你想了 6 個 query，請看：\n第一個是...第二個是...",
    )

    out = fqe.expand_queries("馬斯克 SpaceX 在德州", n=6)
    assert len(out) == 6
    # 至少有一個帶上「馬斯克」（fallback 抓到 nr）或者退而求其次帶上 desc 前綴
    joined = " ".join(out)
    assert "馬斯克" in joined or "SpaceX" in joined or "德州" in joined


# ─── 5. LLM 回不足 n 個 → 從 fallback 補滿 ───────────────────────────────────
def test_expand_queries_pads_when_llm_returns_too_few(monkeypatch):
    _install_fake_local_llm(
        monkeypatch,
        json.dumps(["q1", "q2", "q3"]),
    )

    out = fqe.expand_queries("馬斯克在 SpaceX 公司宣布德州測試", n=6)
    assert len(out) == 6
    # 前 3 個來自 LLM
    assert out[:3] == ["q1", "q2", "q3"]
    # 後 3 個是 fallback
    assert all(q not in {"q1", "q2", "q3"} for q in out[3:])


# ─── 6. desc 為空 → 直接 fallback，不打 LLM ──────────────────────────────────
def test_expand_queries_empty_desc_skips_llm(monkeypatch):
    calls = _install_fake_local_llm(monkeypatch, json.dumps(["a"] * 6))

    out = fqe.expand_queries("", n=6)
    assert len(out) == 6
    # LLM 不該被叫到
    assert calls["prompts"] == []


# ─── 7. _fallback_queries：抓到專有名詞套模板 ────────────────────────────────
def test_fallback_with_proper_nouns():
    out = fqe._fallback_queries(
        "馬斯克 2026 年宣布 SpaceX 在德州測試新火箭", n=6,
    )
    assert len(out) == 6
    # 至少有 1 個帶馬斯克 / SpaceX / 德州（jieba 至少抓到其一）
    joined = " ".join(out)
    assert any(k in joined for k in ("馬斯克", "SpaceX", "德州"))


# ─── 8. _fallback_queries：沒有專有名詞 → desc 前綴變體 ──────────────────────
def test_fallback_no_proper_nouns_uses_seed():
    # 全是普通形容詞，沒專有名詞
    out = fqe._fallback_queries("這是一段非常無聊的描述沒有任何特定名詞", n=6)
    assert len(out) == 6
    # 必有 fact check 變體（鋪滿用的）
    joined = " ".join(out)
    assert "fact check" in joined or "真假" in joined or "爭議" in joined


# ─── 9. _parse_query_list 過濾非 str ─────────────────────────────────────────
def test_parse_query_list_filters_non_strings():
    raw = json.dumps(["good1", 123, None, "good2", "", "  ", "good3"])
    out = fqe._parse_query_list(raw)
    assert out == ["good1", "good2", "good3"]


# ─── 10. 去重 + 數量 ─────────────────────────────────────────────────────────
def test_expand_queries_dedupes_and_caps(monkeypatch):
    # LLM 回 8 個但有 2 個重複
    _install_fake_local_llm(
        monkeypatch,
        json.dumps(["a", "b", "a", "c", "d", "e", "f", "b"]),
    )

    out = fqe.expand_queries("test", n=6)
    assert len(out) == 6
    # 必須去重
    assert len(set(out)) == len(out)
    # 前 6 個獨立 query 應該保留
    assert out[:6] == ["a", "b", "c", "d", "e", "f"]
