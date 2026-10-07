"""Tests for the OCR-response fix in media_pipeline.

Bug: when vision desc is unavailable, analyze_image used to dump raw OCR text
(`📷 OCR 抽到的文字…`) instead of responding to the content. Now it routes the
OCR text through the local 14B (`_respond_to_ocr_text`) and, if the local LLM is
also down, returns no visible reply while retaining categorical outage alerts.

All tests are mock-based (no model load and no Discord transport).
"""
from __future__ import annotations

import sys
import threading
import time
import types
from io import BytesIO

import pytest

import media_pipeline as mp


def test_easyocr_load_is_cache_only(monkeypatch):
    import ocr_helper

    calls = []
    fake_easyocr = types.ModuleType("easyocr")
    fake_easyocr.Reader = lambda languages, **kwargs: calls.append(
        (languages, kwargs)
    ) or object()
    monkeypatch.setitem(sys.modules, "easyocr", fake_easyocr)
    monkeypatch.setattr(ocr_helper, "_reader", None)

    assert ocr_helper._ensure_loaded() is True
    assert calls == [
        (["ch_tra", "en"], {"gpu": False, "download_enabled": False})
    ]


def test_ocr_bytes_stay_in_memory_without_temp_file(monkeypatch):
    import numpy as np
    from PIL import Image
    import ocr_helper

    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    observed = []

    class FakeReader:
        def readtext(self, source):
            observed.append(source)
            return [(None, "synthetic", 0.99)]

    monkeypatch.setattr(ocr_helper, "_reader", FakeReader())

    assert ocr_helper.extract_text(buffer.getvalue()) == "synthetic"
    assert len(observed) == 1
    assert isinstance(observed[0], np.ndarray)


def test_ocr_bytes_skip_pytesseract_temp_fallback(monkeypatch):
    import ocr_helper

    calls = []
    fake_pytesseract = types.ModuleType("pytesseract")
    fake_pytesseract.image_to_string = lambda *_a, **_k: calls.append(True)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setattr(ocr_helper, "_reader", "tesseract")

    assert ocr_helper.extract_text(b"private-image") is None
    assert calls == []


# ── _respond_to_ocr_text ────────────────────────────────────────────────────


def test_build_image_argument_prompt_requires_response_only():
    prompt = mp._build_image_argument_prompt("幫我看這張")
    assert "只供內部參考" in prompt
    assert "圖片內容：" not in prompt
    assert "不要編造來源" in prompt



def test_ensure_image_argument_structure_preserves_answer_without_ocr():
    answer = "先比較每月總成本，再決定是否換方案。"
    assert mp._ensure_image_argument_structure(answer, ocr_text="方案A每月省30%") == answer



def test_ensure_image_argument_structure_keeps_structured_reply():
    reply = (
        "圖片內容：這張圖在比較兩種方案。\n"
        "正方：A 方案成本低。\n"
        "反方：B 方案穩定性較好。\n"
        "統一論點：先看需求再選。"
    )

    out = mp._ensure_image_argument_structure(reply)
    assert "圖片內容" not in out
    assert "這張圖在比較" not in out
    assert "先看需求再選" in out



def test_has_image_argument_structure_rejects_empty_sections():
    empty = "圖片內容：\n正方：\n反方：\n統一論點："

    assert not mp._has_image_argument_structure(empty)
    wrapped = mp._ensure_image_argument_structure(empty, desc="圖片在談補助")
    assert wrapped is None



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
    assert "圖片內容：" not in out
    assert "只供內部參考" in captured["system_prompt"]



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


def test_text_fallback_success_does_not_reset_vision_alert_guard(monkeypatch):
    fake = types.ModuleType("local_llm")
    fake.chat = lambda *a, **k: "這是一個具體的回應內容。"
    monkeypatch.setitem(sys.modules, "local_llm", fake)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", True, raising=False)

    out = mp._respond_to_ocr_text("文字")
    assert out is not None
    assert mp._local_llm_down_alerted is True


# ── _alert_local_llm_down ───────────────────────────────────────────────────


def test_alert_fires_once_per_process(monkeypatch):
    sent = []
    import notify_discord
    monkeypatch.setattr(
        notify_discord,
        "send_dm_result",
        lambda msg: sent.append(msg) or types.SimpleNamespace(status="sent"),
    )
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("cleanup_failed")
    mp._alert_local_llm_down("snapshot_missing")  # deduped within process

    assert len(sent) == 1
    assert "圖片模型子程序無法安全回收" in sent[0]
    assert "WD_BLACK" not in sent[0]


def test_alert_uses_allowlisted_category_instead_of_untrusted_reason(monkeypatch):
    sent = []
    import notify_discord
    monkeypatch.setattr(
        notify_discord,
        "send_dm_result",
        lambda msg: sent.append(msg) or types.SimpleNamespace(status="sent"),
    )
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("@everyone " + "x" * 500)

    assert len(sent) == 1
    assert "@everyone" not in sent[0]
    assert "x" * 20 not in sent[0]
    assert "不代表外接碟或模型快取損壞" in sent[0]


def test_load_failure_alert_alone_recommends_storage_checks(monkeypatch):
    sent = []
    import notify_discord
    monkeypatch.setattr(
        notify_discord,
        "send_dm_result",
        lambda msg: sent.append(msg) or types.SimpleNamespace(status="sent"),
    )
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("snapshot_missing")

    assert len(sent) == 1
    assert "WD_BLACK" in sent[0]
    assert "huggingface" in sent[0]


def test_model_init_failure_alert_does_not_blame_storage(monkeypatch):
    sent = []
    import notify_discord
    monkeypatch.setattr(
        notify_discord,
        "send_dm_result",
        lambda msg: sent.append(msg) or types.SimpleNamespace(status="sent"),
    )
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("model_init_failed")

    assert len(sent) == 1
    assert "初始化失敗" in sent[0]
    assert "請檢查 WD_BLACK" not in sent[0]


def test_warmup_timeout_alert_is_categorical_and_does_not_blame_storage(monkeypatch):
    sent = []
    import notify_discord
    monkeypatch.setattr(
        notify_discord,
        "send_dm_result",
        lambda msg: sent.append(msg) or types.SimpleNamespace(status="sent"),
    )
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("warmup_timeout")

    assert len(sent) == 1
    assert "預熱逾時" in sent[0]
    assert "請檢查 WD_BLACK" not in sent[0]


def test_alert_never_raises_on_notify_failure(monkeypatch):
    import notify_discord

    def _boom(*a, **k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(notify_discord, "send_dm_result", _boom)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    # must not propagate
    mp._alert_local_llm_down("reason")


def test_alert_retries_after_definite_delivery_failure(monkeypatch):
    sent = []
    statuses = iter(["definite_failed", "sent"])
    import notify_discord

    def fake_send(message):
        sent.append(message)
        return types.SimpleNamespace(status=next(statuses))

    monkeypatch.setattr(notify_discord, "send_dm_result", fake_send)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)

    mp._alert_local_llm_down("model_init_failed")
    assert mp._local_llm_down_alerted is False
    mp._alert_local_llm_down("model_init_failed")

    assert len(sent) == 2
    assert mp._local_llm_down_alerted is True


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
    monkeypatch.setattr(mp, "_respond_to_ocr_text", lambda t: "合計少算了一筆，建議核對明細。")
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: writes.append(a))

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is not None
    assert "圖片內容：" not in out
    assert "合計少算了一筆" in out
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

    def fake_describe_image(image, prompt=None, **_kwargs):
        captured["prompt"] = prompt
        return "補助能降低購車成本，但仍要比較後續保養支出。"

    fake_vision.describe_image = fake_describe_image
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)
    monkeypatch.setattr(
        mp,
        "_v4_news_style_pipeline",
        lambda desc, ocr: web_context_called.__setitem__("n", 1) or desc,
    )

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert web_context_called["n"] == 0
    assert "只供內部參考" in captured["prompt"]
    assert out is not None
    assert out == "補助能降低購車成本，但仍要比較後續保養支出。"



def test_analyze_image_forwards_vision_deadline(monkeypatch):
    captured = {}
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")

    def fake_describe_image(*_args, **kwargs):
        captured.update(kwargs)
        return "先核對總成本，再決定是否換方案。"

    fake_vision.describe_image = fake_describe_image
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    assert mp.analyze_image(b"\x00" * 2048, timeout_sec=12.5)
    assert 0 < captured["timeout_sec"] <= 12.5


def test_analyze_image_default_deadline_bounds_vision(monkeypatch):
    captured = {}
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")

    def fake_describe_image(*_args, **kwargs):
        captured.update(kwargs)
        return "先核對總成本，再決定是否換方案。"

    fake_vision.describe_image = fake_describe_image
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    assert mp.analyze_image(b"\x00" * 2048)
    assert 0 < captured["timeout_sec"] <= mp._IMAGE_ANALYSIS_DEFAULT_TIMEOUT_SEC


def test_analyze_image_deadline_includes_ocr_and_skips_late_vision(monkeypatch):
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")

    def slow_ocr(*_args, **_kwargs):
        time.sleep(0.02)
        return "synthetic OCR"

    fake_ocr.extract_text = slow_ocr
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    calls = []
    fake_vision.describe_image = lambda *a, **k: calls.append((a, k))
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    with pytest.raises(mp.MediaVisionTimeoutError):
        mp.analyze_image(b"\x00" * 2048, timeout_sec=0.005)

    assert calls == []


def test_analyze_image_propagates_vision_timeout(monkeypatch):
    class VisionTimeoutError(TimeoutError):
        pass

    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.VisionTimeoutError = VisionTimeoutError

    def timed_out(*_args, **_kwargs):
        raise VisionTimeoutError("synthetic deadline")

    fake_vision.describe_image = timed_out
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    with pytest.raises(VisionTimeoutError):
        mp.analyze_image(b"\x00" * 2048, timeout_sec=0.01)


@pytest.mark.parametrize(
    ("error_name", "error_base", "error_message"),
    [
        ("VisionBusyError", RuntimeError, "vision worker is busy"),
        ("VisionBusyError", RuntimeError, "vision worker is warming up"),
        ("VisionTimeoutError", TimeoutError, "vision worker timed out"),
    ],
)
def test_vision_capacity_failure_with_ocr_stays_silent_without_dump(
    monkeypatch, error_name, error_base, error_message
):
    error_type = type(error_name, (error_base,), {})
    writes = []
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        mp,
        "_maybe_write_media_cache",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: "訂單編號 A123，總額 1,280 元"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    setattr(fake_vision, error_name, error_type)

    def unavailable(*_args, **_kwargs):
        raise error_type(error_message)

    fake_vision.describe_image = unavailable
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)
    monkeypatch.setattr(
        mp,
        "_respond_to_ocr_text",
        lambda *_a, **_k: pytest.fail("capacity fallback must not call text LLM"),
    )
    monkeypatch.setattr(
        mp,
        "_alert_local_llm_down",
        lambda *_a, **_k: pytest.fail("ordinary capacity fallback must not alert"),
    )

    out = mp.analyze_image(b"\x00" * 2048, timeout_sec=1)

    assert out is None
    assert writes == []



@pytest.mark.parametrize("ocr_result", [None, " \n\t"])
def test_vision_busy_without_ocr_preserves_existing_failure_path(
    monkeypatch, ocr_result
):
    class VisionBusyError(RuntimeError):
        pass

    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: ocr_result
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.VisionBusyError = VisionBusyError
    fake_vision.describe_image = lambda *_a, **_k: (_ for _ in ()).throw(
        VisionBusyError("synthetic busy")
    )
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    with pytest.raises(VisionBusyError):
        mp.analyze_image(b"\x00" * 2048, timeout_sec=1)


def test_ocr_only_has_no_visible_fallback():
    assert mp._build_ocr_degraded_reply("訂單A123\n" + "長" * 1200) is None
    assert not mp._is_cache_quality_reply(None)



def test_ocr_fallback_reserves_time_before_vision_timeout(monkeypatch):
    captured = {}
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        mp,
        "_extract_ocr_with_deadline",
        lambda *_a, **_k: "可用 OCR",
    )
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *_a, **_k: "unused"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    class VisionTimeoutError(TimeoutError):
        pass

    fake_vision = types.ModuleType("vision_llm")
    fake_vision.VisionTimeoutError = VisionTimeoutError

    def timed_out(*_args, **kwargs):
        captured.update(kwargs)
        raise VisionTimeoutError("synthetic timeout")

    fake_vision.describe_image = timed_out
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    out = mp.analyze_image(b"\x00" * 2048, timeout_sec=1.5)

    assert out is None
    assert 0 < captured["timeout_sec"] <= 0.5


def test_low_remaining_budget_with_ocr_skips_vision(monkeypatch):
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        mp,
        "_extract_ocr_with_deadline",
        lambda *_a, **_k: "可用 OCR",
    )
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *_a, **_k: "unused"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.describe_image = lambda *_a, **_k: pytest.fail(
        "vision must not start after the OCR fallback reserve begins"
    )
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    out = mp.analyze_image(b"\x00" * 2048, timeout_sec=0.5)

    assert out is None


def test_exception_name_alone_cannot_trigger_ocr_capacity_fallback(monkeypatch):
    registered_timeout = type("VisionTimeoutError", (TimeoutError,), {})
    unregistered_timeout = type("VisionTimeoutError", (TimeoutError,), {})
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(mp, "_respond_to_ocr_text", lambda *_a, **_k: None)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *_a, **_k: "可用 OCR"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)
    fake_vision = types.ModuleType("vision_llm")
    fake_vision.VisionTimeoutError = registered_timeout
    fake_vision.describe_image = lambda *_a, **_k: (_ for _ in ()).throw(
        unregistered_timeout("spoofed class name")
    )
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    out = mp.analyze_image(b"\x00" * 2048, timeout_sec=2)

    assert out is None



def test_warmup_alert_does_not_delay_ocr_fallback(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    workers = []
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        mp,
        "_local_llm_alert_dispatch_epochs",
        set(),
        raising=False,
    )
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)
    monkeypatch.setattr(mp, "_local_llm_alert_inflight", None, raising=False)
    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *_a, **_k: "可用 OCR"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    class VisionUnavailableError(RuntimeError):
        def __init__(self, message, *, code="worker_unavailable"):
            super().__init__(message)
            self.code = code

    fake_vision = types.ModuleType("vision_llm")
    fake_vision.VisionUnavailableError = VisionUnavailableError
    fake_vision.describe_image = lambda *_a, **_k: (_ for _ in ()).throw(
        VisionUnavailableError("synthetic", code="warmup_timeout")
    )
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    def slow_alert(_reason, **_kwargs):
        workers.append(threading.current_thread())
        entered.set()
        try:
            assert release.wait(timeout=2)
        finally:
            finished.set()

    monkeypatch.setattr(mp, "_alert_local_llm_down", slow_alert)

    started = time.monotonic()
    out = mp.analyze_image(b"\x00" * 2048, timeout_sec=1.5)
    elapsed = time.monotonic() - started

    assert out is None
    assert entered.wait(timeout=0.2)
    assert elapsed < 0.5
    release.set()
    assert finished.wait(timeout=1)
    workers[0].join(timeout=1)
    assert not workers[0].is_alive()


def test_warmup_alert_dispatch_is_bounded_per_outage_epoch(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []
    monkeypatch.setattr(
        mp,
        "_local_llm_alert_dispatch_epochs",
        set(),
        raising=False,
    )
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)
    monkeypatch.setattr(mp, "_local_llm_alert_inflight", None, raising=False)

    def slow_alert(reason, **_kwargs):
        calls.append(reason)
        entered.set()
        try:
            assert release.wait(timeout=2)
        finally:
            finished.set()

    monkeypatch.setattr(mp, "_alert_local_llm_down", slow_alert)

    first = mp._dispatch_local_llm_alert("warmup_timeout")
    assert first is not None
    assert entered.wait(timeout=0.2)
    assert mp._dispatch_local_llm_alert("warmup_timeout") is None
    release.set()
    assert finished.wait(timeout=1)
    first.join(timeout=1)

    assert calls == ["warmup_timeout"]
    assert not first.is_alive()


def test_image_cache_ignores_old_unstructured_reply(monkeypatch):
    import memory
    deleted: list[int] = []

    monkeypatch.setattr(memory, "compute_sha256", lambda _data: "a" * 64)
    monkeypatch.setattr(
        memory,
        "lookup_media_cache",
        lambda _group_id, _media_type, _sha: {
            "cache_id": 1,
            "last_reply": "圖片描述：舊格式圖片描述",
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



@pytest.mark.parametrize("user_prompt", ["", "請列正反方並附來源"])
def test_image_web_wrapper_generation_stays_local(monkeypatch, user_prompt):
    captured = {}
    answer = "補助可降低初期成本，但仍要確認申請資格與稽核方式。"

    def fake_chat(text, **kwargs):
        captured["prompt"] = text
        captured.update(kwargs)
        return answer

    fake_local = types.SimpleNamespace(chat=fake_chat)
    fake_search = types.SimpleNamespace(
        search_duckduckgo=lambda *_a, **_k: [],
        search_google_news=lambda *_a, **_k: [],
        search_wiki_full=lambda *_a, **_k: None,
    )
    fake_gemini = types.ModuleType("gemini_client")

    def _gemini_should_not_run(*_a, **_k):
        raise AssertionError("image wrapper must not call Gemini")

    fake_gemini.chat = _gemini_should_not_run
    monkeypatch.setitem(sys.modules, "local_llm", fake_local)
    monkeypatch.setitem(sys.modules, "web_scraper", fake_search)
    monkeypatch.setitem(sys.modules, "gemini_client", fake_gemini)

    assert mp._wrap_with_gemini_news_style("政策補助截圖", "補助 30%", user_prompt=user_prompt) == answer
    if user_prompt:
        assert user_prompt in captured["prompt"]
    assert "只有使用者明確要求時" in captured["prompt"]
    assert "至少 4" not in captured["prompt"]
    assert "簡潔" in captured["system_prompt"]


@pytest.mark.parametrize("verdict, expected_refine", [
    ("supported", False), ("contradicted", True), ("unsupported", True),
])
def test_image_v4_refines_factual_defects_only(monkeypatch, verdict, expected_refine):
    import os
    import self_critique

    answer = "補助可降低初期成本，但申請資格仍需確認。"
    refined = "補助申請資格尚未確認，先核對公告再估算成本。"
    observed = []
    source = {"title": "補助公告", "url": "https://example.org/policy", "snippet": "需審查資格"}
    monkeypatch.setitem(sys.modules, "finetune_query_expansion", types.SimpleNamespace(
        expand_queries=lambda *_a, **_k: ["政策補助"],
    ))
    monkeypatch.setitem(sys.modules, "source_aggregator", types.SimpleNamespace(
        aggregate_sources=lambda *_a, **_k: [source],
    ))
    monkeypatch.setitem(sys.modules, "fulltext_fetcher", types.SimpleNamespace(
        fetch_top_sources=lambda sources, **_k: sources,
    ))
    monkeypatch.setitem(sys.modules, "grounding_local", types.SimpleNamespace(
        score_response=lambda *_a: {"score_avg": 1.0},
    ))

    def fake_chat(prompt, **kwargs):
        assert "請列正反方並附來源" in prompt
        assert "至少 5" not in prompt
        assert "只有使用者明確要求時" in prompt
        assert "400-700" not in kwargs["system_prompt"]
        return answer

    monkeypatch.setitem(sys.modules, "local_llm", types.SimpleNamespace(chat=fake_chat))

    def fake_critique(*_a):
        assert os.environ["SELF_CRITIQUE_FORCE_LOCAL"] == "1"
        return {
            "claims": [{"claim": "補助可降低初期成本", "verdict": verdict}],
            "missing_facts": [{"fact": "申請截止"}, {"fact": "申請窗口"}],
        }

    def fake_refine(*_a, **kwargs):
        assert kwargs["user_prompt"] == "請列正反方並附來源"
        assert os.environ["SELF_CRITIQUE_FORCE_LOCAL"] == "1"
        observed.append(True)
        return refined

    monkeypatch.setattr(self_critique, "critique_reply", fake_critique)
    monkeypatch.setattr(self_critique, "refine_reply", fake_refine)
    monkeypatch.setenv("SELF_CRITIQUE_FORCE_LOCAL", "previous")
    assert mp._v4_news_style_pipeline("政策補助截圖", "補助資訊", user_prompt="請列正反方並附來源") == (refined if expected_refine else answer)
    assert bool(observed) == expected_refine
    assert os.environ["SELF_CRITIQUE_FORCE_LOCAL"] == "previous"



def test_analyze_image_no_desc_local_down_degrades_without_dump(monkeypatch, _vision_down):
    alerts = []
    writes = []
    monkeypatch.setattr(mp, "_respond_to_ocr_text", lambda t: None)  # local LLM down
    monkeypatch.setattr(mp, "_alert_local_llm_down", lambda r: alerts.append(r))
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: writes.append(a))

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is None
    assert alerts == []
    assert writes == []



def test_vision_cleanup_failure_does_not_call_policy_disabled_text_model(monkeypatch):
    calls = []
    alerts = []
    writes = []
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(
        mp, "_alert_local_llm_down", lambda code, **_kwargs: alerts.append(code)
    )

    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: "圖中的文字內容"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    fake_vision = types.ModuleType("vision_llm")

    def _vision_failed(*_args, **_kwargs):
        failure = RuntimeError(
            "vision worker cleanup failed: process_survived_terminate_and_kill"
        )
        failure.code = "cleanup_failed"
        raise failure

    fake_vision.describe_image = _vision_failed
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    fake_local = types.ModuleType("local_llm")
    fake_local.runtime_enabled = lambda: False
    fake_local.chat = lambda *_a, **_k: calls.append(True) or "must not run"
    monkeypatch.setitem(sys.modules, "local_llm", fake_local)

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is None
    assert calls == []
    assert alerts == ["cleanup_failed"]
    assert writes == []



def test_vision_warmup_timeout_stays_silent_and_alerts(monkeypatch):
    alerts = []
    writes = []
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(
        mp, "_dispatch_local_llm_alert", lambda code: alerts.append(code)
    )

    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: "圖中的文字內容"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    fake_vision = types.ModuleType("vision_llm")

    class VisionUnavailableError(RuntimeError):
        def __init__(self, message, *, code="worker_unavailable"):
            super().__init__(message)
            self.code = code

    fake_vision.VisionUnavailableError = VisionUnavailableError

    def _vision_failed(*_args, **_kwargs):
        raise VisionUnavailableError(
            "private implementation detail", code="warmup_timeout"
        )

    fake_vision.describe_image = _vision_failed
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    fake_local = types.ModuleType("local_llm")
    fake_local.runtime_enabled = lambda: False
    fake_local.chat = lambda *_a, **_k: pytest.fail("text MLX must stay disabled")
    monkeypatch.setitem(sys.modules, "local_llm", fake_local)

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is None
    assert alerts == ["warmup_timeout"]
    assert writes == []



def test_no_ocr_vision_failure_still_emits_categorical_alert(monkeypatch):
    alerts = []
    writes = []
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(mp, "_alert_local_llm_down", lambda code: alerts.append(code))

    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    fake_vision = types.ModuleType("vision_llm")

    class VisionUnavailableError(RuntimeError):
        def __init__(self, message, *, code="worker_unavailable"):
            super().__init__(message)
            self.code = code

    fake_vision.VisionUnavailableError = VisionUnavailableError

    def _vision_failed(*_args, **_kwargs):
        raise VisionUnavailableError(
            "private implementation detail", code="warmup_timeout"
        )

    fake_vision.describe_image = _vision_failed
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    out = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")

    assert out is None
    assert alerts == ["warmup_timeout"]
    assert writes == []


def test_persistent_vision_failure_with_text_fallback_alerts_once(monkeypatch):
    sent = []
    import notify_discord

    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *a, **k: None)
    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)
    monkeypatch.setattr(mp, "_local_llm_alert_inflight", None, raising=False)
    monkeypatch.setattr(
        notify_discord,
        "send_dm_result",
        lambda message: sent.append(message) or types.SimpleNamespace(status="sent"),
    )

    fake_ocr = types.ModuleType("ocr_helper")
    fake_ocr.extract_text = lambda *a, **k: "這是 OCR 內容"
    monkeypatch.setitem(sys.modules, "ocr_helper", fake_ocr)

    fake_vision = types.ModuleType("vision_llm")

    def _vision_failed(*_args, **_kwargs):
        failure = RuntimeError("private implementation detail")
        failure.code = "local_model_unavailable"
        raise failure

    fake_vision.describe_image = _vision_failed
    monkeypatch.setitem(sys.modules, "vision_llm", fake_vision)

    fake_local = types.ModuleType("local_llm")
    fake_local.runtime_enabled = lambda: True
    fake_local.chat = lambda *_a, **_k: (
        "圖片內容：OCR 顯示一項待處理資訊。\n"
        "正方：可先依文字內容判斷。\n"
        "反方：缺少視覺脈絡，不能過度推論。\n"
        "統一論點：先保留 OCR 結論，待圖片模型恢復再確認。"
    )
    monkeypatch.setitem(sys.modules, "local_llm", fake_local)

    first = mp.analyze_image(b"\x00" * 2048, group_id="Gtest")
    second = mp.analyze_image(b"\x01" * 2048, group_id="Gtest")

    assert first is not None and second is not None
    assert len(sent) == 1


def test_stale_alert_delivery_cannot_overwrite_visual_recovery(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    import notify_discord

    monkeypatch.setattr(mp, "_local_llm_down_alerted", False, raising=False)
    monkeypatch.setattr(mp, "_local_llm_alert_inflight", None, raising=False)
    monkeypatch.setattr(mp, "_local_llm_alert_epoch", 0, raising=False)

    def blocking_send(_message):
        entered.set()
        assert release.wait(timeout=1)
        return types.SimpleNamespace(status="sent")

    monkeypatch.setattr(notify_discord, "send_dm_result", blocking_send)
    sender = threading.Thread(
        target=mp._alert_local_llm_down,
        args=("model_init_failed",),
    )
    sender.start()
    assert entered.wait(timeout=1)

    # A true visual result recovered while the older alert was in flight.
    mp._reset_local_llm_alert_guard()
    release.set()
    sender.join(timeout=1)

    assert not sender.is_alive()
    assert mp._local_llm_down_alerted is False


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


def test_extract_market_screenshot_reply_interprets_direction_without_dump():
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
    assert "表現有分歧" in out
    assert "道瓊指數" in out
    assert "42,197.79" not in out
    assert "-0.70%" not in out
    assert "黃金" in out
    assert "4,313.20" not in out
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
    assert "黃金走強" in out
    assert "道瓊指數、納斯達克指數轉弱" in out
    assert "42,197.79" not in out



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
    assert "黃金偏強" in out
    assert "4,313.20" not in out


@pytest.mark.parametrize("v4_enabled", ["1", "0"])
def test_image_dispatch_preserves_explicit_request(monkeypatch, v4_enabled):
    request = "請列正反方並附來源"
    answer = "正方：可降低成本。\n反方：需要資格審查。\n來源：https://example.org/policy"
    captured = {}
    reads, writes = [], []
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *_a: reads.append(True) or "舊的預設回答")
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *_a: writes.append(True))
    monkeypatch.setenv("MEDIA_IMAGE_WEB_CONTEXT", "1")
    monkeypatch.delenv("MEDIA_HYBRID_DISABLED", raising=False)
    monkeypatch.setenv("MEDIA_PIPELINE_V4", v4_enabled)
    monkeypatch.setitem(sys.modules, "ocr_helper", types.SimpleNamespace(extract_text=lambda *_a: None))
    monkeypatch.setitem(sys.modules, "vision_llm", types.SimpleNamespace(
        describe_image=lambda *_a, **_k: "補助需資格審查。",
    ))

    def wrapper(desc, ocr_text="", user_prompt=""):
        captured["request"] = user_prompt
        return answer

    target = "_v4_news_style_pipeline" if v4_enabled == "1" else "_wrap_with_gemini_news_style"
    monkeypatch.setattr(mp, target, wrapper)
    assert mp.analyze_image(b"synthetic", user_prompt=request) == answer
    assert captured["request"] == request
    assert reads == []
    assert writes == []


def test_v4_aggregation_failure_preserves_request_in_legacy_wrapper(monkeypatch):
    request = "請列正反方並附來源"
    captured = {}
    monkeypatch.setitem(sys.modules, "finetune_query_expansion", types.SimpleNamespace(
        expand_queries=lambda *_a, **_k: ["補助"],
    ))

    def fail_aggregation(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setitem(sys.modules, "source_aggregator", types.SimpleNamespace(
        aggregate_sources=fail_aggregation,
    ))

    def legacy(desc, ocr_text="", user_prompt=""):
        captured["request"] = user_prompt
        return "資格需確認。"

    monkeypatch.setattr(mp, "_wrap_with_gemini_news_style", legacy)
    assert mp._v4_news_style_pipeline("補助", "", user_prompt=request) == "資格需確認。"
    assert captured["request"] == request


def test_local_refinement_receives_explicit_sources_request(monkeypatch):
    import self_critique

    request = "請列正反方並附來源"
    answer = "正方：可降低成本。\n反方：須確認資格。\n來源：https://example.org/policy"
    captured = {}
    monkeypatch.setattr(self_critique, "_call_gemini", lambda *_a, **_k: None)

    def local_refine(prompt, **_kwargs):
        captured["prompt"] = prompt
        return answer

    monkeypatch.setattr(self_critique, "_call_local_14b", local_refine)
    assert self_critique.refine_reply(
        "符合所有資格，來源：https://example.org/policy",
        {"claims": [{"claim": "符合所有資格", "verdict": "unsupported"}]},
        [{"url": "https://example.org/policy", "text": "須審查資格"}],
        user_prompt=request,
    ) == answer
    assert request in captured["prompt"]
    assert "可核實來源必須保留" in captured["prompt"]


def test_ocr_fallback_generation_receives_explicit_request(monkeypatch):
    captured = {}
    request = "請列正反方"
    answer = "正方：可降低成本。\n反方：申請資格需要確認。"

    def fake_chat(text, **_kwargs):
        captured["prompt"] = text
        return answer

    monkeypatch.setitem(sys.modules, "local_llm", types.SimpleNamespace(chat=fake_chat))
    assert mp._respond_to_ocr_text("補助申請條件", user_prompt=request) == answer
    assert request in captured["prompt"]


def test_ocr_explicit_request_skips_default_cache_read_and_write(monkeypatch, _vision_down):
    reads, writes = [], []
    request = "請列正反方"
    answer = "正方：可降低成本。\n反方：仍要審查資格。"
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *_a: reads.append(True) or "舊的預設回答")
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *_a: writes.append(True))

    def respond(ocr, user_prompt=""):
        assert user_prompt == request
        return answer

    monkeypatch.setattr(mp, "_respond_to_ocr_text", respond)
    assert mp.analyze_image(b"synthetic", user_prompt=request, group_id="synthetic-group") == answer
    assert reads == []
    assert writes == []


@pytest.mark.parametrize("user_prompt", ["", "  "])
def test_default_image_request_still_reads_cache(monkeypatch, user_prompt):
    reads = []
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *_a: reads.append(True) or "先核對申請資格。")
    assert mp.analyze_image(b"synthetic", user_prompt=user_prompt) == "先核對申請資格。"
    assert reads == [True]
