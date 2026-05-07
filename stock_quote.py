"""即時股價查詢 — 給咪寶 bot 用，避免假裝有資料庫。

支援：
- 台股 4 位數代號（2330、0050 等）→ 自動加 .TW
- 美股 ticker（AAPL、NVDA、SOXL 等）
- 指數（^SOX、^GSPC、^TWII 等）
- 中文名稱對應（台積電 → 2330）

報價來源 fallback chain（2026-05-07 加，原本只用日線收盤太慢）：
    1. Yahoo TW / Yahoo Finance 即時頁面（requests + bs4 解析）→ 真即時
    2. yfinance fast_info（intraday 約延遲 1-15 分）
    3. yfinance history(period="5d")（日線收盤，最後保險）

用法：
    from stock_quote import get_quotes_text
    s = get_quotes_text("台積電現在多少？SOXL 呢？")
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests
import yfinance as yf
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

_TWSE_4DIGIT_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")

# 抓 Yahoo 即時頁的瀏覽器 UA（不裝得太誇張，避免被擋）
_YAHOO_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_REQ_TIMEOUT_S = 3.0


def detect_symbols(text: str) -> list[str]:
    """從文字偵測股票代號 / 標的。回 list of yfinance symbols（去重）。"""
    if not text:
        return []
    symbols: list[str] = []
    seen: set[str] = set()

    def _add(sym: str) -> None:
        if sym not in seen:
            seen.add(sym)
            symbols.append(sym)

    # 1. 指數（先掃，避免「台股」被當成 4 位數抓到）
    for name, sym in _INDEX_MAP.items():
        if name in text:
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
        return float(s)
    txt = str(s).strip().replace(",", "").replace("%", "")
    if not txt or txt in ("-", "--", "N/A"):
        return None
    try:
        return float(txt)
    except ValueError:
        return None


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


# ── public quote getters: realtime → fast_info → history ─────────────────────


def get_realtime_quote(symbol: str) -> Optional[dict]:
    """從 Yahoo 即時頁抓報價。失敗 / 解析不到回 None。"""
    if not symbol:
        return None
    is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO")
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
      1. Yahoo 即時頁（real-time）
      2. yfinance fast_info（intraday）
      3. yfinance history(5d)（日線收盤）

    `timeout_s` 為相容舊 signature 保留，未使用（內部 per-call timeout 已固定 3s）。
    全失敗回 None。
    """
    rt = get_realtime_quote(symbol)
    if rt and rt.get("last_price") is not None:
        return rt

    fi = get_fast_info_quote(symbol)
    if fi and fi.get("last_price") is not None:
        return fi

    return get_history_quote(symbol)


# ── 文字輸出 ─────────────────────────────────────────────────────────────────


_SOURCE_LABEL = {
    "yahoo_realtime": "Yahoo 即時",
    "fast_info": "Yahoo 延遲",
    "history": "日線收盤",
}


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
    # 取最高優先序的 source 當主標籤（real-time > fast_info > history）
    priority = ("yahoo_realtime", "fast_info", "history")
    best_src = next((s for s in priority if s in sources), sources[0])
    src_label = _SOURCE_LABEL.get(best_src, best_src)
    header = f"【即時股價｜{header_ts}（{src_label}）】"
    return header + "\n" + "\n".join(quotes)


def _label_for(symbol: str) -> str:
    """yfinance symbol → 中文標籤（如有）。"""
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
