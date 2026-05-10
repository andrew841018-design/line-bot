"""tests/test_self_critique.py — pure mock tests for self_critique.

涵蓋：
   1. critique_reply: parse Gemini JSON 正常路徑（claims/contradictions/missing_facts 三類齊全）
   2. critique_reply: supported / contradicted / unsupported 三類 verdict 都正確 normalize
   3. critique_reply: Gemini 回 markdown 圍欄包的 JSON → 仍能 parse
   4. critique_reply: Gemini 爆 quota（429）→ fallback 到 14B、14B 給合法 JSON
   5. critique_reply: Gemini + 14B 都失敗 → graceful empty schema
   6. critique_reply: Gemini 回壞 JSON / 全空 → 14B 補位
   7. critique_reply: empty reply / 無 source → graceful empty schema
   8. refine_reply: Gemini 給新版本 → 直接回（剝 code fence）
   9. refine_reply: Gemini 爆 → 14B fallback 改寫
   10. refine_reply: Gemini + 14B 都爆 → 回原 reply（不阻塞）
   11. refine_reply: 砍 contradicted/unsupported claim 的 prompt 真的有把 critique 帶進去
   12. refine_reply: empty critique / empty source 也能 graceful 跑完

mock 策略：
   - Gemini：monkeypatch `gemini_client._client.models.generate_content` 回 fake response
   - 14B：把 sys.modules['local_llm'].chat 換成 stub
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# bootstrap env (跟其他 tests 對齊)
os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

LINE_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(LINE_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(LINE_BOT_ROOT))

import self_critique as sc  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────
def _fake_gemini_response(text: str) -> Any:
    """模擬 google-genai SDK 回的 response 物件（只關心 .text）。"""
    resp = MagicMock()
    resp.text = text
    return resp


def _install_fake_gemini(monkeypatch, *, side_effect=None, return_value=None):
    """把 gemini_client._client.models.generate_content 換成 mock。

    return_value: str → 包成 _fake_gemini_response 回傳
    side_effect: callable / Exception list — 直接設給 mock
    """
    import gemini_client

    fake_models = MagicMock()
    if side_effect is not None:
        fake_models.generate_content = MagicMock(side_effect=side_effect)
    elif return_value is not None:
        if isinstance(return_value, str):
            fake_models.generate_content = MagicMock(
                return_value=_fake_gemini_response(return_value)
            )
        else:
            fake_models.generate_content = MagicMock(return_value=return_value)
    else:
        fake_models.generate_content = MagicMock(
            return_value=_fake_gemini_response("")
        )
    fake_client = MagicMock()
    fake_client.models = fake_models
    monkeypatch.setattr(gemini_client, "_client", fake_client)
    return fake_models


def _install_fake_local_llm(monkeypatch, outputs):
    """outputs: list[str|None]，依序回傳；用完取最後一個。"""
    state = {"i": 0}

    def fake_chat(prompt, **kwargs):
        i = state["i"]
        state["i"] += 1
        arr = outputs
        if not arr:
            return None
        if i < len(arr):
            return arr[i]
        return arr[-1]

    fake = types.ModuleType("local_llm")
    fake.chat = fake_chat
    fake._calls = state  # 給 test 檢查用
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    return fake


# ── sample fixtures ────────────────────────────────────────────────────────
@pytest.fixture
def sample_reply() -> str:
    return (
        "正方：黃仁勳本週宣布 NVIDIA Q1 營收 260 億美元，創新高。\n"
        "反方：但毛利率從 78% 跌到 75%，需注意。\n"
        "整合：成長放緩但仍領先。\n"
        "Actionable: 盯下季 guidance。\n"
        "來源：https://example.com/a"
    )


@pytest.fixture
def sample_sources() -> list[dict]:
    return [
        {
            "title": "NVIDIA Q1 earnings",
            "url": "https://example.com/a",
            "text": "NVIDIA reported Q1 revenue of $26B, up 18% YoY. Gross margin 75.1%.",
        },
        {
            "title": "Nikkei coverage",
            "url": "https://example.com/b",
            "text": "Jensen Huang said the firm has 80% data-center share.",
        },
    ]


# ═══════════════════════════════════════════════════════════════════════════
# 1. critique_reply — Gemini 正常路徑，三類齊全
# ═══════════════════════════════════════════════════════════════════════════
def test_critique_reply_parses_full_schema(monkeypatch, sample_reply, sample_sources):
    fake_json = json.dumps(
        {
            "claims": [
                {
                    "claim": "NVIDIA Q1 營收 260 億美元",
                    "verdict": "supported",
                    "evidence": "https://example.com/a",
                },
                {
                    "claim": "毛利率從 78% 跌到 75%",
                    "verdict": "contradicted",
                    "evidence": "source 顯示 75.1%，沒提 78%",
                },
                {
                    "claim": "黃仁勳本週宣布",
                    "verdict": "unsupported",
                    "evidence": "",
                },
            ],
            "contradictions": [
                {
                    "summary": "source A 75.1% vs reply 78%→75%",
                    "sources": [
                        "https://example.com/a",
                        "https://example.com/b",
                    ],
                }
            ],
            "missing_facts": [
                {
                    "fact": "data-center 市占 80%",
                    "source": "https://example.com/b",
                }
            ],
        },
        ensure_ascii=False,
    )
    _install_fake_gemini(monkeypatch, return_value=fake_json)
    _install_fake_local_llm(monkeypatch, outputs=[])  # 不該被叫到

    out = sc.critique_reply(sample_reply, sample_sources)

    assert isinstance(out, dict)
    assert len(out["claims"]) == 3
    assert len(out["contradictions"]) == 1
    assert len(out["missing_facts"]) == 1
    assert out["missing_facts"][0]["fact"].startswith("data-center")


# ═══════════════════════════════════════════════════════════════════════════
# 2. critique_reply — supported / contradicted / unsupported 三類 normalize
# ═══════════════════════════════════════════════════════════════════════════
def test_critique_reply_normalizes_all_three_verdicts(
    monkeypatch, sample_reply, sample_sources
):
    fake_json = json.dumps(
        {
            "claims": [
                {"claim": "A", "verdict": "supported"},
                {"claim": "B", "verdict": "Contradicted"},  # 大小寫
                {"claim": "C", "verdict": "unsupported"},
                {"claim": "D", "verdict": "supports it"},  # 拼字寬鬆 → support
                {"claim": "E", "verdict": "conflict"},  # → contradicted
                {"claim": "F", "verdict": "noidea"},  # → unsupported
            ],
        },
        ensure_ascii=False,
    )
    _install_fake_gemini(monkeypatch, return_value=fake_json)
    _install_fake_local_llm(monkeypatch, outputs=[])

    out = sc.critique_reply(sample_reply, sample_sources)
    verdicts = [c["verdict"] for c in out["claims"]]

    assert verdicts == [
        "supported",
        "contradicted",
        "unsupported",
        "supported",
        "contradicted",
        "unsupported",
    ]
    # 三類都至少出現一次
    assert "supported" in verdicts
    assert "contradicted" in verdicts
    assert "unsupported" in verdicts


# ═══════════════════════════════════════════════════════════════════════════
# 3. critique_reply — markdown 圍欄包的 JSON 也能 parse
# ═══════════════════════════════════════════════════════════════════════════
def test_critique_reply_strips_code_fence(monkeypatch, sample_reply, sample_sources):
    inner = json.dumps(
        {
            "claims": [{"claim": "A", "verdict": "supported"}],
            "contradictions": [],
            "missing_facts": [],
        },
        ensure_ascii=False,
    )
    fenced = f"```json\n{inner}\n```"
    _install_fake_gemini(monkeypatch, return_value=fenced)
    _install_fake_local_llm(monkeypatch, outputs=[])

    out = sc.critique_reply(sample_reply, sample_sources)
    assert len(out["claims"]) == 1
    assert out["claims"][0]["claim"] == "A"
    assert out["claims"][0]["verdict"] == "supported"


# ═══════════════════════════════════════════════════════════════════════════
# 4. critique_reply — Gemini 爆 quota → fallback 14B 給合法 JSON
# ═══════════════════════════════════════════════════════════════════════════
def test_critique_reply_gemini_quota_fallback_to_14b(
    monkeypatch, sample_reply, sample_sources
):
    _install_fake_gemini(
        monkeypatch,
        side_effect=Exception("429 RESOURCE_EXHAUSTED PerDay quota exceeded"),
    )
    local_json = json.dumps(
        {
            "claims": [
                {"claim": "from-14b", "verdict": "supported", "evidence": ""},
            ],
            "contradictions": [],
            "missing_facts": [
                {"fact": "extra fact", "source": "https://x.com"},
            ],
        },
        ensure_ascii=False,
    )
    _install_fake_local_llm(monkeypatch, outputs=[local_json])

    out = sc.critique_reply(sample_reply, sample_sources)

    assert len(out["claims"]) == 1
    assert out["claims"][0]["claim"] == "from-14b"
    assert out["missing_facts"][0]["fact"] == "extra fact"


# ═══════════════════════════════════════════════════════════════════════════
# 5. critique_reply — Gemini + 14B 都失敗 → graceful empty schema
# ═══════════════════════════════════════════════════════════════════════════
def test_critique_reply_graceful_when_all_fail(
    monkeypatch, sample_reply, sample_sources
):
    _install_fake_gemini(monkeypatch, side_effect=Exception("network down"))
    _install_fake_local_llm(monkeypatch, outputs=[None])  # 14B 也回 None

    out = sc.critique_reply(sample_reply, sample_sources)
    assert out == {"claims": [], "contradictions": [], "missing_facts": []}


# ═══════════════════════════════════════════════════════════════════════════
# 6. critique_reply — Gemini 回壞 JSON / 全空 → 14B 補位
# ═══════════════════════════════════════════════════════════════════════════
def test_critique_reply_bad_json_then_14b_recovers(
    monkeypatch, sample_reply, sample_sources
):
    # Gemini 回完全不是 JSON 的東西
    _install_fake_gemini(monkeypatch, return_value="this is not JSON at all")
    fallback_json = json.dumps(
        {
            "claims": [{"claim": "rescued", "verdict": "supported"}],
            "contradictions": [],
            "missing_facts": [],
        },
        ensure_ascii=False,
    )
    _install_fake_local_llm(monkeypatch, outputs=[fallback_json])

    out = sc.critique_reply(sample_reply, sample_sources)

    assert len(out["claims"]) == 1
    assert out["claims"][0]["claim"] == "rescued"


# ═══════════════════════════════════════════════════════════════════════════
# 7. critique_reply — empty reply / 無 source → graceful empty schema
# ═══════════════════════════════════════════════════════════════════════════
def test_critique_reply_empty_reply_returns_empty(monkeypatch):
    # 不該打任何 LLM
    fake_models = _install_fake_gemini(monkeypatch, return_value="should-not-be-called")
    _install_fake_local_llm(monkeypatch, outputs=[])

    out = sc.critique_reply("", [{"title": "x", "url": "y", "text": "z"}])
    assert out == {"claims": [], "contradictions": [], "missing_facts": []}
    fake_models.generate_content.assert_not_called()


def test_critique_reply_no_source_still_runs(monkeypatch, sample_reply):
    # 沒 sources 也要能跑完，prompt 裡 sources 區塊是「（無 sources）」
    captured: dict[str, Any] = {}

    def capture_call(**kwargs):
        captured["contents"] = kwargs.get("contents", "")
        return _fake_gemini_response(
            json.dumps(
                {
                    "claims": [
                        {"claim": "everything", "verdict": "unsupported"},
                    ],
                    "contradictions": [],
                    "missing_facts": [],
                },
                ensure_ascii=False,
            )
        )

    import gemini_client

    fake_models = MagicMock()
    fake_models.generate_content = MagicMock(side_effect=capture_call)
    fake_client = MagicMock()
    fake_client.models = fake_models
    monkeypatch.setattr(gemini_client, "_client", fake_client)
    _install_fake_local_llm(monkeypatch, outputs=[])

    out = sc.critique_reply(sample_reply, [])
    assert out["claims"][0]["verdict"] == "unsupported"
    assert "（無 sources）" in captured["contents"]


# ═══════════════════════════════════════════════════════════════════════════
# 8. refine_reply — Gemini 給新版本，回（剝圍欄）
# ═══════════════════════════════════════════════════════════════════════════
def test_refine_reply_strips_fence_and_returns(
    monkeypatch, sample_reply, sample_sources
):
    refined = (
        "正方：NVIDIA Q1 營收 260 億美元（source A）。data-center 市占 80%（source B）。\n"
        "反方：毛利率 75.1%（source A 顯示，不是 reply 寫的 78%→75%）。\n"
        "整合：成長健康。\n"
        "Actionable: 盯下季 guidance + 競品壓力。\n"
        "來源：https://example.com/a, https://example.com/b"
    )
    fenced = f"```\n{refined}\n```"
    _install_fake_gemini(monkeypatch, return_value=fenced)
    _install_fake_local_llm(monkeypatch, outputs=[])

    critique = {
        "claims": [
            {"claim": "78%→75%", "verdict": "contradicted", "evidence": ""},
        ],
        "contradictions": [],
        "missing_facts": [
            {"fact": "data-center 市占 80%", "source": "https://example.com/b"},
        ],
    }
    out = sc.refine_reply(sample_reply, critique, sample_sources)

    # 剝掉 ``` 後應與 refined 對齊
    assert out.strip() == refined.strip()
    assert "data-center" in out  # missing fact 被補進去
    assert "75.1" in out  # 正確數字進來


# ═══════════════════════════════════════════════════════════════════════════
# 9. refine_reply — Gemini 爆 → 14B fallback 改寫
# ═══════════════════════════════════════════════════════════════════════════
def test_refine_reply_gemini_fail_then_14b(monkeypatch, sample_reply, sample_sources):
    _install_fake_gemini(monkeypatch, side_effect=Exception("429 quota exhausted"))
    fallback_text = (
        "正方：NVIDIA Q1 營收 260 億（source A）。\n"
        "整合：成長放緩但仍領先。\n"
        "Actionable: 盯 guidance。\n"
        "來源：https://example.com/a"
    )
    _install_fake_local_llm(monkeypatch, outputs=[fallback_text])

    out = sc.refine_reply(sample_reply, {}, sample_sources)
    assert out.strip() == fallback_text.strip()


# ═══════════════════════════════════════════════════════════════════════════
# 10. refine_reply — Gemini + 14B 都爆 → 回原 reply（不阻塞）
# ═══════════════════════════════════════════════════════════════════════════
def test_refine_reply_both_fail_returns_original(
    monkeypatch, sample_reply, sample_sources
):
    _install_fake_gemini(monkeypatch, side_effect=Exception("network down"))
    _install_fake_local_llm(monkeypatch, outputs=[None])

    out = sc.refine_reply(sample_reply, {}, sample_sources)
    assert out == sample_reply  # 原樣回


# ═══════════════════════════════════════════════════════════════════════════
# 11. refine_reply — critique_json 真的進 prompt（讓 LLM 砍掉 contradicted）
# ═══════════════════════════════════════════════════════════════════════════
def test_refine_reply_includes_critique_in_prompt(
    monkeypatch, sample_reply, sample_sources
):
    captured: dict[str, Any] = {}

    def capture_call(**kwargs):
        captured["contents"] = kwargs.get("contents", "")
        return _fake_gemini_response("refined output here")

    import gemini_client

    fake_models = MagicMock()
    fake_models.generate_content = MagicMock(side_effect=capture_call)
    fake_client = MagicMock()
    fake_client.models = fake_models
    monkeypatch.setattr(gemini_client, "_client", fake_client)
    _install_fake_local_llm(monkeypatch, outputs=[])

    critique = {
        "claims": [
            {
                "claim": "毛利率從 78% 跌到 75%",
                "verdict": "contradicted",
                "evidence": "source 顯示 75.1%",
            }
        ],
        "contradictions": [
            {"summary": "兩 source 數字不一致", "sources": ["a", "b"]}
        ],
        "missing_facts": [
            {"fact": "data-center 80% 市占", "source": "https://example.com/b"},
        ],
    }

    out = sc.refine_reply(sample_reply, critique, sample_sources)

    assert out == "refined output here"
    prompt = captured["contents"]
    # critique JSON 被序列化進 prompt
    assert "毛利率從 78% 跌到 75%" in prompt
    assert "contradicted" in prompt
    assert "data-center 80% 市占" in prompt
    # 原 reply / sources 也都進 prompt
    assert "黃仁勳" in prompt
    assert "https://example.com/a" in prompt
    # 主要規則文字（refine 任務）有出現
    assert "砍掉 contradicted" in prompt
    assert "missing_facts" in prompt


# ═══════════════════════════════════════════════════════════════════════════
# 12. refine_reply — empty critique / empty source 也能 graceful 跑完
# ═══════════════════════════════════════════════════════════════════════════
def test_refine_reply_empty_critique_and_sources(monkeypatch, sample_reply):
    _install_fake_gemini(monkeypatch, return_value="just a refined line")
    _install_fake_local_llm(monkeypatch, outputs=[])

    out = sc.refine_reply(sample_reply, None, None)
    assert out == "just a refined line"

    # 空 reply：直接回空，不打 LLM
    out2 = sc.refine_reply("", {"claims": []}, [])
    assert out2 == ""
