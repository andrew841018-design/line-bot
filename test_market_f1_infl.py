"""批 1 審查後修正 F1（資料／安全層）的通膨部分（2026-10-28）：F1-1 BLS 回「未來月份」、F1-3 CPI 的 last_probe 記在未來。

審查原文：reviews/w1_review_gp2.md 的 C1、N4；reviews/w1_review_seat3.md 的 nit4。判定：reviews/w1_phase4_triage.md 的 F1-1、F1-3。
這個檔案單獨一份（不和別的修正軌改同一個測試檔）；只借 test_market_pipeline_infl／test_market_publish_infl 的 helper 與
fixture，不借它們的 test_* 函式（借了會被收集第二次）。每個測試都要在 w1-int（4e984e8）上失敗、修正後通過。
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import market_calc
import market_pipeline_infl as pipe
import market_pipeline_us
from test_market_pipeline_infl import (  # noqa: F401  env 是 fixture（用名字被 pytest 找到）
    CPI_TASK,
    PROBE,
    at,
    bls_response,
    cpi_state,
    ctx_at,
    env,
    run,
    saved_values,
    seed_cpi,
    stored_months,
)
from test_market_publish_infl import (  # noqa: F401  world 是 fixture
    NOW as PUBLISH_NOW,
    _seed_other_data,
    cpi_payload,
    cpi_series,
    world,
)

ET = ZoneInfo("America/New_York")
FUTURE = "2099-12"
FIRST_POLL = at(2026, 10, 14, 8, 30)  # 週三 08:30 美東：8–20 號的窗口裡，期待 2026-09、上限 2026-10


def poison(env, *, series_ids=market_calc.CPI_SERIES, month=FUTURE) -> None:
    """把已存的 obs 改成「某幾個序列帶著一個未來月份」（模擬舊程式或壞掉的回應已經存進去）。"""
    got = env.store.get_obs(pipe.OBS_INFL_CPI)
    payload = copy.deepcopy(got["payload"])
    for series_id in series_ids:
        payload["series"][series_id][month] = 340.0
    env.store.put_obs(pipe.OBS_INFL_CPI, payload, data_date=f"{month}-28", period=month)


def response_with(series_id: str, extra_month: str, through: str = "2026-09") -> dict:
    return bls_response(series_id, {**saved_values(series_id, through), extra_month: "340.000"})


# ═══ F1-1：collect 拒收未來月份 ═════════════════════════════════════════════════════════════════════════


def test_a_probe_response_with_a_future_month_is_refused_and_nothing_is_written(env):
    """gp2 C1 的觸發條件：某一次 200 回應含一列晚於現在的月份。以前照收，之後每輪都是 conflict:regress。"""
    seed_cpi(env, "2026-08")
    before = env.store.get_obs(pipe.OBS_INFL_CPI)
    env.http.bls[PROBE] = response_with(PROBE, FUTURE)
    assert run(env, CPI_TASK, FIRST_POLL) == "date"  # ParseError("date", "future")
    assert env.http.bls_calls == [PROBE]  # 探針就停，不再抓另外三個
    after = env.store.get_obs(pipe.OBS_INFL_CPI)
    assert after["payload"] == before["payload"] and after["period"] == "2026-08" and after["error"] == "date"
    assert FUTURE not in str(after["payload"])


def test_a_follow_up_series_with_a_future_month_also_writes_nothing(env):
    seed_cpi(env, "2026-08")
    before = env.store.get_obs(pipe.OBS_INFL_CPI)
    last = market_calc.CPI_SERIES[-1]
    env.http.bls[last] = response_with(last, "2100-01")
    assert run(env, CPI_TASK, FIRST_POLL) == "date"
    assert env.http.bls_calls == list(market_calc.CPI_SERIES)  # 前三個都正常，最後一個才出事
    assert env.store.get_obs(pipe.OBS_INFL_CPI)["payload"] == before["payload"]  # 不寫半組


@pytest.mark.parametrize("extra, result, months", [
    ("2026-10", "ok", {"2026-09"}),  # 上限＝「應已公布最新月（2026-09）＋1」：提早一個月公布的容錯
    ("2026-11", "date", {"2026-08"}),
    ("2027-09", "date", {"2026-08"}),
])
def test_the_ceiling_is_the_expected_month_plus_one(env, extra, result, months):
    seed_cpi(env, "2026-08")
    env.http.bls[PROBE] = response_with(PROBE, extra)
    assert run(env, CPI_TASK, FIRST_POLL) == result
    if result == "ok":
        # 探針比別的序列多一個月：整組只算到最舊的那個（2026-09）
        assert max(stored_months(env).values()) == "2026-10" and min(stored_months(env).values()) == "2026-09"
    else:
        assert set(stored_months(env).values()) == months


@pytest.mark.parametrize("day, ceiling", [
    (date(2026, 10, 7), "2026-09"),  # 7 號：期待 8 月 → 上限 9 月
    (date(2026, 10, 8), "2026-10"),  # 8 號：期待 9 月 → 上限 10 月
    (date(2027, 1, 7), "2026-12"),
    (date(2027, 1, 8), "2027-01"),
    (date(2026, 3, 1), "2026-02"),
])
def test_the_ceiling_flips_on_the_eighth_like_the_expected_month(day, ceiling):
    assert market_calc.cpi_month_ceiling(market_calc.cpi_expected_month(day)) == ceiling


def test_the_ceiling_uses_the_eastern_day_not_the_taipei_day(env):
    """美東 10/7 23:30＝台北 10/8 11:30：上限還是 9 月（用台北日期會多放一個月）。"""
    seed_cpi(env, "2026-07")
    env.http.bls_through = "2026-09"
    env.http.bls[PROBE] = response_with(PROBE, "2026-10")
    late_on_the_seventh = datetime(2026, 10, 7, 19, 30, tzinfo=ET).timestamp()  # 週三 19:30（還在 08:30–20:00 的時段內）
    assert datetime.fromtimestamp(late_on_the_seventh, ZoneInfo("Asia/Taipei")).day == 8
    assert run(env, CPI_TASK, late_on_the_seventh) == "date"
    assert set(stored_months(env).values()) == {"2026-07"}


def test_trim_future_drops_only_months_above_the_ceiling():
    series = {sid: {"2026-08": 1.0, "2026-09": 2.0, "2026-10": 3.0} for sid in market_calc.CPI_SERIES}
    series[PROBE]["2099-12"] = 9.0
    series[PROBE]["bad"] = 5.0  # 不是月份的鍵不是這個函式的事（別的程式本來就忽略）
    trimmed, dropped = market_calc.cpi_trim_future(series, "2026-10")
    assert dropped is True and trimmed[PROBE] == {"2026-08": 1.0, "2026-09": 2.0, "2026-10": 3.0, "bad": 5.0}
    assert "2099-12" in series[PROBE]  # 不改輸入
    again, dropped = market_calc.cpi_trim_future(trimmed, "2026-10")
    assert dropped is False and again == trimmed
    assert market_calc.cpi_trim_future({}, "2026-10") == ({}, False)


# ── 已經毒化的 store：自癒 ─────────────────────────────────────────────────────────────────────────────


def test_a_poisoned_probe_series_is_ignored_and_the_next_normal_response_advances(env):
    """gp2 C1 的證據 1、2：只有探針序列帶著未來月份。以前 held＝未來月 → 每輪 conflict:regress，永遠前進不了。"""
    seed_cpi(env, "2026-08")
    poison(env, series_ids=(PROBE,))
    assert run(env, CPI_TASK, FIRST_POLL) == "ok"
    assert set(stored_months(env).values()) == {"2026-09"}
    assert FUTURE not in str(env.store.get_obs(pipe.OBS_INFL_CPI)["payload"])  # 毒化的月份也被洗掉
    assert env.http.bls_calls == list(market_calc.CPI_SERIES)


def test_all_four_series_poisoned_do_not_freeze_the_schedule(env):
    """gp2 C1 的證據 3：四個序列都帶未來月份 → cpi_stored_month ≥ expected → done，之後完全不再問。"""
    seed_cpi(env, "2026-08")
    poison(env)
    assert set(stored_months(env).values()) == {FUTURE}
    saturday = at(2026, 10, 24, 10, 0)  # 不在窗口、不在時段：只有「當成冷啟動」才會問
    assert run(env, CPI_TASK, saturday) == "ok"
    assert env.http.bls_calls == list(market_calc.CPI_SERIES)
    assert set(stored_months(env).values()) == {"2026-09"}
    assert run(env, CPI_TASK, saturday + 900) == "not_due"  # 洗乾淨以後恢復正常排程


def test_a_stray_future_month_on_an_up_to_date_set_is_rewritten_even_without_an_advance(env):
    """其餘月份都是最新的（2026-09），只有一個序列多了未來月份：沒有「前進」可以觸發寫入，也要把它洗掉。"""
    seed_cpi(env, "2026-09")
    poison(env, series_ids=(PROBE,))
    saturday = at(2026, 10, 24, 10, 0)
    assert run(env, CPI_TASK, saturday) == "ok"
    payload = env.store.get_obs(pipe.OBS_INFL_CPI)["payload"]
    assert FUTURE not in str(payload) and set(stored_months(env).values()) == {"2026-09"}
    assert env.http.bls_calls == list(market_calc.CPI_SERIES)


def test_the_outcome_does_not_report_a_future_month_as_done(env):
    from jobs import market_snapshot as job

    seed_cpi(env, "2026-09")
    now = at(2026, 10, 24, 10, 0)
    assert job._infl_status(env.store, now) == {"infl_cpi_month": "2026-09", "infl_cpi_lag_months": 0, "infl_bls_poll": "done"}
    poison(env)
    assert job._infl_status(env.store, now) == {"infl_cpi_month": "2026-09", "infl_cpi_lag_months": 0, "infl_bls_poll": "pending"}
    seed_cpi(env, "2026-08")
    poison(env, series_ids=(PROBE,))
    out = job._infl_status(env.store, now)
    assert out["infl_cpi_month"] == "2026-08" and out["infl_cpi_lag_months"] == 1 and out["infl_bls_poll"] == "pending"


# ── publish：快照不能發出一列「新鮮」的 2099 年資料 ─────────────────────────────────────────────────────


def test_publish_leaves_the_cpi_row_missing_when_any_series_has_a_future_month(world):
    series = cpi_series()
    series[market_calc.CPI_SERIES[1]][FUTURE] = 340.0  # 不是探針：每個序列都要看
    world.store.put_obs("us.infl.cpi", cpi_payload(series), data_date=f"{FUTURE}-28", period=FUTURE)
    _seed_other_data(world.store)
    assert world.row("us.infl.cpi")["state"] == "never"
    outcome = world.publish()
    assert outcome["kind"] == "ok" and outcome["row_errors"] == {"us.infl.cpi": "bad:cpi_future"}
    snapshot = world.snapshot()
    assert snapshot["rows"]["us.infl.cpi"]["state"] == "never" and snapshot["rows"]["macro.UST10Y"]["state"] == "ok"
    assert market_calc.row_ok("us.infl.cpi", snapshot["rows"]["us.infl.cpi"])


def test_publish_accepts_a_month_up_to_the_ceiling_and_uses_the_eastern_date(world):
    early = {sid: dict(points, **{"2026-10": points["2026-09"] * 1.002}) for sid, points in cpi_series().items()}
    world.store.put_obs("us.infl.cpi", cpi_payload(early), data_date="2026-10-31", period="2026-10")
    row = world.row("us.infl.cpi", PUBLISH_NOW)  # 美東 10/22：上限 2026-10
    assert row["state"] == "ok" and row["period"] == "2026-10"
    on_the_seventh = datetime(2026, 10, 7, 23, 30, tzinfo=ET).timestamp()  # 台北已經是 10/8，美東還是 10/7：上限 2026-09
    assert world.row("us.infl.cpi", on_the_seventh)["state"] == "never"
    on_the_eighth = datetime(2026, 10, 8, 0, 30, tzinfo=ET).timestamp()
    assert world.row("us.infl.cpi", on_the_eighth)["state"] == "ok"


# ═══ F1-3：last_probe 記在未來（時鐘倒退）不能卡住輪詢 ══════════════════════════════════════════════════


def et(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


@pytest.mark.parametrize("ahead", [60.0, 30 * 86400.0, 5 * 365 * 86400.0])
def test_a_probe_stamp_in_the_future_counts_as_never_probed(ahead):
    now = et(2026, 10, 14, 10, 30)  # 窗口內的平日
    stamp = now.timestamp() + ahead
    assert market_calc.cpi_poll(now, "2026-08", stamp, enabled=True) == market_calc.cpi_poll(now, "2026-08", None, enabled=True)
    assert market_calc.cpi_poll(now, "2026-08", stamp, enabled=True).due is True
    # 冷啟動的每天一次也一樣
    assert market_calc.cpi_poll(et(2026, 10, 24, 3, 0), None, et(2026, 10, 24, 3, 0).timestamp() + ahead, enabled=True).due is True


def test_a_probe_stamp_equal_to_now_is_still_a_recent_probe():
    now = et(2026, 10, 14, 10, 30)
    assert market_calc.cpi_poll(now, "2026-08", now.timestamp(), enabled=True).due is False
    assert market_calc.cpi_poll(now, "2026-08", now.timestamp() - 7200, enabled=True).due is True


def test_the_task_asks_again_when_the_stored_probe_time_is_in_the_future(env):
    seed_cpi(env, "2026-08")
    env.http.bls_through = "2026-08"  # BLS 還沒公布 9 月：這一輪只是探針、沒有進展
    ctx = ctx_at(env, FIRST_POLL)
    market_pipeline_us._save_state(ctx, pipe.CPI_KEY, {"last_probe": FIRST_POLL + 30 * 86400.0})  # 時鐘曾經往前跳
    assert run(env, CPI_TASK, FIRST_POLL) == "not_yet"
    assert env.http.bls_calls == [PROBE]
    assert cpi_state(env, FIRST_POLL)["last_probe"] == FIRST_POLL  # 記回正確的時間，之後照 2 小時的規則
    assert run(env, CPI_TASK, FIRST_POLL + 7199) == "not_due"
