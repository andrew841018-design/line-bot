"""股票推薦回測 C1：成員報酬（returns 階段）、股票池基準 p、選版、週報的推薦成績（jobs/stock_picks_review_returns.py）。

盤勢 DB 用盤勢 job 自己的 market_store 建（同 schema：coverage、exrights、quarantine、obs、calendar_overrides 都在），
資料是合成、縮小過的；bot 目錄與 outbox 由 conftest 指到暫存目錄；不連網。
"""

from __future__ import annotations

import json
import sqlite3
import sys
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


def _insert_returns(paths: runner.Paths, family: str, version: pool.Version, h: int, values: dict) -> None:
    """member_returns 直接寫：{代號: (r, b, skip)}。"""
    conn = rdb.open_db(paths.db)
    try:
        with rdb.transaction(conn):
            conn.executemany(returns.INSERT_SQL, [
                (version.m, family, version.t, version.ver_s, version.derive_version, code, h, 2, r, b, skip, "{}", NOW)
                for code, (r, b, skip) in values.items()])
    finally:
        conn.close()


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
        assert cols == ["m", "family", "t", "ver_s", "derive_version", "code", "h", "ledger_version", "r", "b", "skip",
                        "inputs_json", "frozen_at"]
        pk = [row[1] for row in sorted(con.execute("PRAGMA table_info(member_returns)"), key=lambda r: r[5]) if row[5]]
        assert pk == ["m", "family", "t", "ver_s", "derive_version", "code", "h", "ledger_version"]
        with pytest.raises(sqlite3.IntegrityError):  # 有值的列一定沒有 skip、有 b（寫入不會默默丟掉不合格的列）
            con.execute(returns.INSERT_SQL, ("tw", "tw_v1", "2026-10-27", 1, 1, "2330", 5, 2, 0.1, None, None, "{}", 1.0))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(returns.INSERT_SQL, ("tw", "tw_v1", "2026-10-27", 1, 1, "2330", 5, 2, 0.1, 0.0, "range", "{}", 1.0))
        key = ("tw", "tw_v1", "2026-10-27", 1, 1, "2330", 5, 2)
        con.execute(returns.INSERT_SQL, (*key, 0.1, 0.0, None, "{}", 1.0))
        con.execute(returns.INSERT_SQL, (*key, 0.9, 0.0, None, "{}", 2.0))  # 凍結後不改：同一個鍵不覆寫、不報錯
        assert con.execute("SELECT r, frozen_at FROM member_returns").fetchall() == [(0.1, 1.0)]
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
def _scenario(paths: runner.Paths) -> dict:
    """台股 v1 一版（E 收盤前）＋同一天 E 收盤剛好那一刻才算出的一版（不算）＋日曆上還沒有 E 的 AI 一版＋美股一版。"""
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
    # 價格庫：DB 沒有的成員（6666、AAPL），以及比 DB 早的日曆（台股 TAIEX、美股 VOO）
    early = weekdays("2026-08-03", "2026-08-31")
    _store_prices(paths, [("tw", "6666", d, 80.0) for d in DAYS] + [("us", "AAPL", d, 150.0) for d in DAYS]
                  + [("tw", "TAIEX", d, 19000.0) for d in early] + [("us", "VOO", d, 450.0) for d in early])
    members = {"2330": 100.0, "1101": 50.0, "6666": 80.0, "9999": None, "7777": 45.0}
    on_time = _version("tw", "v1", T, ts("2026-10-02", 5, 20), members, picks=("2330", "1101", "6666"))
    late = _version("tw", "v1", T, TW_DEADLINE, members, picks=("2330", "1101", "6666"))
    no_e = _version("tw", "ai_v1", DAYS[-1], ts("2026-10-21", 5), {"2330": 109.2})
    us = _version("us", "us_v1", T, ts("2026-10-02", 8), {"NVDA": 200.0, "AAPL": 150.0})  # 美東 10/01 20:00
    _write(paths, on_time, late, no_e, us)
    return {"on_time": on_time, "late": late, "no_e": no_e, "us": us}


def test_returns_stage_freezes_due_members_and_waits_for_the_rest(paths):
    v = _scenario(paths)
    out = _stage(paths)
    assert out["status"] == "ok" and out["markets"] == {"tw": "ok", "us": "ok"}
    assert (out["versions"], out["due_versions"], out["late"], out["wait_e"]) == (4, 2, 1, 1)
    assert (out["evaluated"], out["frozen"], out["waiting"]) == (28, 7, 21)  # (5＋2) 檔 × 4 個 h；只有 5 日到期
    assert out["families"] == {"tw_v1": {"evaluated": 20, "frozen": 5, "waiting": 15},
                               "tw_ai": {"evaluated": 0, "frozen": 0, "waiting": 0},
                               "us": {"evaluated": 8, "frozen": 2, "waiting": 6}}
    assert out["waits"] == {"not_due": 21} and out["budget_hit"] is False
    rows = _returns(paths)
    on_time = v["on_time"].ver_s
    assert {k for k in rows if k[0] == "tw_v1"} == {("tw_v1", T, on_time, c, 5) for c in ("2330", "1101", "6666", "9999",
                                                                                          "7777")}
    first = rows[("tw_v1", T, on_time, "2330", 5)]
    assert (first["m"], first["derive_version"], first["ledger_version"], first["r"], first["b"], first["skip"]) == (
        "tw", 1, 2, 0.05, 0.0, None)  # 109.2 ÷ 104（E 的收盤）− 1
    assert json.loads(first["inputs_json"]) == {"th": "2026-10-09", "c0": 100.0, "ce": 104.0, "ch": 109.2, "cf": 1.0,
                                                "be": 100.0, "bh": 100.0, "bf": 1.0}
    assert first["frozen_at"] == NOW
    assert rows[("tw_v1", T, on_time, "6666", 5)]["r"] == 0.0  # DB 沒有的成員：價格庫供價
    missing = rows[("tw_v1", T, on_time, "9999", 5)]  # 沒有 c0：凍結時記 missing，不編數字
    assert (missing["r"], missing["b"], missing["skip"]) == (None, None, "missing")
    assert rows[("tw_v1", T, on_time, "7777", 5)]["skip"] == "mismatch"  # c0 和 DB 的 t 收盤差 > 0.5%（perf 的規則）
    assert not any(k[2] == v["late"].ver_s for k in rows)  # E 收盤那一刻才算出來的版本永遠不算
    assert {(k[0], k[3], k[4]) for k in rows if k[0] == "us"} == {("us", "NVDA", 5), ("us", "AAPL", 5)}
    assert rows[("us", T, v["us"].ver_s, "AAPL", 5)]["r"] == 0.0  # 美股價格庫供價
    again = _stage(paths)  # 冪等：凍結的不再算，等的照樣等
    assert (again["evaluated"], again["frozen"], again["waiting"]) == (21, 0, 21)
    assert len(_returns(paths)) == 7


def test_returns_stage_dry_run_counts_but_writes_nothing(paths):
    _scenario(paths)
    out = _stage(paths, dry_run=True)
    assert out["status"] == "dry_run" and out["frozen"] == 7
    assert _returns(paths) == {}


def test_returns_stage_resumes_in_bounded_batches(paths, monkeypatch):
    """一輪只載入 MAX_CANDIDATES 筆（至少一個版本）：剩下的下一輪接著算；快到期的版本排前面，不會被 240 日擋住。"""
    v = _scenario(paths)
    monkeypatch.setattr(returns, "MAX_CANDIDATES", 1)
    first = _stage(paths)
    assert first["budget_hit"] is True and first["due_versions"] == 1 and first["frozen"] in (2, 5)
    second = _stage(paths)  # 第一版剩下的都是 20 日以後：另一版的 5 日排前面
    assert second["due_versions"] == 1 and second["frozen"] in (2, 5) and first["frozen"] + second["frozen"] == 7
    assert {k[0] for k in _returns(paths)} == {"tw_v1", "us"}
    assert len({k[2] for k in _returns(paths)}) == 2 and v["late"].ver_s not in {k[2] for k in _returns(paths)}


def test_due_order_compares_markets_with_different_calendar_lengths():
    """排先後用「離 T_h 還有幾個交易日」：價格庫讓某個市場的日曆多了一年也一樣比（只影響順序，不影響判斷）。"""
    longer = tuple(weekdays("2025-09-01", "2026-08-31")) + tuple(DAYS)
    assert returns._sessions_to(longer, T, 5) == returns._sessions_to(tuple(DAYS), T, 5) == -7  # T_5＝10/09，日曆到 10/20
    assert returns._sessions_to(tuple(DAYS), T, 20) == 8


def test_returns_stage_stops_at_the_time_budget_after_minimum_progress(paths, monkeypatch):
    _scenario(paths)
    monkeypatch.setattr(returns, "BUDGET_S", -1.0)  # 一開始就超過預算
    monkeypatch.setattr(returns, "CHECK_EVERY", 3)
    out = _stage(paths)
    assert out["budget_hit"] is True and out["due_versions"] == 1 and out["evaluated"] == 3  # 至少評完前 3 筆


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


def test_returns_stage_needs_its_table_in_a_real_run(paths):
    rdb.open_db(paths.db).close()
    con = sqlite3.connect(paths.db)
    con.execute("DROP TABLE member_returns")
    con.commit()
    con.close()
    with pytest.raises(rdb.SchemaError):
        _stage(paths)
    assert _stage(paths, dry_run=True) == {"status": "skipped:no_table"}  # 唯讀開的舊庫：不算、不失敗


def test_c0_none_member_is_recorded_missing_even_if_perf_could_value_it(paths):
    """美股 t 之後有分割時 perf 不比 c0（_c0_ok 直接過）：沒有 c0 的成員照樣記 missing（不拿後來的價格回填）。"""
    cand = perf.Candidate("us", "us_v1", T, 1, "AAPL", None, 5)
    value = {"v": 2, "m": "us", "rule": "us_v1", "t": T, "rev": 1, "code": "AAPL", "h": 5, "th": "2026-10-09",
             "r": 0.1, "b": 0.0, "skip": None, **{k: 1.0 for k in perf.INPUT_KEYS}}
    row = returns.member_row(perf, "us", 1, cand, value, NOW)
    assert row[8:11] == (None, None, "missing")
    with pytest.raises(ValueError):  # 看不懂的 perf 列不寫
        returns.member_row(perf, "us", 1, perf.Candidate("us", "us_v1", T, 1, "AAPL", 1.0, 5), {**value, "skip": "bogus"},
                           NOW)


# ── C1 契約：job 組的 MarketData 重算 perf 帳的一筆，結果完全一樣 ───────────────────────────────
def _history(name: str, lines: list[dict]) -> None:
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


def _report_world(paths: runner.Paths, monkeypatch) -> None:
    """AI 兩天（T1 對得上、T2 的池子入選對不上）、美股 3 次、舊五條件 T0 家人看到（還沒有池子紀錄）＋T1 背景照算。"""
    monkeypatch.setattr(sp, "STATE_DIR", paths.state_dir)  # 正式環境：bot 的 state/ 就是 job 的 state/
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
        _lrow(T0, v[0], 0.04, 0.02, rule="v1"), _lrow(T0, v[1], -0.02, 0.02, rule="v1"),
        _lrow(T0, v[2], 0.01, 0.02, rule="v1"),
    ]
    (paths.state_dir / perf.LEDGER_NAME).write_text("".join(json.dumps(r) + "\n" for r in ledger), encoding="utf-8")

    def ai_line(t: str, at: float, picks: list[str]) -> dict:
        return {"t": t, "rev": 1, "rule": "ai_v1", "computed_at": at, "source": "core_only", "core_version": "x",
                "picks": [{"code": c, "name": "x", "close": 10.0, "ai_source": "AI", "p60": 0} for c in picks]}

    lines = [ai_line(T1, ts("2026-10-06", 5), a[:3]), ai_line(T2, ts("2026-10-07", 5), [a[0], a[1], a[3]])]
    (paths.state_dir / sp.AI_HISTORY_NAME).write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def _performance(paths: runner.Paths) -> list[str]:
    ctx = _ctx(paths, SUNDAY)
    try:
        conn = ctx.db()
        calendars = {m: weekly.trading_days(conn, m) for m in weekly.CALENDAR_CODES}
        return returns.report_lines(ctx, conn, calendars)
    finally:
        ctx.close()


def test_report_compares_live_picks_with_the_pool_and_benchmark(paths, monkeypatch):
    _report_world(paths, monkeypatch)
    lines = _performance(paths)
    assert lines[0] == returns.HEAD
    ai = lines.index("台股 AI 相關股")
    # 6 次：r 平均 2.9%；股票池只有 T1 有 p（T2 有值的成員 3/5＜80%）：p＝1.9%，贏 1/3；0050 平均 0.5%
    assert lines[ai + 1] == "・5 日 6 次：平均 +2.9%，股票池 +1.9%（贏股票池 1/3 次），0050 +0.5%"
    assert lines[ai + 2:ai + 5] == ["・20 日：紀錄還不夠（目前 0 次）", "・60 日：紀錄還不夠（目前 0 次）",
                                    "・240 日：紀錄還不夠（目前 0 次）"]
    us = lines.index("美股")
    assert lines[us + 1] == "・5 日：紀錄還不夠（目前 3 次）"
    v1 = lines.index("對照組舊五條件（到 10/1 是家人看到的推薦，之後背景照算）")
    # T0 家人看到的 3 次（沒有池子紀錄）＋T1 背景照算 3 次（member_returns）；p 只有 T1：0.9%，贏 1/3
    assert lines[v1 + 1] == "・5 日 6 次：平均 +0.9%，股票池 +0.9%（贏股票池 1/3 次），0050 +1.2%"
    assert "・1 天推薦和池子紀錄的入選對不上（改用推薦隔天收盤前最後一版）" in lines
    assert "・2 天找不到推薦隔天收盤前的池子紀錄（股票池不比）" in lines  # 美股 T1、舊五條件 T0
    assert "・3 筆的股票池有值的成員不到 8 成（不比）" in lines
    # 自我檢查：T1 三檔（差 0.05% 不算）＋T2 三檔（2302 帳有值、股票池跳過；2304 差 0.2%）
    assert lines[-1] == "・自我檢查：成績帳和股票池算法比對 6 筆，差超過 0.1% 的 1 筆、一邊有值一邊跳過 1 筆"


def test_report_does_not_read_a_ledger_from_another_state_dir(paths, monkeypatch):
    _report_world(paths, monkeypatch)
    monkeypatch.setattr(sp, "STATE_DIR", paths.bot_dir / "elsewhere")
    lines = _performance(paths)
    assert "・成績帳不在這個 bot 目錄（測試或 staging）：家人看到的推薦這週沒有算進來" in lines
    assert "・台股 AI 相關股：還沒有成績" in lines and "・美股：還沒有成績" in lines
    v1 = lines.index("對照組舊五條件（背景照算，家人沒看到）")  # 背景日照樣從 member_returns 來
    assert lines[v1 + 1] == "・5 日：紀錄還不夠（目前 3 次）"


def test_report_says_when_the_ledger_cannot_be_read(paths, monkeypatch):
    _report_world(paths, monkeypatch)
    (paths.state_dir / perf.LEDGER_NAME).unlink()
    (paths.state_dir / perf.LEDGER_NAME).mkdir()
    lines = _performance(paths)
    assert any(line.startswith("・成績帳讀不到（") and line.endswith("：家人看到的推薦這週沒有算進來") for line in lines)


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
    assert "【推薦成績】" in text and "・5 日 6 次：平均 +2.9%" in text
    assert "自動冷凍績效差的股票：尚未啟用" in text and "成績比較（持有" not in text  # C1 上線了，不再列成之後的檢查點
    assert text.index("【推薦成績】") < text.index("【之後的檢查點】") and text.rstrip().endswith(weekly.METHOD)
    assert len(text) <= weekly.REPORT_MAX_CHARS and len(_relay_chunks(text)) <= 3


def test_largest_possible_report_still_fits_three_chunks():
    """三族、4 個天數都有數字、所有說明行都出現（數字取最長的寫法）：整份週報的 C1 這段加上 A 段還是三段以內。"""
    entries = {family: [returns.Entry(family, "tw", "2026-10-01", "2330", h, -0.987654, 12.3456, None, "ok", -0.5, live)
                        for h in perf.HORIZONS for live in (True, False) for _ in range(500)]
               for family in returns.FAMILY_MARKET}
    from collections import Counter

    report = returns.Performance(entries, Counter(version_fallback=999, version_missing=999),
                                 Counter(compared=99999, diff=99999, oneside=99999), {"tw_v1": "2026-12-31"})
    entries["tw_ai"].append(returns.Entry("tw_ai", "tw", "2026-10-01", "1", 5, 0.1, 0.1, None, "pending", None, True))
    entries["us"].append(returns.Entry("us", "us", "2026-10-01", "A", 5, 0.1, 0.1, None, "pool_missing", None, True))
    lines = returns.format_lines(report, "error:PermissionError", perf.HORIZONS)
    c1 = "\n".join(lines)
    assert len(c1) < 1200 and max(len(line) for line in lines) < 120
    a_part = 1500  # A 段（收集狀態、休市表兩行警示、檢查點、算法）實際約 1,000 字，這裡從寬算
    assert len(c1) + a_part < weekly.REPORT_MAX_CHARS
    assert len(_relay_chunks(("x" * 100 + "\n") * 15 + c1)) <= 2


def test_missing_versions_before_pool_logging_say_why():
    from collections import Counter

    entries = {family: [] for family in returns.FAMILY_MARKET}
    before = returns.Performance(entries, Counter(version_missing=4), Counter(), {},
                                 frozenset({"2026-10-08", "2026-10-22"}), "2026-10-27")
    assert "・4 天找不到推薦隔天收盤前的池子紀錄（股票池不比；池子紀錄 10/27 才開始記）" in returns.format_lines(
        before, "ok", perf.HORIZONS)
    after = returns.Performance(entries, Counter(version_missing=1), Counter(), {}, frozenset({"2026-11-02"}),
                                "2026-10-27")
    assert "・1 天找不到推薦隔天收盤前的池子紀錄（股票池不比）" in returns.format_lines(after, "ok", perf.HORIZONS)


def test_pct_and_short_records():
    assert [returns.pct(x) for x in (0.0294, -0.0305, 0.0, 0.00049, -0.00049, 0.0125, None)] == [
        "+2.9%", "-3.1%", "0.0%", "0.0%", "0.0%", "+1.3%", "—"]
    for value in (0.0294, -0.0305, 0.00451, -0.00451, 0.1235):  # 和家人成績卡的千分位同一種四捨五入
        assert returns._permille(value) == sp._permille(value)
    stats = returns.stats_of([returns.Entry("us", "us", T, "A", 5, 0.1, 0.0, None, "ok", 0.0, True)] * 4, 5)
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
