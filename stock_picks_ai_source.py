"""AI 相關股的公開名單來源：只輸出代號、權重、日期與固定分類。

不依賴 stock_picks，不呼叫模型、不搜尋。網頁文字與核心名單名稱不進卡片；
顯示名稱由呼叫端按代號接證交所官方簡稱，分類只收固定清單。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import requests
import safe_fetch

KGI_URL = "https://www.kgifund.com.tw/Fund/Detail?fundID=J020"
MAX_BYTES = 1024 * 1024
CORE_PATH = Path(__file__).resolve().with_name("stock_picks_ai_core.json")
SEGMENTS = frozenset({
    "AI 晶片代工", "先進封測", "AI 晶片測試", "AI ASIC 設計", "AI 伺服器",
    "AI 電源", "AI 散熱", "ABF 載板", "高速 CCL", "AI 伺服器 PCB", "AI 網通",
    "伺服器機構件", "伺服器連接器／線材",
})
_CODE = re.compile(r"[0-9]{4}")
_WEIGHT = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,8})?")  # float() 會收全形數字、底線、nan、1e3：先用 ASCII 數字的樣式擋掉
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HOLDING_DATE = re.compile(r"\(([0-9]{4})/([0-9]{2})/([0-9]{2})\)")
_HEADERS = ("股票代號", "股票名稱", "股數", "權重(%)")
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


class SourceError(ValueError):
    """只帶固定原因碼；呼叫端不可把原始網頁／例外文字寫進 log。"""

    def __init__(self, reason: str, *, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class CoreEntry:
    segment: str
    source_url: str
    source_date: date


@dataclass(frozen=True)
class CoreList:
    as_of: date
    entries: dict[str, CoreEntry]


@dataclass(frozen=True)
class Holdings:
    as_of: date
    rows: tuple[tuple[str, float], ...]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(code for code, _weight in self.rows)


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _date_of(value: object) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("date")
    return date.fromisoformat(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def load_core(path: Path | str | None = None, *, today: date | None = None) -> CoreList:
    """任何一列不合格就整份不用；name 只驗型別、不回傳給卡片。"""
    today = today or _today()
    try:
        with Path(path or CORE_PATH).open("rb") as stream:
            body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("size")
        raw = json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or set(raw) != {"schema", "as_of", "entries"}:
            raise ValueError("schema")
        if type(raw["schema"]) is not int or raw["schema"] != 1:
            raise ValueError("schema")
        as_of = _date_of(raw["as_of"])
        if as_of > today or not isinstance(raw["entries"], list) or not 1 <= len(raw["entries"]) <= 120:
            raise ValueError("entries")
        entries = {}
        for item in raw["entries"]:
            if not isinstance(item, dict) or set(item) != {"code", "name", "segment", "source_url", "source_date"}:
                raise ValueError("entry")
            code, name, segment, url = (item[k] for k in ("code", "name", "segment", "source_url"))
            if not isinstance(code, str) or not _CODE.fullmatch(code) or code in entries:
                raise ValueError("code")
            if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
                raise ValueError("name")
            if not isinstance(segment, str) or segment not in SEGMENTS:
                raise ValueError("segment")
            if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 or c == "\\" for c in url):
                raise ValueError("url")
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
                raise ValueError("url")
            # 草稿 MoneyDJ 的 a 是文章識別碼；追蹤與 session 參數不收。
            if any(key.lower().startswith("utm_") or key.lower() in {"session", "token", "sid", "sessionid"}
                   for key, _value in parse_qsl(parts.query)):
                raise ValueError("url")
            source_date = _date_of(item["source_date"])
            if source_date > as_of:
                raise ValueError("date")
            entries[code] = CoreEntry(segment, url, source_date)
        return CoreList(as_of, entries)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise SourceError("core_invalid") from None


class _HoldingsParser(HTMLParser):
    """限持股區段、逐格收文字；實際頁面有桌機／手機兩份相同表格。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.section_depth = 0
        self.dates: list[str] = []
        self.date_depth = 0
        self.date_parts: list[str] = []
        self.tables: list[tuple[tuple[str, ...], ...]] = []
        self.table: list[tuple[str, ...]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in _VOID:
            self.stack.append(tag)
            if len(self.stack) > 128:
                raise SourceError("html_structure")
        if attrs.get("id") == "FundDetail-06":
            if self.section_depth:
                raise SourceError("html_structure")
            self.section_depth = len(self.stack)
        if not self.section_depth:
            return
        if tag == "p" and "fund-asset__date" in (attrs.get("class") or "").split():  # 沒有值的 class（<p class>）是 None
            self.date_depth = len(self.stack)
            self.date_parts = []
        if tag == "table":
            if self.table is not None:
                raise SourceError("html_structure")
            self.table = []
        elif tag == "tr" and self.table is not None:
            if self.row is not None:
                raise SourceError("html_structure")
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            if self.cell is not None:
                raise SourceError("html_structure")
            self.cell = []

    def handle_endtag(self, tag):
        if self.section_depth:
            if tag in {"td", "th"} and self.cell is not None and self.row is not None:
                self.row.append("".join(self.cell).strip())
                self.cell = None
            elif tag == "tr" and self.row is not None and self.table is not None:
                self.table.append(tuple(self.row))
                self.row = None
            elif tag == "table" and self.table is not None:
                if self.row is not None or self.cell is not None:
                    raise SourceError("html_structure")
                self.tables.append(tuple(self.table))
                self.table = None
            if tag == "p" and self.date_depth:
                self.dates.append("".join(self.date_parts).strip())
                self.date_depth = 0
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]
        if self.section_depth and len(self.stack) < self.section_depth:
            self.section_depth = 0

    def handle_data(self, data):
        if self.date_depth:
            self.date_parts.append(data)
        if self.cell is not None:
            self.cell.append(data)


def parse_holdings(html: bytes | str, *, today: date, previous_as_of: date | None = None) -> Holdings:
    """先驗整份持股（含上櫃），再由 eligible_codes 過上市普通股資格。"""
    try:
        if isinstance(html, bytes):
            if len(html) > MAX_BYTES:
                raise SourceError("html_size")
            html = html.decode("utf-8", errors="strict")
        elif not isinstance(html, str) or len(html.encode("utf-8")) > MAX_BYTES:
            raise SourceError("html_size")
        parser = _HoldingsParser()
        parser.feed(html)
        parser.close()
        if parser.table is not None or not parser.dates or len(set(parser.dates)) != 1:
            raise SourceError("html_structure")
        match = _HOLDING_DATE.fullmatch(parser.dates[0])
        if match is None:
            raise SourceError("holdings_date")
        as_of = date(*(int(v) for v in match.groups()))
        if not 0 <= (today - as_of).days <= 10:
            raise SourceError("holdings_age")
        if previous_as_of is not None and as_of < previous_as_of:
            raise SourceError("holdings_older")
        tables = [table for table in parser.tables if table and table[0] == _HEADERS]
        if not tables or any(table != tables[0] for table in tables[1:]):
            raise SourceError("holdings_tables")
        rows = tables[0][1:]
        if not 40 <= len(rows) <= 60:
            raise SourceError("holdings_count")
        result, seen = [], set()
        for row in rows:
            if len(row) != 4 or not _CODE.fullmatch(row[0]) or row[0] in seen:
                raise SourceError("holdings_code")
            if _WEIGHT.fullmatch(row[3]) is None:
                raise SourceError("holdings_weight")
            weight = float(row[3])
            if not math.isfinite(weight) or weight <= 0:
                raise SourceError("holdings_weight")
            seen.add(row[0])
            result.append((row[0], weight))
        if not 85 <= math.fsum(weight for _code, weight in result) <= 101:
            raise SourceError("holdings_weight")
        return Holdings(as_of, tuple(result))
    except SourceError:
        raise
    except (UnicodeError, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise SourceError("holdings_invalid") from None


def fetch_holdings(*, today: date, previous_as_of: date | None = None, deadline: float = 5.0) -> Holdings:
    """固定來源、無轉址、嚴格 MIME／UTF-8，時間上限包含整個抓取。"""
    if not math.isfinite(deadline) or deadline <= 0:
        raise SourceError("fetch_timeout")
    try:
        response = safe_fetch.fetch(
            KGI_URL, headers={"Accept": "text/html", "User-Agent": "LineBotStockPicks/1.0"},
            timeout=min(deadline, 5.0), deadline=min(deadline, 5.0), max_bytes=MAX_BYTES,
            accept_types={"text/html"}, allow_redirects=False,
        )
    except (safe_fetch.FetchRefused, requests.exceptions.RequestException, OSError):
        raise SourceError("fetch_failed") from None
    if response.status_code != 200:
        raise SourceError("fetch_status", status_code=response.status_code)
    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "text/html":
        raise SourceError("fetch_type")
    return parse_holdings(response.content, today=today, previous_as_of=previous_as_of)


def eligible_codes(codes: Iterable[str], listed: Mapping[str, object]) -> tuple[str, ...]:
    """整個聯集都以目前上市資料為準；不截斷過大的有效名單。"""
    eligible = set()
    for code in codes:
        if not isinstance(code, str) or not _CODE.fullmatch(code) or code.startswith("91"):
            continue
        row = listed.get(code)
        if row is None or getattr(row, "kind", None) != "stock":
            continue
        name = getattr(row, "name", "")
        if not isinstance(name, str) or "-DR" in name.upper() or "-創" in name:
            continue
        eligible.add(code)
        if len(eligible) > 120:
            raise SourceError("universe_size")
    return tuple(sorted(eligible))
