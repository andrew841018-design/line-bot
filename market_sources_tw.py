"""台股資料來源（/盤勢，2026-10-17）：證交所、證交所 OpenAPI、期交所 OpenAPI、FinMind、MoneyDJ 0050 的解析與抓取。

Andrew 2026-10-17 的要求：台股也要看技術面、成交量（「我要知道當前量是多還是少」）、0050 成分股跌破／站上
20／60／120／240 日均線的家數、上漲與下跌家數、外資動向、市場槓桿（融資）、期權（台指選擇權的未平倉牆、外資台指期）；
卡片只描述狀態、不寫買賣（DECISIONS.md）。這個模組只負責把公開來源變成乾淨的資料，計算在 market_calc。

每個來源兩個函式（INTERFACES §3）：
- 純解析：收已解析的 JSON／文字，回 dataclass；壞資料丟 `ParseError(kind)`（kind 只是列舉值、不帶回應內容，
  outcome 只記列舉值）。回應本身就不是 JSON／CSV 時另帶程式自己的 detail（json、csv、header、columns），
  log 才分得出是哪一種 format（2026-10-23）。
- 抓取：組網址 → `http.fetch(...)`（預設 market_http，第一次抓才 import；測試注入假的）→ 自己用 utf-8-sig 嚴格解碼
  → 解析。`FetchError` 原樣往上丟。

口徑（PLAN §3.2、§18.4、§18.10–18.12）：
- ALLBUT0999：依表頭找表與欄（不靠表序）；根層 `date` 與表題的民國日期都要等於目標日 T；收盤只有字面「--」
  ＝當天無成交（None，不是 0），其他看不懂的值算壞列；漲跌號欄是 HTML，去標籤後只認 + - X 與空白；成交金額取
  「總計(1~15)」；漲跌家數取「股票」欄，「684(19)」括號內是漲停／跌停家數；「無比價」另計數、不算漲跌；
  收盤收全部普通股（4 碼數字）＋0050（job 保留最近 10 個交易日，新成分不必再打請求）。
- 三大法人：外資＝「外資及陸資(不含外資自營商)」；外資自營商已含在自營商內，不重複加。
- 休市表依「名稱＋說明」分類：放假／補假／休市、市場無交易（僅辦理結算交割）＝非交易日；開始交易日／最後交易日
  ＝交易日；看不懂（或同時符合兩類）的標 unknown 交給每日維護，不猜。
- 期交所：外資台指期分大台（臺股期貨）／小台（小型臺指期貨）／微台（微型臺指期貨），名稱完全相同才算；
  TXO 只收一般盤、預設只收月選（ContractMonth(Week) 原字串保留，週選不會被併進月選）；PCR 是帶 BOM 的 CSV。
  其他端點同一個網址 CDN 快取到 JSON 就回 JSON、快取到 CSV 就回 CSV（2026-10-23）：DailyMarketReportOpt 照實測的
  表頭兩種都收；表頭沒實測過的端點不猜對應，CSV 一律 format（`_taifex_body`）。
- FinMind：同一份 payload 同一天重複且值不同 → `ParseError("conflict")`（這檔這次整檔拒收）；
  msg 不是 success → `FinMindStatusError`（kind＝"status"，job 停這輪補洞）；來源修訂用 `finmind_revised_dates`
  找出來。
- MoneyDJ 0050 持股共用 A 的 `market_sources_us.parse_moneydj_holdings`（INTERFACES §3 B：只呼叫、不重寫）。

不 import main、stock_picks、fcn_eval、card_pages；import 時不做任何 I/O。
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence
from urllib.parse import quote, urlencode

import market_calc as _calc  # 共用的 ParseError 與 clean_external（2026-10-18 拿掉「還沒有 market_calc」時的本檔版本）

# 三個 sources 模組共用 market_calc 的那一個（job 才能一次接住）
ParseError = _calc.ParseError


class FinMindStatusError(ValueError):
    """FinMind 回應自己說失敗（msg 不是 success、status 不是 200，例如次數用完）：job 看到 kind＝"status" 就停這輪補洞。

    不是 ParseError：market_calc 的 ParseError 只收六種 kind；這個一樣是 ValueError、帶 `.kind`。"""

    def __init__(self, kind: str = "status"):  # 收一個參數：子程序之間 pickle 回來時會用 args 重建
        super().__init__("status")
        self.kind = "status"


# ── 端點與預算（PLAN §3.2、§9；最早時間是台北時間，資料還沒公布前不浪費次數） ─────────────────────
_KIB = 1024
_MIB = 1024 * 1024

TWSE_WWW = "https://www.twse.com.tw"
TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TAIFEX_OPENAPI = "https://openapi.taifex.com.tw/v1"
FINMIND_DATA = "https://api.finmindtrade.com/api/v4/data"


@dataclass(frozen=True)
class Endpoint:
    host: str
    earliest_tpe: Optional[time]  # 台北時間幾點後才抓；None＝不限
    daily_max: int  # 每天最多幾次（含重試與補洞），主機總量另由 HOST_DAILY_MAX 管
    max_bytes: int
    timeout_s: float  # 整個請求的總時間上限（market_http.fetch 的 timeout）
    accept: tuple[str, ...]


ENDPOINTS: dict[str, Endpoint] = {
    # 證交所 www：五個端點每天合計 12 次（Andrew 接受和 /股票 各自限速）。2026-10-23 起各端點上限加起來 13：
    # TWT49U 1→2（凌晨 00:0x 補昨天那次會用掉當天唯一的名額，16:00 那次從來沒跑過）；第 2 次只在凌晨補過昨天、
    # 或 16:00 那次要重試時才用到，合計仍由主機帳本擋在 12（擋下＝budget，沒送出、不算失敗）
    "twse_allbut0999": Endpoint("www.twse.com.tw", time(14, 30), 4, 2 * _MIB, 15.0, ("json",)),
    "twse_bfi82u": Endpoint("www.twse.com.tw", time(15, 30), 3, 256 * _KIB, 15.0, ("json",)),
    "twse_twt49u": Endpoint("www.twse.com.tw", time(16, 0), 2, 4 * _MIB, 15.0, ("json",)),
    "twse_margin_ms": Endpoint("www.twse.com.tw", time(21, 30), 3, 256 * _KIB, 15.0, ("json",)),
    "twse_holidays": Endpoint("www.twse.com.tw", None, 1, 256 * _KIB, 15.0, ("json",)),
    # 證交所 OpenAPI（另一個主機；資料本來就晚，不設最早時間）
    "openapi_bwibbu": Endpoint("openapi.twse.com.tw", None, 2, 4 * _MIB, 15.0, ("json",)),
    "openapi_revenue": Endpoint("openapi.twse.com.tw", None, 2, 4 * _MIB, 15.0, ("json",)),
    # 期交所 OpenAPI：常晚一天（實測 10/17 週六上午還是 10/15、10/18 週日中午才有 10/16），以回應裡的資料日為準；
    # 每天 6 次、間隔 3 小時（2026-10-18 審查 r6c I1：30 分鐘一次會在午夜後一小時就把次數用完）。實測（10/17）每個端點都回 application/octet-stream
    # （內容是 JSON，PutCallRatio 是 CSV），所以收 octet-stream；DailyMarketReportOpt 全量實測 846 KB。
    # 2026-10-23：期交所的檔案（外資台指期、DailyMarketReportOpt、匯率）都是約 06:36 才放上前一個交易日，00／03／06
    # 點那幾次一定白打 → 07:00 起才抓、間隔改 2 小時、每天 6→9 次（07、09…23 點；匯率仍每天 2 次：07、09 點）。
    # 10/23 的 09／12／15 點剛好碰上 CDN 給 CSV 的時段，額度就燒光了，要到隔天 00:09 才再試。
    # market_pipeline_tw.taifex_due 照這個最早時間擋；匯率也是同一個 CDN、同樣會碰到 CSV 的時段，2 次太少，改 5 次
    # （2026-10-23 審查 r9 gp1）；主機合計 4×9＋5＝41 次／天，在帳本的預設上限（200）內
    "taifex_futures_inst": Endpoint("openapi.taifex.com.tw", time(7, 0), 9, 1 * _MIB, 15.0, ("octet-stream", "json")),
    "taifex_calls_puts_inst": Endpoint("openapi.taifex.com.tw", time(7, 0), 9, 1 * _MIB, 15.0, ("octet-stream", "json")),
    "taifex_pcr": Endpoint("openapi.taifex.com.tw", time(7, 0), 9, 256 * _KIB, 15.0, ("octet-stream", "csv")),
    "taifex_daily_options": Endpoint(
        "openapi.taifex.com.tw", time(7, 0), 9, 4 * _MIB, 30.0, ("octet-stream", "json", "csv")
    ),
    "taifex_fx": Endpoint("openapi.taifex.com.tw", time(7, 0), 5, 256 * _KIB, 15.0, ("octet-stream", "json")),
    # FinMind：51 檔＋TAIEX 補洞；一年以上的 bootstrap 用 30 秒（TAIEX 全歷史約 6,900 列、1.5 MB）
    "finmind_prices": Endpoint("api.finmindtrade.com", None, 60, 4 * _MIB, 15.0, ("json",)),
    # 0050 持股實際走 A 的 market_sources_us.fetch_moneydj_holdings（它的 moneydj 端點）；這列給 job 排程用
    "moneydj_0050": Endpoint("www.moneydj.com", None, 2, 2 * _MIB, 15.0, ("html",)),
}
FINMIND_BOOTSTRAP_TIMEOUT_S = 30.0
HOST_DAILY_MAX = {"www.twse.com.tw": 12, "api.finmindtrade.com": 60}
HOST_MIN_INTERVAL_S = {
    "www.twse.com.tw": 10.0,
    "api.finmindtrade.com": 3.0,
    "openapi.twse.com.tw": 2.0,
    "openapi.taifex.com.tw": 2.0,
    "www.moneydj.com": 2.0,
}

# ── 代號與格式 ───────────────────────────────────────────────────────────────────────
# 用 [0-9] 不用 \d：\d 會吃全形與其他文字的數字
# 4–6 碼：4 碼數字開頭，後面最多兩個數字或大寫字母（ETF 00953B、特別股 1312A／2887Z1、TDR 6 碼）
_TW_CODE_RE = re.compile(r"[0-9]{4}[0-9A-Z]{0,2}")
_COMMON_STOCK_RE = re.compile(r"[1-9][0-9]{3}")  # 普通股；0 開頭是 ETF、帶字母是特別股、6 碼是 TDR／ETF
_FINMIND_ID_RE = re.compile(r"[0-9]{4,6}[A-Z]?|TAIEX")
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")
_INT_TEXT_RE = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)")
_NUM_TEXT_RE = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")
_COUNT_TEXT_RE = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\(([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\))?")
_ROC_TEXT_RE = re.compile(r"([0-9]{2,3})年([0-9]{1,2})月([0-9]{1,2})日")
_DASHES = frozenset({"-", "--", "---", ""})
_NO_DATA_MARK = "沒有符合條件"  # 證交所無資料時的 stat：「很抱歉，沒有符合條件的資料!」

MAX_BAD_CLOSE_ROWS = 5  # ALLBUT0999 普通股收盤壞列上限；超過代表欄位整個錯位，整份拒收


# ── 資料型別（欄位名與順序照 INTERFACES；新增欄位一律放最後並給預設值） ─────────────────────────
class TwClose(NamedTuple):
    close: Optional[float]  # None＝當天無成交（「--」）
    volume: int  # 成交股數
    change_sign: str  # "+"、"-"、"X"（不比價）、""（平盤或無成交）
    change: Optional[float]  # 有正負號的漲跌價差；不比價或無成交為 None


@dataclass(frozen=True)
class TwDaily:
    date: date
    taiex_close: float
    taiex_change: float  # 點數（有正負號）
    turnover_twd: int  # 「總計(1~15)」成交金額（元）
    adv: int  # 「股票」欄上漲家數
    dec: int
    flat: int
    closes: dict[str, TwClose]
    adv_limit: Optional[int] = None  # 漲停家數（「684(19)」括號內）
    dec_limit: Optional[int] = None
    untraded: Optional[int] = None  # 未成交家數（表上沒有這列時 None）
    no_compare: int = 0  # 無比價家數（不算漲跌）
    bad_rows: int = 0  # 普通股收盤列解析失敗或同代號衝突而丟掉的列數


@dataclass(frozen=True)
class Inst3:
    date: date
    foreign: int  # 外資及陸資(不含外資自營商) 買賣差額（元）
    trust: int
    dealer_self: int
    dealer_hedge: int


@dataclass(frozen=True)
class MarginTw:
    date: date
    margin_balance_k: int  # 融資金額今日餘額（仟元；證交所說以前日餘額為準，卡上寫「今日（參考）」）
    margin_prev_k: int
    short_units: int  # 融券今日餘額（交易單位）
    short_prev_units: int


@dataclass(frozen=True)
class ExRight:
    date: date
    code: str
    pre_close: float  # 除權息前收盤價
    ref_price: float  # 除權息參考價
    kind: str  # 「息」「權」「權息」（原字）


HOLIDAY_CLOSED = "closed"  # 放假、補假、休市
HOLIDAY_SETTLEMENT_ONLY = "settlement_only"  # 市場無交易，僅辦理結算交割
HOLIDAY_TRADING = "trading"  # 開始交易日、最後交易日
HOLIDAY_UNKNOWN = "unknown"  # 看不懂或同時符合兩類：報維護，不猜


@dataclass(frozen=True)
class Holiday:
    date: date
    name: str
    note: str
    kind: str = HOLIDAY_UNKNOWN

    @property
    def is_trading(self) -> Optional[bool]:
        """交易日 True、非交易日 False、看不懂 None。"""
        if self.kind == HOLIDAY_TRADING:
            return True
        if self.kind in (HOLIDAY_CLOSED, HOLIDAY_SETTLEMENT_ONLY):
            return False
        return None


@dataclass(frozen=True)
class Revenue:
    year: int
    month: int
    yoy: Optional[float]  # 營業收入-去年同月增減(%)；空白為 None


class FutPosition(NamedTuple):
    oi_long: int
    oi_short: int
    oi_net: int  # 未平倉淨口數（多−空）
    trade_net: int  # 當日交易淨口數


@dataclass(frozen=True)
class FuturesInst:
    date: date
    foreign: dict[str, FutPosition]  # "TX"（大台）一定有；"MTX"（小台）、"TMF"（微台）缺了就不放


class OptPosition(NamedTuple):
    oi_long: int
    oi_short: int
    oi_net: int
    oi_value_net_k: int  # 未平倉契約金額淨額（千元）


@dataclass(frozen=True)
class OptionsInst:
    date: date
    foreign_call: OptPosition
    foreign_put: OptPosition


@dataclass(frozen=True)
class PcrRow:
    date: date
    put_volume: int
    call_volume: int
    volume_ratio_pct: float
    put_oi: int
    call_oi: int
    oi_ratio_pct: float  # 買賣權未平倉量比率%（全部台指選擇權）


@dataclass(frozen=True)
class TxoRow:
    month: str  # ContractMonth(Week) 原字串（月選＝YYYYMM）
    strike: float
    kind: str  # "C"／"P"
    oi: Optional[int]  # 「-」為 None
    settle: Optional[float]
    date: Optional[date] = None  # 期交所資料日（常晚一天）


@dataclass(frozen=True)
class FxRow:
    date: date
    usd_ntd: float
    prev_date: Optional[date] = None  # 同一份資料裡的前一筆（日變化要核對它是不是前一個交易日）
    prev_usd_ntd: Optional[float] = None


@dataclass(frozen=True)
class TwBar:
    date: date
    close: float  # 原始（未還原）收盤
    volume: int  # 股
    money: int  # 元


# ── 共用小工具 ───────────────────────────────────────────────────────────────────────
def _squash(value: Any) -> str:
    """表頭與列名比對用：NFKC（全形括號變半形）＋去掉所有空白。"""
    if not isinstance(value, str):
        return ""
    return _SPACE_RE.sub("", unicodedata.normalize("NFKC", value))


def _text_number(value: Any) -> float:
    """「1,234.56」「-46,147,162,837」→ 數字；「--」、空白、亂碼都丟 format（呼叫端自己先處理「--」）。"""
    if not isinstance(value, str):
        raise ParseError("format")
    text = value.strip()
    if not _NUM_TEXT_RE.fullmatch(text):
        raise ParseError("format")
    number = float(text.replace(",", ""))
    if not math.isfinite(number):
        raise ParseError("format")
    return number


def _text_int(value: Any) -> int:
    if not isinstance(value, str):
        raise ParseError("format")
    text = value.strip()
    if not _INT_TEXT_RE.fullmatch(text):
        raise ParseError("format")
    return int(text.replace(",", ""))


def _dash_or_number(value: Any) -> Optional[float]:
    """期交所的「-」＝沒有值；其他照 `_text_number`。"""
    if isinstance(value, str) and value.strip() in _DASHES:
        return None
    return _text_number(value)


def _dash_or_int(value: Any) -> Optional[int]:
    if isinstance(value, str) and value.strip() in _DASHES:
        return None
    return _text_int(value)


def _json_number(value: Any) -> float:
    """JSON 數字（FinMind）：bool、NaN、inf、字串都不收。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParseError("format")
    number = float(value)
    if not math.isfinite(number):
        raise ParseError("format")
    return number


def _json_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ParseError("format")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ParseError("format")


def _ymd(value: Any) -> date:
    """「20261016」→ date。"""
    if not isinstance(value, str) or not (len(value.strip()) == 8 and value.strip().isascii() and value.strip().isdigit()):
        raise ParseError("format")
    text = value.strip()
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        raise ParseError("format") from None


def _iso(value: Any) -> date:
    """「2026-10-16」→ date。"""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value.strip()):
        raise ParseError("format")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ParseError("format") from None


def _roc_compact(value: Any) -> date:
    """OpenAPI 的「1151008」→ 2026-10-08。"""
    if not isinstance(value, str):
        raise ParseError("format")
    text = value.strip()
    if not (6 <= len(text) <= 7 and text.isascii() and text.isdigit()):
        raise ParseError("format")
    try:
        return date(int(text[:-4]) + 1911, int(text[-4:-2]), int(text[-2:]))
    except ValueError:
        raise ParseError("format") from None


def _roc_text(value: Any) -> date:
    """「115年06月01日」→ 2026-06-01。"""
    match = _ROC_TEXT_RE.fullmatch(_squash(value))
    if match is None:
        raise ParseError("format")
    try:
        return date(int(match.group(1)) + 1911, int(match.group(2)), int(match.group(3)))
    except ValueError:
        raise ParseError("format") from None


def _roc_month(value: Any) -> tuple[int, int]:
    """營收的資料年月「11508」→ (2026, 8)。"""
    if not isinstance(value, str):
        raise ParseError("format")
    text = value.strip()
    if not (4 <= len(text) <= 5 and text.isascii() and text.isdigit()):
        raise ParseError("format")
    year, month = int(text[:-2]) + 1911, int(text[-2:])
    if not 1 <= month <= 12:
        raise ParseError("format")
    return year, month


def _title_date(title: Any) -> Optional[date]:
    """表題開頭的民國日期（「115年10月16日 大盤統計資訊」）；沒有日期回 None。"""
    match = _ROC_TEXT_RE.match(_squash(title))
    if match is None:
        return None
    try:
        return date(int(match.group(1)) + 1911, int(match.group(2)), int(match.group(3)))
    except ValueError:
        raise ParseError("format") from None


def _check_title(title: Any, expect: date) -> None:
    """根層 date 可能只是回聲請求參數，表題的日期才是證交所實際給的那天。"""
    found = _title_date(title)
    if found is not None and found != expect:
        raise ParseError("date")


def _twse_stat(payload: Any) -> None:
    """證交所 stat：OK（大小寫不拘，休市表回小寫 ok）才往下；無資料 → empty；其他 → format。"""
    if not isinstance(payload, dict):
        raise ParseError("format")
    stat = payload.get("stat")
    if not isinstance(stat, str):
        raise ParseError("format")
    text = stat.strip()
    if text.upper() == "OK":
        return
    if _NO_DATA_MARK in text:
        raise ParseError("empty")
    raise ParseError("format")


def _sign(value: Any) -> str:
    """漲跌號欄：證交所給 HTML（兩種寫法 `<p style ='color:red'>+</p>`、`<p style= color:red>+</p>`），
    只看去掉標籤後的字，不看顏色。"""
    if not isinstance(value, str):
        raise ParseError("format")
    text = unicodedata.normalize("NFKC", html.unescape(_TAG_RE.sub("", value))).strip()
    if text in ("+", "-", ""):
        return text
    if text in ("X", "x"):
        return "X"
    raise ParseError("format")


class _Table(NamedTuple):
    title: Any
    fields: list[str]  # `_squash` 過的表頭
    data: list


def _twse_tables(payload: dict) -> list[_Table]:
    """證交所多表回應的 tables（空的 {} 跳過）；不是 list 就是格式壞了。"""
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise ParseError("format")
    out: list[_Table] = []
    for table in tables:
        if not isinstance(table, dict) or not table:
            continue
        fields, data = table.get("fields"), table.get("data")
        if not isinstance(fields, list) or not isinstance(data, list):
            continue
        out.append(_Table(table.get("title"), [_squash(name) for name in fields], data))
    return out


def _field_index(fields: list[str], names: Sequence[str]) -> Optional[dict[str, int]]:
    """表頭裡每個名字的位置；缺任何一個回 None（這張不是要找的表）。表頭重複也當不是。"""
    index: dict[str, int] = {}
    for name in names:
        key = _squash(name)
        if fields.count(key) != 1:
            return None
        index[name] = fields.index(key)
    return index


def _cell(row: Any, position: int) -> Any:
    if not isinstance(row, list) or position >= len(row):
        raise ParseError("format")
    return row[position]


def _label(row: Any, position: int) -> str:
    """列名（比對用）；列太短或不是 list 回空字串：不相干的列壞掉不該讓整份失敗，要用的那列才嚴格檢查。"""
    if not isinstance(row, list) or position >= len(row):
        return ""
    return _squash(row[position])


def _json_body(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ParseError("format", "json") from None  # 2026-10-23：detail 讓 log 分得出「不是 JSON」和其他 format


def _http_module(http: Any) -> Any:
    if http is None:
        import market_http as http  # noqa: PLC0415 - 第一次抓才載入，import 本模組不拉 requests

    return http


def _get(http: Any, ledger: Any, name: str, url: str, *, timeout: Optional[float] = None) -> bytes:
    endpoint = ENDPOINTS[name]
    response = _http_module(http).fetch(
        url,
        max_bytes=endpoint.max_bytes,
        timeout=endpoint.timeout_s if timeout is None else timeout,
        accept=endpoint.accept,
        ledger=ledger,
    )
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        raise ParseError("format")
    return bytes(body)


def _keep_unique(table: dict, conflicts: set, key: Any, value: Any) -> None:
    """同一份資料同鍵出現兩次：值一樣就算了，不一樣記衝突（不採最後一筆）。"""
    if key in table and table[key] != value:
        conflicts.add(key)
        return
    table.setdefault(key, value)


# ── 證交所 ALLBUT0999（加權指數、成交金額、漲跌家數、全部收盤） ────────────────────────────
_TAIEX_NAME = "發行量加權股價指數"
_INDEX_FIELDS = ("指數", "收盤指數", "漲跌(+/-)", "漲跌點數")
_TURNOVER_FIELDS = ("成交統計", "成交金額(元)")
_AD_FIELDS = ("類型", "股票")
_CLOSE_FIELDS = ("證券代號", "收盤價", "成交股數", "漲跌(+/-)", "漲跌價差")
_AD_LABELS = (("上漲", "adv"), ("下跌", "dec"), ("持平", "flat"), ("未成交", "untraded"), ("無比價", "no_compare"))


def _parse_taiex(tables: list[_Table]) -> tuple[float, float]:
    hits: set[tuple[float, float]] = set()
    for table in tables:
        index = _field_index(table.fields, _INDEX_FIELDS)
        if index is None:
            continue
        pct_at = table.fields.index("漲跌百分比(%)") if table.fields.count("漲跌百分比(%)") == 1 else None
        for row in table.data:
            if _label(row, index["指數"]) != _TAIEX_NAME:
                continue
            close = _text_number(_cell(row, index["收盤指數"]))
            points = _text_number(_cell(row, index["漲跌點數"]))
            sign = _sign(_cell(row, index["漲跌(+/-)"]))
            if close <= 0 or points < 0 or sign == "X" or (sign == "" and points != 0):
                raise ParseError("format")
            change = -points if sign == "-" else points
            if pct_at is not None:
                # 去 HTML 的漲跌號用有正負號的百分比再核一次：標籤寫法變了不會悄悄把漲寫成跌
                pct = _text_number(_cell(row, pct_at))
                if (pct > 0 and change < 0) or (pct < 0 and change > 0):
                    raise ParseError("conflict")
            hits.add((close, change))
    if not hits:
        raise ParseError("missing_field")
    if len(hits) > 1:
        raise ParseError("conflict")
    return hits.pop()


def _parse_turnover(tables: list[_Table]) -> int:
    hits: list[int] = []
    for table in tables:
        index = _field_index(table.fields, _TURNOVER_FIELDS)
        if index is None:
            continue
        for row in table.data:
            # 「證券合計(1+6+14+15)」只有股票類，會讓倍數低 8%；要的是「總計(1~15)」
            if _label(row, index["成交統計"]).startswith("總計"):
                hits.append(_text_int(_cell(row, index["成交金額(元)"])))
    if not hits:
        raise ParseError("missing_field")
    if len(set(hits)) > 1:
        raise ParseError("conflict")
    if hits[0] <= 0:
        raise ParseError("format")
    return hits[0]


def _parse_counts(tables: list[_Table]) -> dict[str, tuple[int, Optional[int]]]:
    results: list[dict[str, tuple[int, Optional[int]]]] = []
    for table in tables:
        index = _field_index(table.fields, _AD_FIELDS)
        if index is None:
            continue
        counts: dict[str, tuple[int, Optional[int]]] = {}
        for row in table.data:
            label = _label(row, index["類型"])
            key = next((name for prefix, name in _AD_LABELS if label.startswith(prefix)), None)
            if key is None:
                continue
            match = _COUNT_TEXT_RE.fullmatch(_squash(_cell(row, index["股票"])))
            if match is None:
                raise ParseError("format")
            value = (int(match.group(1).replace(",", "")), int(match.group(2).replace(",", "")) if match.group(2) else None)
            if key in counts and counts[key] != value:
                raise ParseError("conflict")
            counts[key] = value
        results.append(counts)
    if not results:
        raise ParseError("missing_field")
    if any(other != results[0] for other in results[1:]):
        raise ParseError("conflict")
    counts = results[0]
    if any(key not in counts for key in ("adv", "dec", "flat", "no_compare")):
        raise ParseError("missing_field")
    return counts


def _parse_close_row(row: list, index: dict[str, int]) -> TwClose:
    close_text = _cell(row, index["收盤價"])
    if not isinstance(close_text, str):
        raise ParseError("format")
    if close_text.strip() == "--":
        # 無成交：漲跌號與價差沒有意義，不拿來判斷壞列（零股、鉅額可能讓成交股數 >0）
        volume_text = _cell(row, index["成交股數"])
        volume = 0 if isinstance(volume_text, str) and volume_text.strip() in ("--", "") else _text_int(volume_text)
        if volume < 0:
            raise ParseError("format")
        return TwClose(None, volume, "", None)
    close = _text_number(close_text)
    volume = _text_int(_cell(row, index["成交股數"]))
    sign = _sign(_cell(row, index["漲跌(+/-)"]))
    magnitude = _text_number(_cell(row, index["漲跌價差"]))
    if close <= 0 or volume < 0 or magnitude < 0:
        raise ParseError("format")
    if sign == "X":
        change: Optional[float] = None
    elif sign == "":
        if magnitude != 0:
            raise ParseError("format")
        change = 0.0
    else:
        change = -magnitude if sign == "-" else magnitude
    return TwClose(close, volume, sign, change)


def _parse_closes(tables: list[_Table], extra_codes: frozenset[str]) -> tuple[dict[str, TwClose], int]:
    closes: dict[str, TwClose] = {}
    conflicts: set[str] = set()
    bad = 0
    found = False
    for table in tables:
        index = _field_index(table.fields, _CLOSE_FIELDS)
        if index is None:
            continue
        found = True
        for row in table.data:
            raw_code = row[index["證券代號"]] if isinstance(row, list) and index["證券代號"] < len(row) else None
            if not isinstance(raw_code, str):
                bad += 1
                continue
            code = raw_code.strip()
            if not (_COMMON_STOCK_RE.fullmatch(code) or code in extra_codes):
                continue
            try:
                item = _parse_close_row(row, index)
            except ParseError:
                bad += 1
                continue
            _keep_unique(closes, conflicts, code, item)
    if not found:
        raise ParseError("missing_field")
    for code in conflicts:
        closes.pop(code, None)
        bad += 1
    if bad > MAX_BAD_CLOSE_ROWS:
        raise ParseError("format")
    if any(code not in closes for code in extra_codes):
        raise ParseError("missing_field")
    if not any(_COMMON_STOCK_RE.fullmatch(code) for code in closes):
        raise ParseError("too_few")
    return closes, bad


def parse_twse_allbut0999(payload: Any, expect_date: date, *, extra_codes: Sequence[str] = ("0050",)) -> TwDaily:
    """證交所 `MI_INDEX?type=ALLBUT0999` 的一天。

    依表頭找四張表：價格指數（加權指數收盤與漲跌）、大盤統計資訊（「總計(1~15)」成交金額）、漲跌證券數合計
    （「股票」欄）、每日收盤行情（普通股＋`extra_codes`；`extra_codes` 每一檔都要在，預設 0050）。
    無資料（休市、還沒公布）→ empty；日期不是 `expect_date` → date；壞列超過 `MAX_BAD_CLOSE_ROWS` → format。
    """
    extras = frozenset(extra_codes)
    if any(not isinstance(code, str) or not _TW_CODE_RE.fullmatch(code) for code in extras):
        raise ValueError("extra_codes")
    _twse_stat(payload)
    if _ymd(payload.get("date")) != expect_date:
        raise ParseError("date")
    tables = _twse_tables(payload)
    for table in tables:
        _check_title(table.title, expect_date)
    taiex_close, taiex_change = _parse_taiex(tables)
    turnover = _parse_turnover(tables)
    counts = _parse_counts(tables)
    closes, bad = _parse_closes(tables, extras)
    untraded = counts.get("untraded")
    return TwDaily(
        date=expect_date,
        taiex_close=taiex_close,
        taiex_change=taiex_change,
        turnover_twd=turnover,
        adv=counts["adv"][0],
        dec=counts["dec"][0],
        flat=counts["flat"][0],
        closes=closes,
        adv_limit=counts["adv"][1],
        dec_limit=counts["dec"][1],
        untraded=untraded[0] if untraded else None,
        no_compare=counts["no_compare"][0],
        bad_rows=bad,
    )


# ── 證交所 BFI82U（三大法人買賣金額） ───────────────────────────────────────────────────
_BFI_FIELDS = ("單位名稱", "買進金額", "賣出金額", "買賣差額")
_BFI_ROWS = {
    _squash("自營商(自行買賣)"): "dealer_self",
    _squash("自營商(避險)"): "dealer_hedge",
    _squash("投信"): "trust",
    # 外資自營商已計入自營商（證交所表註），不另加
    _squash("外資及陸資(不含外資自營商)"): "foreign",
}
_BFI_TOTAL = _squash("合計")


def _payload_day(payload: dict, expect_date: Optional[date]) -> date:
    day = _ymd(payload.get("date"))
    if expect_date is not None and day != expect_date:
        raise ParseError("date")
    return day


def parse_twse_bfi82u(payload: Any, *, expect_date: Optional[date] = None) -> Inst3:
    """證交所 `BFI82U?type=day`：外資＝「外資及陸資(不含外資自營商)」的買賣差額（元）。

    每列核買進−賣出＝差額、四項合計＝「合計」列（外資自營商不在合計內），對不上 → conflict。
    """
    _twse_stat(payload)
    day = _payload_day(payload, expect_date)
    _check_title(payload.get("title"), day)
    fields = payload.get("fields")
    data = payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise ParseError("format")
    index = _field_index([_squash(name) for name in fields], _BFI_FIELDS)
    if index is None:
        raise ParseError("missing_field")
    values: dict[str, int] = {}
    total: Optional[int] = None
    for row in data:
        label = _label(row, index["單位名稱"])
        if label not in _BFI_ROWS and label != _BFI_TOTAL:
            continue
        buy, sell = _text_int(_cell(row, index["買進金額"])), _text_int(_cell(row, index["賣出金額"]))
        net = _text_int(_cell(row, index["買賣差額"]))
        if buy - sell != net:
            raise ParseError("conflict")
        if label == _BFI_TOTAL:
            total = net
            continue
        key = _BFI_ROWS[label]
        if key in values and values[key] != net:
            raise ParseError("conflict")
        values[key] = net
    if len(values) != len(_BFI_ROWS):
        raise ParseError("missing_field")
    if total is not None and total != sum(values.values()):
        raise ParseError("conflict")
    return Inst3(day, values["foreign"], values["trust"], values["dealer_self"], values["dealer_hedge"])


# ── 證交所 MI_MARGN（信用交易統計） ─────────────────────────────────────────────────────
_MARGIN_FIELDS = ("項目", "前日餘額", "今日餘額")
_MARGIN_AMOUNT = _squash("融資金額(仟元)")
_SHORT_UNITS = _squash("融券(交易單位)")


def parse_twse_margin_ms(payload: Any, *, expect_date: Optional[date] = None) -> MarginTw:
    """證交所 `MI_MARGN?selectType=MS`：融資金額（仟元）與融券（交易單位）的今日、前日餘額。"""
    _twse_stat(payload)
    day = _payload_day(payload, expect_date)
    tables = _twse_tables(payload)
    found: dict[str, tuple[int, int]] = {}
    for table in tables:
        _check_title(table.title, day)
        index = _field_index(table.fields, _MARGIN_FIELDS)
        if index is None:
            continue
        for row in table.data:
            label = _label(row, index["項目"])
            if label not in (_MARGIN_AMOUNT, _SHORT_UNITS):
                continue
            value = (_text_int(_cell(row, index["今日餘額"])), _text_int(_cell(row, index["前日餘額"])))
            if value[0] < 0 or value[1] < 0:
                raise ParseError("format")
            if label in found and found[label] != value:
                raise ParseError("conflict")
            found[label] = value
    if _MARGIN_AMOUNT not in found or _SHORT_UNITS not in found:
        raise ParseError("missing_field")
    (balance, prev), (short, short_prev) = found[_MARGIN_AMOUNT], found[_SHORT_UNITS]
    return MarginTw(day, balance, prev, short, short_prev)


# ── 證交所 TWT49U（除權除息計算結果） ───────────────────────────────────────────────────
_TWT49U_FIELDS = ("資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "權/息")


def parse_twse_twt49u(
    payload: Any, *, expect_start: Optional[date] = None, expect_end: Optional[date] = None
) -> list[ExRight]:
    """證交所 `TWT49U?startDate&endDate`：每筆除權息的前收與參考價（不篩代號：0050 自己也會除息）。

    回應的 strDate／endDate 要等於請求區間（job 用它記 coverage）；證交所回「沒有符合條件的資料」→ 這段沒有事件，回 []。
    表註：不含減資／分割減資（台股 ±10% 隔離會接住）。
    """
    if not isinstance(payload, dict):
        raise ParseError("format")
    stat = payload.get("stat")
    if isinstance(stat, str) and _NO_DATA_MARK in stat:
        return []
    _twse_stat(payload)
    start, end = _ymd(payload.get("strDate")), _ymd(payload.get("endDate"))
    if start > end or (expect_start is not None and start != expect_start) or (expect_end is not None and end != expect_end):
        raise ParseError("date")
    fields, data = payload.get("fields"), payload.get("data", [])
    if not isinstance(fields, list) or not isinstance(data, list):
        raise ParseError("format")
    index = _field_index([_squash(name) for name in fields], _TWT49U_FIELDS)
    if index is None:
        raise ParseError("missing_field")
    events: dict[tuple[date, str], ExRight] = {}
    for row in data:
        day = _roc_text(_cell(row, index["資料日期"]))
        if not start <= day <= end:
            raise ParseError("date")
        code = _cell(row, index["股票代號"])
        if not isinstance(code, str) or not _TW_CODE_RE.fullmatch(code.strip()):
            raise ParseError("format")
        pre_close = _text_number(_cell(row, index["除權息前收盤價"]))
        ref_price = _text_number(_cell(row, index["除權息參考價"]))
        kind = _squash(_cell(row, index["權/息"]))
        if pre_close <= 0 or ref_price <= 0 or not kind:
            raise ParseError("format")
        event = ExRight(day, code.strip(), pre_close, ref_price, kind)
        key = (day, event.code)
        if key in events and events[key] != event:
            raise ParseError("conflict")
        events[key] = event
    return [events[key] for key in sorted(events)]


# ── 證交所休市表 ────────────────────────────────────────────────────────────────────
_HOLIDAY_FIELDS = ("日期", "名稱", "說明")
# 順序無關；同時符合兩類就是 unknown。不能只比「交易」兩個字（「市場無交易」也有）。
_HOLIDAY_RULES = (
    (HOLIDAY_TRADING, ("開始交易", "最後交易")),
    (HOLIDAY_SETTLEMENT_ONLY, ("無交易",)),
    # 2026-10-18 審查 r6c Q1：颱風等臨時停市的寫法（真實表還沒有實例；認不出會整天變 unknown、均線窗口壞很久）
    (HOLIDAY_CLOSED, ("放假", "補假", "休市", "停止交易", "停止上班", "颱風", "天然災害")),
)


def classify_twse_holiday(name: str, note: str) -> str:
    """休市表一列的種類：結算交割那幾天字在「名稱」、說明是空的，所以名稱與說明一起看。"""
    text = _squash(name) + "|" + _squash(note)
    kinds = {kind for kind, words in _HOLIDAY_RULES if any(word in text for word in words)}
    return kinds.pop() if len(kinds) == 1 else HOLIDAY_UNKNOWN


HOLIDAY_NAME_MAX = 24
HOLIDAY_NOTE_MAX = 80


def _clean_text(value: Any, max_len: int) -> str:
    """外部短字串（休市表名稱／說明）走 market_calc.clean_external（PLAN §19.1）；洗不過回空字串。"""
    return _calc.clean_external(value, max_len) or ""


def parse_twse_holidays(payload: Any, year: int) -> list[Holiday]:
    """證交所 `holidaySchedule?date=YYYY0101`：當年度特別開休市日（不含一般週末）。

    queryYear 與每列日期都要在 `year`；total 要等於列數；同一天出現兩種說法 → 那天標 unknown。
    """
    _twse_stat(payload)
    query_year = payload.get("queryYear")
    if query_year is not None:
        if isinstance(query_year, bool) or str(query_year).strip() != str(year):
            raise ParseError("date")
    fields, data = payload.get("fields"), payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise ParseError("format")
    if not data:
        raise ParseError("empty")
    total = payload.get("total")
    if isinstance(total, int) and not isinstance(total, bool) and total != len(data):
        raise ParseError("too_few" if len(data) < total else "format")
    index = _field_index([_squash(name) for name in fields], _HOLIDAY_FIELDS)
    if index is None:
        raise ParseError("missing_field")
    rows: list[Holiday] = []
    for row in data:
        raw_day = _cell(row, index["日期"])
        try:
            day = _iso(raw_day)
        except ParseError:
            day = _roc_text(raw_day)
        if day.year != year:
            raise ParseError("date")
        raw_name, raw_note = _cell(row, index["名稱"]), _cell(row, index["說明"])
        # 分類看原文（清洗會截長度、可能整則丟掉），存下來的名稱／說明才用清洗過的
        kind = classify_twse_holiday(raw_name, raw_note)
        name, note = _clean_text(raw_name, HOLIDAY_NAME_MAX), _clean_text(raw_note, HOLIDAY_NOTE_MAX)
        rows.append(Holiday(day, name, note, kind))
    kinds_by_day: dict[date, set[str]] = {}
    for item in rows:
        kinds_by_day.setdefault(item.date, set()).add(item.kind)
    rows = [
        item if len(kinds_by_day[item.date]) == 1 else Holiday(item.date, item.name, item.note, HOLIDAY_UNKNOWN)
        for item in rows
    ]
    return sorted(rows, key=lambda item: item.date)


# ── 證交所 OpenAPI（本益比、月營收） ────────────────────────────────────────────────────
def _openapi_rows(rows: Any) -> list[dict]:
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ParseError("format")
    if not rows:
        raise ParseError("empty")
    return rows


def bwibbu_date(rows: Any) -> date:
    """BWIBBU_ALL 的資料日（每列 Date 都要同一天，否則 date）。"""
    days = {_roc_compact(row.get("Date")) for row in _openapi_rows(rows)}
    if len(days) != 1:
        raise ParseError("date")
    return days.pop()


def parse_openapi_bwibbu(rows: Any, *, expect_date: Optional[date] = None) -> dict[str, Optional[float]]:
    """`/v1/exchangeReport/BWIBBU_ALL` → {代號: 本益比}；「-」、空白、≤0（虧損）為 None；同代號值不同 → 那檔不收。

    資料日用 `bwibbu_date(rows)` 取（`expect_date` 給了就要相等）。
    """
    day = bwibbu_date(rows)
    if expect_date is not None and day != expect_date:
        raise ParseError("date")
    table: dict[str, Optional[float]] = {}
    conflicts: set[str] = set()
    for row in rows:
        code = row.get("Code")
        if not isinstance(code, str) or not _TW_CODE_RE.fullmatch(code.strip()):
            continue
        raw = row.get("PEratio")
        if raw is None or (isinstance(raw, str) and raw.strip() in _DASHES | {"N/A"}):
            pe: Optional[float] = None
        else:
            value = _text_number(raw)
            pe = value if value > 0 else None
        _keep_unique(table, conflicts, code.strip(), pe)
    for code in conflicts:
        del table[code]
    if not table:
        raise ParseError("missing_field")
    return table


def parse_openapi_revenue(rows: Any) -> dict[str, Revenue]:
    """`/v1/opendata/t187ap05_L` → {代號: Revenue}；同代號取最新月份，同一個月兩筆不一樣 → 那檔不收。"""
    latest: dict[str, Revenue] = {}
    conflicts: set[tuple[str, int, int]] = set()
    for row in _openapi_rows(rows):
        code = row.get("公司代號")
        if not isinstance(code, str) or not _TW_CODE_RE.fullmatch(code.strip()):
            continue
        year, month = _roc_month(row.get("資料年月"))
        raw = row.get("營業收入-去年同月增減(%)")
        yoy = None if raw is None or (isinstance(raw, str) and raw.strip() in _DASHES | {"N/A"}) else _text_number(raw)
        revenue = Revenue(year, month, yoy)
        old = latest.get(code.strip())
        if old is None or (old.year, old.month) < (year, month):
            latest[code.strip()] = revenue
        elif (old.year, old.month) == (year, month) and old != revenue:
            conflicts.add((code.strip(), year, month))
    for code, year, month in conflicts:
        current = latest.get(code)
        if current is not None and (current.year, current.month) == (year, month):
            del latest[code]
    if not latest:
        raise ParseError("missing_field")
    return latest


# ── 期交所 OpenAPI ───────────────────────────────────────────────────────────────────
# 名稱完全相同才算（「小型電子期貨」也是「小型」開頭；期交所用「臺」不是「台」）
TAIFEX_FUTURES = {"臺股期貨": "TX", "小型臺指期貨": "MTX", "微型臺指期貨": "TMF"}
_FOREIGN_ITEM = "外資及陸資"
_CALL_PUT = {"CALL": "C", "買權": "C", "PUT": "P", "賣權": "P"}


def _taifex_rows(rows: Any) -> tuple[list[dict], date]:
    """期交所 JSON：每列 Date（YYYYMMDD）都要同一天，回 (rows, 資料日)。"""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ParseError("format")
    if not rows:
        raise ParseError("empty")
    days = {_ymd(row.get("Date")) for row in rows}
    if len(days) != 1:
        raise ParseError("date")
    return rows, days.pop()


def _taifex_body(body: bytes, csv_columns: Optional[Sequence[tuple[str, str]]] = None) -> Any:
    """期交所 OpenAPI 的回應 → JSON 形狀的列（交給原本的 JSON 解析）。

    2026-10-23 實測：期交所前面的 Cloudflare 同一個網址快取到 JSON 就回 JSON、快取到 CSV 就回 CSV，不看 Accept
    （送 Accept: application/json 也拿到同一份 CSV）；只收 JSON 的話，快取是 CSV 的那幾個小時整個端點都是 format。
    去掉 BOM 與空白後開頭是 [ 或 { 就照原本當 JSON；其他當 CSV（UTF-8 BOM、中文表頭）：`csv_columns` 是
    (英文鍵, 中文表頭) 照欄序，表頭要一字不差、順序也相同，才照位置換成英文鍵。沒有實測過 CSV 表頭的端點
    （`csv_columns` 是 None）不猜對應，CSV 一律 format。detail 只放程式自己的字，log 才看得出是哪一種：
    json（像 JSON 卻解析不了）、csv（不收 CSV 或讀不了）、header（表頭不是實測那一份）、columns（某列欄數不對，例如截斷）。
    """
    head = body[3:] if body.startswith(b"\xef\xbb\xbf") else body
    if head.lstrip()[:1] in (b"[", b"{"):
        return _json_body(body)
    if head.lstrip()[:1] == b"<":  # 維護頁／錯誤頁：log 上不能像「期交所改了表頭」（2026-10-23 審查 r9 gp1）
        raise ParseError("format", "html")
    if csv_columns is None:
        raise ParseError("format", "csv")
    try:
        lines = [line for line in csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")) if line]
    except (UnicodeDecodeError, csv.Error):
        raise ParseError("format", "csv") from None
    if not lines or lines[0] != [name for _, name in csv_columns]:
        raise ParseError("format", "header")
    keys = [key for key, _ in csv_columns]
    rows = []
    for line in lines[1:]:
        if len(line) != len(keys):
            raise ParseError("format", "columns")  # 截斷或欄位錯位：整份拒收，不猜哪幾欄對得上
        rows.append(dict(zip(keys, line)))
    return rows


def parse_taifex_futures_inst(rows: Any) -> FuturesInst:
    """`MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate`：外資的大台／小台／微台分開。

    卡上只寫大台（外資台指期淨未平倉）；小台、微台列在細節。每列核多−空＝淨，對不上 → conflict。
    """
    rows, day = _taifex_rows(rows)
    foreign: dict[str, FutPosition] = {}
    for row in rows:
        contract = row.get("ContractCode")
        if _squash(row.get("Item")) != _FOREIGN_ITEM or not isinstance(contract, str):
            continue
        code = TAIFEX_FUTURES.get(_squash(contract))
        if code is None:
            continue
        position = FutPosition(
            _text_int(row.get("OpenInterest(Long)")),
            _text_int(row.get("OpenInterest(Short)")),
            _text_int(row.get("OpenInterest(Net)")),
            _text_int(row.get("TradingVolume(Net)")),
        )
        if position.oi_long < 0 or position.oi_short < 0 or position.oi_long - position.oi_short != position.oi_net:
            raise ParseError("conflict")
        if code in foreign and foreign[code] != position:
            raise ParseError("conflict")
        foreign[code] = position
    if "TX" not in foreign:
        raise ParseError("missing_field")
    return FuturesInst(day, foreign)


def parse_taifex_calls_puts_inst(rows: Any, *, contract: str = "臺指選擇權") -> OptionsInst:
    """`MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate`：外資台指選擇權買權、賣權的淨未平倉。"""
    rows, day = _taifex_rows(rows)
    found: dict[str, OptPosition] = {}
    for row in rows:
        if _squash(row.get("ContractCode")) != _squash(contract) or _squash(row.get("Item")) != _FOREIGN_ITEM:
            continue
        side = _CALL_PUT.get(_squash(row.get("CallPut")).upper())
        if side is None:
            raise ParseError("format")
        position = OptPosition(
            _text_int(row.get("OpenInterest(Long)")),
            _text_int(row.get("OpenInterest(Short)")),
            _text_int(row.get("OpenInterest(Net)")),
            _text_int(row.get("ContractValueofOpenInterest(Net)(Thousands)")),
        )
        if position.oi_long < 0 or position.oi_short < 0 or position.oi_long - position.oi_short != position.oi_net:
            raise ParseError("conflict")
        if side in found and found[side] != position:
            raise ParseError("conflict")
        found[side] = position
    if "C" not in found or "P" not in found:
        raise ParseError("missing_field")
    return OptionsInst(day, found["C"], found["P"])


_PCR_COLUMNS = (
    ("date", "日期"),
    ("put_volume", "賣權成交量"),
    ("call_volume", "買權成交量"),
    ("volume_ratio_pct", "買賣權成交量比率%"),
    ("put_oi", "賣權未平倉量"),
    ("call_oi", "買權未平倉量"),
    ("oi_ratio_pct", "買賣權未平倉量比率%"),
)


def _ratio_matches(put: int, call: int, pct: float) -> bool:
    """期交所的比率是兩位小數：和自己算的差 >0.011 代表欄位錯位。"""
    if call <= 0:
        return True
    return abs(put / call * 100 - pct) <= 0.011


# 2026-10-18 實測：同一個網址今天回 JSON（英文欄名），10/17 抓的樣本是 CSV（中文表頭）；兩種都收
_PCR_JSON_KEYS = (
    ("date", "Date"),
    ("put_volume", "PutVolume"),
    ("call_volume", "CallVolume"),
    ("volume_ratio_pct", "PutCallVolumeRatio%"),
    ("put_oi", "PutOI"),
    ("call_oi", "CallOI"),
    ("oi_ratio_pct", "PutCallOIRatio%"),
)


def _pcr_row(values: Mapping[str, Any]) -> PcrRow:
    raw_day = values["date"].strip() if isinstance(values["date"], str) else values["date"]
    day = _ymd(raw_day.replace("/", "") if isinstance(raw_day, str) else raw_day)
    item = PcrRow(
        day,
        _text_int(values["put_volume"]),
        _text_int(values["call_volume"]),
        _text_number(values["volume_ratio_pct"]),
        _text_int(values["put_oi"]),
        _text_int(values["call_oi"]),
        _text_number(values["oi_ratio_pct"]),
    )
    if min(item.put_volume, item.call_volume, item.put_oi, item.call_oi) < 0:
        raise ParseError("format")
    if not (
        _ratio_matches(item.put_volume, item.call_volume, item.volume_ratio_pct)
        and _ratio_matches(item.put_oi, item.call_oi, item.oi_ratio_pct)
    ):
        raise ParseError("conflict")
    return item


def _pcr_by_day(items: Iterable[PcrRow]) -> list[PcrRow]:
    by_day: dict[date, PcrRow] = {}
    for item in items:
        if item.date in by_day and by_day[item.date] != item:
            raise ParseError("conflict")
        by_day[item.date] = item
    if not by_day:
        raise ParseError("empty")
    return [by_day[day] for day in sorted(by_day)]


def parse_taifex_pcr_csv(text: Any) -> list[PcrRow]:
    """`/v1/PutCallRatio`（近一個月、新到舊）→ 依日期由舊到新。收 CSV（UTF-8 BOM、中文表頭）或 JSON（英文欄名）。

    依表頭／欄名找欄；比率和兩個量自己算的對不上 → conflict；同一天兩列不一樣 → conflict。
    """
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseError("format") from None
    if not isinstance(text, str):
        raise ParseError("format")
    if text.lstrip("\ufeff \t\r\n")[:1] in ("[", "{"):
        try:
            payload = json.loads(text.lstrip("\ufeff"))
        except ValueError:
            raise ParseError("format") from None
        if not isinstance(payload, list):
            raise ParseError("format")
        if not payload:
            raise ParseError("empty")
        rows = []
        for entry in payload:
            if not isinstance(entry, dict) or any(key not in entry for _, key in _PCR_JSON_KEYS):
                raise ParseError("missing_field")
            rows.append(_pcr_row({name: entry[key] for name, key in _PCR_JSON_KEYS}))
        return _pcr_by_day(rows)
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    try:
        lines = [line for line in reader if any(cell.strip() for cell in line)]
    except csv.Error:
        raise ParseError("format") from None
    if not lines:
        raise ParseError("empty")
    header = [_squash(cell.lstrip("\ufeff")) for cell in lines[0]]
    index = _field_index(header, [name for _, name in _PCR_COLUMNS])
    if index is None:
        raise ParseError("missing_field")
    return _pcr_by_day(_pcr_row({key: _cell(line, index[name]) for key, name in _PCR_COLUMNS}) for line in lines[1:])


_MONTHLY_RE = re.compile(r"([0-9]{4})([0-9]{2})")
_CONTRACT_MONTH_RE = re.compile(r"[0-9]{6}[0-9A-Z]{0,4}")
# 2026-10-23：DailyMarketReportOpt 的 CSV 表頭（10/23 實測整份 11,302 列，UTF-8 BOM、18 欄），和 10/17 的 JSON 樣本
# 18 個英文鍵一一對應、順序相同；照位置換回英文鍵後原本的解析就能用（期交所改了表頭就 format／header，不猜）
_DAILY_OPT_CSV = (
    ("Date", "日期"),
    ("Contract", "契約"),
    ("ContractMonth(Week)", "到期月份(週別)"),
    ("StrikePrice", "履約價"),
    ("CallPut", "買賣權"),
    ("Open", "開盤價"),
    ("High", "最高價"),
    ("Low", "最低價"),
    ("Close", "最後成交價"),
    ("Volume", "成交量"),
    ("SettlementPrice", "結算價"),
    ("OpenInterest", "未沖銷契約量"),
    ("BestBid", "最後最佳買價"),
    ("BestAsk", "最後最佳賣價"),
    ("HistoricalHigh", "歷史最高價"),
    ("HistoricalLow", "歷史最低價"),
    ("TradingHalt", "是否因訊息面暫停交易"),
    ("TradingSession", "交易時段"),
)


def parse_taifex_daily_options(rows: Any, contract: str = "TXO", *, monthly_only: bool = True) -> list[TxoRow]:
    """`/v1/DailyMarketReportOpt` → 指定商品（預設 TXO）一般盤的各履約價未平倉與結算價。

    盤後盤同一鍵會再出現一次（值是「-」），只收一般盤；`monthly_only` 預設只收月選（ContractMonth(Week)＝YYYYMM），
    月份字串照原樣保留，週選（例 202610W2）不會被併進 202610。同鍵兩列不一樣 → conflict。
    """
    rows, day = _taifex_rows(rows)
    out: dict[tuple[str, float, str], TxoRow] = {}
    for row in rows:
        if _squash(row.get("Contract")) != contract or _squash(row.get("TradingSession")) != "一般":
            continue
        month = row.get("ContractMonth(Week)")
        if not isinstance(month, str) or not _CONTRACT_MONTH_RE.fullmatch(month.strip()):
            raise ParseError("format")
        month = month.strip()
        monthly = _MONTHLY_RE.fullmatch(month)
        if monthly is not None and not 1 <= int(monthly.group(2)) <= 12:
            raise ParseError("format")
        if monthly_only and monthly is None:
            continue
        kind = _CALL_PUT.get(_squash(row.get("CallPut")).upper())
        strike = _text_number(row.get("StrikePrice"))
        oi = _dash_or_int(row.get("OpenInterest"))
        settle = _dash_or_number(row.get("SettlementPrice"))
        if kind is None or strike <= 0 or (oi is not None and oi < 0) or (settle is not None and settle < 0):
            raise ParseError("format")
        item = TxoRow(month, strike, kind, oi, settle, day)
        key = (month, strike, kind)
        if key in out and out[key] != item:
            raise ParseError("conflict")
        out[key] = item
    if not out:
        raise ParseError("empty")
    return [out[key] for key in sorted(out)]


def parse_taifex_fx(rows: Any) -> FxRow:
    """`/v1/DailyForeignExchangeRates` → 最新一天的 USD/NTD，附同一份資料裡的前一筆（日變化用）。"""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ParseError("format")
    if not rows:
        raise ParseError("empty")
    series: dict[date, float] = {}
    conflicts: set[date] = set()
    seen_key = False
    for row in rows:
        day = _ymd(row.get("Date"))
        if "USD/NTD" not in row:
            continue
        seen_key = True
        value = _dash_or_number(row.get("USD/NTD"))
        if value is None:
            continue
        if value <= 0:
            raise ParseError("format")
        _keep_unique(series, conflicts, day, value)
    if conflicts:
        raise ParseError("conflict")
    if not seen_key:
        raise ParseError("missing_field")
    if not series:
        raise ParseError("empty")
    days = sorted(series)
    prev = days[-2] if len(days) >= 2 else None
    return FxRow(days[-1], series[days[-1]], prev, series[prev] if prev is not None else None)


# ── FinMind（歷史與補洞） ─────────────────────────────────────────────────────────────
def parse_finmind_prices(payload: Any, code: str) -> list[TwBar]:
    """FinMind `TaiwanStockPrice` → 依日期由舊到新的原始（未還原）日線；只用 date、stock_id、close、Trading_Volume、
    Trading_money。

    msg 不是 success／status 不是 200 → FinMindStatusError（job 停這輪補洞）；data 空 → empty；stock_id 不是 `code` → format；
    同一天重複且值不同 → conflict（整檔拒收）、值一樣只留一筆；收盤 ≤0 的列不能用（跳過；全部都不能用 → format）。
    """
    if not isinstance(payload, dict):
        raise ParseError("format")
    status = payload.get("status")
    if payload.get("msg") != "success" or isinstance(status, bool) or status != 200:
        raise FinMindStatusError()
    data = payload.get("data")
    if not isinstance(data, list):
        raise ParseError("format")
    if not data:
        raise ParseError("empty")
    bars: dict[date, TwBar] = {}
    skipped = 0
    for row in data:
        if not isinstance(row, dict) or row.get("stock_id") != code:
            raise ParseError("format")
        day = _iso(row.get("date"))
        close = _json_number(row.get("close"))
        volume, money = _json_int(row.get("Trading_Volume")), _json_int(row.get("Trading_money"))
        if volume < 0 or money < 0:
            raise ParseError("format")
        if close <= 0:
            skipped += 1
            continue
        bar = TwBar(day, close, volume, money)
        if day in bars and bars[day] != bar:
            raise ParseError("conflict")
        bars[day] = bar
    if not bars:
        raise ParseError("format" if skipped else "empty")
    return [bars[day] for day in sorted(bars)]


def finmind_revised_dates(stored: Mapping[date, tuple[float, int]], fresh: Iterable[TwBar]) -> list[date]:
    """已存的 (收盤, 量) 和新抓的同一天不一樣的日期（來源修訂）：有任何一天，job 就在一個交易內整檔替換並重算。"""
    changed = {
        bar.date
        for bar in fresh
        if bar.date in stored and (stored[bar.date][0] != bar.close or stored[bar.date][1] != bar.volume)
    }
    return sorted(changed)


# ── MoneyDJ 0050 持股（共用 A 的解析；B 只呼叫、不重寫） ───────────────────────────────────
def _us_sources() -> Any:
    import market_sources_us  # noqa: PLC0415 - 真的要用時才載入（import 本模組不連帶載入美股來源）

    return market_sources_us


def parse_moneydj_0050(page: Any, *, min_rows: Optional[int] = None) -> Any:
    """MoneyDJ `Basic0007B.xdjhtm?etfid=0050.TW`：連結代號「NNNN.TW」的列是股票，期貨（`.TF`）、現金列記 dropped；
    回 A 的 `HoldingList`（代號去掉「.TW」；0050 預設至少 40 檔，`min_rows` 給測試用的小頁）。"""
    return _check_0050_weights(_us_sources().parse_moneydj_holdings(page, "0050", min_rows=min_rows))


def _check_0050_weights(holdings: Any) -> Any:
    """0050 的權重都是百分比：單檔超過 100 或股票合計超過 100.5 代表欄位錯位（共用解析不檢查範圍，這裡補）。"""
    weights = [getattr(row, "weight", None) for row in holdings.rows]
    if any(isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or not 0 <= w <= 100 for w in weights):
        raise ParseError("format")
    if sum(weights) > 100.5:
        raise ParseError("format")
    return holdings


# ── 抓取（組網址 → http.fetch → 解碼 → 解析；FetchError 原樣往上丟） ─────────────────────────────
def _ymd_text(day: date) -> str:
    return day.strftime("%Y%m%d")


def fetch_twse_allbut0999(
    day: date, *, http: Any = None, ledger: Any = None, extra_codes: Sequence[str] = ("0050",)
) -> TwDaily:
    query = urlencode({"date": _ymd_text(day), "type": "ALLBUT0999", "response": "json"})
    body = _get(http, ledger, "twse_allbut0999", f"{TWSE_WWW}/rwd/zh/afterTrading/MI_INDEX?{query}")
    return parse_twse_allbut0999(_json_body(body), day, extra_codes=extra_codes)


def fetch_twse_bfi82u(day: date, *, http: Any = None, ledger: Any = None) -> Inst3:
    query = urlencode({"type": "day", "dayDate": _ymd_text(day), "response": "json"})
    body = _get(http, ledger, "twse_bfi82u", f"{TWSE_WWW}/rwd/zh/fund/BFI82U?{query}")
    return parse_twse_bfi82u(_json_body(body), expect_date=day)


def fetch_twse_margin_ms(day: date, *, http: Any = None, ledger: Any = None) -> MarginTw:
    query = urlencode({"date": _ymd_text(day), "selectType": "MS", "response": "json"})
    body = _get(http, ledger, "twse_margin_ms", f"{TWSE_WWW}/rwd/zh/marginTrading/MI_MARGN?{query}")
    return parse_twse_margin_ms(_json_body(body), expect_date=day)


def fetch_twse_twt49u(start: date, end: date, *, http: Any = None, ledger: Any = None) -> list[ExRight]:
    if start > end:
        raise ValueError("start>end")
    query = urlencode({"startDate": _ymd_text(start), "endDate": _ymd_text(end), "response": "json"})
    body = _get(http, ledger, "twse_twt49u", f"{TWSE_WWW}/rwd/zh/exRight/TWT49U?{query}")
    return parse_twse_twt49u(_json_body(body), expect_start=start, expect_end=end)


def fetch_twse_holidays(year: int, *, http: Any = None, ledger: Any = None) -> list[Holiday]:
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ValueError("year")
    query = urlencode({"date": f"{year}0101", "response": "json"})
    body = _get(http, ledger, "twse_holidays", f"{TWSE_WWW}/rwd/zh/holidaySchedule/holidaySchedule?{query}")
    return parse_twse_holidays(_json_body(body), year)


def fetch_openapi_bwibbu(*, http: Any = None, ledger: Any = None) -> tuple[date, dict[str, Optional[float]]]:
    """回 (資料日, {代號: 本益比})：資料日要從原始列取，所以抓取這裡一起回。"""
    rows = _json_body(_get(http, ledger, "openapi_bwibbu", f"{TWSE_OPENAPI}/exchangeReport/BWIBBU_ALL"))
    return bwibbu_date(rows), parse_openapi_bwibbu(rows)


def fetch_openapi_revenue(*, http: Any = None, ledger: Any = None) -> dict[str, Revenue]:
    rows = _json_body(_get(http, ledger, "openapi_revenue", f"{TWSE_OPENAPI}/opendata/t187ap05_L"))
    return parse_openapi_revenue(rows)


# 2026-10-23：外資台指期、外資選擇權、匯率的 CSV 表頭還沒抓到真樣本，不猜對應：CDN 回 CSV 時 format（detail csv），
# 只收 JSON；抓到真樣本後再照 DailyMarketReportOpt 的做法加表頭
def fetch_taifex_futures_inst(*, http: Any = None, ledger: Any = None) -> FuturesInst:
    url = f"{TAIFEX_OPENAPI}/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    return parse_taifex_futures_inst(_taifex_body(_get(http, ledger, "taifex_futures_inst", url)))


def fetch_taifex_calls_puts_inst(*, http: Any = None, ledger: Any = None) -> OptionsInst:
    url = f"{TAIFEX_OPENAPI}/MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate"
    return parse_taifex_calls_puts_inst(_taifex_body(_get(http, ledger, "taifex_calls_puts_inst", url)))


def fetch_taifex_pcr(*, http: Any = None, ledger: Any = None) -> list[PcrRow]:
    # 這個端點的 Content-Type 是 application/octet-stream（內容是 UTF-8 BOM 的 CSV）
    return parse_taifex_pcr_csv(_get(http, ledger, "taifex_pcr", f"{TAIFEX_OPENAPI}/PutCallRatio"))


def fetch_taifex_daily_options(contract: str = "TXO", *, http: Any = None, ledger: Any = None) -> list[TxoRow]:
    body = _get(http, ledger, "taifex_daily_options", f"{TAIFEX_OPENAPI}/DailyMarketReportOpt")
    return parse_taifex_daily_options(_taifex_body(body, _DAILY_OPT_CSV), contract)  # JSON 或 CSV（2026-10-23）


def fetch_taifex_fx(*, http: Any = None, ledger: Any = None) -> FxRow:
    body = _get(http, ledger, "taifex_fx", f"{TAIFEX_OPENAPI}/DailyForeignExchangeRates")
    return parse_taifex_fx(_taifex_body(body))


def fetch_finmind_prices(code: str, start: date, end: date, *, http: Any = None, ledger: Any = None) -> list[TwBar]:
    """代號來自第三方清單：送出前再驗一次格式（不合就 ValueError、不打請求），網址參數再 quote。"""
    if not isinstance(code, str) or not _FINMIND_ID_RE.fullmatch(code):
        raise ValueError("code")
    if start > end:
        raise ValueError("start>end")
    query = urlencode(
        {
            "dataset": "TaiwanStockPrice",
            "data_id": code,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        quote_via=quote,
        safe="",
    )
    timeout = FINMIND_BOOTSTRAP_TIMEOUT_S if (end - start).days > 31 else None
    body = _get(http, ledger, "finmind_prices", f"{FINMIND_DATA}?{query}", timeout=timeout)
    return parse_finmind_prices(_json_body(body), code)


def fetch_moneydj_0050(*, http: Any = None, ledger: Any = None) -> Any:
    """0050 持股：網址、大小上限、解碼都照 A 的 MoneyDJ 端點（美股三檔也走同一個），回 A 的 `HoldingList`。"""
    return _check_0050_weights(_us_sources().fetch_moneydj_holdings("0050", http=http, ledger=ledger))
