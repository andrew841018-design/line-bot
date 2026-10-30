"""股票推薦定期回測（計畫 B，2026-10-29）的 launchd job。A 段：封存、三族池子紀錄、價格庫、週報骨架。

Andrew 10/28 要股票推薦「定期回測（週/月/季/年）……每週 discord 推播給我」。A 段先把會消失的輸入每天記下來
（AI 狀態 T＝10/28 那份 10/30 清晨就會被蓋掉），週報先報告收集狀態；冷凍、背景規則在之後的段。
規格：ops/state/line_bot_stock_backtest_20261028/PLAN.md（v5）§2、§3、§4、§9、§10。

launchd（com.andrew.stock-picks-review）每 30 分鐘、以及三個狀態檔一變就叫一次：

  python jobs/stock_picks_review.py                        一輪：封存 → 池子紀錄 → 價格 → 成員報酬（C1）→ 週報
  python jobs/stock_picks_review.py --dry-run              只看不寫：不拿鎖、不建任何檔，review.sqlite 用 immutable 唯讀
                                                           （不讀 -wal：job 正在寫時可能讀到舊資料或讀不到，只供診斷）
  python jobs/stock_picks_review.py --import-archive DIR   一次性匯入手動封存（stock_picks_tw.json.<ts>.gz 等）
  python jobs/stock_picks_review.py --print-plist          launchd plist 內容

規則：
- 停用旗標 state/stock_picks_review.disabled 存在就安靜結束（拿到鎖之後再看一次：部署可能剛放）。
- 建任何檔之前先 umask 077（結束時還原：測試在同一個程序裡）。`state/` 本身不合格（不是自己的、group／other
  有權限、symlink）就不跑，也不建立、不 chmod 它（restart_lock 要它 0700）；自己的目錄 state/stock_picks_review/
  只建最後一層（0700），已經在卻不合格就不跑。
- 鎖：state/stock_picks_review/job.lock 的 flock（非阻塞），拿不到就安靜結束；會寫入的指令都要拿。
- 斷網 audit hook、alarm(240)、faulthandler(250, exit=True) 只在 `__main__` 裝（_install_guards）：pytest 是同一個
  程序，audit hook 裝了拿不掉、faulthandler 會殺掉整個 pytest。逾時丟 JobTimeout（BaseException 的子類），
  各階段只接 Exception，所以會一路到外層，last_run 記 timeout。
- 本 job 的其他模組與 bot 的模組都在各階段的 try 裡才 import：部署中途 import 失敗只算那個階段失敗，照樣寫
  last_run、發得出短訊。封存只用標準庫，不受 bot 程式或 review.sqlite 壞掉影響。
- 只呼叫 bot 的純函式：解析、條件（checks_of、ai_checks_of、us_checks_of）、數值（_facts_json／_facts_for、美股的
  _facts_json）、排序（choose_picks、ai_candidates、choose_us_picks：算 rank）、Trend、stock_db_ro（清單在
  stock_picks_review_pool 的模組說明）；不碰 _ai_universe、_ensure_loaded、_state_file_lock 或任何寫檔函式。
  不 import config、不讀 .env；launchd 只給 HOME、PATH。
- --now（注入時間）只給測試與 staging：會刪過期的價格與封存、會放週報，所以只准和 --dry-run 一起用，或同時明確給
  --bot-dir 和 --maintenance-dir、而且兩個都不是正式路徑；否則回 EXIT_CONFIG、什麼都不做（審查 impl r2#2）。
- last_run.json 只放列舉值（階段結果、例外類別、次數、日期、大小；讀回時未來的時間夾回現在）；log 也一樣，
  不寫例外訊息與 traceback（只記例外類別和出事的函式名、行號）。
- 連續 3 輪失敗：outbox 一天一則短訊（階段＋例外類別）。DbBusy／hot journal 不算失敗。
- outbox 與 bot 目錄都在呼叫時才算（參數或環境變數 STOCK_PICKS_REVIEW_MAINTENANCE_DIR／
  STOCK_PICKS_REVIEW_BOT_DIR），conftest 一律指到暫存目錄。
這支程式 import 時不做任何 I/O（只把 bot 目錄放進 sys.path）。
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import faulthandler
import fcntl
import json
import logging
import math
import numbers
import os
import re
import signal
import stat
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

log = logging.getLogger("stock_picks_review")

LABEL = "com.andrew.stock-picks-review"
BOT_DIR_ENV = "STOCK_PICKS_REVIEW_BOT_DIR"
MAINTENANCE_ENV = "STOCK_PICKS_REVIEW_MAINTENANCE_DIR"
DEFAULT_MAINTENANCE = Path("/Users/andrew/Desktop/andrew/Data_engineer/ops/maintenance")
PRODUCTION_BOT_DIR = Path("/Users/andrew/Desktop/andrew/Data_engineer/line_bot_related/line_bot")  # --now 不准指到這裡
ALARM_S = 240
FAULT_S = 250
NOTE_AFTER_FAILURES = 3
FAILURE_NOTE = "stock-picks-review-failed"  # outbox 檔名：YYYY-MM-DD-stock-picks-review-failed.md（和週報前綴分得開）
LAST_RUN_SCHEMA = 1
LAST_RUN_MAX_BYTES = 256 * 1024
TPE = ZoneInfo("Asia/Taipei")

EXIT_OK = 0
EXIT_FAILED = 1  # 這一輪有階段失敗或逾時（launchctl list 看得到）
EXIT_CONFIG = 2  # state/ 或自己的目錄、鎖檔不安全
STAGE_NAMES = ("archive", "pool", "prices", "returns", "weekly")
FAILED_STATUSES = ("error", "timeout")

_ENUM_RE = re.compile(r"[A-Za-z0-9_.:/\-]{0,64}")
_CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_DROP = object()


class JobTimeout(BaseException):
    """alarm(240) 到了：BaseException，各階段的 except Exception 接不到，一路到外層記 timeout。"""


class NetworkBlocked(RuntimeError):
    """這支 job 不連網：誤呼叫到會連網的函式時由 audit hook 丟出（不會真的去打 Yahoo，不吃到共用的 429 冷卻）。"""


class UnsafePath(OSError):
    """目錄或鎖檔不是自己的、有 group／other 權限、是 symlink。"""


# ── 路徑（一律在呼叫時才算） ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class Paths:
    bot_dir: Path
    maintenance: Path  # outbox／sent／failed 在它底下（discord_relay）

    @property
    def state_dir(self) -> Path:
        return self.bot_dir / "state"

    @property
    def root(self) -> Path:
        return self.state_dir / "stock_picks_review"

    @property
    def disabled(self) -> Path:
        return self.state_dir / "stock_picks_review.disabled"

    @property
    def lock(self) -> Path:
        return self.root / "job.lock"

    @property
    def last_run(self) -> Path:
        return self.root / "last_run.json"

    @property
    def db(self) -> Path:
        return self.root / "review.sqlite"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def spool(self) -> Path:
        return self.root / "spool"

    @property
    def drafts(self) -> Path:
        return self.root / "drafts"  # 週報、短訊先寫在這裡（fsync），再整個搬進 outbox

    @property
    def market_db(self) -> Path:
        return self.state_dir / "market_history.sqlite"

    @property
    def perf_result(self) -> Path:
        return self.state_dir / "stock_picks_perf.json"

    @property
    def outbox(self) -> Path:
        return self.maintenance / "outbox"


def resolve_paths(bot_dir: Optional[Path | str] = None, maintenance_dir: Optional[Path | str] = None) -> Paths:
    """參數 → 環境變數 → 預設（bot 目錄＝這支程式的上一層）。"""
    bot = bot_dir or os.environ.get(BOT_DIR_ENV) or BASE
    maintenance = maintenance_dir or os.environ.get(MAINTENANCE_ENV) or DEFAULT_MAINTENANCE
    return Paths(bot_dir=Path(bot), maintenance=Path(maintenance))


# ── 私有檔案工具 ────────────────────────────────────────────────────────────────
def _private(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and (info.st_mode & 0o077) == 0


def state_dir_ok(path: Path) -> bool:
    """bot 的 state/：要已經存在、是目錄不是 symlink、是自己的、沒有 group／other 權限。只看不改。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and _private(info)


def ensure_private_dir(path: Path) -> None:
    """不存在就建最後一層（0700，不建上層）；已經在就要是自己的、沒有 group／other 權限的真目錄。不 chmod。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
        info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or not _private(info):
        raise UnsafePath(errno.EPERM, "unsafe directory")


ROOT_TMP_PREFIX = ".stock_picks_review.tmp-"  # 不會被 bot 清暫存檔的 .stock_picks_tw*.tmp 撿到


def prepare_root(paths: Paths) -> None:
    """state/stock_picks_review/（0700）；第一次建它的時候順便建 spool/（B1 的 hook 只寫進已經存在、自己的 0700 目錄）。
    之後不再建 spool/：回滾時把它移走就停掉 hook，job 不會建回來（PLAN §5.2）。
    第一次是在 state/ 裡先建好暫存目錄（含 spool/），再整個改名成 stock_picks_review：不會留下「root 建好、spool 沒建」
    的半成品（那樣 hook 會無聲停用，審查 impl r1#8）。另一個程序先建好了 root 就用它的。"""
    if not os.path.lexists(paths.root):
        tmp = Path(tempfile.mkdtemp(prefix=ROOT_TMP_PREFIX, dir=paths.state_dir))  # 0700
        try:
            os.mkdir(tmp / paths.spool.name, 0o700)
            os.rename(tmp, paths.root)
        except OSError:
            if not os.path.lexists(paths.root):  # 不是被別人搶先建好：照實丟出去
                raise
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(tmp / paths.spool.name)
            with contextlib.suppress(OSError):
                os.rmdir(tmp)
    ensure_private_dir(paths.root)


def write_atomic(path: Path, data: bytes) -> None:
    """同目錄暫存檔（mkstemp：O_EXCL、0600、.tmp- 開頭）→ fsync → os.replace → fsync 目錄。"""
    directory = path.parent
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def flag_set(path: Path) -> bool:
    """停用旗標：存在就是停用；「不存在」以外的錯誤（權限、I/O）也當成停用。"""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise UnsafePath(errno.EPERM, "unsafe lock file")
    return fd


# ── outbox（discord_relay 撿 outbox/*.md、送完搬到 sent/、放棄搬到 failed/，撞名會加 -<ts>） ────────────
def queued(prefix: str, maintenance: Path) -> bool:
    """outbox／sent／failed 裡有這個前綴的檔就是已經排過（同 token_weekly_report._queued）。"""
    for sub in ("outbox", "sent", "failed"):
        directory = maintenance / sub
        try:
            names = os.listdir(directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if any(name.startswith(prefix) for name in names):
            return True
    return False


def drop(maintenance: Path, name: str, text: str, drafts: Path) -> Path:
    """先在自己的 drafts/ 寫完（fsync），再整個搬進 outbox，relay 不會撿到寫一半的檔（同 token_weekly_report._drop；
    drafts 和 outbox 都在內建碟）。"""
    ensure_private_dir(drafts)
    outbox = maintenance / "outbox"
    outbox.mkdir(exist_ok=True)  # 只建 outbox 這一層：maintenance 目錄本身要已經在
    target = outbox / name
    tmp = drafts / f"{name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return target


# ── 執行環境 ────────────────────────────────────────────────────────────────────
@dataclass
class Context:
    """一輪的環境：各階段只透過它拿路徑、時間、DB 與檔案工具（其他模組不 import 這支程式：它在 launchd 下是
    __main__，再 import 一次會變成另一份模組、另一個 JobTimeout 類別）。"""

    paths: Paths
    now: float
    dry_run: bool = False
    inputs: Optional[dict] = None
    _conn: Any = field(default=None, repr=False)
    _conn_error: Optional[Exception] = field(default=None, repr=False)

    @property
    def today(self) -> date:
        return datetime.fromtimestamp(self.now, TPE).date()

    def db(self):
        """review.sqlite（第一次用到才開；開不了的例外記住，之後的階段直接丟同一個）。dry-run 沒有庫回 None。"""
        if self._conn_error is not None:
            raise self._conn_error
        if self._conn is None:
            from jobs import stock_picks_review_db as rdb

            try:
                if self.dry_run:
                    self._conn = rdb.open_readonly(self.paths.db)
                    if self._conn is None:
                        return None
                else:
                    self._conn = rdb.open_db(self.paths.db)
            except Exception as exc:
                self._conn_error = exc
                raise
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    def ensure_dir(self, path: Path) -> None:
        if self.dry_run:
            raise RuntimeError("dry-run must not create directories")
        ensure_private_dir(path)

    def write_atomic(self, path: Path, data: bytes) -> None:
        if self.dry_run:
            raise RuntimeError("dry-run must not write files")
        write_atomic(path, data)

    def queued(self, prefix: str) -> bool:
        return queued(prefix, self.paths.maintenance)

    def drop(self, name: str, text: str) -> Path:
        if self.dry_run:
            raise RuntimeError("dry-run must not queue messages")
        return drop(self.paths.maintenance, name, text, self.paths.drafts)


def taipei_day(now: float) -> date:
    return datetime.fromtimestamp(now, TPE).date()


# ── 防護（只在 __main__） ───────────────────────────────────────────────────────
_BLOCKED_EVENTS = frozenset({
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
    "socket.sendto", "socket.sendmsg",
})
_GUARDS_INSTALLED = False


def _audit(event: str, _args) -> None:
    if event in _BLOCKED_EVENTS:
        raise NetworkBlocked(event)


def _on_alarm(_signum, _frame) -> None:
    raise JobTimeout()


def _install_guards(*, alarm_s: int = ALARM_S, fault_s: float = FAULT_S) -> None:
    """斷網 audit hook＋alarm＋faulthandler。只有 `__main__`（launchd 的那個程序）呼叫：audit hook 裝了就拿不掉，
    在 pytest 裡呼叫會擋到之後所有用真 socket 的測試；faulthandler 時間到會結束整個程序。"""
    global _GUARDS_INSTALLED
    sys.addaudithook(_audit)
    signal.signal(signal.SIGALRM, _on_alarm)
    if alarm_s:
        signal.alarm(int(alarm_s))
    if fault_s:
        faulthandler.dump_traceback_later(fault_s, exit=True)
    _GUARDS_INSTALLED = True


def _disarm_alarm() -> None:
    """寫 last_run 之前先關掉 alarm（不讓它打斷收尾）；faulthandler 的 250 秒照樣在（真的卡住還是會結束）。"""
    if _GUARDS_INSTALLED:
        signal.alarm(0)


def _cancel_guards() -> None:
    if _GUARDS_INSTALLED:
        signal.alarm(0)
        faulthandler.cancel_dump_traceback_later()


# ── last_run.json（只放列舉值） ─────────────────────────────────────────────────
def sanitize(value: Any, _depth: int = 0) -> Any:
    """只留數字、布林、None 與 [A-Za-z0-9_.:/-]{0,64} 的字串（種類、日期、代號）；其他整個丟掉。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        number = int(value)
        return number if abs(number) < 2**53 else _DROP
    if isinstance(value, numbers.Real):
        number = float(value)
        return round(number, 3) if math.isfinite(number) else _DROP
    if isinstance(value, str):
        return value if _ENUM_RE.fullmatch(value) else _DROP
    if _depth >= 6:
        return _DROP
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:200]:
            if isinstance(key, str) and key and _ENUM_RE.fullmatch(key):
                cleaned = sanitize(item, _depth + 1)
                if cleaned is not _DROP:
                    out[key] = cleaned
        return out
    if isinstance(value, (list, tuple)):
        items = (sanitize(item, _depth + 1) for item in list(value)[:100])
        return [item for item in items if item is not _DROP]
    return _DROP


def _clean(value: Any) -> Any:
    result = sanitize(value)
    return None if result is _DROP else result


def _clamp(value, now: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return min(float(value), now)


def read_last_run(path: Path, now: float) -> dict:
    """上一輪的 last_run；讀不到是空的。時間欄位比現在晚的夾回現在（時鐘倒退不能讓之後的判斷一直卡住）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > LAST_RUN_MAX_BYTES:
            return {}
        data = os.read(fd, LAST_RUN_MAX_BYTES + 1)
    except OSError:
        return {}
    finally:
        os.close(fd)
    try:
        obj = json.loads(data)
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    for key in ("started_at", "finished_at", "last_success_at"):
        obj[key] = _clamp(obj.get(key), now)
    failures = obj.get("consecutive_failures")
    valid = isinstance(failures, int) and not isinstance(failures, bool) and failures >= 0
    obj["consecutive_failures"] = failures if valid else 0
    return obj


def _where(exc: BaseException) -> Optional[str]:
    """例外最後發生在哪個函式（模組:函式:行號）；只有程式位置，沒有資料。"""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    last = frames[-1]
    return f"{Path(last.filename).stem}:{last.name}:{last.lineno}"


def _sizes(paths: Paths) -> dict:
    out: dict[str, Any] = {}
    try:
        from jobs import stock_picks_review_db as rdb
        from jobs import stock_picks_review_pool as pool

        out["sqlite_bytes"] = rdb.db_sizes(paths.db)
        out["archive_bytes"], out["archive_files"] = pool.archive_sizes(paths.archive)
    except Exception as exc:  # noqa: BLE001 - 大小量不出來不能讓 last_run 寫不出來
        out["error"] = type(exc).__name__
    return out


SPOOL_INVALID_WINDOW_S = 7 * 86400.0


def _spool_invalid_recent(paths: Paths) -> Optional[int]:
    """近 7 天被移到 spool/done/*.invalid 的檔數（只數檔名與時間，不讀內容）。壞 spool 只讓看到它的那一輪失敗、
    之後就移走了；hook 的格式漂移時是零星的單輪失敗、湊不到連續 3 輪，所以另外把這個數字放進 last_run 給每日維護看。
    讀不了回 None。"""
    done = paths.spool / "done"
    cutoff = time.time() - SPOOL_INVALID_WINDOW_S  # 檔案年齡看真實時間
    try:
        with os.scandir(done) as entries:
            return sum(1 for e in entries if e.name.endswith(".invalid")
                       and e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_mtime >= cutoff)
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def _first_failure(stages: dict) -> tuple[str, str]:
    for name, result in stages.items():
        status = result.get("status") if isinstance(result, dict) else None
        if status in FAILED_STATUSES:
            klass = result.get("error") if status == "error" else "JobTimeout"
            return name, klass if isinstance(klass, str) and _CLASS_RE.fullmatch(klass) else "Unknown"
    return "unknown", "Unknown"


def _sweep_drafts(drafts: Path, now: float, max_age_s: float = 3600.0) -> None:
    """drop() 寫完、還沒搬進 outbox 就當機留下的暫存檔：超過 1 小時就刪（只刪 drafts/ 裡的 *.tmp）。"""
    try:
        entries = list(os.scandir(drafts))
    except (FileNotFoundError, NotADirectoryError):
        return
    for entry in entries:
        try:
            if entry.name.endswith(".tmp") and entry.is_file(follow_symlinks=False) \
                    and now - entry.stat(follow_symlinks=False).st_mtime > max_age_s:
                os.unlink(entry.path)
        except FileNotFoundError:
            continue


def notify_failure(paths: Paths, *, stage: str, error: str, failures: int, now: float) -> str:
    """一天一則短訊：階段與例外類別；細節只留在本機 log（同 token_weekly_report.notify_failure）。"""
    prefix = f"{taipei_day(now).isoformat()}-{FAILURE_NOTE}"
    if queued(prefix, paths.maintenance):
        return "already"
    stage = stage if stage in STAGE_NAMES else "unknown"
    error = error if _CLASS_RE.fullmatch(error) else "Unknown"
    text = (f"⚠️ 股票推薦回測 job 連續 {failures} 輪失敗\n階段：{stage}\n類別：{error}\n"
            "細節只留在本機 ~/Library/Logs/stock_picks_review_stderr.log")
    drop(paths.maintenance, f"{prefix}.md", text, paths.drafts)
    return "queued"


# ── 一輪 ───────────────────────────────────────────────────────────────────────
def _stage_archive(ctx: Context) -> dict:
    from jobs import stock_picks_review_pool as pool

    return pool.archive_stage(ctx)


def _stage_pool(ctx: Context) -> dict:
    from jobs import stock_picks_review_pool as pool

    return pool.pool_stage(ctx)


def _stage_prices(ctx: Context) -> dict:
    from jobs import stock_picks_review_db as rdb

    return rdb.prices_stage(ctx)


def _stage_returns(ctx: Context) -> dict:
    from jobs import stock_picks_review_returns as returns

    return returns.returns_stage(ctx)


def _stage_weekly(ctx: Context) -> dict:
    from jobs import stock_picks_review_weekly as weekly

    return weekly.weekly_stage(ctx)


Stage = tuple[str, Callable[[Context], dict]]
DEFAULT_STAGES: tuple[Stage, ...] = (
    ("archive", _stage_archive), ("pool", _stage_pool), ("prices", _stage_prices), ("returns", _stage_returns),
    ("weekly", _stage_weekly),
)


def run_stages(ctx: Context, stages: Sequence[Stage], results: Optional[dict] = None) -> tuple[dict, bool]:
    """各階段各自 try（只接 Exception）；JobTimeout 一路到這裡，記在當下的階段。回 (各階段結果, 是否逾時)。
    results 可以由呼叫端給（逾時落在兩個階段之間時，已經跑完的結果照樣留著）。"""
    results = {} if results is None else results
    current = None
    try:
        for name, fn in stages:
            current = name
            started = time.monotonic()
            try:
                result = fn(ctx)
                result = dict(result) if isinstance(result, dict) else {"status": "ok"}
            except Exception as exc:  # noqa: BLE001 - 一個階段壞掉不能擋住其他階段（封存一定要先做完）
                result = {"status": "error", "error": type(exc).__name__, "where": _where(exc)}
                kind = getattr(exc, "kind", None)
                if isinstance(kind, str):
                    result["kind"] = kind
                log.error("stock picks review stage=%s status=error error_type=%s where=%s",
                          name, type(exc).__name__, result["where"])
            result["duration_s"] = round(time.monotonic() - started, 3)
            results[name] = result
    except JobTimeout:
        results[current or "unknown"] = {"status": "timeout"}
        log.error("stock picks review stage=%s status=timeout", current)
        return results, True
    return results, False


def run_locked(paths: Paths, *, now: float, stages: Sequence[Stage] = DEFAULT_STAGES) -> dict:
    """拿著鎖跑一輪，寫 last_run，需要時發短訊。回 last_run 的內容。"""
    previous = read_last_run(paths.last_run, now)
    _sweep_drafts(paths.drafts, time.time())  # 檔案年齡看真實時間
    ctx = Context(paths=paths, now=now)
    results: dict = {}
    timed_out = False
    try:
        _, timed_out = run_stages(ctx, stages, results)
    except JobTimeout:  # 落在 run_stages 自己的 try 外面（理論上只有最後一行）：照樣記成逾時
        timed_out = True
    finally:
        _disarm_alarm()
        ctx.close()
    failed = timed_out or any(r.get("status") in FAILED_STATUSES for r in results.values())
    failures = previous.get("consecutive_failures", 0) + 1 if failed else 0
    pool_result = results.get("pool") or {}
    out: dict[str, Any] = {
        "schema": LAST_RUN_SCHEMA,
        "status": "timeout" if timed_out else "error" if failed else "ok",
        "started_at": now,
        "stages": results,
        "consecutive_failures": failures,
        "last_success_at": previous.get("last_success_at") if failed else now,
        "last_t": pool_result.get("last_t") if isinstance(pool_result.get("last_t"), dict) else previous.get("last_t"),
        "sizes": _sizes(paths),
        "spool_invalid_7d": _spool_invalid_recent(paths),
    }
    if failed and failures >= NOTE_AFTER_FAILURES:
        stage, error = _first_failure(results)
        try:
            out["note"] = notify_failure(paths, stage=stage, error=error, failures=failures, now=now)
        except Exception as exc:  # noqa: BLE001 - 短訊發不出去：只記類別
            out["note"] = "error:" + type(exc).__name__
    out["finished_at"] = time.time()
    try:
        data = json.dumps(_clean(out), ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        write_atomic(paths.last_run, data)
    except Exception as exc:  # noqa: BLE001
        log.error("stock picks review last_run not written error_type=%s", type(exc).__name__)
    log.info("stock picks review status=%s failures=%d %s", out["status"], failures,
             " ".join(f"{name}={(r or {}).get('status')}" for name, r in results.items()))
    return out


@contextlib.contextmanager
def _locked(paths: Paths, *, what: str, locked_exit: int, recheck_disabled: bool) -> Iterator[Optional[int]]:
    """會寫入的指令共用的骨架：umask 077 → state/ 合格 → 自己的目錄 → job.lock（flock 非阻塞）。
    yield None＝拿到鎖、可以開始做；yield 結束碼＝不能做（呼叫端直接回這個碼）：
    state/、自己的目錄或鎖檔不安全 → EXIT_CONFIG；有人拿著鎖 → locked_exit；recheck_disabled 時，拿到鎖後停用旗標
    已經在（部署可能剛好在拿鎖前放了旗標）→ EXIT_OK。離開時放開鎖、還原 umask（測試在同一個程序裡）。"""
    old_umask = os.umask(0o077)
    try:
        if not state_dir_ok(paths.state_dir):
            log.error("stock picks review %s skipped reason=state_dir_unsafe", what)
            yield EXIT_CONFIG
            return
        try:
            prepare_root(paths)
            fd = open_lock(paths.lock)
        except OSError:
            log.error("stock picks review %s skipped reason=root_or_lock_unsafe", what)
            yield EXIT_CONFIG
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.info("stock picks review %s skipped reason=locked", what)
                yield locked_exit
                return
            if recheck_disabled and flag_set(paths.disabled):
                log.info("stock picks review %s skipped reason=disabled", what)
                yield EXIT_OK
                return
            yield None
        finally:
            os.close(fd)  # 關掉就放開 flock
    finally:
        os.umask(old_umask)


def run(paths: Paths, *, now: Optional[float] = None, guards: Optional[Callable[[], None]] = None,
        stages: Sequence[Stage] = DEFAULT_STAGES) -> int:
    """一輪（停用旗標 → umask → 自己的目錄 → 鎖 → 再看一次停用旗標 → 防護 → 各階段）。回結束碼。"""
    if flag_set(paths.disabled):
        log.info("stock picks review run skipped reason=disabled")
        return EXIT_OK
    with _locked(paths, what="run", locked_exit=EXIT_OK, recheck_disabled=True) as blocked:
        if blocked is not None:
            return blocked
        if guards is not None:
            guards()
        out = run_locked(paths, now=time.time() if now is None else now, stages=stages)
        return EXIT_FAILED if out["status"] != "ok" else EXIT_OK


def dry_run(paths: Paths, *, now: Optional[float] = None) -> dict:
    """只看不寫：不拿鎖、不建任何目錄或檔、不寫 last_run、不放 outbox。回各階段會做什麼。"""
    ctx = Context(paths=paths, now=time.time() if now is None else now, dry_run=True)
    results: dict = {}
    timed_out = False
    try:
        _, timed_out = run_stages(ctx, DEFAULT_STAGES, results)
    except JobTimeout:
        timed_out = True
    finally:
        _disarm_alarm()
        ctx.close()
    return {"dry_run": True, "timeout": timed_out, "stages": results}


def import_archive(paths: Paths, directory: Path, *, now: Optional[float] = None,
                   guards: Optional[Callable[[], None]] = None) -> int:
    """--import-archive DIR：拿鎖，把手動封存匯入封存與池子紀錄。"""
    from_dir = Path(directory)
    if not from_dir.is_dir():
        log.error("stock picks review import skipped reason=not_a_directory")
        return EXIT_CONFIG
    with _locked(paths, what="import", locked_exit=EXIT_FAILED, recheck_disabled=False) as blocked:
        if blocked is not None:
            return blocked
        if guards is not None:
            guards()
        ctx = Context(paths=paths, now=time.time() if now is None else now)
        try:
            from jobs import stock_picks_review_pool as pool

            result = pool.import_archive_dir(ctx, from_dir)
        finally:
            _disarm_alarm()
            ctx.close()
        sys.stdout.write(json.dumps(_clean(result), ensure_ascii=False, sort_keys=True) + "\n")
        return EXIT_OK


# ── launchd plist ───────────────────────────────────────────────────────────────
PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- 股票推薦定期回測（2026-10-29）：每 30 分鐘、三個推薦狀態檔一變就跑一次（封存、池子紀錄、價格庫、週報）。
     只用 HOME、PATH，不讀 .env。停用：touch state/stock_picks_review.disabled（不用卸載）。 -->
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{bot_dir}/.venv/bin/python</string>
\t\t<string>{bot_dir}/jobs/stock_picks_review.py</string>
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
\t<key>StartInterval</key>
\t<integer>1800</integer>
\t<key>WatchPaths</key>
\t<array>
\t\t<string>{bot_dir}/state/stock_picks_tw.json</string>
\t\t<string>{bot_dir}/state/stock_picks_tw_ai.json</string>
\t\t<string>{bot_dir}/state/stock_picks_us.json</string>
\t</array>
\t<key>ThrottleInterval</key>
\t<integer>60</integer>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>Nice</key>
\t<integer>10</integer>
\t<key>LowPriorityIO</key>
\t<true/>
\t<key>Umask</key>
\t<integer>63</integer>
\t<key>StandardOutPath</key>
\t<string>{home}/Library/Logs/stock_picks_review_stdout.log</string>
\t<key>StandardErrorPath</key>
\t<string>{home}/Library/Logs/stock_picks_review_stderr.log</string>
</dict>
</plist>
"""


def render_plist(bot_dir: Optional[Path | str] = None, home: Optional[str] = None) -> str:
    bot = str(bot_dir) if bot_dir is not None else str(BASE)
    home_dir = home or os.environ.get("HOME") or str(Path.home())
    return PLIST_TEMPLATE.format(label=LABEL, bot_dir=escape(bot.rstrip("/")), home=escape(home_dir.rstrip("/")))


# ── 進入點 ──────────────────────────────────────────────────────────────────────
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="stock_picks_review", description="股票推薦定期回測 job")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true",
                        help="只看不寫（review.sqlite 用 immutable 唯讀、不讀 -wal：job 正在寫時可能讀到舊資料或讀不到，只供診斷）")
    action.add_argument("--import-archive", metavar="DIR", type=Path, help="一次性匯入手動封存")
    action.add_argument("--print-plist", action="store_true", help="印出 launchd plist")
    # 以下給測試與 staging：正式 launchd 不帶
    parser.add_argument("--bot-dir", type=Path, help=f"bot 目錄（預設：環境變數 {BOT_DIR_ENV}，再來是這支程式的上一層）")
    parser.add_argument("--maintenance-dir", type=Path, help=f"outbox 所在目錄（預設：環境變數 {MAINTENANCE_ENV}）")
    parser.add_argument("--now", type=float, help="注入現在時間（epoch 秒）：只准和 --dry-run 一起用，或同時給"
                        " --bot-dir、--maintenance-dir 兩個都不是正式路徑的目錄")
    return parser.parse_args(argv)


def _same_dir(a: Path, b: Path) -> bool:
    """同一個目錄嗎：用 samefile 比 inode（macOS 預設大小寫不分，還有 /System/Volumes/Data 的 firmlink，比字串會被
    繞過）。其中一邊不存在＝不是同一個；其他判斷不了的情況當作是正式路徑（拒絕）。"""
    try:
        return os.path.samefile(a, b)
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError, ValueError):
        return True


def now_allowed(args: argparse.Namespace) -> bool:
    """--now 會讓價格庫、封存依假的「今天」清掉舊資料、讓週報依假的日期送出：只准 --dry-run，或明確給的
    --bot-dir、--maintenance-dir 都不是正式路徑（環境變數不算明確給）。"""
    if args.now is None or args.dry_run:
        return True
    if args.bot_dir is None or args.maintenance_dir is None:
        return False
    if any(_same_dir(args.bot_dir, prod) for prod in (BASE, PRODUCTION_BOT_DIR)):
        return False
    return not _same_dir(args.maintenance_dir, DEFAULT_MAINTENANCE)


def main(argv: Optional[Sequence[str]] = None, *, install_guards: bool = False) -> int:
    """install_guards 只有 `__main__` 傳 True（pytest 裡呼叫 main() 不會裝防護）。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    paths = resolve_paths(args.bot_dir, args.maintenance_dir)
    if args.print_plist:
        sys.stdout.write(render_plist(args.bot_dir or os.environ.get(BOT_DIR_ENV) or BASE))
        return EXIT_OK
    if not now_allowed(args):
        log.error("stock picks review skipped reason=now_needs_dry_run_or_test_dirs")
        return EXIT_CONFIG
    guards = _install_guards if install_guards else None
    try:
        if args.dry_run:
            if guards is not None:
                guards()
            result = dry_run(paths, now=args.now)
            weekly = (result["stages"].get("weekly") or {}).pop("text", None)
            sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            if weekly:
                sys.stdout.write("\n" + weekly + "\n")
            return EXIT_OK
        if args.import_archive is not None:
            return import_archive(paths, args.import_archive, now=args.now, guards=guards)
        return run(paths, now=args.now, guards=guards)
    except Exception as exc:  # noqa: BLE001 - traceback 可能帶到資料：只記型別
        log.error("stock picks review failed error_type=%s where=%s", type(exc).__name__, _where(exc))
        return EXIT_FAILED
    finally:
        _cancel_guards()


if __name__ == "__main__":
    sys.exit(main(install_guards=True))
