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
- 半導體兩段（2026-10-24 第 9 輪 A14，Andrew 第 3 項「季節性要包含 SOXX」）：吃 api.nasdaq.com 的日線回應原檔
  （`--soxx-json`、`--sox-json`，不重抓），一樣取月底收盤。SOXX＝ETF 自身（Nasdaq 的 ETF 歷史固定只給最近 10 年，
  第一個月變化從資料的第一個月底的「下一個月」算起，所以起點會隨每年重建往後滑）；SOX＝費城半導體指數（真值從
  1994-05-04，第一個月變化 1994-06）。Nasdaq 的日線有「填充列」：1994-05-04 以前整段是固定的 479.49，之後還有
  休市日或沒更新的日子沿用前一筆；判斷條件是「收盤＝前一列收盤且開高低全為 0」（前面沒有列可比的第一列，開高低全 0
  也算填充）。開高低是 0 但收盤和前一列不同的是真實交易日（例 2010-05-17），要留著。月底那一列離月底超過
  7 天（1996 年 Nasdaq 只有每週更新）的月份不列，所以 SOX 有 5 個月變化（1996-01、02、04、05、06）算不出來，
  其餘月份不受影響。SOX 與 SOXX 的段都只是「近似」關係：SOXX 在 2010-10-15 前追蹤別的指數、2010-10-15 到
  2021-06-21 追蹤 SOX、之後追蹤 NYSE 半導體指數（iShares 產品頁，2026-10-23 存檔）。

用法（維護時）：
  # 全部重建（要 S&P 的 CSV 與加權指數的 FinMind JSON；半導體兩段可以一起給）
  .venv/bin/python jobs/build_market_seasonality.py --sp500-csv s-and-p-500.csv \\
      --taiex-json finmind_taiex.json --sox-json nasdaq_sox.json --soxx-json nasdaq_soxx.json \\
      --as-of 2027-01-05 --out market_seasonality.json
  # 只更新半導體兩段（S&P、加權兩段和其他每個位元組原樣不動，不需要它們的輸入檔；就地、原子寫入）
  .venv/bin/python jobs/build_market_seasonality.py --merge-into market_seasonality.json \\
      --sox-json nasdaq_sox.json --soxx-json nasdaq_soxx.json --as-of 2027-01-05
  Nasdaq 的回應用 curl 存檔（SOX 全歷史約 1 MB，超過 market_http 對 nasdaq_historical 的 512 KiB 上限，不要走它）：
  curl -A '<瀏覽器 UA>' 'https://api.nasdaq.com/api/quote/SOX/historical?assetclass=index&fromdate=1994-05-01&todate=<as-of 前一天>&limit=9999' -o nasdaq_sox.json
  curl -A '<瀏覽器 UA>' 'https://api.nasdaq.com/api/quote/SOXX/historical?assetclass=etf&fromdate=<10 年前>&todate=<as-of 前一天>&limit=9999' -o nasdaq_soxx.json
  改了這個 json 就要跑 deploy/rehash.sh：它在 job 的 code hash 裡（jobs/market_snapshot.py 的 code_files）。
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
import re
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
# 半導體兩段（2026-10-24 第 9 輪 A14）
SOX_FIRST: YearMonth = (1994, 6)  # 真值從 1994-05-04：1994-05 是第一個月底、1994-06 是第一個月變化
SOX_SOURCE = "Nasdaq 費城半導體指數（SOX）日線的月底收盤"
SOXX_SOURCE = "Nasdaq SOXX（iShares 半導體 ETF）日線的月底收盤"
SEMI_BASIS = "月底收盤"
SEMI_MAX_GAP_DAYS = 7  # 月底那一列離月底超過這麼多天 → 該月不列（1996 年的 SOX 只有每週更新）
SOXX_WINDOW_YEARS = 10  # Nasdaq 的 ETF 歷史固定只給最近 10 年；只用在「還沒有資料」時佔位的起點


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


_NASDAQ_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _nasdaq_number(value: Any) -> float | None:
    """「10,731.98」→ 10731.98；「--」、空字串、不是數字、不是有限數 → None。"""
    if not isinstance(value, str):
        return None
    try:
        number = float(value.replace(",", "").strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_nasdaq_index_history(payload: Mapping[str, Any], symbol: str = "SOX") -> list[tuple[date, float]]:
    """api.nasdaq.com `quote/<symbol>/historical` 的回應（指數 assetclass=index、ETF assetclass=etf 同一種形狀）
    → [(日期, 收盤)]（依日期排序）。

    - status.rCode 要是 200、data.symbol 要是 `symbol`、rows 要是清單，否則 raise ValueError（`format:`）；
      rows 是 null（例：存檔的 SOXX 2001–2016 回應 totalRecords 0）→ `empty:`。
    - 日期 MM/DD/YYYY；收盤去掉千分位逗號；日期或收盤讀不出來、收盤 ≤0 的列略過；同一天兩個不同收盤 → `conflict:`。
    - 填充列（Nasdaq 對休市日或沒更新的日子補的列）：**收盤＝前一列收盤，而且開高低全為 0** → 丟掉。前面沒有列可比的
      第一列，開高低全 0 也丟（存檔的第一列 1990-01-01 就是）。開高低是 0 但收盤和前一列不同＝真實的交易日
      （SOX 2010-05-17 收盤 359.39），要留；收盤和前一列相同但開高低不是 0（SOXX 2016-12-30 和 2017-01-03）也留。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("format: payload is not an object")
    status = payload.get("status")
    if not isinstance(status, Mapping) or status.get("rCode") != 200:
        raise ValueError("format: Nasdaq status is not 200")
    data = payload.get("data")
    if not isinstance(data, Mapping) or data.get("symbol") != symbol:
        raise ValueError(f"format: payload is not {symbol}")
    trades = data.get("tradesTable")
    rows = trades.get("rows") if isinstance(trades, Mapping) else None
    if rows is None:
        raise ValueError("empty: no rows")
    if not isinstance(rows, list):
        raise ValueError("format: rows is not a list")
    seen: dict[date, tuple[float, bool]] = {}  # 日期 → (收盤, 開高低全為 0)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        match = _NASDAQ_DATE_RE.fullmatch(str(row.get("date") or "").strip())
        if match is None:
            continue
        try:
            day = date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        except ValueError:
            continue
        close = _nasdaq_number(row.get("close"))
        if close is None or close <= 0:
            continue
        zero = all(_nasdaq_number(row.get(key)) == 0.0 for key in ("open", "high", "low"))
        if day in seen and seen[day][0] != close:
            raise ValueError(f"conflict: two closes on {day.isoformat()}")
        seen.setdefault(day, (close, zero))
    if not seen:
        raise ValueError("empty: no usable rows")
    daily: list[tuple[date, float]] = []
    previous: float | None = None
    for day in sorted(seen):
        close, zero = seen[day]
        if not (zero and (previous is None or close == previous)):
            daily.append((day, close))
        previous = close  # 比的是原始資料的前一列（被丟掉的填充列也算），不是留下來的前一列
    if not daily:
        raise ValueError("empty: only filler rows")
    return daily


def month_end_levels(
    daily: Iterable[tuple[date, float]], *, max_gap_days: int | None = None
) -> dict[YearMonth, float]:
    """每個月最後一個交易日的收盤；資料裡沒有更晚的日期 → 那個月還不能確定收完，不列。

    max_gap_days 不是 None：那一列離月底（日曆日）超過這麼多天，它就不是「月底收盤」，該月不列
    （加權指數不傳，行為不變）。"""
    ordered = sorted(daily)
    if not ordered:
        return {}
    last_day = ordered[-1][0]
    levels: dict[YearMonth, float] = {}
    last_seen: dict[YearMonth, date] = {}
    for day, close in ordered:
        levels[(day.year, day.month)] = close
        last_seen[(day.year, day.month)] = day
    out: dict[YearMonth, float] = {}
    for key, value in levels.items():
        month_end = date(key[0], key[1], calendar.monthrange(key[0], key[1])[1])
        if month_end >= last_day:
            continue
        if max_gap_days is not None and (month_end - last_seen[key]).days > max_gap_days:
            continue
        out[key] = value
    return out


def first_change_month(levels: Mapping[YearMonth, float]) -> YearMonth | None:
    """第一個算得出月變化的月份：有月底收盤的最早月份的下一個月（它的前一個月底在資料裡）。"""
    if not levels:
        return None
    year, month = min(levels)
    return (year + 1, 1) if month == 12 else (year, month + 1)


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


def build_semiconductor_section(
    daily: Iterable[tuple[date, float]] | None,
    *,
    as_of: date,
    end_year: int,
    source: str,
    first: YearMonth | None = None,
) -> dict[str, Any]:
    """SOX／SOXX 的一段（月底收盤，離月底超過 7 天的月份不列）。

    first＝第一個月變化的月份：SOX 固定 1994-06（缺資料時有效月份不到 90% → pending）；不給（SOXX）就從資料的第一個
    月底的下一個月算起——Nasdaq 的 ETF 歷史是固定 10 年的滑動視窗，起點每年往後移，不能寫死。完全沒有資料時用
    「截止年往前 10 年」佔位，整段 pending。`built_on` 是這一段的建置日（用 --merge-into 時各段可以不同天）。"""
    levels = month_end_levels(daily, max_gap_days=SEMI_MAX_GAP_DAYS) if daily else {}
    start = first or first_change_month(levels) or (end_year - SOXX_WINDOW_YEARS + 1, 1)
    section = build_section(levels, first=start, end_year=end_year, as_of=as_of, source=source, basis=SEMI_BASIS)
    section["built_on"] = as_of.isoformat()
    return section


def build_table(
    sp500_levels: Mapping[YearMonth, float],
    taiex_daily: Iterable[tuple[date, float]] | None,
    *,
    as_of: date,
    end_year: int | None = None,
    sox_daily: Iterable[tuple[date, float]] | None = None,
    soxx_daily: Iterable[tuple[date, float]] | None = None,
) -> dict[str, Any]:
    """整張表；S&P 必須完整（不完整就 raise），加權指數與半導體兩段不完整只標 pending_full_data。"""
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
        "soxx": build_semiconductor_section(soxx_daily, as_of=as_of, end_year=end_year, source=SOXX_SOURCE),
        "sox": build_semiconductor_section(
            sox_daily, as_of=as_of, end_year=end_year, source=SOX_SOURCE, first=SOX_FIRST
        ),
    }


def merge_semiconductors(
    table: Mapping[str, Any],
    *,
    as_of: date,
    end_year: int | None = None,
    sox_daily: Iterable[tuple[date, float]] | None = None,
    soxx_daily: Iterable[tuple[date, float]] | None = None,
) -> dict[str, Any]:
    """把 SOXX／SOX 段併進既有的整張表：只有傳進來的那段會換（沒有的接在最後），其他每個鍵（含 sp500、taiex）
    是同一個物件、原樣不動——S&P 與加權的輸入檔已經不在磁碟上，沒辦法重算。

    樣本截止年要和既有 S&P 段的一樣（job 用 S&P 段的截止年算這一列何時過期）；不一樣 → ValueError。"""
    if not isinstance(table, Mapping) or table.get("schema") != SCHEMA:
        raise ValueError("format: not a seasonality table (schema)")
    sp, tw = table.get("sp500"), table.get("taiex")
    if not isinstance(sp, Mapping) or not isinstance(tw, Mapping):
        raise ValueError("format: the sp500 and taiex sections are required")
    if sox_daily is None and soxx_daily is None:
        raise ValueError("empty: nothing to merge")
    end_year = as_of.year - 1 if end_year is None else end_year
    sample = sp.get("sample")
    if not (isinstance(sample, list) and len(sample) == 2 and sample[1] == end_year):
        raise ValueError(f"end_year: the S&P section ends in {sample!r} but this build ends in {end_year}")
    merged = dict(table)
    if soxx_daily is not None:
        merged["soxx"] = build_semiconductor_section(soxx_daily, as_of=as_of, end_year=end_year, source=SOXX_SOURCE)
    if sox_daily is not None:
        merged["sox"] = build_semiconductor_section(
            sox_daily, as_of=as_of, end_year=end_year, source=SOX_SOURCE, first=SOX_FIRST
        )
    return merged


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


def _dump(table: Mapping[str, Any]) -> str:
    return json.dumps(table, ensure_ascii=False, indent=2) + "\n"


def _load_nasdaq(path: Path | None, symbol: str) -> list[tuple[date, float]] | None:
    if path is None:
        return None
    return parse_nasdaq_index_history(json.loads(path.read_text(encoding="utf-8")), symbol=symbol)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建 /盤勢 的季節性靜態表")
    parser.add_argument("--sp500-csv", type=Path, help="datahub core/s-and-p-500 的 CSV（完整重建時必給）")
    parser.add_argument("--taiex-json", type=Path, help="FinMind TaiwanStockPrice TAIEX 的完整回應（JSON）")
    parser.add_argument("--sox-json", type=Path, help="api.nasdaq.com 的 SOX 指數日線回應（存檔的原始 payload，不重抓）")
    parser.add_argument("--soxx-json", type=Path, help="api.nasdaq.com 的 SOXX ETF 日線回應（存檔的原始 payload，不重抓）")
    parser.add_argument(
        "--merge-into", type=Path,
        help="既有的 market_seasonality.json：只更新 --sox-json／--soxx-json 給的那幾段（就地、原子寫入），其他位元組不動",
    )
    parser.add_argument("--as-of", required=True, type=date.fromisoformat, help="重算日（YYYY-MM-DD）")
    parser.add_argument("--end-year", type=int, help="樣本截止年（預設 as-of 的前一年）")
    parser.add_argument("--out", type=Path, help="輸出檔（不給就印到 stdout）")
    args = parser.parse_args(argv)
    if args.merge_into is not None:
        if args.out is not None or args.sp500_csv is not None or args.taiex_json is not None:
            parser.error("--merge-into 只更新半導體兩段、就地寫回：不能和 --sp500-csv、--taiex-json、--out 一起用")
        if args.sox_json is None and args.soxx_json is None:
            parser.error("--merge-into 要給 --sox-json 或 --soxx-json（至少一個）")
        table = json.loads(args.merge_into.read_text(encoding="utf-8"))
        sox = _load_nasdaq(args.sox_json, "SOX")  # 兩份都解析完、併好才寫：任何一份壞掉都不會留下半個結果
        soxx = _load_nasdaq(args.soxx_json, "SOXX")
        merged = merge_semiconductors(table, as_of=args.as_of, end_year=args.end_year, sox_daily=sox, soxx_daily=soxx)
        _write_atomic(args.merge_into, _dump(merged))
        return 0
    if args.sp500_csv is None:
        parser.error("完整重建要給 --sp500-csv（只更新半導體兩段請用 --merge-into）")
    levels = parse_sp500_csv(args.sp500_csv.read_text(encoding="utf-8"))
    daily = None
    if args.taiex_json is not None:
        daily = parse_finmind_daily(json.loads(args.taiex_json.read_text(encoding="utf-8")))
    table = build_table(
        levels, daily, as_of=args.as_of, end_year=args.end_year,
        sox_daily=_load_nasdaq(args.sox_json, "SOX"), soxx_daily=_load_nasdaq(args.soxx_json, "SOXX"),
    )
    text = _dump(table)
    if args.out is None:
        sys.stdout.write(text)
    else:
        _write_atomic(args.out, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
