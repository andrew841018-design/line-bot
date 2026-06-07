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

import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

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


def test_bug8_burst_empty_quota_suppresses_without_pending():
    """Andrew 2026-06-06: pending reply disabled. Quota exhausted + burst empty
    should not send an immediate fallback and should not enqueue pending reply.
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

    mock_reply.assert_not_called()
    pending = pending_store.load().get("GRP001", [])
    assert pending == []


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


def test_explicit_market_quote_success_marks_reply_only():
    evt = _make_text_event(text="咪寶 現在多少？")
    context = [("user", "我覺得 NVDA 會再突破")]
    reply_calls = []

    def fake_reply(*args, **kwargs):
        reply_calls.append((args, kwargs))

    with (
        patch("main._detect_image_gen_request", return_value=None),
        patch("main._is_calendar_query", return_value=False),
        patch("main._build_quoted_block", return_value=""),
        patch("main._prefetch_urls", side_effect=lambda text: text),
        patch("main.memory.get_context", return_value=context),
        patch("main.memory.top_facts", return_value=[]),
        patch("main._get_persona_notes", return_value=[]),
        patch("main._thinking_indicator", return_value=_noop_cm()),
        patch("main._llm_chat", return_value="NVDA 現在約 180 美元"),
        patch("main.memory.append_turn"),
        patch("main._try_save_correction"),
        patch("main._maybe_extract_facts"),
        patch("main._maybe_capture_calendar_event"),
        patch("main._reply", side_effect=fake_reply),
    ):
        main._handle_explicit_text(evt, "GRP001", "現在多少？")

    assert reply_calls
    assert reply_calls[-1][1]["allow_push_fallback"] is False


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


def test_bug10_explicit_quota_miss_suppresses_without_pending():
    """Explicit @ bot quota miss should not store pending and should not reply fallback."""
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

    mock_reply.assert_not_called()
    pending = pending_store.load().get("GRP001", [])
    assert pending == []


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
