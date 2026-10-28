"""stock_db_ro（盤勢 DB 唯讀存取，2026-10-28）的測試：tmp sqlite，不碰正式 state。"""

from __future__ import annotations

import sqlite3
import time

import pytest

import stock_db_ro as db


def _make(path, journal="delete"):
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA journal_mode={journal}")
    con.execute(
        "CREATE TABLE closes (market TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,"
        " volume INTEGER, source TEXT NOT NULL, provisional INTEGER NOT NULL DEFAULT 0, fetched_at REAL NOT NULL,"
        " PRIMARY KEY (market, symbol, date)) WITHOUT ROWID"
    )
    con.execute("CREATE TABLE obs (obs_id TEXT PRIMARY KEY, json TEXT, data_date TEXT)")
    con.execute("INSERT INTO closes VALUES ('us','SPY','2026-10-27',600.5,1,'nasdaq',0,1.0)")
    con.execute("INSERT INTO obs VALUES ('ok','{\"a\": 1}','2026-10-27'), ('bad','{not json','2026-10-27')")
    con.commit()
    con.close()


def test_rows_reads_and_closes(tmp_path):
    path = tmp_path / "m.sqlite"
    _make(path)
    assert db.rows("SELECT symbol, close FROM closes WHERE market = ?", ("us",), path=path) == [("SPY", 600.5)]
    # 讀完就關：寫者馬上拿得到 EXCLUSIVE 鎖、commit 得了
    writer = sqlite3.connect(path, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO closes VALUES ('us','SPY','2026-10-28',601.0,1,'nasdaq',0,2.0)")
    writer.commit()
    writer.close()


def test_rows_is_read_only(tmp_path):
    path = tmp_path / "m.sqlite"
    _make(path)
    with pytest.raises(ValueError):
        db.rows("DELETE FROM closes", path=path)
    with pytest.raises(ValueError):
        db.rows("  insert into closes values (1)", path=path)
    # 就算繞過前綴檢查，連線本身也是唯讀
    with pytest.raises(db.DbUnavailable):
        db.rows("WITH x AS (SELECT 1) DELETE FROM closes", path=path)
    assert len(db.rows("SELECT * FROM closes", path=path)) == 1


def test_busy_writer_is_db_busy_not_unavailable(tmp_path):
    path = tmp_path / "m.sqlite"
    _make(path)
    writer = sqlite3.connect(path, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        with pytest.raises(db.DbBusy):
            db.rows("SELECT * FROM closes", path=path, timeout=0.2)
        assert time.monotonic() - start < 1.5
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize(
    "sql, kind",
    [
        ("SELECT nope FROM closes", "sqlite_error"),
        ("SELECT * FROM no_such_table", "sqlite_error"),
    ],
)
def test_schema_changes_are_unavailable(tmp_path, sql, kind):
    path = tmp_path / "m.sqlite"
    _make(path)
    with pytest.raises(db.DbUnavailable) as info:
        db.rows(sql, path=path)
    assert info.value.kind == kind


def test_missing_db_is_unavailable(tmp_path):
    with pytest.raises(db.DbUnavailable) as info:
        db.rows("SELECT 1", path=tmp_path / "nope.sqlite")
    assert info.value.kind == "missing"


def test_path_with_spaces_and_question_marks(tmp_path):
    folder = tmp_path / "a b?c#d"
    folder.mkdir()
    path = folder / "m.sqlite"
    _make(path)
    assert db.rows("SELECT count(*) FROM closes", path=path) == [(1,)]


def test_obs_json(tmp_path):
    path = tmp_path / "m.sqlite"
    _make(path)
    assert db.obs_json("ok", path=path) == {"a": 1}
    assert db.obs_json("missing", path=path) is None
    with pytest.raises(db.DbUnavailable) as info:
        db.obs_json("bad", path=path)
    assert info.value.kind == "bad_json"


def test_db_path_env_override_and_default(monkeypatch, tmp_path):
    import stock_picks

    monkeypatch.delenv(db.DB_PATH_ENV, raising=False)
    assert db.db_path() == stock_picks.STATE_DIR / db.DB_NAME
    monkeypatch.setenv(db.DB_PATH_ENV, str(tmp_path / "x.sqlite"))
    assert db.db_path() == tmp_path / "x.sqlite"


# ── 共用查詢：用盤勢 job 自己的 market_store 建同 schema 的 DB ─────────────────────────────
from datetime import date as _date  # noqa: E402

import market_store  # noqa: E402


@pytest.fixture()
def store_path(tmp_path):
    path = tmp_path / "market_history.sqlite"
    store = market_store.Store(path)
    d = _date.fromisoformat
    store.upsert_closes("us", "SPY", [(d("2026-10-26"), 600.0, 1), (d("2026-10-27"), 601.0, 1)], "nasdaq")
    store.upsert_closes("us", "QQQ", [(d("2026-10-23"), 500.0, 1)], "nasdaq")
    store.upsert_closes("us", "NVDA", [(d("2026-10-26"), 200.0, 1), (d("2026-10-27"), 210.0, 1)], "nasdaq")
    store.upsert_closes("us", "AAPL", [(d("2026-10-26"), 250.0, 1)], "nasdaq")
    store.upsert_closes("us", "AAPL", [(d("2026-10-27"), 251.0, 1)], "cboe", provisional=True)
    store.upsert_closes("tw", "TAIEX", [(d("2026-10-22"), 46041.43, None), (d("2026-10-27"), 46096.23, None)], "twse")
    store.upsert_closes("tw", "2330", [(d("2026-10-22"), 2385.0, 1)], "twse")
    store.upsert_closes("tw_all", "2330", [(d("2026-10-27"), 2400.0, 1)], "twse")
    store.add_quarantine("us", "AAPL", d("2026-10-01"), d("2026-10-02"), "jump")
    store.upsert_splits([("NVDA", d("2026-11-05"), 2.0, 1.0)], "nasdaq")
    store.upsert_exrights([(d("2026-10-23"), "2330", 2400.0, 2395.0, "息")])
    store.add_coverage("twt49u", d("2025-08-15"), d("2026-10-27"))
    store.put_obs("tw.holidays.2026", {"year": 2026, "rows": [
        {"date": "2026-10-26", "name": "x", "note": "", "kind": "closed"},
        {"date": "2026-01-02", "name": "x", "note": "", "kind": "trading"},
    ], "unknown": []}, data_date=None, period="2026")
    store.set_calendar_override("tw", d("2026-10-23"), "closed", "allbut0999_empty")
    store.close()
    return path


def test_us_calendar_is_the_union_of_three_etfs(store_path):
    assert db.us_calendar("2026-10-01", path=store_path) == ["2026-10-23", "2026-10-26", "2026-10-27"]


def test_closes_skip_provisional_rows(store_path):
    got = db.closes("us", ["NVDA", "AAPL", "ZZZ"], "2026-10-01", path=store_path)
    assert got == {"NVDA": {"2026-10-26": 200.0, "2026-10-27": 210.0}, "AAPL": {"2026-10-26": 250.0}}


def test_coverage_and_latest_closes(store_path):
    assert db.coverage_by_date("us", ["NVDA", "AAPL"], "2026-10-01", path=store_path) == {"2026-10-26": 2, "2026-10-27": 1}
    latest = db.latest_closes("us", path=store_path)
    assert latest["NVDA"] == ("2026-10-27", 210.0) and latest["AAPL"] == ("2026-10-26", 250.0)


def test_quarantine_splits_exrights_coverage(store_path):
    assert db.active_quarantine("us", ["AAPL", "NVDA"], path=store_path) == {"AAPL": [("2026-10-01", "2026-10-02")]}
    assert db.splits_after(["NVDA"], "2026-10-27", path=store_path) == {"NVDA": ["2026-11-05"]}
    assert db.splits_after(["NVDA"], "2026-11-05", path=store_path) == {}
    assert db.exrights(["2330"], "2026-10-22", path=store_path) == {"2330": [("2026-10-23", 2400.0, 2395.0)]}
    assert db.coverage_to("twt49u", path=store_path) == "2026-10-27"
    assert db.coverage_to("nope", path=store_path) is None


def test_tw_calendar_and_closed_days(store_path):
    assert db.tw_calendar("2026-10-01", path=store_path) == ["2026-10-22", "2026-10-27"]
    # 颱風暫定休市：10 天內不算確定（盤勢 job 可能撤銷）；過了 10 天才算
    assert db.tw_closed_days([2026], today="2026-10-28", path=store_path) == {"2026-10-26"}
    assert db.tw_closed_days([2026], today="2026-11-03", path=store_path) == {"2026-10-23", "2026-10-26"}
    assert db.tw_closed_days([2025, 2026], today="2026-10-28", path=store_path) is None  # 2025 的表讀不到


def test_confirmed_or_revoked_overrides(store_path):
    store = market_store.Store(store_path)
    store.set_calendar_override("tw", _date(2026, 10, 23), "closed", "manual", confirmed=True)
    store.close()
    assert "2026-10-23" in db.tw_closed_days([2026], today="2026-10-24", path=store_path)
    store = market_store.Store(store_path)
    store.set_calendar_override("tw", _date(2026, 10, 23), "revoked", "seen_data")
    store.close()
    assert "2026-10-23" not in db.tw_closed_days([2026], today="2026-12-01", path=store_path)


def test_holiday_table_with_unknown_rows_is_not_trusted(store_path):
    store = market_store.Store(store_path)
    store.put_obs("tw.holidays.2026", {"year": 2026, "rows": [
        {"date": "2026-10-26", "name": "x", "note": "", "kind": "closed"},
    ], "unknown": [{"date": "2026-11-02", "name": "?"}]}, data_date=None, period="2026")
    store.close()
    assert db.tw_closed_days([2026], today="2026-10-28", path=store_path) is None
