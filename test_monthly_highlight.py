"""Feature C tests — monthly highlight logic (Andrew 2026-05-25)."""

import os
from unittest.mock import patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")


def test_monthly_highlight_main_exists():
    import monthly_highlight
    assert hasattr(monthly_highlight, "main")
    assert callable(monthly_highlight.main)


def test_fetch_last_30d_messages_callable():
    import monthly_highlight
    assert callable(monthly_highlight.fetch_last_30d_messages)


def test_summarize_monthly_via_gemini_empty_returns_none():
    import monthly_highlight
    assert monthly_highlight.summarize_monthly_via_gemini([]) is None


def test_main_returns_1_when_no_group_id():
    import monthly_highlight
    with patch.object(monthly_highlight, "GROUP_ID", ""):
        assert monthly_highlight.main() == 1


def test_main_skips_when_too_few_messages():
    """少於 MIN_MESSAGES_FOR_HIGHLIGHT → skip return 0."""
    import monthly_highlight
    with patch.object(monthly_highlight, "GROUP_ID", "GRP_TEST"), \
         patch.object(
             monthly_highlight, "fetch_last_30d_messages",
             return_value=[("u", "hi", 0)] * 10,
         ), \
         patch.object(monthly_highlight, "_push") as mock_push:
        rc = monthly_highlight.main()
    assert rc == 0
    assert not mock_push.called


def test_main_pushes_when_enough_messages():
    """≥MIN_MESSAGES + summarize 成功 → _push called。"""
    import monthly_highlight
    fake_msgs = [("u", f"msg{i}", 0) for i in range(30)]
    with patch.object(monthly_highlight, "GROUP_ID", "GRP_TEST"), \
         patch.object(
             monthly_highlight, "fetch_last_30d_messages", return_value=fake_msgs,
         ), \
         patch.object(
             monthly_highlight, "summarize_monthly_via_gemini",
             return_value="📅 本月月報\nA. ...",
         ), \
         patch.object(monthly_highlight, "_push", return_value=True) as mock_push:
        rc = monthly_highlight.main()
    assert rc == 0
    assert mock_push.called


def test_main_returns_1_when_push_fails():
    import monthly_highlight
    fake_msgs = [("u", f"msg{i}", 0) for i in range(30)]
    with patch.object(monthly_highlight, "GROUP_ID", "GRP_TEST"), \
         patch.object(
             monthly_highlight, "fetch_last_30d_messages", return_value=fake_msgs,
         ), \
         patch.object(
             monthly_highlight, "summarize_monthly_via_gemini",
             return_value="some monthly summary",
         ), \
         patch.object(monthly_highlight, "_push", return_value=False):
        rc = monthly_highlight.main()
    assert rc == 1
