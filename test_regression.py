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
    processing.  If all items are __bot__, group is cleared with no LLM call.
    Regression: __bot__ entries were processed, causing duplicate pushes.
    """
    main.settings.bot_muted = False
    main._quota_exhausted_until_ts = 0.0

    all_bot_pending: dict = {
        "GRP001": [
            {"user_id": "__bot__", "text": "bot 自己的舊回覆 1", "timestamp": 100, "type": "text"},
            {"user_id": "__bot__", "text": "bot 自己的舊回覆 2", "timestamp": 101, "type": "text"},
        ]
    }

    llm_calls: list = []
    cleared: list[str] = []

    with (
        patch("main._load_pending_explicit", return_value=all_bot_pending),
        patch(
            "main._clear_pending_explicit",
            side_effect=lambda gid: cleared.append(gid),
        ),
        patch(
            "main._llm_chat",
            side_effect=lambda *a, **kw: llm_calls.append(True) or "reply",
        ),
        patch("main.grok_client.quota_exhausted", return_value=False),
    ):
        main._process_pending_on_startup()

    assert len(llm_calls) == 0, "LLM must NOT be called when all pending items are __bot__"
    assert "GRP001" in cleared, "Group must be cleared even when all items are __bot__"


# ── Bug 4 ─────────────────────────────────────────────────────────────────────


def test_bug4_grok_intro_not_sent_twice():
    """Bug 4 (9d2528c): Grok fallback intro sent at most once per group per session.
    _grok_intro_sent_groups gates the push; if group already in set, no intro.
    Regression: intro was pushed on every _process_pending_on_startup call.
    """
    main._quota_exhausted_until_ts = time.time() + 3600  # Gemini gone
    main.settings.bot_muted = False
    # Simulate intro already sent this session
    main._grok_intro_sent_groups.add("GRP001")

    push_requests: list = []
    mock_messaging = MagicMock()
    mock_messaging.push_message.side_effect = lambda req: push_requests.append(req)
    mock_api_ctx = MagicMock()
    mock_api_ctx.__enter__ = MagicMock(return_value=mock_messaging)
    mock_api_ctx.__exit__ = MagicMock(return_value=False)

    # Empty items: no LLM push will happen, so any push must be the intro
    with (
        patch("main._load_pending_explicit", return_value={"GRP001": []}),
        patch("main._clear_pending_explicit"),
        patch("main.grok_client.quota_exhausted", return_value=False),
        patch("main.ApiClient", return_value=mock_api_ctx),
        patch("main.MessagingApi", return_value=mock_messaging),
    ):
        main._process_pending_on_startup()

    # No push at all means no intro was sent
    assert len(push_requests) == 0, (
        "Intro must NOT be sent when group is already in _grok_intro_sent_groups; "
        f"got {len(push_requests)} push(es): {push_requests}"
    )


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


def test_bug6_quota_footer_shows_max_percentage():
    """Bug 6 (7c6ce81): footer displays max(token_pct, req_pct).
    When req_pct > token_pct, the higher value must appear in the footer.
    Regression: footer only showed token_pct, hiding a nearly-full request quota.
    """
    main._quota_exhausted_until_ts = 0.0  # Gemini NOT exhausted → normal footer branch

    mock_info = {
        "used_tokens": 30,
        "limit_tokens": 100,  # token_pct = 30 %
        "used_requests": 8,
        "limit_requests": 10,  # req_pct  = 80 %
        "used_thinking_tokens": 0,
    }
    with patch("main.gemini_client.get_gemini_quota_info", return_value=mock_info):
        footer = main._get_quota_footer()

    assert "80" in footer, f"Footer must show 80 (the higher value), got: {footer!r}"
    # 30 should not appear as a quota percentage (guard both "30%" and "30.0%")
    import re as _re
    assert not _re.search(r"\b30\.?0?%", footer), f"Footer must NOT show 30% (the lower value), got: {footer!r}"


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
