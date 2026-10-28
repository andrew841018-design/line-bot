"""AI 相關股推薦的背景計算（計畫 C）：_maybe_fill_ai 的懶抓與寫入規則、名單（00952＋核心）的抓取帳、
_run 裡唯一的 hook，以及從合成市場一路到 /股票 卡片的端到端。合成行情與假時鐘，不連網、不真的等。
"""

from __future__ import annotations

import ast
import copy
import json
import sys
import threading
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import stock_picks as sp
import stock_picks_ai_source as source
from test_stock_picks_ai import T, TODAY, _facts, _snapshot, _state
from test_stock_picks_data import FakeNet, _chart, _day_rows


class Clock:
    """stock_picks 裡的 time.time／monotonic／sleep 都走這個，測試不用真的等。"""

    def __init__(self):
        self.wall = 1_792_800_000.0 + 6 * 3600  # 2026-10-24 14:00 台北
        self.mono = 5000.0

    def time(self):
        return self.wall

    def time_ns(self):
        return int(self.wall * 1e9)

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


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


def _next_round(clock, seconds=sp.AI_RETRY_S + 1):
    clock.advance(seconds)


def _at(facts, day):
    return replace(facts, data_date=day)


def _series(snapshot, kind="flat"):
    """合成 Yahoo 日線（T 前面的 239 根；T 那根由官方收盤接）。"""
    days = list(snapshot.calendar[:-1])
    n = len(days)
    if kind == "flat":  # 年線之上、季線 0%：合格
        closes = [100.0] * n
    elif kind == "hot":  # 比季線高 13.7%：走勢不合格（但算得出窗口）
        closes = [75.0] * (n - 58) + [88.0] * 58
    elif kind == "below":  # 年線之下：走勢不合格
        closes = [110.0] * n
    elif kind == "short":  # 只有 100 根：這個 T 確定算不出來
        days, closes = days[-100:], [100.0] * 100
    elif kind == "jump":  # 窗內相鄰兩根差超過 25%
        closes = [300.0] * 120 + [100.0] * (n - 120)
    elif kind == "hole":  # 前一個交易日那根不見（可能只是 Yahoo 還沒補）
        del days[-1]
        closes = [100.0] * len(days)
    elif kind == "tbar":  # Yahoo 也有 T，而且和官方收盤差超過 1%
        days, closes = days + [snapshot.t], [100.0] * n + [110.0]
    else:
        raise AssertionError(kind)
    return _chart(days, closes)


class Yahoo:
    """取代 _fetch_chart：記下抓了哪些代號；series 可以指定每個代號的結果（日線種類、日線、None 或要丟的例外）。"""

    def __init__(self, snapshot, clock):
        self.snapshot, self.clock = snapshot, clock
        self.calls: list[str] = []
        self.series: dict = {}
        self.per_call_s = 0.0
        self.timeouts: list = []

    def kind(self, code, kind):
        self.series[f"{code}.TW"] = kind

    def __call__(self, symbol, **kwargs):
        self.calls.append(symbol)
        self.timeouts.append(kwargs.get("timeout"))
        self.clock.advance(self.per_call_s)
        value = self.series.get(symbol, "flat")
        if isinstance(value, str):
            return _series(self.snapshot, value)
        if isinstance(value, Exception):
            raise value
        return value


class Rig:
    """count 檔都是 AI 相關、成長、便宜的候選：代號 1101 起，累計營收年增 100、99、… 所以排名就是代號順序。"""

    def __init__(self, monkeypatch, clock, count, *, pool=False, cum=None):
        self.facts = [_facts(str(1101 + i), revenue=sp.Revenue(2026, 8, 10.0, cum(i) if cum else 100.0 - i))
                      for i in range(count)]
        self.snapshot = _snapshot(self.facts, pool=pool)
        self.clock, self.version, self.universe_calls = clock, "test-v1", 0
        universe = _state(self.facts)["universe"]

        def universe_stub(snapshot, give_up):
            self.universe_calls += 1
            return copy.deepcopy(universe), {f.code: "00952" for f in self.facts}, T.isoformat(), self.version

        monkeypatch.setattr(sp, "_snapshot", self.snapshot)
        monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
        monkeypatch.setattr(sp, "_ai_universe", universe_stub)
        monkeypatch.setattr(sp, "_ai_core", lambda: (None, self.version))
        self.yahoo = Yahoo(self.snapshot, clock)
        monkeypatch.setattr(sp, "_fetch_chart", self.yahoo)

    def run(self, stop=None):
        sp._maybe_fill_ai(stop or threading.Event())
        return sp._ai_state

    @property
    def picks(self):
        return [p["c"] for p in (sp._ai_state or {}).get("picks", [])]

    @property
    def has_result(self):
        return bool((sp._ai_state or {}).get("t"))


def _disk():
    return sp.load_ai_file()


def _history_rows():
    path = sp.STATE_DIR / "stock_picks_tw_ai_history.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _history_days():
    return [row["t"] for row in _history_rows()]


# ── 懶抓：照最後排名一檔一檔查，滿 3 檔就停 ─────────────────────────────────
def test_lazy_fetch_stops_at_three_and_same_t_skips_recomputation(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 10)
    state = rig.run()
    assert rig.yahoo.calls == [f"{f.code}.TW" for f in rig.facts[:3]]
    assert rig.picks == [f.code for f in rig.facts[:3]] and state["t"] == T.isoformat()
    assert state["funnel"] == {"universe": 10, "growth": 10, "cheap": 10, "trend": 3}
    assert state["fail_count"] == 0 and sp._ai_next_try == 0.0
    assert _disk()["picks"] == state["picks"] and _history_days() == [T.isoformat()]
    assert all(p["ai_source"] == "00952" for p in state["picks"])
    _next_round(clock)
    rig.run()
    assert len(rig.yahoo.calls) == 3 and rig.universe_calls == 1  # T 沒變、規則與名單沒變：不重算


def test_every_request_is_clipped_to_the_remaining_budget(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 3)
    rig.yahoo.per_call_s = 25.0
    for code in ("1101", "1102", "1103"):
        rig.yahoo.kind(code, "below")
    rig.run()
    # 預算 60 秒：第 3 次抓之前只剩 10 秒，單次逾時是剩下的四分之一
    assert rig.yahoo.timeouts == [3.0, 3.0, pytest.approx(2.5)]


def test_pool_trends_need_no_yahoo(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4, pool=True)
    rig.run()
    assert rig.yahoo.calls == [] and rig.picks == [f.code for f in rig.facts[:3]]


def test_pool_member_without_a_trend_is_fetched_like_any_other(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 2, pool=True)
    rig.snapshot.pool["1101"] = _facts("1101", trend=None)
    rig.run()
    assert rig.yahoo.calls == ["1101.TW"] and rig.picks == ["1101", "1102"]


def test_trend_failures_are_skipped_in_rank_order(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 6)
    rig.yahoo.kind("1101", "hot")
    rig.yahoo.kind("1102", "below")
    rig.run()
    assert rig.picks == ["1103", "1104", "1105"]
    assert rig.yahoo.calls == [f"{1100 + n}.TW" for n in range(1, 6)]
    assert sp._ai_state["funnel"]["trend"] == 3


def test_fewer_than_three_with_everything_examined_is_written(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 5)
    for code in ("1101", "1102", "1104"):
        rig.yahoo.kind(code, "below")
    state = rig.run()
    assert rig.picks == ["1103", "1105"] and state["fail_count"] == 0 and state["t"] == T.isoformat()
    assert len(rig.yahoo.calls) == 5
    assert state["funnel"] == {"universe": 5, "growth": 5, "cheap": 5, "trend": 2}


def test_no_candidate_at_all_is_a_valid_zero_pick_result(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 3, cum=lambda i: 5.0)  # 累計營收年增都不到 15%
    state = rig.run()
    assert state["t"] == T.isoformat() and state["picks"] == [] and rig.yahoo.calls == []
    assert state["funnel"] == {"universe": 3, "growth": 0, "cheap": 0, "trend": 0}
    assert _history_days() == [T.isoformat()]


def test_twenty_request_cap_is_progress_not_failure(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 25)
    for i in range(20):
        rig.yahoo.kind(f"{1101 + i}", "below")
    rig.run()
    assert len(rig.yahoo.calls) == sp.AI_FETCH_CAP == 20
    assert not rig.has_result and sp._ai_state["fail_count"] == 0  # 還沒查完：不算失敗
    assert sp._ai_next_try == pytest.approx(clock.wall + sp.AI_RETRY_S)
    _next_round(clock)
    rig.run()
    assert len(rig.yahoo.calls) == 23 and len(set(rig.yahoo.calls)) == 23  # 前 20 檔的結果留著，不重抓
    assert rig.picks == ["1121", "1122", "1123"]


def test_unknown_higher_rank_keeps_previous_result_and_retries(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4)
    old_t = T - timedelta(days=1)
    assert sp._commit_ai(_state([_at(_facts("2330"), old_t)], t=old_t))
    rig.yahoo.series["1101.TW"] = None  # 抓不到（兩個主機都失敗）
    rig.run()
    assert sp._ai_state["t"] == old_t.isoformat() and sp._ai_state["picks"][0]["c"] == "2330"
    assert sp._ai_state["fail_count"] == 1 and _disk()["fail_count"] == 1
    assert _history_days() == [] and sp._ai_next_try == pytest.approx(clock.wall + sp.AI_RETRY_S)
    assert rig.yahoo.calls == ["1101.TW", "1102.TW", "1103.TW", "1104.TW"]  # 抓不到的那檔之後照樣往下查，成功的留給下一輪
    del rig.yahoo.series["1101.TW"]
    _next_round(clock)
    rig.run()
    assert rig.picks == ["1101", "1102", "1103"] and sp._ai_state["fail_count"] == 0
    assert rig.yahoo.calls.count("1101.TW") == 2 and rig.yahoo.calls.count("1102.TW") == 1
    assert rig.yahoo.calls.count("1103.TW") == 1  # 成功的不重抓


def test_unknown_ranked_after_the_last_pick_never_blocks(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 6)
    rig.yahoo.series["1104.TW"] = None
    rig.yahoo.series["1105.TW"] = sp.FetchError("timeout")
    state = rig.run()
    assert rig.picks == ["1101", "1102", "1103"] and state["fail_count"] == 0
    assert rig.yahoo.calls == ["1101.TW", "1102.TW", "1103.TW"]


def test_a_failed_candidate_above_the_third_pick_blocks_the_write_and_counts(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 6)
    rig.yahoo.series["1102.TW"] = sp.FetchError("network")
    rig.run()
    assert not rig.has_result and sp._ai_state["fail_count"] == 1
    assert rig.yahoo.calls == ["1101.TW", "1102.TW", "1103.TW", "1104.TW"]  # 失敗的那檔之後照樣往下查，結果留給下一輪


def test_hole_is_unknown_for_three_hours_then_not_eligible_and_recorded(monkeypatch, clock):
    """GP1 I2：以前試 3 次（t+0／20／40 分）就悄悄換掉排名靠前的候選；現在從第一次發現算起 3 小時（真實時間）。"""
    rig = Rig(monkeypatch, clock, 3)
    rig.yahoo.kind("1101", "hole")
    first = clock.wall
    for attempt in range(1, 10):  # 每輪隔 20 分鐘多 1 秒：第 9 輪才過 2 小時 40 分
        rig.run()
        assert not rig.has_result, attempt  # 前一天那根還沒補：先不寫
        assert sp._ai_state["fail_count"] == 0 and sp._ai_hole_first["1101"] == first  # 資料有洞不是失敗
        _next_round(clock)
    rig.run()  # 第 10 輪 3 小時 00 分 09 秒：放棄，當作這個 T 不合格，並記下來
    assert rig.picks == ["1102", "1103"] and sp._ai_state["t"] == T.isoformat()
    assert sp._ai_state["skipped"] == {"1101": "previous_day_missing"} and _disk()["skipped"] == sp._ai_state["skipped"]
    assert rig.yahoo.calls.count("1101.TW") == 10 and rig.yahoo.calls.count("1102.TW") == 1
    _next_round(clock)
    rig.run()
    assert rig.yahoo.calls.count("1101.TW") == 10  # 一小時內不重看


def test_a_skipped_candidate_is_rechecked_hourly_and_recomputes_once_when_it_resolves(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 3)
    rig.yahoo.kind("1101", "hole")
    for _ in range(10):
        rig.run()
        _next_round(clock)
    assert rig.picks == ["1102", "1103"] and sp._ai_state["skipped"] == {"1101": "previous_day_missing"}
    computed_at = sp._ai_state["computed_at"]
    rig.yahoo.kind("1101", "flat")  # Yahoo 終於補上了
    rig.run()
    assert rig.yahoo.calls.count("1101.TW") == 10 and sp._ai_state["computed_at"] == computed_at  # 不到一小時：不看
    clock.advance(sp.AI_SKIP_RECHECK_S)
    rig.run()
    assert rig.picks == ["1101", "1102", "1103"] and sp._ai_state["skipped"] == {}  # 算得出來了：重算一次
    assert rig.yahoo.calls.count("1101.TW") == 11 and rig.yahoo.calls.count("1102.TW") == 1 and sp._ai_state["fail_count"] == 0
    assert [(row["rev"], [p["code"] for p in row["picks"]]) for row in _history_rows()] == [
        (1, ["1102", "1103"]), (2, ["1101", "1102", "1103"])]  # 重算後多了 1101：同一天追加第 2 版，第 1 版不改
    clock.advance(sp.AI_SKIP_RECHECK_S * 2)
    rig.run()
    assert len(rig.yahoo.calls) == 13  # 沒有 skipped 了：不再重看（3 檔＋之後 9 輪的 1101＋這次重看的 1101）


def test_skipped_recheck_is_throttled_capped_and_keeps_the_old_decision(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 3)
    rig.yahoo.kind("1101", "hole")
    for _ in range(10):
        rig.run()
        _next_round(clock)
    before = rig.yahoo.calls.count("1101.TW")
    for n in range(1, sp.AI_SKIP_RECHECK_MAX + 2):
        clock.advance(sp.AI_SKIP_RECHECK_S)  # 每小時一次、每個 T 最多 6 次；洞一直在：維持跳過
        rig.run()
        assert rig.yahoo.calls.count("1101.TW") == before + min(n, sp.AI_SKIP_RECHECK_MAX), n
        assert rig.picks == ["1102", "1103"] and sp._ai_state["skipped"] == {"1101": "previous_day_missing"}
    assert sp._ai_skip_check["n"] == sp.AI_SKIP_RECHECK_MAX


def test_hole_is_not_refetched_inside_the_negative_ttl(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 1)
    rig.yahoo.kind("1101", "hole")
    first = clock.wall
    rig.run()
    assert rig.yahoo.calls == ["1101.TW"]
    sp._ai_next_try = 0.0  # 假設馬上又輪到 AI：負快取 15 分鐘內不重抓
    clock.advance(sp.AI_NEGATIVE_TTL_S - 1)
    rig.run()
    assert rig.yahoo.calls == ["1101.TW"] and sp._ai_hole_first["1101"] == first
    sp._ai_next_try = 0.0
    clock.advance(2)
    rig.run()
    assert rig.yahoo.calls == ["1101.TW", "1101.TW"] and sp._ai_hole_first["1101"] == first  # 第一次發現的時間不重設


@pytest.mark.parametrize("kind", ["short", "jump", "tbar"])
def test_series_that_cannot_make_a_window_is_decided_at_once(monkeypatch, clock, kind):
    rig = Rig(monkeypatch, clock, 4)
    rig.yahoo.kind("1101", kind)
    state = rig.run()
    assert rig.picks == ["1102", "1103", "1104"] and state["fail_count"] == 0  # 排第一的算不出來也不擋後面的
    assert rig.yahoo.calls.count("1101.TW") == 1 and "1101" not in sp._ai_hole_first and "1101" not in sp._ai_fail_tries


def test_window_reasons_match_the_gray_diagnostic():
    snapshot = _snapshot([_facts("1101")])
    row = snapshot.listed["1101"]
    prev = sp._prev_reference(row.close, row.change)
    expected = {"flat": None, "hot": None, "below": None, "short": "bars_short", "jump": "price_jump",
                "hole": "previous_day_missing", "tbar": "close_mismatch"}
    for kind, reason in expected.items():
        bars = sp.parse_chart(_series(snapshot, kind))
        windows = sp.window_closes(bars, snapshot.calendar, snapshot.t, row.close, prev)
        assert (windows is None) == (reason is not None), kind
        if reason is not None:
            assert sp._window_unavailable_reason(bars, snapshot.calendar, snapshot.t, row.close, prev) == reason
    assert sp._AI_HOLE_REASONS == {"previous_day_missing", "unknown"}


def test_a_dead_yahoo_is_a_failure_every_round_and_the_previous_result_stays(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4)
    old_t = T - timedelta(days=1)
    assert sp._commit_ai(_state([_at(_facts("2330"), old_t)], t=old_t))
    for symbol in ("1101.TW", "1102.TW", "1103.TW", "1104.TW", "2330.TW"):  # 整個 Yahoo 壞掉：連抽查用的 2330 也回不出
        rig.yahoo.series[symbol] = None
    for n in (1, 2, 3, 4, 5, 6):
        rig.run()
        assert sp._ai_state["fail_count"] == n and sp._ai_state["picks"][0]["c"] == "2330"  # 抓不到不會被當成「沒有合格」
        assert not sp._ai_fail_tries and not any(v[0] == "no" for v in sp._ai_chart_cache.values())  # 也不累計到單檔頭上
        _next_round(clock)


def test_yahoo_cooldown_is_waiting_not_failure(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4)
    sp._block_yahoo()
    before = copy.deepcopy(sp._worker)
    rig.run()
    assert rig.yahoo.calls == [] and sp._worker == before
    assert not rig.has_result and sp._ai_state["fail_count"] == 0
    assert sp._ai_next_try == pytest.approx(clock.wall + sp.AI_RETRY_S)
    _next_round(clock)  # 冷卻（20 分鐘）過了
    rig.run()
    assert rig.picks == ["1101", "1102", "1103"] and sp._ai_state["fail_count"] == 0


def test_ai_429_blocks_yahoo_but_never_changes_main_worker(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4)
    before = copy.deepcopy(sp._worker)

    def limited(symbol, **kwargs):
        rig.yahoo.calls.append(symbol)
        sp._block_yahoo()  # _fetch_chart 遇到 429 時的行為
        raise sp.FetchError("rate_limited")

    monkeypatch.setattr(sp, "_fetch_chart", limited)
    rig.run()
    assert sp._worker == before and sp._yahoo_blocked()
    assert rig.yahoo.calls == ["1101.TW"]  # 冷卻中不再打下一檔
    assert sp._ai_state["fail_count"] == 1  # 真的撞到 429 算一次失敗，但只記在 AI 自己的狀態
    sp._ai_next_try = 0.0  # 假設馬上又輪到 AI（冷卻還沒過）
    clock.advance(600)
    rig.run()
    assert rig.yahoo.calls == ["1101.TW"] and sp._worker == before and sp._ai_state["fail_count"] == 1


def test_time_budget_stops_the_round_without_failing(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 6)
    rig.yahoo.per_call_s = 40.0
    for n in range(6):
        rig.yahoo.kind(f"{1101 + n}", "below")
    rig.run()
    assert len(rig.yahoo.calls) == 2  # 第 3 檔時 80 秒已經超過 60 秒的預算
    assert not rig.has_result and sp._ai_state["fail_count"] == 0
    rig.yahoo.per_call_s = 0.0
    _next_round(clock)
    rig.run()
    assert rig.yahoo.calls[2:] == [f"{1101 + n}.TW" for n in range(2, 6)]  # 前兩檔留著，往下接
    assert rig.has_result and rig.picks == [] and sp._ai_state["funnel"]["trend"] == 0


def test_stop_event_aborts_without_writing(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 5)
    stop = threading.Event()
    original = rig.yahoo

    def fetch_then_stop(symbol, **kwargs):
        stop.set()
        return original(symbol, **kwargs)

    monkeypatch.setattr(sp, "_fetch_chart", fetch_then_stop)
    rig.run(stop)
    assert sp._ai_state is None and _disk() is None


def test_disabled_ai_makes_no_requests(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 2)
    monkeypatch.setattr(sp, "AI_ENABLED", False)

    def fail(*args, **kwargs):
        raise AssertionError("關閉 AI 時不能抓名單、Yahoo 或讀核心檔")

    monkeypatch.setattr(sp, "_ai_universe", fail)
    monkeypatch.setattr(sp, "_fetch_chart", fail)
    monkeypatch.setattr(sp, "_ai_core", fail)
    assert rig.run() is None and _disk() is None


def test_background_flag_off_skips_everything(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 2)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", False)
    assert rig.run() is None and rig.universe_calls == 0 and rig.yahoo.calls == []


def test_no_snapshot_means_nothing_to_do(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 2)
    monkeypatch.setattr(sp, "_snapshot", None)
    assert rig.run() is None and rig.universe_calls == 0


def test_a_newer_ai_state_than_the_snapshot_is_never_rolled_back(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 2)
    newer = T + timedelta(days=1)
    assert sp._commit_ai(_state([_at(_facts("2330"), newer)], t=newer))
    rig.run()
    assert rig.universe_calls == 0 and sp._ai_state["t"] == newer.isoformat()


def test_new_trading_day_recomputes_and_clears_the_line_cache(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4)
    rig.run()
    assert sp._ai_cache_t == T and set(sp._ai_chart_cache) == {"1101", "1102", "1103"}
    next_day = T + timedelta(days=1)
    rig.snapshot = _snapshot(rig.facts, t=next_day, pool=False)
    monkeypatch.setattr(sp, "_snapshot", rig.snapshot)
    rig.yahoo.snapshot = rig.snapshot
    _next_round(clock)
    rig.run()
    assert sp._ai_state["t"] == next_day.isoformat() and sp._ai_cache_t == next_day
    assert rig.yahoo.calls.count("1101.TW") == 2  # 換了一天：舊日線不能沿用
    assert _history_days() == [T.isoformat(), next_day.isoformat()]


def test_rule_or_core_change_recomputes_the_same_t(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4)
    rig.run()
    assert rig.universe_calls == 1
    rig.version = "test-v2"  # 核心名單換了
    _next_round(clock)
    state = rig.run()
    assert rig.universe_calls == 2 and state["core_version"] == "test-v2" and len(rig.yahoo.calls) == 3  # 日線快取沿用
    old = copy.deepcopy(sp._ai_state)
    old["rule"] = "ai_v0"
    assert sp._commit_ai(old)  # 規則版本變了
    _next_round(clock)
    rig.run()
    assert rig.universe_calls == 3 and sp._ai_state["rule"] == sp.AI_RULE
    assert [row["rev"] for row in _history_rows()] == [1]  # 名單、規則換了重算兩次，推薦沒變：紀錄還是一行


def test_logs_carry_reason_codes_and_counts_only(monkeypatch, clock, caplog):
    rig = Rig(monkeypatch, clock, 25)
    for i in range(20):
        rig.yahoo.kind(f"{1101 + i}", "below")
    rig.run()  # 抓取上限用完：還在等，不算失敗
    assert "stock picks ai waiting reason=limit fail_count=0 retry_in_s=1200" in caplog.text
    rig.yahoo.series["1121.TW"] = None
    _next_round(clock)
    rig.run()  # 抓不到
    assert "stock picks ai failed reason=fetch_failed fail_count=1 retry_in_s=1200" in caplog.text
    rig.yahoo.kind("1121", "hole")
    _next_round(clock)
    rig.run()  # 日線有洞
    assert "stock picks ai waiting reason=line_hole fail_count=1" in caplog.text and "code=1121 hole reason=previous_day_missing waited_s=0" in caplog.text
    rig.yahoo.kind("1121", "flat")
    _next_round(clock)
    rig.run()
    assert "stock picks ai computed t=2026-10-08 source=00952 universe=25 growth=25 cheap=25 picks=3 skipped=0 fetched=" in caplog.text
    assert "Traceback" not in caplog.text and "synthetic" not in caplog.text


# ── _run 裡唯一的 hook ───────────────────────────────────────────────────────
def _calls_in(function_name, callee):
    """stock_picks.py 現在的原始碼裡，函式 function_name 內呼叫 callee 的行號（讀檔案、用 ast，不怕行號位移）。"""
    tree = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function_name)
    return [n.lineno for n in ast.walk(function)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee]


def test_the_hook_has_exactly_one_call_site_after_tick():
    assert _calls_in("_tick", "_maybe_fill_ai") == []  # 不放在 _tick 裡（它有好幾個提早返回的出口）
    hook, tick = _calls_in("_run", "_maybe_fill_ai"), _calls_in("_run", "_tick")
    assert len(hook) == 1 and len(tick) == 1 and tick[0] < hook[0]
    assert sum(len(_calls_in(name, "_maybe_fill_ai")) for name in ("_fail", "_fetch_chart", "refresh", "handle", "_picks_reply")) == 0


class _Spy(threading.Event):
    """取代 wake：記下 _run 要等多久，並讓迴圈在這一輪之後結束。"""

    def __init__(self, stop):
        super().__init__()
        self.stop, self.waits = stop, []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.stop.set()
        return True


def _run_one_round(monkeypatch, tick, hook):
    stop = threading.Event()
    wake = _Spy(stop)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(sp, "_tick", tick)
    monkeypatch.setattr(sp, "_maybe_fill_ai", hook)
    sp._run(stop, wake)
    return wake


@pytest.mark.parametrize("how", ["normal", "raises", "early_return"])
def test_run_calls_the_hook_once_after_every_tick_outcome(monkeypatch, how):
    calls = []
    monkeypatch.setattr(sp, "_fail", lambda *a, **kw: calls.append("fail") or 1.0)
    if how == "early_return":
        sp._worker.next_try = 4e9  # 主 worker 還在退避：_tick 一開始就返回，沒有網路請求
        tick = sp._tick
    else:
        def tick(_):
            calls.append("tick")
            if how == "raises":
                raise RuntimeError("synthetic")
            return 1800.0
    _run_one_round(monkeypatch, tick, lambda _: calls.append("ai"))
    assert calls == {"normal": ["tick", "ai"], "raises": ["tick", "fail", "ai"], "early_return": ["ai"]}[how]


def test_ai_exception_is_recorded_for_ai_only(monkeypatch, clock):
    before = copy.deepcopy(sp._worker)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "_fail", lambda *a, **kw: pytest.fail("AI 的例外不能進主 worker 的 _fail"))

    def hook(_):
        raise RuntimeError("synthetic private text")

    wake = _run_one_round(monkeypatch, lambda _: 1800.0, hook)
    assert sp._worker == before
    assert sp._ai_state["fail_count"] == 1 and sp._ai_state["t"] is None
    assert wake.waits == [pytest.approx(sp.AI_RETRY_S)]  # 比主 worker 的 30 分鐘早醒來，AI 自己 20 分鐘後再試


def test_a_failing_failure_record_cannot_kill_the_worker_thread(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("even the failure record fails")

    monkeypatch.setattr(sp, "_ai_failure", boom)
    wake = _run_one_round(monkeypatch, lambda _: 5.0, boom)
    assert wake.waits == [5.0]  # 照樣走到下一輪的等待


def test_missing_leaf_module_is_an_ai_only_failure(monkeypatch, clock):
    monkeypatch.setattr(sp, "_snapshot", _snapshot([_facts("1101")]))
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    monkeypatch.setitem(sys.modules, "stock_picks_ai_source", None)  # 葉節點模組缺檔
    before = copy.deepcopy(sp._worker)
    with pytest.raises(ImportError):
        sp._maybe_fill_ai(threading.Event())
    wake = _run_one_round(monkeypatch, lambda _: 1800.0, sp._maybe_fill_ai)
    assert sp._worker == before and wake.waits == [pytest.approx(sp.AI_RETRY_S)]
    assert "計算中" in json.dumps(sp.handle("").flex, ensure_ascii=False)  # /股票 照常回覆


def test_failure_throttle_is_ai_own_twenty_minutes(monkeypatch, clock):
    Rig(monkeypatch, clock, 2)
    before = copy.deepcopy(sp._worker)
    calls = []

    def universe(*args):
        calls.append(1)
        raise ValueError("synthetic")

    monkeypatch.setattr(sp, "_ai_universe", universe)
    _run_one_round(monkeypatch, lambda _: 1800.0, sp._maybe_fill_ai)
    assert len(calls) == 1 and sp._ai_state["fail_count"] == 1 and sp._worker == before
    sp._maybe_fill_ai(threading.Event())
    assert len(calls) == 1  # 20 分鐘內不再試
    clock.advance(sp.AI_RETRY_S - 5)
    sp._maybe_fill_ai(threading.Event())
    assert len(calls) == 1
    clock.advance(10)
    with pytest.raises(ValueError):
        sp._maybe_fill_ai(threading.Event())
    assert len(calls) == 2


# ── 名單：00952 持股 ∪ 核心名單，過目前上市普通股資格 ────────────────────────────
HOLDING_CODES = [f"{1100 + n}" for n in range(1, 51)]  # 1101…1150


def _holdings(as_of=date(2026, 10, 22), codes=None):
    return source.Holdings(as_of, tuple((code, 2.0) for code in (codes or HOLDING_CODES)))


def _write_core(path, entries, as_of="2026-10-24"):
    rows = [{"code": code, "name": f"核心{code}", "segment": segment, "source_url": f"https://example.com/{code}",
             "source_date": "2026-10-01"} for code, segment in entries]
    path.write_text(json.dumps({"schema": 1, "as_of": as_of, "entries": rows}), encoding="utf-8")
    return path


class UniverseRig:
    """真的 _ai_universe／_ai_core，只把 00952 的抓取換成替身（behavior 回 Holdings 或要丟的例外）、核心檔換成暫存檔。"""

    def __init__(self, monkeypatch, tmp_path, clock, *, listed=None, core=None, fetch=None):
        codes = listed or [f"{1100 + n}" for n in range(1, 41)]
        self.snapshot = _snapshot([_facts(code) for code in codes])
        entries = core if core is not None else [("1103", "AI 電源"), ("1160", "AI 散熱"), ("2330", "AI 晶片代工")]
        self.core_path = _write_core(tmp_path / "core.json", entries)
        monkeypatch.setattr(source, "CORE_PATH", self.core_path)
        self.fetches: list[dict] = []
        self.behavior = fetch or (lambda **kw: _holdings())
        monkeypatch.setattr(source, "fetch_holdings", self.fetch)

    def fetch(self, **kwargs):
        self.fetches.append(kwargs)
        result = self.behavior(**kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    def universe(self, budget=60.0):
        return sp._ai_universe(self.snapshot, sp.time.monotonic() + budget)


def test_core_version_follows_the_list_content_but_not_the_display_name(monkeypatch, tmp_path):
    path = _write_core(tmp_path / "core.json", [("1101", "AI 電源"), ("1102", "AI 散熱")])
    monkeypatch.setattr(source, "CORE_PATH", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    def version(mutate=lambda data: None):
        data = copy.deepcopy(raw)
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        core, value = sp._ai_core()
        assert core is not None
        return value

    same = version()
    assert len(same) == 64 and version() == same
    assert version(lambda d: d["entries"][0].update(name="只是改了顯示用的名字")) == same  # name 不上卡片，不值得重算
    for change in (lambda d: d["entries"][0].update(segment="AI 網通"), lambda d: d["entries"][0].update(code="1103"),
                   lambda d: d["entries"].pop(), lambda d: d.update(as_of="2026-10-23"),
                   lambda d: d["entries"][0].update(source_url="https://example.com/other"),
                   lambda d: d["entries"][0].update(source_date="2026-09-30")):
        assert version(change) != same


def test_union_is_filtered_by_the_current_listed_ordinary_stocks(monkeypatch, tmp_path, clock):
    held = HOLDING_CODES[:46] + ["0050", "9103", "1150", "1148"]  # ETF、TDR、創新板、已不在上市資料裡的都在持股名單裡
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: _holdings(codes=held))
    rig.snapshot.listed["0050"] = sp.ListedRow("元大台灣50", sp.ETF, "00", 100.0, 0.0, None, None, None)
    rig.snapshot.listed["9103"] = sp.ListedRow("美德醫療-DR", sp.STOCK, "91", 10.0, 0.0, None, None, None)
    rig.snapshot.listed["1150"] = sp.ListedRow("某某-創", sp.STOCK, "01", 10.0, 0.0, 10.0, None, None)
    u, sources, core_as_of, version = rig.universe()
    assert u["source"] == "00952" and u["as_of"] == "2026-10-22" and u["fetched_day"] == TODAY.isoformat()
    assert u["etf_codes"] == sorted(held)  # 整份持股（含上櫃、已下市）的代號都記著
    assert u["codes"] == [f"{1100 + n}" for n in range(1, 41)]  # 只留目前上市普通股（核心名單的 1160、2330 不在上市資料裡）
    assert sources["1103"] == "00952"  # 兩邊都有：00952 優先
    assert core_as_of == "2026-10-24" and len(version) == 64


def test_core_only_names_keep_their_fixed_segment(monkeypatch, tmp_path, clock):
    held = ["1101"] + [f"{3000 + n}" for n in range(49)]
    rig = UniverseRig(monkeypatch, tmp_path, clock, listed=["1101", "1160"], core=[("1160", "AI 散熱")],
                      fetch=lambda **kw: _holdings(codes=held))
    u, sources, _, _ = rig.universe()
    assert u["codes"] == ["1101", "1160"] and sources == {"1101": "00952", "1160": "AI 散熱"}


def test_holdings_are_fetched_once_per_taipei_day(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock)
    rig.universe()
    rig.universe()
    clock.advance(10 * 3600)
    rig.universe()
    assert len(rig.fetches) == 1  # 用抓取當天（fetched_day）判斷，不看網頁上的日期（週末會一直不等於今天）
    kwargs = rig.fetches[0]
    assert kwargs["today"] == TODAY and kwargs["previous_as_of"] is None and 0 < kwargs["deadline"] <= 5.0
    monkeypatch.setattr(sp, "_taipei_today", lambda: TODAY + timedelta(days=1))
    u, *_ = rig.universe()
    assert len(rig.fetches) == 2 and rig.fetches[1]["previous_as_of"] == date(2026, 10, 22)  # 帶著上一份的日期
    assert u["fetched_day"] == (TODAY + timedelta(days=1)).isoformat()


def test_failed_fetch_waits_two_hours_and_stops_after_three_attempts(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: source.SourceError("fetch_failed"))
    u, *_ = rig.universe()
    assert len(rig.fetches) == 1 and u["source"] == "core_only"  # 沒有上一份：只用核心名單
    clock.advance(sp.AI_FETCH_RETRY_S - 1)
    rig.universe()
    assert len(rig.fetches) == 1
    clock.advance(2)
    rig.universe()
    assert len(rig.fetches) == 2
    clock.advance(sp.AI_FETCH_RETRY_S + 1)
    rig.universe()
    clock.advance(sp.AI_FETCH_RETRY_S + 1)
    rig.universe()
    assert len(rig.fetches) == 3 == sp._ai_fetch_budget["attempts"]  # 一天最多 3 次


@pytest.mark.parametrize("status", [403, 429, 500, 503, 599])
def test_blocked_or_broken_site_is_not_hit_again_today(monkeypatch, tmp_path, clock, status):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: source.SourceError("fetch_status", status_code=status))
    rig.universe()
    clock.advance(5 * 3600)
    rig.universe()
    assert len(rig.fetches) == 1 and sp._ai_fetch_budget["attempts"] == sp.AI_FETCH_PER_DAY
    monkeypatch.setattr(sp, "_taipei_today", lambda: TODAY + timedelta(days=1))
    rig.universe()
    assert len(rig.fetches) == 2  # 隔天帳重開


@pytest.mark.parametrize("status", [404, 301, None])
def test_other_failures_only_wait_two_hours(monkeypatch, tmp_path, clock, status):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: source.SourceError("fetch_status", status_code=status))
    rig.universe()
    assert sp._ai_fetch_budget["attempts"] == 1


def test_unexpected_fetch_exception_is_contained_without_leaking_text(monkeypatch, tmp_path, clock, caplog):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: RuntimeError("secret page text"))
    u, *_ = rig.universe()
    assert u["source"] == "core_only" and "secret page text" not in caplog.text
    assert "reason=error" in caplog.text


def test_cached_holdings_are_used_for_up_to_45_days(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: source.SourceError("fetch_failed"))
    cached = {"codes": HOLDING_CODES, "as_of": (TODAY - timedelta(days=45)).isoformat(),
              "fetched_day": (TODAY - timedelta(days=1)).isoformat()}
    monkeypatch.setattr(sp, "_ai_holdings", cached)
    u, sources, *_ = rig.universe()
    assert u["source"] == "cached" and u["as_of"] == cached["as_of"] and u["fetched_day"] == cached["fetched_day"]
    assert sources["1101"] == "00952"
    stale = dict(cached, as_of=(TODAY - timedelta(days=46)).isoformat())
    monkeypatch.setattr(sp, "_ai_holdings", stale)
    monkeypatch.setattr(sp, "_ai_fetch_budget", {})
    u, sources, *_ = rig.universe()
    assert u["source"] == "core_only" and u["etf_codes"] == [] and u["as_of"] == "2026-10-24"
    assert set(sources.values()) <= source.SEGMENTS and u["fetched_day"] is None


def test_a_fetch_error_keeps_the_previous_holdings_object(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: source.SourceError("holdings_older"))
    cached = {"codes": HOLDING_CODES, "as_of": "2026-10-21", "fetched_day": "2026-10-23"}
    monkeypatch.setattr(sp, "_ai_holdings", cached)
    rig.universe()
    assert rig.fetches[0]["previous_as_of"] == date(2026, 10, 21)  # 抓到比上一份舊的就不用，由葉節點模組判斷
    assert sp._ai_holdings is cached


def test_invalid_core_file_means_00952_only(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock)
    rig.core_path.write_text("{broken", encoding="utf-8")
    u, sources, core_as_of, version = rig.universe()
    assert version == "invalid" and core_as_of is None and set(sources.values()) == {"00952"}
    assert u["source"] == "00952" and u["codes"]


def test_nothing_eligible_is_a_failure_not_an_empty_list(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock, listed=["3000"], core=[("1160", "AI 散熱")])
    with pytest.raises(ValueError, match="universe_empty"):
        rig.universe()


def test_oversized_union_fails_closed(monkeypatch, tmp_path, clock):
    listed = [f"{2000 + n}" for n in range(130)]
    rig = UniverseRig(monkeypatch, tmp_path, clock, listed=listed, core=[(code, "AI 伺服器") for code in listed[:100]],
                      fetch=lambda **kw: _holdings(codes=listed[80:130]))
    with pytest.raises(source.SourceError, match="universe_size"):
        rig.universe()


def test_fetch_deadline_never_exceeds_the_round_budget(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock)
    rig.universe(budget=0.5)
    assert 0 < rig.fetches[0]["deadline"] <= 0.5


def test_fetch_budget_survives_a_restart(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: source.SourceError("fetch_failed"))
    rig.universe()
    sp._ai_failure()  # 這輪沒寫成，但抓名單的帳要跟著存
    assert _disk()["fetch_budget"]["attempts"] == 1
    for name, value in (("_ai_state", None), ("_ai_fetch_budget", {}), ("_ai_holdings", None)):
        monkeypatch.setattr(sp, name, value)
    monkeypatch.setattr(sp, "_loaded", False)
    sp._ensure_loaded()
    rig.universe()
    assert len(rig.fetches) == 1  # 重啟後 2 小時內不會馬上再打


def test_missing_00952_today_triggers_one_more_try_later_the_same_day(monkeypatch, tmp_path, clock):
    outcomes = [source.SourceError("fetch_failed"), _holdings()]
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: outcomes.pop(0))
    monkeypatch.setattr(sp, "_snapshot", rig.snapshot)  # 主池有走勢：不需要 Yahoo
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "_fetch_chart", lambda *a, **k: pytest.fail("主池已有走勢"))
    sp._maybe_fill_ai(threading.Event())
    assert sp._ai_state["universe"]["source"] == "core_only" and len(rig.fetches) == 1
    assert [p["c"] for p in sp._ai_state["picks"]] == ["1103"]  # 核心名單裡唯一在上市資料裡的
    clock.advance(600)
    sp._maybe_fill_ai(threading.Event())
    assert len(rig.fetches) == 1  # 失敗後 2 小時內不重試
    clock.advance(sp.AI_FETCH_RETRY_S)
    sp._maybe_fill_ai(threading.Event())
    assert len(rig.fetches) == 2 and sp._ai_state["universe"]["source"] == "00952"
    assert [p["c"] for p in sp._ai_state["picks"]] == ["1101", "1102", "1103"]
    clock.advance(sp.AI_FETCH_RETRY_S * 2)
    sp._maybe_fill_ai(threading.Event())
    assert len(rig.fetches) == 2  # 今天的 00952 到手了：同一個 T 不再重算
    assert [(row["t"], row["rev"], row["source"], [p["code"] for p in row["picks"]]) for row in _history_rows()] == [
        (T.isoformat(), 1, "core_only", ["1103"]), (T.isoformat(), 2, "00952", ["1101", "1102", "1103"])]  # 00952 晚到：第 2 版


def test_blocked_site_does_not_cause_endless_recomputation(monkeypatch, tmp_path, clock):
    rig = UniverseRig(monkeypatch, tmp_path, clock, fetch=lambda **kw: source.SourceError("fetch_status", status_code=403))
    monkeypatch.setattr(sp, "_snapshot", rig.snapshot)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    sp._maybe_fill_ai(threading.Event())
    computed = sp._ai_state["computed_at"]
    for _ in range(5):
        clock.advance(sp.AI_FETCH_RETRY_S + 1)
        sp._maybe_fill_ai(threading.Event())
    assert len(rig.fetches) == 1 and sp._ai_state["computed_at"] == computed


# ── 端到端：合成市場 → 重算快照 → AI 計算 → /股票 卡片 ────────────────────────────
@pytest.fixture
def market(monkeypatch, tmp_path):
    net = FakeNet()
    monkeypatch.setattr(sp, "_get_json", net)
    monkeypatch.setattr(sp, "_MIN_ROWS", 50)
    monkeypatch.setattr(sp, "T86_SPACING_S", 0.0)
    cheap = {"1101", "1102", "1103", "1104", "1105", "1153", "1154"}
    for row in net.pe:  # 便宜：本益比 6（≥ 5.0、遠低於同業中位數）
        if row["Code"] in cheap:
            row["PEratio"] = "6"
    growth = {"1101": "80.0", "1102": "60.0", "1103": "40.0", "1104": "20.0", "1105": "10.0", "1153": "90.0", "1154": "70.0"}
    for row in net.revenue:
        row["累計營業收入-前期比較增減(%)"] = growth.get(row["公司代號"], row["累計營業收入-前期比較增減(%)"])
    held = ["1101", "1102", "1103", "1105", "1153"] + [f"{3000 + n}" for n in range(45)]
    core = _write_core(tmp_path / "core.json", [("1104", "AI 散熱"), ("1154", "ABF 載板"), ("2330", "AI 晶片代工")])
    monkeypatch.setattr(source, "CORE_PATH", core)
    monkeypatch.setattr(source, "fetch_holdings", lambda **kw: _holdings(codes=held))
    return net


def _fresh_snapshot(net):
    from test_stock_picks_data import T as DATA_T
    snapshot = sp.refresh(DATA_T, _day_rows(net), threading.Event(), now=sp.time.time())
    assert sp._commit_snapshot(snapshot) == sp.COMMIT_WRITTEN
    sp._worker.last_ok_check, sp._worker.seen_t = sp.time.time(), DATA_T
    return snapshot


def test_end_to_end_from_market_to_cards(market, monkeypatch):
    from test_stock_picks import _assert_sendable
    from test_stock_picks_data import T as DATA_T
    snapshot = _fresh_snapshot(market)
    assert "1153" not in snapshot.pool and "1101" in snapshot.pool  # 主池是市值前 50 大：1153 不在，要另外抓日線
    legacy = tuple(snapshot.picks)
    worker_before = copy.deepcopy(sp._worker)
    market.calls.clear()
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    sp._maybe_fill_ai(threading.Event())
    state = sp._ai_state
    assert state["t"] == DATA_T.isoformat() and state["universe"]["source"] == "00952"
    # 累計營收年增 ÷ 本益比：1153（90/6）＞ 1101（80/6）＞ 1154（70/6）＞ 1102 ＞ …；
    # 1154 來自核心名單（ABF 載板）、1105 累計不到 15% 被擋、1153 不在主池要另外抓日線
    assert [p["c"] for p in state["picks"]] == ["1153", "1101", "1154"]
    assert [p["ai_source"] for p in state["picks"]] == ["00952", "00952", "ABF 載板"]
    assert state["funnel"]["universe"] == len(state["universe"]["codes"]) == 8 and state["funnel"]["growth"] == 6
    assert state["funnel"]["cheap"] == 6 and state["funnel"]["trend"] == 3
    yahoo = [call[1].rsplit("/", 1)[-1] for call in market.calls if call[0] in sp._YAHOO]
    assert yahoo == ["1153.TW", "1154.TW"]  # 只抓主池沒有的、而且排在前面的；滿 3 檔就停
    assert sp._worker == worker_before and sp._snapshot.picks == legacy  # 主 worker 與舊推薦（影子紀錄）沒被動到
    reply = sp.handle("")
    _assert_sendable(reply.flex, reply.alt_text, "股票推薦：只供參考｜")
    text = json.dumps(reply.flex, ensure_ascii=False)
    assert reply.alt_text == "股票推薦：只供參考｜股1153、股1101、股1154｜資料到 10/08"
    assert "AI 相關：核心名單（ABF 載板）" in text and "AI 相關：凱基台灣AI50 持股（名單 10/22）" in text
    assert "常問股" in text and "市值前 50" not in text
    monkeypatch.setattr(sp, "AI_ENABLED", False)  # 緊急開關：舊推薦卡
    legacy_reply = sp.handle("")
    assert "AI 相關股推薦" not in json.dumps(legacy_reply.flex, ensure_ascii=False)
    assert len(legacy_reply.flex["contents"]) == len(legacy) + 1


def test_end_to_end_next_day_keeps_yesterday_until_the_new_picks_are_ready(market, monkeypatch):
    from test_stock_picks_data import T as DATA_T
    _fresh_snapshot(market)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    sp._maybe_fill_ai(threading.Event())
    assert sp._ai_state["t"] == DATA_T.isoformat()
    # 隔天主快照換了新的 T，但 AI 這輪抓不到日線：照樣顯示上一份並標「資料到」
    next_t = DATA_T + timedelta(days=1)
    monkeypatch.setattr(sp, "_snapshot", replace(sp._snapshot, t=next_t))
    sp._worker.seen_t = next_t
    monkeypatch.setattr(sp, "_fetch_chart", lambda *a, **k: None)
    sp._maybe_fill_ai(threading.Event())
    assert sp._ai_state["t"] == DATA_T.isoformat() and sp._ai_state["fail_count"] == 1
    text = json.dumps(sp.handle("").flex, ensure_ascii=False)
    assert "推薦資料到 10/08" in text and "新資料計算中" not in text and "4 條都符合" in text


def test_the_real_background_thread_refreshes_then_fills_the_ai_picks(market, monkeypatch):
    """真的執行緒、真的 _tick／refresh／_maybe_fill_ai（只有網路是合成的）：第一輪就算出快照，接著同一輪算出 AI 推薦。"""
    import time as real_time
    from test_stock_picks_data import T as DATA_T
    for name in ("_thread", "_stop", "_wake"):
        monkeypatch.setattr(sp, name, None)
    monkeypatch.setattr(sp, "_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0)
    try:
        sp.start_background()
        deadline = real_time.monotonic() + 60
        while real_time.monotonic() < deadline and not (sp._ai_state or {}).get("t"):
            real_time.sleep(0.05)
        assert sp._snapshot is not None and sp._snapshot.t == DATA_T
        assert (sp._ai_state or {}).get("t") == DATA_T.isoformat(), "AI 推薦沒有在同一輪算出來"
        assert [p["c"] for p in sp._ai_state["picks"]] == ["1153", "1101", "1154"]
        assert sp._worker.fail_count == 0 and sp._ai_state["fail_count"] == 0
        assert sp.load_ai_file()["picks"] == sp._ai_state["picks"]
        assert "AI 相關股推薦：第 1 檔" in json.dumps(sp.handle("").flex, ensure_ascii=False)
    finally:
        sp.stop_background(timeout=5.0)


# ── 共用的 Yahoo 冷卻 ────────────────────────────────────────────────────────
def test_blocking_yahoo_keeps_the_longest_deadline(clock):
    sp._block_yahoo()
    first = sp._yahoo_blocked_until
    clock.advance(30)
    sp._block_yahoo()
    assert sp._yahoo_blocked_until == pytest.approx(first + 30) and sp._yahoo_blocked()
    clock.advance(sp.BLOCKED_BACKOFF_S + 1)
    assert not sp._yahoo_blocked()


def test_clear_caches_resets_every_ai_global():
    sp._ai_state, sp._ai_next_try, sp._ai_cache_t, sp._ai_holdings = {"x": 1}, 5.0, T, {"y": 2}
    sp._ai_chart_cache["1101"] = ("no", "bars_short")
    sp._ai_hole_first["1101"], sp._ai_fail_tries["1101"] = 1.0, 2
    sp._ai_skip_check.update(n=3, next=9.0)
    sp._ai_fetch_budget["day"] = "2026-10-24"
    sp._yahoo_blocked_until = 9e9
    sp.clear_caches()
    assert (sp._ai_state, sp._ai_next_try, sp._ai_cache_t, sp._ai_holdings) == (None, 0.0, None, None)
    assert not sp._ai_chart_cache and not sp._ai_hole_first and not sp._ai_fail_tries and not sp._ai_fetch_budget
    assert sp._ai_skip_check == {"n": 0, "next": 0.0} and sp._yahoo_blocked_until == 0.0


# ── 計畫 C 事後審查修正（2026-10-28）：回不出日線的單檔、AI 計時器、同 T 重算 ───────────────
def test_a_symbol_yahoo_cannot_serve_is_dropped_after_three_counted_rounds(monkeypatch, clock):
    """GP1 I1／GP2 #2：排名靠前的一檔永遠回不出日線（404）不能讓整份推薦永遠卡住；
    其他檔的結果都在快取裡時，那一輪只剩它在抓：抽查 2330 確認 Yahoo 是活的才算一次。"""
    rig = Rig(monkeypatch, clock, 6)
    rig.yahoo.series["1101.TW"] = None
    rig.run()
    assert not rig.has_result and sp._ai_fail_tries == {"1101": 1} and sp._ai_state["fail_count"] == 1
    assert rig.yahoo.calls == ["1101.TW", "1102.TW", "1103.TW", "1104.TW"]  # 當輪其他三檔有回資料：算一次
    _next_round(clock)
    rig.run()
    assert rig.yahoo.calls[4:] == ["1101.TW", "2330.TW"] and sp._ai_fail_tries == {"1101": 2}
    _next_round(clock)
    rig.run()
    assert sp._ai_fail_tries == {"1101": 3} and sp._ai_chart_cache["1101"] == ("no", "chart_unavailable")
    assert not rig.has_result and rig.yahoo.calls.count("2330.TW") == 2
    _next_round(clock)
    state = rig.run()  # 下一輪寫入：1101 當作這個 T 不合格，並記在 skipped
    assert rig.picks == ["1102", "1103", "1104"] and state["skipped"] == {"1101": "chart_unavailable"}
    assert state["fail_count"] == 0 and rig.yahoo.calls.count("1101.TW") == 3 and _history_days() == [T.isoformat()]


def test_a_global_outage_after_the_other_fetches_were_cached_never_drops_a_candidate(monkeypatch, clock, caplog):
    rig = Rig(monkeypatch, clock, 6)
    rig.yahoo.series["1101.TW"] = None
    rig.run()
    assert sp._ai_fail_tries == {"1101": 1}
    rig.yahoo.series["2330.TW"] = None  # 從這裡起整個 Yahoo 都回不出（抽查也一樣）
    for _ in range(6):
        _next_round(clock)
        rig.run()
        assert sp._ai_fail_tries == {"1101": 1} and "1101" not in sp._ai_chart_cache and not rig.has_result
    assert "reason=yahoo_not_confirmed" in caplog.text
    del rig.yahoo.series["2330.TW"]  # 恢復：再開始算
    _next_round(clock)
    rig.run()
    assert sp._ai_fail_tries == {"1101": 2}


@pytest.mark.parametrize("error", [sp.FetchError("rate_limited"), sp.FetchError("cooldown"), sp.FetchError("stopped")])
def test_429_cooldown_and_stop_are_never_counted_as_a_symbol_failure(monkeypatch, clock, error):
    rig = Rig(monkeypatch, clock, 4)
    rig.yahoo.series["1101.TW"] = error
    rig.run()
    assert not sp._ai_fail_tries and "1101" not in sp._ai_chart_cache and not rig.has_result


def test_other_fetch_error_kinds_count_like_an_empty_answer(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 4)
    rig.yahoo.series["1101.TW"] = sp.FetchError("network")
    rig.run()
    assert sp._ai_fail_tries == {"1101": 1}


def test_counts_holes_and_skip_rechecks_start_over_for_a_new_t(monkeypatch, clock):
    rig = Rig(monkeypatch, clock, 3)
    sp._ai_cache_t = T - timedelta(days=1)  # 上一個 T 留下的帳
    sp._ai_fail_tries["1101"], sp._ai_hole_first["1101"] = 2, 1.0
    sp._ai_chart_cache["1101"] = ("no", "chart_unavailable")
    sp._ai_skip_check.update(n=4, next=9e12)
    rig.run()
    assert sp._ai_cache_t == T and rig.picks == ["1101", "1102", "1103"]  # 昨天的判定不帶到新的 T
    assert sp._ai_fail_tries == {} and sp._ai_hole_first == {} and sp._ai_skip_check == {"n": 0, "next": 0.0}


def test_a_transient_failure_during_a_same_t_recompute_does_not_mark_the_good_result_failed(monkeypatch, clock):
    """GP1 N6：00952 晚到而重算，這次剛好有一檔抓不到——原本好好的結果不能被標成 failed，也不能掉「新資料計算中」。"""
    rig = Rig(monkeypatch, clock, 4)
    rig.run()
    good = copy.deepcopy(sp._ai_state["picks"])
    sp._ai_state["universe"]["source"] = "cached"  # 當天沒抓到 00952，用的是上一份
    monkeypatch.setattr(sp, "_ai_fetch_due", lambda today, now: True)  # 現在又可以抓了
    del sp._ai_chart_cache["1102"]
    rig.yahoo.series["1102.TW"] = None
    _next_round(clock)
    rig.run()
    assert sp._ai_state["fail_count"] == 0 and sp._ai_state["picks"] == good and sp._ai_state["t"] == T.isoformat()
    assert sp._ai_status_fields(sp._ai_state)["ai_status"] == "ok"
    assert sp._ai_next_try == pytest.approx(clock.wall + sp.AI_RETRY_S)


# ── N1：AI 的計時器醒來時只跑 AI 段，不多打證交所 ──────────────────────────────
class _TimerWake(threading.Event):
    """依序回傳 results 當 wait 的結果（False＝逾時、True＝被叫醒）；用完就結束迴圈。假時鐘跟著走。"""

    def __init__(self, stop, clock, results):
        super().__init__()
        self.stop, self.clock, self.results, self.waits = stop, clock, list(results), []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.clock.advance(timeout)
        result = self.results.pop(0)
        if not self.results:
            self.stop.set()
        return result


def _run_with_wakes(monkeypatch, clock, results):
    stop, ticks, ai_calls = threading.Event(), [], []
    wake = _TimerWake(stop, clock, results)
    monkeypatch.setattr(sp, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(sp, "_AI_BACKGROUND_ENABLED", True)
    monkeypatch.setattr(sp, "_tick", lambda _: ticks.append(clock.time()) or 1800.0)

    def hook(_):
        ai_calls.append(clock.time())
        sp._ai_next_try = clock.time() + sp.AI_RETRY_S  # AI 還在等：20 分鐘後再試

    monkeypatch.setattr(sp, "_maybe_fill_ai", hook)
    sp._run(stop, wake)
    return wake, ticks, ai_calls


def test_ai_timer_wake_runs_only_the_ai_step(monkeypatch, clock):
    start = clock.wall
    wake, ticks, ai_calls = _run_with_wakes(monkeypatch, clock, [False, True])
    assert ticks == [start]  # 主流程 30 分鐘一次：20 分鐘那次只是 AI 計時器
    assert ai_calls == [start, start + sp.AI_RETRY_S]
    assert wake.waits == [pytest.approx(sp.AI_RETRY_S), pytest.approx(1800.0 - sp.AI_RETRY_S)]


def test_an_explicit_wake_still_runs_the_main_tick(monkeypatch, clock):
    wake, ticks, ai_calls = _run_with_wakes(monkeypatch, clock, [True, True])
    assert len(ticks) == 2 and len(ai_calls) == 2


def test_main_tick_runs_again_when_its_own_time_comes(monkeypatch, clock):
    start = clock.wall
    wake, ticks, ai_calls = _run_with_wakes(monkeypatch, clock, [False, False, False, True])
    assert ticks == [start, start + 1800.0]  # 第 3 次醒來時剛好 30 分鐘：主流程到期，照跑
    assert len(ai_calls) == 4
