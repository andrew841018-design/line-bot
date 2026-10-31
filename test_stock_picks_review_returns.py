"""股票推薦回測 C1：成員報酬（returns 階段）、股票池基準 p、選版、週報的推薦成績（jobs/stock_picks_review_returns.py）。

盤勢 DB 用盤勢 job 自己的 market_store 建（同 schema：coverage、exrights、quarantine、obs、calendar_overrides 都在），
資料是合成、縮小過的；bot 目錄與 outbox 由 conftest 指到暫存目錄；不連網。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pytest

import market_store
import stock_db_ro
import stock_picks as sp
import stock_picks_perf as perf
import test_stock_picks_review_fixtures as fx
from jobs import stock_picks_review as runner
from jobs import stock_picks_review_db as rdb
from jobs import stock_picks_review_pool as pool
from jobs import stock_picks_review_returns as returns
from jobs import stock_picks_review_weekly as weekly

TPE = ZoneInfo("Asia/Taipei")
D = date.fromisoformat


def ts(day: str, hour: int, minute: int = 0) -> float:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=TPE).timestamp()


def weekdays(start: str, end: str) -> list[str]:
    out, day = [], D(start)
    while day <= D(end):
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


DAYS = weekdays("2026-09-01", "2026-10-20")  # 合成日曆：每個平日都是交易日（台股、美股一樣）
T = "2026-10-01"  # 週四；E＝10/02；T_5＝10/09；日曆到 10/20，5 日已到期、20 日還沒
TW_DEADLINE = ts("2026-10-02", 13, 30)  # E 收盤（台股 13:30）
NOW = ts("2026-10-20", 21)
WHERE = dict(pool.FAMILY_WHERE)


class Market:
    """測試用的盤勢 DB（market_store 建）。"""

    def __init__(self, path: Path):
        self.path = path

    def closes(self, market: str, symbol: str, series: dict) -> None:
        with market_store.Store(self.path) as store:
            store.upsert_closes(market, symbol, [(D(d), v, None) for d, v in series.items()],
                                "nasdaq" if market == "us" else "twse")

    def coverage(self, start: str, end: str) -> None:
        with market_store.Store(self.path) as store:
            store.add_coverage("twt49u", D(start), D(end))


@pytest.fixture
def paths() -> runner.Paths:
    p = runner.resolve_paths()
    assert p.bot_dir != runner.BASE and p.maintenance != runner.DEFAULT_MAINTENANCE  # conftest 指到暫存目錄
    runner.prepare_root(p)
    return p


def _ctx(paths: runner.Paths, now: float = NOW, **kwargs) -> runner.Context:
    return runner.Context(paths=paths, now=now, **kwargs)


def _stage(paths: runner.Paths, now: float = NOW, **kwargs) -> dict:
    ctx = _ctx(paths, now, **kwargs)
    try:
        return returns.returns_stage(ctx)
    finally:
        ctx.close()


def _version(m: str, rule: str, t: str, computed_at: float, members: dict, picks=(), *,
             dv: int = pool.DERIVE_VERSION) -> pool.Version:
    rows = []
    for code, c0 in members.items():
        rank = list(picks).index(code) + 1 if code in picks else None
        rows.append(pool.Row(code, f"n{code}", c0, code in picks, rank, pool.MEMBER if c0 is not None else pool.NO_CLOSE,
                             {"trend": None, "tr": None}, {}, None))
    return pool.Version(m=m, rule=rule, t=t, computed_at=computed_at, rows=rows, derive_version=dv)


def _write(paths: runner.Paths, *versions: pool.Version) -> None:
    conn = rdb.open_db(paths.db)
    try:
        for version in versions:
            pool.write_version(conn, version, now=NOW)
    finally:
        conn.close()


def _store_prices(paths: runner.Paths, rows: list[tuple]) -> None:
    """job 的價格庫（review.sqlite 的 prices）：(m, code, d, close)。"""
    conn = rdb.open_db(paths.db)
    try:
        with rdb.transaction(conn):
            conn.executemany("INSERT OR REPLACE INTO prices (m, code, d, close, src, seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                             [(m, code, d, close, "tw" if m == "tw" else "us", NOW) for m, code, d, close in rows])
    finally:
        conn.close()


def _returns(paths: runner.Paths) -> dict[tuple, sqlite3.Row]:
    con = sqlite3.connect(paths.db)
    con.row_factory = sqlite3.Row
    try:
        return {(r["family"], r["t"], r["ver_s"], r["code"], r["h"]): r
                for r in con.execute("SELECT * FROM member_returns")}
    finally:
        con.close()


def _insert_returns(paths: runner.Paths, family: str, version: pool.Version, h: int, values: dict, *,
                    rule: Optional[str] = None, calc_version: int = returns.CALC_VERSION) -> None:
    """member_returns 直接寫：{代號: (r, b, skip)}。rule 預設是那一版的 rule。"""
    conn = rdb.open_db(paths.db)
    try:
        with rdb.transaction(conn):
            conn.executemany(returns.INSERT_SQL, [
                (version.m, family, rule or version.rule, version.t, version.ver_s, version.derive_version, code, h,
                 returns.SUPPORTED_LEDGER_VERSION, calc_version, r, b, skip, "{}", NOW)
                for code, (r, b, skip) in values.items()])
    finally:
        conn.close()


def _version_p(paths: runner.Paths, family: str, t: str, h: int) -> tuple:
    """job 自己的讀法：那一天這一族唯一一版池子的 pool_p。"""
    conn = rdb.open_db(paths.db)
    try:
        (version,) = returns.family_versions(conn, family, WHERE[family])[t]
        return returns.pool_p(*returns.version_returns(conn, family, version, True), h)
    finally:
        conn.close()


def _assert_bot_state_is_tmp() -> None:
    """測試會寫 sp.STATE_DIR（紀錄檔、帳）：先確認 conftest 已經把它換成暫存目錄，不是正式的 state/（審查 C1 r2#7）。"""
    real = {(Path(sp.__file__).resolve().parent / "state").resolve(), (runner.PRODUCTION_BOT_DIR / "state").resolve()}
    assert Path(sp.STATE_DIR).resolve() not in real


def _use_bot_state(monkeypatch, paths: runner.Paths) -> None:
    """正式環境：bot 的 state/ 就是 job 的 state/（帳才讀得到）。"""
    _assert_bot_state_is_tmp()
    monkeypatch.setattr(sp, "STATE_DIR", paths.state_dir)


# ── schema ─────────────────────────────────────────────────────────────────
def _v1_db(path: Path) -> None:
    """A 段（user_version 1）的庫：只有 A 段的表與一筆池子紀錄。"""
    con = sqlite3.connect(path)
    try:
        for sql in rdb.MIGRATIONS[1]:
            con.execute(sql)
        con.execute("INSERT INTO pool_days (m, rule, t, ver_s, derive_version, computed_at, origin, n_members, n_picks,"
                    " logged_at) VALUES ('tw', 'v1', '2026-10-27', 1, 1, 1.0, 'simple', 1, 0, 1.0)")
        con.execute("PRAGMA user_version = 1")
        con.commit()
    finally:
        con.close()


def test_schema_migrates_an_a_database_by_adding_member_returns(paths):
    _v1_db(paths.db)
    con = rdb.open_db(paths.db)
    try:
        assert rdb.user_version(con) == 2 and rdb.has_tables(con, rdb.C1_TABLES)
        assert con.execute("SELECT count(*) FROM pool_days").fetchone()[0] == 1  # 舊資料原樣
        cols = [row[1] for row in con.execute("PRAGMA table_info(member_returns)")]
        assert cols == ["m", "family", "rule", "t", "ver_s", "derive_version", "code", "h", "ledger_version",
                        "calc_version", "r", "b", "skip", "inputs_json", "frozen_at"]
        pk = [row[1] for row in sorted(con.execute("PRAGMA table_info(member_returns)"), key=lambda r: r[5]) if row[5]]
        assert pk == ["m", "family", "rule", "t", "ver_s", "derive_version", "code", "h", "ledger_version", "calc_version"]
        key = ("tw", "tw_v1", "v1", "2026-10-27", 1, 1, "2330", 5, 2, 1)
        with pytest.raises(sqlite3.IntegrityError):  # 有值的列一定沒有 skip、有 b（寫入不會默默丟掉不合格的列）
            con.execute(returns.INSERT_SQL, (*key, 0.1, None, None, "{}", 1.0))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(returns.INSERT_SQL, (*key, 0.1, 0.0, "range", "{}", 1.0))
        con.execute(returns.INSERT_SQL, (*key, 0.1, 0.0, None, "{}", 1.0))
        con.execute(returns.INSERT_SQL, (*key, 0.9, 0.0, None, "{}", 2.0))  # 凍結後不改：同一個鍵不覆寫、不報錯
        assert con.execute("SELECT r, frozen_at FROM member_returns").fetchall() == [(0.1, 1.0)]
        con.execute(returns.INSERT_SQL, (*key[:9], 2, 0.9, 0.0, None, "{}", 2.0))  # 新的 calc_version 是另一列
        assert con.execute("SELECT count(*) FROM member_returns").fetchone()[0] == 2
    finally:
        con.close()


def test_a_code_still_runs_against_the_c1_database(paths, monkeypatch):
    """只加表：A 段的程式（SCHEMA_VERSION 1）開 C1 的庫照跑，不降版、不刪表。"""
    rdb.open_db(paths.db).close()
    monkeypatch.setattr(rdb, "SCHEMA_VERSION", 1)
    con = rdb.open_db(paths.db)
    try:
        assert rdb.user_version(con) == 2 and rdb.has_tables(con, rdb.A_TABLES + rdb.C1_TABLES)
    finally:
        con.close()


def test_a_v1_database_missing_its_tables_is_not_silently_upgraded(paths):
    con = sqlite3.connect(paths.db)
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    with pytest.raises(rdb.SchemaError):
        rdb.open_db(paths.db)
    con = sqlite3.connect(paths.db)
    try:  # 整個交易 rollback：沒有建出 C1 的表、版本沒動
        assert rdb.user_version(con) == 1 and not rdb.has_tables(con, rdb.C1_TABLES)
    finally:
        con.close()


# ── returns 階段 ───────────────────────────────────────────────────────────
def _market(paths: runner.Paths) -> None:
    """盤勢 DB（DAYS 的台股、美股收盤與除權息涵蓋）＋job 的價格庫（DB 沒有的成員 6666、AAPL，以及比 DB 早的日曆）。"""
    market = Market(paths.market_db)
    market.closes("tw", "TAIEX", {d: 20000.0 for d in DAYS})
    market.closes("tw", "0050", {d: 100.0 for d in DAYS})
    market.coverage(DAYS[0], DAYS[-1])
    market.closes("tw", "2330", {d: 100.0 if d <= T else 104.0 if d < "2026-10-09" else 109.2 for d in DAYS})
    market.closes("tw", "1101", {d: 50.0 for d in DAYS})
    market.closes("tw_all", "9999", {d: 30.0 for d in DAYS[-10:]})
    market.closes("tw", "9999", {d: 30.0 for d in DAYS})
    market.closes("tw", "7777", {d: 40.0 for d in DAYS})
    market.closes("us", "SPY", {d: 500.0 for d in DAYS})
    market.closes("us", "NVDA", {d: 200.0 for d in DAYS})
    early = weekdays("2026-08-03", "2026-08-31")
    _store_prices(paths, [("tw", "6666", d, 80.0) for d in DAYS] + [("us", "AAPL", d, 150.0) for d in DAYS]
                  + [("tw", "TAIEX", d, 19000.0) for d in early] + [("us", "VOO", d, 450.0) for d in early])


def _scenario(paths: runner.Paths) -> dict:
    """台股 v1 一版（E 收盤前）＋同一天 E 收盤剛好那一刻才算出的一版（不算）＋日曆上還沒有 E 的 AI 一版＋美股一版。"""
    _market(paths)
    members = {"2330": 100.0, "1101": 50.0, "6666": 80.0, "9999": None, "7777": 45.0}
    on_time = _version("tw", "v1", T, ts("2026-10-02", 5, 20), members, picks=("2330", "1101", "6666"))
    late = _version("tw", "v1", T, TW_DEADLINE, members, picks=("2330", "1101", "6666"))
    no_e = _version("tw", "ai_v1", DAYS[-1], ts("2026-10-21", 5), {"2330": 109.2})
    us = _version("us", "us_v1", T, ts("2026-10-02", 8), {"NVDA": 200.0, "AAPL": 150.0})  # 美東 10/01 20:00
    _write(paths, on_time, late, no_e, us)
    return {"on_time": on_time, "late": late, "no_e": no_e, "us": us}


def test_returns_stage_freezes_due_members_and_skips_what_cannot_be_due(paths):
    v = _scenario(paths)
    out = _stage(paths)
    assert out["status"] == "ok" and out["markets"] == {"tw": "ok", "us": "ok"}
    assert (out["versions"], out["due_versions"], out["late"], out["wait_e"], out["not_due_versions"]) == (4, 2, 1, 1, 0)
    # 只有 5 日到期：(5＋2) 檔的 5 日都評、都凍結；20／60／240 日的 T_h 還不在日曆上，先略過不載入
    assert (out["evaluated"], out["frozen"], out["waiting"], out["not_due_members"]) == (7, 7, 0, 21)
    assert out["families"] == {"tw_v1": {"evaluated": 5, "frozen": 5, "waiting": 0},
                               "tw_ai": {"evaluated": 0, "frozen": 0, "waiting": 0},
                               "us": {"evaluated": 2, "frozen": 2, "waiting": 0}}
    assert out["waits"] == {} and (out["capped"], out["out_of_time"], out["unexpected"], out["eval_errors"]) == (
        False, False, 0, 0)
    rows = _returns(paths)
    on_time = v["on_time"].ver_s
    assert {k for k in rows if k[0] == "tw_v1"} == {("tw_v1", T, on_time, c, 5) for c in ("2330", "1101", "6666", "9999",
                                                                                          "7777")}
    first = rows[("tw_v1", T, on_time, "2330", 5)]
    assert (first["m"], first["rule"], first["derive_version"], first["ledger_version"], first["calc_version"],
            first["r"], first["b"], first["skip"]) == ("tw", "v1", 1, 2, returns.CALC_VERSION, 0.05, 0.0, None)
    assert json.loads(first["inputs_json"]) == {"th": "2026-10-09", "c0": 100.0, "ce": 104.0, "ch": 109.2, "cf": 1.0,
                                                "be": 100.0, "bh": 100.0, "bf": 1.0}  # r＝109.2 ÷ 104（E 的收盤）− 1
    assert first["frozen_at"] == NOW
    assert rows[("tw_v1", T, on_time, "6666", 5)]["r"] == 0.0  # DB 沒有的成員：價格庫供價
    missing = rows[("tw_v1", T, on_time, "9999", 5)]  # 沒有 c0：T_h 在日曆上就記 missing，不呼叫 perf、不編數字
    assert (missing["r"], missing["b"], missing["skip"], missing["inputs_json"]) == (None, None, "missing", None)
    assert rows[("tw_v1", T, on_time, "7777", 5)]["skip"] == "mismatch"  # c0 和 DB 的 t 收盤差 > 0.5%（perf 的規則）
    assert not any(k[2] == v["late"].ver_s for k in rows)  # E 收盤那一刻才算出來的版本永遠不算
    assert {(k[0], k[3], k[4]) for k in rows if k[0] == "us"} == {("us", "NVDA", 5), ("us", "AAPL", 5)}
    assert rows[("us", T, v["us"].ver_s, "AAPL", 5)]["r"] == 0.0  # 美股價格庫供價
    again = _stage(paths)  # 冪等：凍結的不再算；兩版剩下的最快是 20 日，T_20 還不在日曆上 → 整版略過、不查成員
    assert (again["evaluated"], again["frozen"], again["waiting"], again["not_due_versions"]) == (0, 0, 0, 2)
    assert again["status"] == "ok" and again["due_versions"] == 0 and len(_returns(paths)) == 7


def test_returns_stage_dry_run_counts_but_writes_nothing(paths):
    _scenario(paths)
    out = _stage(paths, dry_run=True)
    assert out["status"] == "dry_run" and out["frozen"] == 7
    assert _returns(paths) == {}


def test_returns_stage_resumes_in_bounded_batches(paths, monkeypatch):
    """一輪只載入 MAX_CANDIDATES 筆（至少一個版本）：剩下的下一輪接著算；快到期的版本排前面（同樣快時先算出來的先）。"""
    v = _scenario(paths)
    monkeypatch.setattr(returns, "MAX_CANDIDATES", 1)
    first = _stage(paths)  # 台股那版（10/02 05:20 算出）比美股那版（08:00）早：先載入台股的 5 筆，就到上限了
    assert (first["capped"], first["out_of_time"], first["due_versions"], first["frozen"]) == (True, False, 1, 5)
    assert {k[0] for k in _returns(paths)} == {"tw_v1"}
    second = _stage(paths)  # 台股那版剩下的都是 20 日以後（略過）：美股的 5 日接著算，載完就沒有下一版，不算碰到上限
    assert (second["capped"], second["due_versions"], second["frozen"], second["not_due_versions"]) == (False, 1, 2, 1)
    assert {k[0] for k in _returns(paths)} == {"tw_v1", "us"}
    assert len({k[2] for k in _returns(paths)}) == 2 and v["late"].ver_s not in {k[2] for k in _returns(paths)}


def test_due_order_compares_markets_with_different_calendar_lengths():
    """排先後用「離 T_h 還有幾個交易日」：價格庫讓某個市場的日曆多了一年也一樣比（只影響順序與略過，不影響判斷）。"""
    longer = tuple(weekdays("2025-09-01", "2026-08-31")) + tuple(DAYS)
    assert returns._sessions_to(longer, T, 5) == returns._sessions_to(tuple(DAYS), T, 5) == -7  # T_5＝10/09，日曆到 10/20
    assert returns._sessions_to(tuple(DAYS), T, 20) == 8


def test_skipping_by_sessions_to_never_changes_what_evaluate_would_say():
    """_sessions_to > 0 ⇒ perf.evaluate 一定回 (WAIT, not_due)（審查 C1 r2#4、r4#4）：預先略過不會改變任何結果。
    兩個市場的緩衝（台股 2、美股 4）、每個 t、短長的 h 都試。"""
    cal = tuple(DAYS)
    checked = 0
    for market, code in (("tw", "2330"), ("us", "NVDA")):
        data = perf.MarketData(market, cal, {}, {})
        for t in cal:
            for h in (1, 2, 5, 20, 60):
                if returns._sessions_to(cal, t, h) > 0:
                    assert perf.evaluate(perf.Candidate(market, "x", t, 1, code, 1.0, h), data) == (perf.WAIT, "not_due")
                    checked += 1
    assert checked > 100


def test_prefilter_boundary_skips_only_t_h_beyond_the_calendar(paths):
    """邊界：T_h 剛好是日曆最後一天（sessions_to＝0）照樣評（perf 自己因為緩衝回 not_due）；再晚一天（＝1）整版略過，
    而 perf 對它一樣回 not_due。"""
    _market(paths)
    conn = rdb.open_db(paths.db)
    try:
        cal = returns.market_calendar(conn, "tw", "2025-01-01", paths.market_db)
    finally:
        conn.close()
    t0 = next(t for t in cal if returns._sessions_to(cal, t, 5) == 0)
    t1 = next(t for t in cal if returns._sessions_to(cal, t, 5) == 1)
    assert (t0, t1) == ("2026-10-12", "2026-10-13")  # T_5＝10/20（日曆最後一天）、10/21
    _write(paths, *(_version("tw", "v1", t, ts(t, 16), {"2330": 109.2}) for t in (t0, t1)))
    out = _stage(paths)
    assert (out["versions"], out["not_due_versions"], out["due_versions"]) == (2, 1, 1)  # t1 那版整版略過
    assert (out["evaluated"], out["waiting"], out["waits"], out["not_due_members"]) == (1, 1, {"not_due": 1}, 3)
    conn = rdb.open_db(paths.db)
    try:
        skipped = perf.Candidate("tw", "v1", t1, round(ts(t1, 16)), "2330", 109.2, 5)
        data = returns.tw_market_data(perf, conn, [skipped], cal, "2026-10-20", paths.market_db)
        assert perf.evaluate(skipped, data) == (perf.WAIT, "not_due")  # 略過的那一筆，評了也是 not_due
    finally:
        conn.close()


def test_returns_stage_stops_at_the_time_budget_after_minimum_progress(paths, monkeypatch):
    _scenario(paths)
    monkeypatch.setattr(returns, "BUDGET_S", -1.0)  # 一開始就超過預算
    monkeypatch.setattr(returns, "CHECK_EVERY", 3)
    out = _stage(paths)
    assert (out["out_of_time"], out["capped"], out["due_versions"]) == (True, False, 1)
    assert out["evaluated"] == 3  # 至少評完前 3 筆


def test_frozen_rows_are_written_as_they_accumulate(paths, monkeypatch):
    """每滿 INSERT_CHUNK 列就寫一次（審查 C1 r2#5）：評估途中 alarm 到了（JobTimeout 是 BaseException，逐筆隔離接不到，
    一路到外層），前面寫好的不白費。"""
    _scenario(paths)
    monkeypatch.setattr(returns, "INSERT_CHUNK", 2)
    real, seen = perf.evaluate, []

    def evaluate(cand, data):
        seen.append(cand)
        if len(seen) == 4:
            raise runner.JobTimeout()
        return real(cand, data)

    monkeypatch.setattr(perf, "evaluate", evaluate)
    with pytest.raises(runner.JobTimeout):
        _stage(paths)
    assert len(_returns(paths)) == 2  # 前兩列已經寫進去；第三列還在記憶體裡、跟著例外丟掉


def test_one_candidate_that_breaks_evaluate_does_not_stop_the_rest(paths, monkeypatch):
    """perf.evaluate 自己丟例外：只跳過那一筆，排在它後面的照評、照寫；階段記 error（那個例外類別），照樣走連續失敗
    短訊（第 2 輪 r2#G）。"""
    v = _scenario(paths)
    real = perf.evaluate

    def evaluate(cand, data):
        if cand.code == "6666":  # 排在 1101、2330 後面，7777、AAPL、NVDA 前面
            raise KeyError("boom")
        return real(cand, data)

    monkeypatch.setattr(perf, "evaluate", evaluate)
    out = _stage(paths)
    assert (out["status"], out["error"], out["eval_errors"], out["frozen"]) == ("error", "KeyError", 1, 6)
    assert out["where"].startswith("test_stock_picks_review_returns:evaluate:")  # 只有程式位置，沒有例外訊息
    assert out["evaluated"] == out["frozen"] + out["waiting"] + out["unexpected"] + out["eval_errors"] == 7
    rows = _returns(paths)
    assert {k[3] for k in rows} == {"1101", "2330", "7777", "9999", "AAPL", "NVDA"}  # 6666 之後的照樣凍結
    assert ("tw_v1", T, v["on_time"].ver_s, "6666", 5) not in rows


def test_returns_stage_skips_other_ledger_versions(paths, monkeypatch):
    _scenario(paths)
    monkeypatch.setattr(perf, "LEDGER_VERSION", 3)
    assert _stage(paths) == {"status": "skipped:ledger_version", "ledger_version": 3}
    assert _returns(paths) == {}


def test_returns_stage_treats_a_busy_market_db_as_not_a_failure(paths, monkeypatch):
    _scenario(paths)

    def busy(*_args, **_kwargs):
        raise stock_db_ro.DbBusy("SQLITE_BUSY")

    monkeypatch.setattr(stock_db_ro, "exrights", busy)  # 台股的 load_tw 讀不到；美股照算
    out = _stage(paths)
    assert out["status"] == "busy" and out["markets"] == {"tw": "busy", "us": "ok"}
    assert {k[0] for k in _returns(paths)} == {"us"}


@pytest.mark.parametrize("name, error", [
    ("tw_calendar", RuntimeError("calendar broke")),  # 讀日曆那一圈
    ("tw_closed_days", stock_db_ro.DbUnavailable("bad_json")),  # 組 MarketData 那一圈（r3 的重現：load_tw 讀休市表）
])
def test_one_market_db_error_does_not_stop_the_other_market(paths, monkeypatch, name, error):
    """一個市場讀不到（不是忙碌）只跳過那個市場：另一個市場照算、照寫，階段記 error（審查 C1 r3#4）。"""
    _scenario(paths)

    def broken(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(stock_db_ro, name, broken)
    out = _stage(paths)
    kind = type(error).__name__
    assert out["status"] == "error" and out["error"] == kind
    assert out["markets"] == {"tw": "error:" + kind, "us": "ok"}
    assert {k[0] for k in _returns(paths)} == {"us"} and out["frozen"] == 2


def test_unexpected_perf_row_is_skipped_and_fails_the_stage(paths, monkeypatch):
    """perf 回了看不懂的一列：只有那一列不寫，其他照寫；階段記 error（UnexpectedPerfRow），照樣走連續失敗短訊
    （審查 C1 r1#4）。"""
    v = _scenario(paths)
    real = perf.evaluate

    def evaluate(cand, data):
        kind, value = real(cand, data)
        if cand.code == "1101" and kind == perf.FREEZE:
            return kind, {**value, "v": 99}
        return kind, value

    monkeypatch.setattr(perf, "evaluate", evaluate)
    out = _stage(paths)
    assert (out["status"], out["error"], out["unexpected"], out["frozen"]) == ("error", "UnexpectedPerfRow", 1, 6)
    rows = _returns(paths)
    assert len(rows) == 6 and ("tw_v1", T, v["on_time"].ver_s, "1101", 5) not in rows
    for i in range(3):  # 每輪都再評到同一列、每輪都失敗：連續 3 輪發短訊
        assert runner.run(paths, now=NOW + i, stages=(("returns", runner._stage_returns),)) == runner.EXIT_FAILED
    last = _last_run(paths)
    assert last["consecutive_failures"] == 3 and last["stages"]["returns"]["error"] == "UnexpectedPerfRow"
    note = (paths.outbox / "2026-10-20-stock-picks-review-failed.md").read_text(encoding="utf-8")
    assert "階段：returns" in note and "類別：UnexpectedPerfRow" in note


OLD_MEMBER_RETURNS = """CREATE TABLE member_returns (
    m TEXT NOT NULL, family TEXT NOT NULL, t TEXT NOT NULL, ver_s INTEGER NOT NULL,
    derive_version INTEGER NOT NULL, code TEXT NOT NULL, h INTEGER NOT NULL, ledger_version INTEGER NOT NULL,
    r REAL, b REAL, skip TEXT, inputs_json TEXT, frozen_at REAL NOT NULL,
    PRIMARY KEY (m, family, t, ver_s, derive_version, code, h, ledger_version)) WITHOUT ROWID"""  # ec5f14a 的結構


def test_an_old_shape_member_returns_is_a_schema_error_not_a_query_error(paths):
    """migration 2 原地改之前建的表（沒有 rule、calc_version）：returns 記 SchemaError、dry-run 回 skipped:schema、
    週報那一段也是 SchemaError（只換成一行說明），不是查詢丟的籠統 OperationalError（第 2 輪 r2#H）。"""
    _scenario(paths)
    con = sqlite3.connect(paths.db)
    con.execute("DROP TABLE member_returns")
    con.execute(OLD_MEMBER_RETURNS)
    con.commit()
    con.close()
    with pytest.raises(rdb.SchemaError, match="member_returns columns"):
        _stage(paths)
    assert _stage(paths, dry_run=True) == {"status": "skipped:schema"}
    with pytest.raises(rdb.SchemaError, match="member_returns columns"):
        _performance(paths)
    ctx = _ctx(paths, SUNDAY)
    try:
        assert weekly.performance_lines(ctx, ctx.db(), {}) == ["【推薦成績】這週算不出來（SchemaError），其他內容照常"]
    finally:
        ctx.close()


def test_returns_stage_needs_its_table_in_a_real_run(paths):
    rdb.open_db(paths.db).close()
    con = sqlite3.connect(paths.db)
    con.execute("DROP TABLE member_returns")
    con.commit()
    con.close()
    with pytest.raises(rdb.SchemaError):
        _stage(paths)
    assert _stage(paths, dry_run=True) == {"status": "skipped:no_table"}  # 唯讀開的舊庫：不算、不失敗


def test_c0_none_member_is_recorded_missing_even_if_perf_could_value_it():
    """美股 t 之後有分割時 perf 不比 c0（_c0_ok 直接過）：沒有 c0 的成員照樣記 missing（不拿後來的價格回填）。"""
    cand = perf.Candidate("us", "us_v1", T, 1, "AAPL", None, 5)
    value = {"v": 2, "m": "us", "rule": "us_v1", "t": T, "rev": 1, "code": "AAPL", "h": 5, "th": "2026-10-09",
             "r": 0.1, "b": 0.0, "skip": None, **{k: 1.0 for k in perf.INPUT_KEYS}}
    row = returns.member_row(perf, "us", 1, cand, value, NOW)
    assert row[:3] == ("us", "us", "us_v1") and row[8:13] == (2, returns.CALC_VERSION, None, None, "missing")
    with pytest.raises(ValueError):  # 看不懂的 perf 列不寫
        returns.member_row(perf, "us", 1, perf.Candidate("us", "us_v1", T, 1, "AAPL", 1.0, 5), {**value, "skip": "bogus"},
                           NOW)


def test_c0_less_member_is_recorded_missing_as_soon_as_t_h_is_on_the_calendar(paths):
    """沒有 c0 的成員結果一定是 missing：T_h 一在日曆上（過了預濾）就記，不呼叫 perf、不等 T_h 之後 10 個交易日（審查
    C1 r1#3、第 2 輪 r4#1）。perf 自己這一輪還在等（台股：WAIT price）；這條路的列沒有算式輸入（inputs_json 是 NULL）。"""
    _market(paths)
    v = _version("tw", "v1", T, ts("2026-10-02", 5, 20), {"2330": 100.0, "5555": None}, picks=("2330",))
    _write(paths, v)
    conn = rdb.open_db(paths.db)
    try:
        cal = returns.market_calendar(conn, "tw", "2025-01-01", paths.market_db)
        cand = perf.Candidate("tw", "v1", T, v.ver_s, "5555", None, 5)
        data = returns.tw_market_data(perf, conn, [cand], cal, "2026-10-20", paths.market_db)
        assert perf.evaluate(cand, data) == (perf.WAIT, "price")  # perf 自己這一輪還在等（5555 哪裡都沒有收盤）
    finally:
        conn.close()
    out = _stage(paths)
    assert (out["frozen"], out["waiting"]) == (2, 0)
    rows = _returns(paths)
    row = rows[("tw_v1", T, v.ver_s, "5555", 5)]
    assert (row["r"], row["b"], row["skip"], row["inputs_json"]) == (None, None, "missing", None)
    assert ("tw_v1", T, v.ver_s, "5555", 20) not in rows  # 20 日的 T_h 還不在日曆上：照樣等


def test_us_member_without_c0_or_any_close_is_missing_before_settle(paths):
    """美股 ⚪：沒有 c0、哪裡都沒有收盤。perf 在 settle 前一直回第二種 not_due（那一檔的批次還沒寫，stock_picks_perf
    的美股分支），舊的「不再回 not_due 才記」永遠等不到；現在 T_h 一在日曆上就記 missing，同一輪其他成員凍結後那一版的
    p 就算得出來（第 2 輪 r4#1、r1#1、r3#1）。"""
    _market(paths)
    market = Market(paths.market_db)
    market.closes("us", "AMD", {d: 100.0 if d < "2026-10-09" else 110.0 for d in DAYS})  # 5 日 +10%
    market.closes("us", "MSFT", {d: 300.0 for d in DAYS})
    members = {"NVDA": 200.0, "AAPL": 150.0, "AMD": 100.0, "MSFT": 300.0, "ZZZZ": None}
    v = _version("us", "us_v1", T, ts("2026-10-02", 8), members, picks=("NVDA",))
    _write(paths, v)
    conn = rdb.open_db(paths.db)
    try:
        cal = returns.market_calendar(conn, "us", "2025-01-01", paths.market_db)
        assert returns._sessions_to(cal, T, 5) <= -perf.US_BUFFER_SESSIONS  # T_5＋緩衝都在日曆上，還沒 settle
        cand = perf.Candidate("us", "us_v1", T, v.ver_s, "ZZZZ", None, 5)
        data = returns.us_market_data(perf, conn, [cand], cal, paths.market_db)
        assert perf.evaluate(cand, data) == (perf.WAIT, "not_due")  # 舊的規則在這裡永遠不會記
    finally:
        conn.close()
    out = _stage(paths)
    assert (out["frozen"], out["waiting"], out["evaluated"]) == (5, 0, 5)
    row = _returns(paths)[("us", T, v.ver_s, "ZZZZ", 5)]
    assert (row["r"], row["b"], row["skip"], row["inputs_json"]) == (None, None, "missing", None)
    status, p, dv = _version_p(paths, "us", T, 5)  # 4/5 有值＝80%：(0＋0＋0.1＋0)／4
    assert (status, dv) == ("ok", 1) and p == pytest.approx(0.025)


def test_rows_of_another_rule_or_calc_version_do_not_count(paths):
    """member_returns 只看目前的 rule、calc_version（審查 C1 r2#1、r4#3）：只差 rule 的列、舊 calc_version 的列都是誘餌，
    不算完整度、不擋重算，也不進 p。"""
    _market(paths)
    v = _version("tw", "ai_v1", T, ts("2026-10-02", 5, 20), {"2330": 100.0, "1101": 50.0}, picks=("2330",))
    _write(paths, v)
    decoy = {"2330": (0.9, 0.0, None), "1101": (0.9, 0.0, None)}
    _insert_returns(paths, "tw_ai", v, 5, decoy, rule="ai_v2")
    _insert_returns(paths, "tw_ai", v, 5, decoy, calc_version=0)
    conn = rdb.open_db(paths.db)
    try:
        (work,) = returns.incomplete_versions(conn, "tw_ai", WHERE["tw_ai"], perf.HORIZONS, "2026-01-01")
        assert work.pending_h == (5, 20, 60, 240)
        assert [(code, h) for code, _, h in returns.pending_members(conn, work, perf.HORIZONS) if h == 5] == [
            ("1101", 5), ("2330", 5)]
    finally:
        conn.close()
    assert _version_p(paths, "tw_ai", T, 5) == ("pending", None, None)
    out = _stage(paths)
    assert out["frozen"] == 2  # 兩檔的 5 日真的算了：(0.05＋0)／2，不是誘餌的 0.9
    status, p, dv = _version_p(paths, "tw_ai", T, 5)
    assert (status, dv) == ("ok", 1) and p == pytest.approx(0.025)
    con = sqlite3.connect(paths.db)
    try:
        assert con.execute("SELECT count(*) FROM member_returns").fetchone()[0] == 6  # 誘餌還在（舊列留著備查）
    finally:
        con.close()


def test_every_derive_version_is_computed_and_p_uses_the_highest_complete_one(paths):
    """同一版池子有 derive_version 1、2（推導方式改了、從封存重推，審查 C1 r1#6b）：returns 兩個都算，p 用齊全的最大的。"""
    _market(paths)
    at = ts("2026-10-02", 5, 20)
    _write(paths, _version("tw", "v1", T, at, {"2330": 100.0, "1101": 50.0}, picks=("2330",), dv=1),
           _version("tw", "v1", T, at, {"2330": 100.0, "1101": 50.0, "6666": 80.0}, picks=("2330",), dv=2))
    out = _stage(paths)
    assert (out["versions"], out["due_versions"], out["frozen"]) == (2, 2, 5)
    con = sqlite3.connect(paths.db)
    try:
        assert set(con.execute("SELECT derive_version, code FROM member_returns WHERE h = 5")) == {
            (1, "2330"), (1, "1101"), (2, "2330"), (2, "1101"), (2, "6666")}
    finally:
        con.close()
    status, p, dv = _version_p(paths, "tw_v1", T, 5)
    assert (status, dv) == ("ok", 2) and p == pytest.approx(0.05 / 3)  # dv 1 會是 (0.05＋0)／2


def test_spool_replacing_the_simple_version_ignores_the_old_members_rows(paths):
    """spool 取代簡版、成員變了（審查 C1 r1#6c）：舊成員（6666）留下的 member_returns 是孤兒，不算進完整度，也不進 p。"""
    _market(paths)
    at = ts("2026-10-02", 5, 20)
    _write(paths, _version("tw", "ai_v1", T, at, {"2330": 100.0, "1101": 50.0, "6666": 80.0}, picks=("2330",)))
    assert _stage(paths)["frozen"] == 3  # 簡版三個成員的 5 日都凍結了
    spool = replace(_version("tw", "ai_v1", T, at, {"2330": 100.0, "1101": 50.0, "9999": 30.0}, picks=("2330",)),
                    origin=pool.SPOOL)
    conn = rdb.open_db(paths.db)
    try:
        assert pool.write_version(conn, spool, now=NOW) == "replaced"
        (work,) = returns.incomplete_versions(conn, "tw_ai", WHERE["tw_ai"], perf.HORIZONS, "2026-01-01")
        assert work.pending_h == (5, 20, 60, 240)  # 9999 的 5 日還沒算：6666 的列不能補上這個缺
        assert [m for m in returns.pending_members(conn, work, perf.HORIZONS) if m[2] == 5] == [("9999", 30.0, 5)]
    finally:
        conn.close()
    assert _version_p(paths, "tw_ai", T, 5) == ("pending", None, None)
    assert _stage(paths)["frozen"] == 1
    status, p, dv = _version_p(paths, "tw_ai", T, 5)
    assert (status, dv) == ("ok", 1) and p == pytest.approx(0.05 / 3)  # 2330 +5%、1101 與 9999 0；算進孤兒會是 0.05／4


# ── C1 契約：job 組的 MarketData 重算 perf 帳的一筆，結果完全一樣 ───────────────────────────────
def _history(name: str, lines: list[dict]) -> None:
    _assert_bot_state_is_tmp()
    (sp.STATE_DIR / name).write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")


def test_contract_job_market_data_reproduces_the_perf_ledger_exactly(paths):
    """PLAN §5.1：用 job 組的 MarketData（價格庫 700 天＋DB、日曆＝DB ∪ 價格庫，含 0050／SPY）重算家人看到的推薦，
    和 perf 帳的那一列一模一樣（r、b、skip、th、算式輸入）。台股一檔 0050 成分（DB tw 有長歷史）、一檔非成分（DB 只有
    tw_all 最近 10 天，更早的在 perf 的價格庫與 job 的價格庫）；美股一檔。"""
    market = Market(paths.market_db)
    market.closes("tw", "TAIEX", {d: 20000.0 for d in DAYS})
    market.closes("tw", "0050", {d: 100.0 + 0.3 * i for i, d in enumerate(DAYS)})
    market.coverage(DAYS[0], DAYS[-1])
    p2330 = {d: 500.0 + 3 * (i % 5) for i, d in enumerate(DAYS)}
    p3661 = {d: 800.0 - 5 * (i % 4) for i, d in enumerate(DAYS)}
    market.closes("tw", "2330", p2330)
    recent = DAYS[-10:]
    market.closes("tw_all", "3661", {d: p3661[d] for d in recent})
    older = {d: p3661[d] for d in DAYS if d not in recent}
    _assert_bot_state_is_tmp()
    (sp.STATE_DIR / perf.PRICES_NAME).write_text(json.dumps({"schema": 1, "prices": {"3661": older},
                                                             "needed": {"3661": T}}), encoding="utf-8")
    market.closes("us", "SPY", {d: 600.0 + i for i, d in enumerate(DAYS)})
    pnvda = {d: 200.0 + (i % 3) for i, d in enumerate(DAYS)}
    market.closes("us", "NVDA", pnvda)
    early = weekdays("2026-08-03", "2026-08-31")  # 價格庫比 DB 早的日曆
    _store_prices(paths, [("tw", "3661", d, c) for d, c in older.items()] + [("tw", "TAIEX", d, 19000.0) for d in early]
                  + [("us", "SPY", d, 590.0) for d in early])
    closes = {"2330": p2330[T], "3661": p3661[T], "NVDA": pnvda[T]}
    _history(sp.HISTORY_NAME, [{"t": T, "market": "neutral", "computed_at": round(ts(T, 16)),
                                "picks": [{"code": c, "name": "x", "close": closes[c], "score": 4, "p60": 0}
                                          for c in ("2330", "3661")]}])
    _history(perf.US_HISTORY_NAME, [{"t": T, "market": None, "computed_at": round(ts("2026-10-02", 14)), "rule": "us_v1",
                                     "picks": [{"code": "NVDA", "name": "X", "close": closes["NVDA"], "score": 4,
                                                "p60": 0}]}])
    assert perf.update(ai_enabled=False, now=NOW, db_path=paths.market_db) is not None
    ledger = [r for r in perf.read_ledger_rows() if r["v"] == perf.LEDGER_VERSION]
    assert {(r["m"], r["code"], r["h"]) for r in ledger} == {("tw", "2330", 5), ("tw", "3661", 5), ("us", "NVDA", 5)}
    assert all(r["skip"] is None for r in ledger)
    conn = rdb.open_db(paths.db)
    try:
        for row in ledger:
            cand = perf.Candidate(row["m"], row["rule"], row["t"], row["rev"], row["code"], closes[row["code"]], row["h"])
            calendar = returns.market_calendar(conn, row["m"], "2025-01-01", paths.market_db)
            assert len(calendar) > len(DAYS)  # 日曆比 DB 的長（價格庫的部分接在前面）
            if row["m"] == "tw":
                data = returns.tw_market_data(perf, conn, [cand], calendar, "2026-10-20", paths.market_db)
            else:
                data = returns.us_market_data(perf, conn, [cand], calendar, paths.market_db)
            kind, mine = perf.evaluate(cand, data)
            assert kind == perf.FREEZE
            assert mine == {key: row[key] for key in mine}, row["code"]
    finally:
        conn.close()


# ── p 與選版（純函式） ───────────────────────────────────────────────────────
def test_p_is_the_mean_once_every_member_is_frozen():
    members = {1: frozenset("ABCDE")}
    full = {(1, 5, c): (r, 0.0, None) for c, r in zip("ABCDE", (0.1, 0.2, 0.3, 0.4, 0.5))}
    assert returns.pool_p(members, full, 5) == ("ok", 0.3, 1)
    assert returns.pool_p(members, full, 20) == ("pending", None, None)  # 20 日一筆都還沒凍結
    partial = {k: v for k, v in full.items() if k[2] != "E"}
    assert returns.pool_p(members, partial, 5) == ("pending", None, None)  # 還有成員沒凍結：不算
    four = {**full, (1, 5, "E"): (None, None, "missing")}  # 4/5 有值＝80%：照算，只平均有值的
    assert returns.pool_p(members, four, 5) == ("ok", 0.25, 1)
    three = {**four, (1, 5, "D"): (None, None, "range")}  # 3/5＝60%
    assert returns.pool_p(members, three, 5) == ("pool_missing", None, 1)


def test_p_uses_the_highest_complete_derive_version():
    """新的 derive_version 重算到一半（alarm 預算分好幾輪）：退回上一個齊全的版本；齊了才換（辯論 r3）。"""
    members = {1: frozenset("AB"), 2: frozenset("ABC")}
    old = {(1, 5, "A"): (0.1, 0.0, None), (1, 5, "B"): (0.3, 0.0, None)}
    half = {**old, (2, 5, "A"): (0.5, 0.0, None)}
    assert returns.pool_p(members, half, 5) == ("ok", 0.2, 1)
    done = {**half, (2, 5, "B"): (0.5, 0.0, None), (2, 5, "C"): (0.5, 0.0, None)}
    assert returns.pool_p(members, done, 5) == ("ok", 0.5, 2)
    assert returns.pool_p({2: frozenset("ABC")}, half, 5) == ("pending", None, None)  # 只有新版、還不齊


def _pv(ver_s: int, computed_at: float, picks: str) -> returns.PoolVersion:
    return returns.PoolVersion("tw_ai", "tw", "ai_v1", T, ver_s, computed_at, frozenset(picks))


def test_version_selection_matches_falls_back_or_is_missing():
    early, later, late = _pv(1, TW_DEADLINE - 9000, "ABC"), _pv(2, TW_DEADLINE - 60, "ABD"), _pv(3, TW_DEADLINE, "ABC")
    options = [early, later, late]
    assert returns.select_version(options, TW_DEADLINE, frozenset("ABC")) == (early, None)  # picks 相同的最後一版
    assert returns.select_version(options, TW_DEADLINE, frozenset("ABD")) == (later, None)
    assert returns.select_version(options, TW_DEADLINE, frozenset("XYZ")) == (later, "version_fallback")
    assert returns.select_version([late], TW_DEADLINE, frozenset("ABC")) == (None, "version_missing")  # E 收盤那一刻才算
    assert returns.select_version([], TW_DEADLINE, frozenset("ABC")) == (None, "version_missing")
    assert returns.select_version(options, None, frozenset("ABC")) == (None, "pending")  # 日曆上還沒有 E
    assert returns.select_version(options, TW_DEADLINE, None) == (later, None)  # 舊五條件的背景日：最後一版


# ── 週報的推薦成績 ───────────────────────────────────────────────────────────
T0, T1, T2 = "2026-10-01", "2026-10-05", "2026-10-06"
CAL = weekdays("2026-09-28", "2026-11-30")
SUNDAY = ts("2026-11-01", 20, 30)


def _lrow(t: str, code: str, r: Optional[float], b: Optional[float], *, m: str = "tw", rule: str = "ai_v1",
          rev: int = 1, h: int = 5, skip: Optional[str] = None, v: int = 2) -> dict:
    return {"v": v, "m": m, "rule": rule, "t": t, "rev": rev, "code": code, "h": h, "th": "2026-10-20", "r": r, "b": b,
            "skip": skip}


def _jsonl(path: Path, objs: list[dict]) -> None:
    path.write_text("".join(json.dumps(x) + "\n" for x in objs), encoding="utf-8")


def _pick(code: str, close: float) -> dict:
    return {"code": code, "name": "x", "close": close, "score": 4, "p60": 0}


def _report_world(paths: runner.Paths, monkeypatch) -> None:
    """AI 兩天（T1 對得上、T2 的池子入選對不上）、美股 3 次、舊五條件 T0 家人看到（還沒有池子紀錄）＋T1 背景照算。
    三份紀錄檔都有那一行；三族各有一筆代號不在那一行裡的帳列（不算，和 aggregate 一樣）。"""
    _use_bot_state(monkeypatch, paths)
    _store_prices(paths, [("tw", "TAIEX", d, 20000.0) for d in CAL] + [("us", "SPY", d, 600.0) for d in CAL])
    a = ["2301", "2302", "2303", "2304", "2305"]
    ai1 = _version("tw", "ai_v1", T1, ts("2026-10-06", 5), {c: 10.0 for c in a}, picks=a[:3])
    ai1_late = _version("tw", "ai_v1", T1, ts("2026-10-06", 14), {c: 10.0 for c in a}, picks=a[:3])  # E 收盤後
    ai2 = _version("tw", "ai_v1", T2, ts("2026-10-07", 5), {c: 10.0 for c in a}, picks=(a[0], a[1], a[4]))
    v = ["1101", "1102", "1103", "1104", "1105"]
    v1 = _version("tw", "v1", T1, ts("2026-10-06", 5), {c: 20.0 for c in v}, picks=v[:3])
    _write(paths, ai1, ai1_late, ai2, v1)
    _insert_returns(paths, "tw_ai", ai1, 5, {a[0]: (0.10, 0.01, None), a[1]: (0.015, 0.01, None),
                                             a[2]: (-0.03, 0.01, None), a[3]: (0.0, 0.01, None), a[4]: (0.01, 0.01, None)})
    _insert_returns(paths, "tw_ai", ai2, 5, {a[0]: (0.05, 0.0, None), a[1]: (None, None, "missing"),
                                             a[2]: (None, None, "range"), a[3]: (0.01, 0.0, None),
                                             a[4]: (-0.01, 0.0, None)})
    _insert_returns(paths, "tw_v1", v1, 5, {v[0]: (0.03, 0.004, None), v[1]: (0.005, 0.004, None),
                                            v[2]: (-0.01, 0.004, None), v[3]: (0.02, 0.004, None),
                                            v[4]: (0.0, 0.004, None)})
    ledger = [
        _lrow(T1, a[0], 0.10, 0.01), _lrow(T1, a[1], 0.015, 0.01), _lrow(T1, a[2], -0.0305, 0.01),
        _lrow(T1, "2309", 0.5, 0.01),  # 紀錄檔那一行沒有這檔：不算（和 aggregate 一樣）
        _lrow(T1, a[0], 0.9, 0.01, rev=2),  # 不是釘住的 rev：不算
        _lrow(T1, a[0], 0.9, 0.01, v=1),  # v1 帳列：不算
        _lrow(T2, a[0], 0.05, 0.0), _lrow(T2, a[1], 0.03, 0.0), _lrow(T2, a[3], 0.012, 0.0),
        _lrow(T2, a[0], None, None, h=20, skip="missing"),  # 跳過的列：不算進次數
        _lrow(T1, "AAA", 0.02, 0.01, m="us", rule="us_v1"), _lrow(T1, "BBB", 0.01, 0.01, m="us", rule="us_v1"),
        _lrow(T1, "CCC", 0.0, 0.01, m="us", rule="us_v1"),
        _lrow(T1, "DDD", 0.3, 0.01, m="us", rule="us_v1"),  # 美股紀錄那一行沒有這檔：不算
        _lrow(T0, v[0], 0.04, 0.02, rule="v1"), _lrow(T0, v[1], -0.02, 0.02, rule="v1"),
        _lrow(T0, v[2], 0.01, 0.02, rule="v1"),
        _lrow(T0, v[3], 0.3, 0.02, rule="v1"),  # 舊規則紀錄那一行沒有這檔：不算
        _lrow(T0, v[0], None, None, rule="v1", h=20, skip="range"),
    ]
    _jsonl(paths.state_dir / perf.LEDGER_NAME, ledger)

    def ai_line(t: str, at: float, picks: list[str]) -> dict:
        return {"t": t, "rev": 1, "rule": "ai_v1", "computed_at": at, "source": "core_only", "core_version": "x",
                "picks": [{"code": c, "name": "x", "close": 10.0, "ai_source": "AI", "p60": 0} for c in picks]}

    _jsonl(paths.state_dir / sp.AI_HISTORY_NAME, [ai_line(T1, ts("2026-10-06", 5), a[:3]),
                                                  ai_line(T2, ts("2026-10-07", 5), [a[0], a[1], a[3]])])
    _jsonl(paths.state_dir / sp.HISTORY_NAME, [{"t": T0, "market": "neutral", "computed_at": round(ts(T0, 16)),
                                                "picks": [_pick(c, 20.0) for c in v[:3]]}])
    _jsonl(paths.state_dir / perf.US_HISTORY_NAME, [{"t": T1, "market": None, "computed_at": round(ts(T1, 21)),
                                                     "rule": "us_v1", "picks": [_pick(c, 100.0) for c in ("AAA", "BBB",
                                                                                                         "CCC")]}])


def _performance(paths: runner.Paths) -> list[str]:
    ctx = _ctx(paths, SUNDAY)
    try:
        conn = ctx.db()
        calendars = {m: weekly.trading_days(conn, m) for m in ("tw", "us")}
        return returns.report_lines(ctx, conn, calendars)
    finally:
        ctx.close()


def _report_data(paths: runner.Paths) -> returns.Performance:
    """正式的組裝（returns.report_data，日曆和週報一樣用 weekly.trading_days）：測試直接看 entries。"""
    ctx = _ctx(paths, SUNDAY)
    try:
        conn = ctx.db()
        report, state = returns.report_data(ctx, conn, {m: weekly.trading_days(conn, m) for m in ("tw", "us")})
    finally:
        ctx.close()
    assert state == "ok"
    return report


def test_report_compares_live_picks_with_the_pool_and_benchmark(paths, monkeypatch):
    _report_world(paths, monkeypatch)
    lines = _performance(paths)
    assert lines[0] == returns.HEAD == "【推薦成績】"
    ai = lines.index("台股 AI 相關股")
    # 6 次：r 平均 2.9%、0050 平均 0.5%；只有 T1 的 3 次有 p（T2 有值的成員 3/5＜80%）→ 可比的不到 5 次
    assert lines[ai + 1] == "・5 日 6 次：平均 +2.9%，0050 +0.5%；股票池可比的紀錄還不夠（目前 3 次）"
    assert lines[ai + 2:ai + 5] == ["・20 日：紀錄還不夠（目前 0 次）", "・60 日：紀錄還不夠（目前 0 次）",
                                    "・240 日：紀錄還不夠（目前 0 次）"]
    us = lines.index("美股")
    assert us > ai and lines[us + 1] == "・5 日：紀錄還不夠（目前 3 次）"
    control = lines.index("台股舊五條件（對照組）：背景照算的入選股")  # 只有 T1 的背景日（member_returns）
    assert control > us and lines[control + 1] == "・5 日：紀錄還不夠（目前 3 次）"
    seen = lines.index("家人看到的舊五條件（10/1 推薦，只和 0050 比）")  # T0 家人看到的 3 次（帳）
    assert seen > control and lines[seen + 1] == "・5 日：紀錄還不夠（目前 3 次）"
    assert "・1 個推薦日（各族分開數）的推薦和池子紀錄的入選對不上（改用推薦隔天收盤前最後一版）" in lines  # AI T2
    assert "・1 個推薦日（各族分開數）找不到推薦隔天收盤前的池子紀錄（股票池不比）" in lines  # 美股 T1
    assert "・3 筆的股票池有值的成員不到 8 成（不比）" in lines
    assert not any("還沒算完" in line for line in lines)
    # 自我檢查只比台股 AI、美股：T1 三檔（差 0.05% 不算）＋T2 三檔（2302 帳有值、股票池跳過；2304 差 0.2%）
    assert lines[-1] == "・自我檢查：成績帳和股票池算法比對 6 筆，差超過 0.1% 的 1 筆、一邊有值一邊跳過 1 筆"


def test_report_counts_reconcile_with_the_family_score_card(paths, monkeypatch):
    """同一本帳＋紀錄檔：台股 AI＋家人看到的舊五條件＝perf.aggregate 的台股，美股＝美股；每個 h 的次數、跳過數、
    平均報酬都一樣（chosen_ledger_rows 複製了 aggregate 的挑列，審查 C1 r4#1、r2#6、r1#6a）。aggregate 用的那一行
    由 perf 自己的公開函式從紀錄檔重建，不拿 job 的。"""
    _report_world(paths, monkeypatch)
    rows = perf.read_ledger_rows()
    history: dict = {}
    for market, name, fixed, default in (("tw", sp.HISTORY_NAME, perf.TW_OLD_RULE, perf.TW_OLD_RULE),
                                         ("tw", sp.AI_HISTORY_NAME, None, sp.AI_RULE),
                                         ("us", perf.US_HISTORY_NAME, None, perf.US_RULE)):
        objs = [json.loads(x) for x in (paths.state_dir / name).read_text(encoding="utf-8").splitlines()]
        for t, recs in perf.parse_rows(objs, market=market, fixed_rule=fixed, default_rule=default,
                                       today="2026-11-01").items():
            history.setdefault(market, {}).setdefault(t, []).extend(recs)
    lines = {key: perf.pinned_line(history.get(key[0], {}).get(key[1], []), pin) for key, pin in perf.pins_of(rows).items()}
    assert None not in lines.values()  # 每個釘住的 t 都找得到那一行：「代號不在那一行」的過濾三族都會用到
    card = perf.aggregate(rows, lines, [], {}, now=SUNDAY, ledger_rows=len(rows), ledger_full=False)["markets"]
    report = _report_data(paths)
    for market, families in (("tw", ("tw_ai", returns.SEEN_V1)), ("us", ("us",))):
        for h in perf.HORIZONS:
            mine = [e for family in families for e in report.entries[family] if e.h == h]
            counted = [e for e in mine if e.skip is None]
            want = card[market]["h"][str(h)]
            mean = returns._permille(math.fsum(e.r for e in counted) / len(counted)) if counted else None
            assert (len(counted), sum(1 for e in mine if e.skip is not None), mean) == (
                want["n"], want["skipped"], want["r_pm"]), (market, h)
    assert (card["tw"]["h"]["5"]["n"], card["tw"]["h"]["20"]["skipped"], card["us"]["h"]["5"]["n"]) == (9, 2, 3)


def test_control_group_never_takes_a_day_the_family_saw_the_old_rule(paths, monkeypatch):
    """帳裡釘住 v1 的 t（家人看到舊五條件）只在最後那一段；沒釘住的才是對照組的背景日，兩邊不重疊。T0 也補一版準時的
    v1 池子與它的 member_returns：誤算進對照組的話會多出 T0。"""
    _report_world(paths, monkeypatch)
    v = ["1101", "1102", "1103", "1104", "1105"]
    seen_day = _version("tw", "v1", T0, ts("2026-10-02", 5), {c: 20.0 for c in v}, picks=v[:3])
    _write(paths, seen_day)
    _insert_returns(paths, "tw_v1", seen_day, 5, {c: (0.01, 0.0, None) for c in v})
    report = _report_data(paths)
    control = report.entries["tw_v1"]  # 背景日：族是 tw_v1、有選版（p_status 不是 not_compared）
    assert {e.t for e in control} == {T1} and all(e.family == "tw_v1" and e.p_status != returns.NOT_COMPARED
                                                  for e in control)
    seen = report.entries[returns.SEEN_V1]  # 家人看到的：族是 SEEN_V1、不和股票池比
    assert {e.t for e in seen} == {T0} and all(e.family == returns.SEEN_V1 and e.p_status == returns.NOT_COMPARED
                                               for e in seen)
    assert report.seen_runs == ((T0, T0),)
    assert report.flags == Counter(version_fallback=1, version_missing=1)  # 家人看到的舊五條件不選版：不多出 flag


def test_seen_block_splits_its_days_into_runs_on_the_trading_calendar(paths, monkeypatch):
    """家人看到的舊五條件：日期照台股交易日分成連續的幾段（第 2 輪 r3#3）。家人 10/1（T0）、10/2 看到舊五條件，之後 AI
    開了幾天（T1、T2），10/8 AI 又關掉：標題寫「10/1–10/2、10/8」，不是跨過 AI 那幾天的「10/1–10/8」。"""
    _report_world(paths, monkeypatch)
    v = ["1101", "1102", "1103"]
    rows = [json.loads(x) for x in (paths.state_dir / perf.LEDGER_NAME).read_text(encoding="utf-8").splitlines()]
    rows += [_lrow(day, code, 0.01, 0.0, rule="v1") for day in ("2026-10-02", "2026-10-08") for code in v]
    _jsonl(paths.state_dir / perf.LEDGER_NAME, rows)
    lines = [json.loads(x) for x in (paths.state_dir / sp.HISTORY_NAME).read_text(encoding="utf-8").splitlines()]
    lines += [{"t": day, "market": "neutral", "computed_at": round(ts(day, 16)), "picks": [_pick(c, 20.0) for c in v]}
              for day in ("2026-10-02", "2026-10-08")]
    _jsonl(paths.state_dir / sp.HISTORY_NAME, lines)
    assert _report_data(paths).seen_runs == ((T0, "2026-10-02"), ("2026-10-08", "2026-10-08"))
    text = _performance(paths)
    seen = text.index("家人看到的舊五條件（10/1–10/2、10/8 推薦，只和 0050 比）")
    assert text[seen + 1] == "・5 日 9 次：平均 +1.0%，0050 +0.7%"  # (0.04−0.02＋0.01＋6 × 0.01)／9、(3 × 0.02)／9
    assert returns.trading_runs(["2026-10-02", "2026-10-05"], CAL) == (("2026-10-02", "2026-10-05"),)  # 週五、週一相鄰
    assert returns.trading_runs(["2026-10-02", "2026-10-03", "2026-10-05"], CAL) == (  # 週六不在日曆上：自己一段
        ("2026-10-02", "2026-10-02"), ("2026-10-03", "2026-10-03"), ("2026-10-05", "2026-10-05"))


def test_seen_block_without_any_counted_record_is_one_line(paths, monkeypatch):
    """家人看到的舊五條件每個 h 都還沒有次數（例如全是跳過的列）：和三族一樣只寫一行「還沒有成績」。"""
    _report_world(paths, monkeypatch)
    rows = [json.loads(x) for x in (paths.state_dir / perf.LEDGER_NAME).read_text(encoding="utf-8").splitlines()]
    _jsonl(paths.state_dir / perf.LEDGER_NAME, [{**r, "r": None, "b": None, "skip": "range"} if r["rule"] == "v1" else r
                                                 for r in rows])
    lines = _performance(paths)
    assert "・家人看到的舊五條件（10/1 推薦，只和 0050 比）：還沒有成績" in lines
    assert not any(line.startswith("家人看到的舊五條件") for line in lines)


def test_report_does_not_read_a_ledger_from_another_state_dir(paths, monkeypatch):
    _report_world(paths, monkeypatch)
    monkeypatch.setattr(sp, "STATE_DIR", paths.bot_dir / "elsewhere")
    lines = _performance(paths)
    assert "・成績帳不在這個 bot 目錄（測試或 staging）：家人看到的推薦這週沒有算進來" in lines
    assert "・台股 AI 相關股：還沒有成績" in lines and "・美股：還沒有成績" in lines
    control = lines.index("台股舊五條件（對照組）：背景照算的入選股")  # 背景日照樣從 member_returns 來
    assert lines[control + 1] == "・5 日：紀錄還不夠（目前 3 次）"
    assert not any(line.startswith("家人看到的舊五條件") for line in lines)  # 沒讀帳就沒有這一段


def test_report_says_when_the_ledger_cannot_be_read(paths, monkeypatch):
    _report_world(paths, monkeypatch)
    (paths.state_dir / perf.LEDGER_NAME).unlink()
    (paths.state_dir / perf.LEDGER_NAME).mkdir()
    lines = _performance(paths)
    assert any(line.startswith("・成績帳讀不到（") and line.endswith("：家人看到的推薦這週沒有算進來") for line in lines)


def _entry(t: str, r: float, p_status: str, p: Optional[float]) -> returns.Entry:
    return returns.Entry("tw_ai", "tw", t, "2330", 5, r, 0.004, None, p_status, p)


def test_pool_comparison_uses_only_the_entries_that_have_p():
    """平均、0050 算全部次數；和股票池比只用有 p 的那幾次（審查 C1 r1#1、r3#1 的反例 A：分母不同時方向會整個反過來）。
    期望值手算：5 次 −1%（股票池 0%，p ok）＋5 次 +20%（version_missing）→ 10 次平均 (−0.05＋1.00)／10＝+9.5%，
    0050 每次都是 +0.4%；可比的 5 次：推薦 −1.0%、股票池 0.0%，−1% > 0% 一次都不成立 → 贏 0/5。"""
    paired = [_entry(f"2026-10-0{i}", -0.01, "ok", 0.0) for i in range(1, 6)]
    unpaired = [_entry(f"2026-10-1{i}", 0.20, "version_missing", None) for i in range(5)]
    assert returns._h_line(5, returns.stats_of(paired + unpaired, 5), "0050") == (
        "・5 日 10 次：平均 +9.5%，0050 +0.4%；可和股票池比的 5 次：推薦 -1.0%、股票池 0.0%（贏 0/5）")
    # 可比的只有 3 次：(−0.03＋1.00)／8＝+12.1%，後半寫紀錄還不夠
    assert returns._h_line(5, returns.stats_of(paired[:3] + unpaired, 5), "0050") == (
        "・5 日 8 次：平均 +12.1%，0050 +0.4%；股票池可比的紀錄還不夠（目前 3 次）")
    # 家人看到的舊五條件那一段不和股票池比
    assert returns._h_line(5, returns.stats_of(paired + unpaired, 5), "0050", with_pool=False) == (
        "・5 日 10 次：平均 +9.5%，0050 +0.4%")


def _relay_chunks(text: str, limit: int = 1890) -> list[str]:
    """ops/maintenance/discord_relay.split_chunks 的同一種切法（照行切、每段最多 1,890 字）：那支程式不在這個 repo。"""
    chunks, current = [], ""
    for line in text.strip().splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def test_weekly_report_keeps_phase_a_content_and_fits_three_chunks(paths, monkeypatch):
    _report_world(paths, monkeypatch)
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    ctx = _ctx(paths, SUNDAY)
    try:
        text = weekly.build_report(ctx, D("2026-11-01"))
    finally:
        ctx.close()
    assert text.startswith("📊 股票推薦回測週報（11/1）") and "【資料收集】" in text and "【台股休市表】" in text
    assert "【推薦成績】\n台股 AI 相關股\n・5 日 6 次：平均 +2.9%" in text
    assert "自動冷凍績效差的股票：尚未啟用" in text and "成績比較（持有" not in text  # C1 上線了，不再列成之後的檢查點
    assert text.index("【推薦成績】") < text.index("【之後的檢查點】") and text.rstrip().endswith(weekly.METHOD)
    assert text.count("推薦隔天收盤進場") == 1  # 算法只寫在最後一行（【推薦成績】的標題不重複）
    assert len(text) <= weekly.REPORT_MAX_CHARS and len(_relay_chunks(text)) <= 3


def test_largest_possible_report_still_fits_three_chunks():
    """三族加上家人看到的舊五條件、4 個天數都有數字、所有說明行都出現（數字取最長的寫法）：整份週報的 C1 這段加上 A 段
    還是三段以內。"""
    entries = {family: [returns.Entry(family, "tw", "2026-10-01", "2330", h, -0.987654, 12.3456, None, "ok", -0.5)
                        for h in perf.HORIZONS for _ in range(1000)]
               for family in returns.FAMILY_MARKET}
    seen_days = weekdays("2026-10-08", "2026-10-31")[::2]  # 家人看到的日子斷成 9 段（最長的標題）
    entries[returns.SEEN_V1] = [returns.Entry(returns.SEEN_V1, "tw", day, "2330", h, -0.987654, 12.3456, None,
                                              returns.NOT_COMPARED, None)
                                for h in perf.HORIZONS for day in seen_days for _ in range(120)]
    entries["tw_ai"].append(returns.Entry("tw_ai", "tw", "2026-10-01", "1", 5, 0.1, 0.1, None, "pending", None))
    entries["us"].append(returns.Entry("us", "us", "2026-10-01", "A", 5, 0.1, 0.1, None, "pool_missing", None))
    report = returns.Performance(entries, Counter(version_fallback=999, version_missing=999),
                                 Counter(compared=99999, diff=99999, oneside=99999),
                                 seen_runs=returns.trading_runs(seen_days, weekdays("2026-10-01", "2026-11-30")))
    lines = returns.format_lines(report, "error:PermissionError", perf.HORIZONS)
    assert len(lines) == 1 + 3 * 5 + 5 + 4 + 2  # 標題、三族 × (標題＋4 天)、家人看到的那段、4 行說明、帳、自我檢查
    c1 = "\n".join(lines)
    assert len(c1) < 1600 and max(len(line) for line in lines) < 120
    a_part = 1500  # A 段（收集狀態、休市表兩行警示、檢查點、算法）實際約 1,000 字，這裡從寬算
    assert len(c1) + a_part < weekly.REPORT_MAX_CHARS
    assert len(_relay_chunks(("x" * 100 + "\n") * 15 + c1)) <= 2


def test_missing_versions_before_pool_logging_say_why():
    entries = {family: [] for family in returns.FAMILY_MARKET}
    before = returns.Performance(entries, Counter(version_missing=4), Counter(),
                                 missing_ts=frozenset({"2026-10-08", "2026-10-22"}), first_pool="2026-10-27")
    assert "・4 個推薦日（各族分開數）找不到推薦隔天收盤前的池子紀錄（股票池不比；池子紀錄 10/27 才開始記）" in (
        returns.format_lines(before, "ok", perf.HORIZONS))
    after = returns.Performance(entries, Counter(version_missing=1), Counter(), missing_ts=frozenset({"2026-11-02"}),
                                first_pool="2026-10-27")
    assert "・1 個推薦日（各族分開數）找不到推薦隔天收盤前的池子紀錄（股票池不比）" in returns.format_lines(
        after, "ok", perf.HORIZONS)


def test_pct_and_short_records():
    assert [returns.pct(x) for x in (0.0294, -0.0305, 0.0, 0.00049, -0.00049, 0.0125, None)] == [
        "+2.9%", "-3.1%", "0.0%", "0.0%", "0.0%", "+1.3%", "—"]
    for value in (0.0294, -0.0305, 0.00451, -0.00451, 0.1235):  # 和家人成績卡的千分位同一種四捨五入
        assert returns._permille(value) == sp._permille(value)
    stats = returns.stats_of([returns.Entry("us", "us", T, "A", 5, 0.1, 0.0, None, "ok", 0.0)] * 4, 5)
    assert returns._h_line(5, stats, "SPY") == "・5 日：紀錄還不夠（目前 4 次）"


# ── 和其他階段的隔離 ─────────────────────────────────────────────────────────
@pytest.fixture
def ready(paths) -> runner.Paths:
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state(), us=fx.us_snapshot())
    fx.make_market_db(paths.market_db, fx.standard_market_rows(), obs={"tw.holidays.2026": {"rows": []}})
    return paths


def _last_run(paths: runner.Paths) -> dict:
    return json.loads(paths.last_run.read_text(encoding="utf-8"))


def test_perf_import_error_fails_only_the_returns_stage(ready, monkeypatch):
    monkeypatch.setitem(sys.modules, "stock_picks_perf", None)  # perf 部署到一半、import 不了
    assert runner.run(ready, now=SUNDAY) == runner.EXIT_FAILED
    out = _last_run(ready)
    stages = {name: s["status"] for name, s in out["stages"].items()}
    assert stages == {"archive": "ok", "pool": "ok", "prices": "ok", "returns": "error", "weekly": "queued"}
    assert out["stages"]["returns"]["error"] in ("ImportError", "ModuleNotFoundError")
    text = (ready.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "【推薦成績】這週算不出來（" in text and "【資料收集】" in text  # 週報照送，只有這一段換成說明


def test_busy_market_db_in_the_returns_stage_is_not_a_failure(ready, monkeypatch):
    def busy(*_args, **_kwargs):
        raise stock_db_ro.DbBusy("SQLITE_BUSY")

    for name in ("tw_calendar", "us_calendar"):
        monkeypatch.setattr(stock_db_ro, name, busy)
    assert runner.run(ready, now=SUNDAY) == runner.EXIT_OK
    out = _last_run(ready)
    assert out["status"] == "ok" and out["consecutive_failures"] == 0
    assert out["stages"]["returns"]["status"] == "busy" and out["stages"]["weekly"]["status"] == "queued"
    assert out["stages"]["returns"]["markets"] == {"tw": "busy", "us": "busy"}


def test_returns_stage_error_does_not_stop_later_runs_or_stages(ready, monkeypatch):
    def boom(_ctx):
        raise RuntimeError("returns broke")

    monkeypatch.setattr(returns, "returns_stage", boom)
    assert runner.run(ready, now=fx.NOW) == runner.EXIT_FAILED
    stages = _last_run(ready)["stages"]
    assert stages["archive"]["status"] == stages["pool"]["status"] == stages["prices"]["status"] == "ok"
    assert stages["returns"]["status"] == "error" and stages["weekly"]["status"] == "skipped:not_due"


def test_a_full_run_never_calls_bot_write_functions_or_locks(paths, monkeypatch):
    """PLAN §4.1／§9（審查 C1 r2#2、r2#7）：returns 是 job 第一次呼叫 perf（evaluate、load_tw／load_us、
    read_ledger_rows、parse_rows）。bot 與 perf 的寫入函式、跨程序鎖全部換成「先記下來再丟例外」，週日跑一整輪：
    returns ok 而且真的凍結了成員、週報排進 outbox、真的讀了帳，而且一次都沒呼叫。只靠丟例外不夠：階段、週報的
    那一段、load_ledger 都會把例外吞成一行說明。"""
    _scenario(paths)
    _use_bot_state(monkeypatch, paths)
    ledger = [_lrow(T, code, r, 0.0, rule="v1") for code, r in (("2330", 0.05), ("1101", 0.0), ("6666", 0.0))]
    _jsonl(paths.state_dir / perf.LEDGER_NAME, ledger + [_lrow(T, c, 0.0, 0.0, m="us", rule="us_v1")
                                                        for c in ("NVDA", "AAPL")])
    _jsonl(paths.state_dir / sp.HISTORY_NAME, [{"t": T, "market": "neutral", "computed_at": round(ts(T, 16)),
                                                "picks": [_pick(c, 100.0) for c in ("2330", "1101", "6666")]}])
    calls: list[str] = []

    def guard(name: str):
        def boom(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"stock_picks_review must not call {name}")
        return boom

    # 照名稱樣式換（第 2 輪 r2#F）：寫入、追加、commit、存檔、清檔、更新、回補，以及任何帶 lock 的。lock 也會碰到 sp 的
    # Yahoo 冷卻（_block_yahoo、_yahoo_blocked…，名字裡有 block）：只在連網的路徑上，job 一樣不該碰，照樣換。
    risky = re.compile(r"^_?(write|append|commit|save|sweep|update|backfill)")
    patched: set[str] = set()
    for module in (sp, perf):
        for name in sorted(dir(module)):
            if callable(getattr(module, name)) and (risky.match(name) or "lock" in name.lower()):
                monkeypatch.setattr(module, name, guard(f"{module.__name__}.{name}"))
                patched.add(f"{module.__name__}.{name}")
    assert {"stock_picks._write_json_file", "stock_picks._state_file_lock", "stock_picks_perf.update",  # 改名時不能默默少
            "stock_picks_perf.backfill_prices", "stock_picks_perf._append_rows", "stock_picks_perf._write_library",
            "stock_picks_perf._sweep_stale_tmp"} <= patched
    assert runner.run(paths, now=SUNDAY) == runner.EXIT_OK
    out = _last_run(paths)
    assert out["stages"]["returns"]["status"] == "ok" and out["stages"]["returns"]["frozen"] == 7
    assert out["stages"]["weekly"]["status"] == "queued"
    text = (paths.outbox / "2026-11-01-stock-picks-weekly.md").read_text(encoding="utf-8")
    assert "家人看到的舊五條件（10/1 推薦，只和 0050 比）" in text  # 真的讀了帳
    assert "這週算不出來" not in text and "成績帳讀不到" not in text and "不在這個 bot 目錄" not in text
    assert calls == []

