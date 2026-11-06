"""stock_picks_review_hook（回測週報的 AI 池子 spool，2026-10-29 回測 PLAN §5.2）的測試：寫入時機（_commit_ai 回 True 之後
才寫）、例外不外漏、目錄檢查、檔數上限、100 ms、AI 關著不做事、unknown 和 no 分開記，以及和回測 job 的契約（spool 格式與
檔名、目錄配置、review.sqlite 的路徑：用 job 真的程式核對）。B2 的冷凍（PLAN_B2C2 §2）：effective、read_cooldown、
apply_cooldown 的邊界，_maybe_fill_ai 的過濾與 spool 的全名單，同一個 T 重算不收回；美股那一半在 test_stock_picks_us.py。
合成行情、tmp state、不連網。"""

from __future__ import annotations

import ast
import copy
import importlib
import json
import os
import statistics
import sys
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import stock_picks as sp
import stock_picks_perf as perf
import stock_picks_review_hook as hook
from test_stock_picks_ai import T, TODAY, _calendar, _facts, _snapshot, _state
from test_stock_picks_ai_worker import Clock, Rig


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(sp, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_ai(monkeypatch):
    monkeypatch.setattr(sp, "AI_ENABLED", True)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", False)
    monkeypatch.setattr(sp, "_taipei_today", lambda: TODAY)


def _make_spool(mode: int = 0o700, parent_mode: int = 0o700) -> Path:
    """回測 job 建的目錄：state/stock_picks_review（0700）/spool（0700）。"""
    spool = hook.spool_dir()
    spool.mkdir(parents=True)
    os.chmod(spool.parent, parent_mode)
    os.chmod(spool, mode)
    return spool


def _spool_files(spool: Path) -> list[Path]:
    return sorted(p for p in spool.iterdir() if p.is_file())


def _history_days() -> list[str]:
    path = sp.STATE_DIR / sp.AI_HISTORY_NAME
    return [json.loads(x)["t"] for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _inputs(n: int = 2):
    """直接呼叫 write_ai_spool 用的最小輸入：n 檔都在主池、都是候選、前 3 檔入選。"""
    facts = [_facts(str(1101 + i)) for i in range(n)]
    state = _state(facts)
    state["picks"] = state["picks"][:3]
    return state, _snapshot(facts), list(facts), {f.code: "00952" for f in facts}


def _write(state, snapshot, candidates, sources, *, chart_cache=None, fail_tries=None) -> bool:
    return hook.write_ai_spool(state, snapshot, candidates, sources, chart_cache=chart_cache or {},
                               fail_tries=fail_tries or {})


def _hook_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "stock_picks_review_hook" and r.levelname == "WARNING"]


# ── 寫入時機與內容 ────────────────────────────────────────────────────────────
def test_one_spool_file_after_a_successful_commit_with_every_trend_kind(monkeypatch, clock):
    spool = _make_spool()
    rig = Rig(monkeypatch, clock, 10, cum=lambda i: 100.0 - i if i < 9 else 5.0)  # 1101–1109 是候選；1110 營收不夠
    rig.snapshot.pool["1101"] = _facts("1101")  # 主池有走勢
    sp._ai_cache_t = rig.snapshot.t  # 同一個 T 前幾輪留下的日線快取
    sp._ai_chart_cache.update({"1102": (sp._AI_NO, "chart_unavailable"), "1107": (sp._AI_HOLE, clock.mono + 600)})
    sp._ai_fail_tries["1108"] = 1
    rig.yahoo.kind("1103", "below")  # 算得出窗口、但在年線之下
    rig.yahoo.kind("1104", "short")  # 這個 T 確定算不出窗口
    state = rig.run()
    assert rig.picks == ["1101", "1105", "1106"] and state["skipped"] == {"1102": "chart_unavailable"}
    assert _history_days() == [T.isoformat()]
    [path] = _spool_files(spool)
    assert path.name == f"tw_ai-{T.isoformat()}-{round(state['computed_at'])}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["schema"], data["family"], data["t"], data["rule"]) == (1, "tw_ai", T.isoformat(), "ai_v1")
    assert data["computed_at"] == state["computed_at"] == sp._ai_state["computed_at"]  # 對得回已經 commit 的那一版
    assert (data["source"], data["core_version"]) == ("00952", "test-v1")
    assert data["picks"] == ["1101", "1105", "1106"] and data["funnel"] == state["funnel"]
    assert data["skipped"] == {"1102": "chart_unavailable"}
    assert (data["cooldown_read"], data["cooldown"]) == ("missing", {"removed": [], "reason": None})  # 沒有冷凍檔
    members = {m["c"]: m for m in data["members"]}
    assert [m["c"] for m in data["members"]] == [f.code for f in rig.facts]  # 冷凍前的全名單
    assert all(m["c0"] == 100.0 and m["ai_source"] == "00952" and m["status"] == "member" for m in members.values())
    assert members["1101"]["n"] == "股1101" and members["1101"]["facts"]["c"] == "1101"
    assert [members[c]["rank"] for c in sorted(members)] == [1, 2, 3, 4, 5, 6, 7, 8, 9, None]  # 1110 不是候選
    assert [c for c, m in members.items() if m["picked"]] == ["1101", "1105", "1106"]
    assert members["1101"]["checks"] == {"ai": True, "growth": True, "cheap": True, "trend": True}
    assert members["1101"]["tr"] == members["1101"]["facts"]["tr"] == [100.0, 100.0, 90.0, 0]
    labels = {c: m["trend"] for c, m in members.items()}
    assert labels == {
        "1101": "pool",
        "1102": "skipped:chart_unavailable",
        "1103": "ok",  # 算得出窗口、但在年線之下
        "1104": "no:bars_short",
        "1105": "ok", "1106": "ok",
        "1107": "unknown",  # 前幾輪留下的洞
        "1108": "unknown",  # 這個 T 抓失敗過
        "1109": "unchecked",  # 排在第 3 檔入選者後面：這一版沒查
        "1110": None,  # 不是候選、主池也沒有
    }
    assert members["1103"]["checks"]["trend"] is False and members["1103"]["tr"][3] < 0
    assert members["1104"]["checks"]["trend"] is None and members["1104"]["tr"] is None  # 不知道 ≠ 不合格
    assert members["1110"]["checks"] == {"ai": True, "growth": False, "cheap": True, "trend": None}


@pytest.mark.parametrize("why", ["disk_is_newer", "write_failed"])
def test_no_spool_when_commit_ai_returns_false(why, monkeypatch, clock):
    spool = _make_spool()
    rig = Rig(monkeypatch, clock, 4)
    calls = []
    real = hook.write_ai_spool
    monkeypatch.setattr(hook, "write_ai_spool", lambda *a, **k: calls.append(1) or real(*a, **k))
    commits = []
    real_commit = sp._commit_ai
    if why == "disk_is_newer":  # 別的程序已經寫了比較新的 T：_commit_ai 回 False、改用磁碟上的
        newer = T + timedelta(days=1)
        sp._write_json_file(sp.STATE_DIR / sp.AI_NAME, _state([_facts("2330", data_date=newer)], t=newer))
        monkeypatch.setattr(sp, "_commit_ai", lambda payload: commits.append(real_commit(payload)) or commits[-1])
    else:  # 鎖忙、驗證不過
        monkeypatch.setattr(sp, "_commit_ai", lambda payload: commits.append(False) or False)
    rig.run()
    assert commits == [False] and rig.yahoo.calls  # 真的算完、走到 _commit_ai
    assert calls == [] and _spool_files(spool) == [] and _history_days() == []


def test_hook_failures_never_reach_the_ai_round(monkeypatch, clock, caplog):
    rig = Rig(monkeypatch, clock, 4)

    def boom(*_a, **_k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(hook, "write_ai_spool", boom)
    state = rig.run()  # 不丟例外
    assert state["t"] == T.isoformat() and state["fail_count"] == 0 and sp._ai_next_try == 0.0
    assert _history_days() == [T.isoformat()]  # 推薦照常寫好、照常追加歷史
    assert "stock picks ai review spool failed error_type=RuntimeError" in caplog.text
    assert "synthetic" not in caplog.text
    monkeypatch.setitem(sys.modules, "stock_picks_review_hook", None)  # 部署途中葉節點還沒放好
    clock.advance(sp.AI_RETRY_S + 1)
    rig.version = "test-v2"  # 名單換版：同一個 T 重算一次
    state = rig.run()
    assert state["core_version"] == "test-v2" and state["fail_count"] == 0 and sp._ai_next_try == 0.0
    assert "stock picks ai review spool failed error_type=ModuleNotFoundError" in caplog.text


def test_write_errors_inside_the_hook_are_caught(monkeypatch, caplog):
    _make_spool()
    state, snapshot, candidates, sources = _inputs()

    def disk_full(*_a, **_k):
        raise OSError("synthetic")

    monkeypatch.setattr(sp, "_write_json_file", disk_full)
    assert _write(state, snapshot, candidates, sources) is False
    assert _hook_warnings(caplog) == ["stock picks review spool failed error_type=OSError"]
    bad = dict(state, computed_at=float("nan"))
    assert _write(bad, snapshot, candidates, sources) is False  # 狀態不像樣：不寫、不丟例外


def test_ai_disabled_is_a_no_op(monkeypatch, clock):
    spool = _make_spool()
    state, snapshot, candidates, sources = _inputs()
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    assert _write(state, snapshot, candidates, sources) is False and _spool_files(spool) == []
    rig = Rig(monkeypatch, clock, 4)
    rig.run()  # LINE_BOT_STOCK_PICKS_AI=0：AI 段整段不跑
    assert sp._ai_state is None and _spool_files(spool) == [] and rig.yahoo.calls == []


# ── 目錄與檔數 ───────────────────────────────────────────────────────────────
def test_missing_spool_dir_is_skipped_quietly(caplog):
    state, snapshot, candidates, sources = _inputs()
    assert _write(state, snapshot, candidates, sources) is False
    hook.spool_dir().parent.mkdir(mode=0o700)  # 只有上一層：一樣安靜跳過
    assert _write(state, snapshot, candidates, sources) is False
    assert _hook_warnings(caplog) == [] and not hook.spool_dir().exists()  # 不 mkdir


@pytest.mark.parametrize("setup, reason", [
    ("symlink", "not_dir"), ("file", "not_dir"), ("mode", "mode"), ("parent_writable", "review_dir_writable"),
    ("parent_symlink", "review_dir_not_dir"), ("owner", "review_dir_owner"),
])
def test_spool_dir_must_be_our_own_0700_directory(setup, reason, caplog, tmp_path):
    spool = hook.spool_dir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    os.chmod(elsewhere, 0o700)
    if setup == "symlink":
        spool.parent.mkdir(mode=0o700)
        spool.symlink_to(elsewhere)
    elif setup == "file":
        spool.parent.mkdir(mode=0o700)
        spool.write_text("x", encoding="utf-8")
    elif setup == "mode":
        _make_spool(mode=0o755)
    elif setup == "parent_writable":
        _make_spool(parent_mode=0o777)
    elif setup == "parent_symlink":
        (elsewhere / "spool").mkdir(mode=0o700)
        os.chmod(elsewhere / "spool", 0o700)
        spool.parent.symlink_to(elsewhere)
    else:
        _make_spool()
    state, snapshot, candidates, sources = _inputs()
    with pytest.MonkeyPatch.context() as patch:  # 別人的目錄（只在這一次呼叫裡假裝 uid 不同）
        if setup == "owner":
            real_uid = os.getuid()
            patch.setattr(os, "getuid", lambda: real_uid + 1)
        assert _write(state, snapshot, candidates, sources) is False
    assert _hook_warnings(caplog) == [f"stock picks review spool skipped reason={reason}"]
    assert list(elsewhere.rglob("*.json")) == []


def test_file_cap_skips_and_warns(caplog):
    spool = _make_spool()
    (spool / "done").mkdir(mode=0o700)  # job 匯入後搬去的子目錄不算
    for i in range(30):
        (spool / "done" / f"old-{i}.json").write_text("{}", encoding="utf-8")
    for i in range(hook.SPOOL_MAX_FILES - 1):
        (spool / f"tw_ai-2026-10-01-{i}.json").write_text("{}", encoding="utf-8")
    state, snapshot, candidates, sources = _inputs()
    assert _write(state, snapshot, candidates, sources) is True  # 第 200 個照寫
    assert len(_spool_files(spool)) == hook.SPOOL_MAX_FILES
    later = dict(state, computed_at=state["computed_at"] + 60)
    assert _write(later, snapshot, candidates, sources) is False  # 滿了：不寫，記一個 warning
    assert _hook_warnings(caplog) == ["stock picks review spool skipped reason=too_many_files limit=200"]
    assert len(_spool_files(spool)) == hook.SPOOL_MAX_FILES


def test_maximal_input_is_written_within_100_ms():
    spool = _make_spool()
    for i in range(hook.SPOOL_MAX_FILES - 2):  # 目錄快滿：數檔數也是最壞情況
        (spool / f"tw_ai-2026-10-01-{i}.json").write_text("{}", encoding="utf-8")
    facts = [_facts(str(1101 + i), name="測試股票名稱十六個字以內好", revenue=sp.Revenue(2026, 8, 10.0, 100.0 - i / 10))
             for i in range(120)]  # AI 狀態檔允許的最大名單
    snapshot = _snapshot(facts, pool=False)
    for f in facts[:50]:
        snapshot.pool[f.code] = f
    windows = ([100.0] * 240, [100.0] * 60)
    cache = {f.code: (sp._AI_OK, windows) for f in facts[50:100]}
    cache.update({f.code: (sp._AI_NO, "bars_short") for f in facts[100:110]})
    state = _state(facts)
    state["picks"] = state["picks"][:3]
    sources = {f.code: "AI 伺服器" for f in facts}
    timings = []
    for i in range(2):
        run = dict(state, computed_at=state["computed_at"] + i)
        start = time.perf_counter()
        assert hook.write_ai_spool(run, snapshot, facts, sources, chart_cache=cache, fail_tries={"1211": 1}) is True
        timings.append(time.perf_counter() - start)
    assert max(timings) < 0.1, timings
    newest = max(_spool_files(spool), key=lambda p: p.stat().st_mtime_ns)
    data = json.loads(newest.read_text(encoding="utf-8"))
    assert len(data["members"]) == 120 and all(m["rank"] for m in data["members"])
    assert newest.stat().st_size < sp.STATE_MAX_BYTES / 10
    assert statistics.mean(timings) < 0.1


# ── unknown 和 no 分開 ────────────────────────────────────────────────────────
def test_unknown_is_never_recorded_as_no():
    facts = [_facts(str(1101 + i)) for i in range(9)]
    snapshot = _snapshot(facts, pool=False)
    snapshot.pool["1101"] = facts[0]
    cache = {
        "1102": (sp._AI_OK, ([100.0] * 240, [100.0] * 60)),
        "1103": (sp._AI_HOLE, time.monotonic() + 600),  # 有洞、還在等 Yahoo 補
        "1104": (sp._AI_HOLE, time.monotonic() - 600),  # 有洞、等太久（快取過期）
        "1105": (sp._AI_NO, "bars_short"),  # 這個 T 確定算不出窗口
        "1106": (sp._AI_NO, "previous_day_missing"),  # 洞等了 3 小時才放棄：資料拿不到，不是不合格
        "1107": (sp._AI_NO, "Not A Code!"),  # 看不懂的原因不照抄
    }
    data = hook.build_ai_spool(_state(facts), snapshot, facts, {f.code: "00952" for f in facts},
                               chart_cache=cache, fail_tries={"1108": 2})
    members = {m["c"]: m for m in data["members"]}
    assert {c: m["trend"] for c, m in members.items()} == {
        "1101": "pool", "1102": "ok",
        "1103": "unknown", "1104": "unknown",  # 有洞（還在等或已過期）：未知
        "1105": "no:bars_short",  # 這個 T 確定算不出窗口
        "1106": "skipped:previous_day_missing",  # 洞等了 3 小時才放棄：資料拿不到，不是不合格
        "1107": "no:other",  # 看不懂的原因不照抄
        "1108": "unknown",  # 抓失敗過
        "1109": "unchecked",
    }
    assert members["1102"]["tr"] == [100.0, 100.0, 100.0, 0]
    cond = {c: m["checks"]["trend"] for c, m in members.items()}
    assert (cond["1101"], cond["1102"]) == (True, True)
    assert all(cond[c] is None for c in ("1103", "1104", "1105", "1106", "1107", "1108", "1109"))


def test_market_comes_from_the_main_snapshot_and_is_null_without_one():  # impl r1 #2：C3 的「大盤偏熱」要每個樣本的大盤
    state, snapshot, candidates, sources = _inputs()

    def market_of(snap):
        return hook.build_ai_spool(state, snap, candidates, sources, chart_cache={}, fail_tries={})["market"]

    assert market_of(replace(snapshot, market=sp.Market(sp.HOT, 35, 61))) == [sp.HOT, 35, 61]  # 主快照狀態檔的寫法
    assert market_of(replace(snapshot, market=None)) is None


# ── 和回測 job 的契約（impl r1 #3、r2 #4、r4 #1） ─────────────────────────────────────────────
# job 不 import 這個葉節點（A 比 B1 先部署），spool 格式與檔名、目錄配置、review.sqlite 的路徑兩邊各寫一份：
# 任一邊改了，spool 會整批被判 invalid、默默退回簡版（週報只看得到「spool 0 份」）。job 的模組用到時才 import。
def _job(name: str):
    return importlib.import_module(f"jobs.{name}")


def _job_spool(monkeypatch) -> Path:
    """job 自己建的目錄（prepare_root：state/stock_picks_review/ 與 spool/），bot 的 STATE_DIR 指到同一個 state/。"""
    runner = _job("stock_picks_review")
    paths = runner.resolve_paths()
    assert paths.bot_dir != runner.BASE and paths.maintenance != runner.DEFAULT_MAINTENANCE  # conftest 指到暫存目錄
    runner.prepare_root(paths)
    monkeypatch.setattr(sp, "STATE_DIR", paths.state_dir)
    assert hook.spool_dir() == paths.spool
    return paths.spool


def _job_accepts(pool, path: Path, state: dict):
    """job 匯入一個 spool 的步驟：檔名（SPOOL_NAME_RE）→ 讀檔（自己的一般檔、大小上限）→ spool_version
    （不合格丟 ValueError）。"""
    match = pool.SPOOL_NAME_RE.fullmatch(path.name)
    assert match is not None, path.name
    family, day, computed_at = match.group(1), match.group(2), float(match.group(3))
    assert (family, day, round(computed_at)) == (hook.FAMILY_AI, state["t"], round(state["computed_at"]))
    obj = json.loads(pool.read_private_bytes(path, pool.SPOOL_MAX_BYTES))
    return pool.spool_version(obj, family, day, {"rule": state["rule"], "computed_at": state["computed_at"]})


def test_contract_directory_names_and_review_db_match_the_job(monkeypatch):
    runner = _job("stock_picks_review")
    paths = runner.Paths(bot_dir=Path("/nonexistent/bot"), maintenance=Path("/nonexistent/maintenance"))
    assert paths.spool == paths.state_dir / hook.REVIEW_DIR_NAME / hook.SPOOL_DIR_NAME
    assert paths.db == paths.state_dir / perf.REVIEW_DB  # perf.backfill_prices 唯讀的那個庫
    monkeypatch.setattr(sp, "STATE_DIR", paths.state_dir)
    assert hook.spool_dir() == paths.spool
    assert hook.cooldown_path() == paths.state_dir / "stock_picks_review" / "cooldown.json"  # PLAN_B2C2 §4.4
    pool = _job("stock_picks_review_pool")
    assert (hook.FAMILY_AI, hook.FAMILY_US, hook.FROZEN) == (pool.FAMILY_AI, pool.FAMILY_US, pool.FROZEN)


def test_contract_a_real_spool_is_found_and_accepted_by_the_job(monkeypatch, clock):
    pool = _job("stock_picks_review_pool")
    spool = _job_spool(monkeypatch)
    assert pool.spool_dir_state(spool) == "ok"  # job 建的目錄 hook 寫得進去，job 也認得
    rig = Rig(monkeypatch, clock, 10, cum=lambda i: 100.0 - i if i < 9 else 5.0)
    rig.snapshot.pool["1101"] = _facts("1101")
    monkeypatch.setattr(sp, "_snapshot", replace(rig.snapshot, market=sp.Market(sp.HOT, 35, 61)))
    sp._ai_cache_t = rig.snapshot.t
    sp._ai_chart_cache.update({"1102": (sp._AI_NO, "chart_unavailable"), "1107": (sp._AI_HOLE, clock.mono + 600)})
    sp._ai_fail_tries["1108"] = 1
    rig.yahoo.kind("1103", "below")
    rig.yahoo.kind("1104", "short")
    state = rig.run()
    [path] = _spool_files(spool)
    version = _job_accepts(pool, path, state)  # bot 真的寫出來的位元組，原樣交給 job
    data = json.loads(path.read_text(encoding="utf-8"))
    got = [(r.code, r.name, r.c0, r.picked, r.rank, r.status, r.trend["trend"], r.trend["tr"]) for r in version.rows]
    assert got == [(m["c"], m["n"], m["c0"], m["picked"], m["rank"], m["status"], m["trend"], m["tr"])
                   for m in data["members"]]  # 每一列、每個欄位都照 hook 寫的讀進去（改名會在這裡失敗）
    assert len(got) == 10 and {m["trend"] for m in data["members"]} >= {"pool", "ok", "skipped:chart_unavailable",
                                                                       "no:bars_short", "unknown", "unchecked", None}
    assert version.market == data["market"] == [sp.HOT, 35, 61]  # impl r1 #2
    assert version.cooldown_read == "missing"  # C2b 合併前的檢查點：B2 活著


def test_contract_the_largest_spool_is_accepted_by_the_job():
    pool = _job("stock_picks_review_pool")
    facts = [_facts(str(1101 + i), name="測試股票名稱十六個字以內好", revenue=sp.Revenue(2026, 8, 10.0, 100.0 - i / 10))
             for i in range(120)]  # AI 狀態檔允許的最大名單、名稱最長
    snapshot = _snapshot(facts, pool=False)
    for f in facts[:50]:
        snapshot.pool[f.code] = f
    cache = {f.code: (sp._AI_OK, ([100.0] * 240, [100.0] * 60)) for f in facts[50:100]}
    state = _state(facts)
    state["picks"] = state["picks"][:3]
    payload = hook.build_ai_spool(state, snapshot, facts, {f.code: "AI 伺服器" for f in facts}, chart_cache=cache,
                                  fail_tries={})
    data = json.loads(json.dumps(payload, ensure_ascii=False))  # 和 _write_json_file 同一種序列化
    version = pool.spool_version(data, hook.FAMILY_AI, state["t"], {"rule": state["rule"],
                                                                    "computed_at": state["computed_at"]})
    assert len(version.rows) == 120 and all(r.rank for r in version.rows)


def test_contract_every_trend_label_the_hook_writes_is_accepted_by_the_job():  # impl r4 #7：兩邊的原因碼規則
    pool = _job("stock_picks_review_pool")
    bot_reasons = ["bars_short", "calendar_short", "close_invalid", "close_mismatch", "previous_close_mismatch",
                   "price_jump", *sorted(sp._AI_SKIP_REASONS)]  # bot 會放進日線快取的原因
    odd = ["a" * 32, "a" * 33, "1st", "Not A Code!", "", None]  # 最長的、太長的、數字開頭、看不懂的
    reasons = list(dict.fromkeys(bot_reasons + odd))
    facts = [_facts(str(1101 + i)) for i in range(len(reasons) + 6)]
    snapshot = _snapshot(facts, pool=False)
    snapshot.pool[facts[0].code] = facts[0]  # pool
    cache = {facts[1].code: (sp._AI_OK, ([100.0] * 240, [100.0] * 60)),  # ok
             facts[2].code: (sp._AI_HOLE, time.monotonic() + 600)}  # unknown（有洞）
    cache.update({f.code: (sp._AI_NO, r) for f, r in zip(facts[5:-1], reasons)})  # no:／skipped:
    state = _state(facts)
    state["picks"] = state["picks"][:3]
    # facts[3]：這個 T 抓失敗過（unknown）；facts[4]：候選、沒查到（unchecked）；最後一檔：不是候選（null）
    payload = hook.build_ai_spool(state, snapshot, facts[:-1], {f.code: "00952" for f in facts}, chart_cache=cache,
                                  fail_tries={facts[3].code: 1})
    data = json.loads(json.dumps(payload, ensure_ascii=False))
    labels = [m["trend"] for m in data["members"]]
    assert {"pool", "ok", "unknown", "unchecked", None, "no:bars_short", "no:price_jump", "skipped:unknown",
            "skipped:previous_day_missing", "skipped:chart_unavailable", "no:" + "a" * 32, "no:other"} <= set(labels)
    version = pool.spool_version(data, hook.FAMILY_AI, state["t"], {"rule": state["rule"],
                                                                    "computed_at": state["computed_at"]})
    assert [r.trend["trend"] for r in version.rows] == labels  # 一個都沒被判不合格，原樣收下


# ── 結構：呼叫點、沒有鎖／mkdir／連網／模組狀態 ──────────────────────────────────────
def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_call_site_is_after_commit_and_history_and_wrapped_in_try():
    tree = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
    fill = _function(tree, "_maybe_fill_ai")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "write_ai_spool"]
    assert len(calls) == 1 and calls[0] in list(ast.walk(fill))  # 整個 stock_picks.py 只有這一個呼叫點
    commit_if = next(n for n in ast.walk(fill) if isinstance(n, ast.If) and isinstance(n.test, ast.Call)
                     and getattr(n.test.func, "id", None) == "_commit_ai")
    body = commit_if.body
    history = next(i for i, s in enumerate(body) if "_append_ai_history" in ast.dump(s))
    guarded = next(i for i, s in enumerate(body) if isinstance(s, ast.Try) and "write_ai_spool" in ast.dump(s))
    assert history < guarded  # _commit_ai 回 True、_append_ai_history 之後
    handlers = body[guarded].handlers
    assert [h.type.id for h in handlers] == ["Exception"] and not body[guarded].finalbody


def test_hook_module_takes_no_locks_makes_no_dirs_and_keeps_no_state():
    tree = ast.parse(Path(hook.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}  # 只看程式裡的名字，不看說明文字
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for word in ("_state_file_lock", "_lock", "mkdir", "makedirs", "socket", "urlopen", "_get_json", "_fetch_chart",
                 "_commit_ai", "_ai_record"):
        assert word not in names, word
    assert not any(isinstance(n, (ast.Global, ast.Nonlocal)) for n in ast.walk(tree))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "json", "logging", "math", "os", "re", "stat", "time", "dataclasses", "datetime",
                        "pathlib", "typing", "stock_picks"}
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "_write_json_file"]
    assert len(writes) == 1
    for node in tree.body:  # 模組層級只有常數、logger、regex
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            assert all(n == "logger" or n.lstrip("_").isupper() for n in names), names
            assert not isinstance(node.value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp)), names
    for name in ("STATE_DIR", "AI_ENABLED", "_write_json_file", "_facts_json", "_facts_for", "ai_checks_of",
                 "trend_of", "_AI_OK", "_AI_NO", "_AI_HOLE", "_AI_SKIP_REASONS", "_ai_chart_cache", "_ai_fail_tries"):
        assert hasattr(sp, name), name


# ── 冷凍（B2，PLAN_B2C2 §2）：T 是 10/8 週四，since 是它之前的週日，until 是 since＋105 個日曆天 ─────────────
SINCE, UNTIL = "2026-10-04", "2027-01-17"  # 10/4＋27＝10/31，＋30＝11/30，＋31＝12/31，＋17＝1/17
FRIDAY, SUNDAY, MONDAY = date(2026, 10, 9), "2026-10-11", date(2026, 10, 12)
SUNDAY_UNTIL = "2027-01-24"  # 10/11＋105 天
MEMBERS = [str(1101 + i) for i in range(8)]  # 8 檔：最多拿掉 ceil(2)＝2 檔
DAY = T.isoformat()


def _frozen(code: str, since: str = SINCE, until: str = UNTIL) -> dict:
    return {"code": code, "since": since, "until": until}


def _cooldown(ai=(), us=()) -> dict:
    """回測 job 寫的格式（PLAN_B2C2 §4.4）。"""
    return {"schema": 1, "families": {"tw_ai": {"frozen": list(ai)}, "us": {"frozen": list(us)}}}


def _write_cooldown(content) -> Path:
    """bytes 原樣寫，其他寫成 JSON。"""
    path = hook.cooldown_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
    return path


def _spool_data(spool: Path) -> dict:
    [path] = _spool_files(spool)
    return json.loads(path.read_text(encoding="utf-8"))


def _boom(*_a, **_k):
    raise RuntimeError("synthetic")


@pytest.mark.parametrize("day, removed", [
    ("2026-10-03", frozenset()),  # since 前一天
    ("2026-10-04", frozenset({"1101"})),  # since 當天
    ("2027-01-17", frozenset({"1101"})),  # until 當天
    ("2027-01-18", frozenset()),  # until 隔天
])
def test_effective_only_counts_days_from_since_to_until(day, removed):
    assert hook.effective(hook.FAMILY_AI, MEMBERS, _cooldown([_frozen("1101")]), day) == (removed, None)


def test_effective_keeps_only_members_of_this_family():
    obj = _cooldown([_frozen("1101"), _frozen("9999")], [_frozen("BRK.B"), _frozen("NVDA")])
    assert hook.effective(hook.FAMILY_AI, MEMBERS, obj, DAY) == (frozenset({"1101"}), None)
    symbols = ["BRK.B", "MSFT", "AAPL", "AMZN"]
    assert hook.effective(hook.FAMILY_US, symbols, obj, DAY) == (frozenset({"BRK.B"}), None)


@pytest.mark.parametrize("size, limit", [(1, 1), (4, 1), (5, 2), (8, 2), (9, 3), (26, 7), (50, 13), (54, 14)])
def test_effective_25pct_boundary(size, limit, caplog):
    members = [str(1000 + i) for i in range(size)]
    exact = _cooldown([_frozen(c) for c in members[:limit]])
    assert hook.effective(hook.FAMILY_AI, members, exact, DAY) == (frozenset(members[:limit]), None)  # 剛好 ceil 不擋
    assert _hook_warnings(caplog) == []
    if limit == size:
        return
    over = _cooldown([_frozen(c) for c in members[:limit + 1]])
    assert hook.effective(hook.FAMILY_AI, members, over, DAY) == (frozenset(), "over_25pct")  # 多一檔整份不用
    assert _hook_warnings(caplog) == [
        f"stock picks review cooldown not applied reason=over_25pct family=tw_ai removed={limit + 1} members={size}"]


def test_effective_25pct_only_counts_what_would_really_be_removed():
    expired = [_frozen(c, "2026-01-04", "2026-04-19") for c in MEMBERS[2:6]]  # 上一輪的冷凍：已經過期
    outsiders = [_frozen(c) for c in ("9001", "9002", "9003")]  # 已經不在名單裡
    obj = _cooldown([_frozen("1101"), _frozen("1102"), *expired, *outsiders])
    assert hook.effective(hook.FAMILY_AI, MEMBERS, obj, DAY) == (frozenset({"1101", "1102"}), None)


def test_read_cooldown_missing_is_normal(caplog):
    assert hook.read_cooldown() == ("missing", None)  # 上一層也還沒有
    hook.cooldown_path().parent.mkdir(mode=0o700)
    assert hook.read_cooldown() == ("missing", None)
    assert hook.apply_cooldown(hook.FAMILY_AI, MEMBERS, DAY) == hook.Applied("missing", frozenset(), None)
    assert _hook_warnings(caplog) == [] and not hook.cooldown_path().exists()


def test_read_cooldown_accepts_a_good_file(caplog):
    obj = _cooldown([_frozen("2330"), _frozen("2454", "2026-10-04", "2026-10-04")], [_frozen("BRK.B")])  # 105 天、同一天
    _write_cooldown(obj)
    assert hook.read_cooldown() == ("ok", obj)
    assert _hook_warnings(caplog) == []


def test_read_cooldown_on_a_fifo_does_not_block(caplog):
    path = hook.cooldown_path()
    path.parent.mkdir(mode=0o700)
    os.mkfifo(path)
    got: list = []
    reader = threading.Thread(target=lambda: got.append(hook.read_cooldown()), daemon=True)
    reader.start()
    reader.join(5)
    stuck = reader.is_alive()
    if stuck:  # 開一個寫端放走卡住的 open：失敗時也不留執行緒，更不會卡住整套測試
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(5)
    assert not stuck and got == [("invalid", None)]
    assert _hook_warnings(caplog) == ["stock picks review cooldown invalid reason=ValueError"]


def test_read_cooldown_refuses_symlinks_and_directories(tmp_path, caplog):
    elsewhere = tmp_path / "cooldown.json"
    elsewhere.write_text(json.dumps(_cooldown([_frozen("1101")])), encoding="utf-8")
    path = hook.cooldown_path()
    path.parent.mkdir(mode=0o700)
    path.symlink_to(elsewhere)  # 內容是好的也不跟
    assert hook.read_cooldown() == ("invalid", None)
    path.unlink()
    path.symlink_to(tmp_path / "nowhere")  # 斷掉的 symlink 不是「檔案不存在」
    assert hook.read_cooldown() == ("invalid", None)
    path.unlink()
    path.mkdir()
    assert hook.read_cooldown() == ("invalid", None)
    assert len(_hook_warnings(caplog)) == 3


def test_read_cooldown_refuses_someone_elses_file():
    _write_cooldown(_cooldown([_frozen("1101")]))
    real_uid = os.getuid()
    with pytest.MonkeyPatch.context() as patch:  # 只在這一次呼叫裡假裝 uid 不同
        patch.setattr(os, "getuid", lambda: real_uid + 1)
        assert hook.read_cooldown() == ("invalid", None)


def test_read_cooldown_size_limit_is_64_kb():
    body = json.dumps(_cooldown([_frozen("1101")])).encode("utf-8")
    _write_cooldown(body + b" " * (64 * 1024 - len(body)))  # 剛好 64 KB
    assert hook.read_cooldown()[0] == "ok"
    _write_cooldown(body + b" " * (64 * 1024 - len(body) + 1))  # 多 1 byte
    assert hook.read_cooldown() == ("invalid", None)


@pytest.mark.parametrize("content", [
    b"", b"{", b"\xff\xfe", b"[" * (64 * 1024), b"[]", b"null", b"1", b'"tw_ai"',
], ids=["empty", "broken", "not_utf8", "too_deep", "list", "null", "number", "string"])
def test_read_cooldown_bad_json_is_invalid(content, caplog):
    _write_cooldown(content)
    assert hook.read_cooldown() == ("invalid", None)
    assert len(_hook_warnings(caplog)) == 1


@pytest.mark.parametrize("mutate", [
    lambda o: o.update(schema=2),
    lambda o: o.pop("schema"),
    lambda o: o.pop("families"),
    lambda o: o.update(families=[]),
    lambda o: o["families"].pop("us"),
    lambda o: o["families"].update(us=[]),
    lambda o: o["families"]["us"].pop("frozen"),
    lambda o: o["families"]["us"].update(frozen={}),
    lambda o: o["families"]["us"]["frozen"].append("NVDA"),
    lambda o: o["families"].update(tw_v1={"frozen": 1}),
    lambda o: o["families"]["us"]["frozen"][0].pop("code"),
    lambda o: o["families"]["us"]["frozen"][0].update(code=2330),
    lambda o: o["families"]["us"]["frozen"][0].pop("since"),
    lambda o: o["families"]["us"]["frozen"][0].pop("until"),
    lambda o: o["families"]["us"]["frozen"][0].update(since=20261004),
    lambda o: o["families"]["us"]["frozen"][0].update(since="20261004"),  # fromisoformat 收得下，字串比較會錯：要 _ISO_RE 擋
    lambda o: o["families"]["us"]["frozen"][0].update(since="2026-10-04T00:00:00"),
    lambda o: o["families"]["us"]["frozen"][0].update(until="2026-13-01"),
    lambda o: o["families"]["us"]["frozen"][0].update(until="2026-11-31"),
    lambda o: o["families"]["us"]["frozen"][0].update(until="2026-10-03"),  # until 在 since 之前
    lambda o: o["families"]["us"]["frozen"][0].update(until="2027-01-18"),  # 106 天
])
def test_any_bad_field_invalidates_the_whole_file(mutate, caplog):
    obj = _cooldown([_frozen("1101")], [_frozen("NVDA")])
    mutate(obj)
    _write_cooldown(obj)
    assert hook.read_cooldown() == ("invalid", None)
    assert hook.apply_cooldown(hook.FAMILY_AI, MEMBERS, DAY) == hook.Applied("invalid", frozenset(), None)  # 壞在美股：AI 也不用
    assert len(_hook_warnings(caplog)) == 2  # 每次一個 warning（原因是 content，日期不存在時是 ValueError）


def test_apply_cooldown_reports_what_it_did():
    _write_cooldown(_cooldown([_frozen("1102"), _frozen("1101")]))
    assert hook.apply_cooldown(hook.FAMILY_AI, MEMBERS, DAY) == hook.Applied("ok", frozenset({"1101", "1102"}), None)
    assert hook.apply_cooldown(hook.FAMILY_AI, MEMBERS, "2026-10-03") == hook.Applied("ok", frozenset(), None)
    _write_cooldown(_cooldown([_frozen(c) for c in MEMBERS[:3]]))
    assert hook.apply_cooldown(hook.FAMILY_AI, MEMBERS, DAY) == hook.Applied("ok", frozenset(), "over_25pct")


@pytest.mark.parametrize("name", ["read_cooldown", "effective", "cooldown_path"])
def test_apply_cooldown_never_raises(name, monkeypatch, caplog):
    _write_cooldown(_cooldown([_frozen("1101")]))
    monkeypatch.setattr(hook, name, _boom)
    assert hook.apply_cooldown(hook.FAMILY_AI, MEMBERS, DAY) == hook.Applied("error", frozenset(), None)
    assert _hook_warnings(caplog) == ["stock picks review cooldown failed error_type=RuntimeError"]
    assert "synthetic" not in caplog.text


def test_frozen_codes_leave_the_round_and_stay_in_the_spool(monkeypatch, clock):
    spool = _make_spool()
    rig = Rig(monkeypatch, clock, 10)
    rig.snapshot.pool["1101"] = _facts("1101")  # 主池有走勢：冷凍股的條件值照算
    _write_cooldown(_cooldown([_frozen("1101"), _frozen("1103")]))  # 10 檔最多 ceil(2.5)＝3 檔
    state = rig.run()
    assert rig.picks == ["1102", "1104", "1105"]
    assert rig.yahoo.calls == ["1102.TW", "1104.TW", "1105.TW"]  # 冷凍股連日線都不抓
    assert state["funnel"] == {"universe": 8, "growth": 8, "cheap": 8, "trend": 3}
    codes = ["1102", "1104", "1105", "1106", "1107", "1108", "1109", "1110"]
    assert state["universe"]["codes"] == codes
    assert state["universe"]["etf_codes"] == [f.code for f in rig.facts]  # 名單來源的紀錄不動
    assert sp.load_ai_file()["picks"] == state["picks"]  # ai_state_from_json 收得下，真的寫到磁碟
    assert sp.ai_state_from_json(json.loads(json.dumps(state)))["universe"]["codes"] == codes
    data = _spool_data(spool)
    assert (data["cooldown_read"], data["cooldown"]) == ("ok", {"removed": ["1101", "1103"], "reason": None})
    assert data["funnel"] == state["funnel"] and data["picks"] == ["1102", "1104", "1105"]
    assert [m["c"] for m in data["members"]] == [f.code for f in rig.facts]  # 冷凍前的全名單、原本的順序
    members = {m["c"]: m for m in data["members"]}
    assert [c for c, m in members.items() if m["status"] == "frozen"] == ["1101", "1103"]
    assert all(m["status"] == "member" for c, m in members.items() if c not in ("1101", "1103"))
    assert [members[c]["rank"] for c in codes] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [c for c, m in members.items() if m["picked"]] == ["1102", "1104", "1105"]
    first, third = members["1101"], members["1103"]
    assert (first["rank"], first["picked"], third["rank"], third["picked"]) == (None, False, None, False)
    assert (first["n"], first["c0"], first["ai_source"]) == ("股1101", 100.0, "00952")
    assert first["facts"]["c"] == "1101" and first["trend"] == "pool" and first["tr"] == [100.0, 100.0, 90.0, 0]
    assert first["checks"] == {"ai": True, "growth": True, "cheap": True, "trend": True}
    assert third["trend"] is None and third["tr"] is None  # 不是候選、主池也沒有：bot 沒看它的走勢
    assert third["checks"] == {"ai": True, "growth": True, "cheap": True, "trend": None}


def test_frozen_codes_are_not_reported_as_skipped(monkeypatch, clock):  # 每日維護把 ai_skipped 當成「日線拿不到」
    rig = Rig(monkeypatch, clock, 4)
    sp._ai_cache_t = rig.snapshot.t
    sp._ai_chart_cache["1101"] = (sp._AI_NO, "chart_unavailable")
    _write_cooldown(_cooldown([_frozen("1101")]))
    assert rig.run()["skipped"] == {}


def test_over_25pct_leaves_the_round_alone_and_says_so(monkeypatch, clock):
    spool = _make_spool()
    rig = Rig(monkeypatch, clock, 8)
    _write_cooldown(_cooldown([_frozen(c) for c in ("1101", "1102", "1103")]))  # 8 檔上限 2 檔
    state = rig.run()
    assert rig.picks == ["1101", "1102", "1103"]
    assert state["funnel"] == {"universe": 8, "growth": 8, "cheap": 8, "trend": 3}
    data = _spool_data(spool)
    assert (data["cooldown_read"], data["cooldown"]) == ("ok", {"removed": [], "reason": "over_25pct"})
    assert {m["status"] for m in data["members"]} == {"member"}


def test_invalid_file_leaves_the_round_alone_and_says_so(monkeypatch, clock):
    spool = _make_spool()
    rig = Rig(monkeypatch, clock, 4)
    _write_cooldown(b"{")
    state = rig.run()
    assert rig.picks == ["1101", "1102", "1103"] and state["fail_count"] == 0
    assert (_spool_data(spool)["cooldown_read"], _spool_data(spool)["cooldown"]) == (
        "invalid", {"removed": [], "reason": None})


@pytest.mark.parametrize("failure, read, cooldown", [
    ("apply_cooldown", None, None),  # 呼叫丟例外：applied 是 None，兩個欄位維持 null
    ("read_cooldown", "error", {"removed": [], "reason": None}),  # apply_cooldown 自己接住
])
def test_hook_errors_do_not_change_the_recommendation(failure, read, cooldown, monkeypatch, clock):
    spool = _make_spool()
    rig = Rig(monkeypatch, clock, 4)
    _write_cooldown(_cooldown([_frozen("1101")]))  # 檔案其實要拿掉 1101
    monkeypatch.setattr(hook, failure, _boom)
    state = rig.run()
    assert rig.picks == ["1101", "1102", "1103"] and state["fail_count"] == 0 and sp._ai_next_try == 0.0
    assert state["funnel"] == {"universe": 4, "growth": 4, "cheap": 4, "trend": 3}
    data = _spool_data(spool)
    assert (data["cooldown_read"], data["cooldown"]) == (read, cooldown)
    assert [m["c"] for m in data["members"]] == ["1101", "1102", "1103", "1104"]
    assert {m["status"] for m in data["members"]} == {"member"}


def test_without_a_cooldown_file_the_round_is_what_it_was_before_b2(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 10)
    state = copy.deepcopy(rig.run())
    reply = sp._ai_picks_reply(sp._snapshot)
    assert [p["c"] for p in state["picks"]] == ["1101", "1102", "1103"]
    assert state["funnel"] == {"universe": 10, "growth": 10, "cheap": 10, "trend": 3}
    assert state["universe"]["codes"] == [f.code for f in rig.facts]
    monkeypatch.setitem(sys.modules, "stock_picks_review_hook", None)  # hook 不在＝B2 之前的程式
    rig.version = "test-v2"  # 名單換版：同一個 T 再算一次
    before = copy.deepcopy(rig.run())
    assert before["core_version"] == "test-v2"
    same = ("t", "rule", "universe", "picks", "funnel", "fail_count", "core_as_of", "skipped")
    assert {k: before[k] for k in same} == {k: state[k] for k in same}
    assert sp._ai_picks_reply(sp._snapshot) == reply


def _on_day(rig: Rig, monkeypatch, day: date) -> None:
    """主快照換成 day 這個資料日（Rig 的主快照是 T；走勢都在主池，換日不用多餵日線）。"""
    rig.snapshot = replace(rig.snapshot, t=day, calendar=_calendar(day))
    monkeypatch.setattr(sp, "_snapshot", rig.snapshot)


def test_a_same_t_recompute_does_not_take_back_what_the_family_saw(monkeypatch, clock):
    """PLAN_B2C2 §2.1：T＝週五、週日凍結、週一清晨同 T 重算，X 還在；T＝週一才拿掉。"""
    rig = Rig(monkeypatch, clock, 4, pool=True)  # 4 檔最多 ceil(1)＝1 檔
    universe = sp._ai_universe

    def no_holdings_today(snapshot, give_up):  # 00952 當天沒抓到：holdings_due 讓同一個 T 一直重算
        u, sources, core_as_of, version = universe(snapshot, give_up)
        return {**u, "source": "core_only"}, sources, core_as_of, version

    monkeypatch.setattr(sp, "_ai_universe", no_holdings_today)
    _on_day(rig, monkeypatch, FRIDAY)
    rig.run()
    assert rig.picks == ["1101", "1102", "1103"] and rig.universe_calls == 1  # 週六家人看到的
    _write_cooldown(_cooldown([_frozen("1101", SUNDAY, SUNDAY_UNTIL)]))  # 週日 20:00 job 凍 1101
    clock.advance(sp.AI_RETRY_S + 1)
    rig.run()
    assert rig.universe_calls == 2 and sp._ai_state["t"] == FRIDAY.isoformat()  # 真的重算了
    assert rig.picks == ["1101", "1102", "1103"]
    assert _history_days() == [FRIDAY.isoformat()]  # picks 沒變，推薦紀錄不多一版，成績帳不會改取新的 rev
    _on_day(rig, monkeypatch, MONDAY)
    clock.advance(sp.AI_RETRY_S + 1)
    rig.run()
    assert sp._ai_state["t"] == MONDAY.isoformat() and rig.picks == ["1102", "1103", "1104"]
    assert sp._ai_state["funnel"]["universe"] == 3
    assert _history_days() == [FRIDAY.isoformat(), MONDAY.isoformat()]


def test_contract_a_spool_with_frozen_members_is_accepted_by_the_job(monkeypatch, clock):
    pool = _job("stock_picks_review_pool")
    spool = _job_spool(monkeypatch)
    rig = Rig(monkeypatch, clock, 10)
    _write_cooldown(_cooldown([_frozen("1101"), _frozen("1103")]))
    state = rig.run()
    [path] = _spool_files(spool)
    version = _job_accepts(pool, path, state)  # bot 真的寫出來的位元組，原樣交給 job
    assert version.cooldown_read == "ok" and version.meta["funnel"]["universe"] == 8
    assert [r.code for r in version.rows] == [f.code for f in rig.facts]  # 10 檔都在
    frozen = [r for r in version.rows if r.status == pool.FROZEN]
    assert [(r.code, r.c0, r.rank, r.picked) for r in frozen] == [("1101", 100.0, None, False), ("1103", 100.0, None, False)]
    assert [r.code for r in version.rows if r.picked] == ["1102", "1104", "1105"]
