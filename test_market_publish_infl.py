"""通膨列（us.infl.be、us.infl.cpi；2026-10-24 批 2 C 軌）在 publish 與 bot 兩端的測試。

用真的 market_store.Store（暫存目錄）塞合成／保存的資料，時間一律注入。不連網、不碰正式 state。重點：
- publish 只放數字、列舉碼與 ISO 日期，型別不對或碼不認得的整列改 never／被 validate_snapshot 擋下；
- 不升 SNAPSHOT_SCHEMA、不新增表、不動既有列：舊快照（沒有新列）與新快照（多了新列）兩個方向 bot 都照常；
- publish 不 import pipeline 模組（import 很輕），obs 鍵兩邊各存一份、這裡比對。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import market_calc
import market_dashboard
import market_paths
import market_publish
import market_store

TPE = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")
WORK = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 23, 8, 0, tzinfo=TPE).timestamp()  # 台北週五 08:00＝美東週四 20:00：D＝10/22

# 裁自 research_bodies/infl_diag/ust_nominal_202610.body 與 ust_real_202610.body（2026-10 全部 15 個共同日期）
OCT_ROWS = (  # (日期, 名目5, 名目10, 實質5, 實質10)
    ("2026-10-01", 5.01, 5.24, 2.65, 2.88),
    ("2026-10-02", 5.06, 5.28, 2.69, 2.92),
    ("2026-10-05", 5.06, 5.31, 2.70, 2.95),
    ("2026-10-06", 5.03, 5.27, 2.66, 2.91),
    ("2026-10-07", 5.03, 5.28, 2.66, 2.92),
    ("2026-10-08", 4.99, 5.22, 2.62, 2.87),
    ("2026-10-09", 5.02, 5.24, 2.68, 2.91),
    ("2026-10-13", 5.00, 5.21, 2.67, 2.88),
    ("2026-10-14", 5.00, 5.19, 2.64, 2.84),
    ("2026-10-15", 4.91, 5.10, 2.58, 2.78),
    ("2026-10-16", 4.89, 5.09, 2.52, 2.74),
    ("2026-10-19", 4.94, 5.15, 2.56, 2.80),
    ("2026-10-20", 4.94, 5.12, 2.55, 2.77),
    ("2026-10-21", 4.95, 5.13, 2.55, 2.76),
    ("2026-10-22", 4.78, 4.96, 2.33, 2.64),
)


def be_payload(rows=OCT_ROWS) -> dict:
    return {"rows": [{"date": d, "n5": n5, "n10": n10, "r5": r5, "r10": r10} for d, n5, n10, r5, r10 in rows]}


def cpi_series() -> dict:
    """合成的四個 CPI 序列 2025-01…2026-09（每月 +0.2%；2025-10 缺值，和 BLS 停擺那個月一樣）。保存的真實值另外在
    test_market_calc_infl（計算）和 test_market_pipeline_infl（collect → publish 接縫）用。"""
    out = {}
    for series_id, base in zip(market_calc.CPI_SERIES, (300.0, 301.0, 305.0, 306.0)):
        points = {}
        for index in range(21):
            year, month = 2025 + index // 12, index % 12 + 1
            if (year, month) != (2025, 10):
                points[f"{year}-{month:02d}"] = round(base * 1.002 ** index, 3)
        out[series_id] = points
    return out


def cpi_payload(series=None, retrieved="2026-10-24") -> dict:
    return {"series": cpi_series() if series is None else series, "retrieved": retrieved}


class FakeCharts:
    """沒有圖要畫（沒有期權資料）；publish 只在真的要畫時才用它。"""


class World:
    def __init__(self, root: Path) -> None:
        state = root / "state"
        state.mkdir(mode=0o700)
        os.chmod(state, 0o700)
        (root / "market_seasonality.json").write_bytes((WORK / "market_seasonality.json").read_bytes())
        self.paths = market_paths.Paths.under(root)
        self.store = market_store.Store(self.paths.history_db, now_fn=lambda: NOW)

    def ctx(self, now: float = NOW) -> market_publish._Ctx:
        return market_publish._Ctx(self.store, self.paths, now)

    def row(self, row_id: str, now: float = NOW) -> dict:
        return market_publish._build_row(self.ctx(now), row_id)

    def publish(self, now: float = NOW) -> dict:
        return market_publish.publish(self.store, self.paths, now=now, charts=FakeCharts())

    def snapshot(self) -> dict:
        return json.loads(self.paths.snapshot.read_bytes().decode("utf-8"))


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.store.close()


# ── 損益兩平列 ────────────────────────────────────────────────────────────────


def test_the_breakeven_row_from_the_saved_october_files(world):
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")
    row = world.row("us.infl.be")
    assert row["state"] == "ok" and row["data_date"] == "2026-10-22" and row["period"] is None
    assert row["source"] == "美國財政部" and row["basis"] == market_calc.TEXT_BE_BASIS and row["label"] == "損益兩平通膨"
    assert row["values"] == {
        "y5": {"date": "2026-10-22", "value": 2.45, "chg5_bp": 12, "move": "up"},
        "y10": {"date": "2026-10-22", "value": 2.32, "chg5_bp": 0, "move": "flat"},
        "spread_bp": 13, "spread": "five_higher",
    }
    # 日資料：資料日後第 4 個預期交易日的收盤（10/23、10/26、10/27、10/28）
    assert row["stale_after"] == datetime(2026, 10, 28, 16, 0, tzinfo=ET).timestamp()
    assert market_calc.row_ok("us.infl.be", row)


def test_no_obs_or_unusable_rows_mean_no_row(world):
    assert world.row("us.infl.be")["state"] == "never"
    world.store.put_obs("us.infl.be", {"rows": [None, 5, "x", {}, {"date": "2026-10-22"}, [1, 2]]}, data_date="2026-10-22")
    assert world.row("us.infl.be")["state"] == "never"
    world.store.put_obs("us.infl.be", {"rows": "oops"}, data_date="2026-10-22")
    assert world.row("us.infl.be")["state"] == "never"
    world.store.put_obs("us.infl.be", {}, data_date="2026-10-22")
    assert world.row("us.infl.be")["state"] == "never"
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")  # 對照：同一個流程，資料正常就是 ok
    assert world.row("us.infl.be")["state"] == "ok"


def test_rows_after_d_are_not_published(world):
    rows = OCT_ROWS + (("2026-10-23", 4.80, 4.99, 2.30, 2.60),)  # 台北 10/23 08:00 時美東還在 10/22，D＝10/22
    world.store.put_obs("us.infl.be", be_payload(rows), data_date="2026-10-23")
    row = world.row("us.infl.be")
    assert row["data_date"] == "2026-10-22" and row["values"]["y5"]["value"] == 2.45


def test_the_row_carries_its_own_date_when_the_real_curve_is_a_day_behind(world):
    """財政部先放名目：實質還沒有的那天不算；列的資料日停在兩邊都有的那天（比 10 年債列晚或早一天都照實寫）。"""
    rows = OCT_ROWS[:-1] + (("2026-10-22", 4.78, 4.96, None, None),)
    world.store.put_obs("us.infl.be", be_payload(rows), data_date="2026-10-22")
    row = world.row("us.infl.be")
    assert row["state"] == "ok" and row["data_date"] == "2026-10-21"
    assert row["values"]["y5"]["date"] == row["values"]["y10"]["date"] == "2026-10-21"


def test_one_tenor_can_publish_without_the_other(world):
    rows = OCT_ROWS[:-1] + (("2026-10-22", 4.78, 4.96, None, 2.64),)  # 實質 5 年缺格
    world.store.put_obs("us.infl.be", be_payload(rows), data_date="2026-10-22")
    row = world.row("us.infl.be")
    assert row["data_date"] == "2026-10-22" and row["values"]["y10"]["date"] == "2026-10-22"
    assert row["values"]["y5"]["date"] == "2026-10-21" and row["values"]["spread_bp"] is None
    assert market_calc.row_ok("us.infl.be", row)


def test_the_row_goes_stale_with_the_daily_rule(world):
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")
    boundary = datetime(2026, 10, 28, 16, 0, tzinfo=ET).timestamp()
    before, after = world.row("us.infl.be", boundary - 60), world.row("us.infl.be", boundary + 60)
    assert before["state"] == after["state"] == "ok"  # never 列的 stale_after 是 0.0，不能拿來當「過期」的證據
    assert before["stale_after"] == after["stale_after"] == boundary
    assert boundary - 60 <= before["stale_after"] and after["stale_after"] < boundary + 60  # bot 只比 now > stale_after


# ── 整份快照 ─────────────────────────────────────────────────────────────────


def _seed_other_data(store: market_store.Store) -> None:
    """讓 bot 有東西可以回（不是「資料準備中」）：10 年債一列。"""
    store.put_obs("us.ust", {"rows": [["2026-10-21", 4.25, 4.81, 5.13, 5.42], ["2026-10-22", 4.22, 4.65, 4.96, 5.29]]},
                  data_date="2026-10-22")


def test_publish_writes_the_new_row_and_the_snapshot_validates(world):
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")
    _seed_other_data(world.store)
    outcome = world.publish()
    assert outcome["kind"] == "ok" and "row_errors" not in outcome and "shared_errors" not in outcome, outcome
    snapshot = world.snapshot()
    assert snapshot["schema"] == market_calc.SNAPSHOT_SCHEMA == 1  # 不升 SNAPSHOT_SCHEMA
    assert set(snapshot["rows"]) == set(market_calc.ROW_IDS) and "us.infl.be" in snapshot["rows"]
    assert snapshot["rows"]["us.infl.be"]["state"] == "ok"
    assert market_publish.validate_snapshot(snapshot) == []


@pytest.mark.parametrize("mutate", [
    lambda row: row["values"]["y5"].update(move="漲翻天"),  # 不是列舉碼的字（不論從哪來）不能出現在快照
    lambda row: row["values"].update(spread="<b>x</b>"),
    lambda row: row["values"]["y10"].update(date="10/22"),
    lambda row: row["values"]["y10"].update(chg5_bp="12"),
    lambda row: row["values"].pop("spread"),
])
def test_validate_snapshot_rejects_a_new_row_with_free_text_or_wrong_types(world, mutate):
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")
    _seed_other_data(world.store)
    assert world.publish()["kind"] == "ok"
    snapshot = world.snapshot()
    mutate(snapshot["rows"]["us.infl.be"])
    assert "row:us.infl.be" in market_publish.validate_snapshot(snapshot)


def test_a_builder_that_returns_an_unknown_code_makes_only_that_row_never(world, monkeypatch):
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")
    _seed_other_data(world.store)
    real = market_calc.breakeven_values

    def broken(rows):
        values = real(rows)
        values["y5"]["move"] = "free text"
        return values

    monkeypatch.setattr(market_calc, "breakeven_values", broken)
    outcome = world.publish()
    assert outcome["kind"] == "ok" and outcome["row_errors"] == {"us.infl.be": "bad:row_ids"}
    rows = world.snapshot()["rows"]
    assert rows["us.infl.be"]["state"] == "never" and rows["macro.UST10Y"]["state"] == "ok"  # 其他列不受影響


# ── CPI 與核心 CPI 列 ─────────────────────────────────────────────────────────


def test_the_cpi_row_envelope_and_values(world):
    world.store.put_obs("us.infl.cpi", cpi_payload(), data_date="2026-09-30", period="2026-09")
    row = world.row("us.infl.cpi")
    assert row["state"] == "ok" and row["source"] == "BLS" and row["label"] == "美國物價（CPI）"
    assert row["basis"] == market_calc.TEXT_INFL_BASIS and row["period"] == "2026-09" and row["data_date"] == "2026-09-30"
    assert row["values"] == market_calc.cpi_values(cpi_series(), date(2026, 10, 24))  # publish 只是把 obs 交給共用的計算
    assert row["values"]["retrieved"] == "2026-10-24" and row["values"]["aligned"] is True
    assert row["values"]["cpi"]["month"] == "2026-09-01" and row["values"]["cpi"]["yoy_month"] == "2026-09-01"
    # 月資料：月底（9/30）＋62 天＝12/1 台北 0 點過期，和 FINRA、月營收同一套
    assert row["stale_after"] == datetime(2026, 12, 1, tzinfo=TPE).timestamp()
    assert market_calc.row_ok("us.infl.cpi", row) and market_calc.ROW_IDS["us.infl.cpi"] is market_calc.INFL_SPEC


def test_the_cpi_row_goes_stale_after_62_days(world):
    world.store.put_obs("us.infl.cpi", cpi_payload(), data_date="2026-09-30", period="2026-09")
    stale_after = world.row("us.infl.cpi")["stale_after"]
    assert world.row("us.infl.cpi", stale_after - 1)["stale_after"] == stale_after
    assert datetime.fromtimestamp(stale_after, TPE).date() == date(2026, 12, 1)


def test_the_retrieval_date_is_required_and_comes_from_the_collected_data(world):
    """BLS 條款：應註明取得日期。取得日是 collect 抓的那天（obs 裡），不是 publish 的現在；沒有或壞掉就不發布這一列。"""
    world.store.put_obs("us.infl.cpi", cpi_payload(retrieved="2026-10-16"), data_date="2026-09-30", period="2026-09")
    assert world.row("us.infl.cpi")["values"]["retrieved"] == "2026-10-16"  # publish 的 NOW 是 10/23
    for bad in (None, "10/16/2026", "2026-13-40", 20261016, ""):
        payload = cpi_payload()
        payload["retrieved"] = bad
        world.store.put_obs("us.infl.cpi", payload, data_date="2026-09-30", period="2026-09")
        assert world.row("us.infl.cpi")["state"] == "never", bad
    payload = cpi_payload()
    del payload["retrieved"]
    world.store.put_obs("us.infl.cpi", payload, data_date="2026-09-30", period="2026-09")
    assert world.row("us.infl.cpi")["state"] == "never"


def test_no_obs_or_unusable_series_mean_no_cpi_row(world):
    assert world.row("us.infl.cpi")["state"] == "never"
    for payload in ({}, {"retrieved": "2026-10-24"}, cpi_payload("oops"), cpi_payload({}), cpi_payload({"CUUR0000SA0": {}}),
                    cpi_payload({"CUUR0000SA0": {"bad": 1.0, "2026-13": 2.0, "2026-9": 3.0}}),
                    cpi_payload({sid: {"2026-09": "abc"} for sid in market_calc.CPI_SERIES}),
                    cpi_payload({sid: {"2026-09": -5.0, "2025-09": 0} for sid in market_calc.CPI_SERIES})):
        world.store.put_obs("us.infl.cpi", payload, data_date="2026-09-30", period="2026-09")
        assert world.row("us.infl.cpi")["state"] == "never", payload
    series = cpi_series()
    del series["CUUR0000SA0"]  # 全項未季調沒有資料：沒有列
    world.store.put_obs("us.infl.cpi", cpi_payload(series), data_date="2026-09-30", period="2026-09")
    assert world.row("us.infl.cpi")["state"] == "never"


def test_unknown_series_and_junk_inside_known_ones_are_dropped(world):
    series = cpi_series()
    series["EVIL"] = {"2026-09": 999.0}
    series["CUUR0000SA0"]["2026-10"] = None  # JSON null
    series["CUUR0000SA0"]["note"] = 5.0
    world.store.put_obs("us.infl.cpi", cpi_payload(series), data_date="2026-09-30", period="2026-09")
    row = world.row("us.infl.cpi")
    assert row["state"] == "ok" and row["values"] == market_calc.cpi_values(cpi_series(), date(2026, 10, 24))


def test_series_that_end_in_different_months_publish_unaligned(world):
    series = cpi_series()
    del series["CUSR0000SA0"]["2026-09"]
    world.store.put_obs("us.infl.cpi", cpi_payload(series), data_date="2026-08-31", period="2026-08")
    values = world.row("us.infl.cpi")["values"]
    assert values["aligned"] is False and values["gap"] is None and values["cpi"]["momentum"] is None
    assert values["cpi"]["sa_month"] == "2026-08-01" and values["cpi"]["month"] == "2026-09-01"


def test_publish_writes_both_new_rows_and_the_snapshot_validates_with_the_bot_unchanged(world):
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")
    world.store.put_obs("us.infl.cpi", cpi_payload(), data_date="2026-09-30", period="2026-09")
    _seed_other_data(world.store)
    outcome = world.publish()
    assert outcome["kind"] == "ok" and "row_errors" not in outcome and "shared_errors" not in outcome, outcome
    snapshot = world.snapshot()
    assert snapshot["schema"] == 1 and snapshot["rows"]["us.infl.cpi"]["state"] == "ok"
    assert market_publish.validate_snapshot(snapshot) == []


@pytest.mark.parametrize("mutate", [
    lambda row: row["values"]["cpi"].update(level="very high"),  # 不是列舉碼的字（不論從哪來）不能出現在快照
    lambda row: row["values"]["cpi"].update(momentum="<b>x</b>"),
    lambda row: row["values"]["core"].update(yoy_move="rising"),
    lambda row: row["values"].update(gap="big"),
    lambda row: row["values"]["cpi"].update(month="2026-09"),
    lambda row: row["values"]["cpi"].update(yoy="3.7"),
    lambda row: row["values"].update(aligned="yes"),
    lambda row: row["values"].update(retrieved="10/24"),
    lambda row: row["values"].pop("retrieved"),
    lambda row: row["values"]["cpi"].pop("yoy_month"),
])
def test_validate_snapshot_rejects_a_cpi_row_with_free_text_or_wrong_types(world, mutate):
    world.store.put_obs("us.infl.cpi", cpi_payload(), data_date="2026-09-30", period="2026-09")
    _seed_other_data(world.store)
    assert world.publish()["kind"] == "ok"
    snapshot = world.snapshot()
    mutate(snapshot["rows"]["us.infl.cpi"])
    assert "row:us.infl.cpi" in market_publish.validate_snapshot(snapshot)


def test_a_cpi_calculation_that_returns_an_unknown_code_makes_only_that_row_never(world, monkeypatch):
    world.store.put_obs("us.infl.cpi", cpi_payload(), data_date="2026-09-30", period="2026-09")
    _seed_other_data(world.store)
    real = market_calc.cpi_values

    def broken(series, retrieved):
        values = real(series, retrieved)
        values["cpi"]["level"] = "free text from somewhere"
        return values

    monkeypatch.setattr(market_calc, "cpi_values", broken)
    outcome = world.publish()
    assert outcome["kind"] == "ok" and outcome["row_errors"] == {"us.infl.cpi": "bad:row_ids"}
    rows = world.snapshot()["rows"]
    assert rows["us.infl.cpi"]["state"] == "never" and rows["macro.UST10Y"]["state"] == "ok"


# ── 版本組合：bot 這一版不讀新列 ──────────────────────────────────────────────────


FLEX_BODIES = ("", " VOO", " QQQ", " SOXX", " 0050", " 期權", " 新聞", " 季節性", " 選舉", " 13F QQQ", " 13F VOO", " 13F SOXX")


def _replies(paths: market_paths.Paths) -> dict:
    market_dashboard.clear_cache()
    return {body: market_dashboard.handle(body, now=NOW, paths=paths) for body in FLEX_BODIES}


def test_the_bot_answers_the_same_with_or_without_the_new_rows(world):
    """舊快照（還沒有新列）與新快照（多了新列）：bot 這一版都不讀，所有指令的回覆一字不差，也不丟例外。"""
    world.store.put_obs("us.infl.be", be_payload(), data_date="2026-10-22")
    world.store.put_obs("us.infl.cpi", cpi_payload(), data_date="2026-09-30", period="2026-09")
    _seed_other_data(world.store)
    assert world.publish()["kind"] == "ok"
    new_snapshot = world.snapshot()
    with_row = _replies(world.paths)
    assert all(reply.flex is not None for reply in with_row.values())  # 有東西可回，不是「資料準備中」
    summary = market_dashboard.status_summary(now=NOW, paths=world.paths)
    assert summary["rows_unexpected"] == [] and "render_error" not in summary and summary["snapshot"] == "ok"
    assert new_snapshot["rows"]["us.infl.be"]["state"] == new_snapshot["rows"]["us.infl.cpi"]["state"] == "ok"
    old_snapshot = json.loads(json.dumps(new_snapshot))
    del old_snapshot["rows"]["us.infl.be"], old_snapshot["rows"]["us.infl.cpi"]
    market_paths.write_private_atomic(world.paths.snapshot, json.dumps(old_snapshot).encode("utf-8"))
    without_row = _replies(world.paths)
    assert without_row == with_row
    summary = market_dashboard.status_summary(now=NOW, paths=world.paths)
    assert summary["snapshot"] == "ok" and "render_error" not in summary and summary["rows_unexpected"] == []


def test_the_bot_does_not_read_the_new_rows():
    assert not {"us.infl.be", "us.infl.cpi"} & set(market_dashboard.READ_ROWS)


def test_the_pipeline_and_the_publisher_agree_on_the_obs_keys():
    import market_pipeline_infl

    assert market_publish.OBS_US_INFL_BE == market_pipeline_infl.OBS_INFL_BE
    assert market_publish.OBS_US_INFL_CPI == market_pipeline_infl.OBS_INFL_CPI


def test_the_publisher_stays_light_and_does_not_import_the_infl_modules():
    code = ("import sys, market_publish; "
            "print(sorted(m for m in ('market_pipeline_infl', 'market_sources_infl', 'market_pipeline_us', 'matplotlib', 'numpy') "
            "if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=WORK, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
