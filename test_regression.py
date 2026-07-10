"""
Regression tests — one per historical bug found in git history.

Each test verifies that the specific bug is fixed and would catch a regression
if the fix were reverted.

Bug refs (commit SHA prefix):
  Bug 1 (30e272f): non-mention text goes to burst_filter, NOT directly to LLM
  Bug 2 (09b9996): quoted_id + empty clean_text → LLM, NOT "嗯？" greeting
  Bug 3 (ea5b877): __bot__ entries in pending are filtered before LLM processing
  Bug 4 (9d2528c): Grok intro message sent only once per group per session
  Bug 5 (e2cfb86): quota exhausted state persists to disk / restored on reload
  Bug 6 (7c6ce81): quota footer shows max(token_pct, req_pct), not either alone
  Bug 7 (e2cfb86/ee45f0d): _reply() skips empty/whitespace text
"""

import sys
import time
import types
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import main
import pending_store
from linebot.v3.webhooks import (
    GroupSource,
    MessageEvent,
    TextMessageContent,
)


# ── shared helpers ────────────────────────────────────────────────────────────


def _make_group_source(group_id: str = "GRP001", user_id: str = "USR001"):
    src = MagicMock(spec=GroupSource)
    src.group_id = group_id
    src.user_id = user_id
    src.type = "group"
    return src


def _make_text_event(
    text: str = "hello",
    group_id: str = "GRP001",
    user_id: str = "USR001",
    quoted_message_id: str | None = None,
):
    msg = MagicMock(spec=TextMessageContent)
    msg.id = "MSG001"
    msg.text = text
    msg.mention = None
    msg.quoted_message_id = quoted_message_id
    msg.quote_token = "qt"
    msg.type = "text"

    evt = MagicMock(spec=MessageEvent)
    evt.message = msg
    evt.source = _make_group_source(group_id, user_id)
    evt.reply_token = "TOKEN001"
    evt.timestamp = int(time.time() * 1000)
    dctx = MagicMock()
    dctx.is_redelivery = False
    evt.delivery_context = dctx
    return evt


@contextmanager
def _noop_cm():
    yield


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        base = cls(2026, 7, 2, 10, 30, tzinfo=ZoneInfo("Asia/Taipei"))
        return base if tz is None else base.astimezone(tz)


def test_prepare_outbound_text_blocks_stale_date_before_line_api(monkeypatch):
    monkeypatch.setattr(main.output_validator, "datetime", _FixedDateTime)

    prepared = main._prepare_outbound_text(
        "現在是2024年，2025年的完整數據尚未發布。",
        source="regression",
    )

    assert "過期的年份基準" in prepared
    assert "2024年" not in prepared


def test_prepare_outbound_text_keeps_normal_local_status(monkeypatch):
    monkeypatch.setattr(main.output_validator, "datetime", _FixedDateTime)

    prepared = main._prepare_outbound_text(
        "目前沒有查到 pending 待辦或提醒事項。",
        source="regression",
    )

    assert prepared == "目前沒有查到 pending 待辦或提醒事項。"


def test_blocked_outbound_text_is_not_stored_as_raw_bot_memory(monkeypatch):
    monkeypatch.setattr(main.output_validator, "datetime", _FixedDateTime)
    stored: list[tuple[str, str, str]] = []
    monkeypatch.setattr(main.memory, "append_turn", lambda *args: stored.append(args))

    main._append_bot_turn(
        "GRP001",
        "現在是2024年，2025年的完整數據尚未發布。",
        source="regression_memory",
    )

    assert len(stored) == 1
    assert stored[0][:2] == ("GRP001", "bot")
    assert "過期的年份基準" in stored[0][2]
    assert "2024年" not in stored[0][2]


def test_build_quoted_block_includes_media_description():
    msg = MagicMock(spec=TextMessageContent)
    msg.quoted_message_id = "IMG001"

    with (
        patch("main.memory.get_raw_message", return_value=("USR002", "[圖片]")),
        patch(
            "main.memory.get_raw_message_meta",
            return_value={
                "media_type": "image",
                "mime_type": "image/jpeg",
                "file_name": "",
                "description": "畫面是一張收據，上面總金額是 1200 元。",
            },
        ),
        patch("main._get_member_display_name", return_value="Alice"),
    ):
        block = main._build_quoted_block(msg, "GRP001")

    assert block is not None
    assert "[Alice]: [圖片]" in block
    assert "媒體資訊：image / image/jpeg" in block
    assert "已知內容摘要：畫面是一張收據" in block


def test_non_explicit_quoted_text_enters_burst_with_original_context():
    evt = _make_text_event(text="這個可以嗎", quoted_message_id="Q001")
    captured: dict[str, str] = {}

    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._try_one_shot_reply", return_value=False),
        patch("main._try_handle_calendar_correction", return_value=False),
        patch("main._detect_user_correction"),
        patch("main._auto_capture_text_if_important"),
        patch("main._maybe_extract_reminder"),
        patch("main._handle_command", return_value=None),
        patch("main._handle_explicit_poll_text", return_value=None),
        patch("main._is_todo_query", return_value=False),
        patch("main._is_dinner_question", return_value=False),
        patch("main._is_web_research_question", return_value=False),
        patch("main._try_piggyback_reminders_fast_path", return_value=False),
        patch("main._extract_gemini_trigger", return_value=None),
        patch("main.memory.get_raw_message", return_value=("USR002", "原始文字內容")),
        patch("main.memory.get_raw_message_meta", return_value=None),
        patch("main._get_member_display_name", return_value="Alice"),
        patch("main.burst_filter.add_to_burst") as mock_burst,
    ):
        main._handle_text_message(evt, "GRP001")

    args = mock_burst.call_args.args
    captured["text"] = args[2]
    assert "原始訊息 開始" in captured["text"]
    assert "原始文字內容" in captured["text"]
    assert "目前回覆 開始" in captured["text"]
    assert "這個可以嗎" in captured["text"]


def test_non_explicit_quoted_url_followup_routes_to_web_research():
    evt = _make_text_event(text="這個真的假的", quoted_message_id="Q001")

    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._try_one_shot_reply", return_value=False),
        patch("main._try_handle_calendar_correction", return_value=False),
        patch("main._detect_user_correction"),
        patch("main._auto_capture_text_if_important"),
        patch("main._maybe_extract_reminder"),
        patch("main._handle_command", return_value=None),
        patch("main._handle_explicit_poll_text", return_value=None),
        patch("main._is_todo_query", return_value=False),
        patch("main._is_dinner_question", return_value=False),
        patch("main._is_web_research_question", return_value=False),
        patch("main._extract_gemini_trigger", return_value=None),
        patch("main.memory.get_raw_message", return_value=("USR002", "原始連結 https://example.com/a")),
        patch("main.memory.get_raw_message_meta", return_value=None),
        patch("main._get_member_display_name", return_value="Alice"),
        patch("main._handle_web_research_question", return_value=True) as mock_web,
        patch("main.burst_filter.cancel_burst") as mock_cancel,
        patch("main.burst_filter.add_to_burst") as mock_burst,
    ):
        main._handle_text_message(evt, "GRP001")

    mock_web.assert_called_once()
    research_text = mock_web.call_args.args[2]
    assert "原始連結 https://example.com/a" in research_text
    assert "這個真的假的" in research_text
    mock_cancel.assert_called_once_with("GRP001")
    mock_burst.assert_not_called()


def test_non_explicit_quoted_media_followup_routes_to_media_quote():
    evt = _make_text_event(text="這張是什麼", quoted_message_id="IMG001")

    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._try_one_shot_reply", return_value=False),
        patch("main._try_handle_calendar_correction", return_value=False),
        patch("main._detect_user_correction"),
        patch("main._auto_capture_text_if_important"),
        patch("main._maybe_extract_reminder"),
        patch("main._handle_command", return_value=None),
        patch("main._handle_explicit_poll_text", return_value=None),
        patch("main._is_todo_query", return_value=False),
        patch("main._is_dinner_question", return_value=False),
        patch("main._is_web_research_question", return_value=False),
        patch("main._extract_gemini_trigger", return_value=None),
        patch("main.memory.get_raw_message", return_value=("USR002", "[圖片]")),
        patch("main.memory.get_raw_message_meta", return_value=None),
        patch("main._get_member_display_name", return_value="Alice"),
        patch("main._handle_media_via_quote") as mock_media,
        patch("main.burst_filter.add_to_burst") as mock_burst,
    ):
        main._handle_text_message(evt, "GRP001")

    mock_media.assert_called_once_with(evt, "GRP001", "這張是什麼", "IMG001", "[圖片]")
    mock_burst.assert_not_called()


def test_quoted_media_description_fallback_answers_when_bytes_missing():
    evt = _make_text_event(text="這張是什麼", quoted_message_id="IMG001")
    stored_user: list[str] = []
    fake_local = types.ModuleType("local_llm")
    fake_local.chat = lambda *a, **k: (
        "圖片內容：這張是在講收據，總金額看起來是 1200 元。\n"
        "正方：有金額摘要方便快速對帳。\n"
        "反方：只看摘要可能漏掉日期、品項或付款方式。\n"
        "統一論點：先用它當線索，再回原收據核對細節。"
    )

    with (
        patch.dict(sys.modules, {"local_llm": fake_local}),
        patch(
            "main.memory.get_raw_message_meta",
            return_value={"description": "畫面是一張收據，上面總金額是 1200 元。"},
        ),
        patch("main.memory.get_context", return_value=[]),
        patch("main._llm_chat") as mock_llm_chat,
        patch("main.memory.append_turn", side_effect=lambda _g, _r, t: stored_user.append(t)),
        patch("main._append_bot_turn") as mock_bot_turn,
        patch("main._reply") as mock_reply,
    ):
        handled = main._handle_quoted_media_description_fallback(
            evt, "GRP001", "這張是什麼", "IMG001", "圖片"
        )

    assert handled
    assert "既有摘要" in stored_user[0]
    mock_llm_chat.assert_not_called()
    mock_bot_turn.assert_called_once()
    mock_reply.assert_called_once()
    sent = mock_reply.call_args.args[1]
    assert "圖片內容：" in sent
    assert "正方：" in sent
    assert "反方：" in sent
    assert "統一論點：" in sent


def test_youtube_live_ytdlp_metadata_is_usable_without_subtitles(monkeypatch):
    info = {
        "title": "重大新聞直播",
        "uploader": "新聞頻道",
        "channel_url": "https://www.youtube.com/@news",
        "webpage_url": "https://www.youtube.com/watch?v=LIVE1234567",
        "is_live": True,
        "live_status": "is_live",
        "description": "今天直播討論最新國際情勢與市場反應。",
        "view_count": 12345,
        "duration": None,
        "subtitles": {},
        "automatic_captions": {},
    }

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            assert not download
            return info

    monkeypatch.setattr(main, "_YTDLP_AVAILABLE", True)
    monkeypatch.setattr(main, "_yt_dlp", MagicMock(YoutubeDL=FakeYDL))
    monkeypatch.setattr(main, "_extract_subtitles_from_info", lambda _info: None)

    block = main._fetch_video_ytdlp("https://www.youtube.com/watch?v=LIVE1234567")

    assert block is not None
    assert "標題：重大新聞直播" in block
    assert "頻道：新聞頻道" in block
    assert "直播狀態：正在直播" in block
    assert "今天直播討論最新國際情勢" in block
    assert "未取得可用字幕或逐字稿" in block
    assert "請自行" not in block


def test_youtube_oembed_fallback_does_not_tell_user_to_click(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "title": "直播標題",
        "author_name": "直播頻道",
    }
    monkeypatch.setattr(main._requests, "get", lambda *a, **kw: mock_resp)

    block = main._fetch_youtube_meta("https://youtube.com/watch?v=LIVE1234567")

    assert block is not None
    assert "標題：直播標題" in block
    assert "頻道：直播頻道" in block
    assert "資料限制" in block
    assert "請自行" not in block
    assert "自己點" not in block


def test_prefetch_youtube_trims_trailing_punctuation(monkeypatch):
    seen: list[str] = []

    def fake_ytdlp(url):
        seen.append(url)
        return "YOUTUBE_BLOCK"

    monkeypatch.setattr(main, "_fetch_video_ytdlp", fake_ytdlp)
    monkeypatch.setattr(main, "_fetch_youtube_meta", lambda _url: None)

    out = main._prefetch_urls("看這個 https://youtu.be/LIVE1234567。")

    assert seen == ["https://www.youtube.com/watch?v=LIVE1234567"]
    assert "YOUTUBE_BLOCK" in out


def test_youtube_live_url_is_canonicalized():
    assert (
        main._canonical_youtube_url("https://www.youtube.com/live/LIVE1234567?si=abc")
        == "https://www.youtube.com/watch?v=LIVE1234567"
    )
    assert (
        main._canonical_youtube_url("https://www.youtube.com/embed/LIVE1234567")
        == "https://www.youtube.com/watch?v=LIVE1234567"
    )


def test_youtube_html_metadata_fallback_parses_player_response(monkeypatch):
    seen: list[str] = []
    html = """
    <html><head>
      <meta property="og:url" content="https://www.youtube.com/watch?v=LIVE1234567">
    </head><body>
      <script>
        var ytInitialPlayerResponse = {"videoDetails":{"title":"現場直播標題","author":"現場頻道","shortDescription":"直播描述文字","isLiveContent":true,"lengthSeconds":"123","viewCount":"4567"}};
      </script>
    </body></html>
    """

    def fake_get(url, **kwargs):
        seen.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = html
        return resp

    monkeypatch.setattr(main._requests, "get", fake_get)

    block = main._fetch_youtube_html_meta(
        "https://www.youtube.com/live/LIVE1234567?si=abc"
    )

    assert seen == ["https://www.youtube.com/watch?v=LIVE1234567"]
    assert block is not None
    assert "標題：現場直播標題" in block
    assert "頻道：現場頻道" in block
    assert "直播狀態：直播內容或直播重播" in block
    assert "描述：直播描述文字" in block
    assert "不要要求使用者自行點擊觀看" in block


def test_prefetch_youtube_all_fetchers_fail_still_adds_context(monkeypatch):
    monkeypatch.setattr(main, "_fetch_video_ytdlp", lambda _url: None)
    monkeypatch.setattr(main, "_fetch_youtube_meta", lambda _url: None)
    monkeypatch.setattr(main, "_fetch_youtube_html_meta", lambda _url: None)

    out = main._prefetch_urls("幫我看 https://www.youtube.com/live/LIVE1234567。")

    assert "系統已辨識為 YouTube 影片或直播" in out
    assert "影片 ID：LIVE1234567" in out
    assert "禁止使用把操作交回使用者" in out
    assert "幫我看 https://www.youtube.com/live/LIVE1234567。" in out


def test_prefetch_accepts_bare_youtube_url(monkeypatch):
    seen: list[str] = []

    def fake_context(url):
        seen.append(url)
        return "YOUTUBE_BLOCK"

    monkeypatch.setattr(main, "_fetch_youtube_context", fake_context)

    out = main._prefetch_urls("幫我看 youtu.be/LIVE1234567")

    assert seen == ["https://youtu.be/LIVE1234567"]
    assert "YOUTUBE_BLOCK" in out


def test_prefetch_prioritizes_youtube_when_third_url(monkeypatch):
    seen: list[str] = []

    def fake_context(url):
        seen.append(url)
        return "YOUTUBE_BLOCK"

    class FakeResp:
        status_code = 200
        text = "<html><body>" + ("一般網頁內容 " * 20) + "</body></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(main, "_fetch_youtube_context", fake_context)
    monkeypatch.setattr(main._requests, "get", lambda *a, **kw: FakeResp())

    out = main._prefetch_urls(
        "先看 https://example.com/a 再看 https://example.org/b "
        "最後看 https://www.youtube.com/live/LIVE1234567"
    )

    assert seen == ["https://www.youtube.com/live/LIVE1234567"]
    assert "YOUTUBE_BLOCK" in out


def test_web_research_youtube_question_prefetches_before_llm(monkeypatch):
    captured: dict[str, str] = {}
    event = _make_text_event(
        "這直播在講什麼 https://www.youtube.com/live/LIVE1234567",
        group_id="GRP001",
    )

    def fake_llm(prompt, context, facts, pnotes):
        captured["prompt"] = prompt
        return "這場直播主題可從 metadata 判斷。"

    monkeypatch.setattr(main, "_fetch_youtube_context", lambda _url: "YOUTUBE_CONTEXT_BLOCK")
    monkeypatch.setattr(main, "_collect_web_research_sources", lambda _text: [])
    monkeypatch.setattr(main, "_llm_chat", fake_llm)
    monkeypatch.setattr(main, "_thinking_indicator", lambda _group_id: _noop_cm())

    with (
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main.memory.append_turn"),
        patch("main._append_bot_turn"),
        patch("main._reply"),
    ):
        handled = main._handle_web_research_question(
            event,
            "GRP001",
            "這直播在講什麼 https://www.youtube.com/live/LIVE1234567",
        )

    assert handled
    assert "YOUTUBE_CONTEXT_BLOCK" in captured["prompt"]


def test_web_research_detects_bare_youtube_url_question():
    assert main._is_web_research_question("這直播在講什麼 youtu.be/LIVE1234567")


# ── Bug 1 ─────────────────────────────────────────────────────────────────────


def test_bug1_non_mention_routes_to_burst_filter():
    """Bug 1 (30e272f): plain text (no @mention / /ai) must go through burst_filter,
    never call _llm_chat directly.  Regression: LLM was called immediately.
    """
    main.settings.allowed_group_id = "GRP001"
    main.settings.bot_muted = False
    evt = _make_text_event(text="普通文字，沒有觸發詞")

    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._handle_command", return_value=None),
        patch("main._is_dinner_question", return_value=False),
        patch("main._extract_gemini_trigger", return_value=None),
        patch("main.burst_filter.add_to_burst") as mock_burst,
        patch("main._llm_chat") as mock_llm,
    ):
        main._handle_text_message(evt, "GRP001")

    assert mock_burst.called, "Non-mention text must be routed to burst_filter"
    assert not mock_llm.called, "LLM must NOT be called directly for non-mention text"


def test_safe_event_debug_dump_redacts_text_and_user_id():
    evt = _make_text_event(
        text="這是一段不應該被 debug dump 印出的私人訊息",
        user_id="USR_SECRET",
    )

    dumped = main._safe_event_debug_dump(evt)

    assert "私人訊息" not in dumped
    assert "USR_SECRET" not in dumped
    assert "MSG001" in dumped
    assert "text_sha256" in dumped
    assert "user_id_sha256" in dumped


# ── Bug 2 ─────────────────────────────────────────────────────────────────────


def test_bug2_quote_with_empty_clean_text_calls_llm_not_greeting():
    """Bug 2 (09b9996): quoted_id + empty clean_text → LLM, not '嗯？'.
    Regression: the old gate `if not clean_text:` ignored quoted_id,
    causing '嗯？' even when the user was referencing a prior message.
    Fix: gate is `if not clean_text and not quoted_id:`.
    """
    main.settings.allowed_group_id = "GRP001"
    evt = _make_text_event(text="", quoted_message_id="QUOTED_MSG_001")

    reply_texts: list[str] = []

    with (
        patch(
            "main.memory.get_raw_message",
            return_value=("USR001", "這是原始訊息"),
        ),
        patch("main._get_member_display_name", return_value="User"),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda x: x),
        patch("main._llm_chat", return_value="LLM 回覆") as mock_llm,
        patch("main.memory.append_turn"),
        patch("main._maybe_extract_facts"),
        patch("main._try_save_correction"),
        patch(
            "main._reply",
            side_effect=lambda token, text, **kw: reply_texts.append(text),
        ),
    ):
        main._handle_explicit_text(evt, "GRP001", "")

    assert mock_llm.called, "LLM must be called when quoted_id present, even with empty clean_text"
    assert not any(
        "嗯？" in t for t in reply_texts
    ), f"Must NOT reply '嗯？' when quoted_id is present; got: {reply_texts}"


# ── Bug 3 ─────────────────────────────────────────────────────────────────────


def test_bug3_bot_entries_filtered_from_pending():
    """Bug 3 (ea5b877): __bot__ entries in pending must be stripped before
    processing.  If all items are __bot__, they are removed with no LLM call.
    Regression: __bot__ entries were processed, causing duplicate pushes.
    """
    main.settings.bot_muted = False
    main._quota_exhausted_until_ts = 0.0

    pending_store.save_full({
        "GRP001": [
            {
                "message_id": "bot1",
                "user_id": "__bot__",
                "text": "bot 自己的舊回覆 1",
                "timestamp": 100,
                "type": "text",
            },
            {
                "message_id": "bot2",
                "user_id": "__bot__",
                "text": "bot 自己的舊回覆 2",
                "timestamp": 101,
                "type": "text",
            },
        ]
    })

    llm_calls: list = []

    with (
        patch("main._dlq_entry"),
        patch(
            "main._llm_chat",
            side_effect=lambda *a, **kw: llm_calls.append(True) or "reply",
        ),
        # global drain gate（mute / Gemini quota / LINE 月額度）必須通才會進到
        # __bot__ 過濾邏輯。test environment 沒真 LINE token，預設 fail-closed → False
        patch("main._global_pending_drain_ready", return_value=True),
        patch("main._PENDING_REPLY_ENABLED", True),
    ):
        main._process_pending_on_startup()

    assert len(llm_calls) == 0, "LLM must NOT be called when all pending items are __bot__"
    assert pending_store.load().get("GRP001", []) == []


# ── Bug 4: Grok fallback intro test 已移除（2026-04-26 grok 改為 stub）─────────


# ── Bug 5 ─────────────────────────────────────────────────────────────────────


def test_bug5_quota_state_persists_across_restart():
    """Bug 5 (e2cfb86): _mark_quota_exhausted writes state to disk;
    _load_quota_state restores it after a simulated restart (in-memory wipe).
    Regression: state was in-memory only, so restart reset the quota guard.
    conftest redirects _QUOTA_STATE_FILE to a temp file for isolation.
    """
    with (
        patch("main.gemini_client.mark_quota_exhausted_in_usage"),
        patch("main.ApiClient"),  # prevent push attempt
    ):
        main._mark_quota_exhausted()

    saved_ts = main._quota_exhausted_until_ts
    assert saved_ts > time.time(), "_mark_quota_exhausted must set a future timestamp"

    # Simulate restart: wipe in-memory state
    main._quota_exhausted_until_ts = 0.0
    assert not main._quota_exhausted(), "Quota should NOT be exhausted after in-memory wipe"

    # Reload from disk (temp file written above)
    main._load_quota_state()

    assert main._quota_exhausted_until_ts == pytest.approx(
        saved_ts, abs=1
    ), "Disk-restored ts must match the saved value"
    assert main._quota_exhausted(), "Quota must be exhausted again after reload from disk"


# ── Bug 6 ─────────────────────────────────────────────────────────────────────


def test_bug6_quota_footer_no_percentage_unless_exhausted():
    """Bug 6 update (2026-05-05): 用戶要求拿掉 80%+ 百分比 footer，
    避免破壞對話自然度。2026-05-31 再移除 quota exhausted footer；
    其他情境（即使 99% 或已爆）一律不顯示 footer。
    """
    main._quota_exhausted_until_ts = 0.0  # Gemini NOT exhausted

    # 即使 80%+ 也不該顯示百分比
    mock_info = {
        "used_tokens": 80,
        "limit_tokens": 100,
        "used_requests": 9,
        "limit_requests": 10,
        "used_thinking_tokens": 0,
    }
    with patch("main.gemini_client.get_gemini_quota_info", return_value=mock_info):
        footer = main._get_quota_footer()

    assert footer == "", f"Footer 應為空字串（不該有百分比），got: {footer!r}"

    main._quota_exhausted_until_ts = time.time() + 3600
    footer = main._get_quota_footer()
    assert footer == "", f"Quota exhausted 時也不該附加 footer，got: {footer!r}"


def test_bug8_burst_empty_quota_replies_without_pending():
    """Andrew 2026-07-04: pending reply disabled. Quota exhausted + burst empty
    should send a visible degraded reply but should not enqueue pending reply.
    """
    main._quota_exhausted_until_ts = time.time() + 3600

    with (
        patch("main._llm_chat", return_value=""),
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=""),
        patch("main._prefetch_urls", return_value="家人閒聊 message"),
        patch("main._reply") as mock_reply,
        patch("main._maybe_capture_calendar_event"),
        patch("main._thinking_indicator", return_value=_noop_cm()),
    ):
        main._handle_burst_flush("GRP001", "家人閒聊 message", "TOKEN001")

    mock_reply.assert_called_once_with(
        "TOKEN001",
        main._visible_llm_degraded_reply(),
        group_id="GRP001",
        allow_push_fallback=True,
    )
    pending = pending_store.load().get("GRP001", [])
    assert pending == []


def test_explicit_empty_quota_replies_visible_without_pending(monkeypatch):
    """Explicit bot call should not go silent when quota and local fallback both miss."""
    evt = _make_text_event("咪寶 幫我分析")
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)

    with (
        patch("main._detect_image_gen_request", return_value=""),
        patch("main._handle_explicit_poll_text", return_value=None),
        patch("main._is_todo_query", return_value=False),
        patch("main._is_calendar_query", return_value=False),
        patch("main._get_explicit_market_quote_reply", return_value=""),
        patch("main.memory.get_context", return_value=[]),
        patch("main._build_quoted_block", return_value=None),
        patch("main._prefetch_urls", return_value="幫我分析"),
        patch("main._is_market_quote_request", return_value=False),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=""),
        patch("main._llm_chat", return_value=""),
        patch("main._pending_reply_enabled", return_value=False),
        patch("main._reply") as mock_reply,
        patch("main._maybe_capture_calendar_event"),
        patch("main._thinking_indicator", return_value=_noop_cm()),
    ):
        main._handle_explicit_text(evt, "GRP001", "幫我分析")

    mock_reply.assert_called_once_with(
        "TOKEN001",
        main._visible_llm_degraded_reply(),
        group_id="GRP001",
        allow_push_fallback=True,
    )


def test_bug9_reply_suppresses_system_status_messages():
    """Andrew 2026-05-31: LINE group must not receive internal quota/status text."""
    main.settings.bot_muted = False

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    blocked_text = "咪寶聽到了但這個話題不太接得上~\n\n📊 Gemini 今日用量已用完"

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", blocked_text, group_id="GRP001")
        main._reply("TOKEN002", main._quota_exhausted_message(), group_id="GRP001")

    assert not mock_messaging.reply_message.called
    assert not mock_messaging.push_message.called
    assert not main._is_system_status_outbound(main._visible_llm_degraded_reply())


def test_bug10_reply_suppresses_llm_internal_trace():
    """LLM reasoning/checklist text must never be sent to the LINE group."""
    main.settings.bot_muted = False

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    leaked_text = (
        "THOUGHT\n"
        "The user is asking \"你們是去紐西蘭\".\n"
        "My response should:\n"
        "1. Directly answer the question.\n"
        "Let's ensure it adheres to the rules:\n"
        "• Rule 0: 具體判斷句 - OK.\n"
        "咪寶沒有要去紐西蘭喔。"
    )

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", leaked_text, group_id="GRP001")

    assert not mock_messaging.reply_message.called
    assert not mock_messaging.push_message.called


def test_reply_suppresses_chinese_internal_trace():
    """Chinese scratchpad text must never be sent to the LINE group."""
    main.settings.bot_muted = False

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    leaked_text = (
        "[[思考]]\n"
        "使用者貼了一個 Threads 連結，並沒有明確提問。\n"
        "1. 判斷問題類型：可能是摘要。\n"
        "2. 回覆結構：第一句具體判斷。\n"
        "正式回覆：這則貼文需要先讀內容。"
    )

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", leaked_text, group_id="GRP001")

    assert not mock_messaging.reply_message.called
    assert not mock_messaging.push_message.called


def test_reply_strips_user_visible_mode_labels(monkeypatch):
    """Internal fallback/model labels must not be sent to the LINE group."""
    monkeypatch.setattr(main.settings, "bot_muted", False)

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    text = (
        "這是正式回覆。\n\n"
        "（lite mode — local LLM）\n\n"
        "🔍 lite mode（Google 首頁 snippet）：\n"
        "搜尋摘要\n\n"
        "（local LLM fallback）"
    )

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", text, group_id="GRP001")

    request = mock_messaging.reply_message.call_args.args[0]
    sent = request.messages[0].text
    assert "這是正式回覆" in sent
    assert "Google 搜尋片段" in sent
    assert "搜尋摘要" in sent
    assert "lite mode" not in sent
    assert "local LLM" not in sent


def test_reply_uses_text_v2_for_known_plain_mentions(monkeypatch):
    """Plain @ labels in ordinary replies should become real LINE mentions."""
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)

    import line_mentions

    monkeypatch.setattr(line_mentions, "load_user_aliases", lambda: {"U_DAD": "爸爸"})
    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", "@爸爸 記得看這個", group_id="GRP001")

    request = mock_messaging.reply_message.call_args.args[0]
    message = request.messages[0]
    assert message.type == "textV2"
    assert message.text.startswith("{p1}\n")
    assert message.substitution["p1"].mentionee.user_id == "U_DAD"


def test_reply_uses_incoming_mention_payload_targets():
    """Reply payload should notify mentioned user IDs, even when reply text has no @alias."""
    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    msg = MagicMock(spec=TextMessageContent)
    msg.id = "MSG001"
    msg.text = "幫我確認這件事"
    msg.mention = MagicMock()
    mentionee = MagicMock()
    mentionee.user_id = "U_DAD"
    mentionee.index = 0
    mentionee.length = 2
    mentionee.type = "user"
    msg.mention.mentionees = [mentionee]

    main._register_reply_mention_targets("TOKEN001", msg)

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
        patch.object(main.settings, "bot_muted", False),
        patch.object(main, "_reminder_reply_piggyback_enabled", lambda: False),
    ):
        main._reply("TOKEN001", "幫我確認這件事", group_id="GRP001")

    request = mock_messaging.reply_message.call_args.args[0]
    message = request.messages[0]
    assert message.type == "textV2"
    assert message.text.startswith("{p1}\n")
    assert message.substitution["p1"].mentionee.user_id == "U_DAD"
    assert main._consume_reply_mention_targets("TOKEN001") == []


def test_reply_fallback_push_uses_text_v2_for_known_plain_mentions(monkeypatch):
    """Expired reply token fallback must not downgrade real mentions to plain text."""
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)

    import line_mentions

    monkeypatch.setattr(line_mentions, "load_user_aliases", lambda: {"U_DAD": "爸爸"})
    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()
    mock_messaging.reply_message.side_effect = Exception("Invalid reply token")

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", "@爸爸 記得看這個", group_id="GRP001")

    request = mock_messaging.push_message.call_args.args[0]
    message = request.messages[0]
    assert message.type == "textV2"
    assert message.text.startswith("{p1}\n")
    assert message.substitution["p1"].mentionee.user_id == "U_DAD"


def test_reply_does_not_mention_bare_alias_without_at(monkeypatch):
    """Ordinary replies should not notify people unless the label is explicit."""
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)

    import line_mentions

    monkeypatch.setattr(line_mentions, "load_user_aliases", lambda: {"U_DAD": "爸爸"})
    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", "爸爸剛剛說的那件事，我晚點整理。", group_id="GRP001")

    request = mock_messaging.reply_message.call_args.args[0]
    message = request.messages[0]
    assert message.type == "text"
    assert "爸爸" in message.text


def test_reply_blocks_youtube_link_failure_before_line_api(monkeypatch):
    """YouTube deflection text must be replaced before reaching LINE."""
    monkeypatch.setattr(main.settings, "bot_muted", False)

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()
    bad_reply = (
        "這個 YouTube 直播連結本身未提供預覽內容，最直接的方式是點擊觀看以了解即時資訊。\n"
        "我目前手邊的資料僅包含 YouTube 的一般網域資訊，無法解析此直播的具體內容。"
    )

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", bad_reply, group_id="GRP001")

    request = mock_messaging.reply_message.call_args.args[0]
    sent = request.messages[0].text
    assert "YouTube 連結解析流程沒有正確啟動" in sent
    assert "點擊觀看" not in sent
    assert "無法解析此直播" not in sent


def test_reply_suppresses_low_value_checkin_commitment_before_line_api(monkeypatch):
    """Bot-as-human check-in commitments should be removed, not rewritten."""
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", "好喔，我們會好好打卡的。", group_id="GRP001")

    assert not mock_messaging.reply_message.called
    assert not mock_messaging.push_message.called


def test_reply_suppresses_low_value_helpless_text_before_line_api(monkeypatch):
    """Generic filler with no concrete help should not reach LINE."""
    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: False)

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply(
            "TOKEN001",
            "這個說法有一定道理，但需要進一步查證和具體分析。",
            group_id="GRP001",
        )

    assert not mock_messaging.reply_message.called
    assert not mock_messaging.push_message.called


def test_reply_suppressed_primary_can_still_send_due_reminder_piggyback(monkeypatch):
    """Suppressing a low-value primary reply must not block useful due reminders."""
    import calendar_db
    import reminder_push

    monkeypatch.setattr(main.settings, "bot_muted", False)
    monkeypatch.setattr(main, "_reminder_reply_piggyback_enabled", lambda: True)
    monkeypatch.setattr(main, "_pending_reply_enabled", lambda: False)
    monkeypatch.setattr(main, "_load_pending_explicit", lambda: {})
    monkeypatch.setattr(calendar_db, "REMINDER_OFFSETS", [])

    reminder_message = main.TextMessage(text="⏰ 提醒（明天）\n2026-07-20 08:00 打卡")
    monkeypatch.setattr(
        reminder_push,
        "due_reminders_for_reply",
        lambda *a, **kw: [
            {
                "reminder_id": 7,
                "stage": "1d",
                "text": reminder_message.text,
                "message": reminder_message,
            }
        ],
    )
    marked: list[tuple[int, str]] = []
    monkeypatch.setattr(
        reminder_push,
        "mark_reminders_pushed",
        lambda rows: marked.extend(rows) or len(rows),
    )

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
    ):
        main._reply(
            "TOKEN001",
            "這個說法有一定道理，但需要進一步查證和具體分析。",
            group_id="GRP001",
        )

    request = mock_messaging.reply_message.call_args.args[0]
    assert len(request.messages) == 1
    assert request.messages[0].text == reminder_message.text
    assert marked == [(7, "1d")]


def test_market_quote_reply_token_expired_does_not_fallback_push(monkeypatch):
    """Market quote replies are reply-only; expired reply token must not push to group."""
    monkeypatch.setattr(main.settings, "bot_muted", False)

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()
    mock_messaging.reply_message.side_effect = Exception("Invalid reply token")

    quote_text = "【市場報價｜夜間參考】\nTSM ADR: 250.00"

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._get_quota_footer", return_value=""),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", quote_text, group_id="GRP001")

    assert mock_messaging.reply_message.called
    assert not mock_messaging.push_message.called


def test_reply_only_market_quote_policy_skips_push_without_prefix(monkeypatch):
    """Gemini may paraphrase quotes, so callsite policy must suppress push too."""
    monkeypatch.setattr(main.settings, "bot_muted", False)

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()
    mock_messaging.reply_message.side_effect = Exception("Invalid reply token")

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._get_quota_footer", return_value=""),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply(
            "TOKEN001",
            "TSM 現在約 250 美元，夜盤先看期貨。",
            group_id="GRP001",
            allow_push_fallback=False,
        )

    assert mock_messaging.reply_message.called
    assert not mock_messaging.push_message.called


def test_market_quote_request_detects_contextual_followup_without_broad_now():
    context = [("user", "我覺得 NVDA 會再突破"), ("assistant", "要看財報")]

    assert main._is_market_quote_request("現在多少？", context=context)
    assert not main._is_market_quote_request("現在晚餐吃什麼？", context=context)
    assert not main._is_market_quote_request("距離 6/15 還有多少天？", context=context)
    assert not main._is_market_quote_request("NVDA 距離 6/15 還有多少天？", context=context)


def test_explicit_market_quote_success_uses_deterministic_reply_only():
    evt = _make_text_event(text="咪寶 現在多少？")
    context = [("user", "我覺得 NVDA 會再突破")]
    reply_calls = []

    def fake_reply(*args, **kwargs):
        reply_calls.append((args, kwargs))

    with (
        patch("main._detect_image_gen_request", return_value=None),
        patch("main._is_calendar_query", return_value=False),
        patch(
            "main._get_explicit_market_quote_reply",
            return_value="【市場報價｜測試】\nNVDA: 180.00",
        ),
        patch("main._build_quoted_block", return_value=""),
        patch("main._prefetch_urls", side_effect=lambda text: text),
        patch("main.memory.get_context", return_value=context),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._thinking_indicator", return_value=_noop_cm()),
        patch("main._llm_chat") as mock_llm,
        patch("main.memory.append_turn"),
        patch("main._try_save_correction"),
        patch("main._maybe_extract_facts"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply", side_effect=fake_reply),
    ):
        main._handle_explicit_text(evt, "GRP001", "現在多少？")

    assert reply_calls
    assert reply_calls[-1][1]["allow_push_fallback"] is False
    mock_llm.assert_not_called()


def test_explicit_market_quote_quota_exhausted_still_skips_llm():
    evt = _make_text_event(text="咪寶 黃金現在多少？")
    reply_calls = []

    def fake_reply(*args, **kwargs):
        reply_calls.append((args, kwargs))

    with (
        patch("main._detect_image_gen_request", return_value=None),
        patch("main._is_calendar_query", return_value=False),
        patch(
            "main._get_explicit_market_quote_reply",
            return_value="【市場報價｜測試】\nCOMEX 黃金近月期貨: 4,313.00",
        ),
        patch("main.memory.get_context", return_value=[]),
        patch("main._quota_exhausted", return_value=True),
        patch("main._llm_chat") as mock_llm,
        patch("main.memory.append_turn"),
        patch("main._reply", side_effect=fake_reply),
    ):
        main._handle_explicit_text(evt, "GRP001", "黃金現在多少？")

    assert reply_calls
    assert "黃金" in reply_calls[-1][0][1]
    assert reply_calls[-1][1]["allow_push_fallback"] is False
    mock_llm.assert_not_called()


def test_explicit_market_quote_helper_uses_stock_quote_without_llm():
    with (
        patch("stock_quote.get_taiex_month_line_text", return_value=None),
        patch(
            "stock_quote.get_contextual_quotes_text",
            return_value="【市場報價｜測試】\nCOMEX 黃金近月期貨: 4,313.00",
        ) as mock_quote,
    ):
        out = main._get_explicit_market_quote_reply("黃金現在多少？", context=[])

    assert out and "COMEX 黃金" in out
    mock_quote.assert_called_once()


def test_burst_market_quote_error_fallback_marks_reply_only():
    context = [("user", "我覺得 NVDA 會再突破")]
    reply_calls = []

    def fake_reply(*args, **kwargs):
        reply_calls.append((args, kwargs))

    with (
        patch("main.memory.get_context", return_value=context),
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda text: text),
        patch("main._thinking_indicator", return_value=_noop_cm()),
        patch("main._llm_chat", side_effect=RuntimeError("boom")),
        patch("main._reply", side_effect=fake_reply),
    ):
        main._handle_burst_flush("GRP001", "現在多少？", "TOKEN001")

    assert reply_calls
    assert reply_calls[-1][1]["allow_push_fallback"] is False


def test_burst_market_quote_empty_fallback_marks_reply_only(monkeypatch):
    monkeypatch.setattr(main, "_quota_exhausted_until_ts", 0)
    context = [("user", "我覺得 NVDA 會再突破")]
    reply_calls = []

    def fake_reply(*args, **kwargs):
        reply_calls.append((args, kwargs))

    with (
        patch("main.memory.get_context", return_value=context),
        patch("main.memory.check_fact_cache", return_value=None),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._prefetch_urls", side_effect=lambda text: text),
        patch("main._thinking_indicator", return_value=_noop_cm()),
        patch("main._llm_chat", return_value=""),
        patch("main._reply", side_effect=fake_reply),
    ):
        main._handle_burst_flush("GRP001", "現在多少？", "TOKEN001")

    assert reply_calls
    assert reply_calls[-1][1]["allow_push_fallback"] is False


def test_bug10_explicit_quota_miss_replies_without_pending():
    """Explicit @ bot quota miss should send visible fallback without pending."""
    evt = _make_text_event(text="咪寶 幫我分析")

    with (
        patch(
            "main._llm_chat",
            side_effect=[
                Exception("429 RESOURCE_EXHAUSTED PerDay free_tier_requests"),
                "",
            ],
        ),
        patch("main._mark_quota_exhausted"),
        patch("main.memory.get_context", return_value=[]),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=""),
        patch("main._prefetch_urls", return_value="幫我分析"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._thinking_indicator", return_value=_noop_cm()),
        patch("main._reply") as mock_reply,
    ):
        main._handle_explicit_text(evt, "GRP001", "幫我分析")

    mock_reply.assert_called_once_with(
        "TOKEN001",
        main._visible_llm_degraded_reply(),
        group_id="GRP001",
        allow_push_fallback=True,
    )
    pending = pending_store.load().get("GRP001", [])
    assert pending == []


def test_quota_saver_side_task_gate_reserves_main_reply_requests(monkeypatch):
    monkeypatch.setattr(main, "_quota_exhausted", lambda: False)
    monkeypatch.setattr(
        main.gemini_client,
        "get_gemini_quota_info",
        lambda: {
            "used_tokens": 1000,
            "limit_tokens": 1_000_000,
            "used_requests": 12,
            "limit_requests": 20,
            "used_thinking_tokens": 0,
        },
    )

    assert not main._gemini_side_task_allowed("test_side_task")


def test_quota_saver_fact_extract_skips_before_counter_bump(monkeypatch):
    bump = MagicMock()
    extract = MagicMock()

    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda reason: False)
    monkeypatch.setattr(main.memory, "bump_and_should_extract", bump)
    monkeypatch.setattr(main.gemini_client, "extract_facts", extract)

    main._maybe_extract_facts("GRP001")

    bump.assert_not_called()
    extract.assert_not_called()


def test_quota_saver_calendar_capture_uses_regex_path_without_gemini(monkeypatch):
    import calendar_db
    import calendar_extractor

    inserted: list[dict] = []

    def fake_extract_many(text, primary=None):
        assert primary is not None
        assert primary["has_event"] is False
        return {
            "is_cancellation": False,
            "cancel_target_keyword": None,
            "date": None,
            "time": None,
            "events": [
                {
                    "has_event": True,
                    "title": "預約羽球場",
                    "date": "2099-07-05",
                    "time": "18:00",
                    "location": None,
                    "participants": [],
                    "event_type": "family_gathering",
                }
            ],
        }

    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda reason: False)
    monkeypatch.setattr(
        calendar_extractor,
        "extract",
        MagicMock(side_effect=AssertionError("Gemini calendar extractor called")),
    )
    monkeypatch.setattr(calendar_extractor, "extract_many", fake_extract_many)
    monkeypatch.setattr(
        calendar_db,
        "insert_event",
        lambda **kw: inserted.append(kw) or 42,
    )

    main._maybe_capture_calendar_event(
        "GRP001", "6/28 18:00 預約7/5羽球場地", "USR001", "MSG001"
    )

    assert inserted
    assert inserted[0]["title"] == "預約羽球場"


def test_auto_capture_local_regex_saves_shared_badminton_events(monkeypatch):
    import calendar_db
    from datetime import datetime as real_datetime

    inserted: list[dict] = []

    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 27, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", FixedDateTime)
    monkeypatch.setattr(
        calendar_db,
        "insert_event",
        lambda **kw: inserted.append(kw) or f"event-{len(inserted)}",
    )

    main._auto_capture_text_if_important(
        "GRP001",
        "咪寶：幫我排入行程：\n7/5 1600和7/12 1800 全家打羽球",
        "USR001",
        "MSG001",
    )

    assert [
        (item["event_date"], item["event_time"], item["title"]) for item in inserted
    ] == [
        ("2026-07-05", "16:00", "全家打羽球"),
        ("2026-07-12", "18:00", "全家打羽球"),
    ]


def test_quota_saver_reminder_uses_regex_without_gemini(monkeypatch):
    saved: list[tuple] = []

    monkeypatch.setattr(main, "_gemini_side_task_allowed", lambda reason: False)
    monkeypatch.setattr(
        main.gemini_client,
        "extract_reminder",
        MagicMock(side_effect=AssertionError("Gemini reminder extractor called")),
    )
    monkeypatch.setattr(
        main,
        "_calendar_regex_to_reminder_result",
        lambda text, today, user_id: {
            "action": "預約羽球場",
            "year": 2099,
            "month": 7,
            "day": 5,
            "hour": 18,
            "minute": 0,
            "mention_aliases": [],
        },
    )
    monkeypatch.setattr(
        main.memory,
        "add_reminder",
        lambda *args, **kwargs: saved.append((args, kwargs)) or 7,
    )

    main._maybe_extract_reminder(
        "6/28 18:00 記得預約7/5羽球場地", "GRP001", "USR001", "MSG001"
    )

    assert saved
    assert saved[0][0][2] == "預約羽球場"


def test_quota_saver_classifier_is_rule_only_by_default(monkeypatch):
    import message_classifier

    main.settings.allowed_group_id = "GRP001"
    main.settings.bot_muted = False
    evt = _make_text_event(text="這是一則普通閒聊，沒有明確分類詞")
    updated: list[str] = []

    monkeypatch.delenv("GEMINI_CLASSIFIER_FALLBACK_ENABLED", raising=False)

    with (
        patch("main.feedback_collector.in_feedback_window", return_value=False),
        patch("main._try_one_shot_reply", return_value=False),
        patch("main._try_handle_calendar_correction", return_value=False),
        patch("main._detect_user_correction"),
        patch("main._auto_capture_text_if_important"),
        patch("main._maybe_extract_reminder"),
        patch("message_classifier.classify_async") as mock_async,
        patch(
            "message_classifier.update_category",
            side_effect=lambda group_id, message_id, category: updated.append(category),
        ),
        patch("main._handle_command", return_value=None),
        patch("main._is_dinner_question", return_value=False),
        patch("main._extract_gemini_trigger", return_value=None),
        patch("main.burst_filter.add_to_burst"),
    ):
        main._handle_text_message(evt, "GRP001")

    mock_async.assert_not_called()
    assert updated == [message_classifier.DEFAULT_CATEGORY]


def test_quota_exhausted_llm_chat_uses_direct_local_when_lite_misses(monkeypatch):
    import sys
    import types

    fake_lite = types.ModuleType("lite_reply")
    fake_lite.lite_reply = lambda text, context=None: None

    local_calls: list[dict] = []
    fake_local = types.ModuleType("local_llm")

    def fake_local_chat(text, context=None, system_prompt="", max_tokens=0):
        local_calls.append({
            "text": text,
            "context": context,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
        })
        return "本機模型回覆"

    fake_local.chat = fake_local_chat
    monkeypatch.setitem(sys.modules, "lite_reply", fake_lite)
    monkeypatch.setitem(sys.modules, "local_llm", fake_local)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)

    out = main._llm_chat(
        "買房子的錢阿婆有出一部分要怎麼處理",
        [("user", "前文")],
        [],
        [],
    )

    assert "本機模型回覆" in out
    assert "local LLM fallback" not in out
    assert local_calls
    assert local_calls[0]["context"] == [("user", "前文")]
    assert local_calls[0]["max_tokens"] == 360
    assert "即時時間基準" in local_calls[0]["system_prompt"]
    assert "目前台灣時間" in local_calls[0]["system_prompt"]
    assert "不要沿用舊年份" in local_calls[0]["system_prompt"]


def test_quota_exhausted_llm_chat_rechecks_gemini_before_local(monkeypatch):
    import time

    main._quota_exhausted_until_ts = time.time() + 3600
    main._quota_last_probe_ts = 0.0
    monkeypatch.setattr(main, "_GEMINI_QUOTA_RECHECK_INTERVAL_SEC", 1800)
    monkeypatch.setattr(main, "_save_quota_state", lambda: None)
    monkeypatch.setattr(main.gemini_client, "chat", lambda *a, **kw: "Gemini 回覆")

    out = main._llm_chat("你好", [], [], [])

    assert out == "Gemini 回覆"
    assert main._quota_exhausted_until_ts == 0.0
    assert main._quota_notified_for_ts == 0.0
    assert main._quota_last_probe_ts > 0


def test_quota_exhausted_llm_chat_recheck_429_falls_back_local(monkeypatch):
    import sys
    import time
    import types

    marked: list[bool] = []
    fake_lite = types.ModuleType("lite_reply")
    fake_lite.lite_reply = lambda text, context=None: None

    main._quota_exhausted_until_ts = time.time() + 3600
    main._quota_last_probe_ts = 0.0
    monkeypatch.setitem(sys.modules, "lite_reply", fake_lite)
    monkeypatch.setattr(main, "_GEMINI_QUOTA_RECHECK_INTERVAL_SEC", 1800)
    monkeypatch.setattr(main, "_save_quota_state", lambda: None)
    monkeypatch.setattr(
        main.gemini_client,
        "chat",
        MagicMock(side_effect=Exception("429 RESOURCE_EXHAUSTED PerDay")),
    )
    monkeypatch.setattr(main, "_mark_quota_exhausted", lambda: marked.append(True))
    monkeypatch.setattr(main, "_local_text_llm_fallback", lambda *a, **kw: "local 回覆")

    out = main._llm_chat("你好", [], [], [])

    assert out == "local 回覆"
    assert marked == [True]
    assert main._quota_last_probe_ts > 0


def test_quota_exhausted_startup_prewarms_local_llm(monkeypatch):
    started: list[dict] = []

    class FakeThread:
        def __init__(self, target, daemon=False, name=""):
            started.append({"target": target, "daemon": daemon, "name": name})

        def start(self):
            started[-1]["started"] = True

    monkeypatch.setattr(main, "_local_text_prewarm_started", False)
    monkeypatch.setattr(main, "_quota_exhausted", lambda: True)
    monkeypatch.setattr(main.threading, "Thread", FakeThread)

    main._prewarm_local_text_llm_if_needed()

    assert started
    assert started[0]["daemon"] is True
    assert started[0]["name"] == "local-llm-prewarm"
    assert started[0]["started"] is True


# ── Bug 7 ─────────────────────────────────────────────────────────────────────


def test_bug7_reply_skips_empty_text():
    """Bug 7 (e2cfb86/ee45f0d): _reply() must not call the LINE API when text
    is empty or whitespace-only.  Regression: transient minute-level Gemini errors
    return "" from _friendly_gemini_error; without the guard _reply() would send
    an empty body to LINE, causing a 400/403 or an invisible blank message.
    """
    main.settings.bot_muted = False  # ensure mute guard doesn't interfere

    mock_api = MagicMock()
    mock_api.__enter__ = MagicMock(return_value=mock_api)
    mock_api.__exit__ = MagicMock(return_value=False)
    mock_messaging = MagicMock()

    with (
        patch("main.ApiClient", return_value=mock_api),
        patch("main.MessagingApi", return_value=mock_messaging),
        patch("main._get_quota_footer", return_value=""),
        patch("main._load_pending_explicit", return_value={}),
    ):
        main._reply("TOKEN001", "")
        main._reply("TOKEN001", "   ")
        main._reply("TOKEN001", "\n\n")

    assert not mock_messaging.reply_message.called, (
        "LINE reply_message must NOT be called when text is empty/whitespace"
    )
