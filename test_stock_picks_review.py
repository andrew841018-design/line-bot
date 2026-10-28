"""股票推薦回測 job 的執行與防護（jobs/stock_picks_review.py）、週報骨架（jobs/stock_picks_review_weekly.py）。

- 同一個程序：停用旗標、鎖、階段隔離、last_run（只有列舉值、未來時間夾回）、連續失敗一天一則短訊、--dry-run 什麼都不建、
  週報（一週一次、_queued 防重送、只處理最近一個週日）、plist。
- 子程序（和 launchd 同樣的指令形狀：python <腳本>，不加 -I）：斷網 audit hook、alarm 逾時記 timeout、faulthandler
  硬時限，以及照 launchd 的方式跑一整輪。子程序不經過 conftest，所以一律明確帶暫存目錄的參數與環境變數，
  開跑前先確認它們不是正式路徑；main() 從不以預設（正式）路徑執行。
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import test_stock_picks_review_fixtures as fx
from jobs import stock_picks_review as runner
from jobs import stock_picks_review_weekly as weekly

WORK = Path(__file__).resolve().parent
JOB = WORK / "jobs" / "stock_picks_review.py"
SUNDAY_EVENING = fx.ts(2026, 11, 1, 20, 30)
REAL_MAINTENANCE = Path("/Users/andrew/Desktop/andrew/Data_engineer/ops/maintenance")
ENUM = re.compile(r"[A-Za-z0-9_.:/\-]{0,64}")


@pytest.fixture
def paths() -> runner.Paths:
    p = runner.resolve_paths()
    assert p.bot_dir != runner.BASE and p.maintenance != runner.DEFAULT_MAINTENANCE
    return p


@pytest.fixture
def ready(paths) -> runner.Paths:
    """三個狀態檔＋盤勢 DB（含 2026 休市表）都在的 bot 目錄。"""
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    fx.make_market_db(paths.market_db, fx.standard_market_rows(), obs={"tw.holidays.2026": {"rows": []}})
    return paths


def _last_run(paths) -> dict:
    return json.loads(paths.last_run.read_text(encoding="utf-8"))


def _outbox(paths) -> list[str]:
    return sorted(os.listdir(paths.outbox)) if paths.outbox.is_dir() else []


def _assert_enumerated(value) -> None:
    """last_run 只有數字、布林、None 與列舉字串（鍵也一樣）。"""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str) and ENUM.fullmatch(key), key
            _assert_enumerated(item)
    elif isinstance(value, list):
        for item in value:
            _assert_enumerated(item)
    elif isinstance(value, str):
        assert ENUM.fullmatch(value), value
    else:
        assert value is None or isinstance(value, (bool, int, float)), value


def _boom(_ctx):
    raise ValueError("秘密 /Users/someone/private detail")


def _ok(_ctx):
    return {"status": "ok"}


# ── 路徑與隔離 ──────────────────────────────────────────────────────────────────
def test_paths_resolve_to_tmp_under_pytest(paths):
    tmp = Path(os.environ["STOCK_PICKS_REVIEW_BOT_DIR"])
    assert paths.bot_dir == tmp and paths.state_dir == tmp / "state"
    assert paths.maintenance == Path(os.environ["STOCK_PICKS_REVIEW_MAINTENANCE_DIR"])
    assert runner.resolve_paths("/x/bot", "/x/m") == runner.Paths(Path("/x/bot"), Path("/x/m"))


def test_full_weekly_run_never_touches_the_real_outbox(ready):
    """PLAN §9：跑完整個週報流程，正式 outbox／sent／failed 不會多出這支 job 的檔（relay 會立刻 DM Andrew）。
    正式目錄別的程序一直在搬檔，只比這支 job 自己的前綴。"""
    def ours() -> list[str]:
        found = []
        for sub in ("outbox", "sent", "failed"):
            directory = REAL_MAINTENANCE / sub
            if directory.is_dir():
                found += [f"{sub}/{name}" for name in os.listdir(directory)
                          if "stock-picks-weekly" in name or "stock-picks-review" in name]
        return sorted(found)

    before = ours()
    assert runner.run(runner.resolve_paths(), now=SUNDAY_EVENING) == runner.EXIT_OK
    assert ours() == before
    assert _outbox(ready) == ["2026-11-01-stock-picks-weekly.md"]  # 真的組出一份週報（放在暫存的 outbox）


# ── 一輪 ───────────────────────────────────────────────────────────────────────
def test_full_run_records_enumerated_last_run(ready):
    umask = os.umask(0o022)
    os.umask(umask)
    assert runner.run(ready, now=fx.NOW) == runner.EXIT_OK
    assert os.umask(umask) == umask  # 還原了 umask（測試是同一個程序）
    out = _last_run(ready)
    assert out["status"] == "ok" and out["consecutive_failures"] == 0 and out["last_success_at"] == fx.NOW
    assert {name: s["status"] for name, s in out["stages"].items()} == {
        "archive": "ok", "pool": "ok", "prices": "ok", "weekly": "skipped:not_due"}
    assert out["last_t"] == {"tw/v1": "2026-10-27", "tw/ai": "2026-10-27", "us": "2026-10-27"}
    assert out["sizes"]["archive_files"] == 3 and out["sizes"]["sqlite_bytes"] > 0
    assert out["finished_at"] >= out["started_at"]
    _assert_enumerated(out)
    assert (ready.root.stat().st_mode & 0o777) == 0o700
    for name in ("archive", "spool"):
        assert ((ready.root / name).stat().st_mode & 0o777) == 0o700


def test_disabled_flag_exits_quietly_and_creates_nothing(ready):
    ready.disabled.touch()
    before = fx.listing(ready.bot_dir, ready.maintenance)
    assert runner.run(ready, now=fx.NOW) == runner.EXIT_OK
    assert fx.listing(ready.bot_dir, ready.maintenance) == before


def test_locked_run_is_skipped_without_writing(ready):
    runner.ensure_private_dir(ready.root)
    fd = os.open(ready.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.run(ready, now=fx.NOW) == runner.EXIT_OK
        assert not ready.last_run.exists()
    finally:
        os.close(fd)
    assert runner.run(ready, now=fx.NOW) == runner.EXIT_OK and ready.last_run.exists()


def test_unsafe_state_dir_is_refused_and_not_fixed(paths):
    paths.state_dir.chmod(0o755)
    try:
        assert runner.run(paths, now=fx.NOW) == runner.EXIT_CONFIG
        assert (paths.state_dir.stat().st_mode & 0o777) == 0o755 and not paths.root.exists()  # 不 chmod、不建
    finally:
        paths.state_dir.chmod(0o700)


def test_stage_failure_is_isolated_and_message_free(ready):
    stages = (("archive", runner._stage_archive), ("pool", _boom), ("prices", _ok), ("weekly", _ok))
    assert runner.run(ready, now=fx.NOW, stages=stages) == runner.EXIT_FAILED
    out = _last_run(ready)
    assert out["stages"]["archive"]["status"] == "ok" and out["stages"]["prices"]["status"] == "ok"
    assert out["stages"]["pool"]["status"] == "error" and out["stages"]["pool"]["error"] == "ValueError"
    assert out["stages"]["pool"]["where"].startswith("test_stock_picks_review:_boom:")
    text = ready.last_run.read_text(encoding="utf-8")
    assert "秘密" not in text and "/Users" not in text and "private" not in text
    assert out["status"] == "error" and out["consecutive_failures"] == 1


def test_three_failures_send_one_short_note_per_day(ready):
    stages = (("archive", _ok), ("pool", _boom))
    for i in range(2):
        runner.run(ready, now=fx.NOW + i, stages=stages)
    assert _outbox(ready) == []
    runner.run(ready, now=fx.NOW + 10, stages=stages)
    assert _outbox(ready) == ["2026-10-28-stock-picks-review-failed.md"]
    text = (ready.outbox / "2026-10-28-stock-picks-review-failed.md").read_text(encoding="utf-8")
    assert "連續 3 輪失敗" in text and "階段：pool" in text and "類別：ValueError" in text and "秘密" not in text
    runner.run(ready, now=fx.NOW + 20, stages=stages)  # 同一天：不再發
    assert len(_outbox(ready)) == 1 and _last_run(ready)["note"] == "already"
    runner.run(ready, now=fx.NOW + 86400, stages=stages)  # 隔天還在失敗：再一則
    assert len(_outbox(ready)) == 2
    assert runner.run(ready, now=fx.NOW + 86500, stages=(("archive", _ok),)) == runner.EXIT_OK
    assert _last_run(ready)["consecutive_failures"] == 0


def test_last_run_future_timestamps_are_clamped(paths):
    runner.ensure_private_dir(paths.root)
    paths.last_run.write_text(json.dumps({"last_success_at": fx.NOW + 10**6, "consecutive_failures": "x"}))
    prev = runner.read_last_run(paths.last_run, fx.NOW)
    assert prev["last_success_at"] == fx.NOW and prev["consecutive_failures"] == 0


def test_timeout_inside_a_stage_is_recorded(ready):
    def slow(_ctx):
        raise runner.JobTimeout()

    assert runner.run(ready, now=fx.NOW, stages=(("archive", _ok), ("pool", slow), ("prices", _ok))) == runner.EXIT_FAILED
    out = _last_run(ready)
    assert out["status"] == "timeout" and out["stages"]["pool"] == {"status": "timeout"}
    assert "prices" not in out["stages"]  # 逾時就收尾，不再往下跑


def test_spool_dir_is_created_once_and_moving_it_away_stops_the_hook(ready):
    """PLAN §5.2：回滾 B1 時把 spool/ 移走，hook 就停；job 不會把它建回來，三族照樣記（簡版）。"""
    runner.run(ready, now=fx.NOW)
    assert ready.spool.is_dir() and (ready.spool.stat().st_mode & 0o777) == 0o700
    ready.spool.rename(ready.root / "spool.moved")
    fx.write_states(ready.state_dir, ai=fx.ai_state(computed_at=fx.AI_AT + 100, picks=fx.AI_PICKS[:1]))
    assert runner.run(ready, now=SUNDAY_EVENING) == runner.EXIT_OK
    assert not os.path.lexists(ready.spool)
    pool_out = _last_run(ready)["stages"]["pool"]
    assert pool_out["spool"]["dir"] == "missing" and pool_out["tw_ai"]["new"] == 1
    text = (ready.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "（spool 目錄不在：hook 停著）" in text


def test_unsafe_spool_dir_does_not_block_the_families(ready):
    runner.prepare_root(ready)
    ready.spool.chmod(0o755)
    assert runner.run(ready, now=fx.NOW) == runner.EXIT_OK
    pool_out = _last_run(ready)["stages"]["pool"]
    assert pool_out["status"] == "ok" and pool_out["spool"]["dir"] == "unsafe"
    assert pool_out["tw_v1"] == "new" and pool_out["tw_ai"]["new"] == 1 and pool_out["us"] == "new"
    assert (ready.spool.stat().st_mode & 0o777) == 0o755  # 不 chmod


# ── --dry-run ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("existing", [False, True])
def test_dry_run_creates_nothing(ready, existing):
    if existing:
        runner.run(ready, now=fx.NOW)
        fx.write_states(ready.state_dir, ai=fx.ai_state(computed_at=fx.AI_AT + 100, picks=fx.AI_PICKS[:1]))
    before = fx.listing(ready.bot_dir, ready.maintenance)
    out = runner.dry_run(ready, now=SUNDAY_EVENING)
    assert fx.listing(ready.bot_dir, ready.maintenance) == before
    stages = out["stages"]
    assert all(s["status"] not in ("error", "timeout") for s in stages.values()), stages
    assert stages["weekly"]["status"] == "dry_run" and "股票推薦回測週報" in stages["weekly"]["text"]
    if existing:
        assert stages["pool"]["tw_v1"] == "exists" and stages["pool"]["tw_ai"]["new"] == 1
    else:
        assert stages["archive"]["tw"] == "new" and stages["prices"]["status"] == "dry_run"


def test_main_in_process_never_installs_guards(ready, capsys):
    assert runner.main(["--dry-run", "--now", str(fx.NOW)]) == runner.EXIT_OK
    assert runner._GUARDS_INSTALLED is False
    assert json.loads(capsys.readouterr().out.splitlines()[0])["dry_run"] is True
    a, b = socket.socketpair()  # 這個程序的 socket 照常能用（沒有裝 audit hook）
    a.close()
    b.close()


# ── 週報 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("now, expected", [
    (fx.ts(2026, 10, 29, 12), None),  # A 段上線那週：不補 10/25
    (fx.ts(2026, 11, 1, 19, 59), None),
    (fx.ts(2026, 11, 1, 20, 0), "2026-11-01"),
    (fx.ts(2026, 11, 2, 8), "2026-11-01"),
    (fx.ts(2026, 11, 8, 19, 0), "2026-11-01"),
    (fx.ts(2026, 11, 8, 20, 1), "2026-11-08"),
])
def test_report_sunday(now, expected):
    sunday = weekly.report_sunday(now)
    assert (sunday.isoformat() if sunday else None) == expected


def test_weekly_once_per_week_and_only_the_latest_sunday(ready):
    runner.run(ready, now=SUNDAY_EVENING)
    assert _outbox(ready) == ["2026-11-01-stock-picks-weekly.md"]
    runner.run(ready, now=SUNDAY_EVENING + 1800)
    assert _last_run(ready)["stages"]["weekly"]["status"] == "skipped:queued"
    # relay 送完搬到 sent/，撞名時加 -<ts>：一樣算已排過
    (ready.maintenance / "sent").mkdir()
    sent = ready.maintenance / "sent" / "2026-11-01-stock-picks-weekly-1793000000.md"
    (ready.outbox / "2026-11-01-stock-picks-weekly.md").rename(sent)
    runner.run(ready, now=SUNDAY_EVENING + 3600)
    assert _outbox(ready) == []
    # 錯過一個週日（11/08 那份沒排），之後只補最近一個（11/15），不補 11/08
    runner.run(ready, now=fx.ts(2026, 11, 15, 21))
    assert _outbox(ready) == ["2026-11-15-stock-picks-weekly.md"]


def test_weekly_content_reports_collection_status_in_one_message(ready):
    runner.run(ready, now=fx.NOW)  # 先收一天
    runner.run(ready, now=SUNDAY_EVENING)
    text = (ready.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert len(text) <= weekly.MAX_CHARS < 1900
    assert text.startswith("📊 股票推薦回測週報（11/1）")
    assert "・台股 AI 相關股：記到 10/27，共 1 個交易日（本週 +1），沒有缺漏；簡版 1 份、spool 0 份" in text
    assert "・台股舊規則（對照組）：記到 10/27，共 1 個交易日" in text
    assert "・美股：記到 10/27，共 1 個交易日" in text
    # 台股 50＋8＋0050＋TAIEX；美股 50＋3 檔 ETF，缺的是兩檔 ⚪（DB 沒有它們的收盤）
    assert "・價格庫：台股 60 檔都有收盤、美股 51 檔有收盤、缺 2 檔，共 " in text
    assert "【推薦成績卡】推薦成績尚未上線" in text
    assert "【台股休市表】2026 已取得；2027 尚未取得（要在 2/8 前取得，不然跨年的成績會延後）" in text
    assert "自動冷凍績效差的股票：尚未啟用" in text and "最早約 2027/5" in text and "2027/1/31" in text
    assert text.rstrip().endswith(weekly.METHOD)


def test_weekly_reads_perf_card_status_read_only(ready):
    perf = {"schema": 1, "computed_at": int(fx.ts(2026, 10, 31, 9)), "ledger_rows": 12, "ledger_full": False,
            "pending": 3, "skipped": 0, "markets": {
                "tw": {"first_t": "2026-10-08", "from": "2026-10-08", "to": "2026-10-20", "ai_from": None,
                       "h": {"5": {"n": 12}, "20": {"n": 0}}},
                "us": {"first_t": None, "from": None, "to": None, "ai_from": None, "h": {"5": {"n": 0}, "20": {"n": 0}}}}}
    fx.write_json(ready.perf_result, perf)
    before = ready.perf_result.read_bytes()
    runner.run(ready, now=SUNDAY_EVENING)
    text = (ready.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "【推薦成績卡】上線中（最後更新 10/31）：台股 5 日 12 次、20 日 0 次；美股 5 日 0 次、20 日 0 次" in text
    assert ready.perf_result.read_bytes() == before


def test_weekly_says_us_not_live_without_its_state_file(paths):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state())
    fx.make_market_db(paths.market_db, fx.standard_market_rows())
    runner.run(paths, now=SUNDAY_EVENING)
    text = (paths.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "・美股：還沒有紀錄（美股推薦還沒上線）" in text
    assert "【台股休市表】2026 尚未取得；2027 尚未取得" in text


# ── plist ──────────────────────────────────────────────────────────────────────
def test_print_plist(capsys):
    assert runner.main(["--print-plist", "--bot-dir", "/opt/bot"]) == runner.EXIT_OK
    plist = plistlib.loads(capsys.readouterr().out.encode("utf-8"))
    home = os.environ.get("HOME") or str(Path.home())
    assert plist["Label"] == "com.andrew.stock-picks-review"
    assert plist["ProgramArguments"] == ["/opt/bot/.venv/bin/python", "/opt/bot/jobs/stock_picks_review.py"]
    assert plist["WorkingDirectory"] == "/opt/bot"
    assert plist["StartInterval"] == 1800 and plist["ThrottleInterval"] == 60 and plist["RunAtLoad"] is True
    assert plist["WatchPaths"] == [f"/opt/bot/state/{name}" for name in
                                   ("stock_picks_tw.json", "stock_picks_tw_ai.json", "stock_picks_us.json")]
    assert plist["Nice"] == 10 and plist["LowPriorityIO"] is True and plist["Umask"] == 63
    assert set(plist["EnvironmentVariables"]) == {"HOME", "PATH"}
    assert plist["StandardOutPath"] == f"{home}/Library/Logs/stock_picks_review_stdout.log"
    assert plist["StandardErrorPath"] == f"{home}/Library/Logs/stock_picks_review_stderr.log"


def test_import_archive_cli_takes_the_lock(ready, tmp_path, capsys):
    manual = tmp_path / "manual"
    manual.mkdir()
    fx.write_gz(manual / "stock_picks_tw_ai.json.20261029_003901.gz", fx.ai_state())
    assert runner.main(["--import-archive", str(manual)]) == runner.EXIT_OK
    assert json.loads(capsys.readouterr().out)["archived"] == 1
    runner.ensure_private_dir(ready.root)
    fd = os.open(ready.lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.main(["--import-archive", str(manual)]) == runner.EXIT_FAILED  # 有人拿著鎖：不寫
    finally:
        os.close(fd)


# ── 子程序：和 launchd 一樣的指令形狀 ──────────────────────────────────────────────────
def _env(paths: runner.Paths, tmp_path: Path) -> dict:
    """子程序不經過 conftest：環境一律明確給暫存目錄，開跑前確認不是正式路徑。"""
    env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "LINE_BOT_DISABLE_DOTENV": "1",
           "PYTHONDONTWRITEBYTECODE": "1",
           runner.BOT_DIR_ENV: str(paths.bot_dir), runner.MAINTENANCE_ENV: str(paths.maintenance)}
    for key in (runner.BOT_DIR_ENV, runner.MAINTENANCE_ENV):
        value = Path(env[key]).resolve()
        assert value != runner.BASE.resolve() and value != runner.DEFAULT_MAINTENANCE.resolve()
        assert "stock_picks_review" in str(value)  # conftest 的暫存目錄
    return env


DRIVER = textwrap.dedent("""
    import json, socket, sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from jobs import stock_picks_review as runner
    mode, bot, maintenance = sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
    paths = runner.resolve_paths(bot, maintenance)
    assert paths.bot_dir != runner.BASE and paths.maintenance != runner.DEFAULT_MAINTENANCE
    if mode == "network":
        runner._install_guards(alarm_s=0, fault_s=0)
        try:
            socket.create_connection(("127.0.0.1", 9), timeout=2)
        except BaseException as exc:
            print(type(exc).__name__)
    elif mode == "alarm":
        runner._install_guards(alarm_s=1, fault_s=30)
        def slow(ctx):
            time.sleep(10)
            return {"status": "ok"}
        code = runner.run(paths, stages=(("archive", slow), ("pool", slow)))
        print(code)
    elif mode == "fault":
        runner._install_guards(alarm_s=0, fault_s=1)
        time.sleep(20)
        print("still alive")
""")


def _driver(tmp_path: Path) -> Path:
    path = tmp_path / "driver.py"
    path.write_text(DRIVER, encoding="utf-8")
    return path


def test_guard_blocks_network_connections(paths, tmp_path):
    env = _env(paths, tmp_path)
    proc = subprocess.run([sys.executable, str(_driver(tmp_path)), str(WORK), "network", str(paths.bot_dir),
                           str(paths.maintenance)], env=env, capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "NetworkBlocked", proc.stderr  # 沒擋的話會是 ConnectionRefusedError


def test_guard_alarm_is_recorded_as_timeout(paths, tmp_path):
    env = _env(paths, tmp_path)
    proc = subprocess.run([sys.executable, str(_driver(tmp_path)), str(WORK), "alarm", str(paths.bot_dir),
                           str(paths.maintenance)], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == str(runner.EXIT_FAILED), proc.stderr
    out = _last_run(paths)
    assert out["status"] == "timeout" and out["stages"] == {"archive": {"status": "timeout"}}
    assert out["consecutive_failures"] == 1


def test_guard_faulthandler_ends_a_stuck_process(paths, tmp_path):
    env = _env(paths, tmp_path)
    proc = subprocess.run([sys.executable, str(_driver(tmp_path)), str(WORK), "fault", str(paths.bot_dir),
                           str(paths.maintenance)], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0 and "still alive" not in proc.stdout and "Timeout" in proc.stderr


def test_launchd_shaped_run_end_to_end(ready, tmp_path):
    """和 plist 一樣：python jobs/stock_picks_review.py（不加 -I；工作目錄＝bot 目錄），路徑全部是暫存的。
    驗證腳本當 __main__ 時，`from jobs import …` 拿得到其他模組，防護裝了也能跑完整輪。"""
    env = _env(ready, tmp_path)
    proc = subprocess.run([sys.executable, str(JOB), "--bot-dir", str(ready.bot_dir),
                           "--maintenance-dir", str(ready.maintenance)],
                          env=env, cwd=str(WORK), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = _last_run(ready)
    assert out["status"] == "ok" and out["stages"]["pool"]["tw_ai"]["new"] == 1
    assert out["stages"]["prices"]["status"] == "ok"
