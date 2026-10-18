"""季節性靜態表（/盤勢 季節性，2026-10-17）：從 datahub S&P 500 月資料與 FinMind TAIEX 日線產生 market_seasonality.json。

Andrew 2026-10-17 要把「季節效應」做進盤勢卡（原話：「你再試試看…我覺得可以做到」）；這張表只在每年維護時
重算一次、放進 repo，bot 與 job 都不連網重算。口徑（PLAN v6.2 §3.3、§18.15）：
- S&P 500：datahub `core/s-and-p-500` 的 Shiller 月均價（SP500 欄）；月變化＝當月均價／前月均價−1（標
  「月均價口徑」）；第四季＝12 月均價／9 月均價−1；樣本寫死從 1950 年起。datahub 近年的 Dividend／Earnings／
  CPI 是 0.0 佔位，這裡只用 SP500 欄，SP500 ≤0 也當缺值。
- 加權指數：FinMind TaiwanStockPrice（stock_id=TAIEX）日線取月底收盤；月變化＝月底收盤／前月底收盤−1；
  第一個完整月份寫死 1999-02；只有資料裡出現更晚日期的月份才當「已收完」（月底收盤才可信）。
- 每個樣本兩端月份都要有值，N 從有效樣本算；還沒結束的月份（月底 ≥ as_of）不算；期中選舉年（年 %4==2）
  的同月與第四季另列、各自的 N（卡上只在期中選舉年顯示）。
- 樣本截止年預設 as_of 的前一年（年度維護：隔年初重算）。加權指數的有效月份不到應有的 90%（例如只拿到
  裁小的 fixture）→ 不算統計，整段標 `pending_full_data: true`，等拿到完整 FinMind 資料再重算。

用法（維護時）：
  .venv/bin/python jobs/build_market_seasonality.py --sp500-csv s-and-p-500.csv \\
      --taiex-json finmind_taiex.json --as-of 2027-01-05 --out market_seasonality.json
純函式不碰檔案，import 時不做 I/O；只有 main() 讀寫檔。
"""

from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import math
import os
import statistics
import sys
import tempfile
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import market_calc  # noqa: E402  （只拿固定句與期中選舉年的定義，純常數、不做 I/O）

YearMonth = tuple[int, int]

SCHEMA = 1
SP_FIRST: YearMonth = (1950, 1)
TW_FIRST: YearMonth = (1999, 2)
MIN_COVERAGE = 0.90
SP_SOURCE = "datahub core/s-and-p-500（Shiller 月均價）"
SP_BASIS = "月均價"
TW_SOURCE = "FinMind TaiwanStockPrice（TAIEX 日線的月底收盤）"
TW_BASIS = "月底收盤"


# ── 解析 ──────────────────────────────────────────────────────────────────
def parse_sp500_csv(text: str) -> dict[YearMonth, float]:
    """datahub CSV（Date,SP500,Dividend,…）→ {(年, 月): 月均價}；只看 SP500 欄，≤0 或不是數字的當缺值。"""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Date" not in reader.fieldnames or "SP500" not in reader.fieldnames:
        raise ValueError("format: missing Date/SP500 header")
    levels: dict[YearMonth, float] = {}
    for row in reader:
        raw_date, raw_value = (row.get("Date") or "").strip(), (row.get("SP500") or "").strip()
        try:
            day = date.fromisoformat(raw_date)
            value = float(raw_value)
        except ValueError:
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        key = (day.year, day.month)
        if key in levels and levels[key] != value:
            raise ValueError(f"conflict: two values for {key[0]}-{key[1]:02d}")
        levels[key] = value
    if not levels:
        raise ValueError("empty: no usable SP500 rows")
    return levels


def parse_finmind_daily(payload: Mapping[str, Any], stock_id: str = "TAIEX") -> list[tuple[date, float]]:
    """FinMind TaiwanStockPrice 回應 → [(日期, 收盤)]（依日期排序）；同一天兩個不同收盤 → 整份拒收。"""
    if not isinstance(payload, Mapping):
        raise ValueError("format: payload is not an object")
    if payload.get("msg", "success") != "success" or payload.get("status", 200) != 200:
        raise ValueError("format: FinMind status is not success")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("format: data is not a list")
    closes: dict[date, float] = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("stock_id") != stock_id:
            continue
        try:
            day = date.fromisoformat(str(row.get("date")))
        except ValueError:
            continue
        value = row.get("close")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            continue
        if day in closes and closes[day] != float(value):
            raise ValueError(f"conflict: two closes on {day.isoformat()}")
        closes[day] = float(value)
    if not closes:
        raise ValueError("empty: no usable rows")
    return sorted(closes.items())


def month_end_levels(daily: Iterable[tuple[date, float]]) -> dict[YearMonth, float]:
    """每個月最後一個交易日的收盤；資料裡沒有更晚的日期 → 那個月還不能確定收完，不列。"""
    ordered = sorted(daily)
    if not ordered:
        return {}
    last_day = ordered[-1][0]
    levels: dict[YearMonth, float] = {}
    for day, close in ordered:
        levels[(day.year, day.month)] = close
    return {
        key: value
        for key, value in levels.items()
        if date(key[0], key[1], calendar.monthrange(key[0], key[1])[1]) < last_day
    }


# ── 計算 ──────────────────────────────────────────────────────────────────
def _prev(key: YearMonth) -> YearMonth:
    year, month = key
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_finished(key: YearMonth, as_of: date) -> bool:
    """月底 < as_of 才算結束（as_of 當天所在的月份不算）。"""
    return date(key[0], key[1], calendar.monthrange(key[0], key[1])[1]) < as_of


def months_in_sample(first: YearMonth, end_year: int) -> list[YearMonth]:
    out: list[YearMonth] = []
    year, month = first
    while year <= end_year:
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def monthly_changes(
    levels: Mapping[YearMonth, float], *, first: YearMonth, end_year: int, as_of: date
) -> dict[YearMonth, float]:
    """{(年, 月): 當月值／前月值−1}；兩端月份都要有值、當月要已經結束。"""
    out: dict[YearMonth, float] = {}
    for key in months_in_sample(first, end_year):
        prev = _prev(key)
        if not month_finished(key, as_of) or key not in levels or prev not in levels:
            continue
        out[key] = levels[key] / levels[prev] - 1.0
    return out


def q4_changes(levels: Mapping[YearMonth, float], *, years: Iterable[int], as_of: date) -> dict[int, float]:
    """{年: 12 月值／9 月值−1}；12 月要已經結束。"""
    out: dict[int, float] = {}
    for year in years:
        dec, sep = (year, 12), (year, 9)
        if month_finished(dec, as_of) and dec in levels and sep in levels:
            out[year] = levels[dec] / levels[sep] - 1.0
    return out


def summarize(changes: Iterable[float]) -> dict[str, Any] | None:
    """上漲比例（%，1 位）、中位數與平均（%，2 位）、N；沒有樣本 → None。"""
    values = list(changes)
    if not values:
        return None
    return {
        "up_pct": round(100.0 * sum(1 for v in values if v > 0) / len(values), 1),
        "median": round(100.0 * statistics.median(values), 2),
        "mean": round(100.0 * math.fsum(values) / len(values), 2),
        "n": len(values),
    }


def _by_month(changes: Mapping[YearMonth, float], years: set[int] | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for month in range(1, 13):
        stats = summarize(v for (y, m), v in sorted(changes.items()) if m == month and (years is None or y in years))
        if stats is not None:
            out[str(month)] = stats
    return out


def build_section(
    levels: Mapping[YearMonth, float],
    *,
    first: YearMonth,
    end_year: int,
    as_of: date,
    source: str,
    basis: str,
) -> dict[str, Any]:
    """一個指數的統計；有效月份不到應有的 90% → pending_full_data（不放統計，免得用殘缺樣本冒充）。"""
    expected = [key for key in months_in_sample(first, end_year) if month_finished(key, as_of)]
    changes = monthly_changes(levels, first=first, end_year=end_year, as_of=as_of)
    section: dict[str, Any] = {
        "source": source,
        "basis": basis,
        "sample": [first[0], end_year],
        "first_month": f"{first[0]}-{first[1]:02d}",
        "last_month": f"{expected[-1][0]}-{expected[-1][1]:02d}" if expected else None,
        "months_valid": len(changes),
        "months_expected": len(expected),
    }
    if not expected or len(changes) < MIN_COVERAGE * len(expected):
        section["pending_full_data"] = True
        return section
    years = range(first[0], end_year + 1)
    midterm_years = [year for year in years if market_calc.is_midterm_year(year)]
    q4 = q4_changes(levels, years=years, as_of=as_of)
    section["pending_full_data"] = False
    section["months"] = _by_month(changes)
    section["q4"] = summarize(q4.values())
    section["midterm"] = {
        "years": midterm_years,
        "months": _by_month(changes, set(midterm_years)),
        "q4": summarize(v for year, v in sorted(q4.items()) if year in set(midterm_years)),
    }
    return section


def build_table(
    sp500_levels: Mapping[YearMonth, float],
    taiex_daily: Iterable[tuple[date, float]] | None,
    *,
    as_of: date,
    end_year: int | None = None,
) -> dict[str, Any]:
    """整張表；S&P 必須完整（不完整就 raise），加權指數不完整只標 pending_full_data。"""
    end_year = as_of.year - 1 if end_year is None else end_year
    sp = build_section(sp500_levels, first=SP_FIRST, end_year=end_year, as_of=as_of, source=SP_SOURCE, basis=SP_BASIS)
    if sp["pending_full_data"]:
        raise ValueError("too_few: S&P 500 months below 90% of the sample")
    tw_levels = month_end_levels(taiex_daily) if taiex_daily is not None else {}
    tw = build_section(tw_levels, first=TW_FIRST, end_year=end_year, as_of=as_of, source=TW_SOURCE, basis=TW_BASIS)
    return {
        "schema": SCHEMA,
        "built_on": as_of.isoformat(),
        "note": market_calc.TEXT_SEASON_NOTE,
        "sp500": sp,
        "taiex": tw,
    }


# ── CLI ────────────────────────────────────────────────────────────────────
def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)  # repo 內的靜態檔，跟其他原始碼一樣可讀
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建 /盤勢 的季節性靜態表")
    parser.add_argument("--sp500-csv", required=True, type=Path, help="datahub core/s-and-p-500 的 CSV")
    parser.add_argument("--taiex-json", type=Path, help="FinMind TaiwanStockPrice TAIEX 的完整回應（JSON）")
    parser.add_argument("--as-of", required=True, type=date.fromisoformat, help="重算日（YYYY-MM-DD）")
    parser.add_argument("--end-year", type=int, help="樣本截止年（預設 as-of 的前一年）")
    parser.add_argument("--out", type=Path, help="輸出檔（不給就印到 stdout）")
    args = parser.parse_args(argv)
    levels = parse_sp500_csv(args.sp500_csv.read_text(encoding="utf-8"))
    daily = None
    if args.taiex_json is not None:
        daily = parse_finmind_daily(json.loads(args.taiex_json.read_text(encoding="utf-8")))
    table = build_table(levels, daily, as_of=args.as_of, end_year=args.end_year)
    text = json.dumps(table, ensure_ascii=False, indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        _write_atomic(args.out, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
