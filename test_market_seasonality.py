"""jobs/build_market_seasonality.py 與 market_seasonality.json 的測試（/盤勢 季節性，2026-10-17）。

公式與 N 用小的合成序列驗（§3.3、§18.15）；datahub CSV 用真實格式裁幾列（含近年 0.0 佔位欄）；
FinMind 用合成的日線。不連網、不讀 repo 以外的檔案。
"""

from __future__ import annotations

import calendar
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


# ── Nasdaq 日線：SOX 指數、SOXX ETF（2026-10-24 第 9 輪 A14） ──────────────────────────────────────────────
# 下面的列原樣取自 2026-10-23 存檔的 api.nasdaq.com 回應（research_bodies/soxx_diag/nasdaq 的 n1、n2），欄位依序是
# (日期, 收盤, 開, 高, 低)；只放判斷填充列、月底收盤、離月底多遠要用到的幾列，不放整段價格。
SOX_REAL = (
    ("01/01/1990", "479.49", "0.00", "0.00", "0.00"),  # 存檔的第一列：開高低全 0、前面沒有列
    ("01/02/1990", "479.49", "0.00", "0.00", "0.00"),
    ("04/29/1994", "479.49", "0.00", "0.00", "0.00"),
    ("05/02/1994", "479.49", "0.00", "0.00", "0.00"),
    ("05/03/1994", "479.49", "0.00", "0.00", "0.00"),  # 1994-05-04 之前整段都是固定的 479.49 填充列
    ("05/04/1994", "119.00", "120.50", "121.30", "118.90"),  # 第一個真值
    ("05/05/1994", "118.30", "119.00", "120.10", "118.00"),
    ("05/31/1994", "121.80", "118.40", "121.90", "117.50"),
    ("06/30/1994", "119.50", "121.00", "121.70", "119.00"),
    ("11/30/1995", "227.50", "231.60", "236.50", "226.50"),
    ("12/29/1995", "200.70", "199.90", "203.80", "198.10"),
    ("01/01/1996", "200.70", "0.00", "0.00", "0.00"),  # 1996-01 整月都是填充
    ("01/31/1996", "200.70", "0.00", "0.00", "0.00"),
    ("02/01/1996", "200.70", "0.00", "0.00", "0.00"),
    ("02/29/1996", "184.52", "189.29", "189.72", "184.28"),
    ("03/29/1996", "186.18", "193.27", "193.85", "185.82"),
    ("04/08/1996", "172.71", "168.21", "174.26", "165.80"),  # 4 月最後一個真值（離月底 22 天）
    ("04/30/1996", "172.71", "0.00", "0.00", "0.00"),
    ("05/01/1996", "172.71", "0.00", "0.00", "0.00"),  # 1996-05 整月都是填充
    ("05/31/1996", "172.71", "0.00", "0.00", "0.00"),
    ("06/03/1996", "172.71", "0.00", "0.00", "0.00"),
    ("06/28/1996", "200.54", "197.92", "200.76", "197.92"),
    ("07/28/1996", "174.71", "174.98", "177.70", "174.52"),
    ("07/31/1996", "174.71", "0.00", "0.00", "0.00"),
    ("08/30/1996", "156.86", "152.86", "156.96", "152.06"),
    ("09/30/1996", "169.46", "171.20", "171.89", "168.98"),
    ("05/14/2010", "353.86", "360.35", "360.38", "347.06"),
    ("05/17/2010", "359.39", "0.00", "0.00", "0.00"),  # 真實的交易日：開高低缺（0.00）、收盤和前一天不同
    ("05/18/2010", "348.85", "362.68", "363.73", "347.37"),
)
SOXX_REAL = (
    ("10/24/2016", "37.4033", "36.9233", "37.4267", "36.9233"),  # Nasdaq 的 ETF 歷史固定只給最近 10 年
    ("10/25/2016", "37.37", "37.4233", "37.5367", "37.30"),
    ("10/31/2016", "37.12", "37.05", "37.2967", "37.05"),
    ("11/30/2016", "39.7933", "40.0033", "40.1067", "39.7767"),
    ("12/29/2016", "41.55", "41.3667", "41.55", "41.1133"),
    ("12/30/2016", "40.9067", "41.70", "41.8867", "40.7733"),
    ("01/03/2017", "40.9067", "41.0467", "41.4933", "40.5567"),  # 和前一天收盤相同，但開高低不是 0：真實列，要留著
)
# 存檔的 SOXX 2001–2016 回應（n3）：Nasdaq 回 200，但 totalRecords 是 0、rows 是 null
N3_EMPTY = {"data": {"symbol": "SOXX", "totalRecords": 0, "tradesTable": {"asOf": None, "headers": None, "rows": None}},
            "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}
NASDAQ_HEADERS = {"date": "Date", "close": "Close/Last", "volume": "Volume", "open": "Open", "high": "High", "low": "Low"}


def nasdaq_payload(symbol, rows, *, rcode=200):
    return {"data": {"symbol": symbol, "totalRecords": len(rows),
                     "tradesTable": {"asOf": None, "headers": dict(NASDAQ_HEADERS), "rows": rows}},
            "message": None, "status": {"rCode": rcode, "bCodeMessage": None, "developerMessage": None}}


def nasdaq_rows(real):
    """(日期, 收盤, 開, 高, 低) → Nasdaq 的列；api.nasdaq.com 是新到舊。"""
    rows = [{"date": d, "close": c, "volume": "--", "open": o, "high": h, "low": low} for d, c, o, h, low in real]
    rows.sort(key=lambda r: (r["date"][6:], r["date"][:2], r["date"][3:5]), reverse=True)
    return rows


def chain(first, last, change):
    """從 first 月的 100.0 起，依 change(年, 月) 累乘（取到小數 2 位，和 payload 的字串一致）到 last 月。"""
    levels, (year, month) = {first: 100.0}, first
    while (year, month) < last:
        before = (year, month)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        levels[(year, month)] = round(levels[before] * (1.0 + change(year, month)), 2)
    return levels


def level_payload(symbol, levels, last_day):
    """月水準 → Nasdaq payload：每個月兩列（10 日、月底；開高低不是 0），最後一個月的最後一列落在 last_day。"""
    rows = []
    for (year, month), level in sorted(levels.items()):
        end = date(year, month, calendar.monthrange(year, month)[1])
        for day, close in ((date(year, month, 10), level * 0.99), (min(end, last_day), level)):
            text = f"{day.month:02d}/{day.day:02d}/{day.year}"
            rows.append({"date": text, "close": f"{close:,.2f}", "volume": "--", "open": f"{close:,.2f}",
                         "high": f"{close * 1.01:,.2f}", "low": f"{close * 0.99:,.2f}"})
    rows.sort(key=lambda r: (r["date"][6:], r["date"][:2], r["date"][3:5]), reverse=True)
    return nasdaq_payload(symbol, rows)


def october_even_years_fall(year, month):
    return -0.05 if month == 10 and year % 2 == 0 else 0.02


class TestNasdaqHistory:
    def test_real_rows_drop_exactly_the_filler_and_keep_the_odd_real_day(self):
        daily = bms.parse_nasdaq_index_history(nasdaq_payload("SOX", nasdaq_rows(SOX_REAL)))
        days = [day for day, _close in daily]
        assert days == sorted(days) and daily[0] == (date(1994, 5, 4), 119.0)
        assert all(day.year != 1990 for day in days)  # 第一列（前面沒有列可比）和後面的 479.49 都是填充
        assert not any(date(1994, 4, 29) <= day <= date(1994, 5, 3) for day in days)
        assert not any(day.year == 1996 and day.month in (1, 5) for day in days)  # 這兩個月整月都是填充
        assert (date(2010, 5, 17), 359.39) in daily  # 開高低 0 但收盤和前一天不同：真的交易日，不能刪
        assert daily[-3:] == [(date(2010, 5, 14), 353.86), (date(2010, 5, 17), 359.39), (date(2010, 5, 18), 348.85)]
        assert len(daily) == 16  # 29 列裡丟掉 13 列填充

    def test_filler_condition_is_close_equals_previous_and_all_three_zero(self):
        def row(day, close, open_="0.00", high="0.00", low="0.00"):
            return {"date": day, "close": close, "volume": "--", "open": open_, "high": high, "low": low}

        rows = [
            row("03/01/2020", "100.00", "99.00", "101.00", "98.00"),
            row("03/02/2020", "100.00"),  # 收盤同前一列、開高低全 0 → 填充，丟
            row("03/03/2020", "100.00", "100.00", "101.00", "99.00"),  # 收盤同前一列但開高低不是 0 → 留
            row("03/04/2020", "105.00"),  # 開高低全 0 但收盤變了 → 留
            row("03/05/2020", "105.00", "0.00", "106.00", "104.00"),  # 只有開是 0 → 留
        ]
        got = bms.parse_nasdaq_index_history(nasdaq_payload("SOX", list(reversed(rows))))
        assert [day.day for day, _ in got] == [1, 3, 4, 5]

    def test_soxx_etf_payload_has_the_same_shape_and_keeps_repeated_closes(self):
        daily = bms.parse_nasdaq_index_history(nasdaq_payload("SOXX", nasdaq_rows(SOXX_REAL)), symbol="SOXX")
        assert daily[0] == (date(2016, 10, 24), 37.4033) and len(daily) == len(SOXX_REAL)
        assert (date(2016, 12, 30), 40.9067) in daily and (date(2017, 1, 3), 40.9067) in daily

    @pytest.mark.parametrize(
        ("payload", "symbol", "kind"),
        [
            (N3_EMPTY, "SOXX", "empty"),  # 存檔的 2001–2016 回應：totalRecords 0、rows null
            (nasdaq_payload("SOX", []), "SOX", "empty"),
            (N3_EMPTY, "SOX", "format"),  # 代號不對（拿 SOXX 的檔當 SOX 指數）
            (nasdaq_payload("SOXX", nasdaq_rows(SOX_REAL)), "SOX", "format"),
            (nasdaq_payload("SOX", nasdaq_rows(SOX_REAL)), "SOXX", "format"),
            (nasdaq_payload("SOX", nasdaq_rows(SOX_REAL), rcode=400), "SOX", "format"),
            ({"data": {"symbol": "SOX", "tradesTable": {"rows": "x"}}, "status": {"rCode": 200}}, "SOX", "format"),
            ({"data": {"symbol": "SOX", "tradesTable": None}, "status": {"rCode": 200}}, "SOX", "empty"),
            ({"data": None, "status": {"rCode": 200}}, "SOX", "format"),
            ({"status": {"rCode": 200}}, "SOX", "format"),
            ({"data": {"symbol": "SOX", "tradesTable": {"rows": []}}}, "SOX", "format"),  # 沒有 status
            ([], "SOX", "format"),
            (nasdaq_payload("SOX", [{"date": "bad", "close": "x"}, {"date": "13/45/2020", "close": "1.00"}, 5]), "SOX", "empty"),
            (nasdaq_payload("SOX", nasdaq_rows([("03/02/2020", "100.00", "0.00", "0.00", "0.00")])), "SOX", "empty"),  # 只有填充列
        ],
    )
    def test_malformed_payloads_raise(self, payload, symbol, kind):
        with pytest.raises(ValueError, match=f"^{kind}"):
            bms.parse_nasdaq_index_history(payload, symbol=symbol)

    def test_two_closes_on_one_day_are_a_conflict_but_an_identical_repeat_is_fine(self):
        row = {"date": "03/02/2020", "close": "100.00", "volume": "--", "open": "99.00", "high": "101.00", "low": "98.00"}
        assert bms.parse_nasdaq_index_history(nasdaq_payload("SOX", [row, dict(row)])) == [(date(2020, 3, 2), 100.0)]
        with pytest.raises(ValueError, match="^conflict"):
            bms.parse_nasdaq_index_history(nasdaq_payload("SOX", [row, dict(row, close="101.00")]))

    def test_closes_with_thousands_separators_and_junk_rows(self):
        rows = [
            {"date": "10/22/2026", "close": "10,731.98", "volume": "--", "open": "11,049.07", "high": "11,140.33", "low": "10,666.41"},
            {"date": "10/21/2026", "close": "--", "volume": "--", "open": "1.00", "high": "1.00", "low": "1.00"},
            {"date": "10/20/2026", "close": "0.00", "volume": "--", "open": "1.00", "high": "1.00", "low": "1.00"},
            {"date": "10/19/2026", "close": "nan", "volume": "--", "open": "1.00", "high": "1.00", "low": "1.00"},
        ]
        assert bms.parse_nasdaq_index_history(nasdaq_payload("SOX", rows)) == [(date(2026, 10, 22), 10731.98)]


class TestMonthEndWithGap:
    def real_levels(self, **kwargs):
        daily = bms.parse_nasdaq_index_history(nasdaq_payload("SOX", nasdaq_rows(SOX_REAL)))
        return bms.month_end_levels(daily, **kwargs)

    def test_default_keeps_the_old_behavior_for_taiex(self):
        assert self.real_levels()[(1996, 4)] == 172.71  # 4/8 的值被當成 4 月底（不傳 max_gap_days＝加權指數的原行為）
        assert bms.month_end_levels([(date(1999, 1, 29), 1.0), (date(1999, 2, 26), 2.0), (date(1999, 3, 2), 3.0)]) == {
            (1999, 1): 1.0, (1999, 2): 2.0}

    def test_a_last_row_more_than_seven_days_before_month_end_is_not_a_month_end(self):
        levels = self.real_levels(max_gap_days=7)
        assert (1996, 4) not in levels  # 4/8 離 4/30 有 22 天
        assert levels[(1996, 7)] == 174.71 and levels[(1996, 6)] == 200.54  # 7/28 離月底 3 天、6/28 離 2 天：留
        assert (1996, 1) not in levels and (1996, 5) not in levels  # 整月都是填充：沒有列可以當月底
        later = (date(2020, 5, 5), 3.0)  # 後面有更晚的日子，3 月才算收完
        assert bms.month_end_levels([(date(2020, 3, 24), 1.0), later], max_gap_days=7) == {(2020, 3): 1.0}  # 剛好 7 天：留
        assert bms.month_end_levels([(date(2020, 3, 23), 1.0), later], max_gap_days=7) == {}  # 8 天：不列
        assert bms.month_end_levels([(date(2020, 3, 23), 1.0), later], max_gap_days=8) == {(2020, 3): 1.0}

    def test_the_five_filler_months_of_1996_have_no_monthly_change(self):
        levels = self.real_levels(max_gap_days=7)
        changes = bms.monthly_changes(levels, first=(1995, 12), end_year=1996, as_of=AS_OF)
        assert set(changes) == {(1995, 12), (1996, 3), (1996, 7), (1996, 8), (1996, 9)}
        missing = {(1996, m) for m in range(1, 13)} - set(changes)
        assert {(1996, m) for m in (1, 2, 4, 5, 6)} <= missing  # 1/2/4/5/6 月：補值列造成的假月份，不能出現
        assert changes[(1996, 3)] == pytest.approx(186.18 / 184.52 - 1)
        assert changes[(1996, 7)] == pytest.approx(174.71 / 200.54 - 1)

    def test_the_current_month_is_not_a_month_end(self):
        levels = chain((2026, 7), (2026, 10), lambda y, m: 0.01)
        daily = bms.parse_nasdaq_index_history(level_payload("SOX", levels, date(2026, 10, 22)))
        got = bms.month_end_levels(daily, max_gap_days=7)
        assert (2026, 9) in got and (2026, 10) not in got  # 10/22 還不是 10 月底


class TestSemiconductorSections:
    SOX_START, SOXX_START = (1994, 5), (2016, 10)

    def payloads(self, change=october_even_years_fall):
        sox = level_payload("SOX", chain(self.SOX_START, (2026, 10), change), date(2026, 10, 22))
        soxx = level_payload("SOXX", chain(self.SOXX_START, (2026, 10), change), date(2026, 10, 22))
        return sox, soxx

    def table(self, **kwargs):
        sox, soxx = self.payloads(**kwargs)
        return bms.build_table(
            synthetic_levels(1950, 2025, lambda y, m: 0.01), None, as_of=date(2026, 10, 24),
            sox_daily=bms.parse_nasdaq_index_history(sox),
            soxx_daily=bms.parse_nasdaq_index_history(soxx, symbol="SOXX"))

    def test_sox_section_covers_1994_06_to_2025_12_with_its_own_n(self):
        sox = self.table()["sox"]
        assert sox["pending_full_data"] is False and sox["sample"] == [1994, 2025]
        assert (sox["first_month"], sox["last_month"]) == ("1994-06", "2025-12")
        assert (sox["months_valid"], sox["months_expected"]) == (379, 379)
        assert sox["months"]["10"]["n"] == 32 and sox["q4"]["n"] == 32
        assert sox["midterm"]["years"] == [1994, 1998, 2002, 2006, 2010, 2014, 2018, 2022]
        assert sox["midterm"]["months"]["10"]["n"] == 8 and sox["midterm"]["q4"]["n"] == 8
        # 偶數年 10 月跌、奇數年漲：10 月上漲比例 50%；期中選舉年全是偶數年 → 0%
        assert sox["months"]["10"]["up_pct"] == 50.0 and sox["midterm"]["months"]["10"]["up_pct"] == 0.0
        # 1994-06 是第一個月變化：6–12 月有 1994 年（32 年），1–5 月從 1995 年起（31 年）
        assert sox["months"]["6"] == {"up_pct": 100.0, "median": 2.0, "mean": 2.0, "n": 32}
        assert sox["months"]["1"]["n"] == 31
        assert sox["basis"] == "月底收盤" and "SOX" in sox["source"] and sox["built_on"] == "2026-10-24"

    def test_soxx_section_starts_after_the_first_month_end_in_the_data(self):
        soxx = self.table()["soxx"]
        assert soxx["pending_full_data"] is False and soxx["sample"] == [2016, 2025]
        assert (soxx["first_month"], soxx["last_month"]) == ("2016-11", "2025-12")  # 2016-10 只有 10/24 起：它是 11 月的基期
        assert (soxx["months_valid"], soxx["months_expected"]) == (110, 110)
        assert soxx["months"]["10"]["n"] == 9 and soxx["q4"]["n"] == 9 and soxx["months"]["11"]["n"] == 10
        assert soxx["midterm"]["years"] == [2018, 2022] and soxx["midterm"]["months"]["10"]["n"] == 2
        assert soxx["basis"] == "月底收盤" and "SOXX" in soxx["source"]

    def test_soxx_first_month_follows_a_window_that_slides(self):
        levels = chain((2017, 10), (2026, 10), october_even_years_fall)
        soxx = bms.parse_nasdaq_index_history(level_payload("SOXX", levels, date(2026, 10, 22)), symbol="SOXX")
        table = bms.build_table(synthetic_levels(1950, 2025, lambda y, m: 0.01), None, as_of=date(2026, 10, 24),
                                soxx_daily=soxx)
        assert table["soxx"]["first_month"] == "2017-11" and table["soxx"]["sample"] == [2017, 2025]
        assert table["soxx"]["months"]["10"]["n"] == 8  # 沒有寫死 2016-11

    def test_sections_are_pending_without_data_or_with_too_few_months(self):
        sp = synthetic_levels(1950, 2025, lambda y, m: 0.01)
        table = bms.build_table(sp, None, as_of=AS_OF)
        for key in ("soxx", "sox"):
            assert table[key]["pending_full_data"] is True and "months" not in table[key] and table[key]["months_valid"] == 0
        assert list(table)[-2:] == ["soxx", "sox"] and table["schema"] == 1  # 新段接在 taiex 後面、schema 不升
        thin = bms.parse_nasdaq_index_history(
            level_payload("SOX", chain((2020, 1), (2026, 10), lambda y, m: 0.01), date(2026, 10, 22)))
        for daily in (thin, []):  # SOX 的起點是固定的 1994-06：只拿到 2020 年以後的資料＝缺資料，不是「短樣本」
            assert bms.build_table(sp, None, as_of=AS_OF, sox_daily=daily)["sox"]["pending_full_data"] is True
        assert bms.build_table(sp, None, as_of=AS_OF, soxx_daily=[])["soxx"]["pending_full_data"] is True

    def test_existing_sections_do_not_change_when_the_semiconductor_inputs_are_added(self):
        base = bms.build_table(synthetic_levels(1950, 2025, lambda y, m: 0.01), None, as_of=AS_OF)
        full = self.table()
        assert json.dumps(full["sp500"]) == json.dumps(base["sp500"]) and json.dumps(full["taiex"]) == json.dumps(base["taiex"])


class TestMergeInto:
    def committed_without_semiconductors(self) -> str:
        table = json.loads((HERE / "market_seasonality.json").read_text(encoding="utf-8"))
        table.pop("soxx", None)
        table.pop("sox", None)
        return json.dumps(table, ensure_ascii=False, indent=2) + "\n"

    def inputs(self, tmp_path):
        sox, soxx = TestSemiconductorSections().payloads()
        (tmp_path / "sox.json").write_text(json.dumps(sox), encoding="utf-8")
        (tmp_path / "soxx.json").write_text(json.dumps(soxx), encoding="utf-8")
        base = tmp_path / "market_seasonality.json"
        base.write_text(self.committed_without_semiconductors(), encoding="utf-8")
        return base, ["--merge-into", str(base), "--sox-json", str(tmp_path / "sox.json"),
                      "--soxx-json", str(tmp_path / "soxx.json"), "--as-of", "2026-10-24"]

    def test_merge_only_appends_the_two_sections_and_leaves_every_other_byte_alone(self, tmp_path):
        base, args = self.inputs(tmp_path)
        before = base.read_text(encoding="utf-8")
        assert bms.main(args) == 0  # 不給 --sp500-csv、--taiex-json：那兩份輸入已經不在磁碟上
        after = base.read_text(encoding="utf-8")
        head = before[: before.rindex("\n}")]  # taiex 之前（含 taiex）的每一個位元組
        assert after.startswith(head + ',\n  "soxx": {') and after.endswith("}\n")
        merged, original = json.loads(after), json.loads(before)
        assert [key for key in merged if key not in original] == ["soxx", "sox"]
        for key in original:
            assert json.dumps(merged[key], ensure_ascii=False) == json.dumps(original[key], ensure_ascii=False), key
        assert merged["built_on"] == "2026-10-17" and merged["note"] == market_calc.TEXT_SEASON_NOTE
        assert merged["sox"]["built_on"] == "2026-10-24" and merged["sox"]["months"]["10"]["n"] == 32
        assert {p.name for p in tmp_path.iterdir()} == {"market_seasonality.json", "sox.json", "soxx.json"}  # 暫存檔有收乾淨

    def test_merging_twice_is_idempotent_and_a_single_input_updates_only_that_section(self, tmp_path):
        base, args = self.inputs(tmp_path)
        assert bms.main(args) == 0
        once = base.read_text(encoding="utf-8")
        assert bms.main(args) == 0 and base.read_text(encoding="utf-8") == once
        sox_only = ["--merge-into", str(base), "--sox-json", str(tmp_path / "sox.json"), "--as-of", "2026-11-02"]
        assert bms.main(sox_only) == 0
        merged = json.loads(base.read_text(encoding="utf-8"))
        assert merged["sox"]["built_on"] == "2026-11-02" and merged["soxx"]["built_on"] == "2026-10-24"
        assert list(merged)[-2:] == ["soxx", "sox"]

    def test_bad_combinations_are_refused_before_anything_is_written(self, tmp_path):
        base, args = self.inputs(tmp_path)
        before = base.read_bytes()
        refused = [
            ["--merge-into", str(base), "--as-of", "2026-10-24"],  # 沒有任何 payload
            args + ["--out", str(tmp_path / "x.json")],  # 合併是就地更新
            args + ["--sp500-csv", str(tmp_path / "sp.csv")],  # 合併不重算 S&P、加權
            args + ["--taiex-json", str(tmp_path / "tw.json")],
            ["--sox-json", str(tmp_path / "sox.json"), "--as-of", "2026-10-24"],  # 完整重建沒給 --sp500-csv
        ]
        for argv in refused:
            with pytest.raises(SystemExit):
                bms.main(argv)
        with pytest.raises(ValueError, match="end_year"):  # 樣本截止年和既有的 S&P 段不同：不合併
            bms.main(args[:-1] + ["2027-02-01"])
        missing = tmp_path / "nope" / "market_seasonality.json"
        with pytest.raises(FileNotFoundError):
            bms.main(["--merge-into", str(missing), "--sox-json", str(tmp_path / "sox.json"), "--as-of", "2026-10-24"])
        assert base.read_bytes() == before

    def test_a_base_that_is_not_the_seasonality_table_is_refused(self, tmp_path):
        base, args = self.inputs(tmp_path)
        for text in ('{"schema": 2, "sp500": {}, "taiex": {}}', '{"schema": 1}', "[]", "not json"):
            base.write_text(text, encoding="utf-8")
            with pytest.raises(ValueError):
                bms.main(args)
            assert base.read_text(encoding="utf-8") == text

    def test_the_soxx_payload_for_the_wrong_symbol_is_refused(self, tmp_path):
        base, args = self.inputs(tmp_path)
        before = base.read_bytes()
        (tmp_path / "soxx.json").write_text(json.dumps(nasdaq_payload("SOX", nasdaq_rows(SOX_REAL))), encoding="utf-8")
        with pytest.raises(ValueError, match="^format"):
            bms.main(args)
        assert base.read_bytes() == before


# 下面兩張表是另外用獨立的腳本（不 import 本專案任何模組）從存檔的 Nasdaq 回應算的：SOX 用 n1（9,501 列）、
# SOXX 用 n2（2,513 列），月底收盤、離月底超過 7 天不列、填充列先丟；欄位依序是 (上漲 %, 中位數 %, 平均 %, 年數)。
# 審查（第 2、3 席）也各自重算過：SOX 10 月 62.5／+3.34（32 年）、第四季 78.1／+9.39、期中年 10 月 87.5（8 年）；
# 重疊的 2017–2025 年 SOXX 10 月 66.7／+2.43。
SOX_MONTHS = {
    1: (60.0, 2.66, 3.68, 30), 2: (63.3, 2.92, 2.69, 30), 3: (54.8, 0.9, 1.35, 31), 4: (46.7, -0.31, 1.55, 30),
    5: (66.7, 5.8, 2.46, 30), 6: (41.9, -1.1, 0.6, 31), 7: (59.4, 1.98, 1.57, 32), 8: (50.0, 0.47, -0.63, 32),
    9: (46.9, -0.45, -2.2, 32), 10: (62.5, 3.34, 2.53, 32), 11: (65.6, 3.54, 3.49, 32), 12: (62.5, 1.16, 2.04, 32),
}
SOX_MIDTERM_MONTHS = {
    1: (57.1, 7.03, 1.83, 7), 2: (57.1, 0.04, 1.45, 7), 3: (42.9, -0.02, 2.32, 7), 4: (42.9, -1.84, -3.27, 7),
    5: (42.9, -5.43, -3.11, 7), 6: (12.5, -5.06, -6.67, 8), 7: (62.5, 3.15, 0.72, 8), 8: (50.0, -3.41, -3.43, 8),
    9: (37.5, -1.75, -2.22, 8), 10: (87.5, 5.04, 7.07, 8), 11: (87.5, 5.85, 9.83, 8), 12: (50.0, -1.08, -2.58, 8),
}
SOXX_MONTHS = {
    1: (77.8, 3.23, 3.36, 9), 2: (66.7, 1.52, 2.07, 9), 3: (55.6, 1.7, -0.29, 9), 4: (22.2, -2.3, -1.27, 9),
    5: (88.9, 8.73, 6.2, 9), 6: (66.7, 5.39, 2.79, 9), 7: (88.9, 4.84, 4.49, 9), 8: (55.6, 2.2, -0.22, 9),
    9: (33.3, -0.85, -1.23, 9), 10: (66.7, 2.43, 1.49, 9), 11: (70.0, 5.63, 7.48, 10), 12: (70.0, 1.98, 1.27, 10),
}


def as_tuple(stats):
    return (stats["up_pct"], stats["median"], stats["mean"], stats["n"])


class TestCommittedSemiconductorSections:
    def table(self):
        return json.loads((HERE / "market_seasonality.json").read_text(encoding="utf-8"))

    def test_the_new_sections_sit_after_taiex_and_the_old_ones_are_untouched(self):
        table = self.table()
        assert list(table) == ["schema", "built_on", "note", "sp500", "taiex", "soxx", "sox"]
        assert table["schema"] == 1 and table["built_on"] == "2026-10-17" and table["note"] == market_calc.TEXT_SEASON_NOTE
        assert "勝率" not in table["note"]
        assert table["sp500"]["months"]["1"] == {"up_pct": 67.1, "median": 1.46, "mean": 1.41, "n": 76}
        text = (HERE / "market_seasonality.json").read_text(encoding="utf-8")
        assert text.endswith('  }\n}\n') and text.count('"pending_full_data"') == 4

    def test_sox_section_matches_the_saved_payload(self):
        sox = self.table()["sox"]
        assert sox["source"] == "Nasdaq 費城半導體指數（SOX）日線的月底收盤" and sox["basis"] == "月底收盤"
        assert sox["sample"] == [1994, 2025] and (sox["first_month"], sox["last_month"]) == ("1994-06", "2025-12")
        assert (sox["months_valid"], sox["months_expected"]) == (374, 379)  # 缺的 5 個月：1996 年 1、2、4、5、6 月（補值列）
        assert sox["pending_full_data"] is False and sox["built_on"] == "2026-10-25"
        assert {int(m): as_tuple(s) for m, s in sox["months"].items()} == SOX_MONTHS
        assert as_tuple(sox["q4"]) == (78.1, 9.39, 8.77, 32)
        assert sox["midterm"]["years"] == [1994, 1998, 2002, 2006, 2010, 2014, 2018, 2022]
        assert {int(m): as_tuple(s) for m, s in sox["midterm"]["months"].items()} == SOX_MIDTERM_MONTHS
        assert as_tuple(sox["midterm"]["q4"]) == (87.5, 9.27, 14.71, 8)

    def test_soxx_section_is_the_nine_years_nasdaq_still_serves(self):
        soxx = self.table()["soxx"]
        assert soxx["source"] == "Nasdaq SOXX（iShares 半導體 ETF）日線的月底收盤" and soxx["basis"] == "月底收盤"
        assert soxx["sample"] == [2016, 2025] and (soxx["first_month"], soxx["last_month"]) == ("2016-11", "2025-12")
        assert (soxx["months_valid"], soxx["months_expected"]) == (110, 110)
        assert soxx["pending_full_data"] is False and soxx["built_on"] == "2026-10-25"
        assert {int(m): as_tuple(s) for m, s in soxx["months"].items()} == SOXX_MONTHS
        assert as_tuple(soxx["q4"]) == (77.8, 11.08, 10.23, 9)
        assert soxx["midterm"]["years"] == [2018, 2022] and soxx["midterm"]["months"]["10"]["n"] == 2  # 只有 2 年：卡上不放
