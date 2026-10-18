"""jobs/build_market_seasonality.py 與 market_seasonality.json 的測試（/盤勢 季節性，2026-10-17）。

公式與 N 用小的合成序列驗（§3.3、§18.15）；datahub CSV 用真實格式裁幾列（含近年 0.0 佔位欄）；
FinMind 用合成的日線。不連網、不讀 repo 以外的檔案。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import market_calc
from jobs import build_market_seasonality as bms

HERE = Path(__file__).resolve().parent
AS_OF = date(2026, 10, 17)

# datahub core/s-and-p-500 的真實格式（裁幾列）：近年 Dividend／Earnings／CPI 是 0.0 佔位
REAL_CSV = """Date,SP500,Dividend,Earnings,Consumer Price Index,Long Interest Rate,Real Price,Real Dividend,Real Earnings,PE10
1949-11-01,16.11,1.10667,2.34333,23.8,2.32,207.21,14.23,30.14,10.22
1949-12-01,16.54,1.14,2.32,23.6,2.32,214.55,14.79,30.09,10.53
1950-01-01,16.88,1.15,2.33667,23.5,2.32,219.89,14.98,30.44,10.75
1950-02-01,17.21,1.16,2.35333,23.5,2.34,224.19,15.11,30.66,10.91
2025-12-01,6853.03,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0
2026-01-01,6929.12,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0
2026-02-01,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0
not-a-date,1.0,0,0,0,0,0,0,0,0
"""


def synthetic_levels(first_year: int, end_year: int, change) -> dict[tuple[int, int], float]:
    """從 (first_year−1, 12)=100 起，依 change(year, month) 累乘出月水準。"""
    levels = {(first_year - 1, 12): 100.0}
    value = 100.0
    for year in range(first_year, end_year + 1):
        for month in range(1, 13):
            value *= 1.0 + change(year, month)
            levels[(year, month)] = value
    return levels


def business_days(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


class TestParsing:
    def test_real_csv_format_ignores_placeholders(self):
        levels = bms.parse_sp500_csv(REAL_CSV)
        assert levels[(1950, 1)] == 16.88 and levels[(2026, 1)] == 6929.12
        assert (2026, 2) not in levels  # SP500 本身是 0.0 的佔位列
        assert len(levels) == 6
        changes = bms.monthly_changes(levels, first=(1950, 1), end_year=1950, as_of=AS_OF)
        assert changes == {(1950, 1): pytest.approx(16.88 / 16.54 - 1), (1950, 2): pytest.approx(17.21 / 16.88 - 1)}

    def test_csv_errors(self):
        with pytest.raises(ValueError):
            bms.parse_sp500_csv("Date,Price\n1950-01-01,16.88\n")
        with pytest.raises(ValueError):
            bms.parse_sp500_csv("Date,SP500\n1950-01-01,16.88\n1950-01-01,17.00\n")
        with pytest.raises(ValueError):
            bms.parse_sp500_csv("Date,SP500\n1950-01-01,0.0\n")

    def test_finmind_payload(self):
        payload = {
            "msg": "success",
            "status": 200,
            "data": [
                {"date": "1999-01-05", "stock_id": "TAIEX", "close": 6152.43},
                {"date": "1999-01-06", "stock_id": "TAIEX", "close": 6199.91},
                {"date": "1999-01-06", "stock_id": "TAIEX", "close": 6199.91},  # 完全相同的重複
                {"date": "1999-01-07", "stock_id": "2330", "close": 80.0},
                {"date": "bad", "stock_id": "TAIEX", "close": 1.0},
                {"date": "1999-01-08", "stock_id": "TAIEX", "close": None},
            ],
        }
        assert bms.parse_finmind_daily(payload) == [(date(1999, 1, 5), 6152.43), (date(1999, 1, 6), 6199.91)]

    @pytest.mark.parametrize(
        "payload",
        [
            {"msg": "error", "status": 200, "data": []},
            {"msg": "success", "status": 402, "data": []},
            {"msg": "success", "status": 200, "data": "x"},
            {"msg": "success", "status": 200, "data": []},
            {"msg": "success", "status": 200, "data": [
                {"date": "1999-01-05", "stock_id": "TAIEX", "close": 1.0},
                {"date": "1999-01-05", "stock_id": "TAIEX", "close": 2.0},
            ]},
            [],
        ],
    )
    def test_finmind_errors(self, payload):
        with pytest.raises(ValueError):
            bms.parse_finmind_daily(payload)


class TestFormula:
    def test_month_change_is_this_over_previous_minus_one_and_needs_both_ends(self):
        levels = {(1949, 12): 100.0, (1950, 1): 110.0, (1950, 2): 99.0, (1950, 4): 120.0, (1950, 5): 132.0}
        changes = bms.monthly_changes(levels, first=(1950, 1), end_year=1950, as_of=AS_OF)
        assert changes == {
            (1950, 1): pytest.approx(0.10),
            (1950, 2): pytest.approx(-0.10),
            (1950, 5): pytest.approx(0.10),
        }  # 3 月沒值 → 3 月與 4 月都不算

    def test_unfinished_months_are_excluded(self):
        levels = {(1949, 12): 100.0, (1950, 1): 110.0, (1950, 2): 99.0}
        assert (1950, 2) not in bms.monthly_changes(levels, first=(1950, 1), end_year=1950, as_of=date(1950, 2, 28))
        assert (1950, 2) in bms.monthly_changes(levels, first=(1950, 1), end_year=1950, as_of=date(1950, 3, 1))
        assert bms.month_finished((2026, 9), AS_OF) and not bms.month_finished((2026, 10), AS_OF)

    def test_q4_is_december_over_september(self):
        levels = {(1950, 9): 100.0, (1950, 12): 108.0, (1951, 9): 100.0}
        assert bms.q4_changes(levels, years=[1950, 1951], as_of=AS_OF) == {1950: pytest.approx(0.08)}
        assert bms.q4_changes(levels, years=[1950], as_of=date(1950, 12, 31)) == {}  # 12 月還沒結束

    def test_summarize(self):
        stats = bms.summarize([0.01, -0.02, 0.0, 0.03])
        assert stats == {"up_pct": 50.0, "median": 0.5, "mean": 0.5, "n": 4}  # 0 不算上漲
        assert bms.summarize([]) is None

    def test_section_stats_midterm_and_q4_with_their_own_n(self):
        def change(year, month):
            if year == 1950 and month == 11:
                return -0.03
            return 0.02 if month in (1, 11, 12) else -0.01

        levels = synthetic_levels(1950, 1953, change)
        section = bms.build_section(levels, first=(1950, 1), end_year=1953, as_of=AS_OF, source="s", basis="b")
        assert section["pending_full_data"] is False
        assert (section["months_valid"], section["months_expected"]) == (48, 48)
        assert section["months"]["1"] == {"up_pct": 100.0, "median": 2.0, "mean": 2.0, "n": 4}
        assert section["months"]["11"] == {"up_pct": 75.0, "median": 2.0, "mean": 0.75, "n": 4}
        assert section["months"]["2"] == {"up_pct": 0.0, "median": -1.0, "mean": -1.0, "n": 4}
        assert section["midterm"]["years"] == [1950]
        assert section["midterm"]["months"]["11"] == {"up_pct": 0.0, "median": -3.0, "mean": -3.0, "n": 1}
        # 第四季＝(1−1%)(1+2%)(1+2%)−1；1950 年 11 月 −3%
        assert section["q4"] == {"up_pct": 75.0, "median": 3.0, "mean": 1.74, "n": 4}
        assert section["midterm"]["q4"] == {"up_pct": 0.0, "median": -2.05, "mean": -2.05, "n": 1}
        assert section["sample"] == [1950, 1953] and section["first_month"] == "1950-01"

    def test_section_marks_pending_when_coverage_is_low(self):
        levels = {(1949, 12): 100.0, (1950, 1): 101.0, (1950, 2): 102.0}
        section = bms.build_section(levels, first=(1950, 1), end_year=1953, as_of=AS_OF, source="s", basis="b")
        assert section["pending_full_data"] is True and "months" not in section
        assert (section["months_valid"], section["months_expected"]) == (2, 48)


class TestTaiex:
    def test_month_end_close_only_for_months_proven_complete(self):
        daily = [(date(1999, 1, 29), 6000.0), (date(1999, 1, 30), 6100.0), (date(1999, 2, 26), 6200.0),
                 (date(1999, 3, 2), 6300.0)]
        assert bms.month_end_levels(daily) == {(1999, 1): 6100.0, (1999, 2): 6200.0}  # 3 月還沒收完
        assert bms.month_end_levels([]) == {}

    def full_daily(self):
        days = business_days(date(1998, 12, 1), date(2026, 1, 9))
        return [(day, 6000.0 + 2.0 * i + (150.0 if day.month in (1, 11) else 0.0)) for i, day in enumerate(days)]

    def test_full_series_starts_in_1999_02_with_n_from_valid_samples(self):
        table = bms.build_table(
            bms.parse_sp500_csv(REAL_CSV) | synthetic_levels(1950, 2025, lambda y, m: 0.01),
            self.full_daily(),
            as_of=date(2026, 1, 15),
        )
        tw = table["taiex"]
        assert tw["pending_full_data"] is False and tw["first_month"] == "1999-02"
        assert tw["months"]["1"]["n"] == 26  # 1999-01 不算（第一個完整月份是 1999-02）
        assert all(tw["months"][str(m)]["n"] == 27 for m in range(2, 13))
        assert tw["midterm"]["years"] == [2002, 2006, 2010, 2014, 2018, 2022]
        assert all(stats["n"] == 6 for stats in tw["midterm"]["months"].values())
        assert tw["q4"]["n"] == 27 and tw["midterm"]["q4"]["n"] == 6
        assert tw["months"]["2"]["up_pct"] == 0.0  # 1 月底被墊高 150 → 2 月一律下跌
        assert tw["months"]["1"]["up_pct"] == 100.0

    def test_trimmed_data_is_pending(self):
        daily = [(d, 6000.0) for d in business_days(date(1999, 1, 5), date(1999, 3, 31))]
        daily += [(d, 47000.0) for d in business_days(date(2026, 8, 3), date(2026, 10, 16))]
        table = bms.build_table(synthetic_levels(1950, 2025, lambda y, m: 0.01), daily, as_of=AS_OF)
        assert table["taiex"]["pending_full_data"] is True and "months" not in table["taiex"]
        assert table["taiex"]["months_expected"] == 323
        assert bms.build_table(synthetic_levels(1950, 2025, lambda y, m: 0.01), None, as_of=AS_OF)["taiex"][
            "pending_full_data"
        ] is True

    def test_incomplete_sp500_is_an_error(self):
        with pytest.raises(ValueError):
            bms.build_table(synthetic_levels(1950, 1960, lambda y, m: 0.01), None, as_of=AS_OF)


class TestBuildTable:
    def test_sp500_sample_1950_to_2025(self):
        table = bms.build_table(synthetic_levels(1950, 2025, lambda y, m: 0.01 if y % 2 else -0.01), None, as_of=AS_OF)
        sp = table["sp500"]
        assert table["schema"] == 1 and table["built_on"] == "2026-10-17"
        assert table["note"] == market_calc.TEXT_SEASON_NOTE
        assert sp["sample"] == [1950, 2025] and sp["last_month"] == "2025-12"
        assert all(sp["months"][str(m)]["n"] == 76 for m in range(1, 13))
        assert all(sp["midterm"]["months"][str(m)]["n"] == 19 for m in range(1, 13))
        assert sp["q4"]["n"] == 76 and sp["midterm"]["q4"]["n"] == 19
        assert sp["midterm"]["years"][0] == 1950 and sp["midterm"]["years"][-1] == 2022
        assert sp["months"]["1"]["up_pct"] == 50.0  # 奇數年漲、偶數年跌，各 38 年
        assert sp["midterm"]["months"]["1"]["up_pct"] == 0.0  # 期中選舉年都是偶數年

    def test_main_writes_the_same_table(self, tmp_path, capsys):
        csv_path = tmp_path / "sp.csv"
        rows = ["Date,SP500"] + [
            f"{y}-{m:02d}-01,{v:.6f}" for (y, m), v in sorted(synthetic_levels(1950, 2025, lambda y, m: 0.005).items())
        ]
        csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        taiex_path = tmp_path / "taiex.json"
        taiex_path.write_text(json.dumps({"msg": "success", "status": 200, "data": [
            {"date": "1999-01-05", "stock_id": "TAIEX", "close": 6152.43}
        ]}), encoding="utf-8")
        out = tmp_path / "out" / "market_seasonality.json"
        args = ["--sp500-csv", str(csv_path), "--taiex-json", str(taiex_path), "--as-of", "2026-10-17"]
        assert bms.main(args + ["--out", str(out)]) == 0
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["sp500"]["months"]["7"]["n"] == 76 and written["taiex"]["pending_full_data"] is True
        assert list(out.parent.iterdir()) == [out]  # 暫存檔有收乾淨
        assert bms.main(args) == 0
        assert json.loads(capsys.readouterr().out) == written
        with pytest.raises(SystemExit):
            bms.main(["--sp500-csv", str(csv_path)])  # 一定要給 --as-of


class TestCommittedTable:
    def table(self):
        return json.loads((HERE / "market_seasonality.json").read_text(encoding="utf-8"))

    def test_sp500_section(self):
        table = self.table()
        sp = table["sp500"]
        assert table["schema"] == 1 and table["note"] == market_calc.TEXT_SEASON_NOTE
        assert sp["basis"] == "月均價" and "datahub" in sp["source"] and sp["sample"] == [1950, 2025]
        assert (sp["first_month"], sp["last_month"], sp["months_valid"], sp["months_expected"]) == (
            "1950-01", "2025-12", 912, 912,
        )
        assert set(sp["months"]) == {str(m) for m in range(1, 13)}
        for stats in sp["months"].values():
            assert stats["n"] == 76 and 0 <= stats["up_pct"] <= 100
        assert all(stats["n"] == 19 for stats in sp["midterm"]["months"].values())
        assert sp["q4"]["n"] == 76 and sp["midterm"]["q4"]["n"] == 19
        assert len(sp["midterm"]["years"]) == 19
        # 用 pandas 另外算過（真實 datahub CSV，1950–2025）：1 月 67.1%／1.46／1.41、第四季 72.4%／3.92／2.64
        assert sp["months"]["1"] == {"up_pct": 67.1, "median": 1.46, "mean": 1.41, "n": 76}
        assert sp["q4"] == {"up_pct": 72.4, "median": 3.92, "mean": 2.64, "n": 76}
        assert sp["midterm"]["q4"] == {"up_pct": 78.9, "median": 4.32, "mean": 4.63, "n": 19}

    def test_taiex_section(self):
        tw = self.table()["taiex"]
        assert tw["first_month"] == "1999-02" and tw["basis"] == "月底收盤"
        if tw["pending_full_data"]:
            assert "months" not in tw  # 只拿到裁小的 FinMind fixture：等完整資料再重算
        else:
            assert tw["months"]["1"]["n"] >= 26 and tw["months"]["2"]["n"] >= 27
