"""market_store（/盤勢 job 的 SQLite）測試：權限、交易、價格、隔離、事件、觀測、貼文、覆寫、帳本、排程、圖檔。

全部用暫存目錄與注入的時鐘；不連網（帳本的整合測試在冷卻中就先擋下，不會走到 safe_fetch）。
"""

from __future__ import annotations

import math
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import market_http
from market_store import Store, StoreError

# 台北 2026-10-16 18:00（UTC 10:00）
T0 = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400.0


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Bar:  # 和 market_sources_us.Bar 同樣有 date、close、volume 屬性
    date: date
    close: float
    volume: int
    open: float = 0.0


@dataclass
class Split:
    symbol: str
    execution_date: date
    new: float
    old: float


@dataclass
class ExRight:
    date: date
    code: str
    pre_close: float
    ref_price: float
    kind: str


@dataclass
class TrumpPost:
    original_id: str
    published_utc: datetime
    own_text: str
    removed: bool = False


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def store(state_dir: Path, clock: FakeClock):
    s = Store(state_dir / "market_history.sqlite", now_fn=clock)
    yield s
    s.close()


def _d(text: str) -> date:
    return date.fromisoformat(text)


# ── 檔案與 schema ───────────────────────────────────────────────────────────────


def test_db_file_is_private_and_state_dir_untouched(state_dir: Path, clock: FakeClock):
    old_umask = os.umask(0o022)
    try:
        with Store(state_dir / "market_history.sqlite", now_fn=clock) as s:
            s.upsert_closes("us", "SPY", [(_d("2026-10-15"), 700.0, 10)], "nasdaq")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(os.stat(state_dir / "market_history.sqlite").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(state_dir).st_mode) == 0o700
    for leftover in state_dir.iterdir():  # journal 檔（若還在）也只能是 0600
        assert stat.S_IMODE(leftover.stat().st_mode) & 0o077 == 0


def test_existing_db_with_loose_mode_is_tightened(state_dir: Path, clock: FakeClock):
    path = state_dir / "market_history.sqlite"
    path.write_bytes(b"")
    os.chmod(path, 0o644)
    Store(path, now_fn=clock).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_store_never_creates_the_state_dir(tmp_path: Path, clock: FakeClock):
    missing = tmp_path / "state"
    with pytest.raises(FileNotFoundError):
        Store(missing / "market_history.sqlite", now_fn=clock)
    assert not missing.exists()


def test_symlinked_db_path_is_refused(state_dir: Path, tmp_path: Path, clock: FakeClock):
    target = tmp_path / "elsewhere.sqlite"
    target.write_bytes(b"")
    (state_dir / "market_history.sqlite").symlink_to(target)
    with pytest.raises(OSError):
        Store(state_dir / "market_history.sqlite", now_fn=clock)


def test_reopen_keeps_data_and_newer_schema_is_refused(state_dir: Path, clock: FakeClock):
    path = state_dir / "market_history.sqlite"
    with Store(path, now_fn=clock) as s:
        s.set_task_state("us.hist", {"cursor": 3})
    with Store(path, now_fn=clock) as s:
        assert s.task_state("us.hist") == {"cursor": 3}
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version=99")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError):
        Store(path, now_fn=clock)


def test_closed_store_refuses_use(state_dir: Path, clock: FakeClock):
    s = Store(state_dir / "market_history.sqlite", now_fn=clock)
    s.close()
    s.close()  # 關兩次不會出錯
    with pytest.raises(StoreError):
        s.symbols("us")


# ── 交易 ────────────────────────────────────────────────────────────────────────


def test_nested_transaction_failure_only_rolls_back_inner(store: Store):
    with store.transaction():
        store.set_task_state("a", {"v": 1})
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.set_task_state("b", {"v": 2})
                raise RuntimeError("inner")
        store.set_task_state("c", {"v": 3})
    assert store.task_state("a") == {"v": 1}
    assert store.task_state("b") == {}
    assert store.task_state("c") == {"v": 3}


def test_outer_transaction_failure_rolls_back_everything(store: Store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.set_task_state("a", {"v": 1})
            store.upsert_closes("us", "SPY", [(_d("2026-10-15"), 700.0, 1)], "nasdaq")
            raise RuntimeError("outer")
    assert store.task_state("a") == {}
    assert store.closes("us", "SPY") == []
    assert not store.in_transaction


# ── 價格 ────────────────────────────────────────────────────────────────────────


def test_upsert_and_read_closes(store: Store):
    store.upsert_closes("us", "QQQ", [(_d("2026-10-15"), 709.8, 66_900_000), (_d("2026-10-14"), 700.0, None)], "nasdaq")
    store.upsert_closes("us", "BRK.B", [Bar(_d("2026-10-15"), 480.5, 3_000_000)], "nasdaq")
    rows = store.closes("us", "QQQ")
    assert rows == [(_d("2026-10-14"), 700.0, None, "nasdaq", False), (_d("2026-10-15"), 709.8, 66_900_000, "nasdaq", False)]
    assert rows[1].volume == 66_900_000 and rows[1].provisional is False
    assert store.closes("us", "QQQ", since=_d("2026-10-15")) == [rows[1]]
    assert store.last_date("us", "QQQ") == _d("2026-10-15")
    assert store.last_date("us", "NONE") is None
    assert store.symbols("us") == ["BRK.B", "QQQ"]
    assert store.symbols("tw") == []


def test_provisional_rows_never_overwrite_final_ones(store: Store):
    day = _d("2026-10-16")
    store.upsert_closes("us", "QQQ", [(day, 709.80, 66_900_000)], "nasdaq")
    store.upsert_closes("us", "QQQ", [(day, 717.23, 24_900_000)], "cboe", provisional=True)
    assert store.closes("us", "QQQ") == [(day, 709.80, 66_900_000, "nasdaq", False)]
    # 反過來：先有備援，正式來源一律蓋掉（PLAN §18.7）
    store.upsert_closes("us", "SPY", [(day, 650.0, 1)], "cboe", provisional=True)
    assert store.last_date("us", "SPY", include_provisional=False) is None
    store.upsert_closes("us", "SPY", [(day, 655.0, 2)], "cboe", provisional=True)
    assert store.closes("us", "SPY") == [(day, 655.0, 2, "cboe", True)]
    store.upsert_closes("us", "SPY", [(day, 660.0, 3)], "nasdaq")
    assert store.closes("us", "SPY") == [(day, 660.0, 3, "nasdaq", False)]


def test_replace_symbol_deletes_every_old_row(store: Store):
    old = [(_d("2026-10-0%d" % n), 4.0 + n, 100) for n in range(1, 6)]
    store.upsert_closes("us", "KNDI", old, "nasdaq")
    new = [(_d("2026-10-03"), 0.4, 1000), (_d("2026-10-06"), 0.41, 1100)]
    store.replace_symbol("us", "KNDI", new, "nasdaq", refetch_on=_d("2026-10-16"))
    assert [(r.date, r.close) for r in store.closes("us", "KNDI")] == [(_d("2026-10-03"), 0.4), (_d("2026-10-06"), 0.41)]
    assert store.last_full_refetch("us", "KNDI") == _d("2026-10-16")


def test_replace_symbol_is_one_transaction(store: Store):
    old = [(_d("2026-10-01"), 10.0, 1), (_d("2026-10-02"), 11.0, 1)]
    store.upsert_closes("us", "BTOC", old, "nasdaq")
    store._db().execute(
        "CREATE TEMP TRIGGER boom BEFORE INSERT ON closes WHEN NEW.date = '2026-10-16'"
        " BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(sqlite3.DatabaseError):
        store.replace_symbol("us", "BTOC", [(_d("2026-10-15"), 3.05, 5), (_d("2026-10-16"), 3.1, 5)], "nasdaq")
    assert [(r.date, r.close) for r in store.closes("us", "BTOC")] == [(_d("2026-10-01"), 10.0), (_d("2026-10-02"), 11.0)]
    with pytest.raises(ValueError):
        store.replace_symbol("us", "BTOC", [], "nasdaq")  # 空的重抓結果不能把歷史清光
    assert len(store.closes("us", "BTOC")) == 2


@pytest.mark.parametrize(
    "rows",
    [
        [(_d("2026-10-15"), float("nan"), 1)],
        [(_d("2026-10-15"), 0.0, 1)],
        [(_d("2026-10-15"), -1.0, 1)],
        [(_d("2026-10-15"), 1.0, -5)],
        [(_d("2026-10-15"), 1.0, True)],
        [(_d("2026-10-15"), 1.0, 1.5)],
        [(datetime(2026, 10, 15, tzinfo=timezone.utc), 1.0, 1)],
        [("2026-10-15", 1.0, 1)],
        [(_d("2026-10-15"), 1.0, 1), (_d("2026-10-15"), 2.0, 1)],  # 同一天兩個不同的值
    ],
)
def test_bad_close_rows_are_rejected_and_nothing_is_written(store: Store, rows):
    good = (_d("2026-10-14"), 5.0, 1)
    with pytest.raises(ValueError):
        store.upsert_closes("us", "SPY", [good, *rows], "nasdaq")
    assert store.closes("us", "SPY") == []


@pytest.mark.parametrize("market,symbol", [("US", "SPY"), ("us", ""), ("us", "S P Y"), ("us", "../x"), ("", "SPY")])
def test_bad_keys_are_rejected(store: Store, market, symbol):
    with pytest.raises(ValueError):
        store.upsert_closes(market, symbol, [(_d("2026-10-15"), 1.0, 1)], "nasdaq")


def test_identical_duplicate_rows_collapse(store: Store):
    row = (_d("2026-10-15"), 1.0, 1)
    store.upsert_closes("tw", "2330", [row, row], "twse")
    assert len(store.closes("tw", "2330")) == 1


def test_prune_keeps_latest_rows_per_symbol(store: Store):
    start = _d("2026-09-01")
    store.upsert_closes("us", "AAA", [(start + timedelta(days=i), 1.0 + i, 1) for i in range(10)], "nasdaq")
    store.upsert_closes("us", "BBB", [(start + timedelta(days=i), 1.0 + i, 1) for i in range(3)], "nasdaq")
    store.upsert_closes("tw", "2330", [(start + timedelta(days=i), 1.0 + i, 1) for i in range(10)], "twse")
    store.prune("us", 4)
    assert [r.date for r in store.closes("us", "AAA")] == [start + timedelta(days=i) for i in range(6, 10)]
    assert len(store.closes("us", "BBB")) == 3
    assert len(store.closes("tw", "2330")) == 10  # 別的市場不動
    with pytest.raises(ValueError):
        store.prune("us", 0)


# ── 隔離區間與待確認 ─────────────────────────────────────────────────────────────


def test_quarantine_intervals(store: Store):
    store.add_quarantine("tw", "1101", _d("2026-09-01"), _d("2026-09-03"), "jump")
    store.add_quarantine("tw", "1101", _d("2026-09-01"), _d("2026-09-03"), "jump")  # 重複不再加
    store.add_quarantine("tw", "1101", _d("2026-10-01"), _d("2026-10-01"), "conflict")
    store.add_quarantine("us", "AAPL", _d("2026-10-01"), _d("2026-10-02"), "conflict")
    assert store.quarantine("tw") == {
        "1101": [(_d("2026-09-01"), _d("2026-09-03"), "jump"), (_d("2026-10-01"), _d("2026-10-01"), "conflict")]
    }
    store.clear_quarantine("tw", "1101", reason="jump")
    assert store.quarantine("tw") == {"1101": [(_d("2026-10-01"), _d("2026-10-01"), "conflict")]}
    store.clear_quarantine("tw", "1101")
    assert store.quarantine("tw") == {}
    assert list(store.quarantine("us")) == ["AAPL"]
    with pytest.raises(ValueError):
        store.add_quarantine("us", "AAPL", _d("2026-10-03"), _d("2026-10-01"), "conflict")
    with pytest.raises(ValueError):
        store.add_quarantine("us", "AAPL", _d("2026-10-01"), _d("2026-10-01"), "跳空")  # 原因只收種類代碼


def test_pending_jump_roundtrip(store: Store):
    assert store.pending_jump("us", "NVDA") is None
    store.set_pending_jump("us", "NVDA", {"date": "2026-10-15", "ratio": 1.23, "seen": 1})
    assert store.pending_jump("us", "NVDA") == {"date": "2026-10-15", "ratio": 1.23, "seen": 1}
    store.set_pending_jump("us", "NVDA", None)
    assert store.pending_jump("us", "NVDA") is None
    with pytest.raises(ValueError):
        store.set_pending_jump("us", "NVDA", ["not", "a", "dict"])


# ── 事件 ────────────────────────────────────────────────────────────────────────


def test_splits_upsert_and_window(store: Store):
    store.upsert_splits([("KNDI", _d("2026-10-16"), 1, 10), Split("BTOC", _d("2026-10-15"), 1, 16)], "nasdaq")
    store.upsert_splits([("KNDI", _d("2026-10-16"), 1, 12.62)], "nasdaq")  # 同一天再看到就更新比例
    assert store.splits_since("KNDI", _d("2026-10-15")) == [("KNDI", _d("2026-10-16"), 1.0, 12.62, "nasdaq")]
    assert store.splits_since("KNDI", _d("2026-10-16")) == []  # 下界不含
    assert store.splits_since("BTOC", _d("2026-10-01"), until=_d("2026-10-15"))[0].execution_date == _d("2026-10-15")
    assert store.splits_since("BTOC", _d("2026-10-01"), until=_d("2026-10-14")) == []
    with pytest.raises(ValueError):
        store.upsert_splits([("KNDI", _d("2026-10-16"), 0, 10)], "nasdaq")


def test_full_refetch_marker(store: Store):
    assert store.last_full_refetch("us", "MTUM") is None
    store.mark_full_refetch("us", "MTUM", _d("2026-11-05"))
    store.mark_full_refetch("us", "MTUM", _d("2026-11-06"))
    assert store.last_full_refetch("us", "MTUM") == _d("2026-11-06")


def test_exrights_accept_tuples_and_objects(store: Store):
    store.upsert_exrights([(_d("2026-07-16"), "2330", 1100.0, 1095.0, "息"), ExRight(_d("2026-08-01"), "0050", 200.0, 195.5, "息")])
    rows = store.exrights("2330")
    assert rows == [(_d("2026-07-16"), "2330", 1100.0, 1095.0, "息")]
    assert rows[0].ref_price / rows[0].pre_close == pytest.approx(1095.0 / 1100.0)
    assert store.exrights("0050")[0].kind == "息"
    with pytest.raises(ValueError):
        store.upsert_exrights([(_d("2026-07-16"), "23", 1.0, 1.0, "息")])


def test_coverage_merges_overlapping_and_adjacent_spans(store: Store):
    store.add_coverage("nasdaq.splits", _d("2026-06-01"), _d("2026-06-30"))
    store.add_coverage("nasdaq.splits", _d("2026-07-01"), _d("2026-07-10"))  # 相鄰
    store.add_coverage("nasdaq.splits", _d("2026-08-01"), _d("2026-08-05"))
    store.add_coverage("nasdaq.splits", _d("2026-07-05"), _d("2026-08-02"))  # 跨過兩段
    assert store.coverage("nasdaq.splits") == [(_d("2026-06-01"), _d("2026-08-05"))]
    store.add_coverage("twse.twt49u", _d("2026-06-01"), _d("2026-06-02"))
    store.add_coverage("twse.twt49u", _d("2026-06-04"), _d("2026-06-05"))
    assert store.coverage("twse.twt49u") == [(_d("2026-06-01"), _d("2026-06-02")), (_d("2026-06-04"), _d("2026-06-05"))]
    with pytest.raises(ValueError):
        store.add_coverage("x", _d("2026-06-05"), _d("2026-06-04"))


# ── 觀測值 ──────────────────────────────────────────────────────────────────────


def test_obs_success_failure_and_not_yet(store: Store, clock: FakeClock):
    assert store.get_obs("tw.inst3") is None
    store.put_obs("tw.inst3", {"foreign": -1.5e9}, data_date="2026-10-16")
    first = store.get_obs("tw.inst3")
    assert first == {
        "payload": {"foreign": -1.5e9}, "data_date": "2026-10-16", "period": None,
        "last_success_at": T0, "checked_at": T0, "fail_streak": 0, "error": None,
    }
    clock.now += 900
    store.obs_checked("tw.inst3", error_kind="rate_limited")
    clock.now += 900
    store.obs_checked("tw.inst3", error_kind="unexpected:TypeError")
    failed = store.get_obs("tw.inst3")
    assert failed["payload"] == {"foreign": -1.5e9} and failed["last_success_at"] == T0  # 舊值留著
    assert failed["fail_streak"] == 2 and failed["error"] == "unexpected:TypeError"
    assert failed["checked_at"] == T0 + 1800
    clock.now += 900
    store.obs_not_yet("tw.inst3")  # 還沒公布：不算失敗、也不算成功
    waiting = store.get_obs("tw.inst3")
    assert waiting["checked_at"] == T0 + 2700 and waiting["fail_streak"] == 2 and waiting["last_success_at"] == T0
    clock.now += 900
    store.put_obs("tw.inst3", {"foreign": 2.0e9}, data_date=_d("2026-10-17"), period="2026-10")
    again = store.get_obs("tw.inst3")
    assert again["fail_streak"] == 0 and again["error"] is None and again["data_date"] == "2026-10-17"
    assert again["period"] == "2026-10" and again["last_success_at"] == T0 + 3600


def test_obs_failure_on_new_id_has_no_payload(store: Store):
    store.obs_checked("us.cftc", error_kind="format")
    assert store.get_obs("us.cftc") == {
        "payload": None, "data_date": None, "period": None, "last_success_at": None,
        "checked_at": T0, "fail_streak": 1, "error": "format",
    }
    store.obs_not_yet("us.finra")
    assert store.get_obs("us.finra")["fail_streak"] == 0


def test_obs_rejects_nan_and_bad_ids(store: Store):
    with pytest.raises(ValueError):
        store.put_obs("macro.VIX", {"value": math.nan}, data_date="2026-10-16")
    with pytest.raises(ValueError):
        store.put_obs("macro VIX", {"value": 1}, data_date="2026-10-16")
    with pytest.raises(ValueError):
        store.obs_checked("macro.VIX", error_kind="bad kind with spaces")
    assert store.get_obs("macro.VIX") is None


def test_obs_ids_prefix_is_literal(store: Store):
    for obs_id in ("tw.holidays.2026", "tw.holidays.2025", "tw.holidaysX", "us.13f.QQQ"):
        store.put_obs(obs_id, {}, data_date=None)
    assert store.obs_ids("tw.holidays.") == ["tw.holidays.2025", "tw.holidays.2026"]
    assert store.obs_ids("tw_") == []  # 「_」不是萬用字元
    assert len(store.obs_ids("")) == 4


# ── 貼文與新聞 ──────────────────────────────────────────────────────────────────


def test_posts_accumulate_by_id(store: Store, clock: FakeClock):
    t1 = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)
    store.upsert_posts("trump", [TrumpPost("115000000000000001", t1, "Tariffs on China")])
    store.upsert_posts("trump", [{"original_id": "115000000000000002", "published_utc": t2, "own_text": "Iran"}])
    clock.now += 600
    store.upsert_posts("trump", [TrumpPost("115000000000000001", t1, "Tariffs on China", removed=True)])
    items = store.posts("trump", since_utc=t1 - timedelta(hours=1))
    assert [item["id"] for item in items] == ["115000000000000002", "115000000000000001"]  # 新的在前
    older = items[1]
    assert older["removed"] is True and older["published_utc"] == t1
    assert older["first_seen_at"] == T0 and older["last_seen_at"] == T0 + 600
    assert [i["id"] for i in store.posts("trump", since_utc=t2)] == ["115000000000000002"]
    assert store.posts("news.us", since_utc=t1) == []
    store.upsert_posts("news.us", [{"guid": "g-1", "published_utc": t2.timestamp(), "title": "美股收高"}])
    assert store.posts("news.us", since_utc=t1)[0]["title"] == "美股收高"


def test_posts_older_than_a_month_are_pruned(store: Store, clock: FakeClock):
    old = datetime.fromtimestamp(T0 - 40 * DAY, timezone.utc)
    store.upsert_posts("trump", [{"id": "1", "published_utc": old}])
    store.upsert_posts("trump", [{"id": "2", "published_utc": datetime.fromtimestamp(T0, timezone.utc)}])
    assert [i["id"] for i in store.posts("trump", since_utc=0.0)] == ["2"]


@pytest.mark.parametrize(
    "item",
    [
        {"published_utc": datetime(2026, 10, 16, tzinfo=timezone.utc)},  # 沒有 id
        {"id": "1"},  # 沒有時間
        {"id": "1", "published_utc": datetime(2026, 10, 16)},  # 沒有時區
        {"id": "1\x00", "published_utc": datetime(2026, 10, 16, tzinfo=timezone.utc)},
    ],
)
def test_bad_posts_are_rejected(store: Store, item):
    with pytest.raises(ValueError):
        store.upsert_posts("trump", [item])


# ── 日曆覆寫 ────────────────────────────────────────────────────────────────────


def test_calendar_overrides(store: Store):
    store.set_calendar_override("tw", _d("2026-07-29"), "closed", "allbut0999_empty+finmind_empty")
    store.set_calendar_override("us", _d("2026-10-14"), "closed", "market_info", confirmed=True)
    assert store.calendar_overrides("tw") == {
        _d("2026-07-29"): {"kind": "closed", "evidence": "allbut0999_empty+finmind_empty", "confirmed": False, "updated_at": T0}
    }
    store.set_calendar_override("tw", _d("2026-07-29"), "revoked", "data_seen")
    assert store.calendar_overrides("tw")[_d("2026-07-29")]["kind"] == "revoked"
    store.clear_calendar_override("tw", _d("2026-07-29"))
    assert store.calendar_overrides("tw") == {}
    assert store.calendar_overrides("us")[_d("2026-10-14")]["confirmed"] is True
    with pytest.raises(ValueError):
        store.set_calendar_override("us", _d("2026-10-14"), "typhoon", "x")


# ── 排程狀態與圖檔 ──────────────────────────────────────────────────────────────


def test_task_state_roundtrip(store: Store):
    assert store.task_state("us.hist") == {}
    store.set_task_state("us.hist", {"cursor": ["AAPL", 3], "day": "2026-10-16"})
    store.set_task_state("_collect", {"run_id": "ab"})
    assert store.task_state("us.hist") == {"cursor": ["AAPL", 3], "day": "2026-10-16"}
    assert store.task_names() == ["_collect", "us.hist"]
    with pytest.raises(ValueError):
        store.set_task_state("us.hist", {"bad": float("inf")})
    with pytest.raises(ValueError):
        store.set_task_state("bad name", {})


def test_chart_registry(store: Store):
    a, b, c = "a" * 32 + ".png", "b" * 32 + ".png", "c" * 32 + ".png"
    store.register_chart(a, "hash1", T0)
    store.register_chart(b, "hash2", T0)
    store.retire_charts({b}, T0 + 10)  # a 不再被引用
    assert store.chart_files()[a]["retired_at"] == T0 + 10 and store.chart_files()[b]["retired_at"] is None
    assert store.charts_to_delete(T0 + 10 + 72 * 3600 - 1, 72 * 3600) == []
    assert store.charts_to_delete(T0 + 10 + 72 * 3600, 72 * 3600) == [a]
    store.retire_charts({a}, T0 + 20)  # a 又被引用：取消退役；b 退役
    assert store.chart_files()[a]["retired_at"] is None and store.chart_files()[b]["retired_at"] == T0 + 20
    store.forget_chart(b)
    assert set(store.chart_files()) == {a}
    store.register_chart(c, "hash3", T0)
    assert store.chart_files()[c]["inputs_hash"] == "hash3"
    for bad in ("../x.png", "A" * 32 + ".png", "a" * 31 + ".png", "x.txt"):
        with pytest.raises(ValueError):
            store.register_chart(bad, "h", T0)


# ── 帳本 ────────────────────────────────────────────────────────────────────────


def test_ledger_spacing_waits_then_reserves(store: Store, clock: FakeClock):
    ledger = store.ledger(sleep_fn=clock.sleep)
    ledger.reserve("api.nasdaq.com")
    ledger.reserve("api.nasdaq.com")
    assert sum(clock.sleeps) == pytest.approx(1.0)
    clock.sleeps.clear()
    ledger.reserve("www.twse.com.tw")
    ledger.reserve("www.twse.com.tw")
    assert sum(clock.sleeps) == pytest.approx(10.0)
    counts = store.fetch_counts()
    assert counts["api.nasdaq.com"]["count"] == 2 and counts["www.twse.com.tw"] == {"count": 2, "cap": 12, "cooldown_until": None}


def test_ledger_daily_cap_resets_on_the_next_taipei_day(store: Store, clock: FakeClock):
    ledger = store.ledger(sleep_fn=clock.sleep)
    for _ in range(12):
        ledger.reserve("www.twse.com.tw")
    with pytest.raises(market_http.FetchError) as exc:
        ledger.reserve("www.twse.com.tw")
    assert exc.value.kind == "budget"
    clock.now = datetime(2026, 10, 17, 0, 0, 1, tzinfo=timezone(timedelta(hours=8))).timestamp()  # 台北隔天
    ledger.reserve("www.twse.com.tw")
    assert store.fetch_counts()["www.twse.com.tw"]["count"] == 1


def test_ledger_cooldown_blocks_until_it_expires(store: Store, clock: FakeClock):
    ledger = store.ledger(sleep_fn=clock.sleep)
    ledger.cooldown("news.google.com", 7200)
    with pytest.raises(market_http.FetchError) as exc:
        ledger.reserve("news.google.com")
    assert exc.value.kind == "cooldown"
    assert store.fetch_counts()["news.google.com"]["cooldown_until"] == T0 + 7200
    clock.now = T0 + 7199
    with pytest.raises(market_http.FetchError):
        ledger.reserve("news.google.com")
    clock.now = T0 + 7200
    ledger.reserve("news.google.com")
    assert store.fetch_counts()["news.google.com"]["count"] == 1


def test_cooldown_survives_the_taipei_midnight(store: Store, clock: FakeClock):
    clock.now = datetime(2026, 10, 16, 23, 30, tzinfo=timezone(timedelta(hours=8))).timestamp()
    store.ledger().cooldown("api.nasdaq.com", 7200)
    clock.now += 3600  # 台北隔天 00:30，冷卻還有一小時
    with pytest.raises(market_http.FetchError) as exc:
        store.ledger().reserve("api.nasdaq.com")
    assert exc.value.kind == "cooldown"


def test_yahoo_hosts_share_one_budget(store: Store, clock: FakeClock):
    ledger = store.ledger(sleep_fn=clock.sleep)
    ledger.reserve("query1.finance.yahoo.com")
    ledger.reserve("query2.finance.yahoo.com")
    assert sum(clock.sleeps) == pytest.approx(5.0)
    with pytest.raises(market_http.FetchError) as exc:
        ledger.reserve("query1.finance.yahoo.com")
    assert exc.value.kind == "budget"
    assert store.fetch_counts()["finance.yahoo.com"] == {"count": 2, "cap": 2, "cooldown_until": None}


def test_unknown_host_uses_default_rule(store: Store, clock: FakeClock):
    ledger = store.ledger(sleep_fn=clock.sleep)
    ledger.reserve("cdn-api.cboe.com")
    ledger.reserve("cdn-api.cboe.com")
    assert sum(clock.sleeps) == pytest.approx(2.0)
    assert store.fetch_counts()["cdn-api.cboe.com"]["cap"] == 200


def test_reservation_is_recorded_before_the_request(state_dir: Path, clock: FakeClock):
    path = state_dir / "market_history.sqlite"
    with Store(path, now_fn=clock) as s:
        s.ledger().reserve("api.finmindtrade.com")  # 預扣後程序被 kill：帳還在
    with Store(path, now_fn=clock) as s:
        assert s.fetch_counts()["api.finmindtrade.com"]["count"] == 1


def test_ledger_wait_aborts_when_asked_to_stop(store: Store, clock: FakeClock):
    store.ledger(sleep_fn=clock.sleep).reserve("www.twse.com.tw")
    stopping = store.ledger(sleep_fn=clock.sleep, stop_fn=lambda: True)
    with pytest.raises(market_http.FetchError) as exc:
        stopping.reserve("www.twse.com.tw")
    assert exc.value.kind == "budget"
    assert store.fetch_counts()["www.twse.com.tw"]["count"] == 1
    capped = store.ledger(sleep_fn=clock.sleep, max_wait_s=1.0)  # 不願意等 10 秒
    with pytest.raises(market_http.FetchError):
        capped.reserve("www.twse.com.tw")


def test_ledger_refuses_to_run_inside_a_transaction(store: Store):
    with store.transaction():
        with pytest.raises(StoreError):
            store.ledger().reserve("api.nasdaq.com")
        with pytest.raises(StoreError):
            store.ledger().cooldown("api.nasdaq.com", 60)


def test_ledger_cooldown_rejects_bad_durations(store: Store):
    with pytest.raises(ValueError):
        store.ledger().cooldown("api.nasdaq.com", -1)
    with pytest.raises(ValueError):
        store.ledger().cooldown("api.nasdaq.com", float("inf"))


def test_market_http_fetch_stops_at_the_ledger_before_any_network(store: Store):
    ledger = store.ledger()
    ledger.cooldown("api.nasdaq.com", 7200)
    with pytest.raises(market_http.FetchError) as exc:
        market_http.fetch("https://api.nasdaq.com/api/market-info", max_bytes=10_000, ledger=ledger)
    assert exc.value.kind == "cooldown"
    assert store.fetch_counts()["api.nasdaq.com"]["count"] == 0
