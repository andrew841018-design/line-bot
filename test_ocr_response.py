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
    fake = types.ModuleType("local_llm")
    fake.chat = lambda text, system_prompt=None, max_tokens=None: "血壓 140/90 已達高血壓標準，建議先量幾天再決定。"
    monkeypatch.setitem(sys.modules, "local_llm", fake)

    out = mp._respond_to_ocr_text("血壓 140/90 算高嗎？")
    assert out is not None
    assert "高血壓" in out  # responded to content, not echoed


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

    assert out == "這是針對內容的回應。"
    assert len(writes) == 1  # the real OCR response IS cached


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
