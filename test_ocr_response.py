"""Tests for the OCR-response fix in media_pipeline.

Bug: when vision desc is unavailable, analyze_image used to dump raw OCR text
(`📷 OCR 抽到的文字…`) instead of responding to the content. Now it routes the
OCR text through the local 14B (`_respond_to_ocr_text`) and, if the local LLM is
also down, returns a graceful honest message + fires a once-per-process alert.

All tests are mock-based (no model load, no Discord — conftest already stubs
notify_discord.send_dm).
"""
from __future__ import annotations

import sys
import types

import pytest

import media_pipeline as mp


# ── _respond_to_ocr_text ────────────────────────────────────────────────────


def test_build_image_argument_prompt_requires_four_sections():
    prompt = mp._build_image_argument_prompt("幫我看這張")

    assert "圖片內容：" in prompt
    assert "正方：" in prompt
    assert "反方：" in prompt
    assert "統一論點：" in prompt
    assert "不要編造來源" in prompt


def test_ensure_image_argument_structure_wraps_plain_reply():
    out = mp._ensure_image_argument_structure(
        "這張圖是在說某個產品很省錢。",
        ocr_text="產品 A 每月省 30%",
    )

    assert out is not None
    assert "圖片內容：" in out
    assert "正方：" in out
    assert "反方：" in out
    assert "統一論點：" in out
    assert "產品 A" in out


def test_ensure_image_argument_structure_keeps_structured_reply():
    reply = (
        "圖片內容：這張圖在比較兩種方案。\n"
        "正方：A 方案成本低。\n"
        "反方：B 方案穩定性較好。\n"
        "統一論點：先看需求再選。"
    )

    assert mp._ensure_image_argument_structure(reply) == reply


def test_has_image_argument_structure_rejects_empty_sections():
    empty = "圖片內容：\n正方：\n反方：\n統一論點："

    assert not mp._has_image_argument_structure(empty)
    wrapped = mp._ensure_image_argument_structure(empty, desc="圖片在談補助")
    assert wrapped is not None
    assert "圖片在談補助" in wrapped


def test_respond_empty_ocr_returns_none_without_calling_llm(monkeypatch):
    called = {"n": 0}

    def _fake_chat(*a, **k):
        called["n"] += 1
        return "should not be called"

    fake = types.ModuleType("local_llm")
    fake.chat = _fake_chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    assert mp._respond_to_ocr_text("") is None
    assert mp._respond_to_ocr_text("   \n  ") is None
    assert called["n"] == 0  # short-circuits before importing/calling the LLM


def test_respond_routes_ocr_to_local_llm(monkeypatch):
    captured = {}
    fake = types.ModuleType("local_llm")

    def fake_chat(text, system_prompt=None, max_tokens=None):
        captured["system_prompt"] = system_prompt
        return "血壓 140/90 已達高血壓標準，建議先量幾天再決定。"

    fake.chat = fake_chat
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    out = mp._respond_to_ocr_text("血壓 140/90 算高嗎？")
    assert out is not None
    assert "高血壓" in out  # responded to content, not echoed
    assert "圖片內容：" in out
    assert "正方：" in out
    assert "反方：" in out
    assert "統一論點：" in out
    assert "圖片內容：" in captured["system_prompt"]
    assert "統一論點：" in captured["system_prompt"]


def test_respond_local_llm_down_returns_none(monkeypatch):
    fake = types.ModuleType("local_llm")
    fake.chat = lambda *a, **k: None  # load failed / quota-less local fail
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    assert mp._respond_to_ocr_text("任何文字") is None


def test_respond_whitespace_reply_collapses_to_none(monkeypatch):
    fake = types.ModuleType("local_llm")
    fake.chat = lambda *a, **k: "   \n  "
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    assert mp._respond_to_ocr_text("文字") is None


def test_respond_resets_alert_guard_on_success(monkeypatch):
    fake = types.ModuleType("local_llm")
    fake.chat = lambda *a, **k: "這是一個具體的回應內容。"
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", True, raising=False)

    out = mp._respond_to_ocr_text("文字")
    assert out is not None
    assert mp._local_llm_down_alerted is False  # recovery → re-arm the alert


# ── _alert_local_llm_down ───────────────────────────────────────────────────


def test_alert_fires_once_per_process(monkeypatch):
    sent = []
    import notify_discord
    monkeypatch.setattr(notify_discord, "send_dm", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("reason A")
    mp._alert_local_llm_down("reason B")  # deduped within process

    assert len(sent) == 1
    assert "本機 AI 模型載不進來" in sent[0]


def test_alert_sanitizes_mentions_and_caps_length(monkeypatch):
    sent = []
    import notify_discord
    monkeypatch.setattr(notify_discord, "send_dm", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("@everyone " + "x" * 500)

    assert len(sent) == 1
    assert "@everyone" not in sent[0]  # mention neutralised
    assert "@ everyone" in sent[0]


def test_alert_never_raises_on_notify_failure(monkeypatch):
    import notify_discord

    def _boom(*a, **k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(notify_discord, "send_dm", _boom)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    # must not propagate
    mp._alert_local_llm_down("reason")


# ── analyze_image no-desc branch routing ────────────────────────────────────


@pytest.fixture
def _vision_down(monkeypatch):
    """Force the no-desc branch: vision returns None, cache misses."""
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: "圖中的文字內容"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.describe_image = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)


def test_analyze_image_no_desc_returns_ocr_response_and_caches(monkeypatch, _vision_down):
    writes = []
    monkeypatch.setattr(mp, "_respond_to_ocr_text", lambda t: "這是針對內容的回應。")
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: writes.append(a))

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is not None
    assert "圖片內容：" in out
    assert "這是針對內容的回應" in out
    assert "正方：" in out
    assert "反方：" in out
    assert "統一論點：" in out
    assert len(writes) == 1  # the real OCR response IS cached


def test_analyze_image_passes_argument_prompt_to_vision_and_wraps_reply(monkeypatch):
    captured = {}
    web_context_called = {"n": 0}
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.delenv("MEDIA_IMAGE_WEB_CONTEXT", raising=False)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    fake_vision = types.ModuleType("vision_llm")

    def fake_describe_image(image, prompt=None):
        captured["prompt"] = prompt
        return "圖片描述：這是一張討論電動車補助的圖。"

    fake_vision.describe_image = fake_describe_image
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)
    monkeypatch.setattr(
        mp,
        "_v4_news_style_pipeline",
        lambda desc, ocr: web_context_called.__setitem__("n", 1) or desc,
    )

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert web_context_called["n"] == 0
    assert "圖片內容：" in captured["prompt"]
    assert "正方：" in captured["prompt"]
    assert "反方：" in captured["prompt"]
    assert "統一論點：" in captured["prompt"]
    assert out is not None
    assert "圖片內容：" in out
    assert "正方：" in out
    assert "反方：" in out
    assert "統一論點：" in out


def test_image_cache_ignores_old_unstructured_reply(monkeypatch):
    import memory
    deleted: list[int] = []

    monkeypatch.setattr(memory, "compute_sha256", lambda _data: "a" * 64)
    monkeypatch.setattr(
        memory,
        "lookup_media_cache",
        lambda _group_id, _media_type, _sha: {
            "cache_id": 1,
            "last_reply": "舊格式圖片描述",
            "seen_count": 1,
        },
    )
    monkeypatch.setattr(
        memory,
        "bump_media_cache_seen",
        lambda _cache_id: (_ for _ in ()).throw(
            AssertionError("stale cache must not be bumped")
        ),
    )
    monkeypatch.setattr(memory, "delete_media_cache", lambda cache_id: deleted.append(cache_id))

    assert mp._maybe_lookup_media_cache(b"x" * 2048, "Gtest", "image") is None
    assert deleted == [1]


def test_image_web_wrapper_generation_stays_local(monkeypatch):
    fake_local = types.ModuleType("local_llm")
    fake_local.chat = lambda *a, **k: (
        "圖片內容：這張圖在談政策補助。\n"
        "正方：補助可降低初期成本。\n"
        "反方：若缺少排富或稽核，可能造成資源浪費。\n"
        "統一論點：可支持方向，但要補完整配套。"
    )
    fake_gemini = types.ModuleType("gemini_client")

    def _gemini_should_not_run(*_a, **_k):
        raise AssertionError("image wrapper must not call Gemini")

    fake_gemini.chat = _gemini_should_not_run
    monkeypatch.setitem(sys.modules, "local_llm", fake_local)
    monkeypatch.setitem(sys.modules, "gemini_client", fake_gemini)

    out = mp._wrap_with_gemini_news_style("政策補助截圖", "補助 30%")

    assert out is not None
    assert "圖片內容：" in out
    assert "正方：" in out
    assert "反方：" in out
    assert "統一論點：" in out


def test_analyze_image_no_desc_local_down_degrades_without_dump(monkeypatch, _vision_down):
    alerts = []
    writes = []
    monkeypatch.setattr(mp, "_respond_to_ocr_text", lambda t: None)  # local LLM down
    monkeypatch.setattr(mp, "_alert_local_llm_down", lambda r: alerts.append(r))
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: writes.append(a))

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is not None
    assert not out.startswith("📷 OCR")          # no longer a raw OCR dump
    assert "載不進來" in out                       # honest degraded message
    assert "圖中的文字內容" not in out             # must NOT leak/echo the raw OCR text
    assert len(alerts) == 1                        # alerted once
    assert writes == []                            # degraded message NOT cached


def test_analyze_image_no_desc_no_ocr_returns_none(monkeypatch):
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: None  # no text in image
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.describe_image = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")
    assert out is None  # nothing to say → leave pending (unchanged contract)


# ── market screenshot OCR grounding ─────────────────────────────────────────


def test_extract_market_screenshot_reply_uses_only_ocr_numbers():
    ocr = """
    富途牛牛
    道瓊
    42,197.79
    -299.29 -0.70%
    那斯達克
    19,406.83
    -61.30 -0.31%
    費半
    5,286.93
    -40.84 -0.77%
    現貨黃金
    4,313.20
    +28.10 +0.66%
    """

    out = mp._extract_market_screenshot_reply(ocr)

    assert out is not None
    assert "依附圖 OCR" in out
    assert "道瓊指數" in out
    assert "42,197.79" in out
    assert "-0.70%" in out
    assert "黃金" in out
    assert "4,313.20" in out
    assert "2,308" not in out
    assert "Yahoo" not in out


def test_extract_market_screenshot_reply_handles_collapsed_ocr_line():
    ocr = (
        "富途牛牛 道瓊 42,197.79 -299.29 -0.70% "
        "那斯達克 19,406.83 -61.30 -0.31% "
        "現貨黃金 4,313.20 +28.10 +0.66%"
    )

    out = mp._extract_market_screenshot_reply(ocr)

    assert out is not None
    assert "道瓊指數：42,197.79" in out
    assert "納斯達克指數：19,406.83" in out
    assert "黃金：4,313.20" in out


def test_extract_market_screenshot_reply_ignores_bare_gold_non_market_text():
    assert mp._extract_market_screenshot_reply("黃金雞塊 99 元 第二件半價") is None


def test_analyze_image_market_screenshot_short_circuits_llm(monkeypatch):
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: "牛牛\n現貨黃金\n4,313.20\n+28.10 +0.66%"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    fake_vision = types.ModuleType("vision_llm")

    def _vision_should_not_run(*_a, **_k):
        raise AssertionError("vision/local LLM path should not run for market OCR")

    fake_vision.describe_image = _vision_should_not_run
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)
    monkeypatch.setattr(mp, "_respond_to_ocr_text", _vision_should_not_run)

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is not None
    assert "4,313.20" in out
    assert "依附圖 OCR" in out
