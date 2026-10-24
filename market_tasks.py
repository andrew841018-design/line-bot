"""市場盤勢（/盤勢，2026-10-17）collect 子程序的任務框架：Task、Context、register、run_collect。

Andrew 2026-10-17 要 /盤勢 一次看美股／台股的技術面、寬度、量能、期權、籌碼、槓桿、13F、宏觀與事件，
來源有二十幾個、各有各的公布時間與限流。jobs/market_snapshot.py 的 collect 子程序 import
market_pipeline_us／tw／misc（各自在 import 時 register 自己的任務），再用 run_collect 依序跑。

規則（PLAN §9、§19.4；INTERFACES §9.3）：
- 順序：fast（30 分鐘來源，整組 ≤20 秒）→ medium（2 小時來源）→ core（有新資料日的日資料；美台交錯，
  每輪輪流誰先）→ backfill（美、台各最多用「開始補洞時剩餘時間」的一半）→ low（週／月／年）。
- 每個任務包 except Exception：只記種類（FetchError／ParseError 的 kind，其他 unexpected:<型別名>），
  不記訊息（log 行另印 ParseError 的 detail，只限 [a-z_]{1,20} 的程式列舉字，2026-10-23）；
  該任務自己累計 fail_streak、退避 15→30→60→120 分鐘（跑到一半被殺的至少 60 分鐘），其他任務照跑。
  自己的帳本說額度不夠（FetchError("budget")）不算失敗、不退避。
- 任務之間檢查這輪該不該停：時間到、父程序換了（監督程序被 kill）、停用旗標出現。
  任務裡面每批之間用 ctx.should_stop()（另外含這個任務自己的時間預算）。
- task_state(任務名) 裡框架只改自己的鍵（FRAMEWORK_KEYS），任務自己存的其他鍵原樣保留。
- run() 可以回 "not_yet"（資料還沒公布：不算失敗、也不算成功）或 "partial"（部分成功，算成功）。

放在 repo 根目錄而不是 jobs/：jobs/ 不是 package，監督程序又是以 __main__ 執行，登錄表放在那支程式裡，
pipeline 模組 import 它時會拿到第二份、登錄不到。jobs/market_snapshot.py 只是把這裡的東西轉出去
（`from jobs.market_snapshot import Task, register` 拿到的是同一份）。

這個模組 import 時不做任何 I/O。
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional, Sequence

import market_paths

if TYPE_CHECKING:
    from market_calendar import Calendar
    from market_store import Store

log = logging.getLogger("market_tasks")

GROUPS = ("fast", "medium", "core", "backfill", "low")
GROUP_CAP_S = {"fast": 20.0}  # PLAN §9：30 分鐘來源整組 ≤20 秒
BACKOFF_S = (900.0, 1800.0, 3600.0, 7200.0)  # 連續失敗 1、2、3、≥4 次後隔多久再試
MARKETS = ("us", "tw")
SCHEDULER_STATE = "_scheduler"  # 框架自己的狀態（輪流誰先）；底線開頭的名字保留給框架與監督程序

OK, NOT_YET, PARTIAL = "ok", "not_yet", "partial"
RUN_STATUSES = frozenset({OK, NOT_YET, PARTIAL})
# 這輪沒有真的執行的原因（只進 outcome，不改 task_state）
NOT_DUE, BACKOFF, DEFERRED, STOPPED = "not_due", "backoff", "deferred", "stopped"
NON_FAILURE_KINDS = frozenset({"budget"})
FRAMEWORK_KEYS = ("last_success", "last_run", "last_kind", "fail_streak", "next_try", "last_duration_s", "in_flight")

_TASK_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_DETAIL_RE = re.compile(r"[a-z_]{1,20}")


@dataclass
class Task:
    name: str  # 例 "us.hist"、"tw.allbut"、"news.us"（也是 task_state 的鍵）
    group: str  # "fast"｜"medium"｜"core"｜"backfill"｜"low"
    due: Callable[["Context"], bool]
    run: Callable[["Context"], Optional[str]]  # 自己寫 store；可以丟例外（框架接住、記 kind、退避）
    budget_s: float  # 這個任務這輪最多用多久
    market: Optional[str] = None  # "us"｜"tw"｜"other"；沒給就看名字開頭（us.／tw.）

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TASK_NAME_RE.fullmatch(self.name):
            raise ValueError("invalid task name")
        if self.group not in GROUPS:
            raise ValueError("invalid task group")
        if not callable(self.due) or not callable(self.run):
            raise ValueError("due and run must be callable")
        budget = float(self.budget_s)
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("budget_s must be > 0")
        self.budget_s = budget
        if self.market is None:
            prefix = self.name.split(".", 1)[0]
            self.market = prefix if prefix in MARKETS else "other"


@dataclass
class Context:
    store: "Store"
    http: Any  # market_http 模組（測試換成假的）
    ledger: Any  # market_store.StoreLedger
    now: float  # 這輪開始的 epoch 秒（任務一律用它判斷「今天」）
    paths: market_paths.Paths
    deadline: float  # 這輪 collect 的結束時間（clock 的時間軸，預設 monotonic）
    us_cal: "Calendar"
    tw_cal: "Calendar"
    parent_pid: Optional[int] = None  # 監督程序的 pid；None＝不檢查（測試）
    clock: Callable[[], float] = time.monotonic
    task_deadline: Optional[float] = None  # 框架在跑某個任務時設：min(這輪結束, 這個任務的預算)
    _started: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    def wall_time(self) -> float:
        """現在的 epoch 秒（以 now 為起點加上經過的時間；測試注入 now 也一致）。"""
        return self.now + (self.clock() - self._started)

    def run_time_left(self) -> float:
        return self.deadline - self.clock()

    def time_left(self) -> float:
        """這個任務還能用多久（不在任務裡時＝這輪還剩多久）。"""
        end = self.deadline if self.task_deadline is None else min(self.deadline, self.task_deadline)
        return end - self.clock()

    def run_stop_reason(self) -> Optional[str]:
        """這一輪要不要整個停下："deadline"、"orphaned"、"disabled" 或 None。"""
        if self.run_time_left() <= 0:
            return "deadline"
        if self.parent_pid is not None and os.getppid() != self.parent_pid:
            return "orphaned"
        if market_paths.flag_set(self.paths.disabled):
            return "disabled"
        return None

    def should_stop(self) -> bool:
        """任務每批之間呼叫：這個任務的時間到、這輪時間到、父程序換了、停用旗標出現。"""
        return self.time_left() <= 0 or self.run_stop_reason() is not None


# ── 登錄表 ──────────────────────────────────────────────────────────────────────

_REGISTRY: list[Task] = []


def _module_of(task: Task) -> Optional[str]:
    return getattr(task.run, "__module__", None)


def register(task: Task) -> None:
    """pipeline 模組在 import 時呼叫。同一個模組重新載入（reload）時同名任務直接換掉；
    兩個模組用了同一個名字 → ValueError（那個模組 import 失敗，outcome 會記下來）。"""
    if not isinstance(task, Task):
        raise TypeError("register() needs a Task")
    for index, existing in enumerate(_REGISTRY):
        if existing.name == task.name:
            if _module_of(existing) != _module_of(task):
                raise ValueError("duplicate task name")
            _REGISTRY[index] = task
            return
    _REGISTRY.append(task)


def registered() -> list[Task]:
    return list(_REGISTRY)


@contextlib.contextmanager
def isolated_registry() -> Iterator[list[Task]]:
    """測試用：區塊內是空的登錄表，結束後還原。"""
    saved = list(_REGISTRY)
    _REGISTRY.clear()
    try:
        yield _REGISTRY
    finally:
        _REGISTRY[:] = saved


def load_modules(names: Sequence[str]) -> dict[str, str]:
    """逐一 import pipeline 模組；壞掉或不存在的只停用它自己的任務。

    回 {模組名: "ok"｜"missing"｜"import_error:<型別名>"}；import 失敗時，它 import 到一半登記的任務全部拿掉。
    """
    status: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not _MODULE_RE.fullmatch(name):
            continue
        before = list(_REGISTRY)
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            _REGISTRY[:] = before
            status[name] = "missing" if exc.name == name else "import_error:ModuleNotFoundError"
        except Exception as exc:  # noqa: BLE001 - 一個模組壞掉不能拖垮整輪
            _REGISTRY[:] = before
            status[name] = "import_error:" + type_name(exc)
        else:
            status[name] = "ok"
    return status


# ── 錯誤種類與退避 ───────────────────────────────────────────────────────────────


def type_name(exc: BaseException) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:40]
    return name or "Exception"


def error_kind(exc: BaseException) -> str:
    """FetchError／ParseError 用它們的 kind；其他一律 unexpected:<型別名>（不記訊息內容）。"""
    if type(exc).__name__ in ("FetchError", "ParseError"):
        kind = getattr(exc, "kind", None)
        if isinstance(kind, str) and _KIND_RE.fullmatch(kind):
            return kind
    return "unexpected:" + type_name(exc)


def error_detail(exc: BaseException) -> Optional[str]:
    """log 行用的 detail（2026-10-23）：同一個 format 可能是 CDN 給了 CSV、表頭變了或回應被截斷，只看 kind 分不出來。

    只收 error_kind 認得的型別（FetchError／ParseError），而且整個字串要符合 [a-z_]{1,20}（程式自己的列舉字，
    例 csv、header）；其他例外的任何內容都不印。"""
    if error_kind(exc).startswith("unexpected:"):
        return None
    detail = getattr(exc, "detail", None)
    return detail if isinstance(detail, str) and _DETAIL_RE.fullmatch(detail) else None


def backoff_seconds(fail_streak: int) -> float:
    if fail_streak <= 0:
        return 0.0
    return BACKOFF_S[min(fail_streak, len(BACKOFF_S)) - 1]


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


# ── 執行 ────────────────────────────────────────────────────────────────────────


def _interleave(tasks: list[Task], first: str) -> list[Task]:
    """美台輪流（first 先），其他市場排在每一輪的最後。"""
    second = "tw" if first == "us" else "us"
    queues = [
        [t for t in tasks if t.market == first],
        [t for t in tasks if t.market == second],
        [t for t in tasks if t.market not in MARKETS],
    ]
    out: list[Task] = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    return out


def _first_market(ctx: Context) -> str:
    """每輪輪流誰先（存在 task_state 的 _scheduler）；狀態讀寫失敗就美股先。"""
    try:
        state = ctx.store.task_state(SCHEDULER_STATE)
        runs = int(state.get("runs") or 0)
        ctx.store.set_task_state(SCHEDULER_STATE, {**state, "runs": runs + 1})
    except Exception as exc:  # noqa: BLE001
        log.warning("market scheduler state error_type=%s", type_name(exc))
        runs = 0
    return MARKETS[runs % 2]


def _record(ctx: Context, task: Task, kind: str, failed: bool, duration: float) -> None:
    try:
        state = ctx.store.task_state(task.name)  # 重讀：任務可能自己寫了游標
        state.pop("in_flight", None)  # 跑完了（成功或失敗）：清掉 run_task 先寫的標記
        wall = ctx.wall_time()
        state["last_run"] = wall
        state["last_kind"] = kind
        state["last_duration_s"] = round(max(0.0, duration), 3)
        streak = int(_number(state.get("fail_streak")) or 0)
        if failed:
            streak += 1
            state["fail_streak"] = streak
            state["next_try"] = wall + backoff_seconds(streak)
        elif kind in (OK, PARTIAL):
            state["fail_streak"] = 0
            state["next_try"] = None
            state["last_success"] = wall
        elif kind == NOT_YET:
            state["fail_streak"] = 0
            state["next_try"] = None
        else:  # 自己的帳本額度不夠：連續失敗次數與下次時間都不動
            state["fail_streak"] = streak
            state.setdefault("next_try", None)
        ctx.store.set_task_state(task.name, state)
    except Exception as exc:  # noqa: BLE001 - 記不下來只寫 log，其他任務照跑
        log.warning("market task state not recorded task=%s error_type=%s", task.name, type_name(exc))


KILLED = "killed"  # 上一輪跑到一半被監督程序 kill（in_flight 標記還在）
KILLED_MIN_BACKOFF_S = 3600.0  # 被殺之後至少隔這麼久再試（2026-10-23 審查 r7b F5）


def run_task(ctx: Context, task: Task, allow_s: Optional[float] = None) -> str:
    """跑一個任務（含退避、due、例外與記帳）；回這輪的結果種類。

    2026-10-18 審查 r6e I1-3：task.run 之前先寫 in_flight 標記，_record 跑完才清掉。被 SIGKILL 的話標記會留著，
    下一輪看到就記一次失敗、照退避跳過，不會每一輪都由同一個卡死的任務吃光整個群組的時間。

    2026-10-23 審查 r7b F4／F5：退避從「這輪看到標記的時間」起算、至少一小時。以前從標記的 since 起算：15 分鐘那一格
    剛好等於排程間隔（since 約是 900 秒前），下一輪照跑、又被殺一次；since 在未來（時鐘異常、DB 被改）還會讓任務
    無聲停用好幾年。現在 since 只是紀錄、不參與計算。F6：標記寫不進去只記 log、任務照跑（標記只是保險，
    不能讓任務被跳過、也不算一次失敗）。"""
    try:
        state = ctx.store.task_state(task.name)
    except Exception as exc:  # noqa: BLE001
        return error_kind(exc)
    if isinstance(state.get("in_flight"), dict):
        streak = int(_number(state.get("fail_streak")) or 0) + 1
        state.pop("in_flight", None)
        backoff = max(KILLED_MIN_BACKOFF_S, backoff_seconds(streak))
        state.update(fail_streak=streak, last_kind=KILLED, next_try=ctx.wall_time() + backoff)
        try:
            ctx.store.set_task_state(task.name, state)
        except Exception as exc:  # noqa: BLE001
            return error_kind(exc)
    next_try = _number(state.get("next_try"))
    if next_try is not None and ctx.wall_time() < next_try:
        return BACKOFF
    allow = task.budget_s if allow_s is None else min(task.budget_s, allow_s)
    ctx.task_deadline = ctx.clock() + allow
    started = ctx.clock()
    failed = False
    detail: Optional[str] = None
    try:
        try:
            due = bool(task.due(ctx))
        except Exception as exc:  # noqa: BLE001
            kind = error_kind(exc)
            detail = error_detail(exc)
            failed = kind not in NON_FAILURE_KINDS
        else:
            if not due:
                return NOT_DUE
            try:
                ctx.store.set_task_state(task.name, {**state, "in_flight": {"since": ctx.wall_time()}})
            except Exception as exc:  # noqa: BLE001 - 標記只是保險：寫不進去照跑、不算失敗（r7b F6）
                log.warning("market task marker not written task=%s error_type=%s", task.name, type_name(exc))
            try:
                status = task.run(ctx)
            except Exception as exc:  # noqa: BLE001
                kind = error_kind(exc)
                detail = error_detail(exc)
                failed = kind not in NON_FAILURE_KINDS
            else:
                kind = status if status in RUN_STATUSES else OK
    finally:
        ctx.task_deadline = None
    duration = ctx.clock() - started
    _record(ctx, task, kind, failed, duration)
    if detail is not None:  # 沒有 detail 時這行和以前一字不差（診斷靠數這行的 kind）
        log.info("market task=%s kind=%s detail=%s dur=%.1f", task.name, kind, detail, duration)
    else:
        log.info("market task=%s kind=%s dur=%.1f", task.name, kind, duration)
    return kind


def run_collect(ctx: Context, tasks: Optional[Sequence[Task]] = None) -> dict:
    """依 GROUPS 順序跑完這輪；回 {"first", "results": {任務: 結果}, "stopped": 原因|None, "tasks": [名字]}。"""
    tasks = registered() if tasks is None else list(tasks)
    first = _first_market(ctx)
    results: dict[str, str] = {}
    stopped: Optional[str] = None
    for group in GROUPS:
        members = [t for t in tasks if t.group == group]
        if group in ("core", "backfill"):
            members = _interleave(members, first)
        caps: dict[str, float] = {}
        if group == "backfill" and members:
            half = max(0.0, ctx.run_time_left()) / 2
            caps = {market: half for market in MARKETS}
        group_cap = GROUP_CAP_S.get(group)
        group_used = 0.0
        market_used: dict[str, float] = {}
        for task in members:
            if stopped is None:
                stopped = ctx.run_stop_reason()
            if stopped is not None:
                results[task.name] = STOPPED
                continue
            allow = task.budget_s
            if group_cap is not None:
                allow = min(allow, group_cap - group_used)
            cap = caps.get(task.market or "")
            if cap is not None:
                allow = min(allow, cap - market_used.get(task.market or "", 0.0))
            if allow <= 0:
                results[task.name] = DEFERRED
                continue
            started = ctx.clock()
            results[task.name] = run_task(ctx, task, allow)
            spent = max(0.0, ctx.clock() - started)
            group_used += spent
            market_used[task.market or ""] = market_used.get(task.market or "", 0.0) + spent
    return {"first": first, "results": results, "stopped": stopped, "tasks": [t.name for t in tasks]}
