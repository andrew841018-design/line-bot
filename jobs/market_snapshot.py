"""市場盤勢（/盤勢，2026-10-17）的 launchd job：監督程序＋collect／publish 兩個子程序。

Andrew 2026-10-17 要 /盤勢 一次看美股／台股的技術面、寬度、量能、期權、籌碼、槓桿、13F、宏觀與事件；
bot 只讀這支 job 寫好的顯示用快照（state/market_dashboard.json）。launchd
（com.andrew.line-bot-market-snapshot）每 15 分鐘叫一次：

  python jobs/market_snapshot.py                        監督程序
  python jobs/market_snapshot.py --worker collect …     子程序（監督程序自己叫；≤240 秒）
  python jobs/market_snapshot.py --worker publish …     子程序（≤90 秒）
  python jobs/market_snapshot.py --print-code-hash      部署腳本寫 state/market_deployed.sha256 用
  python jobs/market_snapshot.py --print-plist          launchd plist 內容

規則（PLAN §2、§9、§19.4、§19.6、§19.7；INTERFACES §9）：
- `state/` 不合格（不是自己的、group／other 有權限、symlink）就不跑，也不去建立或 chmod 它。
- 停用旗標（state/market_snapshot.disabled）存在就安靜結束；拿到鎖之後、兩個子程序之間、子程序每批之間
  都再看一次。
- 鎖：state/market_snapshot.lock 的 flock（LOCK_EX|LOCK_NB），拿不到就安靜結束。鎖的 fd 用 pass_fds 交給
  子程序：監督程序被 kill、子程序還活著時，鎖還在，下一輪不會跟它同時寫。
- 子程序在自己的 process group：逾時就 killpg(SIGKILL) 整組並回收；監督程序收到 SIGTERM／SIGINT／SIGHUP
  也先殺整組、回收，寫完 outcome 才結束。監督程序被 SIGKILL 時，子程序的看門執行緒發現父程序換了就自己結束。
- collect 逾時被 kill，publish 照跑（它只讀 SQLite 裡已 commit 的資料）。
- 部署寫下的 market_*.py sha256（state/market_deployed.sha256）存在卻對不上：不跑 collect（不連網），
  outcome 記 code_unverified；publish 照跑（只讀本機資料）。
- outcome（state/market_snapshot_outcome.json）只放列舉值：種類、主機、次數、時間；不放例外訊息或回應內容。
  log 也一樣。
- 不 import config、不讀 .env；launchd 只給 HOME、PATH。

Task／Context／register 定義在 repo 根目錄的 market_tasks（這裡只轉出去，pipeline 寫
`from jobs.market_snapshot import Task, register` 拿到的是同一份）。
這支程式 import 時不做任何 I/O（只把 bot 目錄放進 sys.path）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib
import json
import logging
import math
import numbers
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from xml.sax.saxutils import escape

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import market_calendar  # noqa: E402
import market_http  # noqa: E402
import market_paths  # noqa: E402
import market_store  # noqa: E402
import market_tasks  # noqa: E402
from market_tasks import Context, Task, error_kind, register, type_name  # noqa: E402,F401  轉出去給 pipeline

log = logging.getLogger("market_snapshot")

THIS_FILE = Path(__file__).resolve()
LABEL = "com.andrew.line-bot-market-snapshot"
PIPELINES = ("market_pipeline_us", "market_pipeline_tw", "market_pipeline_misc")
PUBLISHER = "market_publish"
COLLECT_TIMEOUT_S = 240.0
PUBLISH_TIMEOUT_S = 90.0
COLLECT_MARGIN_S = 35.0  # collect 提早收尾（寫狀態）；要大於單一請求的上限 30 秒（CBOE、SSGA、FinMind），不然最後一個請求會被 kill（2026-10-18 審查 r6a N6）
PARENT_POLL_S = 0.5  # 子程序多久看一次父程序還在不在
WAIT_POLL_S = 0.05
REAP_GRACE_S = 5.0
LOCK_HASH_MAX_BYTES = 4096
OUTCOME_SCHEMA = 1

COLLECT_STATE, PUBLISH_STATE, SUPERVISOR_STATE = "_collect", "_publish", "_supervisor"
US_CALENDAR_CHECK_OBS = "us.calendar.check"  # 要和 market_pipeline_us.OBS_CALENDAR_CHECK 一樣

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2  # state/ 或鎖檔不安全、參數錯
EXIT_ORPHANED = 3  # 子程序：父程序換了
EXIT_BAD_LOCK = 4  # 子程序：沒拿到監督程序的鎖
EXIT_DISABLED = 5  # 子程序：開始時停用旗標已經在
_WORKER_EXIT_KINDS = {
    EXIT_OK: "ok",
    EXIT_ERROR: "error",
    EXIT_CONFIG: "bad_args",
    EXIT_ORPHANED: "orphaned",
    EXIT_BAD_LOCK: "bad_lock",
    EXIT_DISABLED: "disabled",
}

_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_RUN_ID_RE = re.compile(r"[0-9a-f]{0,32}")
_ENUM_RE = re.compile(r"[A-Za-z0-9_.:\-]{0,64}")
_DROP = object()


# ── outcome 只放列舉值 ──────────────────────────────────────────────────────────


def sanitize(value: Any, _depth: int = 0) -> Any:
    """只留數字、布林、None 與符合 [A-Za-z0-9_.:-]{0,64} 的字串（種類、主機、日期、代號）；其他整個丟掉。"""
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
        for key, item in list(value.items())[:300]:
            if isinstance(key, str) and key and _ENUM_RE.fullmatch(key):
                cleaned = sanitize(item, _depth + 1)
                if cleaned is not _DROP:
                    out[key] = cleaned
        return out
    if isinstance(value, (list, tuple)):
        cleaned_items = (sanitize(item, _depth + 1) for item in list(value)[:100])
        return [item for item in cleaned_items if item is not _DROP]
    return _DROP


def _clean(value: Any) -> Any:
    result = sanitize(value)
    return None if result is _DROP else result


# ── 程式版本（部署寫的 sha256） ──────────────────────────────────────────────────


def code_files(base: Path = BASE) -> list[Path]:
    files = sorted(path for path in base.glob("market_*.py") if path.is_file())
    job = base / "jobs" / "market_snapshot.py"
    if job.is_file():
        files.append(job)
    return files


def code_hash(base: Path = BASE) -> str:
    """部署腳本寫進 state/market_deployed.sha256 的值（64 個小寫十六進位字）。

    格式：market_*.py 與 jobs/market_snapshot.py 依相對路徑排序，每個檔一行
    「<相對路徑>\\0<該檔內容的 sha256>\\n」，整串再取 sha256。
    """
    outer = hashlib.sha256()
    for path in code_files(base):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        outer.update(f"{path.relative_to(base).as_posix()}\0{digest}\n".encode("utf-8"))
    return outer.hexdigest()


def code_status(paths: market_paths.Paths, base: Path = BASE) -> str:
    """"unchecked"（沒有部署檔：staging／開發）、"verified" 或 "unverified"（對不上或讀不了）。"""
    try:
        os.lstat(paths.deployed_hash)
    except FileNotFoundError:
        return "unchecked"
    except OSError:
        return "unverified"
    got = market_paths.read_private_file(paths.deployed_hash, LOCK_HASH_MAX_BYTES)
    if got is None:
        return "unverified"
    expected = got[0].decode("ascii", "replace").strip().lower()
    try:
        actual = code_hash(base)
    except OSError:
        return "unverified"
    return "verified" if expected == actual else "unverified"


# ── launchd plist ───────────────────────────────────────────────────────────────

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- /盤勢 的資料 job（2026-10-17）：每 15 分鐘抓公開資料、寫顯示用快照。只用 HOME、PATH，不讀 .env。
     停用：touch state/market_snapshot.disabled（不用卸載）。 -->
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{bot_dir}/.venv/bin/python</string>
\t\t<string>{bot_dir}/jobs/market_snapshot.py</string>
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
\t<integer>900</integer>
\t<key>RunAtLoad</key>
\t<false/>
\t<key>Nice</key>
\t<integer>10</integer>
\t<key>LowPriorityIO</key>
\t<true/>
\t<key>StandardOutPath</key>
\t<string>{home}/Library/Logs/line_bot_market_snapshot_stdout.log</string>
\t<key>StandardErrorPath</key>
\t<string>{home}/Library/Logs/line_bot_market_snapshot_stderr.log</string>
</dict>
</plist>
"""


def render_plist(bot_dir: Optional[Path | str] = None, home: Optional[str] = None) -> str:
    bot = str(bot_dir) if bot_dir is not None else str(BASE)
    home_dir = home or os.environ.get("HOME") or str(Path.home())
    return PLIST_TEMPLATE.format(label=LABEL, bot_dir=escape(bot.rstrip("/")), home=escape(home_dir.rstrip("/")))


# ── 共用 ────────────────────────────────────────────────────────────────────────


def _paths(args: argparse.Namespace) -> market_paths.Paths:
    return market_paths.Paths.under(args.root) if args.root is not None else market_paths.Paths.default()


def _wall_clock(now0: Optional[float]) -> Callable[[], float]:
    """測試用 --now 注入開始時間；之後照真實經過的時間往前走。"""
    if now0 is None:
        return time.time
    mono0 = time.monotonic()
    return lambda: now0 + (time.monotonic() - mono0)


# ── 子程序 ──────────────────────────────────────────────────────────────────────


def _verify_lock_fd(fd: Optional[int], lock_path: Path) -> bool:
    """繼承來的 fd 要正是 state/market_snapshot.lock（監督程序持有 flock 的那個開啟檔）。"""
    if fd is None or fd < 3:
        return False
    try:
        held = os.fstat(fd)
        ref = os.stat(lock_path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(held.st_mode) and (held.st_dev, held.st_ino) == (ref.st_dev, ref.st_ino)


def _start_parent_watch(parent_pid: int, interval: float = PARENT_POLL_S) -> threading.Thread:
    def _watch() -> None:
        while True:
            time.sleep(interval)
            if os.getppid() != parent_pid:
                # 監督程序被 SIGKILL：沒有人會再等這個子程序，也不能讓它拿著鎖繼續跑（任務可能卡在網路上）
                with contextlib.suppress(OSError):
                    os.write(2, b"market_snapshot worker orphaned\n")
                os._exit(EXIT_ORPHANED)

    thread = threading.Thread(target=_watch, name="market-parent-watch", daemon=True)
    thread.start()
    return thread


def _calendars(store: market_store.Store) -> tuple[Any, Any, dict[str, str]]:
    problems: dict[str, str] = {}
    try:
        us_cal = market_calendar.us_calendar(store)
    except Exception as exc:  # noqa: BLE001 - 日曆讀不到時用只有官方表的版本
        problems["us"] = error_kind(exc)
        us_cal = market_calendar.Calendar("us")
    try:
        tw_cal = market_calendar.tw_calendar(store)
    except Exception as exc:  # noqa: BLE001
        problems["tw"] = error_kind(exc)
        tw_cal = market_calendar.Calendar("tw")
    return us_cal, tw_cal, problems


def _calendar_coverage(store: market_store.Store) -> dict[str, Any]:
    """日曆涵蓋到哪天、休市表裡認不出的日數（2026-10-18 審查 r6a I1／I2）：明年的休市表沒抓到、或有認不出的列，
    卡片只會默默變暫缺；寫進 outcome 讓每日維護看得到。只放日期與次數。"""
    us_cal, tw_cal, problems = _calendars(store)
    until: dict[str, Any] = {}
    unclassified: dict[str, int] = {}
    for name, cal in (("us", us_cal), ("tw", tw_cal)):
        value = cal.supported_until
        value = value() if callable(value) else value
        until[name] = value.isoformat() if value is not None else None
        days = cal.unclassified
        days = days() if callable(days) else days
        unclassified[name] = len(list(days or []))
    return {"calendar_until": until, "calendar_unclassified": unclassified, "calendar_problems": problems}


def _collect_worker(args: argparse.Namespace, paths: market_paths.Paths, wall: Callable[[], float]) -> int:
    deadline = time.monotonic() + max(0.0, float(args.budget_s or 0.0))
    store = market_store.Store(paths.history_db, now_fn=wall)
    try:
        modules = market_tasks.load_modules(args.pipelines)
        us_cal, tw_cal, calendar_problems = _calendars(store)
        ctx = Context(
            store=store,
            http=market_http,
            ledger=None,
            now=wall(),
            paths=paths,
            deadline=deadline,
            us_cal=us_cal,
            tw_cal=tw_cal,
            parent_pid=args.parent_pid,
        )
        ctx.ledger = store.ledger(now_fn=wall, stop_fn=ctx.should_stop)
        summary = market_tasks.run_collect(ctx)
        store.set_task_state(
            COLLECT_STATE,
            {
                "run_id": args.run_id,
                "completed_at": wall(),
                "modules": modules,
                "calendars": calendar_problems,
                "tasks": summary["tasks"],
                "results": summary["results"],
                "first": summary["first"],
                "stopped": summary["stopped"],
            },
        )
    finally:
        store.close()
    return EXIT_OK


def _publish_worker(args: argparse.Namespace, paths: market_paths.Paths, wall: Callable[[], float]) -> int:
    store = market_store.Store(paths.history_db, now_fn=wall)
    try:
        state: dict[str, Any] = {"run_id": args.run_id}
        try:
            module = importlib.import_module(args.publisher)
        except ModuleNotFoundError as exc:
            state["kind"] = "missing" if exc.name == args.publisher else "import_error:ModuleNotFoundError"
        except Exception as exc:  # noqa: BLE001 - publish 模組壞掉也要把種類記下來
            state["kind"] = "import_error:" + type_name(exc)
        else:
            started = time.monotonic()
            try:
                fragment = module.publish(store, paths, now=wall())
            except Exception as exc:  # noqa: BLE001
                state["kind"] = error_kind(exc)
            else:
                # market_publish 寫不成快照時不丟例外、在回傳的 kind 寫原因（schema、too_big…）：照它的
                fragment = _clean(fragment) if isinstance(fragment, dict) else None
                reported = fragment.get("kind", "ok") if isinstance(fragment, dict) else "ok"
                state["kind"] = reported if isinstance(reported, str) and reported else "unknown"
                state["fragment"] = fragment
            state["duration_s"] = round(time.monotonic() - started, 3)
        state["finished_at"] = wall()
        store.set_task_state(PUBLISH_STATE, state)
    finally:
        store.close()
    return EXIT_OK


def _worker_main(args: argparse.Namespace) -> int:
    os.umask(0o077)
    paths = _paths(args)
    if args.parent_pid is None or os.getppid() != args.parent_pid:
        return EXIT_ORPHANED
    if not _verify_lock_fd(args.lock_fd, paths.lock):
        return EXIT_BAD_LOCK
    # 子程序自己再開的程序（理論上沒有）不能繼承鎖：鎖的壽命只跟這個子程序
    os.set_inheritable(args.lock_fd, False)
    _start_parent_watch(args.parent_pid)
    if market_paths.flag_set(paths.disabled):
        return EXIT_DISABLED
    wall = _wall_clock(args.now)
    if args.worker == "collect":
        return _collect_worker(args, paths, wall)
    return _publish_worker(args, paths, wall)


# ── 監督程序 ────────────────────────────────────────────────────────────────────


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise OSError("unsafe lock file")
    return fd


@dataclass
class _Child:
    proc: subprocess.Popen
    status: Optional[int] = None  # waitpid 的原始 status
    reaped: bool = False


class Supervisor:
    def __init__(
        self,
        paths: market_paths.Paths,
        args: argparse.Namespace,
        lock_fd: int,
        *,
        wall: Callable[[], float],
    ) -> None:
        self.paths = paths
        self.args = args
        self.lock_fd = lock_fd
        self.wall = wall
        self.run_id = secrets.token_hex(8)
        self.child: Optional[_Child] = None
        self.signal_name: Optional[str] = None

    # 子程序回收：Popen.wait 會拿內部鎖，信號處理裡再叫它可能卡住，所以一律自己用 waitpid
    def _reap(self, child: _Child, *, block_s: float = 0.0) -> bool:
        end = time.monotonic() + block_s
        while True:
            if child.reaped:
                return True
            try:
                pid, status = os.waitpid(child.proc.pid, os.WNOHANG)
            except ChildProcessError:
                child.reaped = True  # 別處（信號處理）已經回收
                if child.proc.returncode is None:
                    child.proc.returncode = -signal.SIGKILL
                return True
            if pid == child.proc.pid:
                child.status = status
                child.reaped = True
                child.proc.returncode = os.waitstatus_to_exitcode(status)
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(WAIT_POLL_S)

    def _kill_group(self, child: _Child) -> None:
        if child.reaped:
            return  # 已回收：那個 pgid 可能被別人拿去用了，不能再送
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.proc.pid, signal.SIGKILL)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        self.signal_name = signal.Signals(signum).name
        child = self.child
        if child is not None and not child.reaped:
            self._kill_group(child)
            self._reap(child, block_s=REAP_GRACE_S)

    def _install_signals(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self._on_signal)

    def _worker_argv(self, kind: str, budget_s: float) -> list[str]:
        args = self.args
        argv = [
            sys.executable, str(THIS_FILE), "--worker", kind,
            "--lock-fd", str(self.lock_fd), "--parent-pid", str(os.getpid()),
            "--run-id", self.run_id, "--budget-s", f"{budget_s:.3f}",
            "--pipelines", ",".join(args.pipelines), "--publisher", args.publisher,
        ]
        if args.root is not None:
            argv += ["--root", str(args.root)]
        if args.now is not None:  # 注入的時間跟著監督程序往前走：publish 的時間才會在 collect 之後
            argv += ["--now", repr(float(self.wall()))]
        return argv

    def run_child(self, kind: str, timeout: float, budget_s: float) -> str:
        """跑一個子程序；回 "ok"、"timeout"、"terminated"、子程序的結束種類或 "signal:<名字>"。"""
        if self.signal_name is not None:
            return "terminated"
        proc = subprocess.Popen(
            self._worker_argv(kind, budget_s),
            pass_fds=(self.lock_fd,),
            process_group=0,
            stdin=subprocess.DEVNULL,
            cwd=str(BASE),
        )
        child = _Child(proc)
        self.child = child
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while not self._reap(child):
                if self.signal_name is not None:
                    self._kill_group(child)
                    self._reap(child, block_s=REAP_GRACE_S)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._kill_group(child)
                    self._reap(child, block_s=REAP_GRACE_S)
                    break
                time.sleep(WAIT_POLL_S)
        finally:
            self.child = None
        if self.signal_name is not None:
            return "terminated"
        if timed_out:
            return "timeout"
        return _exit_kind(child.status)

    def run(self) -> int:
        self._install_signals()
        locked_at = time.monotonic()
        started_at = self.wall()
        code = code_status(self.paths)
        args = self.args
        if code == "unverified":
            collect = "skipped:code_unverified"
        else:
            budget = max(0.0, args.collect_timeout - min(COLLECT_MARGIN_S, args.collect_timeout * 0.25))
            collect = self.run_child("collect", args.collect_timeout, budget)
        if self.signal_name is not None:
            publish = "skipped:signal"
        elif market_paths.flag_set(self.paths.disabled):
            publish = "skipped:disabled"
        else:
            publish = self.run_child("publish", args.publish_timeout, args.publish_timeout)
        outcome = self.compose_outcome(
            code=code, collect=collect, publish=publish, started_at=started_at, locked_at=locked_at
        )
        try:
            data = json.dumps(outcome, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
            market_paths.write_private_atomic(self.paths.outcome, data)
        except Exception as exc:  # noqa: BLE001
            log.error("market outcome not written error_type=%s", type_name(exc))
        log.info(
            "market snapshot status=%s collect=%s publish=%s code=%s lock_held_s=%s",
            outcome.get("status"), collect, publish, code, outcome.get("lock_held_s"),
        )
        if self.signal_name is not None:
            return 128 + int(getattr(signal, self.signal_name))
        return EXIT_OK

    def compose_outcome(
        self, *, code: str, collect: str, publish: str, started_at: float, locked_at: float
    ) -> dict:
        out: dict[str, Any] = {
            "schema": OUTCOME_SCHEMA,
            "run_id": self.run_id,
            "status": "ok",
            "started_at": started_at,
            "code": code,
            "signal": self.signal_name,
            "collect": {"status": collect},
            "publish": {"status": publish},
            "modules": {},
            "calendars": {},
            "calendar_mismatch": [],
            "tasks": {},
            "hosts": {},
            "last_collect_at": None,  # 最後一次「完成」的 collect／publish（逾時、失敗的不算）
            "last_publish_at": None,
        }
        try:
            store = market_store.Store(self.paths.history_db, now_fn=self.wall)
        except Exception as exc:  # noqa: BLE001
            out["store"] = error_kind(exc)
        else:
            try:
                self._fill_from_store(store, out, collect, publish)
            except Exception as exc:  # noqa: BLE001
                out["store"] = error_kind(exc)
            finally:
                store.close()
        if self.signal_name is not None:
            out["status"] = "terminated"
        elif code == "unverified":
            out["status"] = "code_unverified"
        elif out["collect"]["status"] != "ok" or out["publish"]["status"] != "ok" or "store" in out:
            out["status"] = "degraded"
        out["finished_at"] = self.wall()
        out["lock_held_s"] = round(time.monotonic() - locked_at, 3)
        return _clean(out) or {}

    def _fill_from_store(self, store: market_store.Store, out: dict, collect: str, publish: str) -> None:
        cstate = store.task_state(COLLECT_STATE)
        pstate = store.task_state(PUBLISH_STATE)
        sup = store.task_state(SUPERVISOR_STATE)
        names = sup.get("task_names") or []
        results: dict = {}
        if cstate.get("run_id") == self.run_id:
            names = cstate.get("tasks") or names
            results = cstate.get("results") or {}
            out["modules"] = cstate.get("modules") or {}
            out["calendars"] = cstate.get("calendars") or {}
            out["collect"]["stopped"] = cstate.get("stopped")
            out["collect"]["first"] = cstate.get("first")
            if collect == "ok":
                sup["last_completed_collect_at"] = cstate.get("completed_at")
        elif collect == "ok":
            out["collect"]["status"] = "no_state"  # 子程序說好了，卻沒寫下這一輪的紀錄
        if pstate.get("run_id") == self.run_id:
            kind = pstate.get("kind")
            out["publish"]["kind"] = kind
            out["publish"]["duration_s"] = pstate.get("duration_s")
            if isinstance(pstate.get("fragment"), dict):
                out["publish"]["fragment"] = pstate["fragment"]
            if publish == "ok" and kind == "ok":
                sup["last_completed_publish_at"] = pstate.get("finished_at")
            elif publish == "ok":
                out["publish"]["status"] = "failed"
        elif publish == "ok":
            out["publish"]["status"] = "no_state"
        tasks: dict[str, dict] = {}
        for name in names:
            if not isinstance(name, str):
                continue
            state = store.task_state(name)
            tasks[name] = {
                "kind": state.get("last_kind"),  # 最後一次執行的結果種類（market_dashboard.status_summary 讀這個）
                "last_success": state.get("last_success"),
                "fail_streak": state.get("fail_streak", 0),
                "next_try": state.get("next_try"),
                "this_run": results.get(name),
            }
        out["tasks"] = tasks
        out["hosts"] = store.fetch_counts(now=self.wall())
        out["last_collect_at"] = sup.get("last_completed_collect_at")
        out["last_publish_at"] = sup.get("last_completed_publish_at")
        # NYSE 表和 Nasdaq market-info 對不上的種類（market_pipeline_us 存在 obs us.calendar.check）
        check = (store.get_obs(US_CALENDAR_CHECK_OBS) or {}).get("payload")
        mismatches = check.get("mismatches") if isinstance(check, dict) else None
        out["calendar_mismatch"] = sorted({m for m in mismatches if isinstance(m, str)}) if isinstance(mismatches, list) else []
        try:
            out.update(_calendar_coverage(store))
        except Exception as exc:  # noqa: BLE001 - 日曆讀不到不能讓 outcome 寫不出來
            out["calendar_until"] = {"error": error_kind(exc)}
        sup["task_names"] = [n for n in names if isinstance(n, str)]
        store.set_task_state(SUPERVISOR_STATE, sup)


def _exit_kind(status: Optional[int]) -> str:
    if status is None:
        return "lost"
    if os.WIFSIGNALED(status):
        try:
            return "signal:" + signal.Signals(os.WTERMSIG(status)).name
        except ValueError:
            return "signal:unknown"
    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
    return _WORKER_EXIT_KINDS.get(code, "exit:other")


def _supervise(args: argparse.Namespace) -> int:
    os.umask(0o077)
    paths = _paths(args)
    if not market_paths.state_dir_ok(paths.state_dir):
        log.error("market snapshot skipped reason=state_dir_unsafe")
        return EXIT_CONFIG
    if market_paths.flag_set(paths.disabled):
        log.info("market snapshot skipped reason=disabled")
        return EXIT_OK
    try:
        fd = _open_lock(paths.lock)
    except OSError:
        log.error("market snapshot skipped reason=lock_unsafe")
        return EXIT_CONFIG
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("market snapshot skipped reason=locked")
            return EXIT_OK
        if market_paths.flag_set(paths.disabled):  # 部署可能剛好在拿鎖前放了旗標
            log.info("market snapshot skipped reason=disabled")
            return EXIT_OK
        return Supervisor(paths, args, fd, wall=_wall_clock(args.now)).run()
    finally:
        # 只關 fd、不 LOCK_UN：還有子孫拿著同一個開啟檔時，鎖跟著它們，直到最後一個關掉
        os.close(fd)


# ── 進入點 ──────────────────────────────────────────────────────────────────────


def _module_list(text: str) -> list[str]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    for name in names:
        if not _MODULE_RE.fullmatch(name):
            raise argparse.ArgumentTypeError("invalid module name")
    return names


def _timeout(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or not 0 < value <= 3600:
        raise argparse.ArgumentTypeError("timeout out of range")
    return value


def _module_name(text: str) -> str:
    if not _MODULE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("invalid module name")
    return text


def _run_id(text: str) -> str:
    if not _RUN_ID_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("invalid run id")
    return text


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="market_snapshot", description="/盤勢 資料 job")
    parser.add_argument("--worker", choices=("collect", "publish"))
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--run-id", type=_run_id, default="")
    parser.add_argument("--budget-s", type=float, default=0.0)
    # 以下給測試與 staging：正式 launchd 不帶
    parser.add_argument("--root", type=Path, help="全部檔案放到這個目錄底下（Paths.under）")
    parser.add_argument("--pipelines", type=_module_list, default=list(PIPELINES))
    parser.add_argument("--publisher", type=_module_name, default=PUBLISHER)
    parser.add_argument("--collect-timeout", type=_timeout, default=COLLECT_TIMEOUT_S)
    parser.add_argument("--publish-timeout", type=_timeout, default=PUBLISH_TIMEOUT_S)
    parser.add_argument("--now", type=float, help="注入開始時間（epoch 秒）")
    parser.add_argument("--print-code-hash", action="store_true")
    parser.add_argument("--print-plist", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    if args.print_code_hash:
        print(code_hash())
        return EXIT_OK
    if args.print_plist:
        sys.stdout.write(render_plist())
        return EXIT_OK
    try:
        if args.worker:
            return _worker_main(args)
        return _supervise(args)
    except Exception as exc:  # noqa: BLE001 - traceback 可能帶到回應內容：只記型別
        log.error("market snapshot failed error_type=%s", type_name(exc))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
