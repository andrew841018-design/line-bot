"""Synthetic image presentation and legacy-outbound regressions; no transport."""
import sys
import types

import pytest

import image_reply as policy
import main
import media_pipeline as mp
import vision_common


@pytest.mark.parametrize("prefix", ["", "## ", "- ", "▌ ", "• "])
@pytest.mark.parametrize("label", ["回應", "回覆", "最終回覆", "我的回應"])
def test_legacy_analysis_removed_before_line_formatting(prefix, label):
    raw = f"{prefix}圖片內容：SECRET_OCR\n{prefix}{label}：先核對總成本。"
    rendered = main._prepare_outbound_text(raw)
    assert "SECRET_OCR" not in rendered
    assert "先核對總成本。" in rendered
    assert main._prepare_outbound_text(rendered) == rendered


@pytest.mark.parametrize("text", [
    "圖片內容：SECRET_OCR",
    "以下是我的分析：\n圖片內容：SECRET_OCR",
    "📷 補回之前漏掉的圖片\n\n圖片分析：\nSECRET_OCR",
    "⚠️ 文字辨識降級結果：\nOCR 文字摘錄：SECRET_OCR",
])
def test_analysis_only_is_rejected_without_footer_or_fake_answer(text):
    assert main._is_user_rejected_degraded_outbound(text)
    assert main._prepare_outbound_text(text) == ""


@pytest.mark.parametrize("text", [
    "辨識結果：明天三點開會。",
    "畫面描述：車輛在紅燈後繼續通過。\n結論：影片有違規情節。",
    "解析內容：這是語音處理結果。",
    "請不要把圖片內容當成已查證的事實。",
    "OCR 可能誤讀，請先核對總額再付款。",
])
def test_global_fence_preserves_non_image_and_ordinary_sentences(text):
    assert not policy.has_image_analysis_envelope(text)
    assert main._prepare_outbound_text(text) == main._md_to_line(text)


def test_image_prompt_is_separate_from_video_prompt():
    prompt = vision_common.compose_prompt(mp._build_image_argument_prompt("這樣合理嗎？"))
    assert "只供內部參考" in prompt
    assert "只有使用者明確要求才列正反方或來源清單" in prompt
    assert "禁止思緒、推理草稿、規則檢查" in prompt
    assert "不設最低字數" in prompt
    assert "必須保留" not in prompt
    video = vision_common.compose_prompt("摘要這段影片")
    assert policy.IMAGE_RESPONSE_MARKER not in video


def test_public_image_boundary_cleans_even_legacy_cache_stub(monkeypatch):
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *_a: (
        "圖片內容：SECRET_OCR\n回覆：先核對總成本。"
    ))
    assert mp.analyze_image(b"synthetic") == "回覆：先核對總成本。"


def test_cache_namespace_versions_each_media_policy(monkeypatch):
    import memory
    observed = []
    monkeypatch.setattr(memory, "compute_sha256", lambda value: observed.append(value) or "a" * 64)
    monkeypatch.setattr(memory, "lookup_media_cache", lambda *_a: None)
    data = b"x" * 2048
    mp._maybe_lookup_media_cache(data, "synthetic", "image")
    mp._maybe_lookup_media_cache(data, "synthetic", "video")
    assert observed == [mp._IMAGE_CACHE_VERSION + data, mp.VIDEO_CACHE_VERSION + data]


def test_quoted_image_model_failure_never_replays_description(monkeypatch):
    module = types.ModuleType("local_llm")
    module.chat = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("synthetic"))
    monkeypatch.setitem(sys.modules, "local_llm", module)
    monkeypatch.setattr(main.memory, "get_raw_message_meta", lambda *_a: {"description": "SECRET_OCR"})
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    sent = []
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: sent.append(True))
    event = types.SimpleNamespace(reply_token="synthetic")
    assert not main._handle_quoted_media_description_fallback(event, "group", "合理嗎", "image", "圖片")
    assert sent == []


def test_one_shot_legacy_analysis_is_purged_without_send(monkeypatch):
    payload = {"group": "圖片內容：SECRET_OCR"}
    saved, sent, completed = [], [], []
    monkeypatch.setattr(main, "_load_one_shot_replies", lambda: dict(payload))
    monkeypatch.setattr(main, "_save_one_shot_replies", lambda value: saved.append(dict(value)))
    monkeypatch.setattr(main, "_mark_inbound_reply_completed_no_reply", lambda token: completed.append(token) or True)
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: sent.append(True))
    main._try_one_shot_reply(types.SimpleNamespace(reply_token="synthetic"), "group")
    assert saved == [{}]
    assert completed == ["synthetic"]
    assert sent == []


@pytest.mark.parametrize("header", [
    "圖片內容（OCR）：", "圖片內容 (OCR)：", "📷 圖片內容：", "圖片解析如下：",
    "## **圖片內容（OCR）**：",
])
def test_annotated_analysis_headers_never_reach_line(header):
    assert main._prepare_outbound_text(f"{header}SECRET_OCR\n回應：先核對總成本。") == "回應：先核對總成本。"
    assert main._prepare_outbound_text(f"{header}SECRET_OCR") == ""


def test_nested_legacy_ocr_wrapper_is_silent():
    raw = "📷 補回之前漏掉的圖片\n\n⚠️ 文字辨識降級結果\nSECRET_OCR"
    assert policy.render_image_reply(raw) is None
    assert policy.has_image_analysis_envelope(raw)
    assert main._prepare_outbound_text(raw) == ""


def test_raw_ocr_echo_rejected_before_post_check_can_rephrase_it(monkeypatch):
    raw = "圖片中顯示這是一份合成單據，測試總額 1800 元。"
    module = types.ModuleType("local_llm")
    module.chat = lambda *_a, **_k: raw
    monkeypatch.setitem(sys.modules, "local_llm", module)
    monkeypatch.setattr(vision_common, "post_check", lambda text: "從图裡看，" + text)
    assert mp._respond_to_ocr_text(raw) is None


@pytest.mark.parametrize("prefix", ["", "回覆：", "圖片內容：內部解析\n回覆："])
def test_truncated_ocr_echo_is_not_an_answer(monkeypatch, prefix):
    raw = "合成測試文字。" * 250
    module = types.ModuleType("local_llm")
    module.chat = lambda text, **_k: prefix + text
    monkeypatch.setitem(sys.modules, "local_llm", module)
    assert mp._respond_to_ocr_text(raw) is None


def test_vision_post_check_cannot_turn_ocr_echo_into_answer(monkeypatch):
    raw = "圖片中顯示這是一份合成單據，測試總額 1800 元。"
    ocr = types.ModuleType("ocr_helper")
    ocr.extract_text = lambda *_a: raw
    vision = types.ModuleType("vision_llm")
    vision.describe_image = lambda *_a, **_k: vision_common.post_check(raw)
    monkeypatch.setitem(sys.modules, "ocr_helper", ocr)
    monkeypatch.setitem(sys.modules, "vision_llm", vision)
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *_a: None)
    assert mp.analyze_image(b"synthetic", user_prompt="合理嗎") is None


def test_quoted_image_never_echoes_cached_description(monkeypatch):
    raw = "這是合成圖片解析，測試總額 1800 元。"
    module = types.ModuleType("local_llm")
    module.chat = lambda *_a, **_k: raw
    monkeypatch.setitem(sys.modules, "local_llm", module)
    monkeypatch.setattr(main.memory, "get_raw_message_meta", lambda *_a: {"description": raw})
    monkeypatch.setattr(main.memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(main.memory, "append_turn", lambda *_a: None)
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a: None)
    sent = []
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: sent.append(True))
    event = types.SimpleNamespace(reply_token="synthetic")
    assert not main._handle_quoted_media_description_fallback(event, "group", "合理嗎", "image", "圖片")
    assert sent == []
