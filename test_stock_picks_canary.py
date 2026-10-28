"""Yahoo 日曆抽查與共用冷卻：只用合成行情，不連網。"""

import threading
from datetime import date

import pytest

import stock_picks as sp
from test_stock_picks_data import CAL, T, FakeNet, _business_days, _chart, _day_rows


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()
    monkeypatch.setattr(sp, "_get_json", fake)
    monkeypatch.setattr(sp, "T86_SPACING_S", 0.0)
    monkeypatch.setattr(sp, "_MIN_ROWS", 50)
    monkeypatch.setattr(sp, "_yahoo_blocked_until", 0.0, raising=False)
    return fake


@pytest.mark.parametrize("missing", ["index", "canary"])
def test_calendar_hole_stops_before_batch_or_bulk(net, missing):
    days = CAL[:-2] + ([T] if missing == "index" else [])
    symbol = "^TWII" if missing == "index" else "2330.TW"
    net.charts[symbol] = _chart(days, [20000.0 if missing == "index" else 100.0] * len(days))
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    assert caught.value.kind == "calendar_hole"
    assert caught.value.not_yet and not caught.value.blocked
    assert net.count("2330.TW") == 1
    assert net.count("t187ap03_L") == net.count("1101.TW") == 0


@pytest.mark.parametrize("splice", [False, True])
def test_valid_canary_is_reused_and_fmtqik_budget_unchanged(net, splice):
    if splice:
        net.charts["^TWII"] = _chart(CAL[:-1], [20000.0] * (len(CAL) - 1))
    result = sp.refresh(T, _day_rows(net), threading.Event())
    assert result.t == T
    assert net.count("2330.TW") == 1
    assert net.count("FMTQIK") == int(splice)


@pytest.mark.parametrize("failure", ["status", "rate_limited"])
def test_canary_fetch_failure_is_not_yet(net, failure):
    net.overrides["2330.TW"] = sp.FetchError(failure)
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    assert caught.value.not_yet and not caught.value.blocked
    assert net.count("t187ap03_L") == 0
    assert sp._yahoo_blocked() == (failure == "rate_limited")


def test_spliced_calendar_also_checks_canary(net):
    net.charts["^TWII"] = _chart(CAL[:-1], [20000.0] * (len(CAL) - 1))
    net.charts["2330.TW"] = _chart(CAL[:-2], [100.0] * (len(CAL) - 2))
    with pytest.raises(sp.RoundFailed, match="calendar_hole"):
        sp.refresh(T, _day_rows(net), threading.Event())
    assert net.count("FMTQIK") == net.count("2330.TW") == 1


@pytest.mark.parametrize("t", [date(2026, 10, 1), date(2027, 1, 4)])
def test_t_present_month_and_year_boundary_canary(net, monkeypatch, t):
    days = _business_days(300, t)
    if t.month == 1:
        days.remove(date(2027, 1, 1))  # 合成元旦休市
    net.charts["^TWII"] = _chart(days[:-2] + [t], [20000.0] * (len(days) - 1))
    net.charts["2330.TW"] = _chart(days[:-1], [100.0] * (len(days) - 1))
    with pytest.raises(sp.RoundFailed, match="calendar_hole") as caught:
        sp.refresh(t, {}, threading.Event())
    assert caught.value.not_yet
    assert net.count("FMTQIK") == 0


def test_shared_cooldown_wait_does_not_count_as_main_failure(net, monkeypatch):
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    sp._block_yahoo()
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, {}, threading.Event())
    err = caught.value
    assert err.kind == "yahoo_cooldown" and err.not_yet
    sp._fail(err.kind, not_yet=err.not_yet, retry_s=err.retry_s)
    assert sp._worker.fail_count == 0
    assert net.calls == []


def test_main_429_restores_shared_cooldown_from_worker(net, monkeypatch):
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    sp._fail("yahoo_rate_limited", blocked=True)
    assert sp._yahoo_blocked_until == 1000.0 + sp.BLOCKED_BACKOFF_S
    monkeypatch.setattr(sp, "_yahoo_blocked_until", 0.0)
    monkeypatch.setattr(sp, "_worker", sp.load_worker_file())
    assert sp._yahoo_blocked()
    with pytest.raises(sp.FetchError, match="cooldown"):
        sp._fetch_chart("2330.TW")
    assert net.calls == []
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0 + sp.BLOCKED_BACKOFF_S)
    assert not sp._yahoo_blocked()


def test_canary_429_wait_restores_cooldown_without_failure(net, monkeypatch):
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    net.overrides["2330.TW"] = sp.FetchError("rate_limited")
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    err = caught.value
    sp._fail(err.kind, not_yet=err.not_yet, retry_s=err.retry_s)
    assert sp._worker.fail_count == 0 and sp._worker.next_try == 2200.0
    monkeypatch.setattr(sp, "_yahoo_blocked_until", 0.0)
    monkeypatch.setattr(sp, "_worker", sp.load_worker_file())
    assert sp._yahoo_blocked()


def test_gray_log_reason_matches_missing_previous_bar(net, caplog):
    net.charts["1101.TW"] = _chart(CAL[:-2], [100.0] * (len(CAL) - 2))
    result = sp.refresh(T, _day_rows(net), threading.Event())
    assert result.pool["1101"].trend is None
    assert "gray code=1101 reason=previous_day_missing" in caplog.text
    assert "gray count=1 total=50" in caplog.text


# ── 補：抽查失敗的各種樣子，以及它們怎麼進主 worker ─────────────────────────────
def test_canary_chart_unavailable_on_both_hosts_is_not_yet(net):
    del net.charts["2330.TW"]  # 兩個主機都沒有這檔：抽查沒做成，當作還沒出來
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    assert caught.value.kind == "canary_unavailable" and caught.value.not_yet and not caught.value.blocked
    assert net.count("t187ap03_L") == 0 and not sp._yahoo_blocked()


def test_stop_during_the_canary_is_a_clean_stop(net):
    net.overrides["2330.TW"] = sp.FetchError("stopped")
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    assert caught.value.kind == "stopped" and not caught.value.not_yet


def test_calendar_hole_is_a_wait_not_a_failure(net, monkeypatch):
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    net.charts["2330.TW"] = _chart(CAL[:-2], [100.0] * (len(CAL) - 2))
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    err = caught.value
    delay = sp._fail(err.kind, not_yet=err.not_yet, retry_s=err.retry_s)
    assert delay == sp.NOT_YET_RETRY_S == 900  # 15 分鐘後再看，不加失敗次數
    assert sp._worker.fail_count == 0 and sp._worker.last_fail == "" and sp._worker.last_wait == "calendar_hole"
    assert sp.status_summary()["waiting"] == "calendar_hole" and not sp._yahoo_blocked()


def test_pool_429_sets_the_shared_cooldown_and_stays_a_real_failure(net):
    net.overrides["1101.TW"] = sp.FetchError("rate_limited")
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    assert caught.value.kind == "yahoo_rate_limited" and caught.value.blocked and not caught.value.not_yet
    assert sp._yahoo_blocked() and sp._yahoo_blocked_until > 0


def test_pool_fetch_after_a_cooldown_is_a_wait_not_a_failure(net, monkeypatch):
    original = net.__call__

    def block_after_the_canary(host, path, **kwargs):
        result = original(host, path, **kwargs)
        if path.endswith("2330.TW"):
            sp._block_yahoo()  # 抽查之後 AI 段（另一個執行緒）剛好撞到 429
        return result

    monkeypatch.setattr(sp, "_get_json", block_after_the_canary)
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(T, _day_rows(net), threading.Event())
    assert caught.value.kind == "yahoo_cooldown" and caught.value.not_yet and not caught.value.blocked


@pytest.mark.parametrize("kind,expected", [
    ("flat", None), ("prev_missing", "previous_day_missing"), ("short", "bars_short"),
    ("jump", "price_jump"), ("tbar", "close_mismatch"), ("prev_close", "previous_close_mismatch"),
])
def test_gray_reasons_follow_the_order_window_closes_checks(kind, expected):
    days = list(CAL[:-1])
    closes = [100.0] * len(days)
    t_close, change = 100.0, 0.0
    if kind == "prev_missing":
        days, closes = days[:-1], closes[:-1]
    elif kind == "short":
        days, closes = days[-100:], closes[-100:]
    elif kind == "jump":
        closes = [300.0] * 120 + [100.0] * (len(days) - 120)
    elif kind == "tbar":
        days, closes = days + [T], closes + [110.0]
    elif kind == "prev_close":
        change = -30.0  # 官方前一日參考價 130，Yahoo 前一天 100
    bars = dict(zip(days, closes))
    prev = sp._prev_reference(t_close, change)
    windows = sp.window_closes(bars, CAL, T, t_close, prev)
    assert (windows is None) == (expected is not None)
    if expected is not None:
        assert sp._window_unavailable_reason(bars, CAL, T, t_close, prev) == expected
    assert sp._window_unavailable_reason(bars, CAL[-100:], T, t_close, prev) == "calendar_short"
    assert sp._window_unavailable_reason(bars, CAL, T, float("nan"), prev) in ("close_invalid", "calendar_short")


@pytest.mark.parametrize("t", [date(2026, 10, 1), date(2027, 1, 4)])
def test_month_and_year_start_with_matching_series_pass_the_canary(net, t):
    days = _business_days(300, t)
    if t.month == 1:
        days.remove(date(2027, 1, 1))  # 合成元旦休市：前一個交易日是 12/31
    net.charts["^TWII"] = _chart(days, [20000.0] * len(days))
    net.charts["2330.TW"] = _chart(days[:-1], [100.0] * (len(days) - 1))
    with pytest.raises(sp.RoundFailed) as caught:
        sp.refresh(t, {}, threading.Event())
    assert caught.value.kind in ("pe_not_t", "pe_bad")  # 合成市場的本益比是 10/08 的：走到這裡表示抽查過了
    assert net.count("t187ap03_L") == 1 and net.count("FMTQIK") == 0


# ── 計畫 C 事後審查修正（2026-10-28）：共用冷卻的結束時間、旗標關掉時回到 2eb85e1 ───────────────
import json  # noqa: E402
import time  # noqa: E402


@pytest.mark.parametrize("kind", ["yahoo_cooldown", "canary_cooldown"])
def test_waiting_for_the_shared_cooldown_never_ends_before_it_does(monkeypatch, kind):
    """GP1 S9／N4：等別人設的共用冷卻（AI 段的 429）時，下次嘗試不早於冷卻結束；一般的 15 分鐘等待不夠長。"""
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    sp._block_yahoo()  # AI 段撞到 429：冷卻到 2200
    delay = sp._fail(kind, not_yet=True)
    assert sp._worker.next_try == 2200.0 and delay == 1200.0
    assert sp._worker.fail_count == 0 and sp._worker.last_fail == "" and sp._worker.last_wait == kind
    assert sp._worker.yahoo_blocked_until is None  # AI 段的 429 不寫進主 worker（只有主流程自己的 429 才寫）


def test_other_waits_are_not_stretched_to_the_cooldown(monkeypatch):
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    sp._block_yahoo()
    assert sp._fail("t86_not_yet", not_yet=True) == sp.NOT_YET_RETRY_S == 900.0


def test_main_429_writes_an_explicit_block_end_and_not_yet_waits_do_not_extend_it(monkeypatch):
    """GP2 #3：以前用 last_fail=="yahoo_rate_limited" 加 next_try 推冷卻；之後的「還沒出來」把 next_try 往後推，
    就會一直被當成冷卻中（單檔查詢跟著暫時抓不到）。現在冷卻結束時間明確存在 worker 檔裡。"""
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    sp._fail("yahoo_rate_limited", blocked=True)
    assert sp._worker.yahoo_blocked_until == 2200.0
    raw = json.loads((sp.STATE_DIR / sp.WORKER_NAME).read_text(encoding="utf-8"))
    assert raw["yahoo_blocked_until"] == 2200.0 and raw["schema"] == sp.WORKER_SCHEMA == 2  # schema 不升：T86 請求帳要留
    monkeypatch.setattr(sp.time, "time", lambda: 2100.0)
    sp._fail("pe_not_t", not_yet=True)  # next_try 被推到 3000，429 的冷卻在 2200 就結束了
    monkeypatch.setattr(sp, "_yahoo_blocked_until", 0.0)
    monkeypatch.setattr(sp, "_worker", sp.load_worker_file())  # 重啟
    monkeypatch.setattr(sp.time, "time", lambda: 2300.0)
    assert sp._worker.last_fail == "yahoo_rate_limited" and sp._worker.last_wait == "pe_not_t" and sp._worker.next_try == 3000.0
    assert not sp._yahoo_blocked()
    monkeypatch.setattr(sp.time, "time", lambda: 2150.0)
    assert sp._yahoo_blocked()  # 冷卻還沒結束時重啟後仍在


def test_files_written_before_the_field_existed_still_restore_a_real_429(monkeypatch):
    legacy = sp.worker_state_to_json(sp.WorkerState(last_fail="yahoo_rate_limited", next_try=5000.0))
    del legacy["yahoo_blocked_until"]  # 2eb85e1 寫的檔沒有這個鍵
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, legacy)
    monkeypatch.setattr(sp.time, "time", lambda: 4000.0)
    monkeypatch.setattr(sp, "_worker", sp.load_worker_file())
    assert sp._worker.yahoo_blocked_until is None and sp._yahoo_blocked()  # 最後失敗是 429、沒在等資料源：用 next_try 推
    sp._worker.last_wait = "t86_not_yet"  # 在等資料源：next_try 是等待，不是冷卻
    assert not sp._yahoo_blocked()
    monkeypatch.setattr(sp.time, "time", lambda: 5000.0)
    sp._worker.last_wait = ""
    assert not sp._yahoo_blocked()


@pytest.mark.parametrize("value", ["soon", True, -5, [1], {"x": 1}, float("nan"), float("inf")])
def test_a_garbage_block_end_never_makes_the_worker_file_unreadable(value):
    raw = sp.worker_state_to_json(sp.WorkerState(fail_count=2, t86_requests=[time.time() - 60]))
    raw["yahoo_blocked_until"] = value
    loaded = sp.worker_state_from_json(json.loads(json.dumps(raw)))
    assert loaded.yahoo_blocked_until is None and loaded.fail_count == 2 and len(loaded.t86_requests) == 1


def test_a_far_future_block_end_is_clamped():
    raw = sp.worker_state_to_json(sp.WorkerState())
    raw["yahoo_blocked_until"] = 1e12
    loaded = sp.worker_state_from_json(raw)
    assert loaded.yahoo_blocked_until <= time.time() + sp.BLOCKED_BACKOFF_S + sp._CLOCK_SKEW_S + 1


def test_another_process_cooldown_is_merged_when_saving(monkeypatch):
    other = sp.WorkerState(writer="another-process", yahoo_blocked_until=time.time() + 600)
    sp._write_json_file(sp.STATE_DIR / sp.WORKER_NAME, sp.worker_state_to_json(other))
    with sp._lock:
        sp._save_worker()
    assert sp._worker.yahoo_blocked_until == other.yahoo_blocked_until and sp._yahoo_blocked()


def test_flag_off_has_no_canary_and_no_shared_cooldown(net, monkeypatch):
    """GP2 #4：LINE_BOT_STOCK_PICKS_AI=0 時回到 2eb85e1——不抽查日曆、沒有共用冷卻。"""
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    sp._block_yahoo()
    assert sp._yahoo_blocked_until == 0.0 and not sp._yahoo_blocked()
    net.charts["2330.TW"] = _chart(CAL[:-2], [100.0] * (len(CAL) - 2))  # 開著時這是 calendar_hole
    result = sp.refresh(T, _day_rows(net), threading.Event())
    assert result.t == T and net.count("2330.TW") == 1 and result.pool["2330"].trend is None
    assert sp._fail("yahoo_rate_limited", blocked=True) == sp.BLOCKED_BACKOFF_S
    assert sp._worker.yahoo_blocked_until is None and sp._yahoo_blocked_until == 0.0 and not sp._yahoo_blocked()
    monkeypatch.setattr(sp, "_worker", sp.WorkerState())
    monkeypatch.setattr(sp, "_yahoo_blocked_until", 5000.0)  # 就算記憶體裡還留著開著時設的冷卻
    assert sp._fail("yahoo_cooldown", not_yet=True) == sp.NOT_YET_RETRY_S  # 也不延長到冷卻結束


def test_flag_off_chart_fetch_ignores_a_cooldown_in_memory_or_in_the_file(net, monkeypatch):
    monkeypatch.setattr(sp.time, "time", lambda: 1000.0)
    sp._fail("yahoo_rate_limited", blocked=True)  # 開著時寫下的冷卻
    monkeypatch.setattr(sp, "AI_ENABLED", False)
    assert not sp._yahoo_blocked()
    assert sp._fetch_chart("2330.TW") is not None and net.count("2330.TW") == 1
