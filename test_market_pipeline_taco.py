"""market_pipeline_taco（川普壓力指數的 collect 任務 `macro.taco`，2026-10-24）的測試。

假的 http（或 market_http.fetch＋換掉的 safe_fetch）、暫存的 Store 與帳本、固定的日曆、注入的時間；不連網。
時間線（2026-10 美東是 EDT＝UTC−4，台北＝UTC+8，所以台北時間 = 美東 + 12 小時）：週四 10/22 收盤 16:00 美東＝台北週五
04:00，D（收盤＋30 分鐘）＝04:30，再加 4 小時＝台北週五 08:30，站上「應該已經有」10/22 的值。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest

import market_calendar
import market_http
import market_paths
import market_pipeline_taco as pipe
import market_sources_taco as src
import market_store
import market_tasks
from test_market_taco_fixture import (
    CLOUDFLARE, HISTORY, METHOD, assert_no_source_text, imported_modules, make_bytes, make_data)

TPE = ZoneInfo("Asia/Taipei")
HERE = Path(__file__).resolve().parent
URL = "https://ocmacro.com/dashboard/trump"
# fixture 的數列（合成的，見 test_market_taco_fixture）只到 10/22；後面的交易日也是合成的（週五 10/23、週一 10/26、週二 10/27）
EXTRA = ((date(2026, 10, 23), 9.2), (date(2026, 10, 26), 9.4), (date(2026, 10, 27), 9.6))
ALL_ROWS = HISTORY + EXTRA


def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> float:
    return datetime(y, m, d, hh, mm, ss, tzinfo=TPE).timestamp()


READY = at(2026, 10, 23, 8, 30)  # 站上應該已經有 10/22


def page_until(last: date, **index) -> bytes:
    """到 last（含）為止的數列；最後一筆是資料日。"""
    rows = tuple(row for row in ALL_ROWS if row[0] <= last)
    return make_bytes(make_data(index={"asOfDate": rows[-1][0].isoformat(), "value": rows[-1][1], **index}, history=rows))


class Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeHttp:
    """照 market_http.fetch 的樣子：先在帳本預扣，再回佇列裡的下一個（bytes 或例外）；佇列空了回 default。"""

    FetchError = market_http.FetchError

    def __init__(self, default: bytes | None = None) -> None:
        self.default = default
        self.queue: list = []
        self.calls: list[dict] = []

    def fetch(self, url, *, max_bytes, timeout=15.0, accept=("json",), headers=None, ledger=None):
        if ledger is not None:
            ledger.reserve(urlsplit(url).hostname)
        self.calls.append({"url": url, "max_bytes": max_bytes, "timeout": timeout, "accept": accept, "headers": headers})
        item = self.queue.pop(0) if self.queue else self.default
        if isinstance(item, BaseException):
            raise item
        if item is None:
            raise AssertionError("unexpected request")
        return market_http.Response(200, item, {"content-type": "text/html; charset=utf-8"}, url)


@pytest.fixture
def env(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    clock = Clock(READY - 86400)  # 先存的資料比 run 早一天；make_ctx 會把時鐘往前撥到 now
    store = market_store.Store(state / "market_history.sqlite", now_fn=clock)
    yield SimpleNamespace(store=store, http=FakeHttp(), clock=clock, paths=market_paths.Paths.under(tmp_path))
    store.close()


def make_ctx(env: SimpleNamespace, now: float, *, http=None) -> market_tasks.Context:
    env.clock.value = max(env.clock.value, now)
    return market_tasks.Context(
        store=env.store,
        http=env.http if http is None else http,
        ledger=env.store.ledger(now_fn=env.clock, sleep_fn=env.clock.sleep),
        now=now,
        paths=env.paths,
        deadline=1e9,
        us_cal=market_calendar.us_calendar(env.store),
        tw_cal=market_calendar.Calendar("tw"),
        clock=lambda: 0.0,
    )


TASK = pipe.tasks()[0]


def run(env: SimpleNamespace, now: float, *, http=None) -> str:
    return market_tasks.run_task(make_ctx(env, now, http=http), TASK)


def is_due(env: SimpleNamespace, now: float) -> bool:
    return pipe.due(make_ctx(env, now))


def obs(env: SimpleNamespace) -> dict | None:
    return env.store.get_obs(pipe.OBS_TACO)


def pipe_state(env: SimpleNamespace) -> dict:
    return env.store.task_state(pipe.STATE_NAME)


def store_series(env: SimpleNamespace, last: date) -> None:
    """已存的資料到 last（照 run 的寫法）。"""
    rows = tuple(row for row in ALL_ROWS if row[0] <= last)
    series = src.parse_taco(make_bytes(make_data(index={"asOfDate": last.isoformat(), "value": rows[-1][1]}, history=rows)))
    env.store.put_obs(pipe.OBS_TACO, pipe.payload_of(series), data_date=last.isoformat())


# ── 登記 ────────────────────────────────────────────────────────────────────────


def test_one_medium_task_named_macro_taco():
    (task,) = pipe.tasks()
    assert (task.name, task.group, task.market, task.budget_s) == ("macro.taco", "medium", "other", 45.0)
    assert [t.name for t in market_tasks.registered() if t.name == "macro.taco"] == ["macro.taco"]
    assert pipe.MAX_TRIES_PER_DAY == src.ENDPOINTS["taco"].per_day == 6  # 來源端點表的每日預算和任務的上限是同一個數字
    assert (pipe.RETRY_S, pipe.D_DELAY_S, pipe.COOLDOWN_S) == (7200.0, 4 * 3600.0, market_http.COOLDOWN_SECONDS)


def test_the_job_loads_the_module_and_a_broken_source_only_disables_this_task():
    from jobs import market_snapshot

    assert "market_pipeline_taco" in market_snapshot.PIPELINES
    assert market_snapshot.PIPELINES.index("market_pipeline_taco") > market_snapshot.PIPELINES.index("market_pipeline_misc")
    code = f"""
import sys
sys.path.insert(0, {str(HERE)!r})
sys.modules["market_sources_taco"] = None  # import 會丟 ModuleNotFoundError：模擬來源模組壞掉
import market_tasks
status = market_tasks.load_modules(["market_pipeline_taco", "market_pipeline_misc"])
names = sorted(task.name for task in market_tasks.registered() if task.name.startswith("macro."))
print(status["market_pipeline_taco"], status["market_pipeline_misc"], names)
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip() == "import_error:ModuleNotFoundError ok ['macro.yahoo']"  # taco 的任務沒登記、misc 照常


def test_the_pipeline_module_imports_no_other_pipeline_or_source():
    names = {name for name in imported_modules(HERE / "market_pipeline_taco.py") if name.startswith("market_")}
    assert names == {"market_calendar", "market_calc", "market_sources_taco", "market_tasks"}


# ── 什麼時候到期 ────────────────────────────────────────────────────────────────


def test_expected_day_switches_at_close_plus_four_and_a_half_hours(env):
    for now, expected in (
        (at(2026, 10, 23, 8, 29, 59), date(2026, 10, 21)),  # 美東週四 16:29:59：10/22 還沒到 D
        (at(2026, 10, 23, 8, 30), date(2026, 10, 22)),  # 收盤＋30 分鐘＋4 小時整
        (at(2026, 10, 24, 8, 29), date(2026, 10, 22)),  # 週六早上：10/23 的值還不用等
        (at(2026, 10, 24, 8, 30), date(2026, 10, 23)),
        (at(2026, 10, 27, 8, 29), date(2026, 10, 23)),  # 週一收盤要到台北週二 08:30 才算
        (at(2026, 10, 27, 8, 30), date(2026, 10, 26)),
    ):
        assert pipe.expected_d(make_ctx(env, now)) == expected, now


def test_a_calendar_that_cannot_answer_means_not_due_and_no_request(env):
    ctx = make_ctx(env, READY)
    ctx.us_cal = SimpleNamespace(market="us", is_session=lambda day: None, close_time=lambda day: None)  # 日曆說不知道
    assert pipe.due(ctx) is False
    assert pipe.run(ctx) == "not_yet" and env.http.calls == []


def test_due_follows_the_stored_date(env):
    store_series(env, date(2026, 10, 21))
    assert not is_due(env, at(2026, 10, 23, 8, 29))  # 站上還不用有 10/22
    assert is_due(env, READY)
    store_series(env, date(2026, 10, 22))
    assert not is_due(env, READY) and not is_due(env, at(2026, 10, 23, 20, 0))
    assert run(env, READY) == "not_due" and env.http.calls == []


def test_a_fresh_store_is_due_and_fetches_once(env):
    env.http.default = page_until(date(2026, 10, 22))
    assert is_due(env, READY)
    assert run(env, READY) == "ok"
    assert [call["url"] for call in env.http.calls] == [URL]
    assert not is_due(env, READY + 3 * 3600)  # 成功之後這個交易日不再抓


def test_up_to_date_data_stays_untouched_over_the_weekend_until_monday_closes(env):
    env.http.default = page_until(date(2026, 10, 23))
    assert run(env, at(2026, 10, 24, 8, 30)) == "ok"  # 週六早上：週五（10/23）的值
    assert obs(env)["data_date"] == "2026-10-23"
    for now in (at(2026, 10, 24, 10, 30), at(2026, 10, 25, 12, 0), at(2026, 10, 26, 23, 59), at(2026, 10, 27, 8, 29)):
        assert run(env, now) == "not_due", now
    assert len(env.http.calls) == 1
    env.http.default = page_until(date(2026, 10, 26))
    assert run(env, at(2026, 10, 27, 8, 30)) == "ok" and obs(env)["data_date"] == "2026-10-26"
    assert len(env.http.calls) == 2


# ── 存下來的東西 ────────────────────────────────────────────────────────────────


def test_the_obs_has_named_keys_and_only_numbers_dates_and_two_short_strings(env):
    env.http.default = page_until(date(2026, 10, 22))
    assert run(env, READY) == "ok"
    found = obs(env)
    payload = found["payload"]
    assert set(payload) == {"as_of", "value", "change20d", "status_key", "method", "history"}
    assert payload["as_of"] == "2026-10-22" and payload["value"] == 9.0005 and payload["change20d"] == -3.6
    assert payload["status_key"] == "high" and payload["method"] == "v1"  # 只存列舉（2026-10-28 F1-8）；以前是完整的方法版本字串
    assert payload["history"][0] == ["2025-03-26", 2.7065] and payload["history"][-1] == ["2026-10-22", 9.0005]
    assert len(payload["history"]) == 394 and all(len(item) == 2 for item in payload["history"])
    assert found["data_date"] == "2026-10-22" and found["fail_streak"] == 0 and found["error"] is None
    assert found["last_success_at"] == READY
    assert_no_source_text(json.dumps(payload, ensure_ascii=False), found)
    assert pipe.OBS_TACO == "macro.TACO"


def test_a_newer_series_replaces_the_stored_one_and_still_behind_d_is_retried(env):
    store_series(env, date(2026, 10, 20))
    env.http.default = page_until(date(2026, 10, 21))
    assert run(env, READY) == "ok"  # 站上更新了、但還是比 D（10/22）舊：照存
    assert obs(env)["data_date"] == "2026-10-21" and len(obs(env)["payload"]["history"]) == 393
    assert not is_due(env, READY + 3600) and is_due(env, READY + 2 * 3600)  # 2 小時後再看
    env.http.default = page_until(date(2026, 10, 22))
    assert run(env, READY + 2 * 3600) == "ok" and obs(env)["data_date"] == "2026-10-22"
    assert not is_due(env, READY + 10 * 3600)


# ── 還沒更新：2 小時再看、每個台北日最多 6 次 ───────────────────────────────────────


def test_not_yet_is_not_a_failure_and_the_site_is_checked_every_two_hours(env):
    store_series(env, date(2026, 10, 21))
    env.http.default = page_until(date(2026, 10, 21))  # 站上還是 10/21
    assert run(env, READY) == "not_yet"
    found = obs(env)
    assert found["data_date"] == "2026-10-21" and found["fail_streak"] == 0 and found["error"] is None
    assert found["checked_at"] == READY and found["last_success_at"] == READY - 86400  # 只更新 checked_at
    framework = env.store.task_state("macro.taco")
    assert framework["fail_streak"] == 0 and framework["last_kind"] == "not_yet" and framework["next_try"] is None
    # 框架第一次失敗的退避只有 15 分鐘；這裡自己擋 2 小時
    assert not is_due(env, READY + 15 * 60) and not is_due(env, READY + 2 * 3600 - 1)
    assert run(env, READY + 3600) == "not_due"
    assert is_due(env, READY + 2 * 3600)
    assert pipe_state(env)["tries"] == 1 and pipe_state(env)["last_kind"] == "not_yet"


def test_at_most_six_tries_per_taipei_day_and_the_count_resets_at_midnight(env):
    store_series(env, date(2026, 10, 21))
    env.http.default = page_until(date(2026, 10, 21))
    times = [READY + i * 2 * 3600 for i in range(6)]  # 08:30、10:30 … 18:30
    assert [run(env, t) for t in times] == ["not_yet"] * 6
    assert len(env.http.calls) == 6 and pipe_state(env)["tries"] == 6
    for now in (at(2026, 10, 23, 20, 30), at(2026, 10, 23, 22, 30), at(2026, 10, 23, 23, 59)):
        assert not is_due(env, now) and run(env, now) == "not_due", now  # 同一個台北日：上限到了
    assert len(env.http.calls) == 6
    assert is_due(env, at(2026, 10, 24, 0, 30))  # 隔天（台北）重新算；距離上次已超過 2 小時
    assert run(env, at(2026, 10, 24, 0, 30)) == "not_yet" and pipe_state(env)["tries"] == 1
    assert pipe_state(env)["day"] == "2026-10-24"


def test_a_day_the_site_skips_costs_at_most_six_requests(env):
    """站上的歷史只有「共同交易日」：債市休市的哥倫布日（週一 10/12/2026）站上沒有那一列。這裡用週一 10/26 當替身
    （站上停在週五 10/23）：任務會用掉當天的 6 次、都是 not_yet，沒有失敗、也不會多打。"""
    store_series(env, date(2026, 10, 23))
    env.http.default = page_until(date(2026, 10, 23))
    start = at(2026, 10, 27, 8, 30)  # 台北週二 08:30：週一 10/26 的值「應該已經有」
    results = [run(env, start + i * 2 * 3600) for i in range(8)]
    assert results[:6] == ["not_yet"] * 6 and results[6:] == ["not_due", "not_due"]
    assert len(env.http.calls) == 6
    assert env.store.task_state("macro.taco")["fail_streak"] == 0


def test_requests_blocked_by_the_ledger_do_not_count_toward_the_six(env, monkeypatch):
    store_series(env, date(2026, 10, 21))
    env.http.default = page_until(date(2026, 10, 21))
    monkeypatch.setitem(market_store.HOST_RULES, "ocmacro.com", market_store.HostRule(10.0, 1))
    make_ctx(env, READY).ledger.reserve("ocmacro.com")  # 今天的主機額度（模擬 1 次）已用掉
    assert run(env, READY) == "not_yet"  # 帳本說額度不夠＝沒送請求：不是失敗
    assert env.http.calls == [] and pipe_state(env).get("tries", 0) == 0
    assert env.store.task_state("macro.taco")["fail_streak"] == 0
    assert obs(env)["fail_streak"] == 0 and obs(env)["error"] is None  # 沒有 obs_checked
    assert is_due(env, READY + 60)  # 沒算一次：馬上還是到期（帳本明天才有額度）


# ── 失敗 ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("error, kind", [
    (market_http.FetchError("status"), "status"),
    (market_http.FetchError("timeout"), "timeout"),
    (market_http.FetchError("network"), "network"),
    (market_http.FetchError("content_type"), "content_type"),
    (market_http.FetchError("too_big"), "too_big"),
    (src.ParseError("format", "no_push"), "format"),
    (src.ParseError("conflict", "last_row"), "conflict"),
])
def test_source_errors_are_recorded_and_raised_to_the_framework(env, error, kind):
    env.http.queue = [error]
    assert run(env, READY) == kind
    found = obs(env)
    assert found["payload"] is None and found["error"] == kind and found["fail_streak"] == 1
    framework = env.store.task_state("macro.taco")
    assert framework["fail_streak"] == 1 and framework["last_kind"] == kind and framework["next_try"] == READY + 900
    assert pipe_state(env)["tries"] == 1 and pipe_state(env)["last_try"] == READY
    # 2 小時的間隔在 due 擋（框架的 15 分鐘退避過了也一樣）
    assert run(env, READY + 3600) == "not_due" and len(env.http.calls) == 1
    env.http.default = page_until(date(2026, 10, 22))
    assert run(env, READY + 2 * 3600) == "ok"
    assert obs(env)["error"] is None and obs(env)["fail_streak"] == 0
    assert env.store.task_state("macro.taco")["fail_streak"] == 0


def test_an_unexpected_exception_is_recorded_with_its_type_only(env):
    env.http.queue = [ZeroDivisionError("內部錯誤，帶著 高压 的文字")]
    assert run(env, READY) == "unexpected:ZeroDivisionError"
    assert obs(env)["error"] == "unexpected:ZeroDivisionError"
    assert pipe_state(env)["tries"] == 1
    assert_no_source_text(str(obs(env)), str(env.store.task_state("macro.taco")), str(pipe_state(env)))


def test_a_value_dated_after_the_last_closed_session_is_refused(env):
    env.http.default = page_until(date(2026, 10, 23))  # 台北週五 08:30 時，美股最近收完的是 10/22
    assert run(env, READY) == "date"
    assert obs(env)["payload"] is None and obs(env)["error"] == "date"
    assert pipe_state(env)["tries"] == 1
    # 隔天早上 10/23 收完了：同一份數列就收
    assert run(env, at(2026, 10, 24, 8, 30)) == "ok" and obs(env)["data_date"] == "2026-10-23"


@pytest.mark.parametrize("now, result", [(at(2026, 10, 23, 4, 29), "date"), (at(2026, 10, 23, 4, 31), "ok")])
def test_the_not_in_the_future_rule_follows_the_close_plus_half_an_hour(env, now, result):
    """直接呼叫 run（不經 due）：美東週四 16:29 美股最近收完的是 10/21，站上給 10/22 的值＝收盤前的值 → 拒收；
    16:31 之後 10/22 收完了 → 收。"""
    store_series(env, date(2026, 10, 20))
    env.http.default = page_until(date(2026, 10, 22))
    ctx = make_ctx(env, now)
    assert pipe.expected_d(ctx) == date(2026, 10, 21)  # 站上「應該已經有」的還是 10/21
    if result == "date":
        with pytest.raises(src.ParseError) as caught:
            pipe.run(ctx)
        assert (caught.value.kind, caught.value.detail) == ("date", "future")
        assert obs(env)["data_date"] == "2026-10-20"  # 沒存
    else:
        assert pipe.run(ctx) is None and obs(env)["data_date"] == "2026-10-22"


# ── 429／403、冷卻、白名單 ──────────────────────────────────────────────────────


def patch_transport(monkeypatch, state: dict):
    import safe_fetch

    hits: list[str] = []

    def fake(url, **kwargs):
        hits.append(url)
        return SimpleNamespace(status_code=state["status"], headers={"Content-Type": "text/html; charset=utf-8"},
                               content=state["body"])

    monkeypatch.setattr(safe_fetch, "fetch", fake)
    return hits


@pytest.mark.parametrize("status, kind", [(429, "rate_limited"), (403, "blocked")])
def test_429_and_403_use_the_host_cooldown_and_no_more_requests_go_out(env, monkeypatch, status, kind):
    state = {"status": status, "body": CLOUDFLARE}
    hits = patch_transport(monkeypatch, state)
    assert run(env, READY, http=market_http) == kind
    assert env.store.fetch_counts(now=READY)["ocmacro.com"]["cooldown_until"] == pytest.approx(READY + 7200)
    assert obs(env)["error"] == kind and pipe_state(env)["cooldown_until"] == pytest.approx(READY + 7200)
    # 冷卻中：不到期、也不送請求
    state["status"], state["body"] = 200, page_until(date(2026, 10, 22))
    for now, result in ((READY + 60, "backoff"), (READY + 3600, "not_due"), (READY + 7199, "not_due")):
        assert run(env, now, http=market_http) == result, now  # 框架的 15 分鐘退避、之後是任務自己的冷卻
    assert len(hits) == 1
    # 即使任務自己的紀錄不見了，帳本也不放行：run 直接得到 cooldown，沒有請求
    env.store.set_task_state(pipe.STATE_NAME, {})
    assert run(env, READY + 3600, http=market_http) == "cooldown"
    assert len(hits) == 1 and pipe_state(env)["cooldown_until"] == pytest.approx(READY + 3600 + 7200)
    # 兩小時後（帳本與任務的冷卻都過了）正常抓
    later = READY + 3600 + 7201
    assert run(env, later, http=market_http) == "ok"
    assert len(hits) == 2 and "cooldown_until" not in pipe_state(env)
    assert obs(env)["payload"]["as_of"] == "2026-10-22"


def test_the_ledger_cooldown_alone_stops_the_request(env):
    env.http.default = page_until(date(2026, 10, 22))
    make_ctx(env, READY).ledger.cooldown("ocmacro.com", 7200)  # 例如別的程序剛吃到 429
    assert run(env, READY) == "cooldown"
    assert env.http.calls == [] and pipe_state(env).get("tries", 0) == 0  # 沒送請求：不算一次
    assert not is_due(env, READY + 600) and is_due(env, READY + 7201)


def test_outside_the_whitelist_there_is_not_one_request(env, monkeypatch):
    """2026-10-28 F1-4（審查 gp2 N3）：白名單拿掉＝due 一律 False（照 BLS 的總開關），所以結果是 not_due、不再記一次 host 失敗；
    以前是任務照跑、每次回 "host"、進 outcome 的 failing。"""
    state = {"status": 200, "body": page_until(date(2026, 10, 22))}
    hits = patch_transport(monkeypatch, state)
    monkeypatch.setattr(market_http, "ALLOWED_HOSTS", market_http.ALLOWED_HOSTS - {"ocmacro.com"})
    assert run(env, READY, http=market_http) == "not_due"
    assert hits == [] and "ocmacro.com" not in env.store.fetch_counts(now=READY)  # 帳本也沒預扣
    assert pipe_state(env).get("tries", 0) == 0 and obs(env) is None


def test_the_real_transport_end_to_end_through_the_task(env, monkeypatch):
    state = {"status": 200, "body": page_until(date(2026, 10, 22))}
    hits = patch_transport(monkeypatch, state)
    assert run(env, READY, http=market_http) == "ok"
    assert hits == [URL] and env.store.fetch_counts(now=READY)["ocmacro.com"]["count"] == 1
    assert env.store.fetch_counts(now=READY)["ocmacro.com"]["cap"] == 8


# ── 壞掉的排程紀錄不能卡住任務 ──────────────────────────────────────────────────


def test_garbage_in_the_schedule_record_is_ignored(env):
    env.http.default = page_until(date(2026, 10, 22))
    env.store.set_task_state(pipe.STATE_NAME, {"day": 5, "tries": "many", "last_try": "x", "last_kind": 3,
                                               "cooldown_until": READY + 10 ** 9})
    assert is_due(env, READY)  # 比冷卻還遠的未來＝時鐘有問題，不信
    env.store.set_task_state(pipe.STATE_NAME, {"day": "2026-10-23", "tries": True, "last_try": READY + 5000})
    assert is_due(env, READY)  # 記在未來的 last_try、bool 的 tries 都不算數
    assert run(env, READY) == "ok"
    assert pipe_state(env)["tries"] == 1 and pipe_state(env)["last_try"] == READY


def test_a_stored_obs_that_is_not_a_series_counts_as_nothing_stored(env):
    env.store.put_obs(pipe.OBS_TACO, {"as_of": "2026-10-2", "value": 1.0}, data_date="2026-10-22")
    assert pipe.stored_as_of(env.store) is None
    env.store.put_obs(pipe.OBS_TACO, {"as_of": 20261022}, data_date="2026-10-22")
    assert pipe.stored_as_of(env.store) is None
    env.store.obs_checked(pipe.OBS_TACO, error_kind="format")
    assert pipe.stored_as_of(env.store) is None
    assert is_due(env, READY)


def test_the_run_rechecks_that_the_stored_date_is_still_behind(env):
    store_series(env, date(2026, 10, 22))
    assert pipe.run(make_ctx(env, READY)) == "not_yet" and env.http.calls == []


def test_older_site_data_never_replaces_newer_stored_data(env):
    store_series(env, date(2026, 10, 21))
    before = obs(env)["payload"]
    env.http.default = page_until(date(2026, 10, 20))  # 站上倒退
    assert run(env, READY) == "not_yet"
    assert obs(env)["payload"] == before and obs(env)["data_date"] == "2026-10-21"


def test_a_changed_method_version_is_stored_as_other_for_publish_to_label(env):
    """2026-10-28 F1-8（審查 gp2 N10）：以前原樣存來源給的方法版本字串；現在只存 "v1"／"other"，卡片的「方法版本已更新」照樣出得來。"""
    env.http.default = make_bytes(make_data(version="persistent-pressure-new5-v2"))
    assert run(env, READY) == "ok"
    assert obs(env)["payload"]["method"] == "other"
