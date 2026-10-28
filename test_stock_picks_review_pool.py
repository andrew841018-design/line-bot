"""股票推薦回測 A 段：封存與三族池子紀錄（jobs/stock_picks_review_pool.py）。

全部用合成、縮小過的資料（test_stock_picks_review_fixtures），bot 目錄與 outbox 由 conftest 指到暫存目錄。
不連網：盤勢 DB 是測試自己建的小 sqlite。
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3

import pytest

import stock_picks as sp
import test_stock_picks_review_fixtures as fx
from jobs import stock_picks_review as runner
from jobs import stock_picks_review_pool as pool


@pytest.fixture
def paths() -> runner.Paths:
    p = runner.resolve_paths()
    assert p.bot_dir != runner.BASE and p.maintenance != runner.DEFAULT_MAINTENANCE  # conftest 指到暫存目錄
    runner.prepare_root(p)  # 和第一輪一樣：root 與 spool/
    return p


def _ctx(paths: runner.Paths, now: float = fx.NOW, **kwargs) -> runner.Context:
    return runner.Context(paths=paths, now=now, **kwargs)


def _run_pool(paths: runner.Paths, now: float = fx.NOW) -> dict:
    ctx = _ctx(paths, now)
    try:
        pool.archive_stage(ctx)
        return pool.pool_stage(ctx)
    finally:
        ctx.close()


def _rows(paths: runner.Paths, where: str = "1", params: tuple = ()) -> list[sqlite3.Row]:
    con = sqlite3.connect(paths.db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(f"SELECT * FROM pool_rows WHERE {where} ORDER BY m, rule, t, ver_s, code", params).fetchall()
    finally:
        con.close()


def _days(paths: runner.Paths) -> list[sqlite3.Row]:
    con = sqlite3.connect(paths.db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM pool_days ORDER BY m, rule, t, ver_s").fetchall()
    finally:
        con.close()


# ── 封存 ───────────────────────────────────────────────────────────────────
def test_archive_stores_full_original_once_per_relevant_content(paths):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    raw_ai = (paths.state_dir / "stock_picks_tw_ai.json").read_bytes()
    ctx = _ctx(paths)
    first = pool.archive_stage(ctx)
    assert (first["tw"], first["tw_ai"], first["us"]) == ("new", "new", "new")
    files = pool.archived_files(paths.archive)
    assert sorted(name for name, _, _ in files) == ["stock_picks_tw", "stock_picks_tw_ai", "stock_picks_us"]
    ai_file = next(p for name, _, p in files if name == "stock_picks_tw_ai")
    assert ai_file.parent.name == "2026" and ai_file.name.startswith("stock_picks_tw_ai-2026-10-27-")
    assert gzip.decompress(ai_file.read_bytes()) == raw_ai  # 整份原檔
    assert first["files"] == 3 and first["bytes"] > 0
    second = pool.archive_stage(_ctx(paths))
    assert (second["tw"], second["tw_ai"], second["us"]) == ("same", "same", "same")


def test_archive_key_ignores_fields_that_do_not_change_the_recommendation(paths):
    fx.write_states(paths.state_dir, ai=fx.ai_state(), us=fx.us_snapshot())
    pool.archive_stage(_ctx(paths))
    # 等待中的重試（last_try、fetch_budget）與美股只換最新收盤（refresh_only）：不是新的一版
    state = fx.ai_state(last_try=fx.AI_AT + 3600)
    state["fetch_budget"]["attempts"] = 2
    fx.write_states(paths.state_dir, ai=state, us=fx.us_snapshot(latest_bump=1.5))
    again = pool.archive_stage(_ctx(paths))
    assert (again["tw_ai"], again["us"]) == ("same", "same")
    # 換推薦就是新的一版
    fx.write_states(paths.state_dir, ai=fx.ai_state(picks=fx.AI_PICKS[:2]))
    assert pool.archive_stage(_ctx(paths))["tw_ai"] == "new"


def test_archive_reads_only_own_regular_files(paths):
    target = paths.state_dir.parent / "elsewhere.json"
    fx.write_json(target, fx.main_snapshot())
    os.symlink(target, paths.state_dir / "stock_picks_tw.json")
    out = pool.archive_stage(_ctx(paths))
    assert out["tw"] == "invalid" and out["tw_ai"] == "missing"
    assert pool.archived_files(paths.archive) == []


def test_archive_keeps_three_years(paths):
    old_dir = paths.archive / "2023"
    for directory in (paths.archive, old_dir):
        runner.ensure_private_dir(directory)
    expired = old_dir / "stock_picks_tw_ai-2023-10-01-0123abcd.json.gz"
    kept = old_dir / "stock_picks_tw_ai-2023-12-01-0123abcd.json.gz"
    for path in (expired, kept):
        path.write_bytes(gzip.compress(b"{}"))
    out = pool.archive_stage(_ctx(paths))
    assert out["pruned"] == 1 and not expired.exists() and kept.exists()


# ── 池子紀錄：三族 ────────────────────────────────────────────────────────────
@pytest.fixture
def market_db(paths):
    return fx.make_market_db(paths.market_db, fx.standard_market_rows())


def test_three_families_are_logged_with_c0_for_every_member(paths, market_db):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    out = _run_pool(paths)
    assert out["tw_v1"] == "new" and out["us"] == "new"
    assert out["tw_ai"]["new"] == 1 and out["last_t"] == {"tw/v1": "2026-10-27", "tw/ai": "2026-10-27", "us": "2026-10-27"}

    v1 = _rows(paths, "rule = 'v1'")
    assert [r["code"] for r in v1] == sorted(fx.MAIN_UNIVERSE) and len(v1) == 50
    assert "0050" not in {r["code"] for r in v1}  # 常問股卡多放的 0050 不是成員
    assert all(r["status"] == "member" and r["c0"] == fx.close_of(r["code"]) for r in v1)
    picked = {r["code"]: r["rank"] for r in v1 if r["picked"]}
    assert picked == {code: i + 1 for i, code in enumerate(fx.V1_PICKS)}
    flags = json.loads(v1[0]["flags_json"])
    assert set(flags) == {"long", "mid", "growth", "cheap", "inst"}
    assert json.loads(v1[0]["values_json"])["c"] == v1[0]["code"]

    us_rows = {r["code"]: r for r in _rows(paths, "m = 'us'")}
    assert len(us_rows) == 50 and "BRK.B" in us_rows
    assert set(json.loads(us_rows["AAX"]["flags_json"])) == {"long", "mid", "growth", "profit"}
    gray = us_rows[fx.US_GRAY_WITH_LATEST]
    assert gray["status"] == "member" and gray["c0"] == 110.0  # DB 沒有 D 收盤：用快照 latest_closes 裡日期等於 D 的那筆
    assert json.loads(gray["trend_json"]) == {"trend": "unknown", "tr": None, "gray": "missing"}
    nothing = us_rows[fx.US_GRAY_NO_CLOSE]
    assert nothing["status"] == "excluded:no_close" and nothing["c0"] is None  # 不編數字
    days = {(d["m"], d["rule"]): d for d in _days(paths)}
    us_meta = json.loads(days[("us", "us_v1")]["meta_json"])
    assert (us_meta["c0_db"], us_meta["c0_latest"]) == (48, 1)
    assert days[("us", "us_v1")]["n_members"] == 49 and days[("us", "us_v1")]["n_picks"] == 3
    assert us_meta["picks_match"] is True and us_meta["candidates"] == 48 and gray["rank"] is None
    assert json.loads(days[("tw", "v1")]["meta_json"])["picks_match"] is True
    assert all(d["src"] == "simple" and d["derive_version"] == pool.DERIVE_VERSION for d in days.values())
    assert days[("tw", "v1")]["ver_s"] == round(fx.MAIN_AT) and days[("tw", "ai_v1")]["ver_s"] == round(fx.AI_AT)
    assert len(days[("tw", "ai_v1")]["bot_sha"]) == 64


def test_ai_simple_matches_real_ratio_and_never_infers_trend(paths, market_db):
    """真實 10/27：26 檔裡 8 檔不在主池（沒有 tr）；c0 一律取 listed 收盤，走勢只記主池與入選的。"""
    skipped = {fx.AI_NOT_IN_POOL[0]: "chart_unavailable"}
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(skipped=skipped))
    _run_pool(paths)
    rows = {r["code"]: r for r in _rows(paths, "rule = 'ai_v1'")}
    assert len(rows) == 26
    assert all(r["status"] == "member" and r["c0"] == fx.close_of(code) for code, r in rows.items())
    trends = {code: json.loads(r["trend_json"]) for code, r in rows.items()}
    assert sum(1 for t in trends.values() if t["tr"] is None) == 8
    assert {code for code, t in trends.items() if t["trend"] is None} == set(fx.AI_NOT_IN_POOL)
    assert all(trends[code]["trend"] == "pool" for code in fx.AI_IN_POOL)  # 入選的也在主池：bot 用的是主池的走勢
    unknown = json.loads(rows[fx.AI_NOT_IN_POOL[1]]["flags_json"])
    assert unknown == {"ai": True, "growth": True, "cheap": True, "trend": None}  # 不知道，不是 False
    assert {code: r["rank"] for code, r in rows.items() if r["picked"]} == {"2301": 1, "2302": 2, "2303": 3}
    # rank＝條件 1–3 候選照 bot 排序（ai_candidates）的名次，和 B1 spool 同義：走勢不知道的候選也有名次
    assert sorted(r["rank"] for r in rows.values()) == list(range(1, 27))
    values = json.loads(rows[fx.AI_NOT_IN_POOL[0]]["values_json"])
    assert values["skipped"] == "chart_unavailable" and values["ai_source"] == "core" and values["tr"] is None
    assert json.loads(rows["2301"]["values_json"])["ai_source"] == fx.SEGMENT
    day = _days(paths)[0] if _days(paths)[0]["rule"] == "ai_v1" else [d for d in _days(paths) if d["rule"] == "ai_v1"][0]
    meta = json.loads(day["meta_json"])
    assert meta["pick_mismatch"] == 0 and meta["funnel_match"] is True
    assert meta["main_origin"] == "current" and meta["main_ver_s"] == round(fx.MAIN_AT)
    assert day["source"] == "core_only" and day["core_version"] == "test-core-v1" and day["n_members"] == 26


def test_pick_outside_the_main_pool_records_ok_with_the_picks_trend(paths, market_db):
    picks = [fx.AI_IN_POOL[0], fx.AI_NOT_IN_POOL[0]]
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(picks=picks))
    _run_pool(paths)
    row = _rows(paths, "rule = 'ai_v1' AND code = ?", (fx.AI_NOT_IN_POOL[0],))[0]
    assert json.loads(row["trend_json"]) == {"trend": "ok", "tr": [100.0, 100.0, 90.0, 0]}
    assert json.loads(row["flags_json"])["trend"] is True and row["rank"] == 19 and row["picked"] == 1


def test_ai_pairs_with_same_t_main_snapshot_from_archive(paths, market_db):
    # 第一輪：主快照與 AI 都是 T＝10/27 → 封存了主快照(10/27)
    fx.write_states(paths.state_dir, main=fx.main_snapshot())
    _run_pool(paths)
    # 主快照換到 10/28，AI 才算出 10/27：現行的對不上，要從封存配
    fx.write_states(paths.state_dir, main=fx.main_snapshot(fx.T_NEXT, computed_at=fx.MAIN_AT + 86400),
                    ai=fx.ai_state(computed_at=fx.MAIN_AT + 86400 + 60))
    out = _run_pool(paths, now=fx.MAIN_AT + 86400 + 3600)
    assert out["tw_ai"]["new"] == 1
    day = [d for d in _days(paths) if d["rule"] == "ai_v1"][0]
    meta = json.loads(day["meta_json"])
    assert day["t"] == "2026-10-27" and meta["main_origin"] == "archive" and meta["main_ver_s"] == round(fx.MAIN_AT)


def test_ai_without_any_same_t_main_snapshot_is_counted_not_logged(paths, market_db):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(fx.T_NEXT, computed_at=fx.MAIN_AT + 86400), ai=fx.ai_state())
    out = _run_pool(paths, now=fx.MAIN_AT + 90000)
    assert out["tw_ai"]["pair_missing"] == 1 and out["tw_ai"]["new"] == 0
    assert not [d for d in _days(paths) if d["rule"] == "ai_v1"]


def test_pairing_prefers_latest_main_not_after_the_ai_version(paths, market_db):
    early = fx.main_snapshot(computed_at=fx.AI_AT - 3600)
    late = fx.main_snapshot(computed_at=fx.AI_AT + 3600)  # AI 算完之後才重算的主快照：bot 當時手上是 early
    fx.write_states(paths.state_dir, main=early)
    _run_pool(paths, now=fx.AI_AT - 1800)
    fx.write_states(paths.state_dir, main=late, ai=fx.ai_state())
    _run_pool(paths, now=fx.AI_AT + 7200)
    meta = json.loads([d for d in _days(paths) if d["rule"] == "ai_v1"][0]["meta_json"])
    assert meta["main_origin"] == "archive" and meta["main_ver_s"] == round(fx.AI_AT - 3600)


def test_pool_log_is_idempotent(paths, market_db):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    _run_pool(paths)
    before = [tuple(r) for r in _rows(paths)]
    days_before = [tuple(d) for d in _days(paths)]
    out = _run_pool(paths, now=fx.NOW + 1800)
    assert out["tw_v1"] == "exists" and out["us"] == "exists" and out["tw_ai"]["exists"] == 1
    assert [tuple(r) for r in _rows(paths)] == before and [tuple(d) for d in _days(paths)] == days_before


def test_new_ai_version_waits_ten_minutes(paths, market_db):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state())
    out = _run_pool(paths, now=fx.AI_AT + 599)
    assert out["tw_ai"]["waiting"] == 1 and out["tw_ai"]["new"] == 0
    out = _run_pool(paths, now=fx.AI_AT + 601)
    assert out["tw_ai"]["new"] == 1


def test_version_replaced_within_grace_is_logged_later_from_the_archive(paths, market_db):
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state())
    _run_pool(paths, now=fx.AI_AT + 60)  # 第一版還在寬限內，只有封存
    second = fx.ai_state(computed_at=fx.AI_AT + 300, picks=fx.AI_PICKS[:2])
    fx.write_states(paths.state_dir, ai=second)
    out = _run_pool(paths, now=fx.AI_AT + 3600)
    assert out["tw_ai"]["new"] == 2  # 現行那版＋封存裡被蓋掉的那版
    assert sorted(d["ver_s"] for d in _days(paths) if d["rule"] == "ai_v1") == [round(fx.AI_AT), round(fx.AI_AT + 300)]


def _spool(state: dict, main: dict) -> dict:
    listed = main["listed"]
    members = []
    for i, code in enumerate(state["universe"]["codes"]):
        members.append({"c": code, "n": f"股{code}", "c0": listed[code][3], "status": "member",
                        "rank": i + 1 if i < 7 else None, "picked": i < 3,
                        "trend": "pool" if code in fx.AI_IN_POOL else "no:jump", "tr": [100.0, 100.0, 90.0, 0],
                        "checks": {"ai": True, "growth": True, "cheap": True, "trend": True},
                        "facts": {"c": code}, "ai_source": fx.SEGMENT})
    return {"schema": 1, "family": "tw_ai", "t": state["t"], "rule": state["rule"], "computed_at": state["computed_at"],
            "source": "core_only", "core_version": "test-core-v1", "funnel": state["funnel"], "members": members}


def test_spool_replaces_the_simple_version_and_nothing_else(paths, market_db):
    main, state = fx.main_snapshot(), fx.ai_state()
    fx.write_states(paths.state_dir, main=main, ai=state)
    _run_pool(paths)
    assert [d["src"] for d in _days(paths) if d["rule"] == "ai_v1"] == ["simple"]
    name = f"tw_ai-{state['t']}-{state['computed_at']!r}.json"
    fx.write_json(paths.spool / name, _spool(state, main))
    out = _run_pool(paths, now=fx.NOW + 600)
    assert out["spool"]["replaced"] == 1
    ai_days = [d for d in _days(paths) if d["rule"] == "ai_v1"]
    assert [d["src"] for d in ai_days] == ["spool"] and ai_days[0]["ver_s"] == round(fx.AI_AT)
    trend = json.loads(_rows(paths, "rule = 'ai_v1' AND code = ?", (fx.AI_NOT_IN_POOL[0],))[0]["trend_json"])
    assert trend["trend"] == "no:jump"
    assert not (paths.spool / name).exists() and (paths.spool / "done" / name).exists()
    # 同一版的 spool 又來一次：spool 不會被覆寫
    fx.write_json(paths.spool / name, _spool(state, main))
    assert _run_pool(paths, now=fx.NOW + 1200)["spool"]["exists"] == 1


def test_simple_version_is_skipped_when_its_spool_exists(paths, market_db):
    main, state = fx.main_snapshot(), fx.ai_state()
    fx.write_states(paths.state_dir, main=main, ai=state)
    runner.ensure_private_dir(paths.spool)
    fx.write_json(paths.spool / f"tw_ai-{state['t']}-{state['computed_at']!r}.json", _spool(state, main))
    out = _run_pool(paths, now=fx.AI_AT + 30)  # 寬限內 spool 就到了：直接用 spool
    assert out["spool"]["new"] == 1 and out["tw_ai"]["exists"] == 1
    assert [d["src"] for d in _days(paths) if d["rule"] == "ai_v1"] == ["spool"]


def test_spool_that_matches_no_committed_version_is_not_written(paths, market_db):
    main, state = fx.main_snapshot(), fx.ai_state()
    fx.write_states(paths.state_dir, main=main, ai=state)
    runner.ensure_private_dir(paths.spool)
    other = dict(state, computed_at=state["computed_at"] + 42)  # 部署重疊時另一個程序算的、沒有 commit 的那份
    name = f"tw_ai-{other['t']}-{other['computed_at']!r}.json"
    fx.write_json(paths.spool / name, _spool(other, main))
    out = _run_pool(paths)
    assert out["spool"]["unmatched"] == 1 and (paths.spool / name).exists()
    assert [d["src"] for d in _days(paths) if d["rule"] == "ai_v1"] == ["simple"]
    # 放超過一天（看檔案真正的 mtime）還是對不上：移到 done/，不再每輪重看
    old = os.stat(paths.spool / name).st_mtime - pool.SPOOL_UNMATCHED_GRACE_S - 60
    os.utime(paths.spool / name, (old, old))
    assert _run_pool(paths)["spool"]["unmatched"] == 1
    assert not (paths.spool / name).exists() and (paths.spool / "done" / (name + ".unmatched")).exists()


def test_imported_spools_in_done_do_not_hold_back_the_simple_version(paths, market_db):
    """done/ 裡匯入過的 spool 不算「還在等」：review.sqlite 重建時，那一版照樣記簡版（不會一直等）。"""
    main, state = fx.main_snapshot(), fx.ai_state()
    fx.write_states(paths.state_dir, main=main, ai=state)
    runner.ensure_private_dir(paths.spool / "done")
    fx.write_json(paths.spool / "done" / f"tw_ai-{state['t']}-{round(state['computed_at'])}.json", _spool(state, main))
    assert not pool.spool_exists(paths.spool, "tw_ai", state["t"], round(state["computed_at"]))
    assert _run_pool(paths)["tw_ai"]["new"] == 1
    assert [d["src"] for d in _days(paths) if d["rule"] == "ai_v1"] == ["simple"]


def test_spool_named_with_whole_seconds_is_accepted(paths, market_db):
    """B1 的 hook 用 round(computed_at) 命名（tw_ai-<T>-<秒>.json）。"""
    main, state = fx.main_snapshot(), fx.ai_state()
    fx.write_states(paths.state_dir, main=main, ai=state)
    fx.write_json(paths.spool / f"tw_ai-{state['t']}-{round(state['computed_at'])}.json", _spool(state, main))
    out = _run_pool(paths)
    assert out["spool"]["new"] == 1 and out["tw_ai"]["exists"] == 1


def test_invalid_spool_is_moved_aside(paths, market_db):
    main, state = fx.main_snapshot(), fx.ai_state()
    fx.write_states(paths.state_dir, main=main, ai=state)
    runner.ensure_private_dir(paths.spool)
    bad = _spool(state, main)
    bad["members"][0]["c0"] = -1
    name = f"tw_ai-{state['t']}-{state['computed_at']!r}.json"
    fx.write_json(paths.spool / name, bad)
    out = _run_pool(paths)
    assert out["spool"]["invalid"] == 1 and (paths.spool / "done" / (name + ".invalid")).exists()
    assert [d["src"] for d in _days(paths) if d["rule"] == "ai_v1"] == ["simple"]


def test_member_without_c0_is_refused_by_code_and_schema(paths):
    version = pool.Version(m="tw", rule="v1", t="2026-10-27", computed_at=fx.MAIN_AT, family=pool.FAMILY_V1,
                           rows=[pool.Row("2330", "台積電", None, False, None, "member", {}, {}, None)])
    ctx = _ctx(paths)
    conn = ctx.db()
    with pytest.raises(ValueError):
        pool.write_version(conn, version, now=fx.NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO pool_rows (m, rule, t, ver_s, derive_version, code, picked, status)"
                     " VALUES ('tw', 'v1', '2026-10-27', 1, 1, '2330', 0, 'member')")
    ctx.close()


# ── 手動封存匯入 ──────────────────────────────────────────────────────────────
def test_import_archive_archives_and_logs(paths, market_db, tmp_path):
    manual = tmp_path / "archive_manual"
    manual.mkdir()
    fx.write_gz(manual / "stock_picks_tw.json.20261029_003901.gz", fx.main_snapshot())
    fx.write_gz(manual / "stock_picks_tw_ai.json.20261029_003901.gz", fx.ai_state())
    (manual / "stock_picks_history.jsonl.20261029_003901.gz").write_bytes(gzip.compress(b'{"t": "2026-10-27"}\n'))
    (manual / "stock_picks_tw_ai.json.20261029_003902.gz").write_bytes(b"not gzip")
    ctx = _ctx(paths)
    out = pool.import_archive_dir(ctx, manual)
    ctx.close()
    assert (out["archived"], out["invalid"], out["ignored"]) == (2, 1, 1)
    assert out["logged"]["tw_v1"] == ["new"] and out["logged"]["tw_ai"]["new"] == 1
    assert len(pool.archived_files(paths.archive)) == 2
    meta = json.loads([d for d in _days(paths) if d["rule"] == "ai_v1"][0]["meta_json"])
    assert meta["main_origin"] in ("import", "archive")
    # 再匯入一次：封存一樣、紀錄不重複
    ctx = _ctx(paths)
    again = pool.import_archive_dir(ctx, manual)
    ctx.close()
    assert again["same"] == 2 and again["logged"]["tw_v1"] == ["exists"] and again["logged"]["tw_ai"]["exists"] == 1


def test_bot_write_functions_are_never_called(paths, market_db, monkeypatch):
    """PLAN §4.1：bot 的寫入與有副作用的函式全部換成丟例外，整輪照樣成功。"""
    import stock_picks_us

    def boom(*_args, **_kwargs):
        raise AssertionError("stock_picks_review must not call bot write functions")

    for name in ("_write_json_file", "_commit_ai", "_commit_snapshot", "_append_history", "_append_ai_history",
                 "_ai_record", "_save_worker", "_ensure_loaded", "_ai_universe", "_state_file_lock", "_ai_core",
                 "_get_json", "_fetch_chart", "load_snapshot_file", "load_ai_file"):
        monkeypatch.setattr(sp, name, boom, raising=False)
    for name in ("_commit_snapshot", "_save_worker", "_check_history", "_update_perf", "_us_round", "_tick"):
        monkeypatch.setattr(stock_picks_us, name, boom, raising=False)
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    out = _run_pool(paths)
    assert out["tw_v1"] == "new" and out["tw_ai"]["new"] == 1 and out["us"] == "new"
