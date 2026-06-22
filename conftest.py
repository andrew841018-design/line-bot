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
import importlib
import pathlib
import sqlite3
import tempfile
import time
import warnings
from pathlib import Path

import pytest

try:
    from pydantic.warnings import PydanticDeprecatedSince212

    warnings.filterwarnings(
        "ignore",
        message=(
            r"Using `@model_validator` with mode='after' on a classmethod "
            r"is deprecated\..*"
        ),
        category=PydanticDeprecatedSince212,
        module=r"google\.genai\.types",
    )
except Exception:
    pass

warnings.filterwarnings(
    "ignore",
    message=r"pkg_resources is deprecated as an API\..*",
    category=UserWarning,
    module=r"jieba\._compat",
)

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")
_ORIG_SQLITE_ENV = os.environ.get("SQLITE_PATH")
_POST_TEST_SQLITE_PATH = Path(tempfile.gettempdir()) / (
    f"line_bot_pytest_quarantine_{os.getpid()}.db"
)
os.environ["SQLITE_PATH"] = str(_POST_TEST_SQLITE_PATH)

# PySpark needs to know the venv Python (system Python default breaks Java workers)
import sys as _sys
os.environ.setdefault("PYSPARK_PYTHON", _sys.executable)
os.environ.setdefault("PYSPARK_DRIVER_PYTHON", _sys.executable)

import config  # noqa: E402
import finance_view_db  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
import pending_store  # noqa: E402

# Snapshot originals once at import time (before any test runs)
_ORIG_QUOTA_STATE_FILE = main._QUOTA_STATE_FILE
_ORIG_PENDING_PATH = main._PENDING_EXPLICIT_PATH
_ORIG_PENDING_STORE_PATH = pending_store.PENDING_PATH
_ORIG_PENDING_STORE_LOCK = pending_store.LOCK_PATH
_ORIG_BOT_MUTED = main.settings.bot_muted
_ORIG_PENDING_REPLY_ENABLED = main._PENDING_REPLY_ENABLED
_ORIG_ALLOWED_GROUP_ID = main.settings.allowed_group_id
# 2026-05-27 multi-group: tests 用 main.settings.allowed_group_id = "GRP001" 設 gate target，
# 但 webhook gate 讀 settings.allowed_group_ids（computed_field）。當 raw 從 env 載入非空時，
# computed 走 raw 分支忽略 singular → gate 不認 test group → silent drop。
# Fix: 每個 test setup 把 allowed_group_ids_raw 暫時清空，computed 走 singular fallback。
_ORIG_ALLOWED_GROUP_IDS_RAW = main.settings.allowed_group_ids_raw
_ORIG_FV_DB_PATH = finance_view_db._DB_PATH
_ORIG_MEMORY_DB_PATH = memory._DB_PATH

_SQLITE_MODULE_NAMES = (
    "memory",
    "calendar_db",
    "embedding_recall",
    "todo",
    "finance_view_db",
    "food_db",
    "family_poll",
    "anniversary",
    "knowledge_graph",
    "message_classifier",
)


def _sqlite_sidecar_paths(db_path: Path) -> tuple[str, str, str]:
    return (str(db_path), f"{db_path}-wal", f"{db_path}-shm")


def _configure_sqlite_module(module_name: str, db_path: Path) -> None:
    module = importlib.import_module(module_name)
    if hasattr(module, "_DB_PATH"):
        module._DB_PATH = db_path

    if module_name == "memory":
        init = module._init_db
    elif module_name == "knowledge_graph":
        if not hasattr(module, "_ensure_table"):
            return
        init = module._ensure_table
    elif module_name == "message_classifier":
        if not hasattr(module, "ensure_schema"):
            return
        module._SCHEMA_ENSURED = False
        if hasattr(module, "_SCHEMA_ENSURED_PATHS"):
            module._SCHEMA_ENSURED_PATHS.clear()
        init = lambda: module.ensure_schema(db_path)
    elif hasattr(module, "init_db"):
        init = module.init_db
    else:
        return

    for attempt in range(8):
        try:
            init()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower() or attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def _configure_test_sqlite(db_path: Path) -> None:
    os.environ["SQLITE_PATH"] = str(db_path)
    config.settings.sqlite_path = str(db_path)
    main.settings.sqlite_path = str(db_path)
    for module_name in _SQLITE_MODULE_NAMES:
        _configure_sqlite_module(module_name, db_path)


def _restore_sqlite_paths() -> None:
    """After a test, never point async-capable modules back at production DB.

    Background daemon threads/executors may still run after fixture teardown.
    Keeping module globals on a quarantine DB makes future missed async writers
    fail safe instead of writing through to the real line_bot.db.
    """
    _configure_test_sqlite(_POST_TEST_SQLITE_PATH)


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

    tmp_app_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_app_db.close()
    tmp_app_db_path = Path(tmp_app_db.name)

    # ── before ────────────────────────────────────────────────────────────
    _configure_test_sqlite(tmp_app_db_path)
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._PENDING_REPLY_ENABLED = _ORIG_PENDING_REPLY_ENABLED
    main._PENDING_EXPLICIT_PATH = str(tmp_pending_path)
    main._QUOTA_STATE_FILE = tmp_quota_path  # isolate file I/O
    pending_store.PENDING_PATH = tmp_pending_path  # 防 production pending 被寫穿
    pending_store.LOCK_PATH = tmp_pending_lock
    main.settings.bot_muted = True
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID
    main.settings.allowed_group_ids_raw = ""  # 清空讓 computed 走 singular fallback

    yield

    # ── after ─────────────────────────────────────────────────────────────
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._PENDING_REPLY_ENABLED = _ORIG_PENDING_REPLY_ENABLED
    main._PENDING_EXPLICIT_PATH = _ORIG_PENDING_PATH
    main._QUOTA_STATE_FILE = _ORIG_QUOTA_STATE_FILE
    pending_store.PENDING_PATH = _ORIG_PENDING_STORE_PATH
    pending_store.LOCK_PATH = _ORIG_PENDING_STORE_LOCK
    _restore_sqlite_paths()
    main.settings.bot_muted = _ORIG_BOT_MUTED
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID
    main.settings.allowed_group_ids_raw = _ORIG_ALLOWED_GROUP_IDS_RAW

    for p in (
        tmp_quota_path,
        tmp_pending.name,
        str(tmp_pending_lock),
        *_sqlite_sidecar_paths(tmp_app_db_path),
    ):
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
