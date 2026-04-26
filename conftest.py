"""
pytest conftest — resets all mutable globals in main.py before/after every test.

Without this, global state leaks between tests:
  - _quota_exhausted_until_ts set by one test bleeds into the next
  - _grok_intro_sent_groups accumulates across tests (Bug 4 pattern)
  - _QUOTA_STATE_FILE writes touch the real quota_state.json (Bug 5 pattern)
"""

import os
import tempfile

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

import main  # noqa: E402

# Snapshot originals once at import time (before any test runs)
_ORIG_QUOTA_STATE_FILE = main._QUOTA_STATE_FILE
_ORIG_PENDING_PATH = main._PENDING_EXPLICIT_PATH
_ORIG_BOT_MUTED = main.settings.bot_muted
_ORIG_ALLOWED_GROUP_ID = main.settings.allowed_group_id


@pytest.fixture(autouse=True)
def reset_main_globals():
    """Reset all mutable globals before AND after each test."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    tmp_quota_path = tmp.name

    # ── before ────────────────────────────────────────────────────────────
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._PENDING_EXPLICIT_PATH = _ORIG_PENDING_PATH
    main._QUOTA_STATE_FILE = tmp_quota_path  # isolate file I/O
    main.settings.bot_muted = True
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID

    yield

    # ── after ─────────────────────────────────────────────────────────────
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._PENDING_EXPLICIT_PATH = _ORIG_PENDING_PATH
    main._QUOTA_STATE_FILE = _ORIG_QUOTA_STATE_FILE
    main.settings.bot_muted = _ORIG_BOT_MUTED
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID

    try:
        os.unlink(tmp_quota_path)
    except FileNotFoundError:
        pass
