"""即時股價查詢 — 給咪寶 bot 用，避免假裝有資料庫。

支援：
- 台股 4 位數代號（2330、0050 等）→ 自動加 .TW
- 美股 ticker（AAPL、NVDA、SOXL 等）
- 指數（^SOX、^GSPC、^TWII 等）
- 商品（黃金 / 金價 / XAU → GC=F，COMEX 黃金近月期貨）
- 中文名稱對應（台積電 → 2330）

報價來源 fallback chain：
    1. 富途牛牛 / Futu OpenD get_market_snapshot（本機 OpenD 可用時）
    2. Yahoo TW / Yahoo Finance 即時頁面（requests + bs4 / chart 解析）
    3. yfinance fast_info（intraday 約延遲 1-15 分）
    4. yfinance history(period="5d")（日線收盤，最後保險）

用法：
    from stock_quote import get_quotes_text
    s = get_quotes_text("台積電現在多少？SOXL 呢？")
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import os
import queue
import re
import socket
import sys
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import yfinance as yf  # type: ignore[import-untyped]
from bs4 import BeautifulSoup

logger = logging.getLogger("stock_quote")

# 中文 → 代號（常見台股）
_TW_NAME_MAP = {
    "台積電": "2330",
    "台積": "2330",
    "台達電": "2308",
    "台達": "2308",
    "鴻海": "2317",
    "聯發科": "2454",
    "聯電": "2303",
    "中華電": "2412",
    "中華電信": "2412",
    "國泰金": "2882",
    "富邦金": "2881",
    "玉山金": "2884",
    "兆豐金": "2886",
    "陽明": "2609",
    "長榮": "2603",
    "華航": "2610",
    "群創": "3481",
    "友達": "2409",
    "台塑": "1301",
    "南亞": "1303",
    "台化": "1326",
    "中鋼": "2002",
}

# 美股常見 ticker
_US_TICKERS = {
    "AAPL", "NVDA", "TSM", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "SOXL", "SOXS", "SPY", "VOO", "VTI", "QQQ", "TQQQ", "SQQQ",
    "SMH", "SOXX", "AVGO", "AMD", "INTC", "MU", "TXN", "QCOM", "ARM",
    "BRK.B", "JPM", "BAC", "V", "MA", "WMT", "JNJ", "PG", "XOM", "CVX",
    "COIN", "MSTR", "BITO",
}

_EN_NAME_MAP = {
    "TSMC": "2330.TW",
    "TAIWAN SEMICONDUCTOR": "2330.TW",
}

# 指數 / 中文名 → yfinance 代號
_INDEX_MAP = {
    "費半": "^SOX",
    "費城半導體": "^SOX",
    "S&P 500": "^GSPC",
    "S&P500": "^GSPC",
    "標普": "^GSPC",
    "標普500": "^GSPC",
    "納指": "^IXIC",
    "納斯達克": "^IXIC",
    "道瓊": "^DJI",
    "道指": "^DJI",
    "台股大盤": "^TWII",
    "加權": "^TWII",
    "加權指數": "^TWII",
    "台股": "^TWII",
    "VIX": "^VIX",
    "恐慌指數": "^VIX",
}

_COMMODITY_MAP = {
    # Yahoo's reliable intraday gold symbol is COMEX gold futures. Treat it as
    # a market reference, not spot XAU/USD, in bot-facing labels/prompts.
    "GC=F": "COMEX 黃金近月期貨",
}
_GOLD_QUOTE_SYMBOL = "GC=F"
_GOLD_CHINESE_RE = re.compile(r"黃金|金價|國際金|現貨金")
_GOLD_XAU_RE = re.compile(
    r"(?<![A-Z0-9])XAU(?:\s*[/\-.]?\s*USD)?(?![A-Z0-9])",
    re.IGNORECASE,
)
_GOLD_EN_RE = re.compile(r"(?<![A-Z])GOLD(?![A-Z])", re.IGNORECASE)
_GOLD_EN_CONTEXT_RE = re.compile(
    r"\b(price|quote|spot|futures?|ounce|oz|usd|now|today|current)\b",
    re.IGNORECASE,
)
_GOLD_NON_MARKET_RE = re.compile(r"黃金時段|黃金比例|黃金交叉")
_NUMERIC_STOCK_CONTEXT_RE = re.compile(
    r"股價|股票|台股|個股|現股|上市|上櫃|代號|\bticker\b|\bstock\b",
    re.IGNORECASE,
)

_TWSE_4DIGIT_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_TW_FUTURE_SYMBOL_RE = re.compile(r"^W[A-Z]{2,4}[FGHJKMNQUVXZ]\d$")
_TW_FUTURE_SYMBOL_SCAN_RE = re.compile(r"(?<![A-Z0-9])(W[A-Z]{2,4}[FGHJKMNQUVXZ]\d)(?![A-Z0-9])")
_QUOTE_CONTEXT_RE = re.compile(
    r"(股價|報價|價格|現價|市價|即時|漲跌|漲幅|跌幅|夜盤|近月|期貨|"
    r"\bADR\b|\bquote\b|\bprice\b|多少錢|幾塊|幾元|"
    r"(?:現在|目前).{0,8}(?:多少(?!\s*天)|多少錢|幾塊|幾元))",
    re.IGNORECASE,
)
_MARKET_TERM_RE = re.compile(
    r"股價|報價|價格|現價|市價|即時|漲跌|漲幅|跌幅|夜盤|近月|期貨|\bADR\b|\bquote\b|\bprice\b",
    re.IGNORECASE,
)
_TAIEX_TERM_RE = re.compile(r"台股大盤|台股|加權指數|加權|TAIEX|\^TWII", re.IGNORECASE)
_MOVING_AVG_TERM_RE = re.compile(r"月線|20\s*日(?:線|均線)?|二十日(?:線|均線)?")
_MOVING_AVG_ACTION_RE = re.compile(r"跌破|跌穿|摜破|守住|站上|收復|回測|破了|有破")
_COUNTDOWN_DATE_RE = re.compile(
    r"(\d{1,4}\s*[/\-年月.]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2})",
    re.IGNORECASE,
)
_ADR_RE = re.compile(r"\bADR\b|美國存託", re.IGNORECASE)
_FUTURE_RE = re.compile(r"期貨|近月|夜盤|\bfuture\b|\bfutures\b", re.IGNORECASE)

_TW_TZ = ZoneInfo("Asia/Taipei")
_DAY_START_HOUR = 8
_DAY_END_HOUR = 17
_MONTH_CODE = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}
_TW_ADR_MAP = {
    "2330.TW": ("TSM", "台積電 ADR"),
    "2303.TW": ("UMC", "聯電 ADR"),
}
_TW_FUTURE_MAP = {
    "2330.TW": ("CDF", "台積電近月期貨"),
    "2303.TW": ("CCF", "聯電近月期貨"),
}

# 抓 Yahoo 即時頁的瀏覽器 UA（不裝得太誇張，避免被擋）
_YAHOO_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_REQ_TIMEOUT_S = 3.0
_FUTU_DEFAULT_HOST = "127.0.0.1"
_FUTU_DEFAULT_PORT = 11111
_FUTU_OPEND_CONNECT_TIMEOUT_S = 0.35
_FUTU_SNAPSHOT_TIMEOUT_S = 2.0
_FUTU_TIMEOUT_COOLDOWN_S = 60.0
_FUTU_UNSUPPORTED_SYMBOL_RE = re.compile(r"[=]")
_FUTU_US_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,14}$")
_FUTU_SYMBOL_OVERRIDES: dict[str, str] = {}
_FUTU_IMPORT_LOCK = threading.Lock()
_FUTU_MODULE_CACHE = None
_FUTU_MODULE_CACHE_NAME = ""
_FUTU_COOLDOWN_LOCK = threading.Lock()
_FUTU_COOLDOWN_UNTIL = 0.0


def _looks_like_gold_market_query(text: str) -> bool:
    if not text or _GOLD_NON_MARKET_RE.search(text):
        return False
    if _GOLD_CHINESE_RE.search(text) or _GOLD_XAU_RE.search(text):
        return True
    return bool(_GOLD_EN_RE.search(text) and _GOLD_EN_CONTEXT_RE.search(text))


def _has_explicit_numeric_stock_context(text: str) -> bool:
    return bool(_NUMERIC_STOCK_CONTEXT_RE.search(text or ""))


def detect_symbols(text: str) -> list[str]:
    """從文字偵測股票代號 / 標的。回 list of yfinance symbols（去重）。"""
    if not text:
        return []
    symbols: list[str] = []
    seen: set[str] = set()
    has_gold_market_query = _looks_like_gold_market_query(text)

    def _add(sym: str) -> None:
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)

    # 1. 指數（先掃，避免「台股」被當成 4 位數抓到）
    for name, sym in _INDEX_MAP.items():
        if name in text:
            _add(sym)

    # 1.25. 商品：黃金 / XAU / gold price。先加，並在後面避免把
    # 「4313 美元」這類價格誤抓成台股 4313.TW。
    if has_gold_market_query:
        _add(_GOLD_QUOTE_SYMBOL)

    # 1.5. 台股期貨 Yahoo TW symbol（例：WCDFM6）
    for m in _TW_FUTURE_SYMBOL_SCAN_RE.finditer(text.upper()):
        _add(m.group(1))

    # 1.6. English company aliases that are not valid Yahoo tickers.
    upper_text = text.upper()
    for name, sym in _EN_NAME_MAP.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", upper_text):
            _add(sym)

    # 2. 中文台股名
    for name, code in _TW_NAME_MAP.items():
        if name in text:
            _add(f"{code}.TW")

    # 3. 4 位數台股代號（過濾年份範圍 1900-2100）
    for m in _TWSE_4DIGIT_RE.finditer(text):
        code = m.group(1)
        n = int(code)
        if 1900 <= n <= 2100:
            continue
        if has_gold_market_query and not _has_explicit_numeric_stock_context(text):
            continue
        _add(f"{code}.TW")

    # 4. 0050 / 0056 等 ETF（4 位數 regex 會抓到，這邊保險再補）
    for code in ("0050", "0056", "0061", "00878", "00919", "00929"):
        if re.search(rf"(?<!\d){code}(?!\d)", text):
            _add(f"{code}.TW")

    # 5. 美股 ticker（用 \b word boundary，case insensitive）
    for ticker in _US_TICKERS:
        # 處理 BRK.B 這類含點的 ticker
        pat = re.escape(ticker)
        if re.search(rf"(?<!\w){pat}(?!\w)", text, re.IGNORECASE):
            _add(ticker)

    # 上限 5 個避免拖慢
    return symbols[:5]


# ── helpers ──────────────────────────────────────────────────────────────────


def _to_float(s) -> Optional[float]:
    """'2,325' / '+75.00' / '3.33%' / 287.51 → float。失敗回 None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        value = float(s)
        return value if math.isfinite(value) else None
    txt = str(s).strip().replace(",", "").replace("%", "")
    if not txt or txt in ("-", "--", "N/A"):
        return None
    try:
        value = float(txt)
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def _first_number(values) -> Optional[float]:
    for value in values or []:
        n = _to_float(value)
        if n is not None:
            return n
    return None


def _last_number(values) -> Optional[float]:
    for value in reversed(values or []):
        n = _to_float(value)
        if n is not None:
            return n
    return None


def _max_number(values) -> Optional[float]:
    nums = [_to_float(v) for v in (values or [])]
    nums = [n for n in nums if n is not None]
    return max(nums) if nums else None


def _min_number(values) -> Optional[float]:
    nums = [_to_float(v) for v in (values or [])]
    nums = [n for n in nums if n is not None]
    return min(nums) if nums else None


def _format_epoch_market_time(raw_ts, timezone_name: str | None) -> tuple[str, str] | None:
    ts = _to_float(raw_ts)
    if ts is None:
        return None
    try:
        tz = ZoneInfo(timezone_name) if timezone_name else _TW_TZ
    except Exception:
        tz = _TW_TZ
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%Y-%m-%d %H:%M %Z"), dt.strftime("%Y-%m-%d")


def _fetch_yahoo_html(url: str) -> Optional[str]:
    """抓 Yahoo 頁面 HTML，timeout / 429 / 非 200 一律回 None。"""
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": _YAHOO_UA,
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
            timeout=_REQ_TIMEOUT_S,
        )
        if resp.status_code != 200 or not resp.text:
            logger.info("yahoo fetch %s status=%s", url, resp.status_code)
            return None
        return resp.text
    except Exception as e:
        logger.info("yahoo fetch %s 失敗: %s", url, e)
        return None


def _fetch_yahoo_chart_json(symbol: str) -> Optional[dict]:
    """Fetch Yahoo chart JSON. The HTML quote page can lag behind this endpoint."""
    if not symbol:
        return None
    params = {
        "range": "1d",
        "interval": "1m",
        "includePrePost": "false",
    }
    # Yahoo chart API is more reliable with a plain UA; the full Chrome UA can
    # intermittently get 429 even when the same endpoint is otherwise available.
    headers = {"User-Agent": "Mozilla/5.0"}
    encoded_symbol = quote(symbol, safe="")
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{encoded_symbol}"
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_REQ_TIMEOUT_S)
            if resp.status_code != 200 or not resp.text:
                logger.info("yahoo chart fetch %s host=%s status=%s", symbol, host, resp.status_code)
                continue
            return resp.json()
        except Exception as e:
            logger.info("yahoo chart fetch %s host=%s 失敗: %s", symbol, host, e)
            continue
    return None


# ── provider: Futu OpenD / 富途牛牛 ───────────────────────────────────────────


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _futu_quote_enabled() -> bool:
    return _env_flag("FUTU_QUOTE_ENABLED", default=True)


def _futu_opend_host_port() -> tuple[str, int]:
    host = os.getenv("FUTU_OPEND_HOST", _FUTU_DEFAULT_HOST).strip() or _FUTU_DEFAULT_HOST
    raw_port = os.getenv("FUTU_OPEND_PORT", str(_FUTU_DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        logger.info("invalid FUTU_OPEND_PORT=%r, using default", raw_port)
        port = _FUTU_DEFAULT_PORT
    return host, port


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.info("invalid %s=%r, using default", name, raw)
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _futu_snapshot_timeout_s() -> float:
    return _env_float("FUTU_SNAPSHOT_TIMEOUT_S", _FUTU_SNAPSHOT_TIMEOUT_S, minimum=0.05)


def _futu_timeout_cooldown_s() -> float:
    return _env_float("FUTU_TIMEOUT_COOLDOWN_S", _FUTU_TIMEOUT_COOLDOWN_S, minimum=0.0)


def _futu_symbol_map_from_env() -> dict[str, str]:
    raw = os.getenv("FUTU_SYMBOL_MAP_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as e:
        logger.info("invalid FUTU_SYMBOL_MAP_JSON: %s", e)
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in parsed.items():
        k = str(key or "").strip().upper()
        v = str(value or "").strip().upper()
        if k and v:
            out[k] = v
    return out


def _futu_code_for_symbol(symbol: str) -> Optional[str]:
    """Map the bot's Yahoo-style symbol into a Futu OpenAPI code when known.

    Taiwan stocks/futures and commodities intentionally fall back to Yahoo by
    default because their Futu codes differ by account/market availability.
    Add explicit mappings with FUTU_SYMBOL_MAP_JSON when they are validated.
    """
    if not symbol:
        return None
    normalized = symbol.strip().upper()
    env_override = _futu_symbol_map_from_env().get(normalized)
    if env_override:
        return env_override

    if normalized in _FUTU_SYMBOL_OVERRIDES:
        return _FUTU_SYMBOL_OVERRIDES[normalized]
    if normalized in _COMMODITY_MAP or _FUTU_UNSUPPORTED_SYMBOL_RE.search(normalized):
        return None
    if normalized.endswith(".TW") or normalized.endswith(".TWO"):
        return None
    if normalized.startswith("^") or _is_tw_future_symbol(normalized):
        return None
    if _FUTU_US_SYMBOL_RE.match(normalized):
        return f"US.{normalized}"
    return None


def _futu_opend_available(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_FUTU_OPEND_CONNECT_TIMEOUT_S):
            return True
    except OSError:
        return False


def _futu_in_cooldown() -> bool:
    with _FUTU_COOLDOWN_LOCK:
        return time.monotonic() < _FUTU_COOLDOWN_UNTIL


def _mark_futu_cooldown() -> None:
    global _FUTU_COOLDOWN_UNTIL
    cooldown = _futu_timeout_cooldown_s()
    if cooldown <= 0:
        return
    with _FUTU_COOLDOWN_LOCK:
        _FUTU_COOLDOWN_UNTIL = time.monotonic() + cooldown


def _import_futu_module():
    """Import futu lazily while redirecting its import-time file logger.

    The futu package creates a TimedRotatingFileHandler during import under
    HOME/.com.futunn.FutuOpenD/Log. In sandboxed runs HOME may be read-only, so
    point only this import at a writable temp home and restore HOME immediately.
    """
    global _FUTU_MODULE_CACHE, _FUTU_MODULE_CACHE_NAME
    module_name = os.getenv("FUTU_PY_MODULE", "futu").strip() or "futu"
    if _FUTU_MODULE_CACHE is not None and _FUTU_MODULE_CACHE_NAME == module_name:
        return _FUTU_MODULE_CACHE

    with _FUTU_IMPORT_LOCK:
        if _FUTU_MODULE_CACHE is not None and _FUTU_MODULE_CACHE_NAME == module_name:
            return _FUTU_MODULE_CACHE
        if module_name in sys.modules:
            _FUTU_MODULE_CACHE = sys.modules[module_name]
            _FUTU_MODULE_CACHE_NAME = module_name
            return _FUTU_MODULE_CACHE

        old_home = os.environ.get("HOME")
        log_home = os.getenv("FUTU_LOG_HOME", "/private/tmp/line_bot_futu_home")
        try:
            os.makedirs(log_home, exist_ok=True)
            os.environ["HOME"] = log_home
            module = importlib.import_module(module_name)
            _FUTU_MODULE_CACHE = module
            _FUTU_MODULE_CACHE_NAME = module_name
            return module
        except Exception as e:
            logger.info("futu import failed: %s", e)
            return None
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


def _set_futu_threads_daemon(futu) -> None:
    try:
        sys_config = getattr(futu, "SysConfig", None)
        if sys_config and hasattr(sys_config, "set_all_thread_daemon"):
            sys_config.set_all_thread_daemon(True)
    except Exception as e:
        logger.info("futu set_all_thread_daemon skipped: %s", e)


def _records_from_snapshot(data) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        if all(isinstance(v, list) for v in data.values()):
            length = max((len(v) for v in data.values()), default=0)
            return [
                {key: values[i] for key, values in data.items() if i < len(values)}
                for i in range(length)
            ]
        return [data]
    if hasattr(data, "empty") and bool(getattr(data, "empty")):
        return []
    if hasattr(data, "to_dict"):
        try:
            records = data.to_dict("records")
            return [row for row in records if isinstance(row, dict)]
        except Exception:
            return []
    return []


def _parse_futu_snapshot(data, symbol: str, futu_code: str) -> Optional[dict]:
    records = _records_from_snapshot(data)
    if not records:
        return None
    row = records[0]
    last_price = _to_float(row.get("last_price"))
    if last_price is None:
        return None

    prev_close = _to_float(row.get("prev_close_price"))
    change = _to_float(row.get("change_val"))
    change_pct = _to_float(row.get("change_rate"))
    if change is None and prev_close is not None:
        change = last_price - prev_close
    if change_pct is None and change is not None and prev_close:
        change_pct = change / prev_close * 100

    timestamp = str(row.get("update_time") or "").strip() or time.strftime("%Y-%m-%d %H:%M")
    m = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})", timestamp)
    market_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else time.strftime("%Y-%m-%d")

    return {
        "symbol": symbol,
        "futu_code": futu_code,
        "name": str(row.get("name") or "").strip(),
        "last_price": last_price,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "open": _to_float(row.get("open_price")),
        "high": _to_float(row.get("high_price")),
        "low": _to_float(row.get("low_price")),
        "timestamp": timestamp,
        "last_date": market_date,
        "market_date": market_date,
        "source": "futu_opend",
    }


def _get_futu_quote_unbounded(symbol: str, futu_code: str, host: str, port: int, futu) -> Optional[dict]:
    quote_ctx = None
    try:
        _set_futu_threads_daemon(futu)
        quote_ctx = futu.OpenQuoteContext(host=host, port=port)
        ret, data = quote_ctx.get_market_snapshot([futu_code])
        if ret != getattr(futu, "RET_OK", 0):
            logger.info("futu snapshot %s failed: %s", futu_code, data)
            return None
        return _parse_futu_snapshot(data, symbol, futu_code)
    except Exception as e:
        logger.info("get_futu_quote(%s/%s) failed: %s", symbol, futu_code, e)
        return None
    finally:
        if quote_ctx is not None:
            try:
                quote_ctx.close()
            except Exception:
                pass


def _call_futu_with_timeout(symbol: str, futu_code: str, host: str, port: int, futu) -> Optional[dict]:
    result_q: queue.Queue[Optional[dict]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        result = _get_futu_quote_unbounded(symbol, futu_code, host, port, futu)
        try:
            result_q.put_nowait(result)
        except queue.Full:
            pass

    worker = threading.Thread(
        target=_worker,
        name=f"futu-quote-{futu_code}",
        daemon=True,
    )
    worker.start()
    try:
        return result_q.get(timeout=_futu_snapshot_timeout_s())
    except queue.Empty:
        logger.warning("futu snapshot timed out symbol=%s code=%s", symbol, futu_code)
        _mark_futu_cooldown()
        return None


def get_futu_quote(symbol: str) -> Optional[dict]:
    """Fetch one quote from local Futu OpenD. Failure returns None for fallback."""
    if not _futu_quote_enabled() or _futu_in_cooldown():
        return None
    futu_code = _futu_code_for_symbol(symbol)
    if not futu_code:
        return None

    host, port = _futu_opend_host_port()
    if not _futu_opend_available(host, port):
        logger.info("futu OpenD unavailable host=%s port=%s", host, port)
        return None

    futu = _import_futu_module()
    if futu is None:
        return None

    return _call_futu_with_timeout(symbol, futu_code, host, port, futu)


# ── parser: Yahoo TW (tw.stock.yahoo.com/quote/{symbol}) ─────────────────────


def _parse_yahoo_tw_html(html: str) -> Optional[dict]:
    """解析 Yahoo TW 即時頁，回 dict（缺欄位以 None）；解析不到 last_price 回 None。

    頁面結構：label span（成交/開盤/最高/最低/昨收/漲跌幅/漲跌）後跟著
    value span（純數字或百分比）。把 label/value 配成對。
    """
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.info("yahoo_tw bs4 parse 失敗: %s", e)
        return None

    wanted = {"成交", "開盤", "最高", "最低", "昨收", "漲跌幅", "漲跌"}
    fields: dict[str, str] = {}
    val_re = re.compile(r"^[\d,.+\-%]+$")

    for el in soup.find_all("span"):
        label = el.get_text(strip=True)
        if label not in wanted or label in fields:
            continue
        nxt = el.find_next("span")
        # 走訪後續 span 找第一個純數字
        steps = 0
        while nxt is not None and steps < 8:
            v = nxt.get_text(strip=True)
            if v and v != label and val_re.match(v):
                fields[label] = v
                break
            nxt = nxt.find_next("span")
            steps += 1

    last_price = _to_float(fields.get("成交"))
    if last_price is None:
        return None

    open_ = _to_float(fields.get("開盤"))
    high = _to_float(fields.get("最高"))
    low = _to_float(fields.get("最低"))
    prev_close = _to_float(fields.get("昨收"))
    change = _to_float(fields.get("漲跌"))
    change_pct = _to_float(fields.get("漲跌幅"))

    # 補 change/pct（若頁面沒給）
    if change is None and prev_close is not None:
        change = last_price - prev_close
    if change_pct is None and change is not None and prev_close:
        change_pct = change / prev_close * 100

    # 漲跌符號：頁面上的「漲跌」是絕對值，靠價格判斷正負
    if change is not None and prev_close is not None and last_price < prev_close:
        change = -abs(change)
        if change_pct is not None:
            change_pct = -abs(change_pct)

    # 時間戳：「資料時間：2026/05/07 11:03」
    ts_match = re.search(
        r"資料時間\s*[：:]\s*(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})",
        soup.get_text(" ", strip=True),
    )
    timestamp = ts_match.group(1) if ts_match else time.strftime("%Y-%m-%d %H:%M")

    return {
        "last_price": last_price,
        "open": open_,
        "high": high,
        "low": low,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "timestamp": timestamp,
    }


# ── parser: Yahoo US (finance.yahoo.com/quote/{symbol}) ──────────────────────


def _parse_yahoo_us_html(html: str, symbol: str) -> Optional[dict]:
    """解析 Yahoo US 即時頁（fin-streamer + qsp testid），回 dict；缺 last_price 回 None。"""
    if not html:
        return None

    # 1) qsp testid 拿價格 / 漲跌 / 漲跌幅
    def _testid_text(tid: str) -> Optional[str]:
        m = re.search(
            r'data-testid="' + re.escape(tid) + r'"[^>]*>([^<]+)<', html
        )
        return m.group(1).strip() if m else None

    last_price = _to_float(_testid_text("qsp-price"))
    change = _to_float(_testid_text("qsp-price-change"))
    chg_pct_txt = _testid_text("qsp-price-change-percent")
    # qsp-price-change-percent 通常 "(+1.16%)"
    if chg_pct_txt:
        chg_pct_txt = chg_pct_txt.strip("()")
    change_pct = _to_float(chg_pct_txt)

    # 2) 從 label 附近 fin-streamer 拿 prev_close / open / day range
    def _field_near_label(label: str, field: str) -> Optional[str]:
        # 用 word boundary 避免 "Open" 匹到 "Opening Bid"
        m = re.search(r">" + re.escape(label) + r"\s*[<\s]", html)
        if not m:
            return None
        pos = m.start()
        snippet = html[pos:pos + 1500]
        m2 = re.search(
            r'data-field="' + field + r'"[^>]*data-value="([^"]+)"', snippet
        )
        if not m2:
            m2 = re.search(
                r'data-value="([^"]+)"[^>]*data-field="' + field + r'"', snippet
            )
        return m2.group(1) if m2 else None

    prev_close = _to_float(_field_near_label("Previous Close", "regularMarketPreviousClose"))
    open_ = _to_float(_field_near_label("Open", "regularMarketOpen"))
    day_range = _field_near_label("Day's Range", "regularMarketDayRange") or ""
    high = low = None
    rng = re.match(r"\s*([\d.,]+)\s*-\s*([\d.,]+)\s*", day_range)
    if rng:
        low = _to_float(rng.group(1))
        high = _to_float(rng.group(2))

    if last_price is None:
        return None

    if change is None and prev_close is not None:
        change = last_price - prev_close
    if change_pct is None and change is not None and prev_close:
        change_pct = change / prev_close * 100

    return {
        "last_price": last_price,
        "open": open_,
        "high": high,
        "low": low,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "timestamp": time.strftime("%Y-%m-%d %H:%M"),
    }


def _parse_yahoo_chart_json(payload: dict, symbol: str) -> Optional[dict]:
    """Parse Yahoo chart JSON into the quote schema used by the bot."""
    if not payload:
        return None
    try:
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return None
        item = result[0] or {}
        meta = item.get("meta") or {}
        indicators = item.get("indicators") or {}
        quote_items = indicators.get("quote") or []
        quote_item = quote_items[0] if quote_items else {}
    except Exception as e:
        logger.info("yahoo chart parse %s 失敗: %s", symbol, e)
        return None

    closes = quote_item.get("close") or []
    opens = quote_item.get("open") or []
    highs = quote_item.get("high") or []
    lows = quote_item.get("low") or []

    last_price = _to_float(meta.get("regularMarketPrice"))
    if last_price is None:
        last_price = _last_number(closes)
    if last_price is None:
        return None

    prev_close = _to_float(meta.get("previousClose"))
    if prev_close is None:
        prev_close = _to_float(meta.get("chartPreviousClose"))

    # For 1d chart responses previousClose is normally in meta. If not, the
    # daily arrays still give us a bounded fallback without calling yfinance.
    numeric_closes = [_to_float(v) for v in closes]
    numeric_closes = [v for v in numeric_closes if v is not None]
    if prev_close is None and len(numeric_closes) >= 2:
        prev_close = numeric_closes[-2]

    change = (last_price - prev_close) if prev_close is not None else None
    change_pct = (change / prev_close * 100) if (change is not None and prev_close) else None

    market_time = _format_epoch_market_time(
        meta.get("regularMarketTime"),
        meta.get("exchangeTimezoneName") or meta.get("timezone"),
    )
    timestamp = market_date = None
    if market_time:
        timestamp, market_date = market_time

    return {
        "symbol": symbol,
        "last_price": last_price,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "open": _first_number(opens),
        "high": _to_float(meta.get("regularMarketDayHigh")) or _max_number(highs),
        "low": _to_float(meta.get("regularMarketDayLow")) or _min_number(lows),
        "timestamp": timestamp or time.strftime("%Y-%m-%d %H:%M"),
        "last_date": market_date or time.strftime("%Y-%m-%d"),
        "market_date": market_date,
        "source": "yahoo_chart",
    }


# ── public quote getters: realtime → fast_info → history ─────────────────────


def get_realtime_quote(symbol: str) -> Optional[dict]:
    """從 Yahoo 即時頁抓報價。失敗 / 解析不到回 None。"""
    if not symbol:
        return None
    is_tw_future = _is_tw_future_symbol(symbol)
    is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO") or is_tw_future

    if not is_tw:
        chart_payload = _fetch_yahoo_chart_json(symbol)
        chart_parsed = _parse_yahoo_chart_json(chart_payload, symbol) if chart_payload else None
        if chart_parsed and chart_parsed.get("last_price") is not None:
            return chart_parsed

    if is_tw:
        url = f"https://tw.stock.yahoo.com/quote/{symbol}"
    else:
        url = f"https://finance.yahoo.com/quote/{symbol}"

    html = _fetch_yahoo_html(url)
    if not html:
        return None

    parsed = _parse_yahoo_tw_html(html) if is_tw else _parse_yahoo_us_html(html, symbol)
    if not parsed or parsed.get("last_price") is None:
        return None

    parsed["symbol"] = symbol
    parsed["source"] = "yahoo_realtime"
    if is_tw_future:
        parsed["instrument_type"] = "future"
    # 為相容舊 caller，保留 last_date 欄位
    ts = parsed.get("timestamp") or ""
    parsed["last_date"] = ts.split(" ")[0] if ts else time.strftime("%Y-%m-%d")
    return parsed


def get_fast_info_quote(symbol: str) -> Optional[dict]:
    """yfinance fast_info（intraday 約 1-15 分延遲）。失敗回 None。"""
    if not symbol:
        return None
    try:
        t = yf.Ticker(symbol)
        fi = t.fast_info
        last_price = _to_float(getattr(fi, "last_price", None))
        prev_close = _to_float(getattr(fi, "previous_close", None))
        if last_price is None:
            return None
        change = (last_price - prev_close) if prev_close is not None else None
        change_pct = (change / prev_close * 100) if (change is not None and prev_close) else None
        return {
            "symbol": symbol,
            "last_price": last_price,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "open": _to_float(getattr(fi, "open", None)),
            "high": _to_float(getattr(fi, "day_high", None)),
            "low": _to_float(getattr(fi, "day_low", None)),
            "timestamp": time.strftime("%Y-%m-%d %H:%M"),
            "last_date": time.strftime("%Y-%m-%d"),
            "source": "fast_info",
        }
    except Exception as e:
        logger.info("get_fast_info_quote(%s) 失敗: %s", symbol, e)
        return None


def get_history_quote(symbol: str) -> Optional[dict]:
    """日線收盤（最後保險）— 原 get_quote() 行為。"""
    try:
        t = yf.Ticker(symbol)
        h = t.history(period="5d", auto_adjust=False)
        if len(h) == 0:
            logger.info("get_history_quote(%s): empty history", symbol)
            return None
        last_price = float(h["Close"].iloc[-1])
        prev_close = float(h["Close"].iloc[-2]) if len(h) > 1 else None
        last_date = h.index[-1].strftime("%Y-%m-%d")
        change = (last_price - prev_close) if prev_close is not None else None
        change_pct = (change / prev_close * 100) if (change is not None and prev_close) else None
        return {
            "symbol": symbol,
            "last_price": last_price,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "open": float(h["Open"].iloc[-1]) if "Open" in h else None,
            "high": float(h["High"].iloc[-1]) if "High" in h else None,
            "low": float(h["Low"].iloc[-1]) if "Low" in h else None,
            "last_date": last_date,
            "timestamp": last_date,
            "source": "history",
        }
    except Exception as e:
        logger.warning("get_history_quote(%s) 失敗: %s", symbol, e)
        return None


def get_quote(symbol: str, timeout_s: float = 5.0) -> Optional[dict]:
    """取得單一標的當前報價，依 fallback chain 嘗試。

    chain:
      1. 富途牛牛 / Futu OpenD snapshot
      2. Yahoo 即時頁 / chart（real-time）
      3. yfinance fast_info（intraday）
      4. yfinance history(5d)（日線收盤）

    `timeout_s` 為相容舊 signature 保留，未使用（內部 per-call timeout 已固定 3s）。
    全失敗回 None。
    """
    futu_quote = get_futu_quote(symbol)
    if futu_quote and futu_quote.get("last_price") is not None:
        return futu_quote

    rt = get_realtime_quote(symbol)
    if rt and rt.get("last_price") is not None:
        return rt

    fi = get_fast_info_quote(symbol)
    if fi and fi.get("last_price") is not None:
        return fi

    return get_history_quote(symbol)


def is_taiex_month_line_query(text: str) -> bool:
    """Detect questions asking whether TAIEX broke/held the 20-day line."""
    if not text:
        return False
    return bool(
        _TAIEX_TERM_RE.search(text)
        and _MOVING_AVG_TERM_RE.search(text)
        and (
            _MOVING_AVG_ACTION_RE.search(text)
            or any(k in text for k in ("嗎", "?", "？", "如何", "怎麼看"))
        )
    )


def _get_recent_daily_closes(symbol: str, period: str = "3mo") -> list[tuple[str, float]]:
    """Return valid daily closes as (YYYY-MM-DD, close), dropping Yahoo NaN rows."""
    try:
        h = yf.Ticker(symbol).history(period=period, auto_adjust=False)
    except Exception as e:
        logger.info("history for moving average %s failed: %s", symbol, e)
        return []
    if h is None or len(h) == 0:
        return []
    out: list[tuple[str, float]] = []
    for idx, row in h.iterrows():
        close = _to_float(row.get("Close"))
        if close is None:
            close = _to_float(row.get("Adj Close"))
        if close is None:
            continue
        try:
            d = idx.strftime("%Y-%m-%d")
        except Exception:
            d = str(idx)[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            out.append((d, close))
    return out


def _latest_quote_date(quote: dict) -> str:
    for key in ("market_date", "last_date"):
        value = str(quote.get(key) or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return value
    ts = str(quote.get("timestamp") or "").strip()
    m = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})", ts)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _taiex_20ma_from_quote(quote: dict, window: int = 20) -> Optional[float]:
    current_close = _to_float(quote.get("last_price"))
    if current_close is None:
        return None
    market_date = _latest_quote_date(quote)
    closes = _get_recent_daily_closes("^TWII")
    if not closes:
        return None

    if market_date:
        previous = [close for d, close in closes if d != market_date]
        if len(previous) >= window - 1:
            return (sum(previous[-(window - 1):]) + current_close) / window

    values = [close for _d, close in closes]
    if len(values) >= window:
        return sum(values[-window:]) / window
    return None


def _format_points(value: float | None) -> str:
    return "?" if value is None else f"{value:,.2f}"


def _taiex_value_label(quote: dict, now: datetime | None = None) -> str:
    market_date = _latest_quote_date(quote)
    now_tw = _coerce_taipei_now(now)
    if market_date:
        try:
            if datetime.strptime(market_date, "%Y-%m-%d").date() < now_tw.date():
                return "收盤"
        except ValueError:
            pass
    ts = str(quote.get("timestamp") or "")
    m = re.search(r"\b(\d{2}):(\d{2})\b", ts)
    if m and (int(m.group(1)), int(m.group(2))) >= (13, 30):
        return "收盤"
    if market_date == now_tw.strftime("%Y-%m-%d") and now_tw.hour >= 14:
        return "收盤"
    return "目前"


def get_taiex_month_line_text(text: str) -> Optional[str]:
    """Answer TAIEX monthly-line questions with a yes/no technical verdict."""
    if not is_taiex_month_line_query(text):
        return None

    quote = get_quote("^TWII")
    if not quote:
        return None
    last = _to_float(quote.get("last_price"))
    if last is None:
        return None
    low = _to_float(quote.get("low"))
    high = _to_float(quote.get("high"))
    ma20 = _taiex_20ma_from_quote(quote)
    if ma20 is None:
        return None

    intraday_broke = low is not None and low < ma20
    close_broke = last < ma20
    close_label = _taiex_value_label(quote)

    if intraday_broke and close_broke:
        verdict = f"有，台股大盤盤中跌破月線，{close_label}也沒有收回。"
    elif intraday_broke:
        verdict = f"有，台股大盤盤中跌破月線，但{close_label}守住月線。"
    elif close_broke:
        verdict = f"有，台股大盤{close_label}跌破月線。"
    else:
        verdict = f"沒有，台股大盤盤中與{close_label}都守住月線。"

    ts = quote.get("timestamp") or quote.get("last_date") or ""
    low_line = f"低點 {_format_points(low)}" if low is not None else "低點 ?"
    high_part = f" / 高點 {_format_points(high)}" if high is not None else ""
    distance = last - ma20
    pct = distance / ma20 * 100 if ma20 else 0.0

    return "\n".join(
        [
            verdict,
            f"1. 加權指數{close_label} {_format_points(last)}，20日線約 {_format_points(ma20)}，差距 {distance:+,.2f} 點（{pct:+.2f}%）。",
            f"2. 盤中{low_line}{high_part}；所以重點是「低點有沒有破」和「{close_label}有沒有收回」要分開看。",
            "3. 這種走法是跌破後拉回守線，短線仍偏震盪，還不是完全轉強。",
            f"資料：Yahoo Finance / TWSE 日線；時間 {ts}",
        ]
    )


# ── 文字輸出 ─────────────────────────────────────────────────────────────────


_SOURCE_LABEL = {
    "futu_opend": "富途牛牛",
    "yahoo_realtime": "Yahoo 即時",
    "yahoo_chart": "Yahoo Chart",
    "fast_info": "Yahoo 延遲",
    "history": "日線收盤",
}


def _is_tw_future_symbol(symbol: str) -> bool:
    return bool(symbol and _TW_FUTURE_SYMBOL_RE.match(symbol.upper()))


def _coerce_taipei_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(_TW_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=_TW_TZ)
    return now.astimezone(_TW_TZ)


def _is_daytime(now: datetime) -> bool:
    return _DAY_START_HOUR <= now.hour < _DAY_END_HOUR


def _third_wednesday(year: int, month: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=_TW_TZ)
    days_until_wed = (2 - first.weekday()) % 7
    return first + timedelta(days=days_until_wed + 14)


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    idx = (year * 12) + (month - 1) + offset
    return idx // 12, (idx % 12) + 1


def _future_start_month(now: datetime) -> tuple[int, int]:
    now = _coerce_taipei_now(now)
    expiry = _third_wednesday(now.year, now.month)
    # 台灣股期通常每月第三個週三結算；結算日下午後直接看下一個月。
    if now.date() > expiry.date() or (now.date() == expiry.date() and now.hour >= 14):
        return _add_months(now.year, now.month, 1)
    return now.year, now.month


def _candidate_future_symbols(
    product_code: str,
    now: datetime | None = None,
    months_ahead: int = 4,
) -> list[str]:
    """Return Yahoo TW candidate symbols for the nearest monthly stock futures."""
    if months_ahead <= 0:
        return []
    now = _coerce_taipei_now(now)
    start_year, start_month = _future_start_month(now)
    out: list[str] = []
    for offset in range(months_ahead):
        year, month = _add_months(start_year, start_month, offset)
        out.append(f"W{product_code.upper()}{_MONTH_CODE[month]}{year % 10}")
    return out


def _infer_context_symbols(context: list | None) -> list[str]:
    """Look only at recent user-side messages, so bot quote output is not recycled."""
    if not context:
        return []
    for item in reversed(context[-8:]):
        role = ""
        msg = ""
        if isinstance(item, dict):
            role = str(item.get("role") or item.get("speaker") or "").lower()
            msg = str(item.get("text") or item.get("content") or "")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            role = str(item[0]).lower()
            msg = str(item[1])
        else:
            msg = str(item)
        if role in {"assistant", "bot", "__bot__", "model"}:
            continue
        symbols = detect_symbols(msg)
        if symbols:
            return symbols
    return []


def _stock_currency(symbol: str) -> str:
    if symbol.endswith(".TW") or symbol.endswith(".TWO") or _is_tw_future_symbol(symbol):
        return "TWD"
    if symbol.startswith("^"):
        return "index"
    return "USD"


def _stock_role_label(symbol: str) -> str:
    if symbol.startswith("^"):
        return "指數"
    if _is_tw_future_symbol(symbol):
        return "近月期貨"
    if symbol in _COMMODITY_MAP:
        return "商品"
    return "現股"


def _make_quote_spec(
    symbol: str,
    label: str,
    role: str,
    currency: str,
    group: str,
) -> dict[str, str]:
    return {
        "symbol": symbol,
        "label": label,
        "role": role,
        "currency": currency,
        "group": group,
    }


def _base_quote_spec(symbol: str) -> dict[str, str]:
    label = _label_for(symbol)
    role = _stock_role_label(symbol)
    if label and role == "現股":
        label = f"{label}現股"
    elif not label:
        label = role
    return _make_quote_spec(
        symbol=symbol,
        label=label,
        role=role,
        currency=_stock_currency(symbol),
        group=f"base:{symbol}",
    )


def _quote_specs_for_symbol(
    symbol: str,
    *,
    now: datetime,
    night_mode: bool,
    wants_adr: bool,
    wants_future: bool,
) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    base = symbol.upper() if _is_tw_future_symbol(symbol) else symbol

    if _is_tw_future_symbol(base):
        specs.append(_base_quote_spec(base))
        return specs

    mapped_adr = _TW_ADR_MAP.get(base)
    mapped_future = _TW_FUTURE_MAP.get(base)

    if not night_mode and not wants_adr and not wants_future:
        specs.append(_base_quote_spec(base))
        return specs

    if wants_adr and mapped_adr:
        adr_symbol, adr_label = mapped_adr
        specs.append(_make_quote_spec(adr_symbol, adr_label, "ADR", "USD", f"adr:{base}"))

    if wants_future and mapped_future:
        product_code, future_label = mapped_future
        for fut_symbol in _candidate_future_symbols(product_code, now, months_ahead=4):
            specs.append(
                _make_quote_spec(
                    fut_symbol,
                    future_label,
                    "近月期貨",
                    "TWD",
                    f"future:{product_code}",
                )
            )

    if night_mode and not wants_adr and not wants_future:
        if mapped_adr:
            adr_symbol, adr_label = mapped_adr
            specs.append(_make_quote_spec(adr_symbol, adr_label, "ADR", "USD", f"adr:{base}"))
        if mapped_future:
            product_code, future_label = mapped_future
            for fut_symbol in _candidate_future_symbols(product_code, now, months_ahead=4):
                specs.append(
                    _make_quote_spec(
                        fut_symbol,
                        future_label,
                        "近月期貨",
                        "TWD",
                        f"future:{product_code}",
                    )
                )

    if not specs:
        specs.append(_base_quote_spec(base))
    return specs


def _dedupe_quote_specs(specs: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for spec in specs:
        key = (spec["symbol"], spec["group"])
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


def _get_contextual_quote(symbol: str) -> Optional[dict]:
    """Quote getter for chat prefetch: Futu first, then bounded Yahoo realtime."""
    futu_quote = get_futu_quote(symbol)
    if futu_quote and futu_quote.get("last_price") is not None:
        return futu_quote
    return get_realtime_quote(symbol)


def _fetch_contextual_quotes_with_deadline(
    symbols: list[str],
    total_timeout_s: float,
) -> dict[str, dict]:
    if not symbols or total_timeout_s <= 0:
        return {}

    deadline = time.monotonic() + total_timeout_s
    results: dict[str, dict] = {}
    executor = ThreadPoolExecutor(max_workers=min(4, len(symbols)), thread_name_prefix="stock-quote")
    futures = {}
    try:
        for symbol in symbols:
            if time.monotonic() >= deadline:
                break
            futures[executor.submit(_get_contextual_quote, symbol)] = symbol

        remaining = max(0.001, deadline - time.monotonic())
        try:
            for fut in as_completed(futures, timeout=remaining):
                symbol = futures[fut]
                try:
                    quote = fut.result()
                except Exception as e:
                    logger.info("context quote fetch %s failed: %s", symbol, e)
                    continue
                if quote and quote.get("last_price") is not None:
                    results[symbol] = quote
                if time.monotonic() >= deadline:
                    break
        except FuturesTimeoutError:
            logger.info("context quote fetch reached deadline symbols=%d", len(symbols))
    finally:
        for fut in futures:
            fut.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _format_contextual_quote_line(quote: dict, spec: dict[str, str]) -> str:
    cp = quote.get("change_pct")
    c = quote.get("change")
    cp_str = f"{cp:+.2f}%" if cp is not None else "?"
    c_str = f"{c:+.2f}" if c is not None else "?"
    hi, lo = quote.get("high"), quote.get("low")
    hl = ""
    if hi is not None and lo is not None:
        hl = f"  H {hi:,.2f} / L {lo:,.2f}"
    source = _SOURCE_LABEL.get(quote.get("source") or "", quote.get("source") or "Yahoo")
    return (
        f"{spec['symbol']} ({spec['label']}/{spec['currency']}): "
        f"{quote['last_price']:,.2f}  {c_str} ({cp_str}){hl}  [{source}]"
    )


def _quote_market_date(quote: dict) -> str:
    for key in ("market_date", "last_date"):
        value = str(quote.get(key) or "").strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return value
    ts = str(quote.get("timestamp") or "").strip()
    m = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})", ts)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _contextual_mode_label(
    *,
    night_mode: bool,
    emitted_roles: list[str],
    market_dates: list[str],
    now: datetime,
) -> str:
    has_adr_or_future = any(role in {"ADR", "近月期貨"} for role in emitted_roles)
    if has_adr_or_future:
        return "夜間 ADR/期貨參考" if night_mode else "指定 ADR/期貨參考"

    parsed_dates = []
    for value in market_dates:
        try:
            parsed_dates.append(datetime.strptime(value, "%Y-%m-%d").date())
        except ValueError:
            continue
    if parsed_dates and max(parsed_dates) < now.date():
        return "收盤報價"

    if night_mode:
        return "夜間報價參考"
    if emitted_roles and all(role == "現股" for role in emitted_roles):
        return "日間現股"
    return "日間報價"


def get_contextual_quotes_text(
    text: str,
    *,
    context: list | None = None,
    now: datetime | None = None,
    max_symbols: int = 3,
    total_timeout_s: float = 5.0,
) -> Optional[str]:
    """Context-aware quote fetch for chat.

    Daytime in Taipei returns the discussed stock. Nighttime prefers the mapped
    ADR and nearest monthly Taiwan stock futures, when known. Context is used
    only when the new message is clearly asking for a quote.
    """
    if total_timeout_s <= 0:
        return None

    text = text or ""
    now = _coerce_taipei_now(now)
    symbols = _contextual_quote_symbols(text, context=context)
    if not symbols:
        return None

    wants_adr = bool(_ADR_RE.search(text))
    wants_future = bool(_FUTURE_RE.search(text))
    night_mode = not _is_daytime(now)

    specs: list[dict[str, str]] = []
    for symbol in symbols[:max_symbols]:
        specs.extend(
            _quote_specs_for_symbol(
                symbol,
                now=now,
                night_mode=night_mode,
                wants_adr=wants_adr,
                wants_future=wants_future,
            )
        )
    specs = _dedupe_quote_specs(specs)
    if not specs:
        return None

    quote_by_symbol = _fetch_contextual_quotes_with_deadline(
        [spec["symbol"] for spec in specs],
        total_timeout_s,
    )
    if not quote_by_symbol:
        return None

    lines: list[str] = []
    timestamps: list[str] = []
    market_dates: list[str] = []
    emitted_roles: list[str] = []
    emitted_groups: set[str] = set()
    for spec in specs:
        if spec["group"] in emitted_groups:
            continue
        quote = quote_by_symbol.get(spec["symbol"])
        if not quote:
            continue
        emitted_groups.add(spec["group"])
        timestamps.append(quote.get("timestamp") or quote.get("last_date") or "")
        market_date = _quote_market_date(quote)
        if market_date:
            market_dates.append(market_date)
        emitted_roles.append(spec["role"])
        lines.append(_format_contextual_quote_line(quote, spec))

    if not lines:
        return None

    header_ts = max(timestamps) if timestamps else now.strftime("%Y-%m-%d %H:%M")
    mode_label = _contextual_mode_label(
        night_mode=night_mode,
        emitted_roles=emitted_roles,
        market_dates=market_dates,
        now=now,
    )
    return f"【市場報價｜{mode_label}｜{header_ts}】\n" + "\n".join(lines)


def _contextual_quote_symbols(text: str, *, context: list | None = None) -> list[str]:
    if _looks_like_non_quote_countdown(text or ""):
        return []
    symbols = detect_symbols(text)
    if not symbols and _QUOTE_CONTEXT_RE.search(text or ""):
        symbols = _infer_context_symbols(context)
    return symbols


def should_try_contextual_quote(text: str, *, context: list | None = None) -> bool:
    """Cheap predicate for callers that need quote-policy metadata without fetching."""
    return bool(_contextual_quote_symbols(text or "", context=context))


def _looks_like_non_quote_countdown(text: str) -> bool:
    if not text or _MARKET_TERM_RE.search(text):
        return False
    lowered = text.lower()
    if "天" not in text and "days" not in lowered:
        return False
    if not any(k in text for k in ("距離", "離", "還有", "還剩", "多少天", "幾天")):
        return False
    return bool(_COUNTDOWN_DATE_RE.search(text))


def get_quotes_text(text: str, max_symbols: int = 5) -> Optional[str]:
    """從文字偵測標的並批次取得報價，回 plain text 摘要。

    成功格式（即時）：
        【即時股價｜2026-05-07 11:03（Yahoo 即時）】
        2330.TW (台積電): 2,325.00  +75.00 (+3.33%)  H 2,345 / L 2,310

    失敗 / 沒偵測到 → 回 None。
    """
    symbols = detect_symbols(text)
    if not symbols:
        return None
    symbols = symbols[:max_symbols]

    quotes = []
    timestamps = []
    sources = []
    for sym in symbols:
        q = get_quote(sym)
        if q is None:
            continue
        timestamps.append(q.get("timestamp") or q.get("last_date") or "")
        sources.append(q.get("source") or "history")
        cp = q.get("change_pct")
        c = q.get("change")
        cp_str = f"{cp:+.2f}%" if cp is not None else "?"
        c_str = f"{c:+.2f}" if c is not None else "?"
        label = _label_for(sym)
        suffix = f" ({label})" if label else ""
        # H/L 額外資訊（即時 / fast_info 才會有；history 也帶）
        hi, lo = q.get("high"), q.get("low")
        hl = ""
        if hi is not None and lo is not None:
            hl = f"  H {hi:,.2f} / L {lo:,.2f}"
        quotes.append(
            f"{sym}{suffix}: {q['last_price']:,.2f}  {c_str} ({cp_str}){hl}"
        )

    if not quotes:
        return None

    # header：用「最即時」的時間戳 + 主要來源
    header_ts = max(timestamps) if timestamps else time.strftime("%Y-%m-%d %H:%M")
    # 取最高優先序的 source 當主標籤（Futu > real-time > fast_info > history）
    priority = ("futu_opend", "yahoo_realtime", "yahoo_chart", "fast_info", "history")
    best_src = next((s for s in priority if s in sources), sources[0])
    src_label = _SOURCE_LABEL.get(best_src, best_src)
    header = f"【即時股價｜{header_ts}（{src_label}）】"
    return header + "\n" + "\n".join(quotes)


def _label_for(symbol: str) -> str:
    """yfinance symbol → 中文標籤（如有）。"""
    if symbol in _COMMODITY_MAP:
        return _COMMODITY_MAP[symbol]
    if symbol.endswith(".TW"):
        code = symbol.replace(".TW", "")
        for name, c in _TW_NAME_MAP.items():
            if c == code and len(name) >= 2:
                return name
    for name, sym in _INDEX_MAP.items():
        if sym == symbol and len(name) >= 2:
            return name
    return ""


if __name__ == "__main__":
    test_inputs = [
        "台積電 2330 今天股價多少？",
        "SOXL 跟費半的關係",
        "0050 vs VOO 哪個好？",
    ]
    for t in test_inputs:
        print(f"\n>>> {t}")
        print(get_quotes_text(t) or "(沒偵測到 / 失敗)")
