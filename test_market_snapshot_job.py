"""jobs/market_snapshot.py（/盤勢 的 launchd job）與 market_tasks（collect 任務框架）測試。

框架在同一個程序裡用假時鐘測（順序、輪流、退避、例外隔離、補洞各一半、停止條件）；監督程序用真的子程序測
（鎖、停用旗標、state/ 不合格、code_unverified、逾時殺整組、SIGTERM、監督程序被 SIGKILL 後子程序自己結束、
outcome 只有列舉值）。子程序一律帶 --root／--pipelines／--publisher：只載入這裡寫的假模組，不會碰到真的
pipeline，也不連網（conftest 的擋網路傳不進子程序，所以靠這三個參數）。時間用 --now 注入。
"""

from __future__ import annotations

import fcntl
import importlib
import json
import logging
import os
import plistlib
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import market_calc
import market_calendar
import market_http
import market_paths
import market_store
import market_tasks
from market_tasks import Context, Task

WORK = Path(__file__).resolve().parent
JOB = WORK / "jobs" / "market_snapshot.py"
T0 = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc).timestamp()
ENUM_RE = re.compile(r"[A-Za-z0-9_.:\-]{0,64}")


def _load_job():
    return importlib.import_module("jobs.market_snapshot")


# ── 同一個程序：任務框架 ─────────────────────────────────────────────────────────


class FakeMono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def env(tmp_path: Path):
    (tmp_path / "state").mkdir(mode=0o700)
    paths = market_paths.Paths.under(tmp_path)
    mono = FakeMono()
    store = market_store.Store(paths.history_db, now_fn=lambda: T0 + (mono.t - 1000.0))
    with market_tasks.isolated_registry():
        yield SimpleNamespace(paths=paths, mono=mono, store=store)
    store.close()


def _ctx(env, *, run_s: float = 200.0, parent_pid=None) -> Context:
    return Context(
        store=env.store, http=market_http, ledger=None, now=T0 + (env.mono.t - 1000.0), paths=env.paths,
        deadline=env.mono.t + run_s, us_cal=market_calendar.Calendar("us"), tw_cal=market_calendar.Calendar("tw"),
        parent_pid=parent_pid, clock=env.mono,
    )


def _task(name, group="core", *, run=None, due=None, budget=30.0, log=None, cost=0.0):
    def _run(ctx):
        if log is not None:
            log.append(name)
        ctx.clock.advance(cost)
        return run(ctx) if run is not None else None

    return Task(name=name, group=group, due=due or (lambda ctx: True), run=_run, budget_s=budget)


class ParseError(ValueError):  # 和 market_sources_* 的 ParseError 一樣只帶 kind
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def _raise(exc):
    def _run(_ctx):
        raise exc

    return _run


def test_groups_run_in_order_and_core_alternates_between_markets(env):
    log: list[str] = []
    tasks = [
        _task("us.low", "low", log=log), _task("tw.bf", "backfill", log=log), _task("us.c1", log=log),
        _task("us.c2", log=log), _task("tw.c1", log=log), _task("misc.c", log=log),
        _task("news.m", "medium", log=log), _task("news.f", "fast", log=log),
    ]
    first = market_tasks.run_collect(_ctx(env), tasks)
    assert first["first"] == "us"
    assert log == ["news.f", "news.m", "us.c1", "tw.c1", "misc.c", "us.c2", "tw.bf", "us.low"]
    log.clear()
    second = market_tasks.run_collect(_ctx(env), tasks)
    assert second["first"] == "tw"
    assert log[2:6] == ["tw.c1", "us.c1", "misc.c", "us.c2"]
    assert first["tasks"] == [t.name for t in tasks] and first["stopped"] is None


def test_a_failing_task_does_not_stop_the_others_and_only_its_kind_is_kept(env):
    tasks = [
        _task("us.typeerror", run=_raise(TypeError("secret detail zq-leak-77"))),
        _task("us.fetch", run=_raise(market_http.FetchError("rate_limited"))),
        _task("tw.parse", run=_raise(ParseError("format"))),
        _task("tw.badkind", run=_raise(ParseError("Not A Kind!"))),
        _task("tw.ok"),
    ]
    summary = market_tasks.run_collect(_ctx(env), tasks)
    assert summary["results"] == {
        "us.typeerror": "unexpected:TypeError", "us.fetch": "rate_limited", "tw.parse": "format",
        "tw.badkind": "unexpected:ParseError", "tw.ok": "ok",
    }
    for name in ("us.typeerror", "us.fetch", "tw.parse"):
        state = env.store.task_state(name)
        assert state["fail_streak"] == 1 and state["next_try"] == pytest.approx(T0 + 900)
        assert state.get("last_success") is None
    assert env.store.task_state("tw.ok")["last_success"] == pytest.approx(T0)
    dumped = json.dumps([env.store.task_state(t.name) for t in tasks])
    assert "secret" not in dumped and "zq-leak-77" not in dumped


def test_backoff_15_30_60_120_minutes_then_success_resets(env):
    assert [market_tasks.backoff_seconds(n) for n in range(0, 6)] == [0, 900, 1800, 3600, 7200, 7200]
    outcomes = iter([TypeError("x"), TypeError("x"), None])
    calls: list[int] = []

    def _run(_ctx):
        calls.append(1)
        exc = next(outcomes)
        if exc is not None:
            raise exc

    task = _task("us.flaky", run=_run)
    assert market_tasks.run_collect(_ctx(env), [task])["results"]["us.flaky"] == "unexpected:TypeError"
    env.mono.advance(899)
    assert market_tasks.run_collect(_ctx(env), [task])["results"]["us.flaky"] == "backoff"
    env.mono.advance(2)
    market_tasks.run_collect(_ctx(env), [task])
    state = env.store.task_state("us.flaky")
    assert state["fail_streak"] == 2 and state["next_try"] == pytest.approx(T0 + 901 + 1800)
    env.mono.advance(1801)
    assert market_tasks.run_collect(_ctx(env), [task])["results"]["us.flaky"] == "ok"
    state = env.store.task_state("us.flaky")
    assert state["fail_streak"] == 0 and state["next_try"] is None and len(calls) == 3


def test_not_yet_partial_and_budget_are_not_failures(env):
    env.store.set_task_state("tw.wait", {"fail_streak": 2, "last_success": 1.0})
    tasks = [
        _task("tw.wait", run=lambda ctx: "not_yet"),
        _task("tw.part", run=lambda ctx: "partial"),
        _task("us.budget", run=_raise(market_http.FetchError("budget"))),
    ]
    results = market_tasks.run_collect(_ctx(env), tasks)["results"]
    assert results == {"tw.wait": "not_yet", "tw.part": "partial", "us.budget": "budget"}
    wait = env.store.task_state("tw.wait")
    assert wait["fail_streak"] == 0 and wait["last_success"] == 1.0  # 還沒公布：不算成功
    assert env.store.task_state("tw.part")["last_success"] == pytest.approx(T0)
    budget = env.store.task_state("us.budget")
    assert budget["fail_streak"] == 0 and budget.get("next_try") is None


def test_due_false_is_skipped_and_due_errors_are_recorded(env):
    ran: list[str] = []
    tasks = [
        _task("us.notdue", due=lambda ctx: False, log=ran),
        _task("us.duebad", due=_raise(KeyError("x")), log=ran),
    ]
    results = market_tasks.run_collect(_ctx(env), tasks)["results"]
    assert results == {"us.notdue": "not_due", "us.duebad": "unexpected:KeyError"}
    assert ran == [] and env.store.task_state("us.notdue") == {}
    assert env.store.task_state("us.duebad")["fail_streak"] == 1


def test_the_run_stops_at_the_deadline(env):
    tasks = [_task("us.slow", cost=60.0), _task("tw.next")]
    summary = market_tasks.run_collect(_ctx(env, run_s=50.0), tasks)
    assert summary["results"] == {"us.slow": "ok", "tw.next": "stopped"}
    assert summary["stopped"] == "deadline"


def test_the_run_stops_when_the_disabled_flag_appears(env):
    def _disable(ctx):
        ctx.paths.disabled.write_text("")

    summary = market_tasks.run_collect(_ctx(env), [_task("us.first", run=_disable), _task("tw.second")])
    assert summary["results"] == {"us.first": "ok", "tw.second": "stopped"}
    assert summary["stopped"] == "disabled"


def test_the_run_stops_when_the_parent_changed(env):
    ctx = _ctx(env, parent_pid=os.getppid() + 100_000)
    summary = market_tasks.run_collect(ctx, [_task("us.first")])
    assert summary["results"] == {"us.first": "stopped"} and summary["stopped"] == "orphaned"
    assert ctx.should_stop()


def test_should_stop_inside_a_task_follows_its_own_budget(env):
    seen = {}

    def _run(ctx):
        seen["left"] = ctx.time_left()
        seen["before"] = ctx.should_stop()
        ctx.clock.advance(6.0)
        seen["after"] = ctx.should_stop()

    market_tasks.run_collect(_ctx(env), [_task("us.budgeted", run=_run, budget=5.0)])
    assert seen == {"left": 5.0, "before": False, "after": True}


def test_backfill_gives_each_market_at_most_half_of_the_time_left(env):
    log: list[str] = []
    allowed: dict[str, float] = {}

    def _bf(name, cost):
        def _run(ctx):
            allowed[name] = ctx.time_left()
            log.append(name)
            ctx.clock.advance(cost)

        return Task(name=name, group="backfill", due=lambda ctx: True, run=_run, budget_s=40.0)

    tasks = [_bf("us.bf1", 30), _bf("us.bf2", 30), _bf("us.bf3", 30), _bf("tw.bf1", 10), _bf("tw.bf2", 10)]
    results = market_tasks.run_collect(_ctx(env, run_s=100.0), tasks)["results"]
    assert log == ["us.bf1", "tw.bf1", "us.bf2", "tw.bf2"]
    # 美股第二個只剩自己那一半的 20 秒；台股第二個受這輪剩下的 30 秒限制
    assert allowed == {"us.bf1": 40.0, "tw.bf1": 40.0, "us.bf2": 20.0, "tw.bf2": 30.0}
    assert results["us.bf3"] == "deferred"


def test_fast_group_is_capped_at_20_seconds(env):
    tasks = [_task(f"news.f{i}", "fast", cost=15.0) for i in range(3)]
    results = market_tasks.run_collect(_ctx(env), tasks)["results"]
    assert results == {"news.f0": "ok", "news.f1": "ok", "news.f2": "deferred"}


def test_task_owned_state_is_kept(env):
    def _run(ctx):
        state = ctx.store.task_state("us.cursor")
        ctx.store.set_task_state("us.cursor", {**state, "cursor": state.get("cursor", 0) + 7})

    market_tasks.run_collect(_ctx(env), [_task("us.cursor", run=_run)])
    market_tasks.run_collect(_ctx(env), [_task("us.cursor", run=_run)])
    state = env.store.task_state("us.cursor")
    assert state["cursor"] == 14 and state["last_kind"] == "ok" and state["fail_streak"] == 0


def test_task_validation():
    with pytest.raises(ValueError):
        Task(name="us hist", group="core", due=lambda c: True, run=lambda c: None, budget_s=1)
    with pytest.raises(ValueError):
        Task(name="us.hist", group="urgent", due=lambda c: True, run=lambda c: None, budget_s=1)
    with pytest.raises(ValueError):
        Task(name="us.hist", group="core", due=lambda c: True, run=lambda c: None, budget_s=0)
    assert Task(name="tw.allbut", group="core", due=lambda c: True, run=lambda c: None, budget_s=1).market == "tw"
    assert Task(name="news.us", group="fast", due=lambda c: True, run=lambda c: None, budget_s=1).market == "other"


def test_register_rejects_the_same_name_from_another_module():
    with market_tasks.isolated_registry() as registry:
        first = Task(name="us.x", group="core", due=lambda c: True, run=lambda c: None, budget_s=1)
        market_tasks.register(first)
        again = Task(name="us.x", group="low", due=lambda c: True, run=lambda c: None, budget_s=1)
        market_tasks.register(again)  # 同一個模組重新載入：換掉
        assert registry == [again]

        def _other(_ctx):
            return None

        _other.__module__ = "some_other_module"
        with pytest.raises(ValueError):
            market_tasks.register(Task(name="us.x", group="core", due=lambda c: True, run=_other, budget_s=1))
    assert all(t.name != "us.x" for t in market_tasks.registered())


def test_load_modules_disables_only_the_broken_ones(tmp_path: Path, monkeypatch):
    mods = tmp_path / "mods"
    mods.mkdir()
    task_src = (
        "from market_tasks import Task, register\n"
        "register(Task(name={name!r}, group='core', due=lambda c: True, run=lambda c: None, budget_s=1))\n"
    )
    (mods / "fake_good_pipe.py").write_text(task_src.format(name="us.good"))
    (mods / "fake_broken_pipe.py").write_text(task_src.format(name="us.half") + "raise RuntimeError('boom')\n")
    (mods / "fake_dep_pipe.py").write_text("import fake_missing_dependency_xyz\n")
    monkeypatch.syspath_prepend(str(mods))
    names = ["fake_good_pipe", "fake_broken_pipe", "fake_dep_pipe", "fake_absent_pipe", "bad-name"]
    try:
        with market_tasks.isolated_registry():
            status = market_tasks.load_modules(names)
            registered = [t.name for t in market_tasks.registered()]
    finally:
        for name in names:
            sys.modules.pop(name, None)
    assert status == {
        "fake_good_pipe": "ok",
        "fake_broken_pipe": "import_error:RuntimeError",
        "fake_dep_pipe": "import_error:ModuleNotFoundError",
        "fake_absent_pipe": "missing",
    }
    assert registered == ["us.good"]  # 壞模組 import 到一半登記的任務也拿掉了


def test_error_kind():
    assert market_tasks.error_kind(market_http.FetchError("cooldown")) == "cooldown"
    assert market_tasks.error_kind(ParseError("too_few")) == "too_few"
    assert market_tasks.error_kind(ZeroDivisionError("1/0")) == "unexpected:ZeroDivisionError"


class _Detailed(Exception):  # 別的程式庫的例外也可能帶 .detail：一律不印
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def test_the_log_line_shows_a_short_parse_error_detail(env, caplog):
    """2026-10-23：同一個 format 可能是 CDN 給了 CSV、表頭變了或截斷；log 行加印 ParseError 的 detail（只限
    [A-Za-z_]{1,24} 的程式列舉字；美股的 detail 有駝峰，例如 tradesTable，審查 r9 gp1）。沒有 detail 時這行一字不差；
    其他例外的內容一律不印。"""
    tasks = [
        _task("tw.csv", run=_raise(market_calc.ParseError("format", "csv"))),
        _task("tw.long", run=_raise(market_calc.ParseError("format", "x" * 25))),
        _task("tw.upper", run=_raise(market_calc.ParseError("empty", "tradesTable"))),
        _task("tw.newline", run=_raise(market_calc.ParseError("format", "csv\n"))),
        _task("tw.nodetail", run=_raise(market_calc.ParseError("format"))),
        _task("us.fetch", run=_raise(market_http.FetchError("rate_limited"))),
        _task("us.other", run=_raise(_Detailed("leakword"))),
        _task("us.duebad", due=_raise(market_calc.ParseError("date", "header"))),
        _task("tw.ok"),
    ]
    with caplog.at_level(logging.INFO, logger="market_tasks"):
        market_tasks.run_collect(_ctx(env), tasks)
    lines = {}
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("market task="):
            lines[message.split()[1].removeprefix("task=")] = message
    assert lines["tw.csv"].startswith("market task=tw.csv kind=format detail=csv dur=")
    assert lines["us.duebad"].startswith("market task=us.duebad kind=date detail=header dur=")
    assert lines["tw.upper"].startswith("market task=tw.upper kind=empty detail=tradesTable dur=")
    for name in ("tw.long", "tw.newline", "tw.nodetail", "us.fetch", "us.other", "tw.ok"):
        assert "detail=" not in lines[name], name
    assert lines["tw.nodetail"].startswith("market task=tw.nodetail kind=format dur=")
    assert "leakword" not in caplog.text and "x" * 25 not in caplog.text


def test_ledger_wait_gives_up_when_the_task_must_stop(env):
    ctx = _ctx(env)
    sleeps: list[float] = []
    ledger = env.store.ledger(sleep_fn=sleeps.append, stop_fn=ctx.should_stop)
    ledger.reserve("www.twse.com.tw")
    ctx.task_deadline = env.mono.t - 1  # 這個任務的時間用完了
    with pytest.raises(market_http.FetchError) as exc:
        ledger.reserve("www.twse.com.tw")
    assert exc.value.kind == "budget" and sleeps == []


def test_job_module_re_exports_the_framework():
    job = _load_job()
    assert job.Task is market_tasks.Task and job.Context is market_tasks.Context
    assert job.register is market_tasks.register


# ── 同一個程序：outcome、版本、plist ────────────────────────────────────────────


def test_sanitize_keeps_only_enumerated_values():
    job = _load_job()
    dirty = {
        "kind": "ok", "count": 3, "ratio": 0.5, "nan": float("nan"), "flag": True, "none": None,
        "message": "Traceback (most recent call last): secret", "spaced": "free text with spaces",
        "nested": {"us.etf.QQQ": "ok", "bad key!": "x", "deep": [1, "unexpected:TypeError", "含中文"]},
        "date": "2026-10-16", 5: "int key",
    }
    assert job.sanitize(dirty) == {
        "kind": "ok", "count": 3, "ratio": 0.5, "flag": True, "none": None,
        "nested": {"us.etf.QQQ": "ok", "deep": [1, "unexpected:TypeError"]}, "date": "2026-10-16",
    }


def test_code_hash_and_status(tmp_path: Path):
    job = _load_job()
    base = tmp_path / "bot"
    (base / "jobs").mkdir(parents=True)
    (base / "market_a.py").write_text("A = 1\n")
    (base / "market_b.py").write_text("B = 1\n")
    (base / "other.py").write_text("X = 1\n")
    (base / "jobs" / "market_snapshot.py").write_text("J = 1\n")
    first = job.code_hash(base)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    (base / "other.py").write_text("X = 2\n")
    assert job.code_hash(base) == first  # 只看 market_*.py 與這支 job
    (base / "market_b.py").write_text("B = 2\n")
    assert job.code_hash(base) != first
    (tmp_path / "state").mkdir(mode=0o700)
    paths = market_paths.Paths.under(tmp_path)
    assert job.code_status(paths, base) == "unchecked"
    market_paths.write_private_atomic(paths.deployed_hash, (job.code_hash(base) + "\n").encode())
    assert job.code_status(paths, base) == "verified"
    market_paths.write_private_atomic(paths.deployed_hash, b"0" * 64)
    assert job.code_status(paths, base) == "unverified"
    paths.deployed_hash.unlink()
    (tmp_path / "elsewhere").write_text(job.code_hash(base))
    paths.deployed_hash.symlink_to(tmp_path / "elsewhere")
    assert job.code_status(paths, base) == "unverified"  # 不跟 symlink


def test_code_hash_covers_the_seasonality_table(tmp_path: Path):
    """第 9 輪 A14：market_seasonality.json（publish 讀它算 season.now）也是程式的一部分：改了它就要 rehash。"""
    job = _load_job()
    base = tmp_path / "bot"
    (base / "jobs").mkdir(parents=True)
    (base / "market_a.py").write_text("A = 1\n")
    (base / "market_z.py").write_text("Z = 1\n")
    (base / "jobs" / "market_snapshot.py").write_text("J = 1\n")
    (base / "other.json").write_text("{}\n")
    without = job.code_hash(base)  # 沒有這個檔（例：舊的暫存目錄）也照常算
    assert [path.name for path in job.code_files(base)] == ["market_a.py", "market_z.py", "market_snapshot.py"]
    (base / "market_seasonality.json").write_text('{"schema": 1}\n')
    with_table = job.code_hash(base)
    assert with_table != without and re.fullmatch(r"[0-9a-f]{64}", with_table)
    assert [path.name for path in job.code_files(base)] == [
        "market_a.py", "market_seasonality.json", "market_z.py", "market_snapshot.py"]
    (base / "market_seasonality.json").write_text('{"schema": 1, "sox": {}}\n')
    assert job.code_hash(base) != with_table  # 內容變了：簽章要重算
    (base / "market_seasonality.json").write_text('{"schema": 1}\n')
    (base / "other.json").write_text('{"x": 1}\n')
    assert job.code_hash(base) == with_table  # 別的 json 不算
    (tmp_path / "state").mkdir(mode=0o700)
    paths = market_paths.Paths.under(tmp_path)
    market_paths.write_private_atomic(paths.deployed_hash, (with_table + "\n").encode())
    assert job.code_status(paths, base) == "verified"
    (base / "market_seasonality.json").write_text('{"schema": 1, "soxx": {}}\n')
    assert job.code_status(paths, base) == "unverified"  # 只改了季節性表、沒重簽 → job 不連網


def test_the_real_code_hash_includes_the_committed_seasonality_table():
    job = _load_job()
    assert WORK / "market_seasonality.json" in job.code_files()
    assert re.fullmatch(r"[0-9a-f]{64}", job.code_hash())


def test_plist_template():
    job = _load_job()
    plist = plistlib.loads(job.render_plist("/Users/test/bot", "/Users/test").encode())
    assert plist["Label"] == "com.andrew.line-bot-market-snapshot"
    assert plist["ProgramArguments"] == ["/Users/test/bot/.venv/bin/python", "/Users/test/bot/jobs/market_snapshot.py"]
    assert plist["WorkingDirectory"] == "/Users/test/bot"
    assert plist["StartInterval"] == 900 and plist["RunAtLoad"] is False
    assert plist["Nice"] == 10 and plist["LowPriorityIO"] is True
    assert set(plist["EnvironmentVariables"]) == {"HOME", "PATH"}
    assert plist["StandardOutPath"] == "/Users/test/Library/Logs/line_bot_market_snapshot_stdout.log"
    assert plist["StandardErrorPath"] == "/Users/test/Library/Logs/line_bot_market_snapshot_stderr.log"


def test_job_imports_do_not_load_config_or_dotenv():
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(WORK)!r})
        import importlib
        importlib.import_module("jobs.market_snapshot")
        import market_store, market_calendar, market_tasks, market_http, safe_fetch
        bad = sorted(m for m in ("config", "dotenv") if m in sys.modules)
        print("BAD", bad)
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=str(WORK))
    assert result.returncode == 0, result.stderr
    assert "BAD []" in result.stdout


def test_print_code_hash_cli():
    result = subprocess.run(
        [sys.executable, str(JOB), "--print-code-hash"], capture_output=True, text=True, timeout=60, cwd=str(WORK)
    )
    assert result.returncode == 0 and re.fullmatch(r"[0-9a-f]{64}\n", result.stdout)


# ── 真的子程序：監督程序 ─────────────────────────────────────────────────────────

_FAKE_HEADER = """
import os, pathlib, subprocess, time
from market_tasks import Task, register
MARKS = pathlib.Path(os.environ["MARKET_TEST_MARKS"])

def _mark(name, text="1"):
    with open(MARKS / name, "a", encoding="utf-8") as handle:
        handle.write(text + "\\n")
"""

FAKE_MODULES = {
    "fake_ok_tasks": _FAKE_HEADER + """
def _ok(ctx):
    _mark("ok_ran")
    return "ok"

def _boom(ctx):
    _mark("boom_ran")
    raise TypeError("secret detail zq-leak-77")

register(Task(name="us.fake_boom", group="core", due=lambda ctx: True, run=_boom, budget_s=5))
register(Task(name="tw.fake_ok", group="core", due=lambda ctx: True, run=_ok, budget_s=5))
""",
    "fake_block_tasks": _FAKE_HEADER + """
def _block(ctx):
    _mark("block_pid", str(os.getpid()))
    while not ctx.should_stop():
        time.sleep(0.05)

register(Task(name="us.fake_block", group="core", due=lambda ctx: True, run=_block, budget_s=600))
""",
    "fake_hang_tasks": _FAKE_HEADER + """
def _hang(ctx):
    child = subprocess.Popen(["/bin/sleep", "60"])
    _mark("hang_child", str(child.pid))
    _mark("hang_pid", str(os.getpid()))
    time.sleep(60)

register(Task(name="us.fake_hang", group="core", due=lambda ctx: True, run=_hang, budget_s=600))
""",
    "fake_loop_tasks": _FAKE_HEADER + """
def _loop(ctx):
    _mark("loop_pid", str(os.getpid()))
    time.sleep(60)  # 不看 should_stop：只能靠看門執行緒發現監督程序不見了

register(Task(name="us.fake_loop", group="core", due=lambda ctx: True, run=_loop, budget_s=600))
""",
    # market_pipeline_us 的寫法：從 jobs.market_snapshot 拿 Task／register（collect 子程序裡那支程式是 __main__）
    "fake_jobs_import_tasks": """
import os, pathlib
from jobs.market_snapshot import Task, register

def _run(ctx):
    with open(pathlib.Path(os.environ["MARKET_TEST_MARKS"]) / "jobs_import_ran", "a") as handle:
        handle.write("1\\n")

register(Task(name="us.fake_jobs_import", group="core", due=lambda ctx: True, run=_run, budget_s=5))
""",
    "fake_publisher": _FAKE_HEADER + """
def publish(store, paths, *, now):
    _mark("published")
    return {"kind": "ok", "rows": 3, "note": "free text 不能進 outcome", "kinds": {"us.etf.QQQ": "ok"}}
""",
}


@pytest.fixture
def job_env(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "state").mkdir(mode=0o700)
    mods = tmp_path / "mods"
    mods.mkdir()
    for name, source in FAKE_MODULES.items():
        (mods / f"{name}.py").write_text(source)
    marks = tmp_path / "marks"
    marks.mkdir()
    environ = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(mods), "MARKET_TEST_MARKS": str(marks)}
    started: list[subprocess.Popen] = []
    yield SimpleNamespace(root=root, marks=marks, env=environ, paths=market_paths.Paths.under(root), started=started)
    for proc in started:  # 測試失敗時也不要留下程序
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _start(job_env, *, pipelines="fake_ok_tasks", publisher="fake_publisher", collect_timeout=10.0,
           publish_timeout=10.0, extra=()):
    argv = [
        sys.executable, str(JOB), "--root", str(job_env.root), "--pipelines", pipelines, "--publisher", publisher,
        "--collect-timeout", str(collect_timeout), "--publish-timeout", str(publish_timeout), "--now", str(T0), *extra,
    ]
    proc = subprocess.Popen(argv, env=job_env.env, cwd=str(WORK), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    job_env.started.append(proc)
    return proc


def _finish(proc, timeout=60.0) -> int:
    _out, err = proc.communicate(timeout=timeout)
    proc.stderr_text = err.decode("utf-8", "replace")
    return proc.returncode


def _marks(job_env, name) -> list[str]:
    path = job_env.marks / name
    return path.read_text().split() if path.exists() else []


def _wait_for(predicate, timeout=15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _outcome(job_env) -> dict:
    return json.loads(job_env.paths.outcome.read_text())


def _all_enumerated(value) -> bool:
    if isinstance(value, dict):
        return all(isinstance(k, str) and ENUM_RE.fullmatch(k) and _all_enumerated(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_all_enumerated(v) for v in value)
    if isinstance(value, str):
        return bool(ENUM_RE.fullmatch(value))
    return value is None or isinstance(value, (bool, int, float))


def test_full_run_writes_an_enumerated_outcome(job_env):
    with market_store.Store(job_env.paths.history_db) as seed:  # market_pipeline_us 的 market-info 核對結果
        seed.put_obs("us.calendar.check", {"T": "2026-10-16", "mismatches": ["is_business_day", "closing_time", "壞字"]},
                     data_date="2026-10-16")
    proc = _start(job_env, pipelines="fake_ok_tasks,fake_missing_pipeline_xyz,fake_jobs_import_tasks")
    assert _finish(proc) == 0, proc.stderr_text
    assert _marks(job_env, "ok_ran") and _marks(job_env, "boom_ran") and _marks(job_env, "published")
    raw = job_env.paths.outcome.read_text()
    assert stat.S_IMODE(os.stat(job_env.paths.outcome).st_mode) == 0o600
    assert "secret" not in raw and "zq-leak-77" not in raw and "free text" not in raw
    outcome = json.loads(raw)
    assert _all_enumerated(outcome)
    assert outcome["status"] == "ok" and outcome["code"] == "unchecked" and outcome["signal"] is None
    assert outcome["collect"]["status"] == "ok" and outcome["collect"]["stopped"] is None
    assert outcome["publish"] == {
        "status": "ok", "kind": "ok", "duration_s": outcome["publish"]["duration_s"],
        "fragment": {"kind": "ok", "rows": 3, "kinds": {"us.etf.QQQ": "ok"}},
    }
    assert outcome["modules"] == {
        "fake_ok_tasks": "ok", "fake_missing_pipeline_xyz": "missing", "fake_jobs_import_tasks": "ok",
    }
    assert _marks(job_env, "jobs_import_ran")  # 從 jobs.market_snapshot 登記的任務也在同一份登錄表
    assert outcome["tasks"]["us.fake_jobs_import"]["kind"] == "ok"
    assert outcome["tasks"]["us.fake_boom"]["kind"] == "unexpected:TypeError"
    assert outcome["tasks"]["us.fake_boom"]["fail_streak"] == 1
    assert outcome["tasks"]["tw.fake_ok"]["kind"] == "ok" and outcome["tasks"]["tw.fake_ok"]["this_run"] == "ok"
    assert outcome["last_collect_at"] >= T0 and outcome["last_publish_at"] >= T0
    assert outcome["calendar_mismatch"] == ["closing_time", "is_business_day"]
    assert outcome["lock_held_s"] >= 0
    # 鎖檔只放開、不刪；下一輪照樣拿得到
    fd = os.open(job_env.paths.lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_publish_that_reports_a_failure_is_not_counted_as_completed(job_env):
    (job_env.root.parent / "mods" / "fake_bad_publisher.py").write_text(
        "def publish(store, paths, *, now):\n    return {'kind': 'schema', 'schema_problems': ['rows']}\n"
    )
    assert _finish(_start(job_env, publisher="fake_bad_publisher")) == 0
    outcome = _outcome(job_env)
    assert outcome["publish"]["status"] == "failed" and outcome["publish"]["kind"] == "schema"
    assert outcome["last_publish_at"] is None and outcome["last_collect_at"] is not None
    assert outcome["status"] == "degraded"


def test_missing_or_raising_publisher_is_recorded(job_env):
    assert _finish(_start(job_env, publisher="fake_publisher_that_does_not_exist")) == 0
    assert _outcome(job_env)["publish"]["kind"] == "missing"
    (job_env.root.parent / "mods" / "fake_raising_publisher.py").write_text(
        "def publish(store, paths, *, now):\n    raise TypeError('secret detail zq-leak-77')\n"
    )
    assert _finish(_start(job_env, publisher="fake_raising_publisher")) == 0
    outcome = _outcome(job_env)
    assert outcome["publish"]["kind"] == "unexpected:TypeError" and outcome["publish"]["status"] == "failed"
    assert "zq-leak-77" not in job_env.paths.outcome.read_text()


def test_second_run_exits_quietly_while_the_lock_is_held(job_env):
    fd = os.open(job_env.paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert _finish(_start(job_env)) == 0
    finally:
        os.close(fd)
    assert _marks(job_env, "ok_ran") == [] and not job_env.paths.outcome.exists()


def test_disabled_flag_skips_everything(job_env):
    job_env.paths.disabled.write_text("")
    assert _finish(_start(job_env)) == 0
    assert _marks(job_env, "ok_ran") == [] and _marks(job_env, "published") == []
    assert not job_env.paths.outcome.exists()


def test_unsafe_state_dir_is_left_alone(job_env):
    os.chmod(job_env.paths.state_dir, 0o755)
    assert _finish(_start(job_env)) == 2
    assert stat.S_IMODE(os.stat(job_env.paths.state_dir).st_mode) == 0o755  # 不去 chmod
    assert not job_env.paths.outcome.exists() and not job_env.paths.lock.exists()


def test_code_unverified_skips_collect_but_still_publishes(job_env):
    market_paths.write_private_atomic(job_env.paths.deployed_hash, b"0" * 64)
    assert _finish(_start(job_env)) == 0
    assert _marks(job_env, "ok_ran") == [] and _marks(job_env, "published")
    outcome = _outcome(job_env)
    assert outcome["status"] == "code_unverified" and outcome["code"] == "unverified"
    assert outcome["collect"]["status"] == "skipped:code_unverified"
    assert outcome["publish"]["status"] == "ok" and outcome["last_collect_at"] is None


def test_collect_timeout_kills_the_whole_group_and_publish_still_runs(job_env):
    proc = _start(job_env, pipelines="fake_hang_tasks", collect_timeout=2.0)
    assert _finish(proc) == 0, proc.stderr_text
    worker, grandchild = int(_marks(job_env, "hang_pid")[0]), int(_marks(job_env, "hang_child")[0])
    assert _wait_for(lambda: not _alive(worker) and not _alive(grandchild), timeout=5)
    assert _marks(job_env, "published")
    outcome = _outcome(job_env)
    assert outcome["collect"]["status"] == "timeout" and outcome["publish"]["status"] == "ok"
    assert outcome["status"] == "degraded" and outcome["last_collect_at"] is None


def test_sigterm_kills_the_child_group_and_the_lock_blocks_a_second_run(job_env):
    first = _start(job_env, pipelines="fake_block_tasks", collect_timeout=60.0)
    assert _wait_for(lambda: _marks(job_env, "block_pid")), "worker did not start"
    worker = int(_marks(job_env, "block_pid")[0])
    assert _finish(_start(job_env, pipelines="fake_block_tasks")) == 0  # 子程序活著：鎖還在
    assert len(_marks(job_env, "block_pid")) == 1
    first.send_signal(signal.SIGTERM)
    assert _finish(first, timeout=20) == 128 + signal.SIGTERM
    assert _wait_for(lambda: not _alive(worker), timeout=5)
    outcome = _outcome(job_env)
    assert outcome["status"] == "terminated" and outcome["signal"] == "SIGTERM"
    assert outcome["collect"]["status"] == "terminated" and outcome["publish"]["status"] == "skipped:signal"
    assert _marks(job_env, "published") == []


def test_killed_supervisor_leaves_no_worker_behind(job_env):
    sup = _start(job_env, pipelines="fake_loop_tasks", collect_timeout=60.0)
    assert _wait_for(lambda: _marks(job_env, "loop_pid")), "worker did not start"
    worker = int(_marks(job_env, "loop_pid")[0])
    sup.kill()  # SIGKILL：監督程序沒機會清理
    sup.wait(timeout=10)
    fd = os.open(job_env.paths.lock, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got_lock = True
        except BlockingIOError:
            got_lock = False
        if got_lock:  # 拿得到鎖時，子程序一定已經不在（它拿著同一把鎖）
            assert not _alive(worker)
        assert _wait_for(lambda: not _alive(worker), timeout=5), "orphaned worker kept running"
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 子程序走了，鎖就放開了
    finally:
        os.close(fd)


def test_worker_refuses_to_run_without_the_supervisor_lock(job_env):
    argv = [
        sys.executable, str(JOB), "--worker", "collect", "--root", str(job_env.root), "--pipelines", "fake_ok_tasks",
        "--publisher", "fake_publisher", "--parent-pid", str(os.getpid()), "--budget-s", "5",
    ]
    result = subprocess.run(argv, env=job_env.env, cwd=str(WORK), capture_output=True, timeout=60)
    assert result.returncode == 4
    assert _marks(job_env, "ok_ran") == []


def test_calendar_coverage_for_the_outcome(tmp_path):
    """2026-10-18 審查 r6a I1／I2：outcome 帶日曆涵蓋到哪天與認不出的日數；空資料庫時美股用官方表、台股沒有表。"""
    import market_store
    from jobs import market_snapshot as job

    store = market_store.Store(tmp_path / "h.sqlite")
    try:
        out = job._calendar_coverage(store)
    finally:
        store.close()
    assert out["calendar_until"]["us"] is not None and out["calendar_until"]["tw"] is None
    assert out["calendar_unclassified"] == {"us": 0, "tw": 0}
    assert out.get("calendar_missing_bars") == {"us": 0, "tw": 0}  # 沒有任何收盤：沒有洞可數
    assert job.sanitize(out) == out


def test_us_freshness_counts_sessions_and_seconds_behind_the_latest_session(tmp_path):
    """2026-10-23（investigations/format_20261023.md §2、freshness_20261023.md §5 第 5 點）：ETF 落後 1 天、成分股停在
    10/16 時 us.hist 一直是 not_yet，outcome 看不出來。最新場次由 job 的日曆現算；ETF 看最舊的那一列（含 CBOE 備援）；
    只有整數或 None。"""
    from datetime import date

    from jobs import market_snapshot as job

    later = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc).timestamp()  # 美東 10/23 06:30：最新收完的是 10/22
    nulls = {"us_etf_lag_sessions": None, "us_etf_lag_s": None, "us_members_lag_sessions": None, "us_hist_vendor": None}
    store = market_store.Store(tmp_path / "h.sqlite")
    try:
        assert job._us_freshness(store, later) == nulls  # 什麼都還沒有
        for symbol in ("VOO", "QQQ", "SOXX", "SPY"):
            rows = [(date(2026, 10, 20), 100.0, 1), (date(2026, 10, 21), 100.0, 1)]
            store.upsert_closes("us", symbol, rows, "nasdaq")
        store.put_obs("us.hist.status", {"D": "2026-10-21", "members_last": "2026-10-16", "vendor": 3},
                      data_date="2026-10-21")  # 狀態停在 10/21（us.hist 沒再寫）：落後照樣用現在的場次算
        out = job._us_freshness(store, later)
        # ETF 缺 10/22（10/22 16:30 美東收完，到現在 14 小時）；成分股缺 10/19–10/22
        assert out == {"us_etf_lag_sessions": 1, "us_etf_lag_s": 14 * 3600, "us_members_lag_sessions": 4,
                       "us_hist_vendor": 3}
        assert job.sanitize(out) == out
        for symbol in ("VOO", "QQQ", "SPY"):  # CBOE 備援也算卡上的資料日；SOXX 還沒有 → 看最舊的那列
            store.upsert_closes("us", symbol, [(date(2026, 10, 22), 101.0, None)], "cboe", provisional=True)
        assert job._us_freshness(store, later)["us_etf_lag_sessions"] == 1
        store.upsert_closes("us", "SOXX", [(date(2026, 10, 22), 101.0, 1)], "nasdaq")
        store.put_obs("us.hist.status", {"members_last": "2026-10-22", "vendor": 0}, data_date="2026-10-22")
        assert job._us_freshness(store, later) == {"us_etf_lag_sessions": 0, "us_etf_lag_s": 0,
                                                   "us_members_lag_sessions": 0, "us_hist_vendor": 0}
        for bad in ({"members_last": "2026-02-30", "vendor": True}, {"members_last": 20261016, "vendor": -1},
                    {"members_last": "10/16/2026", "vendor": 1.5}):
            store.put_obs("us.hist.status", bad, data_date="2026-10-22")
            got = job._us_freshness(store, later)
            assert (got["us_members_lag_sessions"], got["us_hist_vendor"]) == (None, None), bad
    finally:
        store.close()


def test_calendar_missing_bars_counts_holes_inside_the_stored_history(tmp_path):
    """2026-10-23（tw_history (a)）：已存歷史之內表上是交易日、卻沒有收盤的天數進 outcome，像 2026-07-10 那種休市表
    沒寫的臨時休市，補洞補不了時每日維護才看得到。只數 SPY／加權指數第一筆到最後一筆之間；寫了休市覆寫就不算。"""
    from datetime import date, timedelta

    import market_store
    from jobs import market_snapshot as job

    september = [date(2026, 9, 1) + timedelta(days=n) for n in range(30)]
    weekdays = [day for day in september if day.weekday() < 5]
    store = market_store.Store(tmp_path / "h.sqlite")
    try:
        rows = [{"date": "2026-01-01", "name": "開國紀念日", "note": "依規定放假1日。", "kind": "closed"}]
        store.put_obs("tw.holidays.2026", {"year": 2026, "rows": rows, "unknown": []}, data_date=None)
        hole = date(2026, 9, 15)
        store.upsert_closes("tw", "TAIEX", [(day, 47000.0, 1) for day in weekdays if day != hole], "test")
        store.upsert_closes("us", "SPY", [(day, 700.0, 1) for day in weekdays if day != date(2026, 9, 7)], "test")  # 勞動節
        out = job._calendar_coverage(store)
        assert out.get("calendar_missing_bars") == {"us": 0, "tw": 1}
        assert job.sanitize(out) == out
        store.set_calendar_override("tw", hole, "closed", "allbut0999_empty+finmind_empty")
        assert job._calendar_coverage(store).get("calendar_missing_bars") == {"us": 0, "tw": 0}
    finally:
        store.close()


def test_outcome_reports_how_far_us_data_lags(job_env):
    """2026-10-23：整個 job 跑完，outcome 帶 us_etf_lag_sessions／us_etf_lag_s／us_members_lag_sessions／us_hist_vendor
    （都是整數），每日維護從 status_summary 讀；日曆欄位照樣在。"""
    from datetime import date

    with market_store.Store(job_env.paths.history_db) as seed:
        for symbol in ("VOO", "QQQ", "SOXX", "SPY"):
            seed.upsert_closes("us", symbol, [(date(2026, 10, 13), 100.0, 1), (date(2026, 10, 14), 100.0, 1)], "nasdaq")
        seed.put_obs("us.hist.status", {"D": "2026-10-15", "members_last": "2026-10-13", "vendor": 2},
                     data_date="2026-10-15")
    assert _finish(_start(job_env)) == 0
    outcome = _outcome(job_env)
    # --now＝美東 10/16 06:00 → 最新收完的是 10/15（10/15 16:30 美東收完，到 --now 13.5 小時）
    assert outcome["us_etf_lag_sessions"] == 1 and 13.5 * 3600 <= outcome["us_etf_lag_s"] < 13.5 * 3600 + 120
    assert outcome["us_members_lag_sessions"] == 2 and outcome["us_hist_vendor"] == 2
    assert outcome["calendar_until"]["us"] is not None and outcome["calendar_unclassified"] == {"us": 0, "tw": 0}
    assert _all_enumerated(outcome)


@pytest.mark.parametrize("since", [T0 - 60, T0 - 900, 1e12])
def test_a_task_killed_mid_run_backs_off_next_round(env, since):
    """2026-10-18 審查 r6e I1-3：跑到一半被 kill（in_flight 標記還在）→ 下一輪記一次失敗、照退避跳過；跑完會清掉標記。

    2026-10-23 審查 r7b F4／F5：退避從看到標記的時間起算、至少一小時。T0−900 是真實節奏（每 15 分鐘一輪：以前
    next_try＝since＋900＝現在，下一輪照跑、又被殺）；since 在未來（時鐘異常）也不能讓任務停用好幾年。"""
    seen = []
    task = _task("t.killed", run=lambda ctx: seen.append(dict(ctx.store.task_state("t.killed"))))
    env.store.set_task_state("t.killed", {"in_flight": {"since": since}})
    assert market_tasks.run_task(_ctx(env), task) == market_tasks.BACKOFF
    state = env.store.task_state("t.killed")
    assert state["fail_streak"] == 1 and state["last_kind"] == market_tasks.KILLED and "in_flight" not in state
    assert state["next_try"] == pytest.approx(T0 + 3600)
    env.mono.advance(3599)
    assert market_tasks.run_task(_ctx(env), task) == market_tasks.BACKOFF
    env.mono.advance(2)  # 退避時間到了
    assert market_tasks.run_task(_ctx(env), task) == market_tasks.OK
    assert "in_flight" in seen[-1]  # 跑的當下有標記
    state = env.store.task_state("t.killed")
    assert state["fail_streak"] == 0 and "in_flight" not in state


def test_a_task_killed_again_and_again_waits_at_least_an_hour_each_time(env):
    """2026-10-23 審查 r7b F5：連續被殺時 1、2、3 次都隔一小時，第 4 次起兩小時（BACKOFF_S 的第一格等於排程間隔，
    對被殺的任務等於沒退避）。"""
    task = _task("t.hang")
    waits = []
    for _ in range(5):
        env.store.set_task_state("t.hang", {**env.store.task_state("t.hang"), "in_flight": {"since": T0}})  # 又被殺
        now = _ctx(env).wall_time()
        assert market_tasks.run_task(_ctx(env), task) == market_tasks.BACKOFF
        waits.append(round(env.store.task_state("t.hang")["next_try"] - now))
        env.mono.advance(waits[-1])
    assert waits == [3600, 3600, 3600, 7200, 7200]
    assert env.store.task_state("t.hang")["fail_streak"] == 5


def test_a_marker_write_failure_still_runs_the_task(env, monkeypatch, caplog):
    """2026-10-23 審查 r7b F6：in_flight 標記只是保險；寫不進去（例如 DB 暫時鎖住）只記 log、任務照跑，不算失敗。"""
    ran = []
    real = env.store.set_task_state

    def locked_for_markers(name, state):
        if "in_flight" in state:
            raise sqlite3.OperationalError("database is locked")
        return real(name, state)

    monkeypatch.setattr(env.store, "set_task_state", locked_for_markers)
    with caplog.at_level(logging.WARNING, logger="market_tasks"):
        assert market_tasks.run_task(_ctx(env), _task("t.marker", log=ran)) == market_tasks.OK
    assert ran == ["t.marker"]
    state = env.store.task_state("t.marker")
    assert state["fail_streak"] == 0 and state["last_success"] == pytest.approx(T0) and "in_flight" not in state
    assert "market task marker not written task=t.marker error_type=OperationalError" in caplog.text
    assert "database is locked" not in caplog.text


def test_framework_keys_list_the_in_flight_marker():
    """2026-10-23 審查 r7b F8：框架自己會寫 in_flight（run_task 寫、_record 清），說明用的常數要列出來。"""
    assert "in_flight" in market_tasks.FRAMEWORK_KEYS
