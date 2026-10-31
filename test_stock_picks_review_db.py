"""股票推薦回測 A 段：review.sqlite 的 schema 與價格庫同步（jobs/stock_picks_review_db.py）。

盤勢 DB 是測試自己建的小 sqlite（closes、obs 兩張表）；bot 目錄由 conftest 指到暫存目錄。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta

import pytest

import stock_db_ro
import test_stock_picks_review_fixtures as fx
from jobs import stock_picks_review as runner
from jobs import stock_picks_review_db as rdb
from jobs import stock_picks_review_pool as pool


@pytest.fixture
def paths() -> runner.Paths:
    p = runner.resolve_paths()
    assert p.bot_dir != runner.BASE
    runner.prepare_root(p)  # 和第一輪一樣：root 與 spool/
    return p


def _ctx(paths, now=fx.NOW, **kwargs) -> runner.Context:
    return runner.Context(paths=paths, now=now, **kwargs)


def _sync(paths, now=fx.NOW) -> dict:
    ctx = _ctx(paths, now)
    try:
        return rdb.prices_stage(ctx)
    finally:
        ctx.close()


def _prices(paths, where="1", params=()) -> dict:
    con = sqlite3.connect(paths.db)
    try:
        return {(m, code, d): (close, src, seen) for m, code, d, close, src, seen in con.execute(
            f"SELECT m, code, d, close, src, seen_at FROM prices WHERE {where}", params)}
    finally:
        con.close()


def _log_pools(paths, now=fx.NOW) -> None:
    fx.write_states(paths.state_dir, main=fx.main_snapshot(), ai=fx.ai_state())
    ctx = _ctx(paths, now)
    try:
        pool.archive_stage(ctx)
        pool.pool_stage(ctx)
    finally:
        ctx.close()


def _edit_market(paths, sql: str, params=()) -> None:
    con = sqlite3.connect(paths.market_db)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()
    later = os.stat(paths.market_db).st_mtime + 5  # 同一秒內改兩次也要看得出 mtime 變了
    os.utime(paths.market_db, (later, later))


# ── schema ─────────────────────────────────────────────────────────────────
def test_schema_is_created_with_user_version_and_wal(paths):
    con = rdb.open_db(paths.db)
    try:
        assert rdb.user_version(con) == rdb.SCHEMA_VERSION == 2
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert set(rdb.A_TABLES) | set(rdb.C1_TABLES) <= tables
        # member_returns 的 PK 帶 rule（池子版本的身分）與 calc_version（job 端算法的版本，審查 C1 r2#1、r4#3）
        info = list(con.execute("PRAGMA table_info(member_returns)"))
        assert [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]] == [
            "m", "family", "rule", "t", "ver_s", "derive_version", "code", "h", "ledger_version", "calc_version"]
        assert {row[1] for row in info if row[3]} >= {"rule", "calc_version"}  # NOT NULL
    finally:
        con.close()


def test_member_returns_key_matches_the_migration(paths):
    """returns 與週報比對的 PK（MEMBER_RETURNS_KEY）就是 migration 2 建出來的那一組。"""
    con = rdb.open_db(paths.db)
    try:
        assert rdb.member_returns_key(con) == rdb.MEMBER_RETURNS_KEY
    finally:
        con.close()


def test_calendar_symbols_match_stock_db_ro():
    """job 自己的日曆代號（週報的【資料收集】也用，所以不 import stock_db_ro）要和盤勢 DB 的日曆同一組；價格庫固定要收的
    代號從它推導，值和順序都不變。"""
    assert rdb.CALENDAR_SYMBOLS == {"tw": (stock_db_ro.TW_CALENDAR_SYMBOL,),
                                    "us": tuple(stock_db_ro.US_CALENDAR_SYMBOLS)}
    assert rdb.TW_EXTRA == ("0050", "TAIEX") and rdb.US_EXTRA == ("SPY", "VOO", "QQQ")


def test_newer_schema_from_a_later_phase_still_runs(paths):
    """C 段回滾後，DB 的 user_version 比程式新：只要 A 段的表都在就照跑（審查 r2#6）。"""
    con = rdb.open_db(paths.db)
    con.execute("CREATE TABLE episodes (family TEXT)")
    con.execute("PRAGMA user_version = 4")
    con.close()
    con = rdb.open_db(paths.db)
    try:
        assert rdb.user_version(con) == 4
        assert {"episodes", "member_returns", "prices"} <= {n for (n,) in con.execute("SELECT name FROM sqlite_master")}
    finally:
        con.close()


def test_schema_with_missing_tables_is_an_error_not_a_silent_rebuild(paths):
    con = sqlite3.connect(paths.db)
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    with pytest.raises(rdb.SchemaError):
        rdb.open_db(paths.db)


def test_readonly_open_creates_nothing(paths):
    assert rdb.open_readonly(paths.db) is None and not paths.db.exists()
    rdb.open_db(paths.db).close()
    before = sorted(os.listdir(paths.root))
    con = rdb.open_readonly(paths.db)
    assert con.execute("SELECT count(*) FROM prices").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO sync_state VALUES ('x', '{}', 0)")
    con.close()
    assert sorted(os.listdir(paths.root)) == before


# ── 價格庫 ─────────────────────────────────────────────────────────────────
def test_prices_mirror_wanted_codes_with_tw_preferred_over_tw_all(paths):
    rows = fx.standard_market_rows()
    rows = [r for r in rows if not (r[0] == "tw_all" and r[1] == fx.AI_IN_POOL[0] and r[2] == fx.weekdays(fx.T, 1)[0])]
    rows.append(("tw_all", fx.AI_IN_POOL[0], fx.weekdays(fx.T, 1)[0], 999.0))  # 同一天兩邊不一樣：用 tw
    rows.append(("tw", "9999", "2026-10-27", 1.0))  # 不在池子紀錄裡的代號不收
    rows.append(("us", "SPY", "2026-10-28", 501.0, 1))  # provisional 不收
    fx.make_market_db(paths.market_db, rows)
    _log_pools(paths)
    out = _sync(paths)
    assert out["status"] == "ok" and out["deletes"] == 0
    prices = _prices(paths)
    codes = {(m, code) for m, code, _d in prices}
    assert {("tw", "TAIEX"), ("tw", "0050"), ("us", "SPY"), ("us", "VOO"), ("us", "QQQ")} <= codes
    assert {("tw", c) for c in fx.AI_CODES} <= codes and ("tw", "9999") not in codes
    assert prices[("tw", fx.AI_IN_POOL[0], "2026-10-27")][:2] == (fx.close_of(fx.AI_IN_POOL[0]), "tw")
    assert len([k for k in prices if k[1] == fx.AI_IN_POOL[0]]) == 30  # tw 有 30 天
    assert len([k for k in prices if k[1] == fx.AI_NOT_IN_POOL[0]]) == 10  # 只有 tw_all 的 10 天
    assert ("us", "SPY", "2026-10-28") not in prices


def test_db_is_truth_inside_its_window_and_older_dates_are_kept(paths):
    code = fx.AI_NOT_IN_POOL[0]  # 只在 tw_all（只留最近 10 個交易日）
    fx.make_market_db(paths.market_db, fx.standard_market_rows())
    _log_pools(paths)
    _sync(paths)
    days = fx.weekdays(fx.T, 10)
    # 盤勢 DB 往前滾一天：最舊的那天不見了（範圍外 → 留著），新的一天進來
    _edit_market(paths, "DELETE FROM closes WHERE market = 'tw_all' AND symbol = ? AND date = ?", (code, days[0]))
    _edit_market(paths, "INSERT INTO closes VALUES ('tw_all', ?, '2026-10-28', 77.0, NULL, 'test', 0, 0)", (code,))
    # 範圍內被 DB 刪掉的日子（例如整檔重寫）：副本也刪；範圍內收盤改了：覆蓋
    _edit_market(paths, "DELETE FROM closes WHERE market = 'tw_all' AND symbol = ? AND date = ?", (code, days[5]))
    _edit_market(paths, "UPDATE closes SET close = 66.0 WHERE market = 'tw_all' AND symbol = ? AND date = ?",
                 (code, days[6]))
    out = _sync(paths, now=fx.NOW + rdb.SYNC_THROTTLE_S + 1)
    assert out["status"] == "ok"
    mine = {d: v for (m, c, d), v in _prices(paths).items() if c == code}
    assert days[0] in mine  # DB 已經滾掉的：留著
    assert days[5] not in mine  # DB 範圍內沒有了：跟著刪
    assert mine[days[6]][0] == 66.0 and mine["2026-10-28"][0] == 77.0


def test_unchanged_rows_are_not_rewritten_and_old_rows_expire(paths):
    fx.make_market_db(paths.market_db, fx.standard_market_rows())
    _log_pools(paths)
    _sync(paths)
    con = sqlite3.connect(paths.db)
    old = (fx.T - timedelta(days=rdb.PRICE_KEEP_DAYS + 1)).isoformat()
    young = (fx.T - timedelta(days=rdb.PRICE_KEEP_DAYS - 30)).isoformat()
    con.executemany("INSERT INTO prices VALUES ('tw', '1101', ?, 1.0, 'tw_all', 0)", [(old,), (young,)])
    con.commit()
    con.close()
    _edit_market(paths, "UPDATE closes SET close = 55.5 WHERE market = 'tw' AND symbol = 'TAIEX' AND date = '2026-10-27'")
    later = fx.NOW + rdb.SYNC_THROTTLE_S + 1
    out = _sync(paths, now=later)
    assert out["status"] == "ok" and out["upserts"] == 1 and out["expired"] == 1
    prices = _prices(paths)
    assert prices[("tw", "TAIEX", "2026-10-27")] == (55.5, "tw", later)
    assert prices[("tw", "TAIEX", "2026-10-26")][2] == fx.NOW  # 沒變的列不重寫
    assert ("tw", "1101", old) not in prices and ("tw", "1101", young) in prices


def test_sync_key_skip_throttle_and_code_change(paths):
    fx.make_market_db(paths.market_db, fx.standard_market_rows())
    _log_pools(paths)
    assert _sync(paths)["status"] == "ok"
    assert _sync(paths, now=fx.NOW + 60)["status"] == "skipped:unchanged"
    _edit_market(paths, "UPDATE closes SET close = 1.5 WHERE market = 'tw' AND symbol = 'TAIEX' AND date = '2026-10-27'")
    assert _sync(paths, now=fx.NOW + 120)["status"] == "skipped:throttled"
    # 新的成員進了池子紀錄（代號集合變了）：25 分鐘內也要同步
    fx.write_states(paths.state_dir, us=fx.us_snapshot())
    ctx = _ctx(paths, fx.NOW + 180)
    pool.archive_stage(ctx)
    pool.pool_stage(ctx)
    ctx.close()
    out = _sync(paths, now=fx.NOW + 240)
    assert out["status"] == "ok" and out["codes_us"] == 3 + 50  # 三檔 ETF＋池子 50 檔（含沒有收盤的）
    assert _sync(paths, now=fx.NOW + 300)["status"] == "skipped:unchanged"


def test_future_sync_time_does_not_throttle_forever(paths):
    fx.make_market_db(paths.market_db, fx.standard_market_rows())
    _log_pools(paths)
    _sync(paths, now=fx.NOW + 86400)  # 時鐘曾經跳到明天
    _edit_market(paths, "UPDATE closes SET close = 2.5 WHERE market = 'tw' AND symbol = 'TAIEX' AND date = '2026-10-27'")
    assert _sync(paths, now=fx.NOW)["status"] == "ok"


@pytest.mark.parametrize("error", [stock_db_ro.DbBusy("SQLITE_BUSY"),
                                   stock_db_ro.DbUnavailable("sqlite_readonly_rollback")])
def test_busy_db_is_not_a_failure_and_records_no_key(paths, monkeypatch, error):
    fx.make_market_db(paths.market_db, fx.standard_market_rows())
    _log_pools(paths)

    def busy(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(stock_db_ro, "closes", busy)
    assert _sync(paths)["status"] == "busy"
    con = rdb.open_db(paths.db)
    assert rdb.get_state(con, rdb.SYNC_KEY) is None
    con.close()
    monkeypatch.undo()
    assert _sync(paths, now=fx.NOW + 60)["status"] == "ok"  # 沒記鍵：下一輪不會被「沒變」擋掉


def test_missing_market_db_is_a_failure(paths):
    _log_pools(paths)
    with pytest.raises(stock_db_ro.DbUnavailable):
        _sync(paths)


def test_existing_prices_reads_only_synced_codes_inside_the_window(paths):
    """審查 impl r2#5：比對不把整張 prices 載進記憶體，只讀要同步的代號、保留範圍內的列。"""
    con = rdb.open_db(paths.db)
    try:
        con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, 'tw', 0)", [
            ("tw", "2330", "2026-10-27", 1.0), ("tw", "2330", "2024-01-02", 2.0),  # 第二筆在保留範圍外
            ("tw", "9999", "2026-10-27", 3.0), ("us", "2330", "2026-10-27", 4.0), ("us", "SPY", "2026-10-27", 5.0)])
        codes = {"tw": ["2330"] + [f"{n:04d}" for n in range(1000, 1700)], "us": ["SPY"]}  # 超過一批 500 個
        assert rdb.existing_prices(con, codes, "2025-01-01") == {
            ("tw", "2330", "2026-10-27"): 1.0, ("us", "SPY", "2026-10-27"): 5.0}
        assert rdb.existing_prices(None, codes, "2025-01-01") == {}
    finally:
        con.close()


def test_plan_changes_keeps_codes_the_db_no_longer_has():
    existing = {("tw", "1101", "2026-10-01"): 1.0, ("tw", "1101", "2026-10-02"): 2.0}
    upserts, deletes = rdb.plan_changes(existing, {"tw": {"tw": {}, "tw_all": {}}, "us": {"us": {}}}, "2025-01-01")
    assert upserts == [] and deletes == []
