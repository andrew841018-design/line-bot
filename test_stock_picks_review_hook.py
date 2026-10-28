"""stock_picks_review_hook（回測週報的 AI 池子 spool，2026-10-29 回測 PLAN §5.2）的測試：寫入時機（_commit_ai 回 True 之後
才寫）、例外不外漏、目錄檢查、檔數上限、100 ms、AI 關著不做事、unknown 和 no 分開記。合成行情、tmp state、不連網。"""

from __future__ import annotations

import ast
import json
import os
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

import stock_picks as sp
import stock_picks_review_hook as hook
from test_stock_picks_ai import T, TODAY, _facts, _snapshot, _state
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
    assert (data["cooldown_read"], data["cooldown"]) == (None, None)  # B2 才有
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
    assert imported <= {"__future__", "logging", "math", "os", "re", "stat", "time", "dataclasses", "pathlib",
                        "typing", "stock_picks"}
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
