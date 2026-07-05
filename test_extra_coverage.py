"""
Extra coverage tests — targets handler functions, helpers, and edge branches
not reached by test_handlers.py / test_coverage.py.
"""

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402
from linebot.v3.webhooks import (  # noqa: E402
    AudioMessageContent,
    FileMessageContent,
    GroupSource,
    ImageMessageContent,
    MessageEvent,
    TextMessageContent,
    VideoMessageContent,
)

PASS = 0
FAIL = 0


def check(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")
        raise AssertionError(label)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_group_source(group_id="GRP001", user_id="USR001"):
    src = MagicMock(spec=GroupSource)
    src.group_id = group_id
    src.user_id = user_id
    src.type = "group"
    return src


def _make_text_msg(text="你好", msg_id="MSG001"):
    return TextMessageContent(id=msg_id, text=text, quoteToken="qt")


def _make_message_event(msg, source=None, reply_token="TOKEN001"):
    evt = MagicMock(spec=MessageEvent)
    evt.message = msg
    evt.source = source or _make_group_source()
    evt.reply_token = reply_token
    evt.timestamp = int(time.time() * 1000)
    dctx = MagicMock()
    dctx.is_redelivery = False
    evt.delivery_context = dctx
    return evt


# ═══════════════════════════════════════════════════════════════════════════════
# Test A: _handle_audio_message
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_audio_message():
    print("\n── Test A: _handle_audio_message ──")
    aud_msg = MagicMock(spec=AudioMessageContent)
    aud_msg.id = "AUD001"
    evt = _make_message_event(aud_msg)

    # quota exhausted → return early
    main._quota_exhausted_until_ts = time.time() + 3600
    with patch("main._download_content") as mock_dl:
        main._handle_audio_message(evt, "GRP001")
    check("quota 爆 audio → 不 download", not mock_dl.called)
    main._quota_exhausted_until_ts = 0.0

    # download fails → return
    with (
        patch("main._download_content", side_effect=Exception("dl fail")),
        patch("main._reply") as mock_reply,
    ):
        main._handle_audio_message(evt, "GRP001")
    check("download 失敗 → 不 reply", not mock_reply.called)

    # data too large → return silently
    big_data = b"x" * (main._MEDIA_BYTE_LIMIT + 1)
    with (
        patch("main._download_content", return_value=big_data),
        patch("main._reply") as mock_reply2,
    ):
        main._handle_audio_message(evt, "GRP001")
    check("data 超大 → 不 reply", not mock_reply2.called)

    # Gemini quota error during chat → _mark_quota_exhausted
    small_data = b"x" * 100
    quota_exc = Exception("429 RESOURCE_EXHAUSTED PerDay")
    with (
        patch("main._download_content", return_value=small_data),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", side_effect=quota_exc),
        patch("main._mark_quota_exhausted") as mock_mark,
    ):
        main._handle_audio_message(evt, "GRP001")
    check("Gemini quota error → mark exhausted", mock_mark.called)

    # Normal path → reply
    with (
        patch("main._download_content", return_value=small_data),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="語音分析結果"),
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._reply") as mock_reply3,
    ):
        main._handle_audio_message(evt, "GRP001")
    check("normal audio → _reply called", mock_reply3.called)

    # Empty reply from LLM → not sent
    with (
        patch("main._download_content", return_value=small_data),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value=""),
        patch("main._reply") as mock_reply4,
    ):
        main._handle_audio_message(evt, "GRP001")
    check("空 LLM 回覆 → 不 reply", not mock_reply4.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test B: _handle_explicit_text exception paths
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_explicit_text_exceptions():
    print("\n── Test B: _handle_explicit_text exception paths ──")
    msg = _make_text_msg("/ai 問題")
    evt = _make_message_event(msg)

    # Gemini quota error
    quota_exc = Exception("429 RESOURCE_EXHAUSTED PerDay free_tier_requests")
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", return_value="問題"),
        patch("main._llm_chat", side_effect=quota_exc),
        patch("main._mark_quota_exhausted") as mock_mark,
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(evt, "GRP001", "問題")
    check("explicit quota error → mark exhausted", mock_mark.called)
    check(
        "explicit quota error → visible degraded reply",
        mock_reply.call_args
        and mock_reply.call_args.args[1] == main._visible_llm_degraded_reply(),
    )

    # Quota on first call, local fallback text on retry → reply that text.
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", return_value="問題"),
        patch("main._llm_chat", side_effect=[quota_exc, "本機 explicit 回覆"]),
        patch("main._mark_quota_exhausted") as mock_mark_retry,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply_retry,
    ):
        main._handle_explicit_text(evt, "GRP001", "問題")
    sent_retry = mock_reply_retry.call_args[0][1] if mock_reply_retry.called else ""
    check("explicit quota retry → mark exhausted", mock_mark_retry.called)
    check("explicit quota retry → reply local text", sent_retry == "本機 explicit 回覆")

    # Gemini transient unavailable → local fallback, not suppressible system status.
    unavailable_exc = Exception("503 UNAVAILABLE high demand")
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", return_value="問題"),
        patch("main._llm_chat", side_effect=unavailable_exc),
        patch("main._local_text_llm_fallback", return_value="本機 explicit 503 回覆") as mock_local,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._try_save_correction"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply_unavailable,
    ):
        main._handle_explicit_text(evt, "GRP001", "問題")
    sent_unavailable = (
        mock_reply_unavailable.call_args[0][1]
        if mock_reply_unavailable.called
        else ""
    )
    check("explicit 503 → local fallback called", mock_local.called)
    check(
        "explicit 503 → reply local text",
        sent_unavailable == "本機 explicit 503 回覆",
    )

    # Non-quota exception → reply with error
    other_exc = Exception("500 internal server error")
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", return_value="問題"),
        patch("main._llm_chat", side_effect=other_exc),
        patch("main._reply") as mock_reply2,
    ):
        main._handle_explicit_text(evt, "GRP001", "問題")
    check("explicit 500 error → reply error msg", mock_reply2.called)

    # Media quote path
    msg2 = _make_text_msg("/ai 分析圖片")
    msg2.quoted_message_id = "QUOTED_MEDIA"
    evt2 = _make_message_event(msg2)
    with (
        patch("main.memory.get_raw_message", return_value=("__bot__", "[圖片]")),
        patch("main._handle_media_via_quote") as mock_media,
    ):
        main._handle_explicit_text(evt2, "GRP001", "分析圖片")
    check("引用媒體 → _handle_media_via_quote called", mock_media.called)

    # Quoted block + empty clean text → special prompt
    msg3 = _make_text_msg("/ai")
    msg3.quoted_message_id = "QUOTED_ID"
    evt3 = _make_message_event(msg3)
    with (
        patch("main.memory.get_raw_message", return_value=None),
        patch("main._build_quoted_block", return_value="(原文 block)"),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", return_value="針對引用回覆"),
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._try_save_correction"),
        patch("main._reply") as mock_reply3,
    ):
        main._handle_explicit_text(evt3, "GRP001", "")
    check("空 clean_text + quoted block → reply", mock_reply3.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test C: _handle_burst_flush success + error paths
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_burst_flush_paths():
    print("\n── Test C: _handle_burst_flush 成功/錯誤路徑 ──")
    main._quota_exhausted_until_ts = 0.0

    # Gemini quota error → mark + return
    quota_exc = Exception("429 RESOURCE_EXHAUSTED PerDay free_tier_requests")
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", side_effect=quota_exc),
        patch("main._mark_quota_exhausted") as mock_mark,
        patch("main._maybe_capture_calendar_event"),
    ):
        main._handle_burst_flush("GRP001", "測試文字", "TOKEN")
    check("burst Gemini quota → mark exhausted", mock_mark.called)

    # First Gemini call hits quota, retry returns local LLM text. The retry result
    # must continue through the normal reply path instead of being swallowed by
    # the original exception handler.
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", side_effect=[quota_exc, "本機回覆"]),
        patch("main._mark_quota_exhausted") as mock_mark2,
        patch("main.memory.store_fact_cache") as mock_store2,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply_local,
    ):
        main._handle_burst_flush("GRP001", "測試文字", "TOKEN")
    sent_text = mock_reply_local.call_args[0][1] if mock_reply_local.called else ""
    check("burst quota retry → mark exhausted", mock_mark2.called)
    check("burst quota retry → reply local LLM text", sent_text == "本機回覆")
    check("burst quota retry → store local LLM text", mock_store2.called)

    # Gemini transient/unavailable error should also degrade to local LLM for
    # burst replies instead of sending a suppressible system-status message.
    unavailable_exc = Exception("503 UNAVAILABLE high demand")
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", side_effect=unavailable_exc),
        patch("main._local_text_llm_fallback", return_value="本機503回覆") as mock_local,
        patch("main.memory.store_fact_cache") as mock_store503,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply503,
    ):
        main._handle_burst_flush("GRP001", "測試文字", "TOKEN")
    sent_text503 = mock_reply503.call_args[0][1] if mock_reply503.called else ""
    check("burst 503 → local fallback called", mock_local.called)
    check("burst 503 → reply local LLM text", sent_text503 == "本機503回覆")
    check("burst 503 → store local LLM text", mock_store503.called)

    # Non-quota error → reply error
    other_exc = Exception("500 server down")
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", side_effect=other_exc),
        patch("main._reply") as mock_reply,
    ):
        main._handle_burst_flush("GRP001", "測試文字", "TOKEN")
    check("burst 500 error → reply error msg", mock_reply.called)

    # Empty LLM reply → send short fallback instead of silent drop
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", return_value=""),
        patch("main._reply") as mock_reply2,
    ):
        main._handle_burst_flush("GRP001", "測試文字", "TOKEN")
    check("burst 空回覆 → fallback reply", mock_reply2.called)

    # Success path → store cache + reply
    with (
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", return_value="好的分析結果"),
        patch("main.memory.store_fact_cache") as mock_store,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply") as mock_reply3,
    ):
        main._handle_burst_flush("GRP001", "測試文字", "TOKEN")
    check("burst 成功 → store_fact_cache", mock_store.called)
    check("burst 成功 → reply", mock_reply3.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test D: _handle_file_message
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_file_message():
    print("\n── Test D: _handle_file_message ──")

    def _make_file_msg(name="test.txt", fid="FILE001"):
        msg = MagicMock(spec=FileMessageContent)
        msg.id = fid
        msg.file_name = name
        msg.type = "file"
        msg.quote_token = "qt"
        return msg

    # Unsupported format → reply error
    msg = _make_file_msg("test.exe")
    evt = _make_message_event(msg)
    with patch("main._reply") as mock_reply:
        main._handle_file_message(evt, "GRP001")
    check("不支援格式 → reply 錯誤", mock_reply.called)
    check(
        "不支援格式 reply 含格式說明",
        "exe" in (mock_reply.call_args[0][1] if mock_reply.called else ""),
    )

    # Quota exhausted still attempts local/file fallback; do not hit real local_llm.
    msg2 = _make_file_msg("test.txt")
    evt2 = _make_message_event(msg2)
    main._quota_exhausted_until_ts = time.time() + 3600
    with (
        patch("main._download_content", return_value=b"quota fallback content") as mock_dl,
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="quota fallback reply"),
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._reply") as mock_reply2,
    ):
        main._handle_file_message(evt2, "GRP001")
    check("quota 爆 file → 仍嘗試 download", mock_dl.called)
    check("quota 爆 file → fallback reply", mock_reply2.called)
    main._quota_exhausted_until_ts = 0.0

    # Download fails → reply error
    msg3 = _make_file_msg("test.txt")
    evt3 = _make_message_event(msg3)
    with (
        patch("main._download_content", side_effect=Exception("dl fail")),
        patch("main._reply") as mock_reply3,
    ):
        main._handle_file_message(evt3, "GRP001")
    check("download 失敗 → reply error", mock_reply3.called)

    # Text file → LLM → reply
    msg4 = _make_file_msg("test.txt")
    evt4 = _make_message_event(msg4)
    txt_data = b"Hello world this is a text file content."
    with (
        patch("main._download_content", return_value=txt_data),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="分析結果"),
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._reply") as mock_reply4,
    ):
        main._handle_file_message(evt4, "GRP001")
    check("txt 檔 → reply", mock_reply4.called)

    # PDF (native) → LLM → reply
    msg5 = _make_file_msg("test.pdf")
    evt5 = _make_message_event(msg5)
    pdf_data = b"%PDF-1.4 fake content"
    with (
        patch("main._download_content", return_value=pdf_data),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="PDF 摘要"),
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._reply") as mock_reply5,
    ):
        main._handle_file_message(evt5, "GRP001")
    check("PDF 檔 → reply", mock_reply5.called)

    # Image file (native) → always local media_pipeline, never Gemini _llm_chat
    msg_img = _make_file_msg("photo.jpg")
    evt_img = _make_message_event(msg_img)
    fake_media = types.ModuleType("media_pipeline")
    fake_media.analyze_image = MagicMock(
        return_value=(
            "圖片內容：圖片在講產品廣告。\n"
            "正方：可快速看到主打賣點。\n"
            "反方：缺少來源與完整規格。\n"
            "統一論點：先當廣告線索，購買前回原頁查證。"
        )
    )
    with (
        patch.dict(sys.modules, {"media_pipeline": fake_media}),
        patch("main._download_content", return_value=b"fake image bytes"),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat") as mock_llm_chat_img,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._reply") as mock_reply_img,
    ):
        main._handle_file_message(evt_img, "GRP001")
    check("圖片檔 → local analyze_image", fake_media.analyze_image.called)
    check("圖片檔 → 不走 _llm_chat/Gemini", not mock_llm_chat_img.called)
    check("圖片檔 → reply", mock_reply_img.called)

    # Gemini quota error during file processing
    msg6 = _make_file_msg("test.txt")
    evt6 = _make_message_event(msg6)
    quota_exc = Exception("429 RESOURCE_EXHAUSTED PerDay free_tier_requests")
    with (
        patch("main._download_content", return_value=b"content"),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", side_effect=quota_exc),
        patch("main._mark_quota_exhausted") as mock_mark,
    ):
        main._handle_file_message(evt6, "GRP001")
    check("file Gemini quota → mark exhausted", mock_mark.called)

    # Office file (docx) with _extract_office_text returning None → reply error
    msg7 = _make_file_msg("test.docx")
    evt7 = _make_message_event(msg7)
    with (
        patch("main._download_content", return_value=b"fake docx"),
        patch("main._extract_office_text", return_value=None),
        patch("main._reply") as mock_reply7,
    ):
        main._handle_file_message(evt7, "GRP001")
    check("office 讀取失敗 → reply 錯誤", mock_reply7.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test E: _extract_office_text
# ═══════════════════════════════════════════════════════════════════════════════
def test_extract_office_text():
    print("\n── Test E: _extract_office_text ──")

    # Extension not in list → falls through → return None
    result = main._extract_office_text(b"data", "test.zip")
    check("zip → return None", result is None)

    # No extension
    result2 = main._extract_office_text(b"data", "noextension")
    check("無副檔名 → return None", result2 is None)

    # docx import failure → exception → return None
    with patch.dict("sys.modules", {"docx": None}):
        result3 = main._extract_office_text(b"fake docx", "test.docx")
    check("docx import 失敗 → return None", result3 is None)


# ═══════════════════════════════════════════════════════════════════════════════
# Test G: _handle_dinner_recommendation
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_dinner_recommendation():
    print("\n── Test G: _handle_dinner_recommendation ──")
    msg = _make_text_msg("今晚吃什麼")
    evt = _make_message_event(msg)

    # quota exhausted still goes through _llm_chat so local/lite fallback can answer.
    main._quota_exhausted_until_ts = time.time() + 3600
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="fallback dinner") as mock_llm,
        patch("main._reply") as mock_reply0,
    ):
        main._handle_dinner_recommendation(evt, "GRP001")
    check("quota 爆 dinner → 仍呼叫 LLM fallback", mock_llm.called)
    check("quota 爆 dinner → reply fallback 結果", mock_reply0.called)
    main._quota_exhausted_until_ts = 0.0

    # Quota error during dinner
    quota_exc = Exception("429 RESOURCE_EXHAUSTED PerDay free_tier_requests")
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", side_effect=quota_exc),
        patch("main._local_text_llm_fallback", return_value="local dinner") as mock_local,
        patch("main._mark_quota_exhausted") as mock_mark,
        patch("main._reply") as mock_reply_quota,
    ):
        main._handle_dinner_recommendation(evt, "GRP001")
    check("dinner quota error → mark exhausted", mock_mark.called)
    check("dinner quota error → local fallback", mock_local.called)
    check(
        "dinner quota error → reply local fallback",
        mock_reply_quota.call_args[0][1] == "local dinner",
    )

    # Gemini unavailable during dinner → local fallback
    unavailable_exc = Exception("503 UNAVAILABLE high demand")
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", side_effect=unavailable_exc),
        patch("main._local_text_llm_fallback", return_value="local dinner 503") as mock_local503,
        patch("main._reply") as mock_reply503,
    ):
        main._handle_dinner_recommendation(evt, "GRP001")
    check("dinner 503 → local fallback", mock_local503.called)
    check(
        "dinner 503 → reply local fallback",
        mock_reply503.call_args[0][1] == "local dinner 503",
    )

    # Non-quota error → reply error
    other_exc = Exception("500 server error")
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", side_effect=other_exc),
        patch("main._reply") as mock_reply,
    ):
        main._handle_dinner_recommendation(evt, "GRP001")
    check("dinner 500 error → reply error", mock_reply.called)

    # Success
    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._llm_chat", return_value="推薦餐廳"),
        patch("main._reply") as mock_reply2,
    ):
        main._handle_dinner_recommendation(evt, "GRP001")
    check("dinner 成功 → reply", mock_reply2.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test H: _handle_command extended paths
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_command_extended():
    print("\n── Test H: _handle_command 延伸路徑 ──")
    gid = "GRP001"

    # Empty args after strip: /記住  (double space → fact="")
    result = main._handle_command(gid, "/記住  ")
    check("/記住 空(double space) → 用法提示", "用法" in (result or ""))

    # Empty /忘記 arg
    result2 = main._handle_command(gid, "/忘記  ")
    check("/忘記 空 → 用法提示", "用法" in (result2 or ""))

    # Empty /不要回 arg
    result3 = main._handle_command(gid, "/不要回  ")
    check("/不要回 空 → 用法提示", "用法" in (result3 or ""))

    # Empty /以後要查 arg
    result4 = main._handle_command(gid, "/以後要查  ")
    check("/以後要查 空 → 用法提示", "用法" in (result4 or ""))

    # /檢討 → runs weekly review
    with patch("main.review.run_weekly_review", return_value=("檢討報告", {})):
        result5 = main._handle_command(gid, "/檢討")
    check("/檢討 → 回傳報告", "報告" in (result5 or ""))

    # /檢討 14 → runs with days=14
    with patch(
        "main.review.run_weekly_review", return_value=("14天報告", {})
    ) as mock_review2:
        main._handle_command(gid, "/檢討 14")
    check("/檢討 14 → 呼叫 run_weekly_review", mock_review2.called)

    # /檢討 abc → invalid days
    result7 = main._handle_command(gid, "/檢討 abc")
    check("/檢討 abc → 用法提示", "用法" in (result7 or ""))

    # /檢討 0 → out of range
    result8 = main._handle_command(gid, "/檢討 0")
    check("/檢討 0 → 範圍錯誤", "1~30" in (result8 or ""))

    # /採用 → list drafts (empty)
    with patch("main.memory.list_rule_drafts", return_value=[]):
        result9 = main._handle_command(gid, "/採用")
    check("/採用 無草稿 → 提示", "沒有" in (result9 or ""))

    # /採用 → list drafts (non-empty)
    drafts = [{"draft_id": 1, "kind": "skip", "pattern": "早安", "reason": "太煩"}]
    with patch("main.memory.list_rule_drafts", return_value=drafts):
        result10 = main._handle_command(gid, "/採用")
    check("/採用 有草稿 → 含用法", "用法" in (result10 or ""))

    # /採用 全部
    with patch("main.review.adopt_drafts", return_value=([], "已採用")) as mock_adopt:
        main._handle_command(gid, "/採用 全部")
    check("/採用 全部 → 呼叫 adopt_drafts", mock_adopt.called)

    # /閉嘴 (no reason) → usage hint
    result12 = main._handle_command(gid, "/閉嘴")
    check("/閉嘴 無理由 → 用法", "用法" in (result12 or ""))

    # /閉嘴 with reason → _handle_layer2_correction
    with patch("main._handle_layer2_correction", return_value="已糾正") as mock_l2:
        main._handle_command(gid, "/閉嘴 太囉嗦了")
    check("/閉嘴 有理由 → _handle_layer2_correction", mock_l2.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test I: _handle_layer2_correction + _guess_last_trigger_text
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_layer2_correction():
    print("\n── Test I: _handle_layer2_correction + _guess_last_trigger_text ──")

    # No last bot reply → error message
    with patch("main.memory.get_last_bot_reply", return_value=None):
        result = main._handle_layer2_correction("GRP001", "不要這樣回")
    check("無 bot 回覆 → 錯誤提示", "找不到" in (result or ""))

    # Has reply but generate_filter_rule returns empty → fallback
    with (
        patch("main.memory.get_last_bot_reply", return_value=("ts", "之前的回覆")),
        patch("main._guess_last_trigger_text", return_value="觸發文字"),
        patch("main.gemini_client.generate_filter_rule", return_value=""),
    ):
        result2 = main._handle_layer2_correction("GRP001", "太囉嗦了")
    check("規則生成失敗 → fallback 提示", "自動生成規則失敗" in (result2 or ""))

    # Success path
    with (
        patch("main.memory.get_last_bot_reply", return_value=("ts", "之前的回覆")),
        patch("main._guess_last_trigger_text", return_value="觸發文字"),
        patch("main.gemini_client.generate_filter_rule", return_value="早安問候"),
        patch("main.memory.add_filter_rule", return_value=5),
    ):
        result3 = main._handle_layer2_correction("GRP001", "不要回早安")
    check("規則生成成功 → 含規則 ID", "#5" in (result3 or ""))


def test_guess_last_trigger_text():
    print("\n── Test I.2: _guess_last_trigger_text ──")

    # No bot in recent → empty string
    with patch("main.memory.get_recent_raw_messages", return_value=[]):
        result = main._guess_last_trigger_text("GRP001")
    check("無 recent → 空字串", result == "")

    # Bot found → collect user messages before it
    recent = [
        ("m1", "USR001", "第一則", 1000),
        ("m2", "USR002", "第二則", 1001),
        ("m3", "__bot__", "bot 回覆", 1002),
        ("m4", "USR001", "後來的", 1003),
    ]
    with patch("main.memory.get_recent_raw_messages", return_value=recent):
        result2 = main._guess_last_trigger_text("GRP001")
    check("bot 前的訊息 → 回傳 user 訊息", "第一則" in result2 and "第二則" in result2)
    check("bot 後的訊息 → 不含", "後來的" not in result2)

    # No bot in recent (only user messages)
    recent_no_bot = [
        ("m1", "USR001", "只有這則", 1000),
    ]
    with patch("main.memory.get_recent_raw_messages", return_value=recent_no_bot):
        result3 = main._guess_last_trigger_text("GRP001")
    check("無 bot message → 空字串", result3 == "")


# ═══════════════════════════════════════════════════════════════════════════════
# Test J: _pop_pending_for_piggyback
# ═══════════════════════════════════════════════════════════════════════════════
def test_pop_pending_for_piggyback():
    print("\n── Test J: _pop_pending_for_piggyback ──")
    import pending_store

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        tmp = f.name

    orig_path = main._PENDING_EXPLICIT_PATH
    orig_pending_enabled = main._PENDING_REPLY_ENABLED
    orig_store_path = pending_store.PENDING_PATH
    orig_store_lock = pending_store.LOCK_PATH
    main._PENDING_REPLY_ENABLED = True
    pending_path = Path(tmp)
    pending_lock_path = Path(tmp + ".lock")

    try:
        # Empty pending → None
        with open(tmp, "w") as f:
            json.dump({}, f)
        main._PENDING_EXPLICIT_PATH = tmp
        pending_store.PENDING_PATH = pending_path
        pending_store.LOCK_PATH = pending_lock_path
        result = main._pop_pending_for_piggyback("GRP001")
        check("空 pending → None", result is None)

        # Non-text batch → None
        with open(tmp, "w") as f:
            json.dump({"GRP001": [{"type": "audio", "text": ""}]}, f)
        result2 = main._pop_pending_for_piggyback("GRP001")
        check("非文字 pending → None", result2 is None)

        # LLM fails → None
        with open(tmp, "w") as f:
            json.dump({"GRP001": [{"type": "text", "text": "待回訊息"}]}, f)
        with (
            patch("main.memory.top_facts", return_value=[]),
            patch("main.memory.get_context", return_value=[]),
            patch("main._get_persona_notes", return_value=[]),
            patch("main._llm_chat", side_effect=Exception("fail")),
        ):
            result3 = main._pop_pending_for_piggyback("GRP001")
        check("LLM 失敗 → None", result3 is None)

        # Empty LLM reply → None
        with open(tmp, "w") as f:
            json.dump({"GRP001": [{"type": "text", "text": "待回訊息"}]}, f)
        with (
            patch("main.memory.top_facts", return_value=[]),
            patch("main.memory.get_context", return_value=[]),
            patch("main._get_persona_notes", return_value=[]),
            patch("main._llm_chat", return_value=""),
        ):
            result4 = main._pop_pending_for_piggyback("GRP001")
        check("LLM 空回覆 → None", result4 is None)

        # Success path → returns formatted string + removes from pending
        items = [{"type": "text", "text": "待回訊息", "message_id": "MSG_PIG"}]
        with open(tmp, "w") as f:
            json.dump({"GRP001": items}, f)
        with (
            patch("main.memory.top_facts", return_value=[]),
            patch("main.memory.get_context", return_value=[]),
            patch("main._get_persona_notes", return_value=[]),
            patch("main._llm_chat", return_value="piggyback 回覆"),
        ):
            result5 = main._pop_pending_for_piggyback("GRP001")
        data_after = json.load(open(tmp))
        check(
            "piggyback 成功 → 回傳格式化字串",
            (
                result5 is not None
                and ("piggyback" in result5.lower() or "補回" in result5)
            ),
        )
        check("piggyback 成功 → pending 已清空", "GRP001" not in data_after)
    finally:
        main._PENDING_EXPLICIT_PATH = orig_path
        main._PENDING_REPLY_ENABLED = orig_pending_enabled
        pending_store.PENDING_PATH = orig_store_path
        pending_store.LOCK_PATH = orig_store_lock
        os.unlink(tmp)
        try:
            os.unlink(pending_lock_path)
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Test K: _gemini_group_messages
# ═══════════════════════════════════════════════════════════════════════════════
def test_gemini_group_messages():
    print("\n── Test K: _gemini_group_messages ──")

    # Empty items → []
    result = main._gemini_group_messages([])
    check("空 items → []", result == [])

    # Gemini fails → heuristic fallback (grok 已移除)
    items = [
        {"type": "text", "text": "第一則", "user_id": "USR1", "timestamp": 1000},
        {"type": "text", "text": "第二則", "user_id": "USR2", "timestamp": 1001},
    ]
    with (
        patch("main.gemini_client._client") as mock_client2,
        patch(
            "main._heuristic_group_messages",
            return_value=[{"idxs": [0, 1], "reply_to": 1}],
        ) as mock_heuristic,
    ):
        mock_client2.models.generate_content.side_effect = Exception("Gemini down")
        main._gemini_group_messages(items)
    check("Gemini 失敗 → heuristic fallback", mock_heuristic.called)

    # Gemini returns valid JSON → parse correctly
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({"groups": [{"idxs": [0, 1], "reply_to": 1}]})
    with patch("main.gemini_client._client") as mock_client3:
        mock_client3.models.generate_content.return_value = mock_resp
        result4 = main._gemini_group_messages(items)
    check("Gemini 正常回傳 → 解析分組", len(result4) > 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Test L: _build_group_parts
# ═══════════════════════════════════════════════════════════════════════════════
def test_build_group_parts():
    print("\n── Test L: _build_group_parts ──")

    # Text only
    items = [{"type": "text", "text": "第一則"}, {"type": "text", "text": "第二則"}]
    with patch("main._prefetch_urls", side_effect=lambda x: x):
        parts = main._build_group_parts(items, "GRP001")
    check("text only → parts 有 combined text", len(parts) > 0)

    # Text with quoted_original
    items2 = [{"type": "text", "text": "回覆", "quoted_original": "原文"}]
    with patch("main._prefetch_urls", side_effect=lambda x: x):
        parts2 = main._build_group_parts(items2, "GRP001")
    check("有 quoted_original → parts 含原文", any("原文" in str(p) for p in parts2))

    # File item with missing path
    items3 = [
        {
            "type": "file",
            "text": "",
            "file_name": "test.pdf",
            "media_path": "/nonexistent/path.pdf",
        }
    ]
    with patch("main._prefetch_urls", side_effect=lambda x: x):
        parts3 = main._build_group_parts(items3, "GRP001")
    check("file 路徑不存在 → parts 含遺失提示", any("遺失" in str(p) for p in parts3))

    # Audio item with missing path
    items4 = [
        {
            "type": "audio",
            "text": "",
            "mime_type": "audio/m4a",
            "media_path": "/nonexistent/audio.m4a",
        }
    ]
    with patch("main._prefetch_urls", side_effect=lambda x: x):
        parts4 = main._build_group_parts(items4, "GRP001")
    check("audio 路徑不存在 → parts 含遺失提示", any("遺失" in str(p) for p in parts4))

    # Empty items → empty parts
    parts5 = main._build_group_parts([], "GRP001")
    check("空 items → 空 parts", parts5 == [])


# ═══════════════════════════════════════════════════════════════════════════════
# Test M: small helpers
# ═══════════════════════════════════════════════════════════════════════════════
def test_small_helpers():
    print("\n── Test M: 小 helper 函式 ──")

    # _get_member_display_name
    check(
        "user_id=None → 某人", main._get_member_display_name("GRP001", None) == "某人"
    )
    check(
        "user_id=__bot__ → 我 (bot)",
        main._get_member_display_name("GRP001", "__bot__") == "我 (bot)",
    )

    with patch("main.ApiClient") as mock_api_cls:
        mock_api_cls.side_effect = Exception("API fail")
        name = main._get_member_display_name("GRP001", "USR001")
    check("API 失敗 → 群組成員", name == "群組成員")

    # _get_persona_notes
    with patch(
        "main.memory.list_persona_notes", return_value=[{"note": "test"}]
    ) as mock_notes:
        main._get_persona_notes("GRP001")
    check("_get_persona_notes → calls list_persona_notes", mock_notes.called)

    # _try_save_correction
    with patch("main.memory.add_persona_note") as mock_add:
        main._try_save_correction("GRP001", "x")  # too short → skip
    check("糾正太短 → 不 add", not mock_add.called)

    with patch("main.memory.add_persona_note") as mock_add2:
        main._try_save_correction("GRP001", "這是一段普通訊息，沒有任何糾正關鍵字")
    check("無糾正關鍵字 → 不 add", not mock_add2.called)

    with patch("main.memory.add_persona_note") as mock_add3:
        main._try_save_correction("GRP001", "以後不要這樣回")
    check("有糾正關鍵字 → add persona note", mock_add3.called)

    # _maybe_extract_facts - bump returns True
    with (
        patch("main.memory.bump_and_should_extract", return_value=True),
        patch("main.gemini_client.extract_facts", return_value=["新事實"]),
        patch("main.memory.add_fact", return_value=True),
        patch("main.memory.list_facts", return_value=["新事實"]),
    ):
        main._maybe_extract_facts("GRP001")  # should not raise
    check("_maybe_extract_facts bump=True → 不爆", True)

    # _maybe_extract_facts - bump returns False → skip
    with (
        patch("main.memory.bump_and_should_extract", return_value=False),
        patch("main.gemini_client.extract_facts") as mock_extract,
    ):
        main._maybe_extract_facts("GRP001")
    check("_maybe_extract_facts bump=False → 不 extract", not mock_extract.called)


# ═══════════════════════════════════════════════════════════════════════════════
# Test N: _build_quoted_block recent messages fallback
# ═══════════════════════════════════════════════════════════════════════════════
def test_build_quoted_block_recent():
    print("\n── Test N: _build_quoted_block recent fallback ──")

    msg = _make_text_msg("回覆")
    msg.quoted_message_id = "MISSING_ID"

    # DB 找不到 + 有近期對話 → 用近期對話
    recent = [("m1", "USR001", "說了這些話", 1000)]
    with (
        patch("main.memory.get_raw_message", return_value=None),
        patch("main.memory.get_recent_raw_messages", return_value=recent),
        patch("main._get_member_display_name", return_value="某人"),
    ):
        result = main._build_quoted_block(msg, "GRP001")
    check(
        "找不到但有近期對話 → 含近期訊息",
        result is not None and "說了這些話" in (result or ""),
    )

    # DB 找不到 + 無近期對話
    with (
        patch("main.memory.get_raw_message", return_value=None),
        patch("main.memory.get_recent_raw_messages", return_value=[]),
    ):
        result2 = main._build_quoted_block(msg, "GRP001")
    check(
        "找不到且無近期對話 → 包含說明",
        result2 is not None and "不在記憶中" in (result2 or ""),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test O: grok_client edge cases — 已移除（2026-04-26 grok 改為 stub）
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# Test P: _reply non-muted paths (temporarily unmute)
# ═══════════════════════════════════════════════════════════════════════════════
def test_reply_non_muted():
    print("\n── Test P: _reply non-muted ──")
    orig_muted = main.settings.bot_muted
    main.settings.bot_muted = False

    # Normal reply → calls LINE API
    with (
        patch("main.ApiClient") as mock_api_cls,
        patch("main._get_quota_footer", return_value=""),
        patch("main._pop_pending_for_piggyback", return_value=None),
    ):
        mock_ctx = MagicMock()
        mock_api_cls.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_api_cls.return_value.__exit__ = MagicMock(return_value=False)
        MagicMock()
        mock_ctx.__class__ = MagicMock
        # Just mock the entire ApiClient chain
        with patch("main.MessagingApi") as mock_msg_api:
            mock_instance = MagicMock()
            mock_msg_api.return_value = mock_instance
            resp_mock = MagicMock()
            resp_mock.sent_messages = []
            mock_instance.reply_message.return_value = resp_mock
            main._reply("TOKEN", "測試回覆", group_id="GRP001")
    check("non-muted _reply → 不爆", True)

    # Reply fails → fallback push
    with (
        patch("main.ApiClient"),
        patch("main._get_quota_footer", return_value=""),
        patch("main._pop_pending_for_piggyback", return_value=None),
        patch("main.memory.log_raw_message"),
    ):
        with patch("main.MessagingApi") as mock_msg_api2:
            mock_instance2 = MagicMock()
            mock_msg_api2.return_value = mock_instance2
            mock_instance2.reply_message.side_effect = Exception("expired token")
            mock_instance2.push_message.return_value = None
            main._reply("EXPIRED_TOKEN", "重送訊息", group_id="GRP001")
    check("reply 過期 → fallback push 不爆", True)

    # Empty text → no-op
    with patch("main.MessagingApi") as mock_msg_api3:
        main._reply("TOKEN", "", group_id="GRP001")
    check("空文字 non-muted → 不呼叫 API", not mock_msg_api3.called)

    main.settings.bot_muted = orig_muted


# ═══════════════════════════════════════════════════════════════════════════════
# Test Q: _get_quota_footer paths
# ═══════════════════════════════════════════════════════════════════════════════
def test_get_quota_footer():
    print("\n── Test Q: _get_quota_footer ──")

    # info is None → return ""
    main._quota_exhausted_until_ts = 0.0
    with patch("main.gemini_client.get_gemini_quota_info", return_value=None):
        result = main._get_quota_footer()
    check("gemini info=None → 空字串", result == "")

    main._quota_exhausted_until_ts = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Test R: _next_gemini_reset_tw edge cases
# ═══════════════════════════════════════════════════════════════════════════════
def test_next_gemini_reset_tw():
    print("\n── Test R: _next_gemini_reset_tw 「還有N分鐘」路徑 ──")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Mock time such that reset is < 60 minutes away → "還有N分鐘"
    _PT_TZ = ZoneInfo("America/Los_Angeles")
    almost_reset = datetime.now(tz=_PT_TZ).replace(
        hour=23, minute=59, second=0, microsecond=0
    )
    with patch("main.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: (
            almost_reset.astimezone(tz) if tz else almost_reset
        )
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        abs_str, rel_str = main._next_gemini_reset_tw()
    check("rel_str 含「還有」", "還有" in rel_str)


# ═══════════════════════════════════════════════════════════════════════════════
# Test S: prefetch error branches
# ═══════════════════════════════════════════════════════════════════════════════
def test_prefetch_error_branches():
    print("\n── Test S: prefetch 錯誤分支 ──")

    # TikTok short URL with error code response
    with patch("main._requests") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 400, "message": "not found"}
        mock_req.get.return_value = mock_resp
        result = main._fetch_tiktok_meta("https://www.tiktok.com/@user/video/123")
    check("tiktok error code → None", result is None)

    # TikTok empty title+author
    with patch("main._requests") as mock_req2:
        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = {"title": "", "author_name": "", "html": ""}
        mock_req2.get.return_value = mock_resp2
        result2 = main._fetch_tiktok_meta("https://www.tiktok.com/@user/video/456")
    check("tiktok 空 title+author → None", result2 is None)

    # TikTok HTTP non-200
    with patch("main._requests") as mock_req3:
        mock_resp3 = MagicMock()
        mock_resp3.status_code = 404
        mock_req3.get.return_value = mock_resp3
        result3 = main._fetch_tiktok_meta("https://www.tiktok.com/@user/video/789")
    check("tiktok 404 → None", result3 is None)

    # _prefetch_urls: youtube all fetchers fail but still add a context block
    text_with_yt = "看這個 https://youtube.com/watch?v=TEST123"
    with (
        patch("main._fetch_video_ytdlp", return_value=None),
        patch("main._fetch_youtube_meta", return_value=None),
        patch("main._fetch_youtube_html_meta", return_value=None),
    ):
        result4 = main._prefetch_urls(text_with_yt)
    check("youtube 全失敗 → 仍保留辨識狀態", "系統已辨識為 YouTube 影片或直播" in result4)

    # _prefetch_urls: reddit fetch fails
    text_with_reddit = "看這個 https://reddit.com/r/test/comments/abc/title"
    with patch("main._fetch_reddit_meta", return_value=None):
        result5 = main._prefetch_urls(text_with_reddit)
    check("reddit 失敗 → 回傳原文", text_with_reddit in result5)

    # _prefetch_urls: general page too short
    with patch("main._requests") as mock_req5:
        mock_resp5 = MagicMock()
        mock_resp5.status_code = 200
        mock_resp5.raise_for_status = MagicMock()
        mock_resp5.text = "<html><body>short</body></html>"
        mock_req5.get.return_value = mock_resp5
        result6 = main._prefetch_urls("看這個 https://example.com/page")
    check("短內容頁面 → 回傳原文", "https://example.com/page" in result6)


# ═══════════════════════════════════════════════════════════════════════════════
# Test T: _process_pending_on_startup partial
# ═══════════════════════════════════════════════════════════════════════════════
def test_process_pending_startup_partial():
    print("\n── Test T: _process_pending_on_startup ──")

    # Both Gemini and Grok exhausted → return early (no pending processing)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"GRP001": [{"type": "text", "text": "pending"}]}, f)
        tmp = f.name
    orig_path = main._PENDING_EXPLICIT_PATH
    main._PENDING_EXPLICIT_PATH = tmp
    orig_muted = main.settings.bot_muted
    main.settings.bot_muted = False

    main._quota_exhausted_until_ts = time.time() + 3600
    with patch("main._gemini_group_messages") as mock_group:
        main._process_pending_on_startup()
    check("Gemini quota 爆 → 不分組", not mock_group.called)

    main._quota_exhausted_until_ts = 0.0
    main.settings.bot_muted = orig_muted
    main._PENDING_EXPLICIT_PATH = orig_path
    os.unlink(tmp)

    # Empty pending → return early
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f2:
        json.dump({}, f2)
        tmp2 = f2.name
    main._PENDING_EXPLICIT_PATH = tmp2
    orig_muted2 = main.settings.bot_muted
    main.settings.bot_muted = False
    with patch("main._gemini_group_messages") as mock_group2:
        main._process_pending_on_startup()
    check("空 pending → 不分組", not mock_group2.called)

    main.settings.bot_muted = orig_muted2
    main._PENDING_EXPLICIT_PATH = orig_path
    os.unlink(tmp2)


# ═══════════════════════════════════════════════════════════════════════════════
# Test U: targeted micro-coverage tests
# ═══════════════════════════════════════════════════════════════════════════════
def test_micro_coverage():
    print("\n── Test U: 針對性微覆蓋測試 ──")

    # _friendly_gemini_error with "400" → line 1834
    e400 = Exception("400 Bad Request invalid input")
    result = main._friendly_gemini_error(e400)
    check("_friendly_gemini_error 400 → 含問題描述", "輸入有問題" in result)

    # _friendly_gemini_error 500/503 → line 1835-1838
    e500 = Exception("500 internal server error")
    result2 = main._friendly_gemini_error(e500)
    check("_friendly_gemini_error 500 → 暫時斷線", "暫時斷線" in result2)

    # _friendly_gemini_error 401 → line 1831-1832
    e401 = Exception("401 Unauthorized")
    result3 = main._friendly_gemini_error(e401)
    check("_friendly_gemini_error 401 → API key 問題", "key" in result3)

    # _friendly_gemini_error 429 minute-level (not PerDay) → return ""
    e429min = Exception("429 RESOURCE_EXHAUSTED per_minute_limit")
    result4 = main._friendly_gemini_error(e429min)
    check("_friendly_gemini_error 429 分鐘限制 → 空字串", result4 == "")

    # _get_member_display_name API success → line 1127
    with (
        patch("main.ApiClient") as mock_api_cls,
        patch("main.MessagingApi") as mock_msg_api,
    ):
        mock_profile = MagicMock()
        mock_profile.display_name = "測試用戶"
        mock_msg_api.return_value.get_group_member_profile.return_value = mock_profile
        mock_api_cls.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_api_cls.return_value.__exit__ = MagicMock(return_value=False)
        name = main._get_member_display_name("GRP001", "USR001")
    check("_get_member_display_name API 成功 → display_name", name == "測試用戶")

    # _load_pending_explicit with non-existent file → {} (lines 1427-1428)
    orig_path = main._PENDING_EXPLICIT_PATH
    main._PENDING_EXPLICIT_PATH = "/nonexistent_dir/pending_test_123.json"
    result5 = main._load_pending_explicit()
    main._PENDING_EXPLICIT_PATH = orig_path
    check("_load_pending_explicit 不存在 → {}", result5 == {})

    # _save_pending_explicit_raw to bad path → silent (lines 1435-1436)
    orig_path2 = main._PENDING_EXPLICIT_PATH
    main._PENDING_EXPLICIT_PATH = "/nonexistent_dir/cant_write_here.json"
    try:
        main._save_pending_explicit_raw({"GRP001": []})
        check("_save_pending_explicit_raw 失敗 → 不爆", True)
    except Exception:
        check("_save_pending_explicit_raw 失敗 → 不爆", False)
    finally:
        main._PENDING_EXPLICIT_PATH = orig_path2

    # _load_quota_state with malformed JSON → silently catches (lines 1344-1345)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("NOT VALID JSON")
        tmp_quota = f.name
    orig_quota_path = main._QUOTA_STATE_FILE
    main._QUOTA_STATE_FILE = tmp_quota
    try:
        main._load_quota_state()  # should not raise
        check("_load_quota_state 損壞 JSON → 不爆", True)
    except Exception:
        check("_load_quota_state 損壞 JSON → 不爆", False)
    finally:
        main._QUOTA_STATE_FILE = orig_quota_path
        os.unlink(tmp_quota)

    # _save_quota_state with bad path → silent (lines 1355-1356)
    orig_quota_path2 = main._QUOTA_STATE_FILE
    main._QUOTA_STATE_FILE = "/nonexistent_dir/quota_state_test.json"
    try:
        main._save_quota_state()
        check("_save_quota_state 失敗 → 不爆", True)
    except Exception:
        check("_save_quota_state 失敗 → 不爆", False)
    finally:
        main._QUOTA_STATE_FILE = orig_quota_path2


def test_callback_loop():
    print("\n── Test V: /callback 事件迴圈 ──")
    from fastapi.testclient import TestClient

    client = TestClient(main.app)

    # Patch _parser.parse to return a mock event with _handle_event call
    mock_evt = MagicMock()
    mock_evt.source = MagicMock()
    mock_evt.source.group_id = "GRP001"
    # Make model_dump_json work
    mock_evt.model_dump_json.return_value = '{"type":"test"}'

    with (
        patch("main._parser.parse", return_value=[mock_evt]),
        patch("main._handle_event") as mock_handle,
    ):
        resp = client.post(
            "/callback",
            content=b'{"events":[]}',
            headers={"x-line-signature": "dummy_sig"},
        )
    check("/callback 有事件 → _handle_event called", mock_handle.called)
    check("/callback 有事件 → 回 200", resp.status_code == 200)

    # _handle_event throws → caught, still 200
    with (
        patch("main._parser.parse", return_value=[mock_evt]),
        patch("main._handle_event", side_effect=Exception("handler fail")),
    ):
        resp2 = client.post(
            "/callback",
            content=b'{"events":[]}',
            headers={"x-line-signature": "dummy_sig"},
        )
    check("/callback handler 爆 → 仍回 200", resp2.status_code == 200)


def test_save_pending_quoted_and_media():
    print("\n── Test W: _save_pending_any quoted/file/audio ──")
    import pending_store

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({}, f)
        tmp = f.name
    orig_path = main._PENDING_EXPLICIT_PATH
    orig_pending_enabled = main._PENDING_REPLY_ENABLED
    orig_store_path = pending_store.PENDING_PATH
    orig_store_lock = pending_store.LOCK_PATH
    pending_path = Path(tmp)
    pending_lock_path = Path(tmp + ".lock")
    main._PENDING_EXPLICIT_PATH = tmp
    main._PENDING_REPLY_ENABLED = True
    pending_store.PENDING_PATH = pending_path
    pending_store.LOCK_PATH = pending_lock_path

    try:
        # TextMessageContent with quoted_message_id → lookup raw + store quoted_original
        msg = _make_text_msg("回覆引用")
        msg.quoted_message_id = "QUOTED_ID"
        evt = _make_message_event(msg)
        with patch("main.memory.get_raw_message", return_value=("uid", "原始文字")):
            main._save_pending_any(evt, "GRP001", "USR001", msg)
        data = json.load(open(tmp))
        check(
            "pending 有 quoted_original",
            data.get("GRP001", [{}])[0].get("quoted_original") == "原始文字",
        )

        # FileMessageContent → download fail path
        with open(tmp, "w") as f:
            json.dump({}, f)
        file_msg = FileMessageContent(
            id="FILE001", fileName="test.txt", fileSize=123
        )
        file_evt = _make_message_event(file_msg)
        with patch("main._download_content", side_effect=Exception("dl fail")):
            main._save_pending_any(file_evt, "GRP001", "USR001", file_msg)
        data2 = json.load(open(tmp))
        check(
            "file pending download 失敗 → download_failed=True",
            data2.get("GRP001", [{}])[0].get("download_failed") is True,
        )

        # AudioMessageContent → download fail → download_failed=True
        with open(tmp, "w") as f:
            json.dump({}, f)
        aud_msg = AudioMessageContent(
            id="AUD001", contentProvider={"type": "line"}, duration=1000
        )
        aud_evt = _make_message_event(aud_msg)
        with patch("main._download_content", side_effect=Exception("dl fail")):
            main._save_pending_any(aud_evt, "GRP001", "USR001", aud_msg)
        data3 = json.load(open(tmp))
        check(
            "audio pending download 失敗 → download_failed=True",
            data3.get("GRP001", [{}])[0].get("download_failed") is True,
        )
    finally:
        main._PENDING_EXPLICIT_PATH = orig_path
        main._PENDING_REPLY_ENABLED = orig_pending_enabled
        pending_store.PENDING_PATH = orig_store_path
        pending_store.LOCK_PATH = orig_store_lock
        os.unlink(tmp)
        try:
            os.unlink(pending_lock_path)
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Test Z: Gemini Video Understanding fallback
# ═══════════════════════════════════════════════════════════════════════════════
def test_gemini_video_fallback():
    print("\n── Test Z: Gemini Video Understanding fallback ──")

    # ── Z1: _gemini_video_quota_ok 邏輯 ──
    with patch(
        "main.gemini_client.get_gemini_quota_info",
        return_value={
            "used_requests": 0,
            "limit_requests": 20,
            "used_tokens": 0,
            "limit_tokens": 1_000_000,
        },
    ):
        check("quota ok 充足時回 True", main._gemini_video_quota_ok() is True)

    with patch(
        "main.gemini_client.get_gemini_quota_info",
        return_value={
            "used_requests": 17,
            "limit_requests": 20,
            "used_tokens": 0,
            "limit_tokens": 1_000_000,
        },
    ):
        # 剩 3，<5 不允許
        check("quota 剩 3 → 不允許", main._gemini_video_quota_ok() is False)

    with patch("main.gemini_client.get_gemini_quota_info", return_value=None):
        check("quota info None → 不允許", main._gemini_video_quota_ok() is False)

    # ── Z2: _maybe_video_fallback：YouTube 不觸發 ──
    with patch("main._fetch_video_gemini") as mock_fetch:
        result = main._maybe_video_fallback(
            "https://www.youtube.com/watch?v=abc", None
        )
        check("YouTube 不觸發 fallback", not mock_fetch.called)
        check("YouTube 回傳保持原 None", result is None)

    # ── Z3: _maybe_video_fallback：內容夠厚不觸發 ──
    with patch("main._fetch_video_gemini") as mock_fetch:
        ample = "x" * 500
        result = main._maybe_video_fallback("https://vt.tiktok.com/abc/", ample)
        check("ytdlp 內容已 >=300 字 → 不 fallback", not mock_fetch.called)
        check("ytdlp 內容已豐 → 直接回傳原 block", result == ample)

    # ── Z4: _maybe_video_fallback：薄內容會嘗試 server-side fallback ──
    with patch("main._fetch_video_gemini", return_value=None) as mock_fetch:
        thin = "x" * 50
        result = main._maybe_video_fallback(
            "https://vt.tiktok.com/abc/", thin
        )
        check("薄影片內容 → 嘗試 fallback", mock_fetch.called)
        check("fallback miss → 回傳原本薄 block", result == thin)

    # ── Z5: _maybe_video_fallback：薄內容 + TikTok + quota OK → 觸發 ──
    with patch("main._gemini_video_quota_ok", return_value=True):
        with patch(
            "main._fetch_video_gemini", return_value="GEMINI_VIDEO_RESULT"
        ) as mock_fetch:
            thin = "x" * 50
            result = main._maybe_video_fallback(
                "https://vt.tiktok.com/abc/", thin
            )
            check("TikTok 薄內容 → 觸發 fallback", mock_fetch.called)
            check(
                "fallback 結果與原薄 block 拼接",
                result is not None and "GEMINI_VIDEO_RESULT" in result,
            )

    # ── Z6: _maybe_video_fallback：原 block None + 觸發 → 回傳 fallback ──
    with patch("main._gemini_video_quota_ok", return_value=True):
        with patch(
            "main._fetch_video_gemini", return_value="GEMINI_ONLY"
        ):
            result = main._maybe_video_fallback(
                "https://vt.tiktok.com/abc/", None
            )
            check("原 None + 有 fallback → 回 fallback", result == "GEMINI_ONLY")

    # ── Z7: 非影片域名（一般網頁）不觸發 ──
    with patch("main._fetch_video_gemini") as mock_fetch:
        result = main._maybe_video_fallback(
            "https://news.yahoo.com/article", None
        )
        check("一般網頁不觸發 video fallback", not mock_fetch.called)

    # ── Z8: _fetch_video_gemini：yt-dlp 不可用 → 回 None ──
    orig_avail = main._YTDLP_AVAILABLE
    try:
        main._YTDLP_AVAILABLE = False
        check(
            "yt-dlp 不可用 → 回 None",
            main._fetch_video_gemini("https://vt.tiktok.com/abc/") is None,
        )
    finally:
        main._YTDLP_AVAILABLE = orig_avail

    # ── Z9: _fetch_video_gemini：完整流程 mock ──
    fake_file = MagicMock()
    fake_file.name = "files/abc123"
    fake_file.state = MagicMock()
    fake_file.state.name = "ACTIVE"

    fake_response = MagicMock()
    fake_response.text = "這是一支搞笑短片，主角學貓叫。"

    fake_client = MagicMock()
    fake_client.files.upload.return_value = fake_file
    fake_client.files.get.return_value = fake_file
    fake_client.files.delete.return_value = None
    fake_client.models.generate_content.return_value = fake_response

    def fake_ydl_download(urls):
        # 模擬 yt-dlp 下載成功：寫一個假 mp4 檔
        import hashlib

        for u in urls:
            h = hashlib.md5(u.encode()).hexdigest()[:12]
            with open(f"/tmp/linebot_video_{h}.mp4", "wb") as fp:
                fp.write(b"FAKEMP4DATA")

    fake_ydl = MagicMock()
    fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
    fake_ydl.__exit__ = MagicMock(return_value=False)
    fake_ydl.download = MagicMock(side_effect=fake_ydl_download)

    with patch("main._yt_dlp") as mock_ytdlp:
        mock_ytdlp.YoutubeDL.return_value = fake_ydl
        with patch("main.gemini_client._client", fake_client):
            with patch("main.gemini_client._track_usage"):
                block = main._fetch_video_gemini("https://vt.tiktok.com/test/")

    check("完整流程回傳非 None", block is not None)
    check(
        "block 含 Gemini 分析標記",
        block is not None and "影片分析開始" in block,
    )
    check("block 含分析文字", block is not None and "搞笑短片" in block)
    check("upload 被呼叫", fake_client.files.upload.called)
    check("delete 被呼叫（清理）", fake_client.files.delete.called)

    # ── Z10: 完整流程 — generate_content 拋例外 → 回 None 但仍清理 ──
    fake_client2 = MagicMock()
    fake_client2.files.upload.return_value = fake_file
    fake_client2.files.get.return_value = fake_file
    fake_client2.files.delete.return_value = None
    fake_client2.models.generate_content.side_effect = Exception("api boom")

    with patch("main._yt_dlp") as mock_ytdlp2:
        mock_ytdlp2.YoutubeDL.return_value = fake_ydl
        with patch("main.gemini_client._client", fake_client2):
            block2 = main._fetch_video_gemini("https://vt.tiktok.com/test2/")

    check("generate 例外 → 回 None", block2 is None)
    check("即使例外仍呼叫 delete 清理", fake_client2.files.delete.called)


# ═══════════════════════════════════════════════════════════════════════════════


class AllTests(unittest.TestCase):
    def test_gemini_video_fallback(self):
        test_gemini_video_fallback()

    def test_handle_audio_message(self):
        test_handle_audio_message()

    def test_handle_explicit_text_exceptions(self):
        test_handle_explicit_text_exceptions()

    def test_handle_burst_flush_paths(self):
        test_handle_burst_flush_paths()

    def test_handle_file_message(self):
        test_handle_file_message()

    def test_extract_office_text(self):
        test_extract_office_text()

    def test_handle_dinner_recommendation(self):
        test_handle_dinner_recommendation()

    def test_handle_command_extended(self):
        test_handle_command_extended()

    def test_handle_layer2_correction(self):
        test_handle_layer2_correction()

    def test_guess_last_trigger_text(self):
        test_guess_last_trigger_text()

    def test_pop_pending_for_piggyback(self):
        test_pop_pending_for_piggyback()

    def test_gemini_group_messages(self):
        test_gemini_group_messages()

    def test_build_group_parts(self):
        test_build_group_parts()

    def test_small_helpers(self):
        test_small_helpers()

    def test_build_quoted_block_recent(self):
        test_build_quoted_block_recent()

    def test_reply_non_muted(self):
        test_reply_non_muted()

    def test_get_quota_footer(self):
        test_get_quota_footer()

    def test_next_gemini_reset_tw(self):
        test_next_gemini_reset_tw()

    def test_prefetch_error_branches(self):
        test_prefetch_error_branches()

    def test_process_pending_startup_partial(self):
        test_process_pending_startup_partial()

    def test_micro_coverage(self):
        test_micro_coverage()

    def test_callback_loop(self):
        test_callback_loop()

    def test_save_pending_quoted_and_media(self):
        test_save_pending_quoted_and_media()

    def test_handle_group_message_routing(self):
        test_handle_group_message_routing()

    def test_feedback_collector_window(self):
        test_feedback_collector_window()


# ═══════════════════════════════════════════════════════════════════════════════
# Test X: _handle_group_message routing (lines 817-853) + redelivery no msg_id
# ═══════════════════════════════════════════════════════════════════════════════
def test_handle_group_message_routing():
    print("\n── Test X: _handle_group_message routing ──")
    orig_allowed = main.settings.allowed_group_id
    spawn_patch = patch("main._spawn_piggyback_drain", lambda *a, **k: None)
    main.settings.allowed_group_id = "GRP001"
    spawn_patch.start()
    try:
        # image → log raw only (line 839-841)
        img_msg = MagicMock(spec=ImageMessageContent)
        img_msg.id = "IMG001"
        img_evt = _make_message_event(img_msg)
        with (
            patch("main.memory.log_raw_message") as mock_log,
            patch("main._MEDIA_EXECUTOR.submit") as mock_submit,
        ):
            main._handle_event(img_evt)
        check("img → log_raw_message called", mock_log.called)
        check("img → media worker submitted", mock_submit.called)

        # video → log raw only (line 842-844)
        vid_msg = MagicMock(spec=VideoMessageContent)
        vid_msg.id = "VID001"
        vid_evt = _make_message_event(vid_msg)
        with (
            patch("main.memory.log_raw_message") as mock_log2,
            patch("main._MEDIA_EXECUTOR.submit") as mock_submit2,
        ):
            main._handle_event(vid_evt)
        check("video → log_raw_message called", mock_log2.called)
        check("video → media worker submitted", mock_submit2.called)

        # audio → _handle_audio_message (lines 845-848)
        aud_msg = MagicMock(spec=AudioMessageContent)
        aud_msg.id = "AUD001"
        aud_evt = _make_message_event(aud_msg)
        with (
            patch("main.memory.log_raw_message"),
            patch("main._handle_audio_message") as mock_audio,
        ):
            main._handle_event(aud_evt)
        check("audio → _handle_audio_message called", mock_audio.called)

        # file → _handle_file_message (lines 851-853)
        file_msg = MagicMock(spec=FileMessageContent)
        file_msg.id = "FILE001"
        file_evt = _make_message_event(file_msg)
        with patch("main._handle_file_message") as mock_file:
            main._handle_event(file_evt)
        check("file → _handle_file_message called", mock_file.called)

        # text → _handle_text_message (lines 831-836)
        txt_msg = _make_text_msg("hello routing")
        txt_evt = _make_message_event(txt_msg)
        with (
            patch("main.memory.log_raw_message"),
            patch("main._handle_text_message") as mock_text,
        ):
            main._handle_event(txt_evt)
        check("text → _handle_text_message called", mock_text.called)

        # quota exhausted + text still routes to text handler; only File/Audio
        # have a quota-exhausted early-drop branch.
        main._quota_exhausted_until_ts = time.time() + 3600
        txt_msg2 = _make_text_msg("quota burst msg")
        txt_evt2 = _make_message_event(txt_msg2)
        with (
            patch("main.memory.log_raw_message"),
            patch("main._handle_text_message") as mock_text2,
        ):
            main._handle_event(txt_evt2)
        check("quota exhausted text → _handle_text_message", mock_text2.called)
        main._quota_exhausted_until_ts = 0.0

        # redelivery no msg_id → lines 807-808
        null_msg = MagicMock(spec=TextMessageContent)
        null_msg.id = None
        null_msg.text = "redelivery without id"
        null_msg.type = "text"
        redel_evt = MagicMock(spec=MessageEvent)
        redel_evt.message = null_msg
        redel_evt.source = _make_group_source()
        redel_evt.reply_token = "TOKEN_REDEL"
        redel_evt.timestamp = int(time.time() * 1000)
        dctx = MagicMock()
        dctx.is_redelivery = True
        redel_evt.delivery_context = dctx
        with patch("main.memory.log_raw_message") as mock_log3:
            main._handle_event(redel_evt)
        check("redelivery no msg_id → early return (no log)", not mock_log3.called)

    finally:
        spawn_patch.stop()
        main.settings.allowed_group_id = orig_allowed


# ═══════════════════════════════════════════════════════════════════════════════
# Test Y: feedback_collector window coverage (lines 896-900)
# ═══════════════════════════════════════════════════════════════════════════════
def test_feedback_collector_window():
    print("\n── Test Y: feedback_collector window ──")
    orig_allowed = main.settings.allowed_group_id
    main.settings.allowed_group_id = "GRP001"
    fake_message_classifier = types.ModuleType("message_classifier")
    fake_message_classifier.classify_async = lambda *a, **k: None
    fake_knowledge_graph = types.ModuleType("knowledge_graph")
    fake_knowledge_graph.auto_extract_kg_async = lambda *a, **k: None
    fake_food_signals = types.ModuleType("food_signals")
    fake_food_signals.extract_and_store_async = lambda *a, **k: None
    try:
        txt_msg = _make_text_msg("feedback window msg")
        txt_evt = _make_message_event(txt_msg)
        # Call _handle_text_message directly so feedback code actually runs
        with (
            patch.dict(
                sys.modules,
                {
                    "message_classifier": fake_message_classifier,
                    "knowledge_graph": fake_knowledge_graph,
                    "food_signals": fake_food_signals,
                },
            ),
            patch("main.feedback_collector.in_feedback_window", return_value=True),
            patch("main.feedback_collector.collect_message") as mock_collect,
            patch("main._try_handle_calendar_correction", return_value=False),
            patch("main._detect_user_correction"),
            patch("main._auto_capture_text_if_important"),
            patch("main._maybe_extract_reminder"),
            patch("main._try_piggyback_reminders_fast_path", return_value=False),
            patch("main._handle_command", return_value=None),
            patch("main._is_dinner_question", return_value=False),
            patch("main._extract_gemini_trigger", return_value=None),
            patch("main.burst_filter.add_to_burst"),
        ):
            main._handle_text_message(txt_evt, "GRP001")
        check("feedback window → collect_message called", mock_collect.called)

        # collect_message raises → logged, no crash (lines 898-900)
        with (
            patch.dict(
                sys.modules,
                {
                    "message_classifier": fake_message_classifier,
                    "knowledge_graph": fake_knowledge_graph,
                    "food_signals": fake_food_signals,
                },
            ),
            patch("main.feedback_collector.in_feedback_window", return_value=True),
            patch(
                "main.feedback_collector.collect_message",
                side_effect=RuntimeError("boom"),
            ),
            patch("main._try_handle_calendar_correction", return_value=False),
            patch("main._detect_user_correction"),
            patch("main._auto_capture_text_if_important"),
            patch("main._maybe_extract_reminder"),
            patch("main._try_piggyback_reminders_fast_path", return_value=False),
            patch("main._handle_command", return_value=None),
            patch("main._is_dinner_question", return_value=False),
            patch("main._extract_gemini_trigger", return_value=None),
            patch("main.burst_filter.add_to_burst"),
        ):
            main._handle_text_message(txt_evt, "GRP001")
        check("feedback window exception → no crash", True)
    finally:
        main.settings.allowed_group_id = orig_allowed


if __name__ == "__main__":
    tests = [
        test_gemini_video_fallback,
        test_handle_audio_message,
        test_handle_explicit_text_exceptions,
        test_handle_burst_flush_paths,
        test_handle_file_message,
        test_extract_office_text,
        test_handle_dinner_recommendation,
        test_handle_command_extended,
        test_handle_layer2_correction,
        test_guess_last_trigger_text,
        test_pop_pending_for_piggyback,
        test_gemini_group_messages,
        test_build_group_parts,
        test_small_helpers,
        test_build_quoted_block_recent,
        test_reply_non_muted,
        test_get_quota_footer,
        test_next_gemini_reset_tw,
        test_prefetch_error_branches,
        test_process_pending_startup_partial,
        test_micro_coverage,
        test_callback_loop,
        test_save_pending_quoted_and_media,
        test_handle_group_message_routing,
        test_feedback_collector_window,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {e}")
            FAIL += 1

    print(f"\n{'=' * 60}")
    print(f"PASS: {PASS}  FAIL: {FAIL}")
    if FAIL:
        sys.exit(1)
