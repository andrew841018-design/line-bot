"""市場盤勢（/盤勢，2026-10-17）的雜項來源：宏觀（Yahoo spark 與代理）、新聞、川普原文、期中選舉預測市場。

Andrew 2026-10-17 的要求（DECISIONS.md）：宏觀「有 yahoo 用 yahoo，沒有的情況改代理」；川普／伊朗事件
「你再試試看，truth social or....，我覺得可以做到」——Truth Social 官方對程式回 403，改用第三方存檔
trumpstruth.org 的 RSS；期中選舉看預測市場（Polymarket、Kalshi），民調沒有穩定來源。卡片只描述狀態。

每個來源兩個函式：純解析（給 bytes／str／已解析的 JSON，壞資料丟 `ParseError(kind)`）與抓取（組固定網址、
呼叫 `market_http.fetch`、再交給解析）。規則（PLAN §3.1、§3.3、§18.8、§18.14、§19.1、§19.2）：
- 宏觀：Yahoo spark 每天 1 次（11 個代號、range=1mo）；日期用交易所時區換算，晚於資料日 D 的列丟掉
  （期貨、匯率幾乎 24 小時交易，最後一根可能是下一個交易日還沒收完）。Yahoo 失敗改代理：ETF 走 Nasdaq
  日線（BTC 用現貨比特幣 ETF 的 IBIT，Coinbase 在台灣被封鎖，2026-10-23 拿掉）、台幣走期交所匯率
  （market_sources_tw）；MOVE 沒有代理。
- 新聞：Google News RSS（zh-TW）；pubDate 超過 36 小時或解析不到不列；標題去掉「 - 來源」、來源另存。
- 川普原文：只認 namespace URI、不認前綴；貼文 ID／網址取 item 的直接子元素 `truth:originalId`、
  `truth:originalUrl`（`archive:reference` 裡的是被引用的那則）；本人文字＝description 去掉 blockquote、
  網址、「View image／View video」與引用的固定字樣；移除的、只剩媒體或轉貼的不列；主題用片語表比對
  完整的本人文字（單獨的 deal／market／stock／rate／war 不算）。job 依 originalId 累積 48 小時。
- 預測市場：只看固定的市場 id／ticker，並核對它還是「民主黨」那一邊；佔位市場（沒結束卻沒有
  outcomePrices、Party A/B）不收；買賣價中間值與最後成交價分開給；Kalshi 沒有成交時間。
  （2026-10-23 起台灣封鎖這兩個網站，job 不抓：見 PREDICTION_MARKETS_BLOCKED；解析函式留著。）
- 外部字串（新聞標題、來源名、川普摘錄、預測市場名稱）出這個模組前先清洗，清洗不過整則丟掉。
- XML 只用標準庫 ElementTree；看到 `<!DOCTYPE`／`<!ENTITY` 直接拒收。

import 時不做任何 I/O；`market_http`、`market_sources_us` 都到真的要抓時才載入（測試整個換成假的）。
不 import main、stock_picks、fcn_eval、card_pages。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import market_calc as _calc  # 共用的 ParseError 與 clean_external（2026-10-18 拿掉「還沒有 market_calc」時的本檔版本）

_UTC = timezone.utc

# ── 錯誤 ─────────────────────────────────────────────────────────────────────
PARSE_ERROR_KINDS = frozenset({"format", "missing_field", "empty", "too_few", "date", "conflict"})


# 三個 sources 模組共用 market_calc 的那一個（job 才能一次接住）
ParseError = _calc.ParseError

# ── 長度、時間窗 ───────────────────────────────────────────────────────────────
NEWS_TITLE_MAX = 40  # PLAN §10：標題 40、名稱 24、川普摘錄 100
NAME_MAX = 24
TRUMP_EXCERPT_MAX = 100
NEWS_MAX_AGE_H = 36.0
TRUMP_WINDOW_H = 48.0
# 本機時鐘慢一點時剛發的不該被丟掉；比這更「未來」的日期本身就是錯的
FUTURE_SKEW = timedelta(minutes=5)


# ── 端點與預算（PLAN §9：每個 endpoint 的次數上限寫成常數表並測試） ──────────────
@dataclass(frozen=True)
class Endpoint:
    host: str
    accept: tuple[str, ...]  # market_http 的 Content-Type 子字串
    max_bytes: int
    every_s: int  # 正常排程間隔
    per_run: int  # 每次排程打幾個請求
    retry_per_day: int  # 失敗後另外重試的上限（0＝等下一個排程）

    @property
    def per_day(self) -> int:
        return self.per_run * (86_400 // self.every_s)


ENDPOINTS: dict[str, Endpoint] = {
    # Andrew：Yahoo 每天 1 次，失敗就改代理、不重打
    "yahoo_spark": Endpoint("query1.finance.yahoo.com", ("json",), 1 << 20, 86_400, 1, 0),
    # PLAN §19.2：美股、台股 30 分鐘，伊朗一小時
    "gnews.us": Endpoint("news.google.com", ("xml",), 2 << 20, 1_800, 1, 0),
    "gnews.tw": Endpoint("news.google.com", ("xml",), 2 << 20, 1_800, 1, 0),
    "gnews.iran": Endpoint("news.google.com", ("xml",), 2 << 20, 3_600, 1, 0),
    "trumpstruth": Endpoint("www.trumpstruth.org", ("xml",), 2 << 20, 1_800, 1, 0),
    # 預測市場每 2 小時（PLAN §3.3）。2026-10-23 起封鎖中（PREDICTION_MARKETS_BLOCKED）：這四個不會被呼叫，
    # 主機也不在 market_http.ALLOWED_HOSTS 裡；表留著，解除封鎖時預算、網址檢查不用重寫
    "polymarket.house": Endpoint("gamma-api.polymarket.com", ("json",), 1 << 20, 7_200, 1, 0),
    "kalshi.house": Endpoint("api.elections.kalshi.com", ("json",), 256 << 10, 7_200, 1, 0),
    "polymarket.senate": Endpoint("gamma-api.polymarket.com", ("json",), 1 << 20, 7_200, 1, 0),
    "kalshi.senate": Endpoint("api.elections.kalshi.com", ("json",), 256 << 10, 7_200, 1, 0),
}

YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark?symbols={symbols}&range=1mo&interval=1d"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
# 不帶 www 的網址會 301 到這裡，market_http 不跟轉址
TRUMPSTRUTH_URL = "https://www.trumpstruth.org/feed"
POLYMARKET_EVENT_URL = "https://gamma-api.polymarket.com/events?slug={slug}"
KALSHI_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker={event}"

# ── 宏觀代號 ─────────────────────────────────────────────────────────────────
YAHOO_MACRO: dict[str, str] = {
    "DXY": "DX-Y.NYB",
    "WTI": "CL=F",
    "BRENT": "BZ=F",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "BTC": "BTC-USD",
    "USDTWD": "TWD=X",
    "RUT": "^RUT",
    "DJI": "^DJI",
    "IXIC": "^IXIC",
    "MOVE": "^MOVE",
}
# 台幣改期交所匯率（market_sources_tw）、MOVE 只有 Yahoo；金銀用 GLD／SLV（LBMA 403）；BTC 用現貨比特幣 ETF 的
# IBIT（2026-10-23：Coinbase 在台灣被封鎖，Yahoo 的 spark 又常回 429；卡上只寫 IBIT 的單日漲跌幅，不把 ETF 股價當 BTC 價格）
PROXY_ETF: dict[str, str] = {
    "DXY": "UUP",
    "WTI": "USO",
    "BRENT": "BNO",
    "GOLD": "GLD",
    "SILVER": "SLV",
    "BTC": "IBIT",
    "RUT": "IWM",
    "DJI": "DIA",
    "IXIC": "ONEQ",
}
_PROXY_SYMBOLS = frozenset(PROXY_ETF.values())
SOURCE_YAHOO = "Yahoo"

NEWS_QUERIES: dict[str, str] = {"us": "美股", "tw": "台股", "iran": "伊朗"}


def proxy_source_label(etf: str) -> str:
    """卡上「來源」那格的固定字（PLAN §3.1：每格標實際來源，例「ETF 代理：UUP」）。"""
    if etf not in _PROXY_SYMBOLS:
        raise ValueError("not a proxy ETF")
    return f"ETF 代理：{etf}"


# ── 資料結構 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NewsItem:
    guid: str
    title: str  # 已清洗、去掉「 - 來源」
    source: str
    published_utc: datetime


@dataclass(frozen=True)
class TrumpPost:
    original_id: str
    url: Optional[str]  # truth:originalUrl；格式不對或和 ID 對不上就是 None
    published_utc: datetime
    own_text: Optional[str]  # 本人文字的清洗後摘錄（≤100 字）；清洗不過是 None，主題照算
    quoted_author: Optional[str]
    post_type: str
    removed: bool
    topics: tuple[str, ...] = ()  # 用完整的本人文字比對（摘錄只有前 100 字，會漏）


@dataclass(frozen=True)
class FeedSpan:
    """整份 trumpstruth feed（過濾前）的時間範圍；界線和 `_within` 一樣（2026-10-23 審查 r7b F1）。"""

    oldest: datetime  # 視窗內（now − 48 小時 ～ now + FUTURE_SKEW）最舊的一則；視窗內一則都沒有時＝newest
    newest: datetime  # 沒有晚於 now + FUTURE_SKEW 的最新一則（任何年紀）
    reaches_back: bool  # feed 裡還有比視窗起點更舊的 item：這一份 feed 自己就涵蓋了整個 48 小時


@dataclass(frozen=True)
class PredictionQuote:
    platform: str  # "polymarket" | "kalshi"
    market_id: str
    outcome: str  # 市場自己標的那一邊（已清洗），例 "Democratic Party"
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    updated_utc: Optional[datetime]  # Polymarket 的市場更新時間；Kalshi 沒有成交時間 → None
    active: bool
    closed: bool
    result: Optional[str]  # "Yes" | "No" | None（還沒判定）

    @property
    def mid(self) -> Optional[float]:
        """買賣價中間值；有一邊沒掛單（0 或 1）或買價高於賣價時沒有中間值。"""
        if self.bid is None or self.ask is None or not 0.0 < self.bid <= self.ask < 1.0:
            return None
        return round((self.bid + self.ask) / 2.0, 6)


@dataclass(frozen=True)
class PredictionMarket:
    key: str  # 也是 ENDPOINTS 的鍵
    platform: str
    event: str  # Polymarket event slug／Kalshi event ticker（組網址用）
    market_id: str  # Polymarket market id／Kalshi market ticker
    expect_outcome: str


# 2026-10-18 實測固定（PLAN §3.3「參院實作時固定」）：Polymarket 參院 event 32224 的民主黨市場 562793
# （同一個 event 裡的 Party A–F、Other 是沒開的佔位市場）；Kalshi 參院是 CONTROLS-2026-D（yes_sub_title
# 「Democratic Party」）。Yes 都是「民主黨拿下」。
PREDICTION_MARKETS: tuple[PredictionMarket, ...] = (
    PredictionMarket("polymarket.house", "polymarket", "which-party-will-win-the-house-in-2026", "562802", "Democratic Party"),
    PredictionMarket("kalshi.house", "kalshi", "CONTROLH-2026", "CONTROLH-2026-D", "Democratic Party"),
    PredictionMarket("polymarket.senate", "polymarket", "which-party-will-win-the-senate-in-2026", "562793", "Democratic Party"),
    PredictionMarket("kalshi.senate", "kalshi", "CONTROLS-2026", "CONTROLS-2026-D", "Democratic Party"),
)

# 2026-10-23：台灣的 ISP 從 2026-10-21 晚上起，依法院命令（公職人員選舉罷免法的刑事裁定）封鎖 Polymarket 和
# Kalshi（還有 Coinbase）：DNS 把主機導到封鎖頁，之後每次抓取都在 TLS 驗證失敗（kind=network），election.* 任務
# 一直失敗、卡上只剩「暫缺」。不能繞過封鎖（不改 DNS、不寫死 IP、不走 proxy／VPN），所以這兩個來源整個停掉：
# election.* 任務永遠不到期（market_pipeline_misc）、兩個主機不在 market_http.ALLOWED_HOSTS、卡上固定寫明被封鎖
# （publish 在選舉列寫 blocked，market_dashboard 顯示固定句）。解析函式、PREDICTION_MARKETS、ENDPOINTS 都留著。
# TODO(Andrew)：期中選舉這一段要整段拿掉，還是換成合法的來源（例如民調）？決定前維持現狀。
# 改回 False 之前要先把兩個主機加回 market_http.ALLOWED_HOSTS（只改這個值，任務會以 kind=host 失敗）。
PREDICTION_MARKETS_BLOCKED = True

# ── 外部字串清洗（PLAN §19.1） ──────────────────────────────────────────────────
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# 換行類先變空白再刪其他控制字元：直接刪會把前後兩個英文字黏成一個
_WS_CONTROL_RE = re.compile("[\t\n\r\x0b\x0c\x1c-\x1f\x85\u2028\u2029]")
def _plain(text: str) -> str:
    """NFKC、換行類變空白、刪控制／格式／代理／私用／未指派字元、空白收成一個。"""
    s = unicodedata.normalize("NFKC", text)
    s = _WS_CONTROL_RE.sub(" ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _DROP_CATEGORIES)
    return " ".join(s.split())


_clean: Callable[[object, int], Optional[str]] = _calc.clean_external

# ── 共用小工具 ───────────────────────────────────────────────────────────────


def _market_http(http: Any) -> Any:
    if http is not None:
        return http
    import market_http  # noqa: PLC0415 — 真的要抓時才載入，import 本模組不做 I/O

    return market_http


def _get(name: str, url: str, http: Any, ledger: Any) -> bytes:
    endpoint = ENDPOINTS[name]
    if (urlsplit(url).hostname or "") != endpoint.host:
        raise ValueError("url host does not match its endpoint")
    response = _market_http(http).fetch(url, max_bytes=endpoint.max_bytes, accept=endpoint.accept, ledger=ledger)
    return response.body


def _as_json(payload: Any) -> Any:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            payload = bytes(payload).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseError("format") from None
    if isinstance(payload, str):
        if not payload.strip():
            raise ParseError("empty")
        try:
            return json.loads(payload)
        except (ValueError, RecursionError):
            raise ParseError("format") from None
    return payload


_DTD_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _xml_root(data: Any) -> ET.Element:
    if isinstance(data, (bytes, bytearray, memoryview)):
        # 嚴格用 UTF-8 解：其他編碼（例 UTF-16）的 <!DOCTYPE 在位元組裡比對不到
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseError("format") from None
    elif isinstance(data, str):
        text = data.lstrip("\ufeff")
    else:
        raise ParseError("format")
    if not text.strip():
        raise ParseError("empty")
    # 不依賴 expat 的版本與預設處理 DTD、實體展開：有宣告就整份拒收
    if _DTD_RE.search(text):
        raise ParseError("format")
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError):
        raise ParseError("format") from None


def _rss_items(data: Any) -> list[ET.Element]:
    root = _xml_root(data)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ParseError("format")
    items = channel.findall("item")
    if not items:
        raise ParseError("empty")
    return items


def _as_utc(now: Any) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(_UTC)
    if isinstance(now, (int, float)) and not isinstance(now, bool) and math.isfinite(now):
        return datetime.fromtimestamp(now, _UTC)
    raise TypeError("now must be an aware datetime or epoch seconds")


def _check_through(through: Any) -> Optional[date]:
    if through is None:
        return None
    if not isinstance(through, date) or isinstance(through, datetime):
        raise TypeError("through must be a date")
    return through


def _rfc822_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)  # -0000 或沒寫時區：RSS 的慣例是 GMT
    return parsed.astimezone(_UTC)


def _iso_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(_UTC)


def _within(published: datetime, now_utc: datetime, hours: float) -> bool:
    return now_utc - timedelta(hours=hours) <= published <= now_utc + FUTURE_SKEW


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)?|\.\d+")


def _decimal(value: Any) -> Optional[float]:
    """JSON 數字或「0.9020」這種字串 → float；其他（含 bool）回 None。"""
    if isinstance(value, str):
        text = value.strip()
        return float(text) if _DECIMAL_RE.fullmatch(text) else None
    return _finite(value)


# ── Yahoo spark ──────────────────────────────────────────────────────────────
_YAHOO_SYMBOL_RE = re.compile(r"[A-Z0-9^=.\-]{1,20}")
_TZ_NAME_RE = re.compile(r"[A-Za-z_]{1,20}(?:/[A-Za-z0-9_+\-]{1,30}){0,2}")


def _exchange_tz(meta: Any) -> Optional[ZoneInfo]:
    name = meta.get("exchangeTimezoneName") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not _TZ_NAME_RE.fullmatch(name):
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _spark_series(entry: dict, symbol: str, through: Optional[date]) -> list[tuple[date, float]]:
    responses = entry.get("response")
    if not isinstance(responses, list) or not responses or not isinstance(responses[0], dict):
        return []
    body = responses[0]
    meta = body.get("meta")
    if isinstance(meta, dict) and meta.get("symbol") not in (None, symbol):
        return []
    # 沒有交易所時區就不猜：用 UTC 會把倫敦午夜（UTC 前一天 23:00）的匯率算成前一天
    tz = _exchange_tz(meta)
    if tz is None:
        return []
    stamps = body.get("timestamp")
    try:
        closes = body["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return []
    if not isinstance(stamps, list) or not isinstance(closes, list) or len(stamps) != len(closes):
        return []
    by_day: dict[date, float] = {}
    for stamp, close in zip(stamps, closes):
        ts, value = _finite(stamp), _finite(close)
        if ts is None or value is None or value <= 0:
            continue
        try:
            day = datetime.fromtimestamp(ts, tz).date()
        except (OverflowError, OSError, ValueError):
            continue
        if through is not None and day > through:
            continue
        # 同一天有兩根時（盤中那根接在當天開盤那根後面）取後面那根
        by_day[day] = value
    return sorted(by_day.items())


def parse_yahoo_spark(payload: Any, *, through: Optional[date] = None) -> dict[str, list[tuple[date, float]]]:
    """Yahoo v7 spark → {代號: [(交易所當地日期, 收盤)]，舊到新}。`through`＝資料日 D，晚於它的列丟掉。

    單一代號壞掉（沒有時區、長度對不上、重複出現）只把那個代號拿掉，job 對它改走代理；
    一個能用的都沒有才丟 ParseError("empty")。
    """
    through = _check_through(through)
    data = _as_json(payload)
    spark = data.get("spark") if isinstance(data, dict) else None
    if not isinstance(spark, dict):
        raise ParseError("format")
    results = spark.get("result")
    if results is None:
        raise ParseError("empty")
    if not isinstance(results, list):
        raise ParseError("format")
    out: dict[str, list[tuple[date, float]]] = {}
    seen: set[str] = set()
    duplicated: set[str] = set()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        symbol = entry.get("symbol")
        if not isinstance(symbol, str) or not _YAHOO_SYMBOL_RE.fullmatch(symbol):
            continue
        if symbol in seen:
            duplicated.add(symbol)
            continue
        seen.add(symbol)
        series = _spark_series(entry, symbol, through)
        if series:
            out[symbol] = series
    for symbol in duplicated:  # 同一代號出現兩次就不知道哪個對，兩個都不用
        out.pop(symbol, None)
    if not out:
        raise ParseError("empty")
    return out


def fetch_yahoo_spark(
    symbols: Optional[Sequence[str]] = None,
    *,
    through: Optional[date] = None,
    http: Any = None,
    ledger: Any = None,
) -> dict[str, list[tuple[date, float]]]:
    """抓固定的宏觀代號（預設 11 個全部、一個請求）。代號只收 YAHOO_MACRO 裡的。"""
    chosen = tuple(YAHOO_MACRO.values()) if symbols is None else tuple(symbols)
    allowed = frozenset(YAHOO_MACRO.values())
    if not chosen or any(symbol not in allowed for symbol in chosen):
        raise ValueError("symbol not in the fixed Yahoo list")
    url = YAHOO_SPARK_URL.format(symbols=",".join(quote(symbol, safe="") for symbol in chosen))
    return parse_yahoo_spark(_get("yahoo_spark", url, http, ledger), through=through)


# ── 代理：Nasdaq ETF 日線 ────────────────────────────────────────────────────────
def _closes(pairs: list[tuple[date, float]], through: Optional[date]) -> list[tuple[date, float]]:
    by_day: dict[date, float] = {}
    for day, close in pairs:
        if through is not None and day > through:
            continue
        if day in by_day and by_day[day] != close:
            raise ParseError("conflict")
        by_day[day] = close
    if not by_day:
        raise ParseError("empty")
    return sorted(by_day.items())


def _us_historical() -> Callable[..., Any]:
    """A 的 `market_sources_us.fetch_nasdaq_historical`（代理只有 Yahoo 失敗那天才用，所以到那時才 import）。"""
    import market_sources_us  # noqa: PLC0415

    return market_sources_us.fetch_nasdaq_historical


def fetch_proxy_bars(
    symbol: str,
    start: date,
    end: date,
    *,
    http: Any = None,
    ledger: Any = None,
    historical: Optional[Callable[..., Any]] = None,
) -> list[tuple[date, float]]:
    """代理 ETF 的日線收盤 [(日期, 收盤)]，舊到新；`end`＝資料日 D，晚於它的列丟掉（PLAN §18.8）。

    走 A 的 `market_sources_us.fetch_nasdaq_historical`（assetclass=etf；測試可注入 `historical`），
    回的格式和 Yahoo spark 的序列一樣。2026-10-18 拿掉本檔原本的精簡解析（A 進來之後就用不到）。
    """
    if symbol not in _PROXY_SYMBOLS:
        raise ValueError("not a proxy ETF")
    for value in (start, end):
        if not isinstance(value, date) or isinstance(value, datetime):
            raise TypeError("start and end must be dates")
    if start > end:
        raise ValueError("start is after end")
    fetcher = historical if historical is not None else _us_historical()
    kwargs: dict[str, Any] = {"etf": True, "ledger": ledger}
    if http is not None:  # 沒給就讓 A 用它自己的預設（market_http）
        kwargs["http"] = http
    pairs: list[tuple[date, float]] = []
    for bar in fetcher(symbol, start, end, **kwargs):
        day, close = getattr(bar, "date", None), _finite(getattr(bar, "close", None))
        if isinstance(day, date) and not isinstance(day, datetime) and close is not None and close > 0:
            pairs.append((day, close))
    return _closes(pairs, end)


# ── Google News RSS ──────────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,2048}")


def _token(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _TOKEN_RE.fullmatch(value) else None


def _split_news_title(raw_title: str, raw_source: Optional[str]) -> tuple[str, Optional[str]]:
    title = " ".join(raw_title.split())
    source = " ".join(raw_source.split()) if raw_source else None
    head, sep, tail = title.rpartition(" - ")
    if not sep or not head:
        return title, source or None
    if not source:
        return head, tail  # 沒有 <source> 時照 Google News 的慣例：最後一段就是來源
    # 只在最後一段真的是來源名時才去掉（標題中間也可能有「- 國際」）
    if _plain(tail).casefold() == _plain(source).casefold():
        return head, source
    return title, source


def parse_google_news_rss(xml: Any, now: Any, *, max_age_hours: float = NEWS_MAX_AGE_H) -> list[NewsItem]:
    """Google News RSS → 最近 `max_age_hours` 小時內的新聞（新到舊、依 guid 去重）。

    pubDate 解析不到的不列；全部都壞才丟 ParseError("format")；標題或來源清洗不過、沒有來源的整則不列。
    """
    now_utc = _as_utc(now)
    items = _rss_items(xml)
    candidates: list[NewsItem] = []
    bad = 0
    for item in items:
        published = _rfc822_utc(item.findtext("pubDate"))
        guid = _token(item.findtext("guid")) or _token(item.findtext("link"))
        raw_title = item.findtext("title")
        if published is None or guid is None or not raw_title or not raw_title.strip():
            bad += 1
            continue
        if not _within(published, now_utc, max_age_hours):
            continue
        source_el = item.find("source")
        title, source = _split_news_title(raw_title, source_el.text if source_el is not None else None)
        clean_title = _clean(title, NEWS_TITLE_MAX)
        clean_source = _clean(source, NAME_MAX) if source else None
        if clean_title is None or clean_source is None:
            continue
        candidates.append(NewsItem(guid, clean_title, clean_source, published))
    if bad == len(items):
        raise ParseError("format")
    candidates.sort(key=lambda news: news.published_utc, reverse=True)
    out: list[NewsItem] = []
    seen: set[object] = set()
    for news in candidates:
        same_story = (news.title, news.source)
        if news.guid in seen or same_story in seen:
            continue
        seen.update((news.guid, same_story))
        out.append(news)
    return out


def fetch_google_news(topic: str, now: Any, *, http: Any = None, ledger: Any = None) -> list[NewsItem]:
    """topic：us（美股）、tw（台股）、iran（伊朗）。"""
    query = NEWS_QUERIES.get(topic)
    if query is None:
        raise ValueError("unknown news topic")
    url = GOOGLE_NEWS_URL.format(query=quote(query, safe=""))
    return parse_google_news_rss(_get(f"gnews.{topic}", url, http, ledger), now)


# ── 川普原文（trumpstruth.org RSS） ─────────────────────────────────────────────
_NS_TRUTH = "https://truthsocial.com/ns"
_NS_ARCHIVE = "https://www.trumpstruth.org/rss-extensions"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_TAG_ORIGINAL_ID = f"{{{_NS_TRUTH}}}originalId"
_TAG_ORIGINAL_URL = f"{{{_NS_TRUTH}}}originalUrl"
_TAG_POST_TYPE = f"{{{_NS_ARCHIVE}}}postType"
_TAG_REMOVED = f"{{{_NS_ARCHIVE}}}removed"
_TAG_REFERENCE = f"{{{_NS_ARCHIVE}}}reference"
_TAG_CREATOR = f"{{{_NS_DC}}}creator"

_ORIGINAL_ID_RE = re.compile(r"\d{1,25}")
_POST_URL_RE = re.compile(r"https://truthsocial\.com/@[A-Za-z0-9_]{1,30}/(\d{1,25})")
_POST_TYPE_RE = re.compile(r"[a-z_]{1,20}")
_REPOST_TYPES = frozenset({"retruth", "reblog", "repost"})
_MEDIA_LINK_RE = re.compile(r"view\s+(?:image|video)s?", re.IGNORECASE)
_URLISH_RE = re.compile(r"(?:https?://|www\.)\S*|\S+\.\S+/\S*", re.IGNORECASE)
_TEXT_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# 引用貼文時 Truth Social 在內文加「RT: <被引用的網址>」；網址拿掉後剩下的 RT: 也不是本人說的
_RT_MARKER_RE = re.compile(r"(?<!\S)RT:(?!\S)")
_RETRUTH_RE = re.compile(r"RT\s+@")
_QUOTED_INTRO_RE = re.compile(r"Quoted\s+(.+)")


def _child_text(element: ET.Element, tag: str) -> Optional[str]:
    child = element.find(tag)  # 只看直接子元素：archive:reference 裡同名的欄位是被引用的那則
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _bool_text(value: Optional[str]) -> Optional[bool]:
    lowered = (value or "").lower()
    return True if lowered == "true" else False if lowered == "false" else None


class _DescriptionText(HTMLParser):
    """description 的 HTML → 本人文字的段落；blockquote 裡的是被引用的貼文，不收。"""

    _BREAKS = frozenset({"p", "div", "br", "li", "ul", "ol", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "hr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[list[tuple[str, bool]]] = [[]]
        self._quote_depth = 0
        self._anchor: Optional[list[str]] = None

    def _break(self) -> None:
        if self.segments[-1]:
            self.segments.append([])

    def _close_anchor(self) -> None:
        if self._anchor is None:
            return
        text = " ".join("".join(self._anchor).split())
        self._anchor = None
        # 「View image／View video」與網址形式的連結字不是本人說的話
        if text and not _MEDIA_LINK_RE.fullmatch(text) and not _URLISH_RE.fullmatch(text):
            self.segments[-1].extend(((" ", False), (text, True), (" ", False)))

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            self._close_anchor()
            if self._quote_depth == 0:
                self._anchor = []
        elif tag in self._BREAKS:
            self._close_anchor()  # 沒關的 <a> 不能吃掉後面的段落
            if tag == "blockquote":
                self._quote_depth += 1
            self._break()
        elif self._quote_depth == 0 and self._anchor is None:
            # <span> 這類行內標籤前後補空白：存檔的「RT: 網址</span>本人文字」中間沒有空白
            self.segments[-1].append((" ", False))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._close_anchor()
        elif tag in self._BREAKS:
            self._close_anchor()
            if tag == "blockquote" and self._quote_depth:
                self._quote_depth -= 1
            self._break()
        elif self._quote_depth == 0 and self._anchor is None:
            self.segments[-1].append((" ", False))

    def handle_data(self, data: str) -> None:
        if self._quote_depth:
            return
        if self._anchor is not None:
            self._anchor.append(data)
        else:
            self.segments[-1].append((data, False))

    def finish(self) -> list[list[tuple[str, bool]]]:
        self.close()
        self._close_anchor()
        return [segment for segment in self.segments if segment]


def _own_text(description: str, has_reference: bool) -> tuple[str, Optional[str]]:
    """回 (本人文字, 「Quoted <a>原作者</a>」裡的原作者)。"""
    parser = _DescriptionText()
    parser.feed(description)
    parts: list[str] = []
    quoted_intro: Optional[str] = None
    for segment in parser.finish():
        text = " ".join("".join(piece for piece, _ in segment).split())
        if not text:
            continue
        if has_reference and quoted_intro is None:
            # 存檔在被引用的貼文前面固定加一段「Quoted <a>原作者</a>」（在 blockquote 外面）
            anchors = " ".join(piece for piece, is_anchor in segment if is_anchor)
            match = _QUOTED_INTRO_RE.fullmatch(text)
            if match and anchors and match.group(1) == anchors:
                quoted_intro = anchors
                continue
        parts.append(text)
    own = _RT_MARKER_RE.sub(" ", _TEXT_URL_RE.sub(" ", " ".join(parts)))
    return " ".join(own.split()), quoted_intro


def _post_url(url: Optional[str], original_id: str) -> Optional[str]:
    match = _POST_URL_RE.fullmatch(url or "")
    return url if match and match.group(1) == original_id else None


def _post_type(value: Optional[str]) -> str:
    lowered = (value or "").lower()
    return lowered if _POST_TYPE_RE.fullmatch(lowered) else "unknown"


def _keep_newest(posts: dict[str, TrumpPost], post: TrumpPost) -> None:
    existing = posts.get(post.original_id)
    if existing is None or post.published_utc > existing.published_utc:
        posts[post.original_id] = post


def parse_trumpstruth_rss(
    xml: Any,
    now: Any,
    *,
    window_hours: float = TRUMP_WINDOW_H,
    include_removed: bool = False,
) -> list[TrumpPost]:
    """trumpstruth RSS → 近 `window_hours` 小時內有本人文字的貼文（新到舊、依 originalId 去重）。

    `archive:removed`＝true 的不列；`include_removed=True` 時改成回 removed=True 的空殼（只有 ID、時間），
    讓 job 能把之前累積到、後來被移除的那則刪掉。必要欄位（originalId、pubDate、removed、description）
    壞掉的算壞列，全部都壞才丟 ParseError("format")。
    """
    now_utc = _as_utc(now)
    items = _rss_items(xml)
    posts: dict[str, TrumpPost] = {}
    bad = 0
    for item in items:
        original_id = _child_text(item, _TAG_ORIGINAL_ID)
        published = _rfc822_utc(item.findtext("pubDate"))
        removed = _bool_text(_child_text(item, _TAG_REMOVED))
        if original_id is None or not _ORIGINAL_ID_RE.fullmatch(original_id) or published is None or removed is None:
            bad += 1
            continue
        if not _within(published, now_utc, window_hours):
            continue
        post_type = _post_type(_child_text(item, _TAG_POST_TYPE))
        url = _post_url(_child_text(item, _TAG_ORIGINAL_URL), original_id)
        if removed:
            if include_removed:
                _keep_newest(posts, TrumpPost(original_id, url, published, None, None, post_type, True))
            continue
        description = item.findtext("description")
        if description is None:
            bad += 1
            continue
        if post_type in _REPOST_TYPES:
            continue
        reference = item.find(_TAG_REFERENCE)
        try:
            own, quoted_intro = _own_text(description, reference is not None)
        except (ValueError, AssertionError):
            bad += 1
            continue
        # 只剩圖片／影片／轉貼（或只有標點）的不列
        if _RETRUTH_RE.match(own) or not any(ch.isalnum() for ch in own):
            continue
        full = _plain(own)
        quoted_author = None
        if reference is not None:
            quoted_author = _clean(_child_text(reference, _TAG_CREATOR) or quoted_intro or "", NAME_MAX)
        topics = tuple(classify_trump_topics(full))
        excerpt = _clean(full, TRUMP_EXCERPT_MAX)
        _keep_newest(posts, TrumpPost(original_id, url, published, excerpt, quoted_author, post_type, False, topics))
    if bad == len(items):
        raise ParseError("format")
    return sorted(posts.values(), key=lambda post: (post.published_utc, post.original_id), reverse=True)


def fetch_trumpstruth(
    now: Any,
    *,
    http: Any = None,
    ledger: Any = None,
    include_removed: bool = False,
) -> list[TrumpPost]:
    return parse_trumpstruth_rss(_get("trumpstruth", TRUMPSTRUTH_URL, http, ledger), now, include_removed=include_removed)


def trumpstruth_feed_span(xml: Any, now: Any) -> FeedSpan:
    """整份 feed（過濾前）的時間範圍：視窗內最舊一則、最新一則（pubDate，UTC），以及 feed 有沒有比視窗更舊的 item。

    PLAN §18.14 要比「這輪最舊一則」和「上輪成功時最新一則」判斷中間有沒有漏（卡上標「（至少）」）；
    parse_trumpstruth_rss 已經濾掉只有媒體、移除、超過 48 小時的，不能拿它的最舊一則來比，所以這裡看整份 feed。
    只算 originalId 與 pubDate 都有效的 item；一則都沒有就丟 ParseError("format")。

    時間界線和 parse_trumpstruth_rss 的 `_within` 一樣（2026-10-23 審查 r7b F1：f78d5de 改看整份 feed 時把界線也拿掉了）：
    - 晚於 now + FUTURE_SKEW 的是來源打錯時間，整則不算（不然 job 的 last_newest 被拉到未來、只增不減，
      之後真的缺口再也標不出來）；每一則都在未來就丟 ParseError("date")。
    - 比 now − 48 小時舊的不算進 oldest（置頂或被移除的空殼用原始時間，會讓最舊一則看起來比上輪最新還舊、
      蓋掉真的缺口），只記 reaches_back：feed 比視窗起點還舊，代表這一份自己就涵蓋了整個視窗（剛上線或睡了
      很久之後，不必標「（至少）」）。視窗內的只有圖的貼文照算，這是 f78d5de 要的。
    """
    now_utc = _as_utc(now)
    stamps: list[datetime] = []
    for item in _rss_items(xml):
        original_id = _child_text(item, _TAG_ORIGINAL_ID)
        published = _rfc822_utc(item.findtext("pubDate"))
        if original_id is not None and _ORIGINAL_ID_RE.fullmatch(original_id) and published is not None:
            stamps.append(published)
    if not stamps:
        raise ParseError("format")
    start = now_utc - timedelta(hours=TRUMP_WINDOW_H)
    usable = [stamp for stamp in stamps if stamp <= now_utc + FUTURE_SKEW]
    if not usable:
        raise ParseError("date")
    inside = [stamp for stamp in usable if _within(stamp, now_utc, TRUMP_WINDOW_H)]
    newest = max(usable)
    return FeedSpan(min(inside) if inside else newest, newest, any(stamp < start for stamp in usable))


def fetch_trumpstruth_with_span(
    now: Any,
    *,
    http: Any = None,
    ledger: Any = None,
    include_removed: bool = False,
) -> tuple[list[TrumpPost], FeedSpan]:
    """一次請求同時回貼文與整份 feed 的時間範圍（給 job 判斷缺口）。"""
    body = _get("trumpstruth", TRUMPSTRUTH_URL, http, ledger)
    return parse_trumpstruth_rss(body, now, include_removed=include_removed), trumpstruth_feed_span(body, now)


# ── 川普貼文主題（PLAN §3.3 片語表） ─────────────────────────────────────────────
# (代號, 卡上名稱, 不分大小寫的片語, 只認大寫開頭／全大寫的字)；單獨的 deal／market／stock／rate／war 不算
_TOPIC_RULES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("tariff", "關稅", (r"tariffs?",), ()),
    ("iran", "伊朗", (r"iran", r"iranians?", r"tehran"), ()),
    # 小寫的 fed 是一般英文（fed up、well fed），只認 Fed／FED，且 Fed up 不算
    ("fed", "聯準會", (r"federal\s+reserve", r"powell"), (r"(?:Fed|FED)(?![\s-]+[Uu][Pp](?![A-Za-z]))",)),
    ("rates", "利率", (r"interest\s+rates?",), ()),
    ("oil", "油價", (r"oil\s+prices?", r"prices?\s+of\s+oil"), ()),
    ("china", "中國", (r"china", r"chinese"), (r"Xi", r"XI")),
    ("israel", "以色列", (r"israel", r"israelis?"), ()),
    ("ceasefire", "停火", (r"cease[\s-]?fires?",), ()),
    ("taiwan", "台灣", (r"taiwan", r"taiwanese"), ()),
    # bargaining chip、blue chip 不是晶片
    ("chips", "半導體", (r"semiconductors?", r"(?<!bargaining\s)(?<!blue\s)(?<!blue-)chips?"), ()),
    ("stock_market", "股市", (r"stock\s+markets?",), ()),
    ("nato", "北約", (r"nato",), ()),
    ("russia_ukraine", "俄烏", (r"russia", r"russians?", r"ukraine", r"ukrainians?", r"putin", r"zelenskyy?"), ()),
)
TRUMP_TOPIC_LABELS: dict[str, str] = {topic: label for topic, label, _, _ in _TOPIC_RULES}


def _topic_regex(phrases: tuple[str, ...], flags: int) -> Optional[re.Pattern[str]]:
    if not phrases:
        return None
    return re.compile("|".join(rf"(?<![A-Za-z0-9])(?:{phrase})(?![A-Za-z0-9])" for phrase in phrases), flags)


_TOPIC_PATTERNS = tuple(
    (topic, _topic_regex(any_case, re.IGNORECASE), _topic_regex(capitalized, 0))
    for topic, _, any_case, capitalized in _TOPIC_RULES
)


def classify_trump_topics(text: Any) -> list[str]:
    """本人文字 → 命中的主題代號（固定順序、不重複）；中文名稱查 TRUMP_TOPIC_LABELS。"""
    if not isinstance(text, str):
        return []
    plain = _plain(text)
    found: list[str] = []
    for topic, any_case, capitalized in _TOPIC_PATTERNS:
        if (any_case is not None and any_case.search(plain)) or (capitalized is not None and capitalized.search(plain)):
            found.append(topic)
    return found


# ── 預測市場 ─────────────────────────────────────────────────────────────────
_PLACEHOLDER_TITLE_RE = re.compile(r"Party\s+[A-Z]", re.IGNORECASE)
_KALSHI_ACTIVE = frozenset({"active", "open"})
_KALSHI_CLOSED = frozenset({"closed", "determined", "disputed", "amended", "finalized", "settled"})
_KALSHI_RESULTS = {"": None, "yes": "Yes", "no": "No"}


def _price(value: Any) -> Optional[float]:
    """0～1 的價格；沒有值回 None，有值但不是 0～1 的數字就是格式錯。"""
    if value is None:
        return None
    number = _decimal(value)
    if number is None or not 0.0 <= number <= 1.0:
        raise ParseError("format")
    return number


def _json_list(value: Any) -> Optional[list]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    return value if isinstance(value, list) else None


def _outcome_label(value: Any, expect: Optional[str]) -> str:
    label = _clean(value if isinstance(value, str) else "", NEWS_TITLE_MAX)
    if label is None:
        raise ParseError("format")
    # 固定 id 要還是當初那一邊（例「民主黨」）；被改用途就不能照舊顯示
    if expect is not None and label.casefold() != expect.casefold():
        raise ParseError("conflict")
    return label


def parse_polymarket_event(payload: Any, market_id: str, *, expect_outcome: Optional[str] = None) -> PredictionQuote:
    """Polymarket `/events?slug=…`（event 的 list，或單一 event）→ 指定 market id 的 Yes 報價。"""
    data = _as_json(payload)
    events = [data] if isinstance(data, dict) else data
    if not isinstance(events, list):
        raise ParseError("format")
    if not events:
        raise ParseError("empty")
    target: Optional[dict] = None
    for event in events:
        markets = event.get("markets") if isinstance(event, dict) else None
        if not isinstance(markets, list):
            raise ParseError("format")
        for market in markets:
            if isinstance(market, dict) and str(market.get("id")) == str(market_id):
                if target is not None:
                    raise ParseError("conflict")
                target = market
    if target is None:
        raise ParseError("missing_field")
    active, closed = target.get("active"), target.get("closed")
    if not isinstance(active, bool) or not isinstance(closed, bool):
        raise ParseError("format")
    if _json_list(target.get("outcomes")) != ["Yes", "No"]:
        raise ParseError("format")  # bestBid／bestAsk 是第一個結果（Yes）的價格，順序變了就不能用
    raw_prices = _json_list(target.get("outcomePrices"))
    prices: Optional[list[float]] = None
    if raw_prices is not None and len(raw_prices) == 2:
        parsed = [_decimal(price) for price in raw_prices]
        if all(price is not None and 0.0 <= price <= 1.0 for price in parsed):
            prices = parsed  # type: ignore[assignment]
    title = target.get("groupItemTitle") or target.get("question")
    # 佔位市場（Party A/B…）沒有價格；投票後已結束的市場照收（結果要顯示 7 天）
    if not closed and (prices is None or _PLACEHOLDER_TITLE_RE.fullmatch(str(title or "").strip())):
        raise ParseError("empty")
    outcome = _outcome_label(title, expect_outcome)
    bid, ask, last = _price(target.get("bestBid")), _price(target.get("bestAsk")), _price(target.get("lastTradePrice"))
    result = None
    if closed and prices is not None:
        result = "Yes" if prices == [1.0, 0.0] else "No" if prices == [0.0, 1.0] else None
    if bid is None and ask is None and last is None and result is None:
        raise ParseError("missing_field")
    return PredictionQuote(
        "polymarket", str(market_id), outcome, bid, ask, last, _iso_utc(target.get("updatedAt")), active, closed, result
    )


def _kalshi_price(market: dict, base: str) -> Optional[float]:
    if market.get(f"{base}_dollars") is not None:
        return _price(market.get(f"{base}_dollars"))
    cents = market.get(base)  # 舊版 API 用整數美分
    if cents is None:
        return None
    number = _finite(cents)
    if number is None:
        raise ParseError("format")
    return _price(number / 100.0)


def parse_kalshi_markets(payload: Any, ticker: str, *, expect_outcome: Optional[str] = None) -> PredictionQuote:
    """Kalshi `/markets?event_ticker=…`（或 `/markets/{ticker}`）→ 指定 ticker 的 Yes 報價。

    `updated_time` 是市場資料更新時間、不是成交時間（實測停在 7 月），所以 updated_utc 一律 None。
    """
    data = _as_json(payload)
    if not isinstance(data, dict):
        raise ParseError("format")
    if "markets" in data:
        markets = data["markets"]
    elif isinstance(data.get("market"), dict):
        markets = [data["market"]]
    else:
        raise ParseError("format")
    if not isinstance(markets, list):
        raise ParseError("format")
    if not markets:
        raise ParseError("empty")
    matches = [market for market in markets if isinstance(market, dict) and market.get("ticker") == ticker]
    if not matches:
        raise ParseError("missing_field")
    if len(matches) > 1:
        raise ParseError("conflict")
    market = matches[0]
    status = market.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ParseError("missing_field")
    status = status.strip().lower()
    raw_result = market.get("result")
    if raw_result is not None and not isinstance(raw_result, str):
        raise ParseError("format")
    result_key = (raw_result or "").strip().lower()
    if result_key not in _KALSHI_RESULTS:
        raise ParseError("format")
    result = _KALSHI_RESULTS[result_key]
    outcome = _outcome_label(market.get("yes_sub_title") or market.get("title"), expect_outcome)
    bid, ask, last = _kalshi_price(market, "yes_bid"), _kalshi_price(market, "yes_ask"), _kalshi_price(market, "last_price")
    if bid is None and ask is None and last is None and result is None:
        raise ParseError("missing_field")
    closed = status in _KALSHI_CLOSED or result is not None
    return PredictionQuote("kalshi", ticker, outcome, bid, ask, last, None, status in _KALSHI_ACTIVE, closed, result)


def fetch_prediction_quote(market: PredictionMarket, *, http: Any = None, ledger: Any = None) -> PredictionQuote:
    """只抓 PREDICTION_MARKETS 裡固定的市場。"""
    if market not in PREDICTION_MARKETS:
        raise ValueError("not a fixed prediction market")
    if market.platform == "polymarket":
        url = POLYMARKET_EVENT_URL.format(slug=quote(market.event, safe=""))
        body = _get(market.key, url, http, ledger)
        return parse_polymarket_event(body, market.market_id, expect_outcome=market.expect_outcome)
    url = KALSHI_MARKETS_URL.format(event=quote(market.event, safe=""))
    return parse_kalshi_markets(_get(market.key, url, http, ledger), market.market_id, expect_outcome=market.expect_outcome)
