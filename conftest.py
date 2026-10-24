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
import shutil
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
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
warnings.filterwarnings(
    "ignore",
    message=r"builtin type SwigPy(?:Packed|Object) has no __module__ attribute",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r"builtin type swigvarlink has no __module__ attribute",
    category=DeprecationWarning,
)

os.environ.setdefault("LINE_CHANNEL_SECRET", "dummy_secret_32bytes_padding000")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "dummy")
os.environ.setdefault("GEMINI_API_KEY", "dummy")
os.environ.setdefault("GROK_API_KEY", "dummy")
os.environ.setdefault("BOT_MUTED", "true")
# 2026-10-16 字卡大字版：測試預設不產生網頁、不加按鈕（本機有 tunnel 網址檔），要測的測試自己打開
os.environ["LINE_BOT_CARD_PAGES"] = "0"  # 直接設：shell 有 export 也不能讓測試讀真的 tunnel 檔、寫進 state/
# 2026-10-17 TikTok 讀取：預設關閉（0 = 不送 Gemini、不下載），要測的測試用假物件自己打開
# TODO(2026-10-17 D4): 這只關掉讀影片／圖片；沒有替換 yt-dlp 的既有測試仍可能真的連網抓 metadata。
os.environ["LINE_BOT_TIKTOK_MEDIA_DAILY_CAP"] = "0"
# Semantic restatement judge calls Gemini; unit tests enable it explicitly with a fake.
os.environ.setdefault("LINE_BOT_RESTATEMENT_JUDGE", "0")
# 2026-10-04: the separate-quota last Gemini tier stays off unless a test
# turns it on (many tests script exactly the two 2.5 models).
os.environ.setdefault("GEMINI_LAST_TIER_MODEL", "")
_ORIG_SQLITE_ENV = os.environ.get("SQLITE_PATH")
_POST_TEST_SQLITE_PATH = Path(tempfile.gettempdir()) / (
    f"line_bot_pytest_quarantine_{os.getpid()}.db"
)
os.environ["SQLITE_PATH"] = str(_POST_TEST_SQLITE_PATH)
# 2026-10-11 每日記憶：每個測試另有自己的路徑（isolate_daily_memory）；這裡是測試之間、
# 以及測試結束後還在跑的背景執行緒用的隔離路徑——絕不落回正式 state/ 的 ledger 或 HMAC 金鑰
# （金鑰指紋記在 DB 裡，隔離的 DB 要配隔離的金鑰）。
_POST_TEST_DAILY_MEMORY_DIR = Path(tempfile.gettempdir()) / (
    f"line_bot_pytest_quarantine_{os.getpid()}_daily_memory"
)
for _env_name, _leaf in (
    ("LINE_BOT_FACT_LEDGER_PATH", "fact_ledger.jsonl"),
    ("LINE_BOT_DAILY_MEMORY_STATE_DIR", "state"),
    ("LINE_BOT_PRIVACY_TERMS_PATH", "privacy_terms.txt"),
    ("LINE_BOT_FACT_LOCK_DIR", "locks"),
    ("LINE_BOT_FACT_HASH_KEY_PATH", "fact_hash.key"),
    ("LINE_FAMILY_KINSHIP_PATH", "no_kinship.json"),  # 2026-10-17：不存在的路徑，絕不讀到正式的角色檔
):
    os.environ[_env_name] = str(_POST_TEST_DAILY_MEMORY_DIR / _leaf)

# PySpark needs to know the venv Python (system Python default breaks Java workers)
import sys as _sys
os.environ.setdefault("PYSPARK_PYTHON", _sys.executable)
os.environ.setdefault("PYSPARK_DRIVER_PYTHON", _sys.executable)

import burst_filter  # noqa: E402
import claude_client  # noqa: E402
import config  # noqa: E402
import finance_view_db  # noqa: E402
import gemini_client  # noqa: E402
import main  # noqa: E402
import media_pipeline  # noqa: E402
import tiktok_media  # noqa: E402
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
_ORIG_GEMINI_USAGE_FILE = gemini_client._USAGE_FILE
_ORIG_GEMINI_MODEL_USAGE_FILE = gemini_client._MODEL_USAGE_FILE
_ORIG_GEMINI_MODEL_USAGE_LOCK_FILE = gemini_client._MODEL_USAGE_LOCK_FILE

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
# 2026-10-11 爸媽健康：這個模組本身不存在時（還沒部署）跳過，不讓整套測試在 setup 就失敗；
# 它 import 別的東西失敗時照樣報錯，不會默默寫到錯的 DB。
_OPTIONAL_SQLITE_MODULE_NAMES = ("parent_health_db",)


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
    for module_name in _OPTIONAL_SQLITE_MODULE_NAMES:
        try:
            _configure_sqlite_module(module_name, db_path)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise


def _restore_sqlite_paths() -> None:
    """After a test, never point async-capable modules back at production DB.

    Background daemon threads/executors may still run after fixture teardown.
    Keeping module globals on a quarantine DB makes future missed async writers
    fail safe instead of writing through to the real line_bot.db.
    """
    _configure_test_sqlite(_POST_TEST_SQLITE_PATH)


@pytest.fixture(autouse=True)
def reset_main_globals(monkeypatch):
    """Reset all mutable globals before AND after each test."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    tmp_quota_path = tmp.name
    tmp_usage = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_usage.write(b'{"date":"pytest","tokens":0,"requests":0}')
    tmp_usage.close()
    tmp_model_usage = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_model_usage.write(b"{}")
    tmp_model_usage.close()

    tmp_pending = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_pending.write(b"{}")
    tmp_pending.close()
    tmp_pending_path = pathlib.Path(tmp_pending.name)
    tmp_pending_lock = pathlib.Path(tmp_pending.name + ".lock")

    tmp_app_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_app_db.close()
    tmp_app_db_path = Path(tmp_app_db.name)

    # 2026-10-10: display names the bot looks up (line_mentions) go to a temp
    # file, never state/member_display_names.json of the tree running the tests.
    # monkeypatch restores it after any test's own setenv of the same variable.
    tmp_display_names = Path(tempfile.mkdtemp()) / "member_display_names.json"
    monkeypatch.setenv("LINE_MEMBER_DISPLAY_NAMES_PATH", str(tmp_display_names))
    # 2026-10-10: the family's real user_aliases.json / family_roles.local.json
    # must not leak into tests that expect no @ ping on the receipt (爸爸 媽媽
    # resolve in the real file). A fixed test alias map keeps real family
    # names unlisted while giving tests a resolvable alias so helpers that
    # key off "有 user alias 設定" (e.g. aliases_mentioned_in_text) still work
    # for "全家" / "測試成員甲"; tests that need their own aliases setenv
    # their own path to override.
    import json as _json
    tmp_alias_dir = Path(tempfile.mkdtemp())
    tmp_alias_path = tmp_alias_dir / "user_aliases.json"
    tmp_alias_path.write_text(
        _json.dumps(
            {"U_TEST_A": "測試成員甲", "U_TEST_B": "測試成員乙"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp_role_path = tmp_alias_dir / "family_roles.local.json"
    tmp_role_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LINE_USER_ALIASES_PATH", str(tmp_alias_path))
    monkeypatch.setenv("LINE_FAMILY_ROLE_ALIASES_PATH", str(tmp_role_path))
    main._member_labels.clear()
    with burst_filter._labelled_lock:
        burst_filter._labelled_texts.clear()

    # ── before ────────────────────────────────────────────────────────────
    # 2026-09-27: an 8 s burst timer left by an earlier test (e.g. an event run
    # through _handle_event) must not flush inside this one and hit its
    # patched _llm_chat / _reply.
    with burst_filter._lock:
        for timer in burst_filter._timers.values():
            timer.cancel()
        burst_filter._timers.clear()
        burst_filter._pending.clear()
        burst_filter._last_reply_tokens.clear()
        burst_filter._inflight.clear()
    # 2026-10-24: tests reuse reply-token constants; the token -> inbound lookup the send fence
    # keeps after a reply was accepted must not carry one test's event into the next.
    main._inbound_ids_by_token.clear()
    # 2026-10-07: CLI timeouts faked by one test must not leave the CLI
    # resting (claude_client's timeout breaker) in the next.
    claude_client._cli_timeout_streak = 0
    claude_client._cli_timeout_breaker_until = 0.0
    # 2026-10-10: media_pipeline._OCR_ADMISSION is a BoundedSemaphore(1) and
    # _OCR_EXECUTOR is a single-worker ThreadPoolExecutor. Both leak state
    # across tests: (a) add_done_callback(release) fires on the executor
    # thread AFTER future.result() returns on the main thread, so a prior
    # test can leave the semaphore at 0 for a window; (b) future.cancel()
    # on a running task does not stop the thread, so a slow_ocr sleep
    # stays on the executor and blocks the next test's submit. Replace
    # both at test start; _extract_ocr_with_deadline captures sem locally
    # so in-flight callbacks safely release the stale instance.
    try:
        media_pipeline._OCR_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    media_pipeline._OCR_ADMISSION = threading.BoundedSemaphore(1)
    media_pipeline._OCR_EXECUTOR = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="media-ocr"
    )
    main._TIKTOK_MEDIA_SLOT = threading.BoundedSemaphore(1)
    main._TIKTOK_METADATA_SLOTS = threading.BoundedSemaphore(2)
    _configure_test_sqlite(tmp_app_db_path)
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._quota_last_probe_ts = 0.0
    gemini_client._USAGE_FILE = tmp_usage.name
    gemini_client._MODEL_USAGE_FILE = tmp_model_usage.name
    gemini_client._MODEL_USAGE_LOCK_FILE = tmp_model_usage.name + ".lock"
    gemini_client._last_tier_backoff_until = 0.0
    gemini_client._side_task_backoff_until = 0.0
    gemini_client._media_backoff_until = 0.0
    gemini_client._media_bad_requests = 0
    gemini_client._media_cap_warned = ""
    tiktok_media.clear_cache()
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
    claude_client._cli_timeout_streak = 0
    claude_client._cli_timeout_breaker_until = 0.0
    main._quota_exhausted_until_ts = 0.0
    main._quota_notified_for_ts = 0.0
    main._quota_last_probe_ts = 0.0
    gemini_client._USAGE_FILE = _ORIG_GEMINI_USAGE_FILE
    gemini_client._MODEL_USAGE_FILE = _ORIG_GEMINI_MODEL_USAGE_FILE
    gemini_client._MODEL_USAGE_LOCK_FILE = _ORIG_GEMINI_MODEL_USAGE_LOCK_FILE
    gemini_client._last_tier_backoff_until = 0.0
    gemini_client._side_task_backoff_until = 0.0
    gemini_client._media_backoff_until = 0.0
    gemini_client._media_bad_requests = 0
    gemini_client._media_cap_warned = ""
    tiktok_media.clear_cache()
    main._PENDING_REPLY_ENABLED = _ORIG_PENDING_REPLY_ENABLED
    main._PENDING_EXPLICIT_PATH = _ORIG_PENDING_PATH
    main._QUOTA_STATE_FILE = _ORIG_QUOTA_STATE_FILE
    pending_store.PENDING_PATH = _ORIG_PENDING_STORE_PATH
    pending_store.LOCK_PATH = _ORIG_PENDING_STORE_LOCK
    _restore_sqlite_paths()
    main.settings.bot_muted = _ORIG_BOT_MUTED
    main.settings.allowed_group_id = _ORIG_ALLOWED_GROUP_ID
    main.settings.allowed_group_ids_raw = _ORIG_ALLOWED_GROUP_IDS_RAW
    main._member_labels.clear()
    with burst_filter._labelled_lock:
        burst_filter._labelled_texts.clear()
    shutil.rmtree(tmp_display_names.parent, ignore_errors=True)

    for p in (
        tmp_quota_path,
        tmp_usage.name,
        tmp_model_usage.name,
        tmp_model_usage.name + ".lock",
        tmp_pending.name,
        str(tmp_pending_lock),
        *_sqlite_sidecar_paths(tmp_app_db_path),
    ):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def block_external_side_effects(monkeypatch, request):
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
    if request.path.name != "test_embedding_recall.py":
        import embedding_recall
        monkeypatch.setattr(
            embedding_recall,
            "index_message",
            lambda *a, **kw: None,
            raising=False,
        )

    # 2026-09-27: nothing fetched through safe_fetch leaves the machine — no
    # lookup, and no address counts as public, so even a literal public IP is
    # refused before connecting.  Transport tests re-open their local server.
    import safe_fetch

    def _no_lookup(_host, _port):
        raise safe_fetch.BlockedURL("tests never resolve hostnames")

    monkeypatch.setattr(safe_fetch, "_resolve", _no_lookup)
    monkeypatch.setattr(safe_fetch, "is_public_ip", lambda _ip: False)


_DAILY_MEMORY_ENV_KNOBS = (
    "LINE_BOT_DAILY_MEMORY",
    "LINE_BOT_DAILY_MEMORY_BACKEND",
    "LINE_BOT_DAILY_MEMORY_MODEL",
    "LINE_BOT_DAILY_MEMORY_HEARSAY",
    "LINE_BOT_DAILY_MEMORY_HEARSAY_ONLY",
    "LINE_BOT_DAILY_MEMORY_FALLBACK",
    "LINE_BOT_FACTS_PER_PERSON_MAX",
    "LINE_BOT_INLINE_FACT_EXTRACT",
    "LINE_BOT_FACT_DEDUPE_HIGH",
    "LINE_BOT_FACT_DEDUPE_LOW",
)


@pytest.fixture(autouse=True)
def isolate_daily_memory(monkeypatch, tmp_path_factory):
    """2026-10-11 每日記憶：ledger、job 狀態、隱私詞清單（空檔）、鎖、HMAC 金鑰一律指到
    這個測試自己的暫存目錄——正式 state/fact_ledger.jsonl 不能多出測試的列（回滾會照它刪），
    也不讀本機真實的隱私詞。fact_store 預設不用嵌入模型（要用的測試自己注入假模型，
    不載入 MiniLM）。開關與門檻不吃部署環境（.env 或 job 的 load_dotenv）帶進來的值。"""
    base = tmp_path_factory.mktemp("daily_memory")
    terms = base / "privacy_terms.txt"
    terms.write_text("", encoding="utf-8")
    monkeypatch.setenv("LINE_BOT_FACT_LEDGER_PATH", str(base / "fact_ledger.jsonl"))
    monkeypatch.setenv("LINE_BOT_DAILY_MEMORY_STATE_DIR", str(base / "state"))
    monkeypatch.setenv("LINE_BOT_PRIVACY_TERMS_PATH", str(terms))
    monkeypatch.setenv("LINE_BOT_FACT_LOCK_DIR", str(base / "locks"))
    monkeypatch.setenv("LINE_BOT_FACT_HASH_KEY_PATH", str(base / "fact_hash.key"))
    monkeypatch.setenv("LINE_FAMILY_KINSHIP_PATH", str(base / "no_kinship.json"))
    for name in _DAILY_MEMORY_ENV_KNOBS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config.settings, "facts_per_person_max_raw", "", raising=False)
    monkeypatch.setattr(config.settings, "inline_fact_extract_raw", "", raising=False)
    try:
        import fact_store
    except Exception:  # fact_store 自己壞了：只讓它的測試失敗
        return
    monkeypatch.setattr(fact_store, "loaded_embedder", lambda: None)


@pytest.fixture(autouse=True)
def isolate_parent_health(monkeypatch):
    """2026-10-11：爸媽健康的背景工作（抽取 executor、每日檢查執行緒）不能在測試裡起來；要測抽取就
    直接呼叫 observe_message。模組不存在或換成緊急停用版時也不影響其他測試。"""
    try:
        import parent_health
    except Exception:
        return
    monkeypatch.setattr(parent_health, "_BACKGROUND_ENABLED", False, raising=False)


@pytest.fixture(autouse=True)
def isolate_stock_picks(monkeypatch, tmp_path_factory):
    """2026-10-10：股票推薦的背景執行緒不能在測試裡起來（有測試會跑 _app_lifespan）；
    狀態檔一律指到這個測試自己的暫存目錄、不從磁碟載入，不碰正式 state/；stock_picks 的對外
    請求一律擋掉（需要資料的測試自己換 `_get_json`）；單檔查詢的限流與快取每個測試清掉。"""
    try:
        import stock_picks
    except Exception:  # stock_picks 自己壞了：只讓它的測試失敗，不連帶整套測試在 setup 就失敗
        return

    monkeypatch.setattr(stock_picks, "_BACKGROUND_ENABLED", False, raising=False)
    if not hasattr(stock_picks, "WorkerState"):  # 緊急停用版（stock_picks_disabled.py）沒有這些
        return

    def _no_network(*_args, **_kwargs):
        raise AssertionError("stock_picks must not touch the network in tests")

    monkeypatch.setattr(stock_picks, "_get_json", _no_network)
    monkeypatch.setattr(stock_picks, "STATE_DIR", tmp_path_factory.mktemp("stock_picks_state"))
    monkeypatch.setattr(stock_picks, "_snapshot", None)
    monkeypatch.setattr(stock_picks, "_worker", stock_picks.WorkerState())
    monkeypatch.setattr(stock_picks, "_loaded", True)
    stock_picks.clear_caches()


@pytest.fixture(autouse=True)
def isolate_market_dashboard(monkeypatch, tmp_path_factory):
    """2026-10-17：市場盤勢的快照、旗標、歷史庫、圖目錄一律指到這個測試自己的暫存目錄，不碰正式
    state/ 與 /tmp/line_bot_genimg。對外連線另外由上面的 safe_fetch 守門擋掉（market_http 建在它上面）。"""
    try:
        import market_paths
    except Exception:  # market_paths 自己壞了：只讓它的測試失敗
        return
    root = tmp_path_factory.mktemp("market")
    (root / "state").mkdir(mode=0o700)
    paths = market_paths.Paths.under(root)
    monkeypatch.setattr(market_paths.Paths, "default", classmethod(lambda cls: paths))
