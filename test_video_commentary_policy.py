from types import SimpleNamespace
import sys
import time
from contextlib import nullcontext

import pytest
import gemini_client
import lite_reply
import main
import media_pipeline as mp
import memory
from video_reply import VIDEO_COMMENTARY_CONTRACT, VIDEO_COMMENTARY_CONTRACT_NO_SEARCH, VIDEO_CACHE_VERSION


@pytest.mark.parametrize("user_prompt", ["", "這個做法合理嗎？"])
def test_local_video_always_transports_contract_and_cleans_frames(monkeypatch, user_prompt):
    prompts, cleaned, writes = [], [], []
    monkeypatch.setitem(sys.modules, "video_keyframes", SimpleNamespace(
        extract_keyframes=lambda *_a, **_k: ["frame"], cleanup=cleaned.append))
    monkeypatch.setitem(sys.modules, "vision_llm", SimpleNamespace(
        chat_with_images=lambda prompt, *_a, **_k: prompts.append(prompt) or "樣本有限，不能推廣到所有情況。"))
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *_a: None)
    monkeypatch.setattr(mp, "_maybe_write_media_cache", lambda *_a: writes.append(True))
    assert mp.analyze_video(b"synthetic", user_prompt, "group") == "樣本有限，不能推廣到所有情況。"
    assert VIDEO_COMMENTARY_CONTRACT in prompts[0]
    assert "沒有音訊或完整時序" in prompts[0]
    assert user_prompt in prompts[0]
    assert cleaned == [["frame"]]
    assert writes == ([] if user_prompt else [True])


def test_explicit_video_question_bypasses_default_cache(monkeypatch):
    monkeypatch.setattr(mp, "_maybe_lookup_media_cache", lambda *_a: pytest.fail("wrong question cache"))
    monkeypatch.setitem(sys.modules, "video_keyframes", SimpleNamespace(extract_keyframes=lambda *_a, **_k: []))
    assert mp.analyze_video(b"synthetic", "合理嗎", "group") is None


def test_video_cache_reads_and_writes_same_new_namespace(monkeypatch):
    seen = []
    monkeypatch.setattr(memory, "compute_sha256", lambda value: seen.append(value) or "hash")
    monkeypatch.setattr(memory, "lookup_media_cache", lambda *_a: None)
    monkeypatch.setattr(memory, "insert_media_cache", lambda *_a: None)
    data = b"x" * 2048
    mp._maybe_lookup_media_cache(data, "group", "video")
    mp._maybe_write_media_cache(data, "group", "video", None, "有依據的評論" * 100)
    assert seen == [VIDEO_CACHE_VERSION + data] * 2


@pytest.mark.parametrize("user_input", [
    "https://youtu.be/synthetic", "影片既有摘要：有限樣本。",
    [SimpleNamespace(text=None, inline_data=SimpleNamespace(mime_type="video/mp4")), "合理嗎"],
])
def test_gemini_video_contract_survives_all_input_forms(user_input):
    assert VIDEO_COMMENTARY_CONTRACT in gemini_client._build_system_instruction([], user_input=user_input)


@pytest.mark.parametrize("text", ["https://youtu.be/synthetic", "影片資訊開始\n" + "素材" * 300])
def test_lite_video_never_falls_through_to_raw_summary(monkeypatch, text):
    monkeypatch.setattr(lite_reply, "_try_local_llm", lambda *_a, **_k: None)
    monkeypatch.setattr(lite_reply, "_try_youtube_info_from_long_text", lambda *_a: pytest.fail("metadata leak"))
    monkeypatch.setattr(lite_reply, "_STAGE1_HANDLERS", [lambda *_a: pytest.fail("metadata leak")])
    monkeypatch.setattr(lite_reply, "_STAGE3_HANDLERS", [lambda *_a: pytest.fail("summary leak")])
    assert lite_reply.lite_reply(text) is None


def test_video_factual_followup_still_uses_deterministic_answer(monkeypatch):
    monkeypatch.setattr(lite_reply, "_STAGE1_HANDLERS", (lite_reply._try_unit_convert,))
    monkeypatch.setattr(lite_reply, "_try_local_llm", lambda *_a, **_k: None)
    assert "45.3592" in lite_reply.lite_reply("影片提到的 100 磅是多少公斤？")


def test_extracted_video_material_is_not_a_factual_query(monkeypatch):
    monkeypatch.setattr(lite_reply, "_STAGE1_HANDLERS", (lambda *_a: pytest.fail("not user question"),))
    monkeypatch.setattr(lite_reply, "_try_local_llm", lambda *_a, **_k: None)
    assert lite_reply.lite_reply("影片資訊開始\n字幕：100 磅是多少公斤？") is None


def test_local_text_video_contract_even_without_opinion_trigger(monkeypatch):
    captured = []
    monkeypatch.setitem(sys.modules, "local_llm", SimpleNamespace(
        chat=lambda *_a, **kw: captured.append(kw) or "有實質依據的客觀評論。"))
    monkeypatch.setattr(lite_reply, "_needs_lite_opinion_context", lambda *_a, **_k: False)
    assert lite_reply._try_local_llm("https://youtu.be/synthetic")
    assert VIDEO_COMMENTARY_CONTRACT_NO_SEARCH in captured[0]["system_prompt"]
    assert "需要外部查證的主張先查證" not in captured[0]["system_prompt"]


def test_direct_local_fallback_keeps_video_contract(monkeypatch):
    captured = []
    monkeypatch.setitem(sys.modules, "local_llm", SimpleNamespace(
        chat=lambda *_a, **kw: captured.append(kw) or "有限案例不能代表全體。"))
    assert main._local_text_llm_fallback("https://youtu.be/synthetic")
    assert VIDEO_COMMENTARY_CONTRACT_NO_SEARCH in captured[0]["system_prompt"]
    assert "需要外部查證的主張先查證" not in captured[0]["system_prompt"]


def test_stored_video_description_is_internal_material(monkeypatch):
    captured = []
    monkeypatch.setattr(memory, "get_raw_message_meta", lambda *_a: {"description": "舊影片摘要"})
    monkeypatch.setattr(memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(memory, "top_facts", lambda *_a: [])
    monkeypatch.setattr(memory, "append_turn", lambda *_a: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a: None)
    monkeypatch.setattr(main, "_llm_chat", lambda prompt, *_a: captured.append(prompt) or "有限案例不能代表全體。")
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: None)
    assert main._handle_quoted_media_description_fallback(SimpleNamespace(reply_token="synthetic"), "group", "合理嗎", "video", "影片")
    assert VIDEO_COMMENTARY_CONTRACT_NO_SEARCH in captured[0]
    assert "需要外部查證的主張先查證" not in captured[0]
    assert "不是已查證事實或對外回答" in captured[0]


def test_quoted_video_bytes_transport_commentary_contract(monkeypatch):
    captured = []
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(main, "_download_content", lambda *_a: b"synthetic video")
    monkeypatch.setattr(memory, "get_context", lambda *_a: [])
    monkeypatch.setattr(memory, "top_facts", lambda *_a: [])
    monkeypatch.setattr(memory, "append_turn", lambda *_a: None)
    monkeypatch.setattr(main, "_get_persona_notes", lambda *_a: [])
    monkeypatch.setattr(main, "_thinking_indicator", lambda *_a: nullcontext())
    monkeypatch.setattr(main, "_append_bot_turn", lambda *_a: None)
    monkeypatch.setattr(main, "_maybe_extract_facts", lambda *_a: None)
    monkeypatch.setattr(main, "_llm_chat", lambda parts, *_a: captured.append(parts) or "有限案例不能代表全體。")
    monkeypatch.setattr(main, "_reply", lambda *_a, **_k: None)
    main._handle_media_via_quote(SimpleNamespace(reply_token="synthetic"), "group", "合理嗎", "video", "[影片]")
    assert captured[0][0].inline_data.mime_type == "video/mp4"
    assert VIDEO_COMMENTARY_CONTRACT in captured[0][1]


def test_pending_video_without_commentary_completes_without_send(monkeypatch, tmp_path):
    import pending_store
    monkeypatch.setattr(main, "_PENDING_REPLY_ENABLED", True)
    monkeypatch.setattr(pending_store, "BASE", tmp_path)
    monkeypatch.setattr(pending_store, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(pending_store, "LOCK_PATH", tmp_path / "pending.lock")
    monkeypatch.setattr(main, "_MEDIA_DELIVERY_LOCK_DIR", str(tmp_path / "locks"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic")
    pending_store.add_unique("group", {"type": "video", "message_id": "synthetic-video", "media_path": str(video), "timestamp": time.time()})
    monkeypatch.setattr(main, "_try_acquire_drain_slot", lambda *_a: SimpleNamespace(release=lambda: None))
    monkeypatch.setattr(main, "_drop_stale_pending", lambda *_a: [])
    monkeypatch.setattr(main, "_run_media_analysis", lambda fn, *_a: fn())
    monkeypatch.setattr(mp, "analyze_video", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "MessagingApi", lambda *_a: pytest.fail("must not send"))
    main._drain_pending_for_group("group", source="test")
    assert pending_store.list_for_group("group") == []
    assert not pending_store.was_media_delivered("group", "synthetic-video")
