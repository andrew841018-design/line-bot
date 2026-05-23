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
  - notify_discord.send_dm / _alert_quality_violation 真實打 Discord webhook
    (2026-05-19 事件：tests/test_chat_golden.py 用 NEWS_CASE keyword
    + 短 mock reply 觸發 _quality_gate retry 3 次仍違規 → 真實打 Discord DM
    騷擾使用者。fix = autouse fixture 全 test 攔住對外副作用)
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


@pytest.fixture(autouse=True)
def block_external_side_effects(monkeypatch):
    """禁止 test 期間打真實 Discord DM / quality-violation alert。

    Root cause (2026-05-19): tests/test_chat_golden.py 用 NEWS_CASE keyword
    （「保險」「AI 詐騙新聞解析」）+ 短 mock reply（「這是個測試回覆。」）
    觸發 _quality_gate retry 3 次仍違規 → _alert_quality_violation 真實打
    Discord webhook，每次 pytest 至少 4~6 通 DM。

    在 conftest 一次擋住，比每個 test 自己 patch 穩；新 test 也自動受惠。
    """
    import notify_discord
    monkeypatch.setattr(notify_discord, "send_dm", lambda *a, **kw: True, raising=False)

    # 雙保險：直接攔 entry point，未來 production code 若新增其他 alert
    # 也不會在 test 偷打
    import gemini_client
    monkeypatch.setattr(
        gemini_client, "_alert_quality_violation",
        lambda *a, **kw: None, raising=False,
    )
    # 同步攔 _log_quality_violation — tests 在 prod line_bot.db 的 persona_notes
    # 留下大量 C_test_group 違規記錄（2026-05-23 auto iterate 發現 24h 內 30 筆
    # 全是 mock 回覆「這是個測試回覆。」），污染自我學習資料 + 拖慢人工排查。
    monkeypatch.setattr(
        gemini_client, "_log_quality_violation",
        lambda *a, **kw: None, raising=False,
    )
