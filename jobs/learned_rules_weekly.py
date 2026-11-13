"""learned_rules_weekly — 咪寶自動學習糾正的每週 Discord 週報（2026-11-12）。

Andrew 11/12：「自動學習糾正部分，結果請定期推播discord」；選每週日晚上 8 點、獨立一則、本機排程（不靠 Claude，
週用量過 80% 也照送），格式由他選定。計畫、審查與決議：ops/state/line_bot_complaint_weekly_20261112/。

launchd（com.andrew.line-bot-learned-rules-weekly）每小時整點、以及登入時叫一次：

  python jobs/learned_rules_weekly.py               到期而且這週還沒排過 → 抄 DB 私人副本、組週報、放進 outbox
  python jobs/learned_rules_weekly.py --dry-run     照算一份印出來（到期的那週，排過了也印；沒到期算最近 7 天、標
                                                    「預覽」）；不拿鎖、不放 outbox、不記失敗
  python jobs/learned_rules_weekly.py --print-plist launchd plist

- 到期：台北時間最近一個「20:00 已經過了」的週日（週日 20:00 前不補上週那份，當晚就有新的；早於 FIRST_SUNDAY
  不送）。期間 [上週日 20:00, 這週日 20:00)，四張表一律「>= 頭、< 尾」。outbox／sent／failed 任一有
  「<週日>-complaint-learning-weekly」開頭的檔就算排過（relay 撞名會加 -<ts>；sent/ 目前沒有東西會清）。
  20:00 後 GRACE_SECONDS 內才跑到的，先等到 20:03 再抄 DB：最後兩分鐘的判斷列要等模型回來才寫進去。
- 讀 DB：把 line_bot.db 連同 -wal、-shm 當一般檔案抄進自己的 0700 暫存資料夾，用一般連線開副本，quick_check 不是
  ok 就隔幾秒重抄一次（同 kb_places_scan）；跑完整個資料夾刪掉。只 SELECT complaint_judgements、
  complaint_shadow_rules、correction_rules、correction_rule_events 的代碼、計數與規則文字；不讀任何含家人原話的表或欄位。
- 週報只有固定標籤、數字和規則文字；規則印之前用 complaint_learning.rule_problem（含名單）再驗一次，沒過或名單讀
  不到就不列文字。不印群組、訊息、發話人的 id；沒見過的結果代碼只在是固定英文代碼時才當例子印出。
- 放檔：outbox/.<名字>.<pid>.tmp 寫完 fsync，再 os.replace 成 outbox/<名字>.md（relay 只撿 *.md）。
- 鎖與狀態：<狀態目錄>/learned_rules_weekly.lock（flock）、learned_rules_weekly.failures.json（連續失敗次數），
  都用 O_NOFOLLOW 開。停用：touch <狀態目錄>/learned_rules_weekly.disabled（不用卸載）。
- 每一輪一開始（停用中也一樣）先清掉自己超過 20 分鐘的 weekly_* 副本（被 SIGKILL 或斷電留下的）；沒到期、
  已排過的那幾輪到這裡就結束，不載入 .env、不抄 DB。不是正式 checkout（沒指定 outbox、或沒有 state/）就 exit 2，
  要看內容請用 --dry-run。
- 失敗：連續第二次失敗才在 outbox 放一則「<今天>-complaint-learning-failed.md」（階段＋例外類別，一天最多一則；
  前綴和週報分開）。log 只寫階段與例外類別。outbox 所在目錄不在就直接 exit 2（放不了短訊，也不抄 DB）。
- 路徑都在呼叫時才算（環境變數 LEARNED_RULES_WEEKLY_DB／_MAINTENANCE_DIR／_STATE_DIR／_COPY_PARENT），conftest
  一律指到不存在的隔離路徑。DB 沒指定時用 bot 目錄的 line_bot.db；.env 的 SQLITE_PATH 指到別的檔就拒跑（記失敗、
  發短訊），不默默改讀。
- 副本放系統暫存目錄（內建碟）：私人、不在 repo 目錄樹裡、不是可拔的外接碟，整點也不碰 WD_BLACK。launchd 不給
  TMPDIR，所以排程跑時是 /private/tmp/line-bot-learned-rules-weekly-<uid>/（0700、檢查擁有者、不跟 symlink）。

import 時不做任何 I/O（只把 bot 目錄放進 sys.path）；不 import memory、main、config，也不 import jobs.learned_rules
（memory import 時會動正式 DB；learned_rules 有寫正式 DB 的指令、import 時就載入 .env）。.env 只在真的要組週報時
才載入（模式與名單路徑要用）；這支程式不開子程序、不連網、不印任何環境變數。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional, Sequence
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import complaint_learning as cl  # noqa: E402  （import 時不讀檔、不連 DB）

LABEL = "com.andrew.line-bot-learned-rules-weekly"
TPE = ZoneInfo("Asia/Taipei")
FIRST_SUNDAY = date(2026, 11, 15)
SEND_HOUR = 20
WEEK_SECONDS = 7 * 86400  # 台北沒有日光節約，一週固定 7×86400 秒
GRACE_SECONDS = 180  # 判斷列在處理開始時記時間、最多約 110 秒後才寫進 DB（審查 GP1 N1）
LAUNCH_TS = int(datetime(2026, 10, 29, 8, 46, 57, tzinfo=TPE).timestamp())  # 試運轉上線（7afa16c）
NAME_SUFFIX = "complaint-learning-weekly"
FAILED_SUFFIX = "complaint-learning-failed"  # 不能以週報前綴開頭（週日當天失敗會被當成週報已排）
MAX_CHARS = 1850  # relay 每段 1,890 字：保證一則訊息
MAX_RULES = 5
RULE_SHOWN_MAX = 60
COPY_RETRY_SLEEP = 3.0
NOTE_AFTER_FAILURES = 2

DB_ENV = "LEARNED_RULES_WEEKLY_DB"
MAINTENANCE_ENV = "LEARNED_RULES_WEEKLY_MAINTENANCE_DIR"
STATE_ENV = "LEARNED_RULES_WEEKLY_STATE_DIR"
COPY_PARENT_ENV = "LEARNED_RULES_WEEKLY_COPY_PARENT"  # 私人暫存資料夾放在哪（預設系統暫存目錄）
DEFAULT_MAINTENANCE = Path("/Users/andrew/Desktop/andrew/Data_engineer/ops/maintenance")
PRODUCTION_BOT_DIR = Path("/Users/andrew/Desktop/andrew/Data_engineer/line_bot_related/line_bot")
DISABLED_NAME = "learned_rules_weekly.disabled"
LOCK_NAME = "learned_rules_weekly.lock"
FAILURES_NAME = "learned_rules_weekly.failures.json"
COPY_ROOT_NAME = "line-bot-learned-rules-weekly"
COPY_PREFIX = "weekly_"
ALARM_SECONDS = 600
STALE_SECONDS = 2 * ALARM_SECONDS  # 正常的一輪最多 ALARM_SECONDS 秒
_SIDECARS = ("-wal", "-shm")
# 沒見過的代碼只在像代碼時才當例子印：英文字開頭、有 _ 或 :、沒有連續 4 個數字（不像訊息或 LINE id；P2 N2）
_CODE_RE = re.compile(r"(?!.*\d{4})(?=.*[_:])[a-z][a-z0-9_:+]{1,59}")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2
_sleep = time.sleep  # 測試換掉
_GUARDS_INSTALLED = False


class JobTimeout(BaseException):
    """alarm 到了（只有 launchd 的那個程序會裝）：BaseException，run() 明確接住，記失敗、刪副本。"""


# ── 白話標籤（代碼來自 complaint_learning、memory.record_learned_rule；沒在表裡的歸「其他」）──────────

CATEGORY_LABELS = {"only_verified": "只說查證過的", "stay_on_topic": "不離題", "clarity": "講清楚"}
NOT_SENT_LABELS = {
    "no_target": "找不到它在講哪一則咪寶回覆",
    "quote_not_bot": "引用的不是咪寶的話",
    "quote_missing": "引用的那則找不到了",
    "operational": "是在回提醒或收據這類操作訊息",
    "names_unavailable": "名單讀不到，寧可不判",
}
LIMIT_LABELS = {
    "already_judged": "同一則回覆已經判過",
    "cap_actor": "同一人當天的判斷次數到上限",
    "cap_group": "群組當天的判斷次數到上限",
}
NO_ANSWER_LABELS = {
    "not_called": "沒叫到判斷器（Claude 暫停：額度或逾時保護；Gemini 不送或不能用）",
    "called_failed": "模型回錯或逾時",
    "judge_parse_error": "回覆看不懂",
}
VERDICT_LABELS = {
    "not_about_bot": "不是在說咪寶",
    "not_complaint": "不是抱怨",
    "reply_kind": "被抱怨的不是聊天回覆",
    "category": "不在能學的類別",
    "low_confidence": "把握不夠",
}
CHECK_LABELS = {
    "check:template": "句型不對",
    "check:core_reason": "原因寫得不對",
    "check:invisible": "有看不見的字",
    "check:charset": "有不該有的字元",
    "check:evidence": "引用的咪寶片段對不上",
    "check:copies_complaint": "照抄家人原話",
    "check:category_anchor": "和類別對不上",
    "check:names_unavailable": "名單讀不到",
    "check:name": "含名字",
    "check:kinship": "含稱謂",
    "check:contact": "含聯絡方式",
    "check:url": "含網址",
    "check:digits": "含數字",
    "check:private": "含私密內容",
    "check:agent_directed": "像在對 AI 下指令",
    "check:loosening": "會放寬規矩",
    "check:agreement": "會變成附和",
    "check:disclosure": "會洩漏內容",
    "check:asserts_fact": "在斷言事實",
    "check:unprompted": "會限制主動建議（你說過不要這種規則）",
    "check:tombstoned": "和撤銷過的規則一樣",
    "check:too_long": "太長",
    "check:ambiguous": "意思不明確",
    "check:format": "格式不對",
}
VERIFY_NO_ANSWER_LABELS = {"verify_unavailable": "驗證時模型不能用", "verify_parse_error": "驗證的回覆看不懂"}
PASSED_LABELS = {
    "shadow_pass": "試運轉：會學到",
    "created": "學到了",
    "revived": "學到了（以前退休的又出現）",
    "merged": "併到已有的規則",
}
NOT_STORED_LABELS = {
    "unsent": "家人收回了訊息",
    "disabled": "開關被關",
    "refused_active": "啟用中的規則已滿",
    "refused_daily": "當天已學過 1 條",
    "tombstoned": "和撤銷過的規則一樣",
    "ambiguous": "對不到唯一一條已有規則",
    "duplicate": "重複",
    "conflict": "和已有規則衝突",
    "store_error": "存檔失敗",
}
MODE_LINES = {
    cl.MODE_SHADOW: "模式：試運轉（只判斷、不寫入，咪寶的回覆不受影響）",
    cl.MODE_ON: "模式：正式（學到的規則會放進咪寶的提示詞）",
    cl.MODE_OFF: "模式：關閉（不判斷、不學）",
}
MODE_WORDS = {cl.MODE_SHADOW: "試運轉", cl.MODE_ON: "正式", cl.MODE_OFF: "關閉"}
DECISION_LINE = "要正式開（學到的規則開始影響回覆），在 Claude Code 說「正式開自動學習糾正」，開之前會先跑一次安全檢查給你看。"


# ── 路徑（呼叫時才算）───────────────────────────────────────────────────────────────


class ConfigMismatch(Exception):
    """.env 的 SQLITE_PATH 指到 bot 目錄的 line_bot.db 以外的檔：不默默改讀（審查 GP2 對 GP1 N11）。"""


def db_path() -> Path:
    explicit = os.environ.get(DB_ENV)
    if explicit:
        return Path(explicit)
    default = BASE / "line_bot.db"
    raw = (os.environ.get("SQLITE_PATH") or "").strip()  # 同 config：前後空白不算
    if raw:
        configured = Path(raw).expanduser()
        configured = configured if configured.is_absolute() else BASE / configured  # 同 config._resolve_sqlite_path
        if configured.resolve() != default.resolve():
            raise ConfigMismatch("sqlite_path")
    return default


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def maintenance_dir() -> Path:
    return Path(os.environ.get(MAINTENANCE_ENV) or DEFAULT_MAINTENANCE)


def state_dir() -> Path:
    return Path(os.environ.get(STATE_ENV) or BASE / "state")


# ── 到期與期間 ─────────────────────────────────────────────────────────────────────


def report_sunday(now: float) -> Optional[date]:
    """台北時間最近一個「20:00 已經過了」的週日；週日 20:00 前回 None（當晚就有新的一份，不補上週，免得一天兩份）；
    早於 FIRST_SUNDAY 回 None。"""
    local = datetime.fromtimestamp(now, TPE)
    back = (local.weekday() + 1) % 7  # 週日 0、週一 1 …
    if back == 0 and local.hour < SEND_HOUR:
        return None
    sunday = local.date() - timedelta(days=back)
    return sunday if sunday >= FIRST_SUNDAY else None


def week_bounds(sunday: date) -> tuple[int, int]:
    """[上週日 20:00, 這週日 20:00)，epoch 秒。"""
    until = int(datetime(sunday.year, sunday.month, sunday.day, SEND_HOUR, tzinfo=TPE).timestamp())
    return until - WEEK_SECONDS, until


def report_name(sunday: date) -> str:
    return f"{sunday.isoformat()}-{NAME_SUFFIX}"


# ── outbox ──────────────────────────────────────────────────────────────────────────


def queued(maintenance: Path, prefix: str) -> bool:
    """outbox／sent／failed 裡有這個前綴的檔就是已經排過（relay 撞名會加 -<ts>）。"""
    for sub in ("outbox", "sent", "failed"):
        try:
            if any(entry.name.startswith(prefix) for entry in os.scandir(maintenance / sub)):
                return True
        except FileNotFoundError:
            continue
    return False


def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def drop(maintenance: Path, name: str, text: str) -> Path:
    """先在 outbox 寫 .<名字>.<pid>.tmp（relay 只撿 *.md），fsync 後 os.replace 成 <名字>.md。
    maintenance 目錄本身要已經在（不自己建）；outbox 這一層沒有才建。"""
    if not maintenance.is_dir():
        raise FileNotFoundError("maintenance_dir_missing")
    outbox = maintenance / "outbox"
    outbox.mkdir(exist_ok=True)
    target = outbox / f"{name}.md"
    tmp = outbox / f".{name}.{os.getpid()}.tmp"
    try:
        _write_new(tmp, text.rstrip("\n") + "\n")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(outbox)
    return target


def remove_stale_drafts(maintenance: Path) -> None:
    """drop() 寫一半被砍掉留下的 .…-complaint-learning-*.tmp：超過 STALE_SECONDS（20 分鐘）才刪，只刪自己的
    （真的時鐘）。"""
    now = time.time()
    try:
        entries = list(os.scandir(maintenance / "outbox"))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if not (name.startswith(".") and name.endswith(".tmp") and "-complaint-learning-" in name):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and now - info.st_mtime >= STALE_SECONDS:
                os.unlink(entry.path)
        except OSError:
            continue


# ── 失敗計數與短訊 ───────────────────────────────────────────────────────────────────


def _failures_path() -> Path:
    return state_dir() / FAILURES_NAME


def _read_nofollow(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read(4096)


def _record_failure() -> int:
    """連續失敗次數＋1（0600 檔，原子取代）；回新的次數。"""
    path = _failures_path()
    try:
        count = int(json.loads(_read_nofollow(path)).get("consecutive", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        count = 0
    count += 1
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        _write_new(tmp, json.dumps({"consecutive": count}))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return count


def _clear_failures() -> None:
    _failures_path().unlink(missing_ok=True)


def notify_failure(maintenance: Path, stage: str, exc: BaseException, today: date) -> None:
    """一天最多一則短訊：階段與例外類別；細節只留在本機 log。"""
    prefix = f"{today.isoformat()}-{FAILED_SUFFIX}"
    if queued(maintenance, prefix):
        return
    text = (
        "⚠️ 自動學習糾正週報沒送出\n"
        f"階段：{stage}\n"
        f"類別：{type(exc).__name__}\n"
        "每個整點會再試；修好前每天最多通知一次。細節只留在本機 ~/Library/Logs/line_bot_learned_rules_weekly_stderr.log"
    )
    drop(maintenance, prefix, text)


# ── DB 私人副本（同 kb_places_scan）──────────────────────────────────────────────────


class CopyNotOk(Exception):
    """抄了兩次，quick_check 都不是 ok。"""


def _copy_private(src: Path, dst: Path) -> None:
    # 先開來源再建目的檔（0600、不覆蓋）：來源剛好不見時不留空檔。
    with open(src, "rb") as inp:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(inp, out)


def _copy_db(src: Path, dst: Path) -> None:
    """DB 檔＋同名的 -wal、-shm（有才抄）。只當一般檔案讀，不用 SQLite 開來源。"""
    _copy_private(src, dst)
    for suffix in _SIDECARS:
        try:
            _copy_private(Path(f"{src}{suffix}"), Path(f"{dst}{suffix}"))
        except FileNotFoundError:
            continue  # 沒有，或剛好被 checkpoint 收掉


def _open_copy(src: Path, workdir: Path) -> sqlite3.Connection:
    """抄進 workdir、用一般連線開（WAL 副本沒有能用的 -shm 時 mode=ro 開不起來）；quick_check 不是 ok 就刪掉、
    隔幾秒重抄一次（bot 可能剛好在 checkpoint），兩次都不行丟 CopyNotOk。"""
    src = src.resolve()  # 符號連結時 -wal、-shm 在真正的檔旁邊
    dst = workdir / "snapshot.db"
    for attempt in range(2):
        if attempt:
            _sleep(COPY_RETRY_SLEEP)
        _copy_db(src, dst)
        conn = sqlite3.connect(str(dst))
        ok = False
        try:
            ok = conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        except sqlite3.Error:
            pass  # 抄到一半的檔可能直接報 malformed
        finally:
            if not ok:
                conn.close()
        if ok:
            return conn
        for leftover in workdir.iterdir():
            leftover.unlink()
    raise CopyNotOk


def copy_root() -> Path:
    """<上層>/line-bot-learned-rules-weekly-<uid>：0700、自己的、不是 symlink（同 private_tmp.private_tmp_dir 的
    檢查；private_tmp 進 main 後和 kb_places_scan 一起換掉）。上層預設系統暫存目錄，測試指到自己的暫存目錄。"""
    parent = os.environ.get(COPY_PARENT_ENV) or tempfile.gettempdir()
    root = Path(parent) / f"{COPY_ROOT_NAME}-{os.getuid()}"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"{COPY_ROOT_NAME}: temp folder failed ownership validation")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(root, 0o700, follow_symlinks=False)
    return root


def _existing_copy_root() -> Optional[Path]:
    """已經存在而且合格（自己的目錄、不是 symlink）的私人資料夾；沒有就回 None（不為了清理而建）。"""
    parent = os.environ.get(COPY_PARENT_ENV) or tempfile.gettempdir()
    root = Path(parent) / f"{COPY_ROOT_NAME}-{os.getuid()}"
    try:
        info = root.lstat()
    except OSError:
        return None
    return root if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() else None


def remove_stale_copies() -> None:
    """每一輪一開始呼叫（審查 GP2 G2）：被 SIGKILL 或斷電留下的 weekly_* 副本，不等到下一次真的抄 DB 才清。"""
    root = _existing_copy_root()
    if root is not None:
        _remove_stale_copies(root)


def _remove_stale_copies(root: Path) -> None:
    """只清自己的、超過 STALE_SECONDS 的 weekly_* 資料夾（真的時鐘，不用注入的時間）。"""
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(COPY_PREFIX):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and now - info.st_mtime >= STALE_SECONDS:
            shutil.rmtree(entry.path, ignore_errors=True)


@contextlib.contextmanager
def private_snapshot(src: Path) -> Iterator[sqlite3.Connection]:
    """抄一份私人副本來讀；不論成敗（含逾時），先關連線再刪掉整個暫存資料夾。"""
    if not src.is_file():
        raise FileNotFoundError("db_missing")
    root = copy_root()
    _remove_stale_copies(root)
    with tempfile.TemporaryDirectory(prefix=COPY_PREFIX, dir=root) as workdir:  # mkdtemp：0700
        conn = _open_copy(src, Path(workdir))
        try:
            yield conn
        finally:
            conn.close()


@contextlib.contextmanager
def job_lock() -> Iterator[bool]:
    """狀態目錄裡的 flock（非阻塞）：排程和手動同時跑時，只有一個會排週報。"""
    fd = os.open(state_dir() / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


# ── 數字（只 SELECT 四張表的代碼、計數與規則文字）──────────────────────────────────────


def _table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _column(conn, table: str, column: str) -> bool:
    return _table(conn, table) and any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _judgements(conn, since: int, until: int) -> list[tuple[str, str, int, str]]:
    return [
        (str(stage or ""), str(result or ""), int(judged or 0), str(provider or ""))
        for stage, result, judged, provider in conn.execute(
            "SELECT stage, result, judged, provider FROM complaint_judgements WHERE created_at >= ? AND created_at < ?",
            (since, until),
        ).fetchall()
    ]


def _shadow_rules(conn, since: int, until: int) -> list[tuple[str, str, Optional[int], bool]]:
    if not _table(conn, "complaint_shadow_rules"):
        return []
    return [
        (str(category or ""), str(rule or ""), None, True)
        for category, rule in conn.execute(
            "SELECT category, rule FROM complaint_shadow_rules WHERE created_at >= ? AND created_at < ? "
            "ORDER BY created_at, rowid",
            (since, until),
        ).fetchall()
    ]


def _learned_events(conn, since: int, until: int) -> tuple[list[tuple[str, str, Optional[int], bool]], Counter]:
    """學到的規則（origin='learned'）期間內新學到的（create／revive）和各種事件的次數。只用 correction_rules 與
    correction_rule_events（不 join correction_observations：舊列有家人原話）。"""
    if not (_column(conn, "correction_rules", "origin") and _table(conn, "correction_rule_events")):
        return [], Counter()
    new = [
        # 已停用的（家人收回訊息、撤銷、退休）不列文字：試運轉收回時連 shadow 列都刪掉，兩邊一致（P2-I1）
        (str(category or ""), str(text or ""), int(rule_id), status == "active")
        for rule_id, text, category, status in conn.execute(
            "SELECT r.rule_id, r.canonical_rule, CASE WHEN json_valid(e.payload_json) "
            "THEN json_extract(e.payload_json, '$.category') END, r.status FROM correction_rule_events e "
            "JOIN correction_rules r ON r.group_id=e.group_id AND r.rule_id=e.rule_id "
            "WHERE r.origin='learned' AND e.action IN ('create','revive') AND e.created_at >= ? AND e.created_at < ? "
            "ORDER BY e.event_id",
            (since, until),
        ).fetchall()
    ]
    counts = Counter(
        str(action)
        for (action,) in conn.execute(
            "SELECT e.action FROM correction_rule_events e LEFT JOIN correction_rules r "
            "ON r.group_id=e.group_id AND r.rule_id=e.rule_id "
            "WHERE e.created_at >= ? AND e.created_at < ? AND (r.origin='learned' OR e.observation_id LIKE 'judge:%' "
            "OR e.observation_id LIKE 'directive:%')",
            (since, until),
        ).fetchall()
    )
    return new, counts


def _active_learned(conn, now: int) -> int:
    """學到的規則現在會進提示詞的條數（同 memory 放進提示詞的條件：啟用、TTL 內；每群最多 LEARNED_PROMPT_CAP），
    取各群最多的那群。"""
    if not _column(conn, "correction_rules", "origin"):
        return 0
    rows = conn.execute(
        "SELECT COUNT(*) FROM correction_rules WHERE origin='learned' AND status='active' AND occurrence_count>0 "
        "AND last_seen_at >= ? GROUP BY group_id",
        (int(now) - int(cl.LEARNED_TTL_SECONDS),),
    ).fetchall()
    return min(max((int(r[0]) for r in rows), default=0), int(cl.LEARNED_PROMPT_CAP))


def _legacy(conn, since: int, until: int) -> Optional[tuple[int, int, int]]:
    """舊的糾正記憶（origin=''；還沒 migration 的舊 DB 全部算舊的）：(啟用中, 停用, 期間內新收)。沒有表回 None。"""
    if not _table(conn, "correction_rules"):
        return None
    where = "WHERE origin=''" if _column(conn, "correction_rules", "origin") else ""
    active, total, new = conn.execute(
        "SELECT SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), COUNT(*), "
        "SUM(CASE WHEN first_seen_at >= ? AND first_seen_at < ? THEN 1 ELSE 0 END) "
        f"FROM correction_rules {where}",
        (since, until),
    ).fetchone()
    active, total, new = int(active or 0), int(total or 0), int(new or 0)
    return active, total - active, new


# 每一列（stage, result, judged）只落進一格：抓到 = 沒送去判斷 + 重複或上限 + 判斷器沒給結果 + 判斷器看過；
# 判斷器看過 = 底下各格的和（審查 GP1 I1、I2、N9）。「則」算的是判斷列：同一則回覆 25 秒內的幾則留言會併成一列。
TOP_GROUPS = ("not_sent", "limited", "no_answer", "seen")
KNOWN_STAGES = ("target", "names", "dedupe", "cap", "judge", "verdict", "check", "verify", "pass", "shadow", "store")
KNOWN_SEEN_STAGES = ("verdict", "check", "verify", "pass", "shadow", "store")
SEEN_GROUPS = ("verdict", "check", "verify_failed", "verify_no_answer", "passed", "not_stored", "other")


def classify(stage: str, result: str, judged: int) -> tuple[str, str, str]:
    """回 (大格, 小格, 標籤代碼)。小格只在大格是 seen 時有意義。"""
    if stage in ("target", "names"):
        return "not_sent", "", result
    if stage in ("dedupe", "cap"):
        return "limited", "", result
    if stage == "judge":
        if result.startswith("judge_unavailable"):
            return "no_answer", "", "called_failed" if judged else "not_called"
        return "no_answer", "", result
    if result == "unsent" or (result in NOT_STORED_LABELS and stage in ("store", "shadow")):
        return "seen", "not_stored", result
    if result in PASSED_LABELS:
        return "seen", "passed", result
    if stage == "verdict":
        return "seen", "verdict", result
    if stage == "check":
        return "seen", "check", result
    if stage == "verify":
        return "seen", "verify_failed" if result == "verifier_rejected" else "verify_no_answer", result
    if stage not in KNOWN_SEEN_STAGES and not judged:  # 沒見過的關卡又沒叫到模型：不能算「判斷器看過」（P1 N-4）
        return "no_answer", "", result
    return "seen", "other", result


@dataclass(frozen=True)
class Week:
    """一段期間的數字（只有代碼、計數與規則文字）。"""

    total: int
    groups: dict  # 大格 → Counter(標籤代碼)
    seen: dict  # 小格 → Counter(標籤代碼)
    providers: Counter
    shadow: list
    learned: list
    events: Counter
    active_learned: int
    cum_label: str
    cum_total: int
    cum_seen: int
    cum_shadow: int
    cum_learned: int
    legacy: Optional[tuple[int, int, int]]


def gather(conn: sqlite3.Connection, *, since: int, until: int, now: int) -> Week:
    rows = _judgements(conn, since, until)
    groups: dict[str, Counter] = {g: Counter() for g in TOP_GROUPS}
    seen: dict[str, Counter] = {g: Counter() for g in SEEN_GROUPS}
    providers: Counter = Counter()
    for stage, result, judged, provider in rows:
        top, sub, code = classify(stage, result, judged)
        groups[top][code] += 1
        if top == "seen":
            seen[sub][code] += 1
            if provider:
                providers[provider] += 1
    learned, events = _learned_events(conn, since, until)
    cum_since = max(LAUNCH_TS, until - int(cl.RECORD_RETENTION_SECONDS))
    cum_label = (
        f"上線以來（{datetime.fromtimestamp(LAUNCH_TS, TPE):%m/%d} 起）" if cum_since == LAUNCH_TS else "近 180 天"
    )
    cum_rows = _judgements(conn, cum_since, until)
    return Week(
        total=len(rows),
        groups=groups,
        seen=seen,
        providers=providers,
        shadow=_shadow_rules(conn, since, until),
        learned=learned,
        events=events,
        active_learned=_active_learned(conn, now),
        cum_label=cum_label,
        cum_total=len(cum_rows),
        cum_seen=sum(1 for stage, result, judged, _p in cum_rows if classify(stage, result, judged)[0] == "seen"),
        cum_shadow=len(_shadow_rules(conn, cum_since, until)),
        cum_learned=len(_learned_events(conn, cum_since, until)[0]),
        legacy=_legacy(conn, since, until),
    )


# ── 組週報 ────────────────────────────────────────────────────────────────────────────


def _one_line(text, limit: int = RULE_SHOWN_MAX) -> str:
    one = " ".join(str(text or "").split())
    one = "".join(ch for ch in one if unicodedata.category(ch)[0] != "C")
    return one[:limit]


def _labelled(counts: Counter, labels: dict[str, str], top: int = 5) -> str:
    """「找不到它在講哪一則咪寶回覆 1、其他 2、沒見過的代碼（例如 xyz） 1」：表裡有的照次數多到少最多列 top 種，
    其餘併成「其他」；表裡沒有的另外算，最常見的那個是固定英文代碼時才當例子印（DB 是資料，不照印）。"""
    known = sorted(((c, n) for c, n in counts.items() if c in labels), key=lambda kv: (-kv[1], kv[0]))
    unknown = sorted(((c, n) for c, n in counts.items() if c not in labels), key=lambda kv: (-kv[1], kv[0]))
    parts = [f"{labels[code]} {n}" for code, n in known[:top]]
    known_rest = sum(n for _c, n in known[top:])
    if known_rest:
        parts.append(f"其他 {known_rest}")
    if unknown:
        example = f"（例如 {unknown[0][0]}）" if _CODE_RE.fullmatch(unknown[0][0]) else ""
        parts.append(f"沒見過的代碼{example} {sum(n for _c, n in unknown)}")
    return "、".join(parts)


def _rule_lines(rules: list, names, limit: int) -> list[str]:
    """names 是 None 或空的（名單讀不到；正式的名單不會是空的）就整段不列文字，也不呼叫 rule_problem
    （它要逐一比對名單；P2 N1）。已停用的規則只列編號。"""
    if not rules:
        return []
    if not names:
        return ["　（名單讀不到，這次不列規則文字）"]
    lines = []
    for index, (category, text, rule_id, show_text) in enumerate(rules[:limit], 1):
        if not show_text:
            shown = "（已停用，不列文字）"
        elif cl.rule_problem(str(text or ""), str(category or ""), names=names):
            shown = "（這條沒通過送出前的檢查，不列文字）"
        else:
            shown = _one_line(text)
        tag = f"#{rule_id} " if rule_id is not None else ""
        lines.append(f"　{index}. {tag}{shown}（{CATEGORY_LABELS.get(category, '其他類別')}）")
    if len(rules) > limit:
        lines.append(f"　…還有 {len(rules) - limit} 條")
    return lines


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TPE).strftime("%m/%d %H:%M")


def _head(since: int, until: int, preview: bool) -> str:
    return f"🧠 咪寶自動學習糾正週報（{'預覽：' if preview else ''}{_fmt(since)}–{_fmt(until)}）"


def _mode_line(mode: str, env_mode: str) -> str:
    line = MODE_LINES.get(mode, MODE_LINES[cl.MODE_OFF])
    if env_mode != mode:
        line += f"；設定是「{MODE_WORDS.get(env_mode, env_mode)}」，被狀態檔暫停或降級了"
    return line


def format_report(week: Week, *, since: int, until: int, mode: str, env_mode: str, names, limit: int,
                  preview: bool = False) -> str:
    groups, seen = week.groups, week.seen
    lines = [_head(since, until, preview), _mode_line(mode, env_mode), "", "這週",
             f"・抓到疑似不滿的留言：{week.total} 則"]
    for key, title, labels in (
        ("not_sent", "沒送去判斷", NOT_SENT_LABELS),
        ("limited", "重複或超過每日上限", LIMIT_LABELS),
        ("no_answer", "判斷器沒給結果", NO_ANSWER_LABELS),
    ):
        if groups[key]:
            lines.append(f"　・{title}：{sum(groups[key].values())} 則（{_labelled(groups[key], labels)}）")
    seen_line = f"　・判斷器看過：{sum(groups['seen'].values())} 則"
    shown = [f"{name} {week.providers[key]}" for key, name in (("claude", "Claude"), ("gemini", "Gemini"))
             if week.providers[key]]
    if shown:
        seen_line += "（" + "、".join(shown) + "）"
    lines.append(seen_line)
    for key, title, labels in (
        ("verdict", "判斷器說不用學", VERDICT_LABELS),
        ("check", "規則沒過程式檢查", CHECK_LABELS),
        ("verify_failed", "第二次驗證不同意", {"verifier_rejected": "驗證器說不行"}),
        ("verify_no_answer", "驗證器沒給結果", VERIFY_NO_ANSWER_LABELS),
        ("passed", "通過", PASSED_LABELS),
        ("not_stored", "通過但沒存", NOT_STORED_LABELS),
        ("other", "其他", {}),
    ):
        if seen[key]:
            lines.append(f"　　・{title}：{sum(seen[key].values())} 則（{_labelled(seen[key], labels)}）")

    # 試運轉的「會學到」和正式的「學到」各自有就列（換模式那週、狀態檔降級時兩種都會有；審查 GP1 I3）
    learned_shown = mode == cl.MODE_ON or bool(week.learned) or bool(week.events)
    if learned_shown:
        lines.append(f"・新學到的規則：{len(week.learned)} 條")
        lines += _rule_lines(week.learned, names, limit)
        refused = week.events.get("refused_active", 0) + week.events.get("refused_daily", 0)
        lines.append(f"・併到已有的規則：{week.events.get('merge', 0)} 次；到上限沒收：{refused} 條")
        lines.append(f"・撤銷：{week.events.get('retire', 0)} 條；60 天沒再出現自動退休：{week.events.get('expire', 0)} 條")
        if mode == cl.MODE_ON:
            lines.append(f"・現在放進提示詞的學到的規則：{week.active_learned}/{cl.LEARNED_PROMPT_CAP} 條")
        else:  # 只有正式模式才把學到的規則放進提示詞（complaint_learning.prompt_rules_enabled；P1 I-1）
            lines.append(f"・現在放進提示詞的學到的規則：0/{cl.LEARNED_PROMPT_CAP} 條（{MODE_WORDS.get(mode, mode)}，"
                         f"學到的規則不進提示詞；恢復正式後啟用中的 {week.active_learned} 條會回來）")
    if not learned_shown or week.shadow:
        lines.append(f"・{'試運轉時' if learned_shown else ''}會學到的規則：{len(week.shadow)} 條")
        lines += _rule_lines(week.shadow, names, limit)

    parts = [f"抓到 {week.cum_total} 則", f"判斷器看過 {week.cum_seen} 則"]
    if mode == cl.MODE_ON or week.cum_learned:
        parts.append(f"學到 {week.cum_learned} 條")
    if mode != cl.MODE_ON or week.cum_shadow:
        parts.append(f"{'試運轉時' if (mode == cl.MODE_ON or week.cum_learned) else ''}會學到 {week.cum_shadow} 條")
    lines += ["", f"{week.cum_label}：" + "、".join(parts)]
    if week.legacy is not None:
        active, retired, new_legacy = week.legacy
        if active:
            lines.append(f"舊的糾正規則：啟用中 {active} 條、停用 {retired} 條，這週新收 {new_legacy} 條")
        elif retired:
            lines.append(f"舊的糾正規則：{retired} 條都停用中，這週新收 {new_legacy} 條")
        else:
            lines.append(f"舊的糾正規則：沒有，這週新收 {new_legacy} 條")
    if mode == cl.MODE_SHADOW and env_mode == cl.MODE_SHADOW:  # .env 已經是正式、只是被降級時不提（GP1 N8）
        lines += ["", DECISION_LINE]
    return "\n".join(lines)


def build_report(conn: sqlite3.Connection, *, since: int, until: int, mode: str, env_mode: str, names,
                 now: Optional[int] = None, preview: bool = False) -> str:
    """週報全文（只讀 conn）。names＝complaint_learning.load_names() 的結果（None＝讀不到，規則文字一律不列）。
    超過 MAX_CHARS 先少列規則，最後才截斷。"""
    if not _table(conn, "complaint_judgements"):
        return "\n".join([_head(since, until, preview), _mode_line(mode, env_mode), "",
                          "這個資料庫沒有自動學習糾正的表（功能可能已回滾），這週沒有數字。"])
    week = gather(conn, since=since, until=until, now=int(until if now is None else now))
    text = ""
    for limit in range(MAX_RULES, -1, -1):
        text = format_report(week, since=since, until=until, mode=mode, env_mode=env_mode, names=names,
                             limit=limit, preview=preview)
        if len(text) <= MAX_CHARS:
            return text
    return text[: MAX_CHARS - 1] + "…"


# ── 一輪 ─────────────────────────────────────────────────────────────────────────────


def current_modes() -> tuple[str, str]:
    """(實際生效的模式, .env 的模式)：和 bot 讀同一個環境變數與狀態檔。"""
    return cl.mode(), cl._env_mode()


def _load_dotenv() -> None:
    """模式（LINE_BOT_COMPLAINT_LEARNING）與名單路徑：和 bot 讀同一份 .env（不覆蓋已經有的環境變數）。
    只在真的要組週報時才載入；測試換成什麼都不做。"""
    from dotenv import load_dotenv

    load_dotenv(BASE / ".env", override=False)


def _report_text(since: int, until: int, now: float, *, preview: bool = False) -> str:
    names = cl.load_names()  # 一輪只讀一次；None＝讀不到（GP1 I6）
    with private_snapshot(db_path()) as conn:
        mode, env_mode = current_modes()
        return build_report(conn, since=since, until=until, mode=mode, env_mode=env_mode, names=names,
                            now=int(now), preview=preview)


def dry_run(now: float, out) -> int:
    """到期的那週（排過了也印）或最近 7 天的預覽；不拿鎖、不放 outbox、不記失敗。"""
    remove_stale_copies()
    _load_dotenv()
    sunday = report_sunday(now)
    if sunday is not None:
        since, until = week_bounds(sunday)
        preview = False
    else:
        until = int(now)
        since, preview = until - WEEK_SECONDS, True
    text = _report_text(since, until, now, preview=preview)
    print(text, file=out)
    print(f"（dry-run：{len(text)} 字，沒有放進 outbox）", file=out)
    return EXIT_OK


def run(now: Optional[float] = None, *, out=None) -> int:
    now = time.time() if now is None else float(now)
    out = out or sys.stdout
    remove_stale_copies()  # 停用中也清（回滾後不留副本；P1 N-1）
    if (state_dir() / DISABLED_NAME).exists():
        print("learned_rules_weekly: disabled", file=out)
        return EXIT_OK
    sunday = report_sunday(now)
    if sunday is None:
        return EXIT_OK
    if not os.environ.get(MAINTENANCE_ENV) and not _same_dir(BASE, PRODUCTION_BOT_DIR):
        # 別的 checkout（worktree、clone）沒指定 outbox 就跑：會用錯的 DB 排掉正式那週、或發假的失敗短訊（P2 N3）
        print("learned_rules_weekly: not the production checkout; use --dry-run", file=sys.stderr)
        return EXIT_CONFIG
    maintenance = maintenance_dir()
    if not maintenance.is_dir() or not state_dir().is_dir():
        # 放不了週報也放不了短訊，或狀態目錄不在（不是正式 checkout）：不抄 DB，直接報錯（launchd 的 last exit 是 2）
        print("learned_rules_weekly: maintenance or state dir missing", file=sys.stderr)
        return EXIT_CONFIG
    name = report_name(sunday)
    stage = "queued"
    try:  # 從這裡起任何錯都記失敗、發短訊（outbox 讀不了也一樣；P1 N-2）
        if queued(maintenance, name):
            return EXIT_OK
        stage = "lock"
        with job_lock() as got:
            if not got or queued(maintenance, name):
                return EXIT_OK
            stage = "config"
            _load_dotenv()
            db_path()  # SQLITE_PATH 對不上就在這裡停（ConfigMismatch）
            since, until = week_bounds(sunday)
            wait = until + GRACE_SECONDS - now
            if wait > 0:
                stage = "grace"
                _sleep(wait)
            remove_stale_drafts(maintenance)
            stage = "report"
            text = _report_text(since, until, max(now, until + GRACE_SECONDS))
            stage = "drop"
            target = drop(maintenance, name, text)
            _clear_failures()
        print(f"learned_rules_weekly: queued {target.name} ({len(text)} chars)", file=out)
        return EXIT_OK
    except (Exception, JobTimeout) as exc:  # 只記階段與例外類別：訊息可能含路徑或 SQL
        if _GUARDS_INSTALLED:
            signal.alarm(0)
        print(f"learned_rules_weekly: failed stage={stage} error={type(exc).__name__}", file=sys.stderr)
        try:
            count = _record_failure()
        except Exception as count_exc:  # 記不了次數（狀態目錄壞了）也要發短訊，不能每個整點都靜靜失敗（GP2 G6）
            print(f"learned_rules_weekly: failure count not saved error={type(count_exc).__name__}", file=sys.stderr)
            count = NOTE_AFTER_FAILURES
        try:
            if count >= NOTE_AFTER_FAILURES:
                notify_failure(maintenance, stage, exc, datetime.fromtimestamp(now, TPE).date())
        except Exception as note_exc:
            print(f"learned_rules_weekly: failure note not queued error={type(note_exc).__name__}", file=sys.stderr)
        return EXIT_FAILED


# ── launchd ─────────────────────────────────────────────────────────────────────────

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- 咪寶自動學習糾正週報（2026-11-12）：每小時整點、登入時各叫一次；到期（台北週日 20:00 之後）而且這週還沒排過
     才抄 DB 副本、組週報、放進 outbox。停用：touch state/learned_rules_weekly.disabled（不用卸載）。 -->
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{bot_dir}/.venv/bin/python</string>
\t\t<string>-I</string>
\t\t<string>-B</string>
\t\t<string>{bot_dir}/jobs/learned_rules_weekly.py</string>
\t</array>
\t<key>WorkingDirectory</key>
\t<string>{bot_dir}</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>HOME</key>
\t\t<string>{home}</string>
\t\t<key>PATH</key>
\t\t<string>{bot_dir}/.venv/bin:/usr/bin:/bin</string>
\t</dict>
\t<key>StartCalendarInterval</key>
\t<dict>
\t\t<key>Minute</key>
\t\t<integer>0</integer>
\t</dict>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>Nice</key>
\t<integer>10</integer>
\t<key>LowPriorityIO</key>
\t<true/>
\t<key>Umask</key>
\t<integer>63</integer>
\t<key>StandardOutPath</key>
\t<string>{home}/Library/Logs/line_bot_learned_rules_weekly_stdout.log</string>
\t<key>StandardErrorPath</key>
\t<string>{home}/Library/Logs/line_bot_learned_rules_weekly_stderr.log</string>
</dict>
</plist>
"""


def _xml_escape(text: str) -> str:
    # 不 import xml.sax.saxutils：它會拉進 urllib、http、ssl、socket（這支程式不連網；GP 實作後審查 P2 N4）
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_plist(bot_dir: Optional[Path | str] = None, home: Optional[str] = None) -> str:
    escape = _xml_escape
    bot = str(bot_dir) if bot_dir is not None else str(BASE)
    home_dir = home or os.environ.get("HOME") or str(Path.home())
    return PLIST_TEMPLATE.format(label=LABEL, bot_dir=escape(bot.rstrip("/")), home=escape(home_dir.rstrip("/")))


# ── 進入點 ──────────────────────────────────────────────────────────────────────────


def _exit_on_signal(signum, frame):
    # 讓 with 區塊照常收尾：SIGTERM／SIGHUP 時也會刪掉暫存副本
    raise SystemExit(128 + signum)


def _on_alarm(signum, frame):
    raise JobTimeout


def _install_guards() -> None:
    """只有 `__main__`（launchd 的那個程序）呼叫：pytest 是同一個程序，不能換掉它的訊號處理。"""
    global _GUARDS_INSTALLED
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _exit_on_signal)
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(ALARM_SECONDS)
    _GUARDS_INSTALLED = True


def main(argv: Optional[Sequence[str]] = None, *, install_guards: bool = False) -> int:
    parser = argparse.ArgumentParser(prog="learned_rules_weekly", description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="照算一份印出來，不放 outbox")
    action.add_argument("--print-plist", action="store_true", help="印出 launchd plist")
    parser.add_argument("--now", type=float, help="測試用：現在的 epoch 秒（只准和 --dry-run 一起用）")
    args = parser.parse_args(argv)
    if args.print_plist:
        sys.stdout.write(render_plist())
        return EXIT_OK
    if args.now is not None and not args.dry_run:
        print("learned_rules_weekly: --now 只准和 --dry-run 一起用", file=sys.stderr)
        return EXIT_CONFIG
    if install_guards:
        _install_guards()
    now = time.time() if args.now is None else args.now
    if args.dry_run:
        return dry_run(now, sys.stdout)
    return run(now)


if __name__ == "__main__":
    raise SystemExit(main(install_guards=True))
