"""Feature B tests — weekly retro logic (Andrew 2026-05-25)."""

import os
from unittest.mock import patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")


def test_weekly_retro_main_exists():
    """Module + main() entry point 存在。"""
    import weekly_retro
    assert hasattr(weekly_retro, "main")
    assert callable(weekly_retro.main)


def test_fetch_last_7d_messages_callable():
    """fetch_last_7d_messages callable."""
    import weekly_retro
    assert callable(weekly_retro.fetch_last_7d_messages)


def test_summarize_via_gemini_empty_returns_none():
    """空 messages list → None."""
    import weekly_retro
    result = weekly_retro.summarize_via_gemini([])
    assert result is None


def test_main_returns_1_when_no_group_id():
    """GROUP_ID 沒設 → 直接 fail 1."""
    import weekly_retro
    with patch.object(weekly_retro, "GROUP_ID", ""):
        rc = weekly_retro.main()
    assert rc == 1


def test_main_skips_when_too_few_messages():
    """少於 MIN_MESSAGES_FOR_RETRO → skip retro return 0."""
    import weekly_retro
    with patch.object(weekly_retro, "GROUP_ID", "GRP_TEST"), \
         patch.object(
             weekly_retro, "fetch_last_7d_messages",
             return_value=[("u", "hi", 0)] * 3,
         ), \
         patch.object(weekly_retro, "_push") as mock_push:
        rc = weekly_retro.main()
    assert rc == 0
    assert not mock_push.called


def test_main_pushes_when_enough_messages():
    """≥MIN_MESSAGES + summarize 成功 → _push called。"""
    import weekly_retro
    fake_msgs = [("u", f"msg{i}", 0) for i in range(10)]
    with patch.object(weekly_retro, "GROUP_ID", "GRP_TEST"), \
         patch.object(
             weekly_retro, "fetch_last_7d_messages", return_value=fake_msgs,
         ), \
         patch.object(
             weekly_retro, "summarize_via_gemini",
             return_value="📅 本週回顧\n1. xxx",
         ), \
         patch.object(weekly_retro, "_push", return_value=True) as mock_push:
        rc = weekly_retro.main()
    assert rc == 0
    assert mock_push.called


def test_main_returns_1_when_push_fails():
    """summarize 成功但 push 失敗 → return 1。"""
    import weekly_retro
    fake_msgs = [("u", f"msg{i}", 0) for i in range(10)]
    with patch.object(weekly_retro, "GROUP_ID", "GRP_TEST"), \
         patch.object(
             weekly_retro, "fetch_last_7d_messages", return_value=fake_msgs,
         ), \
         patch.object(
             weekly_retro, "summarize_via_gemini", return_value="some summary",
         ), \
         patch.object(weekly_retro, "_push", return_value=False):
        rc = weekly_retro.main()
    assert rc == 1
