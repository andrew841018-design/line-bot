"""機構動向（/盤勢 13F，第一期，2026-10-31）資金流的來源：iShares SOXX 產品頁、SSGA SPY 的 NAV 歷史 xlsx、元大 0050
申購買回清單頁。

Andrew 2026-10-31 定案（PLAN_v2 §1、§3.1；IMPL_SPEC_p1 §2）：SOXX、0050 的申購贖回（在外股數的變化）上卡，SPY 只給戰情室。
這個模組只負責「抓」與「讀懂」；和前一份比、算估算金額、判斷待核對在 market_pipeline_inst。

來源（2026-10-28 實測；保存的回應只當結構參考，測試全用 .py 裡合成的最小結構）：
- iShares：產品頁 HTML（約 1.5 MB）。Key Facts 的「Shares Outstanding 94,600,000 as of Oct 27, 2026」（數字與 as of 在不同
  標籤）；NAV 在頁首的卡「NAV as of Oct 27, 2026 $ $ 510.95」（日期在數字前面；同一張卡頁面上有兩份一模一樣的）。
  頁面的 canonical 網址是 …/239705/ishares-semiconductor-etf；規格指定的 …/ishares-phlx-semiconductor-etf 在 10/28 的樣本裡
  是直接回頁面（登入連結的 redirectUrl 是原路徑），沒有看到轉址——market_http 不轉址，staging 要確認不是 301（回 moved）。
  CSV 下載要 cookie（戰情室實測），不用。
- SSGA：navhist-us-en-spy.xlsx（約 280 KB）：「Ticker Symbol: SPY」＋表頭 Date／NAV／Shares Outstanding／Total Net Assets，
  每天一列、新到舊（2003 年起；2006 年以前的股數是「-」）。用最後網址（舊的 /us/en/intermediary/… 會 301）。
- 元大：申購買回清單頁（約 1 MB，伺服器端算好的 HTML；沒有 <table>，每一格是 div.td：h5 日期、h4 欄名、p 值，所以不能用
  market_sources_us._html_tables，改用同樣只靠標準庫 HTMLParser 的可見文字）。「已發行受益權單位總數」「與前日已發行單位
  差異數」「每受益權單位淨資產價值(元)」（前面的 h5 是它的日期），股票實物申贖那段的「交易日期」。公告日期是下一個交易日
  （這份清單適用的申購日），不是資料日。

解析（嚴格：頁面一改版就該失敗，而不是印出錯的數字）：
- HTML 一律先用 HTMLParser 取可見文字（script／style 的內容不算，每個標籤邊界當成空白，NFKC 後壓空白），再用固定的正則
  找欄名與數字。來源的任何文字都不存、不回傳：結果只有日期與數字，錯誤只有 kind＋detail（程式自己的列舉字）。
- iShares：「Shares Outstanding <數字> as of <Mon DD, YYYY>」必須正好對到一個（0 個或 2 個以上＝format）。NAV 卡「NAV as of
  <日期> $ <數字>」（數字前至少一個 $）全部一致才採用（頁面本來就有兩份）；對不到、彼此不一致、或日期和在外股數的日期不同
  → NAV＝None。和前一份 NAV 比的合理性檢查在 market_pipeline_inst。標題要有 SOXX。
- SSGA：用 market_sources_us._xlsx_rows（先過 zip／XML 的安全檢查）；代號列要是 SPY、表頭四欄各出現一次；取最新一列＋往前
  9 列，日期要嚴格往前（不然 conflict）；最新一列一定要完整，較舊的列遇到不完整的就停（前面的照收）。
- 元大：三個欄名各自只能出現一次（淨值那格可以沒有）；基準日＝「交易日期」（出現兩個不同的 → conflict），沒有才用淨值那格的
  日期，兩個都沒有 → date；淨值那格的日期和基準日不同、或沒印日期 → NAV＝None（不能確定是同一天；2026-10-31 Phase 6 審查：
  以前日期不同是整頁 conflict，改成和 iShares 一致只丟 NAV）。標題要有 (0050)。
- 數字依欄位：在外股數／受益權單位數是正整數（小數部分必須是 0）；NAV 是正數；官方差異數可正可負（淨贖回的日子是負數，
  收「-」「−」「－」與括號）。

每個來源兩個函式：純解析 `parse_*`（bytes）與抓取 `fetch_flow(代號)`（固定網址 → http.fetch（預設 market_http，測試注入假的）
→ 解析）。只 import 標準庫與 market_*；market_http 到真的要抓時才載入；import 時不做任何 I/O。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Callable, Optional, Union

import market_sources_us as us_src
from market_calc import ParseError  # 全專案共用

KIB = 1024
MIB = 1024 * 1024

# ── 網址與端點 ────────────────────────────────────────────────────────────
ISHARES_URL = "https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf"
SSGA_NAVHIST_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/navhist-us-en-spy.xlsx"
YUANTA_URL = "https://www.yuantaetfs.com/tradeInfo/pcf/0050"


@dataclass(frozen=True)
class Endpoint:
    """一個來源的固定網址與抓取設定（主機的間隔、每日上限在 market_store.HOST_RULES）。"""

    url: str
    host: str
    source: str  # payload 的 source（固定字）
    unit: str  # NAV 與估算金額的幣別
    accept: tuple[str, ...]  # Content-Type 要含的子字串
    max_bytes: int
    timeout: float  # 整個請求的總時間上限（秒）


ENDPOINTS: dict[str, Endpoint] = {
    "SOXX": Endpoint(ISHARES_URL, "www.ishares.com", "iShares", "USD", ("html",), 4 * MIB, 30.0),  # 實測 1.5 MB
    "SPY": Endpoint(SSGA_NAVHIST_URL, "www.ssga.com", "SSGA", "USD", ("spreadsheet",), 2 * MIB, 30.0),  # 實測 280 KB
    "0050": Endpoint(YUANTA_URL, "www.yuantaetfs.com", "元大", "TWD", ("html",), 2 * MIB, 30.0),  # 實測 1 MB
}
HISTORY_ROWS = 10  # SSGA：最新一列＋往前 9 列


@dataclass(frozen=True)
class FlowDay:
    as_of: date  # 發行商印的日期
    shares_out: int  # 在外股數（0050 是已發行受益權單位數）
    nav: Optional[float]  # 同一天的每單位淨值；沒有、或發行商印的日期不同 → None


@dataclass(frozen=True)
class FlowPage:
    symbol: str
    days: tuple[FlowDay, ...]  # 新到舊；iShares、元大只有一筆，SSGA 最多 HISTORY_ROWS 筆
    official_change: Optional[int] = None  # 元大「與前日已發行單位差異數」（其他來源沒有）


# ── 共用：可見文字、數字、日期 ─────────────────────────────────────────────


class _VisibleText(HTMLParser):
    """可見文字與 <title>：script／style 的內容不算；每個標籤的邊界都當成空白（欄名和數字常在不同標籤）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._raw = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style"):
            self._raw += 1
        elif tag == "title":
            self._in_title = True
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._raw = max(0, self._raw - 1)
        elif tag == "title":
            self._in_title = False
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._raw:
            return
        self.parts.append(data)
        if self._in_title:
            self.title.append(data)


def _squash(parts: list[str]) -> str:
    return " ".join(unicodedata.normalize("NFKC", "".join(parts)).split())


def _visible(body: bytes) -> tuple[str, str]:
    """(可見文字, 標題)。解碼錯的位元組換成 U+FFFD（數字與欄名都是固定字，壞掉就對不到、判格式錯）。"""
    if not isinstance(body, (bytes, bytearray)):
        raise ParseError("format", "body")
    parser = _VisibleText()
    try:
        parser.feed(bytes(body).decode("utf-8-sig", errors="replace"))
        parser.close()
    except Exception:  # HTMLParser 遇到極端壞的標記可能丟例外：當格式不對
        raise ParseError("format", "html") from None
    return _squash(parser.parts), _squash(parser.title)


_GROUPED = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")
_CURRENCY = re.compile(r"(?:NTD|TWD|NT\$|US\$|USD|\$)\s*", re.IGNORECASE)
_MINUS = ("-", "−")  # NFKC 之後全形的「－」已經是「-」；數學的減號 U+2212 不會被換掉


def _number(raw: Any, *, integer: bool = False, signed: bool = False) -> Optional[Union[int, float]]:
    """來源的數字 → int（integer=True：小數部分必須是 0）或 float。看不懂、不是有限值、或欄位不收負數卻是負的 → None
    （呼叫端決定是格式錯還是缺值）。收「94,600,000」「NTD 108.66」「$512.34」「-23,500,000」「−23,500,000」「(23,500,000)」。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if not math.isfinite(value) or (value < 0 and not signed):
            return None
        if integer:
            if value != math.floor(value):
                return None
            return raw if isinstance(raw, int) else int(value)
        return value
    if not isinstance(raw, str):
        return None
    text = unicodedata.normalize("NFKC", raw).strip()
    negative = False
    if len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        negative, text = True, text[1:-1].strip()
    if text and text[0] in ("+", *_MINUS):
        if negative:
            return None  # 「(-1,234)」兩種負號一起出現：不猜
        negative, text = text[0] in _MINUS, text[1:].strip()
    currency = _CURRENCY.match(text)
    if currency:
        text = text[currency.end():]
    if not _GROUPED.fullmatch(text) or (negative and not signed):
        return None
    plain = text.replace(",", "")
    if integer:
        whole, _, fraction = plain.partition(".")
        if fraction.strip("0"):
            return None
        value_int = int(whole)
        return -value_int if negative else value_int
    value = float(plain)
    if not math.isfinite(value):
        return None
    return -value if negative else value


_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def _date(year: Any, month: Any, day: Any, field: str) -> date:
    try:
        value = date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        raise ParseError("date", field) from None
    if not 2000 <= value.year <= 2100:
        raise ParseError("date", field)
    return value


def _us_date(mon: str, day: str, year: str, field: str) -> date:
    month = _MONTHS.get(mon[:3].lower())
    if month is None:
        raise ParseError("date", field)
    return _date(year, month, day, field)


# ── iShares SOXX ─────────────────────────────────────────────────────────

_US_DATE = r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),\s*(\d{4})"
_ISHARES_SHARES_RE = re.compile(r"Shares Outstanding\s+([+\-−]?[0-9][0-9,]*(?:\.[0-9]+)?)\s+as of\s+" + _US_DATE)
# 至少要有一個「$」（2026-10-31 Phase 6 審查 R2-2）：版面改了、日期後面先接到別的數字（「1 Day NAV Change」「12.5% YTD」）時不能被當成 NAV
_ISHARES_NAV_RE = re.compile(r"(?<![A-Za-z])NAV\s+as of\s+" + _US_DATE + r"\s+(?:\$\s*)+([0-9][0-9,]*(?:\.[0-9]+)?)")
_ISHARES_TITLE_RE = re.compile(r"(?<![A-Z0-9])SOXX(?![A-Z0-9])")


def parse_ishares(body: bytes) -> FlowPage:
    """iShares SOXX 產品頁 → 一筆（在外股數、同一天的 NAV）。規則見模組說明。"""
    text, title = _visible(body)
    if not _ISHARES_TITLE_RE.search(title):
        raise ParseError("format", "fund")  # 不是 SOXX 的頁（轉到別的基金、首頁）
    found = _ISHARES_SHARES_RE.findall(text)
    if len(found) != 1:
        raise ParseError("format", "shares")
    raw, mon, day, year = found[0]
    shares = _number(raw, integer=True)
    if shares is None or shares <= 0:
        raise ParseError("format", "shares")
    as_of = _us_date(mon, day, year, "as_of")
    navs: set[tuple[Optional[date], Optional[float]]] = set()
    for nav_mon, nav_day, nav_year, nav_raw in _ISHARES_NAV_RE.findall(text):
        try:
            when: Optional[date] = _us_date(nav_mon, nav_day, nav_year, "nav_as_of")
        except ParseError:
            when = None
        navs.add((when, _number(nav_raw)))
    nav: Optional[float] = None
    if len(navs) == 1:  # 頁面上的幾份要完全一致
        when, value = navs.pop()
        if when == as_of and value is not None and value > 0:
            nav = float(value)
    return FlowPage("SOXX", (FlowDay(as_of, shares, nav),))


# ── SSGA SPY navhist ─────────────────────────────────────────────────────

_SSGA_HEADER = ("date", "nav", "shares outstanding", "total net assets")
_SSGA_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")


def _cell(row: tuple, index: int) -> Any:
    return row[index] if index < len(row) else None


def _cell_text(row: tuple, index: int) -> str:
    value = _cell(row, index)
    return "" if value is None else " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _ssga_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    match = _SSGA_DATE_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    try:
        return _us_date(match.group(2), match.group(1), match.group(3), "date")
    except ParseError:
        return None


def parse_ssga_navhist(body: bytes) -> FlowPage:
    """SSGA SPY navhist xlsx → 最新一列＋往前 9 列（新到舊）。規則見模組說明。"""
    rows = us_src._xlsx_rows(body)
    fund: Optional[str] = None
    columns: Optional[dict[str, int]] = None
    header_index = 0
    for index, row in enumerate(rows[:40]):
        cells = [_cell_text(row, j).lower() for j in range(len(row))]
        if cells and cells[0] == "ticker symbol:":
            fund = _cell_text(row, 1).upper()
        if all(cells.count(name) == 1 for name in _SSGA_HEADER):
            columns = {name: cells.index(name) for name in _SSGA_HEADER}
            header_index = index
            break
    if columns is None:
        raise ParseError("format", "header")
    if fund != "SPY":
        raise ParseError("format", "fund")
    days: list[FlowDay] = []
    for row in rows[header_index + 1:]:
        if len(days) >= HISTORY_ROWS:
            break
        raw_date, raw_nav, raw_shares = (_cell(row, columns[name]) for name in ("date", "nav", "shares outstanding"))
        if _blank(raw_date) and _blank(raw_nav) and _blank(raw_shares):
            break  # 表格結束（下面是免責聲明）
        day = _ssga_date(raw_date)
        nav = _number(raw_nav)
        shares = _number(raw_shares, integer=True)
        if day is None or nav is None or nav <= 0 or shares is None or shares <= 0:
            if not days:
                raise ParseError("format", "latest_row")  # 最新一列一定要完整
            break  # 較舊的列不完整（例如 2006 年以前的股數是「-」）：前面的照收
        if days and day >= days[-1].as_of:
            raise ParseError("conflict", "order")  # 不是新到舊：檔案的排法變了
        days.append(FlowDay(day, int(shares), float(nav)))
    if not days:
        raise ParseError("empty", "rows")
    return FlowPage("SPY", tuple(days))


# ── 元大 0050 申購買回清單 ───────────────────────────────────────────────

_TW_DATE = r"(\d{4})/(\d{1,2})/(\d{1,2})"
_YUANTA_UNITS = "已發行受益權單位總數"
_YUANTA_DIFF = "與前日已發行單位差異數"
_YUANTA_NAV = "每受益權單位淨資產價值"
_YUANTA_UNITS_RE = re.compile(_YUANTA_UNITS + r"\s*(\S+)")
_YUANTA_DIFF_RE = re.compile(_YUANTA_DIFF + r"\s*(\S+)")
_YUANTA_NAV_RE = re.compile(r"(?:" + _TW_DATE + r"\s*)?" + _YUANTA_NAV + r"\s*(?:\(元\))?\s*(?:NTD|TWD|NT\$)?\s*(\S+)")
_YUANTA_TRADE_RE = re.compile(r"交易日期\s*:\s*" + _TW_DATE)  # NFKC 之後全形冒號已經是「:」


def _one_value(text: str, label: str, pattern: re.Pattern, field: str) -> str:
    """欄名必須正好出現一次，而且後面接著值。"""
    if text.count(label) != 1:
        raise ParseError("format", field)
    match = pattern.search(text)
    if match is None:
        raise ParseError("format", field)
    return match.group(1)


def parse_yuanta_pcf(body: bytes) -> FlowPage:
    """元大 0050 申購買回清單頁 → 一筆（受益權單位數、同一天的淨值）＋官方差異數。規則見模組說明。"""
    text, title = _visible(body)
    if "(0050)" not in title:
        raise ParseError("format", "fund")
    units = _number(_one_value(text, _YUANTA_UNITS, _YUANTA_UNITS_RE, "units"), integer=True)
    if units is None or units <= 0:
        raise ParseError("format", "units")
    change = _number(_one_value(text, _YUANTA_DIFF, _YUANTA_DIFF_RE, "change"), integer=True, signed=True)
    if change is None:
        raise ParseError("format", "change")
    nav: Optional[float] = None
    nav_day: Optional[date] = None
    if text.count(_YUANTA_NAV) == 1:
        match = _YUANTA_NAV_RE.search(text)
        if match is not None:
            if match.group(1) is not None:
                nav_day = _date(match.group(1), match.group(2), match.group(3), "nav_as_of")
            value = _number(match.group(4))
            nav = float(value) if value is not None and value > 0 else None
    trades = {_date(year, month, day, "base_date") for year, month, day in _YUANTA_TRADE_RE.findall(text)}
    if len(trades) > 1:
        raise ParseError("conflict", "base_date")  # 兩個不同的交易日期：不知道單位數是哪一天的
    # 有「交易日期」就以它為準；淨值那格的日期不同時只把 NAV 設 None（和 iShares 一致，2026-10-31 Phase 6 審查 R1-6、R3-9）；
    # 兩個都沒有才是日期錯
    base = trades.pop() if trades else nav_day
    if base is None:
        raise ParseError("date", "base_date")
    return FlowPage("0050", (FlowDay(base, int(units), nav if nav_day == base else None),), official_change=int(change))


# ── 抓取 ──────────────────────────────────────────────────────────────────

_PARSERS: dict[str, Callable[[bytes], FlowPage]] = {
    "SOXX": parse_ishares,
    "SPY": parse_ssga_navhist,
    "0050": parse_yuanta_pcf,
}


def _transport(http: Any) -> Any:
    """抓取層：預設 market_http（用到才 import，這個模組沒有它也能 import 與測試）。"""
    if http is not None:
        return http
    import market_http  # noqa: PLC0415

    return market_http


def enabled(symbol: str) -> bool:
    """一個開關（照 TACO、BLS）：來源的主機在 market_http.ALLOWED_HOSTS 才抓；要停用只要把主機拿掉。每次呼叫才讀白名單
    （測試 monkeypatch）。"""
    return ENDPOINTS[symbol].host in _transport(None).ALLOWED_HOSTS


def fetch_flow(symbol: str, *, http: Any = None, ledger: Any = None) -> FlowPage:
    """抓 `symbol`（SOXX、SPY、0050）的固定網址並解析。http／ledger 由 job 的 Context 傳進來。"""
    endpoint = ENDPOINTS[symbol]
    response = _transport(http).fetch(
        endpoint.url, max_bytes=endpoint.max_bytes, timeout=endpoint.timeout, accept=endpoint.accept, ledger=ledger
    )
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        raise ParseError("format", "body")
    if len(body) > endpoint.max_bytes:
        raise ParseError("format", "body_size")  # 傳輸層已限制；假的或換掉的傳輸層也不能塞超大內容
    return _PARSERS[symbol](bytes(body))
