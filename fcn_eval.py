"""FCN 評估（2026-10-09）：家人打「/FCN 股票… 年利率X%」，回一張 CP值卡片。

Andrew：「你只要評估利率＆公司＆波動，來判斷CP值即可」。所以只看三件事：
- 利率：年利率比無風險利率（美元＝美國短期公債，台幣＝台銀一年期定存）多多少。
- 波動：每檔近一年與近三個月的實際股價波動（取大的，偏保守），照一般 FCN 的
  現金流模擬，算出「照這個波動應該給多少年利率」（合理年利率）。
- 公司：近四季有沒有虧損；有就降一級。
CP值的 E＝(年利率−無風險)÷max(合理−無風險, 0.5 個百分點)。

一般 FCN 的機制（DBS、Emperor Capital 的說明）：每月付息；每 21 個交易日看一次，
全部 ≥ 期初的 KO 就提前結束；期間收盤跌破 KI、到期最差那檔仍 < 執行價，就到期用
執行價接最差那檔。預設 KO 100%、KI＝執行價＝70%（Andrew 描述的「跌 30% 接股」）；
訊息裡有寫就用寫的。記憶式 KO 照一般 KO 粗估（實測合理利率差 ≤ 0.3 個百分點）。

全部數字由程式算，不經過模型；資料抓不到就說抓不到，不補數字。只用 reply token 回，
不推播。卡片上只有固定句型、程式算的數字、表中名稱與解析後的代號，沒有使用者原文。

正規表示式照 H5 規則（agent_rules/line_bot.md）：`*`／`+` 裡不放交替、標籤後綴最多
出現一次、數字一律有界或占有量詞；輸入在 NFKC 之後先限長。
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as _wait_futures
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import numpy as np
import requests

logger = logging.getLogger("fcn_eval")

# ── 常數 ───────────────────────────────────────────────────────────────────
KO_DEFAULT = 1.00
KI_DEFAULT = 0.70
OBS_DAYS = 21
DAYS_PER_YEAR = 252
DEFAULT_MONTHS = 6
MAX_MONTHS = 24
MAX_TICKERS = 4
N_PATHS = 20000
SEED = 20261009
# TODO(calibrate): 起始門檻。目前只有 1 筆公開真實報價（優分析 2026-08-27），拿到
# Andrew 手邊的理專報價後再校準。
GREEN_E = 0.90
YELLOW_E = 0.70
MIN_PREMIUM = 0.005  # 0.5 個百分點
DIV_CAP = 0.08
RATE_MAX = 0.60
BODY_MAX_CHARS = 2000
QUOTE_MAX_CHARS = 3000
DATA_BUDGET_S = 8.0
TIMEOUT_COOLDOWN_S = 60.0
HTTP_TIMEOUT_S = 3.0
CACHE_TTL_S = 6 * 3600
CACHE_MAX = 64
MIN_BARS = 120
STALE_DAYS = 10
DAILY_OVERLAP_MIN = 60
WEEKLY_OVERLAP_MIN = 26

TWD_RF_DEFAULT = 0.01715  # 臺灣銀行一年期定儲機動利率，2026-10-02 牌告
_TW = ZoneInfo("Asia/Taipei")

PREFIX = "FCN 評估："

# ── 固定文字 ────────────────────────────────────────────────────────────────
EXAMPLE = "/FCN NVDA AMD TSLA 年利率18%"
USAGE_TEXT = (
    "FCN 評估：看銀行 FCN（固定配息結構型商品）的 CP值\n"
    "把理專給的股票和年利率打上來，例如：\n"
    f"{EXAMPLE}\n"
    "・1–4 檔，代號或中文名都可以（例：輝達、台積電、2330）\n"
    "・期限沒寫就當 6 個月，可以加「12個月」；有 KO、KI 也可以加，例如 KO95% KI60%\n"
    "・也可以引用理專那則訊息，打 /FCN\n"
    "咪寶會看利率、波動、公司三件事，回一張評估卡；只供參考，以銀行條件書為準"
)
TEXT_TOO_LONG = f"FCN 評估：訊息太長了，請只寫股票和年利率，例如 {EXAMPLE}"
TEXT_UNAVAILABLE = "FCN 評估：暫時拿不到股價資料，請稍後再試"
TEXT_QUOTED_BOT = "FCN 評估：要重算請點卡片上的按鈕，或照格式打，例如 /FCN NVDA AMD 年利率12%"
TEXT_QUOTED_MEDIA = "FCN 評估：引用的是圖片或影片，請貼文字，或照格式打，例如 /FCN NVDA AMD 年利率12%"
TEXT_QUOTED_MISSING = "FCN 評估：找不到被引用的訊息，請照格式打，例如 /FCN NVDA AMD 年利率12%"
TEXT_QUOTED_NOTHING = "FCN 評估：引用的訊息裡找不到股票和年利率，請照格式打，例如 /FCN NVDA AMD 年利率12%"
TEXT_NO_RATE = "FCN 評估：還缺年利率，請補上，例如 /FCN NVDA AMD 年利率12%"
TEXT_NO_SYMBOL = "FCN 評估：沒看到股票，請寫 1–4 檔股票代號或名稱，例如 /FCN NVDA AMD 年利率12%"
TEXT_UNSUPPORTED_CURRENCY = "FCN 評估：目前只支援美元、台幣計價"
TEXT_NO_USD_RF = "FCN 評估：暫時拿不到美國公債利率，請稍後再試"

# ── 名稱表 ──────────────────────────────────────────────────────────────────
# 美股中文名（卡片顯示用的名字放第一個）。
_US_CJK_NAMES: dict[str, str] = {
    "輝達": "NVDA", "英偉達": "NVDA", "超微": "AMD", "超微半導體": "AMD",
    "特斯拉": "TSLA", "蘋果": "AAPL", "微軟": "MSFT", "博通": "AVGO",
    "谷歌": "GOOGL", "字母": "GOOGL", "亞馬遜": "AMZN", "臉書": "META",
    "台積電ADR": "TSM", "台積電 ADR": "TSM", "台積ADR": "TSM", "美光": "MU", "高通": "QCOM",
    "英特爾": "INTC", "網飛": "NFLX", "甲骨文": "ORCL", "可口可樂": "KO",
    "富國銀行": "WFC", "摩根大通": "JPM", "小摩": "JPM", "美國銀行": "BAC",
    "波克夏": "BRK-B", "埃克森美孚": "XOM", "雪佛龍": "CVX", "嬌生": "JNJ",
    "寶僑": "PG", "沃爾瑪": "WMT", "好市多": "COST", "迪士尼": "DIS",
    "耐吉": "NKE", "星巴克": "SBUX", "麥當勞": "MCD", "輝瑞": "PFE",
    "禮來": "LLY", "諾和諾德": "NVO", "艾司摩爾": "ASML", "阿斯麥": "ASML",
    "安謀": "ARM", "美超微": "SMCI", "超級微電腦": "SMCI", "微策略": "MSTR",
    "聯合健康": "UNH", "思科": "CSCO", "應用材料": "AMAT", "科林研發": "LRCX",
    "德州儀器": "TXN", "邁威爾": "MRVL", "戴爾": "DELL", "帕蘭泰爾": "PLTR",
}
_US_DISPLAY: dict[str, str] = {}
for _name, _sym in _US_CJK_NAMES.items():
    _US_DISPLAY.setdefault(_sym, _name)
_US_DISPLAY["TSM"] = "台積電ADR"

# 英文名（不分大小寫，整個 token）。
_ASCII_NAMES: dict[str, str] = {
    "NVIDIA": "NVDA", "TESLA": "TSLA", "APPLE": "AAPL", "MICROSOFT": "MSFT",
    "AMAZON": "AMZN", "GOOGLE": "GOOGL", "ALPHABET": "GOOGL", "BROADCOM": "AVGO",
    "NETFLIX": "NFLX", "ORACLE": "ORCL", "MICRON": "MU", "QUALCOMM": "QCOM",
    "INTEL": "INTC", "PALANTIR": "PLTR", "COINBASE": "COIN", "TSMC": "TSM",
}

# 台股補充（stock_quote._TW_NAME_MAP 沒有的）。
_TW_EXTRA_NAMES: dict[str, str] = {
    "南亞科": "2408", "長榮航": "2618", "環球晶": "6488", "日月光投控": "3711",
    "日月光": "3711", "廣達": "2382", "緯創": "3231", "英業達": "2356",
    "華碩": "2357", "宏碁": "2353", "大立光": "3008", "台泥": "1101",
    "統一超": "2912", "中信金": "2891", "元大金": "2885", "第一金": "2892",
    "合庫金": "5880", "華南金": "2880", "開發金": "2883", "永豐金": "2890",
    "台新金": "2887", "瑞昱": "2379", "聯詠": "3034", "世芯": "3661",
    "創意": "3443", "力積電": "6770", "華邦電": "2344", "旺宏": "2337",
    "欣興": "3037", "台光電": "2383", "奇鋐": "3017", "緯穎": "6669",
    "技嘉": "2376", "微星": "2377", "智邦": "2345", "台灣大": "3045",
    "遠傳": "4904", "和泰車": "2207", "中租": "5871", "萬海": "2615",
    "台塑化": "6505", "元大台灣50": "0050", "富邦台50": "006208",
    "元大高股息": "0056", "國泰永續高股息": "00878", "群益台灣精選高息": "00919",
}

_COMMON_US_ETFS = frozenset({
    "SPY", "QQQ", "VOO", "VTI", "IVV", "DIA", "IWM", "SOXX", "SMH", "XLK", "XLF",
    "XLE", "XLV", "ARKK", "TLT", "GLD", "SLV", "EEM", "EFA", "VEA", "VWO", "SCHD",
    "JEPI", "TQQQ", "SOXL", "SQQQ", "KWEB", "FXI", "EWT", "EWJ", "IBIT",
})


def _tw_name_map() -> dict[str, str]:
    """台股中文名 → 代號。stock_quote 的表＋本模組補充（契約測試守住來源）。"""
    merged: dict[str, str] = {}
    try:
        import stock_quote

        source = getattr(stock_quote, "_TW_NAME_MAP", {})
        if isinstance(source, dict):
            merged.update({str(k): str(v) for k, v in source.items()})
    except Exception as exc:
        logger.warning("fcn tw name map unavailable error_type=%s", type(exc).__name__)
    merged.update(_TW_EXTRA_NAMES)
    return merged


def _us_whitelist() -> frozenset[str]:
    """寬鬆讀取時，英文代號只收這份白名單（避免把 ADR、AI、TEL 讀成股票）。"""
    tickers: set[str] = set(_US_CJK_NAMES.values()) | set(_ASCII_NAMES.values())
    tickers |= _COMMON_US_ETFS
    try:
        import stock_quote

        extra = getattr(stock_quote, "_US_TICKERS", set())
        tickers |= {str(t).upper() for t in extra}
    except Exception as exc:
        logger.warning("fcn us whitelist source unavailable error_type=%s", type(exc).__name__)
    return frozenset(t.replace(".", "-") for t in tickers)


_TW_NAMES = _tw_name_map()
_CJK_NAMES: dict[str, str] = {**{k: v for k, v in _TW_NAMES.items()}, **_US_CJK_NAMES}
_TW_DISPLAY: dict[str, str] = {}
for _name, _code in sorted(_TW_NAMES.items(), key=lambda kv: -len(kv[0])):
    _TW_DISPLAY.setdefault(_code, _name)
_US_WHITELIST = _us_whitelist()
# 最長字優先的字面交替（沒有重複量詞，線性掃描）。
_NAME_RE = re.compile(
    "|".join(re.escape(n) for n in sorted(_CJK_NAMES, key=len, reverse=True))
)

# ── 正規表示式（H5：有界量詞、占有量詞、`*` 內不放交替） ─────────────────────
_NUM = r"(?<![\d.])(\d{1,3}(?:\.\d{1,10})?)(?!\d)(?!\.\d)"
_PCT = _NUM + r"\s{0,3}%"
_SEP = r"\s{0,3}(?:約|為|是|:)?\s{0,3}"
_AMOUNT = r"(\d[\d,]{0,15}+(?:\.\d{1,4}+)?+)"
_CUR_WORDS = (
    "US$|NT$|USD|TWD|NTD|EUR|JPY|HKD|CNY|RMB|AUD|GBP|SGD|CHF|"
    "新台幣|台幣|美元|美金|歐元|日圓|日幣|港幣|港元|人民幣|澳幣|英鎊|新幣"
)
_CUR = r"(?<![A-Za-z])(?P<cur>" + _CUR_WORDS.replace("$", r"\$") + r")(?![A-Za-z])"
_CUR_CODES = {
    "US$": "USD", "USD": "USD", "美元": "USD", "美金": "USD",
    "NT$": "TWD", "TWD": "TWD", "NTD": "TWD", "新台幣": "TWD", "台幣": "TWD",
}

_NOISE_RES = (
    re.compile(r"https?://\S{1,300}"),
    re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,5}"),
    re.compile(r"(?:℡|TEL|Tel\.?|tel|電話|手機|行動|聯絡|分機|傳真|Fax|FAX)\s{0,3}:?\s{0,3}[\d()+\-\s]{3,24}"),
    re.compile(r"(?<!\d)\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2}(?!\d)"),
    re.compile(r"(?<!\d)\d{4}年\d{1,2}月(?:\d{1,2}日)?"),
    re.compile(r"(?<!\d)\d{4}年(?!期)"),
    re.compile(r"(?<!\d)(?:民國\s{0,2})?\d{2,3}年\d{1,2}月(?:\d{1,2}日)?"),
    re.compile(r"民國\s{0,2}\d{2,3}年"),
    re.compile(r"(?<![\d/])\d{1,2}/\d{1,2}(?![\d/])"),
    re.compile(r"(?<!\d)\d{1,2}:\d{2}(?:\s{0,2}[AaPp][Mm])?(?![A-Za-z])"),
    re.compile(r"(?<![A-Z])[A-Z]{2}[A-Z0-9]{9}\d(?![A-Z0-9])"),  # ISIN
    re.compile(
        r"(?:觀察期|每|第)\s{0,3}:?\s{0,3}"
        r"(?:\d{1,2}|[一二兩三四五六七八九十]{1,3})\s{0,2}(?:個月|月期|月|[Mm](?![A-Za-z]))"
    ),
)
_CUR_STRONG_RES = (
    re.compile(r"(?:計價幣別|計價貨幣|計價|幣別)" + _SEP + _CUR),
    re.compile(_CUR + r"\s{0,2}計價"),
)
_AMOUNT_LABEL = r"(?:申購金額|最低申購|申購|面額|本金|投資金額|最低)"
_CUR_WEAK_RES = (
    re.compile(_AMOUNT_LABEL + _SEP + _CUR + r"\s{0,2}" + _AMOUNT),
    re.compile(_AMOUNT_LABEL + _SEP + _AMOUNT + r"\s{0,2}" + _CUR),
)
_CUR_ANY_RE = re.compile(_CUR)
_MONTHLY_RE = re.compile(
    r"(?:每月利率|每月配息|每月付息|每月票息|月配息率|月利率|月配息|月票息|每月領|月息|月領|月配|每月)" + _SEP + _PCT
)
_MONTHLY_SUFFIX_RE = re.compile(_PCT + r"\s{0,3}(?:/\s?月|per\s?month)", re.IGNORECASE)
_ANNUAL_RE = re.compile(
    r"(?<!月)(?:年化報酬率|年化配息率|年化收益率|年化利率|年配息率|年利率|年配息|年化|年息|配息率|利率|票息)"
    + _SEP + _NUM + r"\s{0,3}%?"
)
_PA_RE = re.compile(_PCT + r"\s{0,3}p\.?\s?a\.?(?![A-Za-z])", re.IGNORECASE)
_LABEL_SUFFIX = r"\s{0,2}(?:價格|觸發價|價|水準|level|barrier|trigger)?"
_KO_RE = re.compile(
    r"(?:(?<![A-Za-z])(?:knock[ -]?out|auto[ -]?call|KO)(?![A-Za-z])|自動提前出場|提前出場|敲出)"
    + _LABEL_SUFFIX + _SEP + _PCT,
    re.IGNORECASE,
)
_KI_RE = re.compile(
    r"(?:(?<![A-Za-z])(knock[ -]?in|EKI|AKI|DKI|KI)(?![A-Za-z])|下限價|下限|敲入|保護價|保護)"
    + _LABEL_SUFFIX + _SEP + _PCT,
    re.IGNORECASE,
)
_STRIKE_RE = re.compile(
    r"(?:(?<![A-Za-z])strike(?![A-Za-z])|執行價格|執行價|履約價格|履約價|轉換價格|轉換價)"
    + r"\s{0,2}(?:水準|level)?" + _SEP + _PCT,
    re.IGNORECASE,
)
_DROP_RE = re.compile(
    r"(跌破|低於|下跌|跌)\s{0,2}(期初價?)?\s{0,2}" + _PCT + r"(?:就?接股)?"
)
_EKI_WORD_RE = re.compile(r"到期觀察|歐式")
_LOCKOUT_RE = re.compile(
    r"(?:鎖定期|閉鎖期|不可提前出場期|不可提前出場|不可提前)\s{0,3}:?\s{0,3}"
    r"(\d{1,2}|[一二兩三四五六七八九十]{1,3})\s{0,2}(?:個月|月|[Mm](?![A-Za-z]))"
)
_MEMORY_RE = re.compile(r"記憶式|記憶型|memory", re.IGNORECASE)
_PRICE_RES = (
    re.compile(
        r"(?:期初價格|期初價|期初|收盤價|股價|現價|價格|申購金額|最低申購|申購|面額|本金|投資金額|發行規模|規模|發行量|額度|金額|最低)"
        + _SEP + r"(?:US\$|NT\$|\$|USD|TWD|NTD|美元|美金|新台幣|台幣)?\s{0,2}"
        + _AMOUNT + r"(?!\s{0,3}%)(?:\s{0,2}(?:新台幣|台幣|美元|美金|元|塊))?"
    ),
    re.compile(r"(?<![\d.,])" + _AMOUNT + r"\s{0,2}(?:新台幣|台幣|美元|美金|元|塊|股|張)"),
    re.compile(r"(?:US\$|NT\$|\$|USD|TWD|NTD)\s{0,2}" + _AMOUNT),
)
_SKIP_BEFORE_TENOR = r"(?<!每)(?<!第)(?<!觀察)(?<!鎖定期)(?<!閉鎖期)(?<!不可提前)"
_TENOR_RES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], int]], ...] = (
    (re.compile(_SKIP_BEFORE_TENOR + r"(?<![\d.\-－])(\d{1,2})\s{0,2}(?:個月|月期)"), lambda m: int(m.group(1))),
    (re.compile(r"(?<![\dA-Za-z.\-－])(\d{1,2})\s{0,2}months?(?![A-Za-z])", re.IGNORECASE), lambda m: int(m.group(1))),
    (re.compile(r"(?<![\d.\-－])(\d{1,2})\s{0,2}年期"), lambda m: 12 * int(m.group(1))),
    (re.compile(r"(?<![\d.\-－])(\d{1,2})\s{0,2}年(?![利化息月配\d期])"), lambda m: 12 * int(m.group(1))),
    (re.compile(r"(?<![\dA-Za-z.\-－])(\d{1,2})\s{0,2}years?(?![A-Za-z])", re.IGNORECASE), lambda m: 12 * int(m.group(1))),
    (re.compile(r"半年"), lambda m: 6),
    (re.compile(_SKIP_BEFORE_TENOR + r"(?<![一二兩三四五六七八九十])([一二兩三四五六七八九十]{1,3})\s{0,2}(?:個月|月期)"),
     lambda m: _cn_to_int(m.group(1)) or 0),
    (re.compile(r"(?<![一二兩三四五六七八九十])([一二兩三四五六七八九十]{1,3})\s{0,2}年(?:期)?(?![利化息月配])"),
     lambda m: 12 * (_cn_to_int(m.group(1)) or 0)),
    (re.compile(r"(?<![A-Za-z0-9.\-－])(\d{1,2})\s?([MmYy])(?![A-Za-z0-9])"),
     lambda m: int(m.group(1)) * (12 if m.group(2) in "Yy" else 1)),
)
_LEFTOVER_PCT_RE = re.compile(_PCT)
_CN_DIGITS = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> Optional[int]:
    """一～九十九的中文數字（十、十二、二十四）；其他回 None。"""
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 0) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        if (head and head not in _CN_DIGITS) or (tail and tail not in _CN_DIGITS):
            return None
        return tens * 10 + ones
    return _CN_DIGITS.get(text) if len(text) == 1 else None


_LEFTOVER_TENOR_RE = re.compile(
    r"(?<!每)(?<!第)(?<!觀察)(?<!鎖定期)(?<!閉鎖期)(?<!不可提前)"
    r"(?:[一二兩三四五六七八九十百]{1,4}|\d{1,4})\s{0,2}(?:個月|月期|年期)"
)
_BAD_TENOR_RE = re.compile(
    r"(?<![\dA-Za-z.])\d{3,4}\s{0,2}(?:個月|月期|年期|months?|years?)"
    r"|(?<![\dA-Za-z.])\d{3,4}[MmYy](?![A-Za-z0-9])"
    r"|(?<![\d.])\d{3}\s{0,2}年(?!\d)"
    r"|(?<![\d.])\d{1,3}\.\d{1,3}\s{0,2}(?:個月|月期|年期|年|months?|years?|[MmYy](?![A-Za-z0-9]))"
    r"|[-－]\s{0,2}\d{1,3}\s{0,2}(?:個月|月期|年期|年|months?|years?|[MmYy](?![A-Za-z0-9]))",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(_NUM + r"\s{0,3}%?\s{0,3}(?:~|～|-|－|到|至)\s{0,3}" + _NUM + r"\s{0,3}%")
_NEGATIVE_PCT_RE = re.compile(r"(?<![\d.%])[-－]\s{0,2}\d{1,3}(?:\.\d{1,10})?\s{0,3}%")
_FILLER_RE = re.compile(
    r"FCN|fcn|Fcn|評估|條件|連結|標的|股票|報價|理專|到期日|發行日|交割日|評價日|每月配息|每月付息|每月觀察|月配息|月配|每月|配息|商品|天期|期限|"
    r"幫我看|幫看|看看|請問|划算嗎|可以嗎|怎麼樣|如何|這檔|這個|一下|"
    r"記憶式|記憶型|[嗎呢吧喔啊的了]|[。！？!?：:「」『』（）()\[\]【】〈〉《》…~～*＊]"
)
_TOKEN_RE = re.compile(r"[^\s,，、/／;；|｜+＋&]+")
_CONNECTOR_RE = re.compile(r"還有|[和跟與及]")
_TW_CODE_TOKEN_RE = re.compile(r"\d{4,6}[A-Za-z]?(?:\.(?:TWO|TW|two|tw|Two|Tw))?")
_US_TOKEN_RE = re.compile(r"[A-Za-z]{1,5}(?:[.\-][A-Za-z])?")
_TW_CODE_SCAN_RE = re.compile(
    r"(?<![\d.\-:#/A-Za-z])(\d{4,6}[A-Z]?)(?:\.(TWO|TW))?(?![\d\-:#/%A-Za-z])"
)
_US_SCAN_RE = re.compile(r"(?<![A-Za-z0-9.$])([A-Z]{1,5}(?:[.\-][A-Z])?)(?![A-Za-z0-9])")
_ASCII_NAME_SCAN_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(_ASCII_NAMES, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_STRICT_JARGON = frozenset({"KI", "EKI", "AKI", "DKI", "FCN", "ELN", "PA", "TENOR", "STRIKE", "NOTE"})
_KO_CONTEXT_RE = re.compile(r"\s{0,3}(?:[\d%:：]|barrier|level|trigger|價|觀察)", re.IGNORECASE)
_SYMBOL_FETCH_RE = re.compile(r"[A-Z0-9]{1,6}(?:[.\-][A-Z]{1,3})?")
_MEDIA_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,6}\]")
_SANITIZE_RE = re.compile(r"[^0-9A-Za-z一-鿿.%]")


# ── 資料結構 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Terms:
    ko: float = KO_DEFAULT
    ki: float = KI_DEFAULT
    strike: float = KI_DEFAULT
    eki: bool = False
    memory_ko: bool = False
    months: int = DEFAULT_MONTHS
    ko_given: bool = False
    ki_given: bool = False
    strike_given: bool = False
    months_given: bool = False
    lockout_months: int = 0  # 鎖定期：前 N 個月不會提前結束


@dataclass(frozen=True)
class FcnRequest:
    symbols: tuple[str, ...]
    rate: float
    terms: Terms
    currency: Optional[str] = None  # "USD"/"TWD"；None＝沒寫，之後依標的推測

    def effective(self) -> tuple:
        """比較兩個 request 是否等價（標準指令 round-trip 用）。"""
        t = self.terms
        return (
            self.symbols, round(self.rate, 6), t.ko, t.ki, t.strike, t.eki,
            t.memory_ko, t.months, t.lockout_months, self.currency,
        )


@dataclass(frozen=True)
class FcnReply:
    text: Optional[str] = None
    flex: Optional[dict] = None
    alt_text: Optional[str] = None


@dataclass
class _Problem:
    kind: str  # conflict / unsupported / too_many / no_rate / no_symbol / unread
    text: str


@dataclass
class _Fields:
    rest: str
    rate: Optional[float] = None
    months: Optional[int] = None
    ko: Optional[float] = None
    ki: Optional[float] = None
    strike: Optional[float] = None
    eki: bool = False
    memory_ko: bool = False
    drop_level: Optional[float] = None
    lockout: Optional[int] = None
    currency: Optional[str] = None
    unread_pcts: list[str] = field(default_factory=list)
    problem: Optional[_Problem] = None


@dataclass(frozen=True)
class TickerStats:
    symbol: str
    market: str  # "TW" / "US"
    currency: str
    dates: tuple  # tuple[date, ...]，交易所當地日期
    closes: np.ndarray  # 調整後收盤（含配息），用來算報酬
    spot: float
    last_date: date
    vol: float
    div_yield: float
    fetched_at: float
    instrument_type: str = "EQUITY"


@dataclass(frozen=True)
class SimResult:
    fair: float
    p_convert: float
    convert_counts: tuple[int, ...]
    breach_counts: tuple[int, ...]
    corr_note: bool  # 有配對因資料不足把相關係數當 0


# ── 文字工具 ────────────────────────────────────────────────────────────────
def _normalize(text: str | None) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    return s.replace("　", " ").strip()


def _fmt_pct(x: float, digits: int = 2) -> str:
    """0.17889 → '17.89'（去掉多餘的 0）。"""
    s = f"{x * 100:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _sanitize(token: str) -> str:
    """看不懂的字只留中英數，去掉 @／＠ 等符號，避免回覆變成提及。"""
    return _SANITIZE_RE.sub("", token)[:12]


def _blank(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " " * (span[1] - span[0]) + text[span[1]:]


def _take_all(pattern: re.Pattern[str], text: str) -> tuple[str, list[re.Match[str]]]:
    matches = list(pattern.finditer(text))
    for m in reversed(matches):
        text = _blank(text, m.span())
    return text, matches


def _pct_value(raw: str) -> Optional[float]:
    try:
        value = float(raw) / 100.0
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


# ── 欄位擷取（嚴格與寬鬆共用） ─────────────────────────────────────────────
def _extract_fields(text: str) -> _Fields:
    rest = text
    for pattern in _NOISE_RES:
        rest, _ = _take_all(pattern, rest)
    fields = _Fields(rest=rest)

    # 幣別：有標籤的（強）＞申購／面額旁的（弱）＞單獨出現的（強）；標的報價旁的不算。
    strong: list[str] = []
    weak: list[str] = []
    for pattern in _CUR_STRONG_RES:
        rest, matches = _take_all(pattern, rest)
        strong += [m.group("cur") for m in matches]
    for pattern in _CUR_WEAK_RES:
        rest, matches = _take_all(pattern, rest)
        weak += [m.group("cur") for m in matches]
    for m in list(_CUR_ANY_RE.finditer(rest)):
        before = rest[max(0, m.start() - 3): m.start()]
        after = rest[m.end(): m.end() + 3]
        if re.search(r"[\d$]\s{0,2}$", before) or re.match(r"\s{0,2}[\d$]", after):
            continue  # 接在數字旁：是報價或金額，不是商品幣別
        strong.append(m.group("cur"))
        rest = _blank(rest, m.span())
    for group in (strong, weak):
        for word in group:
            if word not in _CUR_CODES:
                fields.problem = _Problem("unsupported", TEXT_UNSUPPORTED_CURRENCY)
                return fields
    strong_codes = {_CUR_CODES[w] for w in strong}
    weak_codes = {_CUR_CODES[w] for w in weak}
    if len(strong_codes) > 1 or (not strong_codes and len(weak_codes) > 1):
        fields.problem = _Problem("conflict", "FCN 評估：看到兩種計價幣別，請只寫一種（美元或台幣）")
        return fields
    if strong_codes or weak_codes:
        fields.currency = next(iter(strong_codes or weak_codes))

    # 區間、負數的利率直接問。
    if _RANGE_RE.search(rest) or _NEGATIVE_PCT_RE.search(rest):
        fields.problem = _Problem("conflict", "FCN 評估：利率請寫一個數字，例如 年利率18%")
        return fields

    # 機制欄位（KO／KI／執行價／跌幅）先挖掉，免得它們的百分比被當成利率。
    rest, ko_matches = _take_all(_KO_RE, rest)
    rest, ki_matches = _take_all(_KI_RE, rest)
    rest, strike_matches = _take_all(_STRIKE_RE, rest)
    rest, drop_matches = _take_all(_DROP_RE, rest)
    fields.memory_ko = bool(_MEMORY_RE.search(text))
    rest, _memory_words = _take_all(_MEMORY_RE, rest)
    rest, eki_words = _take_all(_EKI_WORD_RE, rest)
    if eki_words:
        fields.eki = True
    rest, lockouts = _take_all(_LOCKOUT_RE, rest)
    lockout_values = sorted({
        int(m.group(1)) if m.group(1).isdigit() else (_cn_to_int(m.group(1)) or -1) for m in lockouts
    })
    if len(lockout_values) > 1 or (lockout_values and lockout_values[0] < 0):
        fields.problem = _Problem("conflict", "FCN 評估：鎖定期看不懂，請寫一個數字，例如 鎖定期1個月")
        return fields
    fields.lockout = lockout_values[0] if lockout_values else None

    # 利率：有前置標籤的月利率、年利率、p.a.，最後才看「X%/月」這種後綴。
    rates: list[float] = []
    rest, monthly = _take_all(_MONTHLY_RE, rest)
    rest, annual = _take_all(_ANNUAL_RE, rest)
    rest, pa = _take_all(_PA_RE, rest)
    rest, monthly_suffix = _take_all(_MONTHLY_SUFFIX_RE, rest)
    monthly = monthly + monthly_suffix
    monthly_values = [v * 12 for v in (_pct_value(m.group(1)) for m in monthly) if v is not None]
    annual_values = [v for v in (_pct_value(m.group(1)) for m in annual + pa) if v is not None]

    def _single(values: list[float], what: str) -> Optional[float]:
        distinct = sorted({round(v, 6) for v in values})
        if len(distinct) > 1:
            fields.problem = _Problem("conflict", f"FCN 評估：{what}寫了不只一個，請只寫一個")
        return distinct[0] if distinct else None

    fields.ko = _single([v for v in (_pct_value(m.group(1)) for m in ko_matches) if v is not None], "KO")
    ki_values = []
    for m in ki_matches:
        label = (m.group(1) or "").upper()
        if label == "EKI":
            fields.eki = True
        value = _pct_value(m.group(2))
        if value is not None:
            ki_values.append(value)
    fields.ki = _single(ki_values, "KI")
    fields.strike = _single(
        [v for v in (_pct_value(m.group(1)) for m in strike_matches) if v is not None], "執行價"
    )
    drop_levels = []
    for m in drop_matches:
        verb, initial, raw = m.group(1), m.group(2), m.group(3)
        x = _pct_value(raw)
        if x is None:
            continue
        if verb in ("下跌", "跌"):
            drop_levels.append(1.0 - x)
        elif initial:
            drop_levels.append(x)
        elif x < 0.5:
            drop_levels.append(1.0 - x)  # Andrew 的用法：「跌破30%的價位」＝跌 30%
        elif x > 0.5:
            drop_levels.append(x)
        else:
            drop_levels.append(0.5)
    fields.drop_level = _single(drop_levels, "跌幅")
    if fields.problem:
        return fields

    # 價格與金額（不當代號）。
    for pattern in _PRICE_RES:
        rest, _ = _take_all(pattern, rest)

    # 期限。
    months: list[int] = []
    for pattern, convert in _TENOR_RES:
        for m in list(pattern.finditer(rest)):
            months.append(convert(m))
            rest = _blank(rest, m.span())
    if _BAD_TENOR_RE.search(rest) or _LEFTOVER_TENOR_RE.search(rest) or 0 in months:
        fields.problem = _Problem("conflict", "FCN 評估：期限請寫 1–24 個月")
        return fields
    distinct_months = sorted(set(months))
    if len(distinct_months) > 1:
        fields.problem = _Problem("conflict", "FCN 評估：看到兩個不同的期限，請只寫一個，例如 6個月")
        return fields
    fields.months = distinct_months[0] if distinct_months else None

    # 利率：月利率、年利率各自只能有一個值；兩者都有時差 ≤ 0.05 個百分點才算同一個。
    distinct_monthly = sorted({round(v, 6) for v in monthly_values})
    distinct_annual = sorted({round(v, 6) for v in annual_values})
    if len(distinct_monthly) > 1 or len(distinct_annual) > 1:
        shown = "、".join(f"{_fmt_pct(r)}%" for r in sorted(set(distinct_annual + distinct_monthly))[:3])
        fields.problem = _Problem("conflict", f"FCN 評估：看到兩個不同的利率（{shown}），請只寫年利率")
        return fields
    if distinct_monthly and distinct_annual:
        if abs(distinct_monthly[0] - distinct_annual[0]) <= 0.0005:
            rates = [monthly_values[0]]
        else:
            rates = [distinct_monthly[0], distinct_annual[0]]
    else:
        rates = monthly_values[:1] or annual_values[:1]
    leftover = [m for m in _LEFTOVER_PCT_RE.finditer(rest)]
    if not rates and len(leftover) == 1:
        value = _pct_value(leftover[0].group(1))
        if value is not None:
            rates = [value]
            rest = _blank(rest, leftover[0].span())
            leftover = []
    fields.unread_pcts = [m.group(0).replace(" ", "") for m in leftover][:3]
    for m in reversed(leftover):
        rest = _blank(rest, m.span())
    distinct_rates = sorted({round(r, 6) for r in rates})
    if len(distinct_rates) > 1:
        shown = "、".join(f"{_fmt_pct(r)}%" for r in distinct_rates[:3])
        fields.problem = _Problem("conflict", f"FCN 評估：看到兩個不同的利率（{shown}），請只寫年利率")
        return fields
    fields.rate = distinct_rates[0] if distinct_rates else None
    fields.rest = rest
    return fields


def _build_terms(fields: _Fields) -> tuple[Optional[Terms], Optional[_Problem]]:
    ki = fields.ki
    strike = fields.strike
    if ki is None and strike is None and fields.drop_level is not None:
        ki = strike = fields.drop_level
    if ki is None and strike is not None:
        # 只寫執行價：一般是「KI＝執行價」；執行價 > 95%（例如期初價接股）時 KI 照預設 70%。
        ki = strike if strike <= 0.95 else None
    if strike is None and ki is not None:
        strike = ki
    ki_given = ki is not None  # 寫了，或從寫了的執行價推出來；套預設的不算
    strike_given = strike is not None
    ki = KI_DEFAULT if ki is None else ki
    strike = KI_DEFAULT if strike is None else strike
    ko = KO_DEFAULT if fields.ko is None else fields.ko
    months = DEFAULT_MONTHS if fields.months is None else fields.months
    bad = "FCN 評估：條件看起來不對（{}），請確認後再打"
    if not 0.80 <= ko <= 1.20:
        return None, _Problem("conflict", bad.format("KO 要在 80%–120%"))
    if not 0.40 <= ki <= 0.95:
        return None, _Problem("conflict", bad.format("KI 要在 40%–95%"))
    if not 0.50 <= strike <= 1.10:
        return None, _Problem("conflict", bad.format("執行價要在 50%–110%"))
    if strike < ki:
        return None, _Problem("conflict", bad.format("執行價比 KI 低"))
    if ko <= ki:
        return None, _Problem("conflict", bad.format("KO 要比 KI 高"))
    if not 1 <= months <= MAX_MONTHS:
        return None, _Problem("conflict", "FCN 評估：期限請寫 1–24 個月")
    lockout = fields.lockout or 0
    if not 0 <= lockout < months:
        return None, _Problem("conflict", "FCN 評估：鎖定期要比期限短，請確認後再打")
    return Terms(
        ko=ko, ki=ki, strike=strike, eki=fields.eki, memory_ko=fields.memory_ko,
        months=months, ko_given=fields.ko is not None, ki_given=ki_given,
        strike_given=strike_given, months_given=fields.months is not None, lockout_months=lockout,
    ), None


def _check_rate(rate: Optional[float]) -> Optional[_Problem]:
    if rate is None:
        return _Problem("no_rate", TEXT_NO_RATE)
    if not 0 < rate <= RATE_MAX:
        return _Problem("conflict", f"FCN 評估：年利率 {_fmt_pct(rate)}% 看起來不對，請確認（0–60%）")
    return None


def _normalize_symbol(token: str) -> Optional[str]:
    t = token.strip()
    if _TW_CODE_TOKEN_RE.fullmatch(t):
        code, _, suffix = t.partition(".")
        code = code.upper()
        return f"{code}.TWO" if suffix.upper() == "TWO" else code
    upper = t.upper()
    if upper in _ASCII_NAMES:
        return _ASCII_NAMES[upper]
    if _US_TOKEN_RE.fullmatch(t) and upper not in _STRICT_JARGON:
        return upper.replace(".", "-")
    return None


def _finish(symbols: list[str], fields: _Fields) -> tuple[Optional[FcnRequest], Optional[_Problem]]:
    deduped: list[str] = []
    for s in symbols:
        base = s.split(".")[0]
        same = [i for i, d in enumerate(deduped) if d.split(".")[0] == base]
        if not same:
            deduped.append(s)
        elif s.endswith(".TWO"):
            deduped[same[0]] = s  # 上櫃後綴比較明確
    if not deduped:
        return None, _Problem("no_symbol", TEXT_NO_SYMBOL)
    if len(deduped) > MAX_TICKERS:
        return None, _Problem("too_many", f"FCN 評估：FCN 最多 4 檔，這裡有 {len(deduped)} 檔")
    problem = _check_rate(fields.rate)
    if problem:
        return None, problem
    terms, problem = _build_terms(fields)
    if problem:
        return None, problem
    return FcnRequest(symbols=tuple(deduped), rate=float(fields.rate), terms=terms, currency=fields.currency), None


def parse_strict(body: str) -> tuple[Optional[FcnRequest], Optional[_Problem]]:
    """家人自己打的 `/FCN …`：每個字都要看得懂。

    先在整段文字裡認名稱表（最長字優先，「台積電 ADR」「和泰車」這種含空白或連接詞的
    名稱不會被切斷），再把剩下的字依空白、標點、連接詞切開，一個一個認代號。
    """
    fields = _extract_fields(_normalize(body))
    if fields.problem:
        return None, fields.problem
    rest = fields.rest
    found: list[tuple[int, str]] = []
    for m in _NAME_RE.finditer(rest):
        found.append((m.start(), _CJK_NAMES[m.group(0)]))
        rest = _blank(rest, m.span())
    rest = _FILLER_RE.sub(lambda m: " " * len(m.group(0)), rest)
    rest = _CONNECTOR_RE.sub(lambda m: " " * len(m.group(0)), rest)
    unread: list[str] = list(fields.unread_pcts)
    for m in _TOKEN_RE.finditer(rest):
        symbol = _normalize_symbol(m.group(0))
        if symbol:
            found.append((m.start(), symbol))
        else:
            unread.append(m.group(0))
    if unread:
        shown = "、".join(s for s in (_sanitize(t) for t in unread[:3]) if s)
        detail = f"（{shown}）" if shown else ""
        return None, _Problem(
            "unread",
            f"FCN 評估：有看不懂的字{detail}，請只寫股票代號或名稱和年利率，例如 {EXAMPLE}",
        )
    return _finish([sym for _pos, sym in sorted(found)], fields)


def extract_lenient(text: str) -> tuple[Optional[FcnRequest], Optional[_Problem], list[str]]:
    """理專訊息：只收名稱表、台股代號、白名單內（原文大寫）的英文代號。"""
    fields = _extract_fields(_normalize(text))
    if fields.problem:
        return None, fields.problem, []
    rest = fields.rest
    found: list[tuple[int, str]] = []
    for m in _NAME_RE.finditer(rest):
        found.append((m.start(), _CJK_NAMES[m.group(0)]))
        rest = _blank(rest, m.span())
    for m in _ASCII_NAME_SCAN_RE.finditer(rest):
        found.append((m.start(), _ASCII_NAMES[m.group(1).upper()]))
        rest = _blank(rest, m.span())
    for m in _TW_CODE_SCAN_RE.finditer(rest):
        code, suffix = m.group(1), m.group(2)
        if re.fullmatch(r"0[1-9]\d{2}", code):
            continue  # 09xx 是手機號碼開頭；台股 0 開頭的代號都是 00xx
        found.append((m.start(), f"{code}.TWO" if suffix == "TWO" else code))
    for m in _US_SCAN_RE.finditer(rest):
        sym = m.group(1).replace(".", "-")
        if sym == "KO" and _KO_CONTEXT_RE.match(rest, m.end()):
            continue  # 「KO Barrier」「KO：」是提前出場，不是可口可樂
        if sym in _US_WHITELIST:
            found.append((m.start(), sym))
    symbols = [s for _, s in sorted(found)]
    if not symbols or fields.rate is None:
        return None, None, fields.unread_pcts
    request, problem = _finish(symbols, fields)
    if problem and problem.kind in ("no_rate", "no_symbol"):
        return None, None, fields.unread_pcts
    return request, problem, fields.unread_pcts


def to_command(req: FcnRequest) -> str:
    """無損的標準指令：確認卡與「重算」按鈕送出的就是這一串。"""
    t = req.terms
    parts = ["/FCN", *req.symbols, f"年利率{_fmt_pct(req.rate, 4)}%"]
    if t.months_given:
        parts.append(f"{t.months}個月")  # 沒寫就不寫，重算後卡片仍標「期限沒寫，照預設」
    # 寫了的、或和預設不同的才寫；只靠預設的不寫，重算後卡片仍標「沒寫，照預設」。
    if t.ko_given or t.ko != KO_DEFAULT:
        parts.append(f"KO{_fmt_pct(t.ko, 4)}%")
    # 執行價 ≤ 95% 又和 KI 不同時一定要寫 KI，否則重新解析會把 KI 推成執行價。
    if t.ki_given or t.ki != KI_DEFAULT or (t.strike != t.ki and t.strike <= 0.95):
        parts.append(f"{'EKI' if t.eki else 'KI'}{_fmt_pct(t.ki, 4)}%")
    elif t.eki:
        parts.append("到期觀察")
    if t.strike != t.ki or (t.strike_given and not t.ki_given):
        parts.append(f"執行價{_fmt_pct(t.strike, 4)}%")
    if t.lockout_months:
        parts.append(f"鎖定期{t.lockout_months}個月")
    if t.memory_ko:
        parts.append("記憶式")
    if req.currency == "USD":
        parts.append("美元")
    elif req.currency == "TWD":
        parts.append("台幣")
    return " ".join(parts)


# ── 抓資料（requests＋固定兩個 Yahoo 主機，同 stock_quote._fetch_yahoo_chart_json_with_params） ──
_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_PRICE_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="fcn-price")
_PRICE_SLOTS = threading.BoundedSemaphore(6)
_FIN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fcn-fin")
_FIN_SLOTS = threading.BoundedSemaphore(4)
_state_lock = threading.Lock()
_cooldown_until = 0.0
_STATS_CACHE: "OrderedDict[str, TickerStats]" = OrderedDict()
_FIN_CACHE: "OrderedDict[str, tuple[float, tuple[float, ...]]]" = OrderedDict()
_RESOLVED: "OrderedDict[str, str]" = OrderedDict()
_RF_CACHE: dict[str, tuple[float, float]] = {}
_SIM_CACHE: "OrderedDict[tuple, SimResult]" = OrderedDict()


def _http_get_json(path: str, params: dict) -> Optional[dict]:
    for host in _HOSTS:
        try:
            resp = requests.get(
                f"https://{host}{path}", params=params, headers=_HEADERS, timeout=HTTP_TIMEOUT_S
            )
        except requests.RequestException as exc:
            logger.info("fcn yahoo fetch failed host=%s error_type=%s", host, type(exc).__name__)
            continue
        if resp.status_code != 200:
            logger.info("fcn yahoo fetch host=%s status=%s", host, resp.status_code)
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _fetch_chart(symbol: str, range_value: str = "1y") -> Optional[dict]:
    if symbol != "^IRX" and not _SYMBOL_FETCH_RE.fullmatch(symbol):
        return None
    return _http_get_json(
        f"/v8/finance/chart/{quote(symbol, safe='')}",
        {"range": range_value, "interval": "1d", "events": "div", "includePrePost": "false"},
    )


def _fetch_net_income(symbol: str) -> Optional[tuple[float, ...]]:
    if not _SYMBOL_FETCH_RE.fullmatch(symbol):
        return None
    end = int(time.time())
    data = _http_get_json(
        f"/ws/fundamentals-timeseries/v1/finance/timeseries/{quote(symbol, safe='')}",
        {"symbol": symbol, "type": "quarterlyNetIncome", "period1": end - 800 * 86400, "period2": end},
    )
    try:
        results = data["timeseries"]["result"]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    rows = []
    for item in results or []:
        for row in item.get("quarterlyNetIncome") or []:
            if not isinstance(row, dict):
                continue
            raw = (row.get("reportedValue") or {}).get("raw")
            when = row.get("asOfDate")
            if isinstance(raw, (int, float)) and math.isfinite(raw) and isinstance(when, str):
                rows.append((when, float(raw)))
    rows.sort()
    if len(rows) < 4:
        return None
    return tuple(v for _, v in rows[-4:])


def _fetch_usd_rf() -> Optional[float]:
    data = _fetch_chart("^IRX", range_value="5d")
    try:
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None
    values = [c for c in closes or [] if isinstance(c, (int, float)) and math.isfinite(c)]
    if not values:
        return None
    rf = values[-1] / 100.0
    return rf if 0.0 <= rf <= 0.2 else None


def _twd_rf() -> float:
    raw = os.getenv("LINE_BOT_FCN_TWD_RF", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if 0.0 <= value <= 0.2:
            return value
        logger.warning("ignoring invalid LINE_BOT_FCN_TWD_RF")
    return TWD_RF_DEFAULT


def _parse_chart(symbol: str, data: Optional[dict]) -> tuple[Optional[TickerStats], str]:
    """回傳 (stats, 問題種類)；問題種類：'' / missing / type / currency / short / stale。"""
    try:
        result = data["chart"]["result"][0]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None, "missing"
    meta = result.get("meta") or {}
    itype = str(meta.get("instrumentType") or "").upper()
    if itype not in ("EQUITY", "ETF"):
        return None, "type"
    currency = str(meta.get("currency") or "").upper()
    if currency not in ("USD", "TWD"):
        return None, "currency"
    try:
        tz = ZoneInfo(str(meta.get("exchangeTimezoneName") or "UTC"))
    except Exception:
        tz = ZoneInfo("UTC")
    stamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    closes = ((indicators.get("quote") or [{}])[0] or {}).get("close") or []
    adj = ((indicators.get("adjclose") or [{}])[0] or {}).get("adjclose") or closes
    by_date: dict[date, tuple[float, float]] = {}
    for ts, c, a in zip(stamps, closes, adj):
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (c, a)):
            continue
        if not isinstance(ts, (int, float)):
            continue
        d = datetime.fromtimestamp(ts, tz).date()
        by_date[d] = (float(c), float(a))
    if len(by_date) < MIN_BARS:
        return None, "short"
    days = sorted(by_date)
    last = days[-1]
    if (datetime.now(_TW).date() - last).days > STALE_DAYS:
        return None, "stale"
    close_arr = np.array([by_date[d][0] for d in days])
    adj_arr = np.array([by_date[d][1] for d in days])
    rets = np.diff(np.log(adj_arr))
    if len(rets) < MIN_BARS - 1:
        return None, "short"
    vol_year = float(np.std(rets, ddof=1) * math.sqrt(DAYS_PER_YEAR))
    vol_recent = float(np.std(rets[-63:], ddof=1) * math.sqrt(DAYS_PER_YEAR))
    vol = max(vol_year, vol_recent)
    if not math.isfinite(vol) or vol <= 0:
        return None, "short"
    spot = float(close_arr[-1])
    dividends = 0.0
    events = (result.get("events") or {}).get("dividends") or {}
    if isinstance(events, dict):
        for item in events.values():
            if not isinstance(item, dict):
                continue
            amount, when = item.get("amount"), item.get("date")
            if isinstance(amount, (int, float)) and math.isfinite(amount) and isinstance(when, (int, float)):
                if (last - datetime.fromtimestamp(when, tz).date()).days <= 365:
                    dividends += float(amount)
    div_yield = min(max(dividends / spot, 0.0), DIV_CAP) if spot > 0 else 0.0
    market = "TW" if currency == "TWD" else "US"
    return TickerStats(
        symbol=symbol, market=market, currency=currency, dates=tuple(days), closes=adj_arr,
        spot=spot, last_date=last, vol=vol, div_yield=div_yield, fetched_at=time.time(),
        instrument_type=itype,
    ), ""


def _fetch_symbol_stats(symbol: str) -> tuple[str, Optional[TickerStats], str]:
    """台股沒寫後綴就先試 .TW 再試 .TWO。回傳 (實際代號, stats, 問題)。"""
    candidates = [f"{symbol}.TW", f"{symbol}.TWO"] if re.fullmatch(r"\d{4,6}[A-Z]?", symbol) else [symbol]
    problem = "missing"
    for candidate in candidates:
        stats, problem = _parse_chart(candidate, _fetch_chart(candidate))
        if stats is not None:
            return candidate, stats, ""
        if problem not in ("missing", "short"):
            break
    return candidates[-1], None, problem


def _submit(pool: ThreadPoolExecutor, slots: threading.BoundedSemaphore, fn, *args) -> Optional[Future]:
    if not slots.acquire(blocking=False):
        return None

    def _run():
        try:
            return fn(*args)
        finally:
            slots.release()

    try:
        future = pool.submit(_run)
    except RuntimeError:
        slots.release()
        return None
    future.add_done_callback(lambda f: slots.release() if f.cancelled() else None)
    return future


def _cache_get(cache: OrderedDict, key: str):
    with _state_lock:
        item = cache.get(key)
        if item is None:
            return None
        fetched_at = item.fetched_at if isinstance(item, TickerStats) else item[0]
        if time.time() - fetched_at > CACHE_TTL_S:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return item


def _cache_put(cache: OrderedDict, key: str, value) -> None:
    with _state_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > CACHE_MAX:
            cache.popitem(last=False)


def clear_caches() -> None:
    global _cooldown_until
    with _state_lock:
        _STATS_CACHE.clear()
        _FIN_CACHE.clear()
        _RESOLVED.clear()
        _RF_CACHE.clear()
        _SIM_CACHE.clear()
        _cooldown_until = 0.0


@dataclass
class _Gathered:
    stats: list[TickerStats]
    net_income: dict[str, Optional[tuple[float, ...]]]
    usd_rf: Optional[float]
    problem: Optional[str] = None


def _final_symbol(sym: str) -> Optional[str]:
    """不用先抓價格就知道 Yahoo 代號的（美股、寫了 .TWO、已解析過的台股）；否則 None。"""
    if re.fullmatch(r"\d{4,6}[A-Z]?", sym):
        with _state_lock:
            return _RESOLVED.get(sym)
    return sym


def _gather(req: FcnRequest, budget_s: float) -> _Gathered:
    """價格、美元利率、財報同時送出、共用同一個截止時間；價格或利率沒到就整個回暫時拿不到，
    財報沒到就是 unknown（不降級）。"""
    global _cooldown_until
    now = time.monotonic()
    with _state_lock:
        cooling = now < _cooldown_until
    if cooling or budget_s <= 0:
        return _Gathered([], {}, None, TEXT_UNAVAILABLE)
    deadline = now + budget_s
    resolved: dict[str, TickerStats] = {}
    price_futs: dict[str, Future] = {}
    fin_futs: dict[str, Future] = {}
    net_income: dict[str, Optional[tuple[float, ...]]] = {}

    def _submit_income(sym: str, actual: str) -> None:
        cached = _cache_get(_FIN_CACHE, actual)
        if cached is not None:
            net_income[sym] = cached[1]
            return
        fut = _submit(_FIN_POOL, _FIN_SLOTS, _fetch_net_income, actual)
        if fut is None:
            net_income[sym] = None
        else:
            fin_futs[sym] = fut

    def _give_up() -> _Gathered:
        for f in list(price_futs.values()) + list(fin_futs.values()):
            f.cancel()
        return _Gathered([], {}, None, TEXT_UNAVAILABLE)

    for sym in req.symbols:
        key = _final_symbol(sym)
        cached = _cache_get(_STATS_CACHE, key) if key else None
        if cached is not None:
            resolved[sym] = cached
            if cached.instrument_type != "ETF":
                _submit_income(sym, cached.symbol)
            continue
        fut = _submit(_PRICE_POOL, _PRICE_SLOTS, _fetch_symbol_stats, sym)
        if fut is None:
            return _give_up()
        price_futs[sym] = fut
        if key is not None:
            _submit_income(sym, key)  # 和價格同時抓（ETF 的財報抓到也不會用）
    usd_rf: Optional[float] = None
    rf_fut: Optional[Future] = None
    with _state_lock:
        cached_rf = _RF_CACHE.get("USD")
    if cached_rf and time.time() - cached_rf[0] <= CACHE_TTL_S:
        usd_rf = cached_rf[1]
    elif req.currency == "USD" or not all(_is_tw(sym) for sym in req.symbols):
        rf_fut = _submit(_PRICE_POOL, _PRICE_SLOTS, _fetch_usd_rf)
    waiting = list(price_futs.values())
    if waiting:
        _done, pending = _wait_futures(waiting, timeout=max(0.0, deadline - time.monotonic()))
        if pending:
            with _state_lock:
                _cooldown_until = time.monotonic() + TIMEOUT_COOLDOWN_S
            logger.info("fcn price fetch timed out pending=%d", len(pending))
            return _give_up()
    for sym, fut in price_futs.items():
        try:
            actual, stats, problem = fut.result()
        except Exception as exc:
            logger.info("fcn price fetch failed error_type=%s", type(exc).__name__)
            return _give_up()
        if stats is None:
            for f in fin_futs.values():
                f.cancel()
            return _Gathered([], {}, None, _symbol_problem_text(sym, problem))
        resolved[sym] = stats
        if sym != actual:
            with _state_lock:
                _RESOLVED[sym] = actual
                while len(_RESOLVED) > CACHE_MAX:
                    _RESOLVED.popitem(last=False)
        _cache_put(_STATS_CACHE, actual, stats)
        if stats.instrument_type == "ETF":
            fut_income = fin_futs.pop(sym, None)
            if fut_income is not None:
                fut_income.cancel()
            net_income.pop(sym, None)
        elif sym not in fin_futs and sym not in net_income:
            _submit_income(sym, actual)  # 台股要先知道是 .TW 還是 .TWO
    if rf_fut is not None:
        _wait_futures([rf_fut], timeout=max(0.0, deadline - time.monotonic()))
        if not rf_fut.done():
            rf_fut.cancel()  # 利率沒到：之後回「拿不到美國公債利率」，不算價格逾時、不冷卻
            rf_fut = None
    if rf_fut is not None:
        try:
            usd_rf = rf_fut.result()
        except Exception as exc:
            logger.info("fcn rf fetch failed error_type=%s", type(exc).__name__)
            usd_rf = None
        if usd_rf is not None:
            with _state_lock:
                _RF_CACHE["USD"] = (time.time(), usd_rf)
    if fin_futs:
        _wait_futures(list(fin_futs.values()), timeout=max(0.0, deadline - time.monotonic()))
        for sym, fut in fin_futs.items():
            if not fut.done():
                fut.cancel()
                net_income[sym] = None
                continue
            try:
                values = fut.result()
            except Exception as exc:
                logger.info("fcn income fetch failed error_type=%s", type(exc).__name__)
                values = None
            net_income[sym] = values
            if values is not None:
                _cache_put(_FIN_CACHE, resolved[sym].symbol, (time.time(), values))
    return _Gathered([resolved[s] for s in req.symbols], net_income, usd_rf)


def _symbol_problem_text(symbol: str, problem: str) -> str:
    shown = symbol if _SYMBOL_FETCH_RE.fullmatch(symbol) else "這檔"
    if problem == "type":
        return f"FCN 評估：{shown} 不是股票或 ETF"
    if problem == "currency":
        return f"FCN 評估：目前只支援台股和美股（{shown}）"
    if problem == "stale":
        return f"FCN 評估：{shown} 的股價資料太舊，請稍後再試"
    return f"FCN 評估：暫時拿不到「{shown}」的股價資料，請確認代號或稍後再試"


# ── 模擬 ────────────────────────────────────────────────────────────────────
def _correlation(stats: list[TickerStats]) -> tuple[np.ndarray, bool]:
    """同市場用日報酬、跨市場用週報酬（收盤時間不同步）；資料不足的配對當 0。"""
    import pandas as pd

    k = len(stats)
    series = [pd.Series(s.closes, index=pd.to_datetime(list(s.dates))) for s in stats]
    corr = np.eye(k)
    note = False
    for i in range(k):
        for j in range(i + 1, k):
            if stats[i].market == stats[j].market:
                a = np.log(series[i]).diff().dropna()
                b = np.log(series[j]).diff().dropna()
                minimum = DAILY_OVERLAP_MIN
            else:
                a = np.log(series[i].resample("W-FRI").last().dropna()).diff().dropna()
                b = np.log(series[j].resample("W-FRI").last().dropna()).diff().dropna()
                minimum = WEEKLY_OVERLAP_MIN
            joined = pd.concat([a, b], axis=1, join="inner").dropna()
            value = float(joined.corr().iloc[0, 1]) if len(joined) >= minimum else float("nan")
            if not math.isfinite(value):
                value, note = 0.0, True
            corr[i, j] = corr[j, i] = max(-0.99, min(0.99, value))
    eigvals, eigvecs = np.linalg.eigh(corr)
    fixed = eigvecs @ np.diag(np.clip(eigvals, 1e-6, None)) @ eigvecs.T
    d = np.sqrt(np.diag(fixed))
    return fixed / np.outer(d, d), note


def simulate(
    stats: list[TickerStats],
    terms: Terms,
    rf_note: float,
    rf_market: dict[str, float],
    *,
    n_paths: Optional[int] = None,
    seed: int = SEED,
) -> SimResult:
    """一般 FCN 的風險中立模擬，回傳合理年利率與被接股機率（依代號排序，排列不變）。"""
    order = sorted(range(len(stats)), key=lambda i: stats[i].symbol)
    ordered = [stats[i] for i in order]
    k = len(ordered)
    corr, note = _correlation(ordered)
    chol = np.linalg.cholesky(corr)
    dt = 1.0 / DAYS_PER_YEAR
    sigma = np.array([s.vol for s in ordered])
    rates = np.array([rf_market.get(s.market, rf_note) for s in ordered])
    q = np.array([s.div_yield for s in ordered])
    drift = (rates - q - 0.5 * sigma**2) * dt
    shock = sigma * math.sqrt(dt)
    n_paths = n_paths or N_PATHS
    rng = np.random.default_rng(seed)
    log_s = np.zeros((n_paths, k))
    alive = np.ones(n_paths, dtype=bool)
    knocked_in = np.zeros(n_paths, dtype=bool)
    breached = np.zeros((n_paths, k), dtype=bool)
    coupon_pv = np.zeros(n_paths)
    principal_pv = np.zeros(n_paths)
    days = terms.months * OBS_DAYS
    log_ki = math.log(terms.ki)
    log_ko = math.log(terms.ko)
    for day in range(1, days + 1):
        z = rng.standard_normal((n_paths, k)) @ chol.T
        log_s[alive] += drift + shock * z[alive]
        below = log_s <= log_ki
        breached |= below & alive[:, None]
        if not terms.eki:
            knocked_in |= alive & below.any(axis=1)
        if day % OBS_DAYS == 0:
            disc = math.exp(-rf_note * day * dt)
            coupon_pv[alive] += disc
            if day < days and day // OBS_DAYS > terms.lockout_months:
                out = alive & (log_s >= log_ko).all(axis=1)
                principal_pv[out] = disc
                alive &= ~out
    disc_end = math.exp(-rf_note * days * dt)
    final = np.exp(log_s)
    worst = final.min(axis=1)
    if terms.eki:
        knocked_in = alive & (worst <= terms.ki)
    convert = alive & knocked_in & (worst < terms.strike)
    principal_pv[alive & ~convert] = disc_end
    principal_pv[convert] = disc_end * worst[convert] / terms.strike
    mean_coupon = float(coupon_pv.mean())
    fair = (1.0 - float(principal_pv.mean())) * 12.0 / mean_coupon if mean_coupon > 0 else float("nan")
    worst_idx = final[convert].argmin(axis=1) if convert.any() else np.array([], dtype=int)
    convert_sorted = np.bincount(worst_idx, minlength=k)
    breach_sorted = breached.sum(axis=0)
    convert_counts = [0] * k
    breach_counts = [0] * k
    for pos, original in enumerate(order):
        convert_counts[original] = int(convert_sorted[pos])
        breach_counts[original] = int(breach_sorted[pos])
    return SimResult(
        fair=fair, p_convert=float(convert.mean()), convert_counts=tuple(convert_counts),
        breach_counts=tuple(breach_counts), corr_note=note,
    )


# ── 判斷 ────────────────────────────────────────────────────────────────────
GREEN, YELLOW, RED, GRAY = "green", "yellow", "red", "gray"
_LEVEL_TEXT = {GREEN: "🟢 高", YELLOW: "🟡 普通", RED: "🔴 偏低", GRAY: "⚪ 資料不足"}
_LEVEL_SHORT = {GREEN: "CP值高", YELLOW: "CP值普通", RED: "CP值偏低", GRAY: "資料不足"}
_LEVEL_COLOR = {GREEN: "#1B873F", YELLOW: "#B7791F", RED: "#C53030", GRAY: "#666666"}


@dataclass(frozen=True)
class Verdict:
    level: str
    e: float
    below_rf: bool
    downgraded: bool


def judge(rate: float, fair: float, rf: Optional[float], had_loss_any: bool) -> Verdict:
    values = (rate, fair, rf)
    if rf is None or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        return Verdict(GRAY, float("nan"), False, False)
    if rate <= rf:
        level, e, below = RED, 0.0, True
    else:
        e = (rate - rf) / max(fair - rf, MIN_PREMIUM)
        if not math.isfinite(e):
            return Verdict(GRAY, float("nan"), False, False)
        level = GREEN if e >= GREEN_E else YELLOW if e >= YELLOW_E else RED
        below = False
    downgraded = False
    if had_loss_any and level in (GREEN, YELLOW):
        level = YELLOW if level == GREEN else RED
        downgraded = True
    return Verdict(level, e, below, downgraded)


# ── 卡片 ────────────────────────────────────────────────────────────────────
def _is_tw(symbol: str) -> bool:
    return symbol.endswith((".TW", ".TWO")) or bool(re.fullmatch(r"\d{4,6}[A-Z]?", symbol))


def _display_name(symbol: str) -> str:
    """卡片上的名字：台股「台積電 2330」、美股「特斯拉 TSLA」；表外的只寫代號。"""
    base = symbol.split(".")[0]
    name = _TW_DISPLAY.get(base) if _is_tw(symbol) else _US_DISPLAY.get(symbol)
    return f"{name} {base}" if name else base


def _alt_name(symbol: str) -> str:
    """altText（通知、聊天列表）用短名：台股中文名、美股代號。"""
    base = symbol.split(".")[0]
    return (_TW_DISPLAY.get(base) or base) if _is_tw(symbol) else base


def _text(text: str, *, size: str = "lg", weight: str | None = None, color: str | None = None) -> dict:
    node = {"type": "text", "text": text, "size": size, "wrap": True}
    if weight:
        node["weight"] = weight
    if color:
        node["color"] = color
    return node


def _button(label: str, command: str) -> dict:
    return {
        "type": "button",
        "style": "primary",
        "height": "md",
        "action": {"type": "message", "label": label, "text": command},
    }


def _separator() -> dict:
    return {"type": "separator", "margin": "md"}


def _currency_label(req: FcnRequest, currency: str) -> str:
    name = "美元" if currency == "USD" else "台幣"
    return name if req.currency else f"{name}，照股票推測"


def _level_words(level: float) -> str:
    """0.7 → 「期初價的 70%」；1.0 → 「期初價」。"""
    return "期初價" if abs(level - 1.0) < 1e-9 else f"期初價的 {_fmt_pct(level)}%"


def _terms_line(terms: Terms, single: bool = False) -> str:
    def below(level: float) -> str:
        return "期初價以下" if abs(level - 1.0) < 1e-9 else f"期初價的 {_fmt_pct(level)}% 以下"

    def at(level: float) -> str:
        return "期初價" if abs(level - 1.0) < 1e-9 else f"期初價的 {_fmt_pct(level)}%"

    who = "" if single else "任一檔"
    when = f"到期那天{who}收盤" if terms.eki else f"{who}收盤"
    take = "接股" if single else "接最差那檔"
    ko_who = "回到" if single else "全部回到"
    ko = f"{ko_who}期初價以上" if abs(terms.ko - 1.0) < 1e-9 else f"{ko_who}{at(terms.ko)} 以上"
    line = (
        f"條件：{when}跌到{below(terms.ki)}（跌超過 {_fmt_pct(1.0 - terms.ki)}%）、"
        f"到期還在{below(terms.strike)}，就用{at(terms.strike)}{'' if terms.strike >= 1.0 - 1e-9 else ' '}{take}；每月看一次，{ko}就提前結束；"
        f"期限 {terms.months} 個月"
    )
    defaults = ["提前出場"] if not terms.ko_given else []
    if not terms.ki_given and not terms.strike_given:
        defaults.append("接股條件")
    elif not terms.ki_given:
        defaults.append("接股門檻")
    elif not terms.strike_given:
        defaults.append("接股價")
    if not terms.months_given:
        defaults.append("期限")
    if defaults:
        line += f"（{'、'.join(defaults)}沒寫，照預設）"
    if terms.lockout_months:
        line += f"；前 {terms.lockout_months} 個月不會提前結束（鎖定期）"
    if terms.memory_ko:
        line += "；記憶式提前出場照一般方式粗估"
    return line


def build_card(
    req: FcnRequest,
    stats: list[TickerStats],
    sim: SimResult,
    verdict: Verdict,
    rf: float,
    currency: str,
    net_income: dict[str, Optional[tuple[float, ...]]],
) -> tuple[dict, str]:
    """評估卡。net_income 以 request 裡的代號為 key（stats 和 req.symbols 同順序）。"""
    pairs = list(zip(req.symbols, stats))
    single = len(stats) == 1
    names = "、".join(_display_name(s.symbol) for s in stats)
    rf_name = "美國短期公債" if currency == "USD" else "台銀一年期定存"
    rate_s = _fmt_pct(req.rate)
    level = verdict.level
    losers = [
        _display_name(s.symbol) for sym, s in pairs
        if net_income.get(sym) and min(net_income[sym]) < 0
    ]
    body: list[dict] = [
        _text(f"{names}｜年利率 {rate_s}%（{_currency_label(req, currency)}）", weight="bold"),
        _text(f"CP值：{_LEVEL_TEXT[level]}", size="xl", weight="bold", color=_LEVEL_COLOR[level]),
    ]
    if level == GRAY:
        body.append(_text("資料不夠，先不給燈號"))
    elif verdict.below_rf:
        body.append(_text(f"年利率比{rf_name}（{_fmt_pct(rf)}%）還低"))
    else:
        these = "這檔" if single else "這幾檔"
        reason = f"照{these}股票的波動，合理年利率約 {_fmt_pct(sim.fair, 1)}%；這個 FCN 給 {rate_s}%"
        if verdict.downgraded and losers:
            reason += f"；{'、'.join(losers)} 近四季有虧損，所以降一級"
        body.append(_text(reason))
    body.append(_separator())
    diff = req.rate - rf
    digits = 2 if abs(diff) < 0.01 else 1
    word = "多" if diff >= 0 else "低"
    body.append(_text(
        f"利率：年利率 {rate_s}%，比{rf_name}（{_fmt_pct(rf)}%）{word} {_fmt_pct(abs(diff), digits)} 個百分點"
    ))
    body.append(_text("漲回期初價會提前結束，實際領到的通常比較少", size="md", color="#555555"))
    vol_idx = max(range(len(stats)), key=lambda i: stats[i].vol)
    move = _fmt_pct(stats[vol_idx].vol / math.sqrt(12), 0)
    biggest = _display_name(stats[vol_idx].symbol)
    body.append(_text(
        f"波動：{biggest} 一個月漲跌約 {move}% 很常見" if single
        else f"波動：{biggest} 最大，一個月漲跌約 {move}% 很常見"
    ))
    if sim.p_convert >= 0.01:
        body.append(_text(
            f"{req.terms.months} 個月後被接股的機率約 {_fmt_pct(sim.p_convert, 0)}%（照近一年波動粗估，寧可估高）",
            size="md", color="#555555",
        ))
    etfs = [_display_name(s.symbol) for s in stats if s.instrument_type == "ETF"]
    companies = [(sym, s) for sym, s in pairs if s.instrument_type != "ETF"]
    unknown = [_display_name(s.symbol) for sym, s in companies if net_income.get(sym) is None]
    if losers:
        tail = "（降一級）" if verdict.downgraded else ""
        body.append(_text(f"公司：{'、'.join(losers)} 近四季有虧損，被接到比較難抱{tail}"))
    elif companies and not unknown:
        body.append(_text("公司：近四季都有賺錢" if len(companies) == 1 else f"公司：{len(companies)} 檔近四季都有賺錢"))
    if unknown:
        body.append(_text(f"公司：{'、'.join(unknown)} 查不到完整的近四季獲利", size="md", color="#555555"))
    if etfs:
        body.append(_text(f"公司：{'、'.join(etfs)} 是 ETF，不看獲利", size="md", color="#555555"))
    body.append(_separator())
    if sim.p_convert < 0.01:
        body.append(_text("照近一年的波動，到期被接股的機率很低"))
    else:
        counts = sim.convert_counts
        rank = {sym: r for r, sym in enumerate(sorted(s.symbol for s in stats))}
        top = max(range(len(stats)), key=lambda i: (counts[i], sim.breach_counts[i], -rank[stats[i].symbol]))
        who = _display_name(stats[top].symbol)
        if req.terms.strike < 1.0:
            drop = 1.0 - req.terms.strike
            ask = f"願意用跌 {_fmt_pct(drop, 0 if abs(drop * 100 - round(drop * 100)) < 1e-9 else 1)}% 的價格買來長期抱著嗎？"
        else:
            ask = "願意用期初價買來長期抱著嗎？"
        lead = f"如果被接，拿到的是 {who}：" if single else f"最可能被接的是 {who}："
        body.append(_text(f"{lead}{ask}不願意的話，這個 FCN 就不適合", weight="bold"))
    footer: list[dict] = [_text(_terms_line(req.terms, single), size="md", color="#555555")]
    if req.terms.strike < 1.0:
        footer.append(_text("如果條件書是用期初價接股，風險會大很多", size="sm", color="#555555"))
    if sim.corr_note:
        footer.append(_text("有幾檔一起漲跌的資料不夠，當成彼此無關", size="sm", color="#555555"))
    last = max(s.last_date for s in stats)
    footer.append(_text(
        f"依據：近一年股價（Yahoo）、{rf_name}｜資料到 {last.month}/{last.day}", size="sm", color="#555555"
    ))
    footer.append(_text("粗估，以銀行條件書為準；只供參考", size="sm", color="#555555"))
    footer.append(_button("🔄 用今天的資料重算", to_command(req)))
    card = {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                _text("FCN 評估", size="xl", weight="bold"),
                _text("FCN＝銀行的固定配息商品：每月領息，跌太多要接股票", size="sm", color="#666666"),
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer},
    }
    alt = f"FCN 評估：{'、'.join(_alt_name(s.symbol) for s in stats)}｜{_LEVEL_SHORT[level]}"
    return card, alt


def build_confirm_card(req: FcnRequest, unread_pcts: list[str]) -> tuple[dict, str]:
    t = req.terms

    def mark(given: bool) -> str:
        return "" if given else "（預設）"

    body: list[dict] = [
        _text(f"股票：{'、'.join(_display_name(s) for s in req.symbols)}", weight="bold"),
        _text(f"年利率：{_fmt_pct(req.rate)}%"),
        _text(f"期限：{t.months} 個月{'' if t.months_given else '（沒寫，照預設）'}"),
        _text(f"提前出場（KO）：{_level_words(t.ko)}{mark(t.ko_given)}", size="md"),
        _text(
            f"接股門檻（{'EKI，只看到期那天' if t.eki else 'KI'}）：{_level_words(t.ki)}"
            f"{mark(t.ki_given)}",
            size="md",
        ),
        _text(f"接股價：{_level_words(t.strike)}{mark(t.strike_given)}", size="md"),
    ]
    if t.lockout_months:
        body.append(_text(f"鎖定期：前 {t.lockout_months} 個月不會提前結束", size="md"))
    if req.currency:
        body.append(_text(f"計價：{'美元' if req.currency == 'USD' else '台幣'}", size="md"))
    if t.memory_ko:
        body.append(_text("記憶式提前出場：照一般方式粗估", size="md", color="#555555"))
    shown = "、".join(s for s in (_sanitize(p.rstrip("%")) for p in unread_pcts) if s)
    if shown:
        body.append(_text(f"沒讀懂的數字：{shown}（單位 %）", size="md", color="#555555"))
    card = {
        "type": "bubble",
        "size": "giga",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [_text("FCN 評估：我讀到這些條件", size="xl", weight="bold")],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                _button("✅ 用這組評估", to_command(req)),
                _text("不對的話照這個格式自己打：/FCN 股票… 年利率X%", size="sm", color="#555555"),
            ],
        },
    }
    short = "、".join(_alt_name(s) for s in req.symbols)
    alt = f"FCN 評估：我讀到 {short}｜年利率 {_fmt_pct(req.rate)}%，點按鈕評估"
    return card, alt


# ── 入口 ────────────────────────────────────────────────────────────────────
def _evaluate(req: FcnRequest, deadline: Optional[float]) -> FcnReply:
    budget = DATA_BUDGET_S
    if deadline is not None:
        budget = min(budget, deadline - time.monotonic() - 3.0)
    gathered = _gather(req, budget)
    if gathered.problem:
        return FcnReply(text=gathered.problem)
    stats = gathered.stats
    markets = {s.market for s in stats}
    currency = req.currency or ("TWD" if markets == {"TW"} else "USD")
    if gathered.usd_rf is None and ("US" in markets or currency == "USD"):
        return FcnReply(text=TEXT_NO_USD_RF)
    twd_rf = _twd_rf()
    rf_note = gathered.usd_rf if currency == "USD" else twd_rf
    rf_market = {"TW": twd_rf}
    if gathered.usd_rf is not None:
        rf_market["US"] = gathered.usd_rf
    sim_key = (req.effective(), tuple((s.symbol, s.fetched_at) for s in stats), rf_note, tuple(sorted(rf_market.items())))
    with _state_lock:
        sim = _SIM_CACHE.get(sim_key)
    if sim is None:
        sim = simulate(stats, req.terms, rf_note, rf_market)
        with _state_lock:
            _SIM_CACHE[sim_key] = sim
            while len(_SIM_CACHE) > CACHE_MAX:
                _SIM_CACHE.popitem(last=False)
    had_loss = any(
        values is not None and min(values) < 0 for values in gathered.net_income.values()
    )
    verdict = judge(req.rate, sim.fair, rf_note, had_loss)
    card, alt = build_card(req, stats, sim, verdict, rf_note, currency, gathered.net_income)
    return FcnReply(flex=card, alt_text=alt)


def handle(
    body: str,
    *,
    quoted_text: Optional[str] = None,
    quoted_is_bot: bool = False,
    deadline: Optional[float] = None,
) -> FcnReply:
    """`/FCN` 指令後面的字（body）＋可選的引用文字 → 回覆（文字或卡片）。"""
    text = _normalize(body)
    if len(text) > BODY_MAX_CHARS:
        return FcnReply(text=TEXT_TOO_LONG)
    has_body = bool(_FILLER_RE.sub("", text).strip())
    strict_problem: Optional[_Problem] = None
    if has_body:
        req, strict_problem = parse_strict(text)
        if req is not None:
            return _evaluate(req, deadline)
        if strict_problem.kind in ("conflict", "unsupported", "too_many"):
            return FcnReply(text=strict_problem.text)
    if quoted_text is not None:
        if quoted_is_bot:
            return FcnReply(text=TEXT_QUOTED_BOT)
        quoted = _normalize(quoted_text)
        if not quoted:
            return FcnReply(text=TEXT_QUOTED_MISSING)
        if _MEDIA_PLACEHOLDER_RE.fullmatch(quoted):
            return FcnReply(text=TEXT_QUOTED_MEDIA)
        if len(quoted) > QUOTE_MAX_CHARS:
            return FcnReply(text=TEXT_TOO_LONG)
        combined = f"{text}\n{quoted}" if has_body else quoted
        req, problem, unread = extract_lenient(combined)
        if problem:
            return FcnReply(text=problem.text)
        if req is None:
            return FcnReply(text=TEXT_QUOTED_NOTHING)
        card, alt = build_confirm_card(req, unread)
        return FcnReply(flex=card, alt_text=alt)
    if not has_body:
        return FcnReply(text=USAGE_TEXT)
    req, problem, unread = extract_lenient(text)
    if problem and problem.kind != "unread":
        return FcnReply(text=problem.text)
    if req is not None:
        card, alt = build_confirm_card(req, unread)
        return FcnReply(flex=card, alt_text=alt)
    return FcnReply(text=strict_problem.text if strict_problem else TEXT_NO_SYMBOL)
