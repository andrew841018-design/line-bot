"""
pytest conftest — resets all mutable globals in main.py before/after every test.

Without this, global state leaks between tests:
  - _quota_exhausted_until_ts set by one test bleeds into the next
  - _grok_intro_sent_groups accumulates across tests (Bug 4 pattern)
  - _QUOTA_STATE_FILE writes touch the real quota_state.json (Bug 5 pattern)
  - pending_store.PENDING_PATH module-level constant writes touch the real
    pending_explicit_reply.json (2026-05-17 incident: pytest 寫穿 production
    pending → 10 筆使用者待回訊息被清空)
  - finance_view_db._DB_PATH module-level constant writes touch real line_bot.db
    (2026-05-18 加：跟 pending_store 同 pattern 隔離)
"""

import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")

# PySpark needs to know the venv Python (system Python default breaks Java workers)
import sys as _sys
os.environ.setdefault("PYSPARK_PYTHON", _sys.executable)
os.environ.setdefault("PYSPARK_DRIVER_PYTHON", _sys.executable)

import finance_view_db  # noqa: E402
import main  # noqa: E402
import pending_store  # noqa: E402

# Snapshot originals once at import time (before any test runs)
_ORIG_QUOTA_STATE_FILE = main._QUOTA_STATE_FILE
_ORIG_PENDING_PATH = main._PENDING_EXPLICIT_PATH
_ORIG_PENDING_STORE_PATH = pending_store.PENDING_PATH
_ORIG_PENDING_STORE_LOCK = pending_store.LOCK_PATH
_ORIG_BOT_MUTED = main.settings.bot_muted
_ORIG_ALLOWED_GROUP_ID = main.settings.allowed_group_id
_ORIG_FV_DB_PATH = finance_view_db._DB_PATH


@pytest.fixture(autouse=True)
def reset_main_globals():
    """Reset all mutable globals before AND after each test."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    tmp_quota_path = tmp.name

    tmp_pending = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_pending.write(b"{}")
    tmp_pending.close()
    tmp_pending_path = pathlib.Path(tmp_pending.name)
    tmp_pending_lock = pathlib.Path(tmp_pending.name + ".lock")

    tmp_fv = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_fv.close()
    tmp_fv_path = pathlib.Path(tmp_fv.name)

    # ── before ────────────────────────────────────────────────────────────
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._PENDING_EXPLICIT_PATH = str(tmp_pending_path)
    main._QUOTA_STATE_FILE = tmp_quota_path  # isolate file I/O
    pending_store.PENDING_PATH = tmp_pending_path  # 防 production pending 被寫穿
    pending_store.LOCK_PATH = tmp_pending_lock
    finance_view_db._DB_PATH = tmp_fv_path  # 防 production line_bot.db 被寫穿
    finance_view_db.init_db()  # 在 temp DB 建 schema
    main.settings.bot_muted = True
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID

    yield

    # ── after ─────────────────────────────────────────────────────────────
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._PENDING_EXPLICIT_PATH = _ORIG_PENDING_PATH
    main._QUOTA_STATE_FILE = _ORIG_QUOTA_STATE_FILE
    pending_store.PENDING_PATH = _ORIG_PENDING_STORE_PATH
    pending_store.LOCK_PATH = _ORIG_PENDING_STORE_LOCK
    finance_view_db._DB_PATH = _ORIG_FV_DB_PATH
    main.settings.bot_muted = _ORIG_BOT_MUTED
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID

    for p in (tmp_quota_path, tmp_pending.name, str(tmp_pending_lock), str(tmp_fv_path)):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
