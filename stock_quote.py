"""即時股價查詢 — 給咪寶 bot 用，避免假裝有資料庫。

支援：
- 台股 4 位數代號（2330、0050 等）→ 自動加 .TW
- 美股 ticker（AAPL、NVDA、SOXL 等）
- 指數（^SOX、^GSPC、^TWII 等）
- 中文名稱對應（台積電 → 2330）

用法：
    from stock_quote import get_quotes_text
    s = get_quotes_text("台積電現在多少？SOXL 呢？")
    # 「2330.TW: 1245.0 (+5.0 / +0.40%) ...」
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import yfinance as yf

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


def get_quote(symbol: str, timeout_s: float = 5.0) -> Optional[dict]:
    """取得單一標的當前報價。失敗 / 找不到回 None。"""
    try:
        t = yf.Ticker(symbol)
        # 用 history 抓最近 2 天，比 fast_info 穩定
        h = t.history(period="5d", auto_adjust=False)
        if len(h) == 0:
            logger.info("get_quote(%s): empty history", symbol)
            return None
        last_price = float(h["Close"].iloc[-1])
        prev_close = float(h["Close"].iloc[-2]) if len(h) > 1 else None
        last_date = h.index[-1].strftime("%Y-%m-%d")
        change = (last_price - prev_close) if prev_close is not None else None
        change_pct = (change / prev_close * 100) if (change is not None and prev_close) else None
        volume = int(h["Volume"].iloc[-1]) if "Volume" in h else None
        return {
            "symbol": symbol,
            "last_price": last_price,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "last_date": last_date,
            "volume": volume,
        }
    except Exception as e:
        logger.warning("get_quote(%s) 失敗: %s", symbol, e)
        return None


def get_quotes_text(text: str, max_symbols: int = 5) -> Optional[str]:
    """從文字偵測標的並批次取得報價，回 plain text 摘要。

    若沒偵測到 / 全部失敗 → 回 None。
    成功格式：
        【即時股價｜2026-05-05】
        2330.TW (台積電): 1,245.00  +5.00 (+0.40%)
        SOXL: 131.33  -2.10 (-1.57%)
    """
    symbols = detect_symbols(text)
    if not symbols:
        return None
    symbols = symbols[:max_symbols]

    quotes = []
    last_dates = []
    for sym in symbols:
        q = get_quote(sym)
        if q is None:
            continue
        last_dates.append(q["last_date"])
        cp = q["change_pct"]
        c = q["change"]
        cp_str = f"{cp:+.2f}%" if cp is not None else "?"
        c_str = f"{c:+.2f}" if c is not None else "?"
        # 名稱反查（中文標籤）
        label = _label_for(sym)
        suffix = f" ({label})" if label else ""
        quotes.append(f"{sym}{suffix}: {q['last_price']:,.2f}  {c_str} ({cp_str})")

    if not quotes:
        return None

    header_date = max(last_dates) if last_dates else time.strftime("%Y-%m-%d")
    header = f"【即時股價｜資料日 {header_date}】"
    return header + "\n" + "\n".join(quotes)


def _label_for(symbol: str) -> str:
    """yfinance symbol → 中文標籤（如有）。"""
    # 反查台股
    if symbol.endswith(".TW"):
        code = symbol.replace(".TW", "")
        for name, c in _TW_NAME_MAP.items():
            if c == code and len(name) >= 2:
                return name
    # 反查指數
    for name, sym in _INDEX_MAP.items():
        if sym == symbol and len(name) >= 2:
            return name
    return ""


if __name__ == "__main__":
    # 簡單 smoke test
    test_inputs = [
        "台積電 2330 今天股價多少？",
        "SOXL 跟費半的關係",
        "0050 vs VOO 哪個好？",
    ]
    for t in test_inputs:
        print(f"\n>>> {t}")
        print(get_quotes_text(t) or "(沒偵測到 / 失敗)")
