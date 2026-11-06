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
import time
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


@pytest.fixture
def settle40(monkeypatch):
    """休市表期限照 perf 的 CALENDAR_SETTLE_SESSIONS＝40 算（週報當下才讀 perf；測試固定下來，不跟著另一邊的改動變）。"""
    monkeypatch.setattr(weekly, "calendar_settle_sessions", lambda: 40)


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


def test_full_weekly_run_never_touches_the_real_outbox(ready, monkeypatch):
    """PLAN §9：跑完整個週報流程，正式 outbox／sent／failed 不會多出這支 job 的檔（relay 會立刻 DM Andrew）。
    - drop 真正寫到哪裡都記下來：一定在暫存的維護目錄底下。
    - 正式目錄別的程序一直在搬檔（relay 把 outbox 的檔搬到 sent/）：只看測試期間三個目錄合起來「新出現」的檔名。"""
    def real_names() -> set[str]:
        found: set[str] = set()
        for sub in ("outbox", "sent", "failed"):
            directory = REAL_MAINTENANCE / sub
            if directory.is_dir():
                found.update(os.listdir(directory))
        return found

    targets: list[Path] = []
    real_drop = runner.drop

    def recording_drop(maintenance, name, text, drafts):
        targets.append(real_drop(maintenance, name, text, drafts))
        return targets[-1]

    monkeypatch.setattr(runner, "drop", recording_drop)
    before = real_names()
    assert runner.run(runner.resolve_paths(), now=SUNDAY_EVENING) == runner.EXIT_OK
    ours = set(_outbox(ready))
    assert ours == {"2026-11-01-stock-picks-weekly.md"}  # 真的組出一份週報（放在暫存的 outbox）
    assert targets and all(ready.maintenance in target.parents for target in targets)
    assert not (real_names() - before) & ours


# ── 一輪 ───────────────────────────────────────────────────────────────────────
def test_full_run_records_enumerated_last_run(ready):
    umask = os.umask(0o022)
    os.umask(umask)
    assert runner.run(ready, now=fx.NOW) == runner.EXIT_OK
    assert os.umask(umask) == umask  # 還原了 umask（測試是同一個程序）
    out = _last_run(ready)
    assert out["status"] == "ok" and out["consecutive_failures"] == 0 and out["last_success_at"] == fx.NOW
    assert {name: s["status"] for name, s in out["stages"].items()} == {
        "archive": "ok", "pool": "ok", "prices": "ok", "returns": "ok", "weekly": "skipped:not_due"}
    returns = out["stages"]["returns"]  # C1：三族各一版，E（10/28）還不在日曆上 → 先等，什麼都沒評
    assert (returns["versions"], returns["wait_e"], returns["evaluated"], returns["frozen"]) == (3, 3, 0, 0)
    assert set(returns["families"]) == {"tw_v1", "tw_ai", "us"}
    assert out["last_t"] == {"tw_v1": "2026-10-27", "tw_ai": "2026-10-27", "us": "2026-10-27"}
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
    paths.last_run.write_text(json.dumps({"last_success_at": fx.NOW + 10**6, "consecutive_failures": "x",
                                          "returns_last_ok_at": fx.NOW + 10**6}))
    prev = runner.read_last_run(paths.last_run, fx.NOW)
    assert prev["last_success_at"] == fx.NOW and prev["consecutive_failures"] == 0
    assert prev["returns_last_ok_at"] == fx.NOW


def test_last_run_keeps_when_returns_was_last_ok(paths):
    """returns 的 busy 不算失敗：last_run 另記 returns 最後一次 ok 的時間（審查 C1 r2#9）。ok 那輪記完成時間；
    busy、error 的輪沿用上一輪的；上一輪的值不是有限數就不沿用。"""
    later = time.time() + 86400  # 比真實時間晚：讀回時不會被夾回 now
    returned = {"status": "ok"}
    stages = (("returns", lambda _ctx: dict(returned)),)
    runner.run(paths, now=later, stages=stages)
    first = _last_run(paths)
    assert first["returns_last_ok_at"] == first["finished_at"] is not None
    for status in ("busy", "error"):
        returned["status"] = status
        runner.run(paths, now=later + 1800, stages=stages)
        assert _last_run(paths)["returns_last_ok_at"] == first["returns_last_ok_at"]
    runner.run(paths, now=later + 3600, stages=(("archive", _ok),))  # 這一輪沒有 returns：照樣沿用
    assert _last_run(paths)["returns_last_ok_at"] == first["returns_last_ok_at"]
    paths.last_run.write_text(json.dumps({"returns_last_ok_at": "x"}))
    returned["status"] = "busy"
    runner.run(paths, now=later + 5400, stages=stages)
    assert _last_run(paths)["returns_last_ok_at"] is None
    returned["status"] = "ok"
    runner.run(paths, now=later + 7200, stages=stages)
    out = _last_run(paths)
    assert out["returns_last_ok_at"] == out["finished_at"] is not None  # 上一輪是 None：這是這一輪記的


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


@pytest.mark.parametrize("mode", [0o755, 0o500])
def test_unsafe_spool_dir_does_not_block_the_families(ready, mode):
    """spool/ 不是剛好 0700（和 hook 的檢查一樣：0755、0500 hook 都不寫）：週報要說權限不對，不是說正常。"""
    runner.prepare_root(ready)
    ready.spool.chmod(mode)
    assert runner.run(ready, now=fx.NOW) == runner.EXIT_OK
    pool_out = _last_run(ready)["stages"]["pool"]
    assert pool_out["status"] == "ok" and pool_out["spool"]["dir"] == "unsafe"
    assert pool_out["tw_v1"] == "new" and pool_out["tw_ai"]["new"] == 1 and pool_out["us"] == "new"
    assert (ready.spool.stat().st_mode & 0o777) == mode  # 不 chmod
    runner.run(ready, now=SUNDAY_EVENING)
    text = (ready.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "（spool 目錄權限不對：hook 停著）" in text and "spool 目錄不在" not in text


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
    (fx.ts(2026, 11, 8, 19, 0), None),  # 週日 20:00 前不補上週那份（當天晚上就有新的一份）
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


def test_weekly_data_section_does_not_need_the_c1_module(ready, monkeypatch, settle40):
    """C1 的模組（returns）import 不了時，週報照樣組得出來：【資料收集】的日曆在 db（缺漏數得出來），只有【推薦成績】
    換成一行說明（第 2 輪 r3#2、r4#5、r1#3）。同一個程序裡要擋 import：拿掉 jobs 套件上的屬性、sys.modules 放 None。"""
    import jobs

    runner.run(ready, now=fx.NOW)  # 先收一天（價格庫有日曆）
    monkeypatch.delattr(jobs, "stock_picks_review_returns", raising=False)
    monkeypatch.setitem(sys.modules, "jobs.stock_picks_review_returns", None)
    ctx = runner.Context(paths=ready, now=SUNDAY_EVENING)
    try:
        text = weekly.build_report(ctx, weekly.report_sunday(SUNDAY_EVENING))
    finally:
        ctx.close()
    assert "・台股 AI 相關股：記到 10/27，共 1 個交易日（本週 +1），沒有缺漏" in text
    assert "【推薦成績】這週算不出來（ModuleNotFoundError），其他內容照常" in text  # 真的擋到了


def test_sunday_before_eight_does_not_send_last_weeks_late_report(ready):
    """Mac 從 11/01 18:00 睡到 11/08 15:00（審查 r3#5）：15:00 不補 11/01 那份，20:00 只送 11/08 這份。"""
    runner.run(ready, now=fx.ts(2026, 11, 8, 15))
    assert _outbox(ready) == [] and _last_run(ready)["stages"]["weekly"]["status"] == "skipped:not_due"
    runner.run(ready, now=fx.ts(2026, 11, 8, 20, 30))
    assert _outbox(ready) == ["2026-11-08-stock-picks-weekly.md"]


def test_weekly_content_reports_collection_status_in_one_message(ready, settle40):
    runner.run(ready, now=fx.NOW)  # 先收一天
    runner.run(ready, now=SUNDAY_EVENING)
    text = (ready.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    max_chars = 1800  # relay 一則 1,900 字：還沒有成績時（A 段的收集狀態＋成績那一段的說明）一則就夠
    assert len(text) <= max_chars < 1900
    assert text.startswith("📊 股票推薦回測週報（11/1）")
    ai = text.index("・台股 AI 相關股：記到 10/27，共 1 個交易日（本週 +1），沒有缺漏；簡版 1 份、spool 0 份")
    us = text.index("・美股：記到 10/27，共 1 個交易日")
    v1 = text.index("・台股舊五條件（對照組）：記到 10/27，共 1 個交易日")  # 名稱、順序和【推薦成績】同一張表
    assert ai < us < v1
    # 台股 50＋8＋0050＋TAIEX；美股 50＋3 檔 ETF，缺的是兩檔 ⚪（DB 沒有它們的收盤）
    assert "・價格庫：台股 60 檔都有收盤、美股 51 檔有收盤、缺 2 檔，共 " in text
    assert "【推薦成績卡】推薦成績尚未上線" in text
    assert ("【台股休市表】2026 已取得；2027 尚未取得（建議 2027/2/23 前取得；"
            "最晚 2027/2/26 前沒有，跨年那幾天的推薦會永久不算）") in text
    assert "⚠️" not in text  # 12/1 之前下一年的表還沒取得只寫在休市表那一行
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


def test_weekly_says_us_not_live_without_its_state_file(paths, settle40):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state())
    fx.make_market_db(paths.market_db, fx.standard_market_rows())
    runner.run(paths, now=SUNDAY_EVENING)
    text = (paths.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "・美股：還沒有紀錄（美股推薦還沒上線）" in text
    assert "【台股休市表】2026 尚未取得；2027 尚未取得（建議 2027/2/23 前取得" in text
    # 當年的表不能用：最上面一直警示（最後期限過了，期間有休市日的推薦都算不了）
    assert text.splitlines()[1] == ("⚠️ 2026 台股休市表尚未取得：已經過了 2026/2/26 的最後期限，"
                                    "期間有休市日的推薦（含跨年那幾天）都會永久不算")


# ── 下一年的台股休市表（審查 r3#1） ─────────────────────────────────────────────────────
def test_holiday_cutoff_follows_the_deployed_perf(monkeypatch):
    """最後期限＝下一年第一個交易日再加 S − 1 個交易日，S＝perf 的 CALENDAR_SETTLE_SESSIONS（舊版 perf 沒有這個常數就用
    SETTLE_SESSIONS；讀不到用 10）。2027：1/1（五）休市，第一個交易日 1/4；S＝10 是 1/15，S＝40 是 2/26。"""
    import stock_picks_perf as perf
    from datetime import date

    assert weekly.calendar_settle_sessions() == getattr(perf, "CALENDAR_SETTLE_SESSIONS", perf.SETTLE_SESSIONS)
    assert weekly.holiday_cutoff(2027, 10) == date(2027, 1, 15)
    assert weekly.holiday_cutoff(2027, 40) == date(2027, 2, 26)
    assert weekly.holiday_cutoff(2028, 40) == date(2028, 2, 25)  # 1/1 是週六：1/3 起算
    # 價格庫日曆已經涵蓋到年初：照日曆（1/5 臨時休市 → 第 10 個交易日往後一天）
    sessions = ["2026-12-30", "2026-12-31", "2027-01-04"] + fx.weekdays(date(2027, 1, 8), 3)
    assert weekly.holiday_cutoff(2027, 10, sessions) == date(2027, 1, 18)
    # 日曆從年中才有（例如價格庫剛建）：不拿年中的日子當年初
    assert weekly.holiday_cutoff(2026, 10, fx.weekdays(fx.T, 30)) == date(2026, 1, 15)
    # 建議期限＝最後期限提早 3 天；建議期限過了只寫最後期限
    assert weekly._deadline_text(date(2027, 2, 26), date(2027, 2, 1)) == (
        "建議 2027/2/23 前取得；最晚 2027/2/26 前沒有，跨年那幾天的推薦會永久不算")
    assert weekly._deadline_text(date(2027, 2, 26), date(2027, 2, 24)) == "最晚 2027/2/26 前沒有，跨年那幾天的推薦會永久不算"
    # 舊版 perf（沒有 CALENDAR_SETTLE_SESSIONS）用 SETTLE_SESSIONS；perf import 不了用 10
    monkeypatch.delattr(perf, "CALENDAR_SETTLE_SESSIONS", raising=False)
    assert weekly.calendar_settle_sessions() == perf.SETTLE_SESSIONS == 10
    monkeypatch.setitem(sys.modules, "stock_picks_perf", None)
    assert weekly.calendar_settle_sessions() == weekly.DEFAULT_SETTLE_SESSIONS == 10


def test_missing_next_year_holidays_become_a_top_warning_from_december(ready, settle40):
    warning_2027 = "⚠️ 2027 台股休市表尚未取得：建議 2027/2/23 前取得；最晚 2027/2/26 前沒有，跨年那幾天的推薦會永久不算"
    runner.run(ready, now=fx.ts(2026, 12, 6, 20, 30))
    lines = (ready.outbox / "2026-12-06-stock-picks-weekly.md").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("📊") and lines[1] == warning_2027
    # 1 月：2027 變成當年，期限前照樣警示；2028 還早，只寫在休市表那一行
    runner.run(ready, now=fx.ts(2027, 1, 3, 20, 30))
    text = (ready.outbox / "2027-01-03-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert text.splitlines()[1] == warning_2027
    assert "⚠️ 2028" not in text and "2028 尚未取得（建議 2028/2/22 前取得；最晚 2028/2/25 前沒有" in text
    # 最後期限過了還沒有：照樣警示，說已經永久不算
    runner.run(ready, now=fx.ts(2027, 2, 28, 20, 30))
    text = (ready.outbox / "2027-02-28-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert text.splitlines()[1] == ("⚠️ 2027 台股休市表尚未取得：已經過了 2027/2/26 的最後期限，"
                                    "期間有休市日的推薦（含跨年那幾天）都會永久不算")


def test_no_holiday_warning_once_next_year_table_is_in(paths):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    fx.make_market_db(paths.market_db, fx.standard_market_rows(),
                      obs={"tw.holidays.2026": {"rows": []}, "tw.holidays.2027": {"rows": []}})
    runner.run(paths, now=fx.ts(2026, 12, 6, 20, 30))
    text = (paths.outbox / "2026-12-06-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "⚠️" not in text and "【台股休市表】2026 已取得；2027 已取得" in text


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


# ── --now 只給測試與 staging（審查 impl r2#2） ──────────────────────────────────────────
def _forbid_running(monkeypatch) -> None:
    """守門壞掉時也不能真的跑到正式路徑：run／import／dry-run 一被呼叫就讓測試失敗。"""
    def boom(*_args, **_kwargs):
        raise AssertionError("--now guard let the job run")

    for name in ("run", "import_archive", "dry_run"):
        monkeypatch.setattr(runner, name, boom)


@pytest.mark.parametrize("extra", [
    [],  # 只有環境變數（conftest 的暫存目錄）不算明確給
    ["--bot-dir", "{tmp}/bot"],
    ["--maintenance-dir", "{tmp}/m"],
    ["--bot-dir", "{base}", "--maintenance-dir", "{tmp}/m"],
    ["--bot-dir", "{prod}", "--maintenance-dir", "{tmp}/m"],
    ["--bot-dir", "{tmp}/bot", "--maintenance-dir", "{maint}"],
    ["--import-archive", "{tmp}", "--bot-dir", "{tmp}/bot", "--maintenance-dir", "{maint}"],
])
def test_now_is_refused_without_dry_run_or_explicit_test_dirs(monkeypatch, tmp_path, extra):
    _forbid_running(monkeypatch)
    names = {"tmp": str(tmp_path), "base": str(runner.BASE), "prod": str(runner.PRODUCTION_BOT_DIR),
             "maint": str(runner.DEFAULT_MAINTENANCE)}
    argv = ["--now", str(fx.NOW)] + [arg.format(**names) for arg in extra]
    assert runner.main(argv) == runner.EXIT_CONFIG
    assert list(tmp_path.iterdir()) == []  # 什麼都沒建


@pytest.mark.skipif(not os.path.isdir(str(runner.BASE).upper()), reason="檔案系統分大小寫：換大小寫就是另一個路徑")
def test_now_guard_is_not_fooled_by_another_spelling_of_the_same_dir(monkeypatch, tmp_path):
    """macOS 預設大小寫不分：換大小寫的正式路徑一樣要擋（審查 impl round2 N2，用 samefile 比 inode）。"""
    _forbid_running(monkeypatch)
    argv = ["--now", str(fx.NOW), "--bot-dir", str(runner.BASE).upper(), "--maintenance-dir", str(tmp_path / "m")]
    assert runner.main(argv) == runner.EXIT_CONFIG
    assert list(tmp_path.iterdir()) == []


def test_last_run_counts_recent_invalid_spools(ready):
    """壞 spool 只讓看到它的那一輪失敗；近 7 天移到 done/*.invalid 的檔數放進 last_run，給每日維護看（round2 N1）。"""
    assert runner.main(["--now", str(fx.NOW), "--bot-dir", str(ready.bot_dir),
                        "--maintenance-dir", str(ready.maintenance)]) == runner.EXIT_OK
    assert _last_run(ready)["spool_invalid_7d"] == 0
    done = ready.bot_dir / "state" / "stock_picks_review" / "spool" / "done"
    done.mkdir(exist_ok=True)
    (done / "tw_ai-2026-10-28-1.json.invalid").write_text("{}")
    old = done / "tw_ai-2026-10-01-1.json.invalid"
    old.write_text("{}")
    os.utime(old, (time.time() - 8 * 86400, time.time() - 8 * 86400))
    assert runner.main(["--now", str(fx.NOW + 1800), "--bot-dir", str(ready.bot_dir),
                        "--maintenance-dir", str(ready.maintenance)]) == runner.EXIT_OK
    assert _last_run(ready)["spool_invalid_7d"] == 1


def test_now_with_dry_run_or_explicit_test_dirs_runs(ready, capsys):
    assert runner.main(["--dry-run", "--now", str(fx.NOW)]) == runner.EXIT_OK
    capsys.readouterr()
    assert runner.main(["--now", str(fx.NOW), "--bot-dir", str(ready.bot_dir),
                        "--maintenance-dir", str(ready.maintenance)]) == runner.EXIT_OK
    assert _last_run(ready)["started_at"] == fx.NOW


# ── 第一次建自己的目錄 ─────────────────────────────────────────────────────────────
def test_root_and_spool_are_created_together_or_not_at_all(paths, monkeypatch):
    """spool/ 建失敗時不能留下「root 在、spool 不在」：之後永遠不會再建 spool/，hook 就無聲停用（審查 impl r1#8）。"""
    real_mkdir = os.mkdir

    def failing_mkdir(path, *args, **kwargs):
        if Path(path).name == "spool":
            raise OSError(28, "No space left on device")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", failing_mkdir)
    with pytest.raises(OSError):
        runner.prepare_root(paths)
    assert sorted(os.listdir(paths.state_dir)) == []  # 沒有半成品，也沒有留下暫存目錄
    monkeypatch.undo()
    runner.prepare_root(paths)
    assert paths.spool.is_dir() and (paths.spool.stat().st_mode & 0o777) == 0o700
    assert (paths.root.stat().st_mode & 0o777) == 0o700 and sorted(os.listdir(paths.state_dir)) == ["stock_picks_review"]
    runner.prepare_root(paths)  # 已經在：照用


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
                           "--maintenance-dir", str(ready.maintenance), "--now", str(fx.NOW)],
                          env=env, cwd=str(WORK), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = _last_run(ready)
    assert out["status"] == "ok" and out["started_at"] == fx.NOW and out["stages"]["pool"]["tw_ai"]["new"] == 1
    assert out["stages"]["prices"]["status"] == "ok" and out["stages"]["weekly"]["status"] == "skipped:not_due"
