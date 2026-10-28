"""川普壓力指數（TACO 指數，MacroMargin，2026-10-24）在 market_calc 與 market_publish 這一段的測試：歷史位階怎麼算、
`macro.TACO` 這一列怎麼組、存下來的資料壞了怎麼辦（只有這一列暫缺）、validate_snapshot 擋型別錯的列。

位階的算法：歷史裡「≤ 最後一筆」的天數 ÷ 總天數 × 100，Decimal 四捨五入（ROUND_HALF_UP）成整數。fixture 的合成數列
（394 筆，最後一筆 9.0005）裡有 347 筆 ≤ 它：347/394 = 88.07 → 88。
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest

import market_calc
import market_publish
from test_market_publish import D, at_et, at_tpe, world  # noqa: F401  (world 是 fixture)
from test_market_taco_fixture import HISTORY, METHOD, assert_no_source_text, float_edge_case

SAVED_NOW = at_tpe(2026, 10, 24, 16, 31)  # 週六：美股最近收完的是週五 10/23，保存的資料日 10/22 還沒過期


def saved_payload(**changes) -> dict:
    """market_pipeline_taco 存的 obs（fixture 的合成數列；method 存完整字串是舊 obs 的樣子，publish 兩種都認得）。"""
    payload = {"as_of": "2026-10-22", "value": 9.0005, "change20d": -3.6, "status_key": "high", "method": METHOD,
               "history": [[day.isoformat(), value] for day, value in HISTORY]}
    payload.update(changes)
    return payload


def publish_taco(world, payload: dict, *, data_date: str = "2026-10-22", now: float = SAVED_NOW):
    world.store.put_obs("macro.TACO", payload, data_date=data_date)
    outcome = world.publish(now=now)
    snapshot = world.snapshot()
    assert outcome["kind"] == "ok", outcome  # 一列壞掉不能擋整份快照
    assert market_publish.validate_snapshot(snapshot) == [], outcome
    return outcome, snapshot["rows"]["macro.TACO"]


# ── market_calc.taco_stats ──────────────────────────────────────────────────────


def test_stats_of_the_saved_series():
    stats = market_calc.taco_stats(HISTORY)
    assert stats == {"percentile": 88, "n": 394, "since": "2025-03-26", "max": 17.3261, "max_date": "2026-03-27",
                     "min": -4.9868, "min_date": "2025-05-20"}
    current = HISTORY[-1][1]
    assert sum(1 for _day, value in HISTORY if value <= current) == 347  # 347 / 394 = 88.07 %


def series(values, last: date = date(2026, 10, 16)) -> list[tuple[date, float]]:
    return [(last - timedelta(days=len(values) - 1 - i), value) for i, value in enumerate(values)]


def test_percentile_rounds_half_up_and_counts_the_last_point_itself():
    # 200 筆：最後一筆是第 101 小（比它小的 100 筆、加它自己）→ 101/200 = 50.5 → 51（四捨五入；Python 的 round() 會給 50）
    values = [float(i) for i in range(100)] + [500.0 + i for i in range(99)] + [100.0]
    assert len(values) == 200 and sum(1 for v in values if v <= values[-1]) == 101
    assert market_calc.taco_stats(series(values))["percentile"] == 51
    # 目前點是歷史最高 → 100；全部一樣 → 100（≤）
    assert market_calc.taco_stats(series([float(i) for i in range(70)]))["percentile"] == 100
    assert market_calc.taco_stats(series([3.0] * 80))["percentile"] == 100
    # 目前點是歷史最低 → 只有它自己：1/200 = 0.5 → 1（進位）；1/394 = 0.25 → 0
    assert market_calc.taco_stats(series([1.0 + i for i in range(199)] + [-1.0]))["percentile"] == 1
    assert market_calc.taco_stats(series([1.0 + i for i in range(393)] + [-1.0]))["percentile"] == 0
    assert market_calc.taco_stats(series([1.0 + i for i in range(59)] + [-1.0]))["percentile"] == 2  # 1/60 = 1.67 → 2


def test_max_and_min_ties_take_the_earliest_day():
    values = [1.0, 5.0, 2.0, 5.0, -3.0, -3.0] + [0.0] * 60 + [1.0]
    stats = market_calc.taco_stats(series(values))
    first = series(values)
    assert stats["max"] == 5.0 and stats["max_date"] == first[1][0].isoformat()
    assert stats["min"] == -3.0 and stats["min_date"] == first[4][0].isoformat()
    assert stats["since"] == first[0][0].isoformat() and stats["n"] == len(values)


def test_stats_need_sixty_days():
    assert market_calc.taco_stats(series([1.0] * 60))["n"] == 60
    with pytest.raises(ValueError):
        market_calc.taco_stats(series([1.0] * 59))


# ── market_calc.TACO_SPEC／taco_values_ok ───────────────────────────────────────

GOOD = {"value": 9.0005, "chg20": -3.6, "percentile": 88, "n": 394, "since": "2025-03-26", "max": 17.3261,
        "max_date": "2026-03-27", "min": -4.9868, "min_date": "2025-05-20", "status_key": "high", "method": "v1"}


def test_the_row_is_in_row_ids_with_the_documented_fields():
    assert market_calc.ROW_IDS["macro.TACO"] is market_calc.TACO_SPEC
    assert set(market_calc.TACO_SPEC) == set(GOOD)
    assert market_calc.values_ok("macro.TACO", GOOD) and market_calc.taco_values_ok(GOOD)
    row = {"label": "宏觀", "values": GOOD, "data_date": "2026-10-22", "period": None, "source": "MacroMargin",
           "basis": "指數值", "stale_after": 1.0e10, "state": "ok"}
    assert market_calc.row_ok("macro.TACO", row)
    never = {**row, "state": "never", "values": {}}
    assert market_calc.row_ok("macro.TACO", never)


@pytest.mark.parametrize("change", [
    {"value": "9.0"}, {"value": None}, {"value": True}, {"value": float("nan")}, {"chg20": "x"}, {"chg20": True},
    {"percentile": "88"}, {"percentile": 88.0}, {"percentile": True}, {"n": 394.0}, {"n": None}, {"since": "2025-3-26"},
    {"since": 20250326}, {"max_date": "2026-02-30"}, {"min_date": None}, {"status_key": 5}, {"method": None},
    {"max": None}, {"min": "x"},
])
def test_the_spec_rejects_wrong_types(change):
    assert not market_calc.values_ok("macro.TACO", {**GOOD, **change})
    assert not market_calc.taco_values_ok({**GOOD, **change})


def test_the_spec_rejects_missing_fields():
    for key in GOOD:
        assert not market_calc.values_ok("macro.TACO", {k: v for k, v in GOOD.items() if k != key}), key
    assert not market_calc.taco_values_ok(None) and not market_calc.taco_values_ok([]) and not market_calc.taco_values_ok("x")


@pytest.mark.parametrize("change", [
    {"value": 100.5}, {"value": -100.5}, {"max": 101.0}, {"min": -101.0}, {"chg20": 200.5}, {"chg20": -200.5},
    {"percentile": 101}, {"percentile": -1}, {"n": 59}, {"n": 20001},
    {"min": 10.0},  # 最低比最高高
    {"value": 18.0},  # 現值在最高之上
    {"value": -5.1},  # 現值在最低之下
    {"since": "2026-03-28"},  # 起始日晚於最高那天
    {"since": "2025-05-21"},  # 起始日晚於最低那天
    {"status_key": "High"}, {"status_key": "a" * 17}, {"status_key": ""}, {"status_key": "高压"}, {"status_key": "level2"},
    {"method": "v2"}, {"method": "persistent-pressure-new4-v1"}, {"method": ""},
])
def test_the_semantic_check_rejects_values_the_spec_lets_through(change):
    assert market_calc.values_ok("macro.TACO", {**GOOD, **change})  # 型別都對
    assert not market_calc.taco_values_ok({**GOOD, **change})


def test_the_semantic_check_accepts_the_edges():
    for change in ({"value": 100.0, "max": 100.0}, {"value": -100.0, "min": -100.0}, {"chg20": None}, {"chg20": -200.0},
                   {"percentile": 0}, {"percentile": 100}, {"n": 60}, {"n": 20000}, {"status_key": None},
                   {"status_key": "a_b_c"}, {"method": "other"}, {"value": 17.3361}, {"value": -4.9968}):
        assert market_calc.taco_values_ok({**GOOD, **change}), change  # 現值可以比最高／最低多 0.01（來源自己四捨五入）


def test_the_status_word_table_only_knows_the_key_seen_in_the_saved_page():
    assert market_calc.TACO_STATUS_WORDS == {"high": "高壓"}
    assert market_calc.TEXT_TACO_STATUS_HIGH == "高壓"


# ── market_publish 的 macro.TACO ───────────────────────────────────────────────


def test_obs_key_is_the_same_on_both_sides():
    import market_pipeline_taco  # 這個測試才需要 collect 那一側；publish 本身不 import 它

    assert market_publish.OBS_TACO == market_pipeline_taco.OBS_TACO == "macro.TACO"
    assert market_publish._builder_for("macro.TACO") is not None


def test_what_the_task_stores_is_what_publish_reads(world):
    """接縫：真的 collect 側（解析保存的頁面 → payload_of）的輸出直接餵 publish，得到文件裡的那一列。"""
    import market_pipeline_taco
    import market_sources_taco
    from test_market_taco_fixture import make_bytes

    payload = market_pipeline_taco.payload_of(market_sources_taco.parse_taco(make_bytes()))
    assert payload == saved_payload(method="v1")  # collect 只存列舉（2026-10-28 F1-8）；舊的 obs 存完整字串，publish 兩種都認得
    _outcome, row = publish_taco(world, payload)
    assert row["state"] == "ok" and row["values"] == GOOD


def test_the_seeded_world_publishes_an_ok_row(world):
    """test_market_publish 的世界已經放了一份合成的 macro.TACO（讓整份快照的 never 清單不用變）。"""
    outcome = world.publish()
    assert outcome["kind"] == "ok" and "row_errors" not in outcome, outcome
    row = world.snapshot()["rows"]["macro.TACO"]
    assert row["state"] == "ok" and row["data_date"] == D.isoformat() and row["values"]["method"] == "v1"
    assert row["stale_after"] == at_et(2026, 10, 22, 16, 0)  # 和 macro.VIX 一樣：資料日之後第 4 個預期交易日收盤
    assert market_calc.taco_values_ok(row["values"])


def test_the_saved_series_makes_the_documented_row(world):
    outcome, row = publish_taco(world, saved_payload())
    assert "row_errors" not in outcome and row["state"] == "ok"
    assert row["values"] == GOOD
    assert (row["data_date"], row["source"], row["basis"]) == ("2026-10-22", "MacroMargin", "指數值")
    assert row["stale_after"] == at_et(2026, 10, 28, 16, 0)  # 10/22 之後的第 4 個預期交易日：10/23、26、27、28
    assert market_calc.row_ok("macro.TACO", row)
    assert_no_source_text(json.dumps(world.snapshot()["rows"]["macro.TACO"], ensure_ascii=False))


def test_a_different_method_version_is_labelled_other(world):
    _outcome, row = publish_taco(world, saved_payload(method="persistent-pressure-new5-v2"))
    assert row["values"]["method"] == "other" and row["state"] == "ok"
    assert_no_source_text(json.dumps(row, ensure_ascii=False))
    assert "persistent" not in json.dumps(row)  # 版本字串不進快照：只有 v1／other


def test_missing_optional_fields_become_none(world):
    _outcome, row = publish_taco(world, saved_payload(change20d=None, status_key=None))
    assert row["values"]["chg20"] is None and row["values"]["status_key"] is None and row["state"] == "ok"


def test_an_old_series_is_published_but_already_stale(world):
    old = [item for item in saved_payload()["history"] if item[0] <= "2026-10-08"]
    _outcome, row = publish_taco(world, saved_payload(as_of="2026-10-08", value=old[-1][1], history=old),
                                 data_date="2026-10-08", now=SAVED_NOW)
    assert row["state"] == "ok" and row["data_date"] == "2026-10-08"
    assert row["stale_after"] == at_et(2026, 10, 14, 16, 0) < SAVED_NOW  # 過期的判斷留給 bot（now > stale_after）


def test_nothing_stored_yet_means_missing_not_an_error(world):
    world.store._db().execute("DELETE FROM obs WHERE obs_id = 'macro.TACO'")
    outcome = world.publish()
    assert outcome["kind"] == "ok" and "macro.TACO" not in outcome.get("row_errors", {})
    row = world.snapshot()["rows"]["macro.TACO"]
    assert row["state"] == "never" and row["values"] == {} and row["stale_after"] == 0.0
    assert outcome["never_reasons"].get("missing", 0) >= 1
    # 只失敗過、從沒成功的 obs（payload 是 None）也是暫缺
    world.store.obs_checked("macro.TACO", error_kind="format")
    outcome = world.publish()
    assert world.snapshot()["rows"]["macro.TACO"]["state"] == "never" and "row_errors" not in outcome


def damaged(**changes):
    return saved_payload(**changes)


def swap_history(payload: dict, mutate) -> dict:
    payload = copy.deepcopy(payload)
    mutate(payload["history"])
    return payload


BAD_STORED = {
    "history_goes_back": (swap_history(saved_payload(), lambda h: h.append(["2026-10-21", 9.0])), "bad:taco_history"),
    "history_duplicate_day": (swap_history(saved_payload(), lambda h: h.append(["2026-10-22", 9.0005])), "bad:taco_history"),
    "history_item_not_a_pair": (swap_history(saved_payload(), lambda h: h.__setitem__(5, ["2025-04-02"])), "bad:taco_history"),
    "history_bad_value": (swap_history(saved_payload(), lambda h: h.__setitem__(5, [h[5][0], "x"])), "bad:taco_history"),
    "history_bad_day": (swap_history(saved_payload(), lambda h: h.__setitem__(5, ["nope", 1.0])), "bad:taco_history"),
    "history_too_short": (swap_history(saved_payload(), lambda h: h.__delitem__(slice(0, 340))), "bad:taco"),
    "history_not_a_list": (saved_payload(history="x"), "bad:taco_history"),
    "history_a_number": (saved_payload(history=5), "bad:taco_history"),
    "history_missing": (saved_payload(history=None), "bad:taco_history"),
    "history_empty": (saved_payload(history=[]), "bad:taco"),
    "last_day_differs": (saved_payload(as_of="2026-10-21"), "bad:taco"),
    "last_value_differs": (saved_payload(value=9.5), "bad:taco"),
    "value_missing": (saved_payload(value=None), "bad:taco"),
    "as_of_missing": (saved_payload(as_of=None), "bad:taco"),
    "change_not_a_number": (saved_payload(change20d="x"), "bad:taco"),
    "status_key_weird": (saved_payload(status_key="HIGH"), "bad:taco_values"),
    "value_out_of_range": (swap_history(saved_payload(value=100.5), lambda h: h.__setitem__(-1, [h[-1][0], 100.5])), "bad:taco_values"),
}


@pytest.mark.parametrize("name", list(BAD_STORED))
def test_damaged_stored_data_only_blanks_this_row(world, name):
    payload, reason = BAD_STORED[name]
    outcome, row = publish_taco(world, payload)
    assert row["state"] == "never" and row["values"] == {}
    assert outcome["row_errors"] == {"macro.TACO": reason}
    other = world.snapshot()["rows"]
    assert other["macro.DXY"]["state"] == "ok" and other["us.etf.QQQ"]["state"] == "ok"  # 其他列不受影響


def test_the_percentile_ranks_the_last_history_point_not_the_slightly_different_index_value(world):
    """歷史自己的最新點才是位階的比較對象：index.value 最多差 0.01，它若剛好把另一筆也算進「≤」，位階會多 1。"""
    values = [round(10.0 + 0.1 * i, 4) for i in range(21)]  # 10.0 … 12.0（共 21 筆，12.0 是第 21 筆）
    values += [12.005] + [round(13.0 + 0.1 * i, 4) for i in range(77)] + [12.0]  # 夾在 12.0 和 12.01 之間的一筆；最後一筆 12.0
    rows = [[day.isoformat(), value] for (day, _old), value in zip(HISTORY[: len(values)], values)]
    assert len(rows) == 100 and sum(1 for _day, v in rows if v <= 12.0) == 22 and sum(1 for _day, v in rows if v <= 12.01) == 23
    payload = saved_payload(as_of=rows[-1][0], value=12.01, history=rows)  # index.value 比最後一筆多 0.01
    outcome, row = publish_taco(world, payload, data_date=rows[-1][0])
    assert "row_errors" not in outcome and row["values"]["value"] == 12.01 and row["values"]["n"] == 100
    assert row["values"]["percentile"] == 22  # 用最後一筆（12.0）比：22/100；用 index.value（12.01）比會是 23


def test_a_difference_of_exactly_one_hundredth_between_the_last_row_and_the_value_is_published(world):
    count, value = float_edge_case()
    rows = [[day.isoformat(), v] for day, v in HISTORY[:count]]
    assert abs(value - rows[-1][1]) > 0.01
    payload = saved_payload(as_of=rows[-1][0], value=value, history=rows)
    outcome, row = publish_taco(world, payload, data_date=rows[-1][0])
    assert row["state"] == "ok" and "row_errors" not in outcome and row["values"]["value"] == value


def test_a_value_dated_after_d_is_not_published(world):
    future = saved_payload(as_of="2026-10-23", value=9.2, history=saved_payload()["history"] + [["2026-10-23", 9.2]])
    outcome, row = publish_taco(world, future, data_date="2026-10-23", now=at_tpe(2026, 10, 24, 3, 0))  # 週五還沒收完
    assert row["state"] == "never" and outcome["row_errors"] == {"macro.TACO": "bad:taco_future"}
    outcome, row = publish_taco(world, future, data_date="2026-10-23", now=at_tpe(2026, 10, 24, 4, 31))
    assert row["state"] == "ok" and row["data_date"] == "2026-10-23" and "row_errors" not in outcome


# ── validate_snapshot 擋型別錯的列 ──────────────────────────────────────────────


def test_validate_snapshot_catches_a_mistyped_taco_row(world):
    world.publish()
    snapshot = world.snapshot()
    assert market_publish.validate_snapshot(snapshot) == []
    for field, bad in (("percentile", "88"), ("n", 394.5), ("value", "9"), ("since", "2025-3-26"), ("method", None),
                       ("max_date", 20260327)):
        broken = copy.deepcopy(snapshot)
        broken["rows"]["macro.TACO"]["values"][field] = bad
        assert "row:macro.TACO" in market_publish.validate_snapshot(broken), field
    broken = copy.deepcopy(snapshot)
    del broken["rows"]["macro.TACO"]["values"]["chg20"]
    assert "row:macro.TACO" in market_publish.validate_snapshot(broken)
    broken = copy.deepcopy(snapshot)
    del broken["rows"]["macro.TACO"]  # 少一列＝列 id 對不上
    assert "row_ids" in market_publish.validate_snapshot(broken)
