"""批 1 審查後修正 F1（資料／安全層）的川普壓力指數（TACO）部分（2026-10-28）：F1-4 主機不在白名單時不到期、F1-5 傳輸層只准一條路徑、
F1-6 被擋（429／403）之後的退避升級與 taco_block_streak、F1-7 change20d 和自己的歷史交叉驗證、F1-8 collect 只存列舉、
F1-12 taco_stats 的說明寫實際行為。

審查原文：reviews/w1_review_gp2.md 的 N1、N2、N3、N7、N10；reviews/w1_review_seat3.md 的 nit3。判定：reviews/w1_phase4_triage.md。
這個檔案單獨一份；只借 test_market_pipeline_taco／test_market_sources_taco／test_market_dashboard 的 helper 與 fixture，不借它們的
test_* 函式。每個測試都要在 w1-int（4e984e8）上失敗、修正後通過；「對照」性質的測試會在註解裡說明。
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import market_calc
import market_http
import market_paths
from test_market_dashboard import NOW as DASH_NOW  # noqa: F401
from test_market_dashboard import make_snapshot, md, paths, write_snapshot  # noqa: F401  md／paths 是 fixture
from test_market_pipeline_taco import (  # noqa: F401  env 是 fixture
    READY,
    URL,
    at,
    env,
    is_due,
    obs,
    page_until,
    patch_transport,
    pipe,
    pipe_state,
    run,
    src,
    store_series,
)
from test_market_sources_taco import parse, parse_error
from test_market_taco_fixture import CLOUDFLARE, HISTORY, METHOD, make_bytes, make_data
from test_market_taco_publish import publish_taco, saved_payload, world  # noqa: F401  world 是 fixture

TPE = ZoneInfo("Asia/Taipei")
BLOCKED = market_http.FetchError("blocked")
RATE_LIMITED = market_http.FetchError("rate_limited")


# ═══ F1-4：主機不在白名單 → 不到期（照 BLS 的總開關） ═════════════════════════════════════════════════════


def test_with_the_host_off_the_whitelist_the_task_is_not_due_and_nothing_is_recorded(env, monkeypatch):
    """gp2 N3：以前拿掉白名單只會讓任務每輪都跑、每次 host 失敗，2 天 fail_streak＝27、進 outcome 的 failing。"""
    env.http.default = page_until(date(2026, 10, 22))
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", market_http.ALLOWED_HOSTS - {"ocmacro.com"})
    assert src.enabled() is False
    assert is_due(env, READY) is False
    assert run(env, READY) == "not_due"
    assert run(env, READY + 3 * 3600) == "not_due"
    assert env.http.calls == [] and "ocmacro.com" not in env.store.fetch_counts(now=READY)  # 帳本也沒預扣
    assert obs(env) is None and pipe_state(env).get("tries", 0) == 0  # 沒有失敗紀錄
    assert env.store.task_state("macro.taco").get("fail_streak", 0) == 0
    monkeypatch.undo()  # 對照：開關打開就照常跑
    assert src.enabled() is True and is_due(env, READY) is True
    assert run(env, READY) == "ok" and len(env.http.calls) == 1


# ═══ F1-5：傳輸層只准 robots.txt 明確 Allow 的那一條路徑 ══════════════════════════════════════════════════


class RecordingLedger:
    def __init__(self) -> None:
        self.reserved: list[str] = []

    def reserve(self, host: str) -> None:
        self.reserved.append(host)

    def cooldown(self, host: str, seconds: float) -> None:
        raise AssertionError("no request was sent")


def test_allowed_paths_name_exactly_the_one_robots_allowed_page():
    assert market_http.ALLOWED_PATHS == {"ocmacro.com": ("/dashboard/trump",)}
    assert set(market_http.ALLOWED_PATHS) <= market_http.ALLOWED_HOSTS
    assert market_http.host_of(src.URL) == "ocmacro.com"  # 對照：這一條本來就通
    assert market_http.host_of("https://OCMACRO.com/dashboard/trump") == "ocmacro.com"  # 主機不分大小寫，路徑分


@pytest.mark.parametrize("url", [
    "https://ocmacro.com/api/trump",
    "https://ocmacro.com/data/trump.csv",
    "https://ocmacro.com/dashboard/",
    "https://ocmacro.com/dashboard",
    "https://ocmacro.com/dashboard/other",
    "https://ocmacro.com/dashboard/trump/",  # 結尾斜線
    "https://ocmacro.com/dashboard/trumpX",  # 前綴延伸
    "https://ocmacro.com/dashboard/trump/../../api/x",
    "https://ocmacro.com/dashboard/./trump",
    "https://ocmacro.com//dashboard/trump",
    "https://ocmacro.com/dashboard/%74rump",  # 百分比編碼
    "https://ocmacro.com/dashboard/trump%2F..%2Fapi",
    "https://ocmacro.com/Dashboard/Trump",
    "https://ocmacro.com/dashboard/trump?download=1",  # 帶查詢字串
    "https://ocmacro.com/dashboard/trump;x",
    "https://ocmacro.com/",
    "https://ocmacro.com",
    "https://ocmacro.com/robots.txt",
])
def test_every_other_path_on_the_host_is_refused_before_the_ledger(url):
    assert market_http.host_of(url) is None
    ledger = RecordingLedger()
    with pytest.raises(market_http.FetchError) as caught:
        market_http.fetch(url, max_bytes=1000, ledger=ledger)
    assert caught.value.kind == "host" and ledger.reserved == []  # 沒有預扣、沒有請求


def test_other_hosts_are_not_path_restricted():
    for url in ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/all/202610",
                "https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0", "https://cdn-api.cboe.com/api/global/delayed_quotes/x.json"):
        assert market_http.host_of(url) is not None, url


def test_the_real_transport_sends_nothing_for_a_refused_path(env, monkeypatch):
    state = {"status": 200, "body": page_until(date(2026, 10, 22))}
    hits = patch_transport(monkeypatch, state)
    ledger = RecordingLedger()
    with pytest.raises(market_http.FetchError) as caught:
        market_http.fetch("https://ocmacro.com/api/trump", max_bytes=4 << 20, accept=("text/html",), ledger=ledger)
    assert caught.value.kind == "host" and hits == [] and ledger.reserved == []
    market_http.fetch(URL, max_bytes=4 << 20, accept=("text/html",), ledger=ledger)  # 對照：那一條照常
    assert hits == [URL] and ledger.reserved == ["ocmacro.com"]


# ═══ F1-6：被擋（429／403）之後不再每 2 小時敲一次 ═══════════════════════════════════════════════════════

DAY1 = READY  # 台北週五 10/23 08:30
MIDNIGHT_2 = at(2026, 10, 24, 0, 0)


def blocked_run(env, now, error=BLOCKED):
    env.http.queue = [error]
    return run(env, now)


def test_the_first_block_cools_down_two_hours_and_counts_one(env):
    assert blocked_run(env, DAY1) == "blocked"
    state = pipe_state(env)
    assert state["cooldown_until"] == pytest.approx(DAY1 + 7200) and state["block_streak"] == 1


def test_the_second_block_waits_until_the_next_taipei_midnight(env):
    """gp2 N1：以前每次都是 2 小時，被擋的站每個台北日仍敲 6 次。"""
    assert blocked_run(env, DAY1) == "blocked"
    later = DAY1 + 7200
    assert is_due(env, later - 1) is False and is_due(env, later) is True
    assert blocked_run(env, later, RATE_LIMITED) == "rate_limited"
    state = pipe_state(env)
    assert state["block_streak"] == 2 and state["cooldown_until"] == pytest.approx(MIDNIGHT_2)
    assert is_due(env, at(2026, 10, 23, 23, 59, 59)) is False and is_due(env, MIDNIGHT_2) is True
    assert state["tries"] == 2  # 兩次都真的送出請求：照舊算進當天的 6 次


def test_a_block_late_in_the_evening_still_waits_two_hours(env):
    """午夜只剩 30 分鐘時第 2 次被擋：至少還是等 2 小時（不比第一次短）。"""
    evening = at(2026, 10, 23, 23, 30)
    env.store.set_task_state(pipe.STATE_NAME, {"block_streak": 1, "block_days": 1, "block_day": "2026-10-23",
                                               "last_try": evening - 7300, "tries": 1, "day": "2026-10-23"})
    assert blocked_run(env, evening) == "blocked"
    state = pipe_state(env)
    assert state["block_streak"] == 2 and state["cooldown_until"] == pytest.approx(evening + 7200)


def test_three_blocked_days_pause_the_task_for_seven_days(env):
    assert blocked_run(env, DAY1) == "blocked" and blocked_run(env, DAY1 + 7200) == "blocked"
    assert blocked_run(env, MIDNIGHT_2) == "blocked"  # 第 2 個台北日
    day3 = at(2026, 10, 25, 0, 0)
    assert pipe_state(env)["cooldown_until"] == pytest.approx(day3) and pipe_state(env)["block_streak"] == 3
    assert blocked_run(env, day3) == "blocked"  # 第 3 個台北日：停 7 天
    state = pipe_state(env)
    assert state["block_streak"] == 4 and state["cooldown_until"] == pytest.approx(day3 + 7 * 86400)
    assert is_due(env, day3 + 7 * 86400 - 1) is False and is_due(env, day3 + 7 * 86400) is True
    assert blocked_run(env, day3 + 7 * 86400) == "blocked"  # 7 天後還是被擋：再停 7 天（不回到 2 小時）
    assert pipe_state(env)["cooldown_until"] == pytest.approx(day3 + 14 * 86400) and pipe_state(env)["block_streak"] == 5


def test_a_cooldown_up_to_the_seven_day_pause_is_honoured_but_garbage_is_not(env):
    env.http.default = page_until(date(2026, 10, 22))
    env.store.set_task_state(pipe.STATE_NAME, {"cooldown_until": READY + 7 * 86400})
    assert is_due(env, READY) is False  # 以前 ahead 只有 2 小時：比 2 小時遠的冷卻被當成時鐘問題而忽略
    env.store.set_task_state(pipe.STATE_NAME, {"cooldown_until": READY + 10 ** 9})
    assert is_due(env, READY) is True  # 離譜的未來還是不信


def test_a_good_fetch_clears_the_block_record(env):
    assert blocked_run(env, DAY1) == "blocked" and blocked_run(env, DAY1 + 7200) == "blocked"
    env.http.default = page_until(date(2026, 10, 22))
    assert run(env, MIDNIGHT_2) == "ok"
    state = pipe_state(env)
    assert not {"block_streak", "block_days", "block_day", "cooldown_until"} & set(state)
    store_series(env, date(2026, 10, 20))  # 站上之後又更新了、已存的比 D 舊；再被擋：又是「第 1 次」（2 小時）
    later = at(2026, 10, 25, 10, 0)
    assert blocked_run(env, later) == "blocked"
    assert pipe_state(env)["block_streak"] == 1 and pipe_state(env)["cooldown_until"] == pytest.approx(later + 7200)


@pytest.mark.parametrize("error, clears", [
    (market_http.FetchError("status"), True),  # 站方有回應、不是擋我們
    (market_http.FetchError("content_type"), True),
    (src.ParseError("format", "no_push"), True),
    (market_http.FetchError("timeout"), False),  # 沒有回應：不知道還有沒有被擋
    (market_http.FetchError("network"), False),
    (ZeroDivisionError("x"), False),
])
def test_only_an_answer_that_is_not_a_block_clears_the_record(env, error, clears):
    assert blocked_run(env, DAY1) == "blocked"
    assert pipe_state(env)["block_streak"] == 1
    assert blocked_run(env, DAY1 + 7201, error) != "ok"
    assert ("block_streak" not in pipe_state(env)) is clears


def test_garbage_in_the_block_fields_counts_as_no_blocks(env):
    env.store.set_task_state(pipe.STATE_NAME, {"block_streak": "many", "block_days": True, "block_day": 5, "tries": 0})
    assert blocked_run(env, DAY1) == "blocked"
    state = pipe_state(env)
    assert state["block_streak"] == 1 and state["block_days"] == 1 and state["cooldown_until"] == pytest.approx(DAY1 + 7200)


def test_a_site_that_blocks_every_day_is_asked_five_times_in_two_weeks(env, monkeypatch):
    """gp2 N1 的模擬：站方一直回 403。以前每個台北日 6 次（14 天 84 次）；現在 5 次：第 1 天 2 次、第 2、3 天各 1 次，
    連續 3 個台北日被擋後停 7 天，第 10 天問 1 次、又被擋、再停 7 天。"""
    hits = patch_transport(monkeypatch, {"status": 403, "body": CLOUDFLARE})
    per_day: dict[date, int] = {}
    now, end = DAY1, DAY1 + 14 * 86400
    while now < end:
        before = len(hits)
        run(env, now, http=market_http)
        if len(hits) > before:
            day = datetime.fromtimestamp(now, TPE).date()
            per_day[day] = per_day.get(day, 0) + 1
        now += 1800
    assert len(hits) == 5, per_day
    assert max(per_day.values()) <= 2
    assert sorted(per_day) == [date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25), date(2026, 11, 1)]
    assert pipe_state(env)["block_streak"] == 5


# ── outcome／status_summary 的 taco_block_streak ───────────────────────────────────────────────────────


def test_the_job_reads_the_pipeline_state_under_the_same_name():
    from jobs import market_snapshot as job

    assert job.TACO_STATE == pipe.STATE_NAME == "taco.pipe:macro.taco"


def test_taco_status_is_a_plain_integer(env):
    from jobs import market_snapshot as job

    assert job._taco_status(env.store) == {"taco_block_streak": 0}
    assert blocked_run(env, DAY1) == "blocked" and blocked_run(env, DAY1 + 7201) == "blocked"
    out = job._taco_status(env.store)
    assert out == {"taco_block_streak": 2} and job.sanitize(out) == out
    for bad in ("2", True, -1, 1.5, None, 10 ** 9, [2]):
        env.store.set_task_state(pipe.STATE_NAME, {"block_streak": bad})
        assert job._taco_status(env.store) == {"taco_block_streak": 0}, bad


def test_the_job_outcome_carries_the_block_streak(env):
    from jobs import market_snapshot as job

    assert blocked_run(env, DAY1) == "blocked"
    supervisor = job.Supervisor(env.paths, job.parse_args(["--root", str(env.paths.state_dir.parent)]), -1, wall=lambda: DAY1)
    outcome = supervisor.compose_outcome(code="unchecked", collect="ok", publish="ok", started_at=DAY1, locked_at=time.monotonic())
    assert outcome["taco_block_streak"] == 1 and "taco_status_error" not in outcome and job.sanitize(outcome) == outcome


def test_status_summary_reports_the_block_streak(md, paths):
    write_snapshot(paths, make_snapshot(DASH_NOW))
    base = {"status": "ok", "code": "verified", "collect": {"status": "ok"}, "publish": {"status": "ok"}}
    for value, expect in ((0, 0), (3, 3), (-1, None), ("3", None), (True, None), (1.5, None), (10 ** 9, None), (None, None)):
        market_paths.write_private_atomic(paths.outcome, json.dumps({**base, "taco_block_streak": value}).encode())
        assert md.status_summary(now=DASH_NOW, paths=paths)["outcome"]["taco_block_streak"] == expect, value
    market_paths.write_private_atomic(paths.outcome, json.dumps(base).encode())  # 舊版 job：沒有這個 key
    assert md.status_summary(now=DASH_NOW, paths=paths)["outcome"]["taco_block_streak"] is None


# ═══ F1-7：change20d 要和自己的歷史對得上 ════════════════════════════════════════════════════════════════

DELTA = HISTORY[-1][1] - HISTORY[-21][1]  # 現值減 20 個交易日前（保存的數列：−3.6）


def test_the_fixture_series_is_self_consistent():
    assert round(DELTA, 1) == -3.6
    assert parse(make_bytes()).change20d == -3.6  # 對照：原本就收


@pytest.mark.parametrize("change", [round(DELTA, 1), DELTA, DELTA + 0.1, DELTA - 0.1, DELTA + 0.05])
def test_a_change_within_a_tenth_of_the_history_difference_is_accepted(change):
    assert parse(make_bytes(make_data(index={"change20d": change}))).change20d == change


@pytest.mark.parametrize("change", [-3.0, 0.0, 3.6, DELTA + 0.11, DELTA - 0.11, DELTA + 1.0, -30.6, 150.0])
def test_a_change_that_disagrees_with_the_history_is_a_conflict(change):
    """gp2 N7：卡片「20 個交易日變化」直接印 index.change20d；來源給的數字和自己的歷史對不上時整頁不收。"""
    assert parse_error(make_bytes(make_data(index={"change20d": change}))) == ("conflict", "change")


def test_a_missing_change_is_still_accepted_and_format_errors_come_first():
    data = make_data()
    del data["index"]["change20d"]
    assert parse(make_bytes(data)).change20d is None
    assert parse_error(make_bytes(make_data(index={"change20d": "-3.6"}))) == ("format", "change")  # 型別錯：先報 format
    assert parse_error(make_bytes(make_data(index={"change20d": 200.5}))) == ("format", "change")  # 超過上限：先報 format


def test_the_extremes_are_accepted_when_the_history_agrees():
    for value, base, change in ((100.0, -100.0, 200.0), (-100.0, 100.0, -200.0)):
        data = make_data(index={"value": value, "change20d": change, "status": None})
        data["history"][-1]["value"] = value
        data["history"][-21]["value"] = base
        series = parse(make_bytes(data))
        assert series.value == value and series.change20d == change


def test_the_check_uses_the_twenty_first_row_from_the_end():
    data = make_data()
    data["history"][-21]["value"] += 1.0  # 第 −21 筆動一下：現值減它變成 −4.6，−3.6 對不上
    assert parse_error(make_bytes(data)) == ("conflict", "change")
    data = make_data()
    data["history"][-20]["value"] += 1.0  # 第 −20 筆不是比較對象
    assert parse(make_bytes(data)).change20d == -3.6


def test_a_conflicting_change_is_not_stored_by_the_task(env):
    env.http.default = make_bytes(make_data(index={"change20d": 3.6}))
    assert run(env, READY) == "conflict"
    assert obs(env)["payload"] is None and obs(env)["error"] == "conflict"


# ═══ F1-8：collect 只存列舉，不存來源給的字 ═══════════════════════════════════════════════════════════════


def test_the_stored_method_is_a_two_value_enumeration(env):
    env.http.default = make_bytes(make_data(version="persistent-pressure-new5-v2"))
    assert run(env, READY) == "ok"
    assert obs(env)["payload"]["method"] == "other"
    assert "new5" not in json.dumps(obs(env)["payload"])
    store_series(env, date(2026, 10, 21))
    env.http.default = page_until(date(2026, 10, 22))  # 目前的方法版本
    assert run(env, READY + 3 * 3600) == "ok"
    assert obs(env)["payload"]["method"] == "v1" and METHOD not in json.dumps(obs(env)["payload"])


def test_only_a_known_status_key_is_stored(env):
    for key, expect in (("high", "high"), ("brand_new_state", None), ("elevated", None)):
        data = make_data(index={"status": {"key": key}})
        env.store.set_task_state(pipe.STATE_NAME, {})
        env.http.default = make_bytes(data)
        env.store.put_obs(pipe.OBS_TACO, {}, data_date="2026-10-20")  # 站上比已存的新
        assert run(env, READY) == "ok", key
        assert obs(env)["payload"]["status_key"] == expect, key
        assert set(market_calc.TACO_STATUS_WORDS) == {"high"}


def test_payload_of_keeps_the_same_keys_and_no_source_text():
    series = parse(make_bytes(make_data(version="persistent-pressure-new5-v2", index={"status": {"key": "weird"}})))
    payload = pipe.payload_of(series)
    assert set(payload) == {"as_of", "value", "change20d", "status_key", "method", "history"}
    assert payload["method"] == "other" and payload["status_key"] is None
    assert pipe.payload_of(parse(make_bytes()))["method"] == "v1"


@pytest.mark.parametrize("stored, expect", [("v1", "v1"), (METHOD, "v1"), ("other", "other"), ("persistent-pressure-new5-v2", "other"),
                                            (None, "other"), (5, "other")])
def test_publish_reads_both_the_enumeration_and_the_old_full_string(world, stored, expect):
    """collect 現在存 "v1"／"other"；舊的 obs 存的是完整的方法版本字串：兩種都要認得（回滾、混合版本都不會錯標成 other）。"""
    outcome, row = publish_taco(world, saved_payload(method=stored))
    assert row["state"] == "ok" and row["values"]["method"] == expect and "row_errors" not in outcome


# ═══ F1-12：taco_stats 的說明寫實際行為 ═════════════════════════════════════════════════════════════════


def test_the_percentile_doc_describes_the_lowest_point_behaviour():
    doc = market_calc.taco_stats.__doc__
    assert "至少 1/n" not in doc and "第 0 百分位" in doc
    # 說明講的行為本身（以前就是這樣，這裡一併釘住）：n＝394 時目前點是歷史最低 → 1/394＝0.25 → 0
    low = [(date(2025, 1, 1) + timedelta(days=i), 1.0 + i) for i in range(393)] + [(date(2026, 3, 1), -1.0)]
    assert market_calc.taco_stats(low)["percentile"] == 0
