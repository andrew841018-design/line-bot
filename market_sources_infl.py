"""通膨資料的來源（/盤勢 第 9 輪批 2 C 軌，2026-10-24）：美國財政部的名目與實質殖利率（算 5 年／10 年損益兩平通膨）、
BLS 的 CPI 與核心 CPI 指數（API v1 GET）。

Andrew 2026-10-23 第 4 項：「通膨指標＋目前通膨狀況」。PLAN_r9_andrew9_v2 A6：損益兩平自己抓名目與實質兩份 CSV、存獨立
的 obs，`market_sources_us.parse_treasury_csv`（`us.ust` 用）一個位元組都不改——實質殖利率檔沒有 3 個月／2 年欄，那支
解析器會直接丟 format；這裡另寫一支同時讀名目與實質檔，兩份都只取 5 年與 10 年。

BLS（A2、A5；2026-10-24 Andrew 同意新增 api.bls.gov）：四個序列 CUUR0000SA0（全項未季調）、CUSR0000SA0（全項季調）、
CUUR0000SA0L1E（核心未季調）、CUSR0000SA0L1E（核心季調），一個序列一個 GET（不用 POST：market_http 沒有送資料出去的通道）。
解析以 (年, 月) 為鍵——未季調原始回應有 35 列（33 個月＋2 個年平均 M13）、季調 33 列，用位置往前數 12 列會在兩邊指到
不同月份；M13 與非月資料略過；`"-"` 是缺值（2025-10 政府停擺，最新月也可能再發生），不存、也不算「前進」；value 只收嚴格的
數字字串，其他（含 `$`、千分位、NaN）整個序列拒收；footnotes、message、periodName 這些自由文字不讀、不存、不印。
200＋`REQUEST_NOT_PROCESSED`（超過每日額度）或 Results 是空的 → ParseError("vendor", "limit")：不是「沒有資料」，collect 把它當
限額（主機冷卻＋任務退避）；HTTP 429 由 market_http 處理。

和 market_sources_us 一樣每個來源兩個函式：純解析 `parse_*`（格式不對丟 `ParseError(kind)`，kind 只有
market_calc.PARSE_ERROR_KINDS 那幾個，detail 只放程式自己的字）與抓取 `fetch_*`（組網址 → `http.fetch(url, …, ledger=ledger)`
→ 解析）。刻意不 import market_sources_us：這個模組壞了或被改了，不能拖累美股其他來源（A22），反過來也一樣。

這個模組 import 時不做任何 I/O；market_http 到真的要抓時才載入（測試整個換成假的）。
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional
from urllib.parse import quote

import market_calc
from market_calc import ParseError  # 全專案共用

KIB = 1024
MIB = 1024 * 1024


def _err(kind: str, detail: str = "") -> ParseError:
    """detail 只放程式自己的字（欄位名、列舉字），不放來源的內容。"""
    return ParseError(kind, detail)


# ── 網址與端點 ────────────────────────────────────────────────────────────
# 和 market_sources_us.TREASURY_URL 只差 type：名目＝daily_treasury_yield_curve、實質＝daily_treasury_real_yield_curve
# （2026-10-23 實測：兩個都是 200、text/csv、無轉址）。
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/all/{ym}"
    "?type={curve}&field_tdr_date_value_month={ym}&page&_format=csv"
)
TREASURY_CURVES = {"nominal": "daily_treasury_yield_curve", "real": "daily_treasury_real_yield_curve"}


@dataclass(frozen=True)
class Endpoint:
    """一個端點的抓取設定。每日次數與間隔由帳本（market_store.HOST_RULES）依主機管，這裡不重複記。"""

    host: str
    accept: tuple[str, ...]  # Content-Type 要含的子字串
    max_bytes: int
    timeout: float  # 整個請求的總時間上限（秒）


BLS_HOST = market_calc.CPI_HOST  # api.bls.gov（2026-10-24 Andrew 同意；v1 免註冊、每天 25 次、HostRule(5.0, 12)）
BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/{series}"
BLS_SERIES = market_calc.CPI_SERIES
BLS_PROBE = market_calc.CPI_NSA  # 先問這一個；月份前進才抓另外三個

ENDPOINTS: dict[str, Endpoint] = {
    "treasury": Endpoint("home.treasury.gov", ("csv",), 1 * MIB, 15.0),  # 名目與實質同一個端點，只換 type
    "bls_cpi": Endpoint(BLS_HOST, ("json",), 64 * KIB, 15.0),  # 單一序列約 3 KB（2026-10-23 實測 3,290 位元組）
}


# ── 文字與日期 ────────────────────────────────────────────────────────────
_MDY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})", re.ASCII)  # re.ASCII：\d 預設連阿拉伯數字等都收，嚴格解析只收 0–9


def _as_text(value: object) -> str:
    """bytes 用 UTF-8（容許 BOM）嚴格解碼；壞位元組 → format。"""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise _err("format", "encoding") from None
    if not isinstance(value, str):
        raise _err("format", "text")
    return value.lstrip("﻿")


# ── 美國財政部：名目與實質殖利率（5 年、10 年） ─────────────────────────────
# 名目檔才有的天期（實質檔只有 5／7／10／20／30 年）：實質請求拿到有這些欄的檔，就是名目檔被當成實質檔——
# 損益兩平會變成 0，所以不收（審查 r9 advisor 5）。
_NOMINAL_ONLY_COLUMNS = frozenset({"1 mo", "1.5 month", "2 mo", "3 mo", "4 mo", "6 mo", "1 yr", "2 yr", "3 yr"})
_NOMINAL_REQUIRED_COLUMNS = frozenset({"3 mo", "2 yr"})  # 名目檔一定有；實質檔沒有
_YIELD_CELL_RE = re.compile(r"[+-]?\d{1,2}(?:\.\d{1,3})?", re.ASCII)
_YIELD_MISSING = frozenset({"", "n/a", "na"})
YIELD_RANGE = (-5.0, 50.0)  # 和 parse_treasury_csv 一樣：範圍外是檔案壞了、不是行情


@dataclass(frozen=True)
class YieldPoint:
    """某天的 5 年與 10 年殖利率（%）；某天沒公布的天期是 None。"""

    date: date
    y5: Optional[float]
    y10: Optional[float]


def _yield_cell(cell: str) -> Optional[float]:
    text = cell.strip()
    if text.lower() in _YIELD_MISSING:
        return None
    if not _YIELD_CELL_RE.fullmatch(text):
        raise _err("format", "rate")
    rate = float(text)
    if not YIELD_RANGE[0] <= rate <= YIELD_RANGE[1]:
        raise _err("format", "range")
    return rate


def _mdy_date(cell: str) -> date:
    match = _MDY_RE.fullmatch(cell.strip())
    if not match:
        raise _err("date", "Date")
    year, month, day = int(match.group(3)), int(match.group(1)), int(match.group(2))
    if not 1900 <= year <= 2200:
        raise _err("date", "Date")
    try:
        return date(year, month, day)
    except ValueError:
        raise _err("date", "Date") from None


def parse_treasury_yields(text: object, curve: str) -> list[YieldPoint]:
    """財政部日殖利率 CSV 的 5 年與 10 年（新日期在上；回傳由舊到新）。

    `curve`＝"nominal"（欄名 "5 Yr"）或 "real"（欄名 "5 YR"，大小寫不同，比對前一律轉小寫）。拿到的檔和要的曲線對不上
    （名目檔當實質、實質檔當名目）→ format。債市休市的日子（哥倫布日、退伍軍人節）本來就沒有列；只有表頭（月初還沒公布）
    → []，不算失敗；整份空白 → empty。"""
    if curve not in TREASURY_CURVES:
        raise ValueError("bad curve")
    body = _as_text(text)
    try:
        lines = list(csv.reader(io.StringIO(body)))
    except csv.Error:
        raise _err("format", "csv") from None
    lines = [line for line in lines if any(cell.strip() for cell in line)]
    if not lines:
        raise _err("empty", "csv")
    header = [cell.strip().lower() for cell in lines[0]]
    if not header or header[0] != "date" or len(set(header)) != len(header):
        raise _err("format", "header")
    if "5 yr" not in header or "10 yr" not in header:
        raise _err("format", "columns")
    nominal_like = _NOMINAL_REQUIRED_COLUMNS <= set(header)
    if curve == "real" and (set(header) & _NOMINAL_ONLY_COLUMNS):
        raise _err("format", "curve")
    if curve == "nominal" and not nominal_like:
        raise _err("format", "curve")
    col5, col10 = header.index("5 yr"), header.index("10 yr")
    found: dict[date, YieldPoint] = {}
    for line in lines[1:]:
        if len(line) != len(header):
            raise _err("format", "row")
        point = YieldPoint(_mdy_date(line[0]), _yield_cell(line[col5]), _yield_cell(line[col10]))
        if point.date in found and found[point.date] != point:
            raise _err("conflict", "Date")
        found[point.date] = point
    return [found[day] for day in sorted(found)]


# ── BLS：CPI 與核心 CPI ──────────────────────────────────────────────────────
_BLS_YEAR_RE = re.compile(r"\d{4}", re.ASCII)
_BLS_PERIOD_RE = re.compile(r"[A-Z]\d{2}", re.ASCII)  # BLS 的期間代碼：M01–M12 月、M13 年平均、S 半年、Q 季…
_BLS_MONTH_RE = re.compile(r"M(0[1-9]|1[0-2])")
_BLS_VALUE_RE = re.compile(r"\d{1,5}(?:\.\d{1,4})?", re.ASCII)
BLS_INDEX_RANGE = (100.0, 1000.0)  # CPI 指數（1982–84＝100）現在約 337；範圍外是檔案壞了、不是行情


@dataclass(frozen=True)
class BlsSeries:
    """一個序列：{"YYYY-MM": 指數}，只含有數字的月份（缺值的月份沒有鍵）。"""

    series_id: str
    values: dict[str, float]

    @property
    def latest(self) -> Optional[str]:
        return max(self.values) if self.values else None


def _as_json(payload: object) -> Any:
    if isinstance(payload, (bytes, bytearray, str)):
        text = _as_text(payload)
        try:
            return json.loads(text, parse_constant=lambda _name: None)
        except (ValueError, RecursionError):
            raise _err("format", "json") from None
    return payload


def parse_bls_series(payload: object, *, expect_series: Optional[str] = None) -> BlsSeries:
    """BLS API v1 `timeseries/data/{序列}` 的回應 → BlsSeries。"""
    root = _as_json(payload)
    if not isinstance(root, dict):
        raise _err("format", "envelope")
    status = root.get("status")
    if not isinstance(status, str):
        raise _err("format", "status")
    if status == "REQUEST_NOT_PROCESSED":
        raise _err("vendor", "limit")  # 超過每日額度（HTTP 200）：限額種類，不是「沒資料」
    if status != "REQUEST_SUCCEEDED":
        raise _err("vendor", "status")
    results = root.get("Results")
    if results is None:
        raise _err("format", "results")
    if results == {} or results == []:
        raise _err("vendor", "limit")  # Results 是空的：BLS 沒有處理這個請求，和額度同一類
    series_list = results.get("series") if isinstance(results, dict) else None
    if not isinstance(series_list, list):
        raise _err("format", "series")
    if not series_list:
        raise _err("vendor", "limit")
    if len(series_list) != 1 or not isinstance(series_list[0], dict):
        raise _err("format", "series")
    item = series_list[0]
    series_id = item.get("seriesID")
    if not isinstance(series_id, str) or (expect_series is not None and series_id != expect_series):
        raise _err("format", "symbol")
    data = item.get("data")
    if not isinstance(data, list):
        raise _err("format", "data")
    if not data:
        raise _err("vendor", "limit")
    values: dict[str, float] = {}
    for point in data:
        if not isinstance(point, dict):
            raise _err("format", "point")
        year, period, value = point.get("year"), point.get("period"), point.get("value")
        if not isinstance(year, str) or not _BLS_YEAR_RE.fullmatch(year) or not isinstance(period, str) \
                or not _BLS_PERIOD_RE.fullmatch(period):
            raise _err("format", "period")
        month = _BLS_MONTH_RE.fullmatch(period)
        if month is None:
            continue  # M13（年平均）、半年／季資料：不是月資料
        if not isinstance(value, str):
            raise _err("format", "value")
        if value == "-":
            continue  # 缺值（2025-10 政府停擺；最新月也可能）：不存，也不算前進
        if not _BLS_VALUE_RE.fullmatch(value):
            raise _err("format", "value")
        number = float(value)
        if not BLS_INDEX_RANGE[0] <= number <= BLS_INDEX_RANGE[1]:
            raise _err("format", "range")
        key = f"{year}-{month.group(1)}"
        if key in values and values[key] != number:
            raise _err("conflict", "month")
        values[key] = number
    if not values:
        raise _err("empty", "data")
    return BlsSeries(series_id, values)


# ── 抓取 ──────────────────────────────────────────────────────────────────
def _transport(http: Any) -> Any:
    """抓取層：預設 market_http（用到才 import）。"""
    if http is not None:
        return http
    import market_http  # noqa: PLC0415

    return market_http


def _fetch(key: str, url: str, http: Any, ledger: Any) -> bytes:
    endpoint = ENDPOINTS[key]
    response = _transport(http).fetch(
        url,
        max_bytes=endpoint.max_bytes,
        timeout=endpoint.timeout,
        accept=endpoint.accept,
        ledger=ledger,
    )
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        raise _err("format", "body")
    if len(body) > endpoint.max_bytes:
        raise _err("format", "body_size")  # 傳輸層已限制；假的或換掉的傳輸層也不能塞超大內容
    return bytes(body)


def fetch_treasury_yields(curve: str, month: date, *, http: Any = None, ledger: Any = None) -> list[YieldPoint]:
    """某個月（取 month 的年月）的 5 年與 10 年殖利率；月初還沒有列時回 []（404 由呼叫端處理）。"""
    if curve not in TREASURY_CURVES:
        raise ValueError("bad curve")
    if not isinstance(month, date) or not 1990 <= month.year <= 2200:
        raise ValueError("bad month")
    url = TREASURY_URL.format(ym=f"{month.year:04d}{month.month:02d}", curve=TREASURY_CURVES[curve])
    rows = parse_treasury_yields(_fetch("treasury", url, http, ledger), curve)
    return [row for row in rows if (row.date.year, row.date.month) == (month.year, month.month)]


def fetch_bls_series(series_id: str, *, http: Any = None, ledger: Any = None) -> BlsSeries:
    """一個序列一個 GET（v1 免註冊、每天 25 次、一次最多 10 年；預設回 2024–2026 三年）。"""
    if series_id not in BLS_SERIES:
        raise ValueError("bad series")
    url = BLS_URL.format(series=quote(series_id, safe=""))
    return parse_bls_series(_fetch("bls_cpi", url, http, ledger), expect_series=series_id)
