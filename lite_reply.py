"""Lite reply 模式 — Gemini 配額爆時的 fallback（2026-05-08 建立）。

設計：規則式 intent dispatch（Siri-like），不用 LLM token。
邏輯：input → intent classify → handler → 回應 or None。

Intents 涵蓋：
  1. URL 摘要         → BeautifulSoup 抓 title + description
  2. 股價查詢         → stock_quote（已有）
  3. 時間 / 日期      → datetime
  4. 簡單計算         → 安全 expression eval（只允許 + - * / ( )）
  5. 維基百科解釋     → MediaWiki API summary（中文版）
  6. 天氣（台灣）     → CWB API
  7. 其他（含閒聊）   → None（讓 main 層回 generic 訊息或沉默）

設計原則：
- 失敗一律 silent（None），caller 自己決定要不要回 fallback 訊息
- 每個 handler 有 timeout 防卡死
- 不接受太長輸入（> 200 字直接拒絕，避免被 inject）
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import quote_plus  # noqa: F401  (used by handlers)

import requests

import stock_quote

logger = logging.getLogger("lite_reply")

_HTTP_TIMEOUT = 5.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ── intent: URL 摘要 ──────────────────────────────────────────────────────────


_URL_RE = re.compile(r"https?://\S+")


def _summarize_url(url: str) -> str | None:
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"},
            timeout=_HTTP_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code != 200 or not resp.text:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        title = ""
        if soup.find("title"):
            title = soup.find("title").get_text(strip=True)[:120]
        desc = ""
        for meta_attr in (
            {"name": "description"},
            {"property": "og:description"},
            {"name": "twitter:description"},
        ):
            m = soup.find("meta", attrs=meta_attr)
            if m and m.get("content"):
                desc = m["content"].strip()[:300]
                break
        if not desc:
            body = soup.find("body")
            if body:
                desc = body.get_text(" ", strip=True)[:300]
        if not (title or desc):
            return None
        out = []
        if title:
            out.append(f"📰 {title}")
        if desc:
            out.append(f"\n{desc}")
        out.append("\n\n（lite mode：Gemini 配額用完，僅抓網頁摘要）")
        return "".join(out)
    except Exception as e:
        logger.info("_summarize_url failed (%s): %s", url, e)
        return None


# ── intent: 時間 / 日期 ─────────────────────────────────────────────────────


_TIME_KEYWORDS = (
    "現在幾點", "現在時間", "幾點了", "what time", "當前時間",
)
_DATE_KEYWORDS = (
    "今天日期", "今天幾月幾號", "今天幾號", "今天星期幾", "今天禮拜幾",
    "what's the date", "today's date",
)


def _now_time() -> str:
    now = datetime.now()
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    return f"⏰ 現在是 {now.strftime('%H:%M')}（{now.strftime('%Y-%m-%d')} 週{weekday}）"


def _today_date() -> str:
    now = datetime.now()
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    return f"📅 今天是 {now.strftime('%Y-%m-%d')}（週{weekday}）"


# ── intent: 簡單計算 ──────────────────────────────────────────────────────────


_CALC_RE = re.compile(r"^[\d\s+\-*/().,，。]+$")
_CALC_TRIGGER_RE = re.compile(r"^[\s]*[\d.()][\d\s+\-*/().,]*[\d.)][\s]*$")


def _try_calculate(text: str) -> str | None:
    """嘗試解 `123 + 456 * (2 - 1)` 這種簡單算式。"""
    text = text.strip().rstrip("?？=＝").replace("，", ",").replace("。", "")
    if not _CALC_TRIGGER_RE.match(text):
        return None
    if len(text) > 80:
        return None
    if any(c.isalpha() for c in text):
        return None
    # 只允許數字 + 運算符 + 括號，杜絕 eval 注入
    safe = re.sub(r"[\d+\-*/().\s,]", "", text)
    if safe:  # 有任何不允許字元
        return None
    try:
        # 限制 builtins 為空
        result = eval(text, {"__builtins__": {}}, {})  # noqa: S307
        return f"🧮 {text} = {result}"
    except Exception:
        return None


# ── intent: 維基百科查詢 ────────────────────────────────────────────────────


_WIKI_TRIGGERS = (
    "是什麼", "什麼是", "what is", "what's", "解釋一下", "介紹一下",
)


def _wiki_summary(query: str) -> str | None:
    """中文維基 API summary。query 太短或太長都拒。"""
    query = query.strip()
    if not (2 <= len(query) <= 30):
        return None
    try:
        url = f"https://zh.wikipedia.org/api/rest_v1/page/summary/{quote_plus(query)}"
        resp = requests.get(
            url,
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        title = data.get("title", "")
        extract = data.get("extract", "").strip()
        if not extract or len(extract) < 10:
            return None
        page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        out = f"📖 維基百科：{title}\n\n{extract[:400]}"
        if page_url:
            out += f"\n\n{page_url}"
        out += "\n\n（lite mode：Gemini 配額用完，從維基抓的）"
        return out
    except Exception as e:
        logger.info("_wiki_summary failed (%s): %s", query, e)
        return None


def _extract_wiki_query(text: str) -> str | None:
    """從『XXX 是什麼』『什麼是 XXX』『解釋一下 XXX』抽 XXX。"""
    text = text.strip().rstrip("?？")
    for pat in (
        r"^(.+?)\s*是什麼$",
        r"^什麼是\s*(.+)$",
        r"^解釋一下\s*(.+)$",
        r"^介紹一下\s*(.+)$",
        r"^what'?s?\s+(.+)$",
        r"^what\s+is\s+(.+)$",
    ):
        m = re.match(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


# ── intent: 天氣（台灣 CWA — 中央氣象署 開放 API）────────────────────────────


_WEATHER_KEYWORDS = ("天氣", "氣溫", "下雨嗎", "會下雨", "weather", "temperature")
_TW_CITIES = (
    "臺北市", "台北市", "新北市", "桃園市", "臺中市", "台中市", "臺南市", "台南市",
    "高雄市", "基隆市", "新竹市", "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
    "嘉義市", "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "台東縣",
    "澎湖縣", "金門縣", "連江縣",
)


def _weather_taiwan(text: str = "") -> str | None:
    """CWA F-C0032-001 36 小時天氣預報（公開資料無需 token）。

    從 text 抽縣市，找不到預設台北。
    """
    target_city = "臺北市"
    for city in _TW_CITIES:
        if city in text:
            target_city = city.replace("台", "臺")  # CWA 統一用「臺」
            break

    try:
        url = "https://opendata.cwa.gov.tw/fileapi/v1/opendataapi/F-C0032-001"
        params = {"format": "JSON"}
        resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        locs = data["cwaopendata"]["dataset"]["location"]
        match = None
        for loc in locs:
            if loc["locationName"] == target_city:
                match = loc
                break
        if not match:
            return None

        # 取近期 1 段（每段 12 小時）
        elements = {e["elementName"]: e for e in match["weatherElement"]}
        wx = elements.get("Wx", {}).get("time", [{}])[0]
        pop = elements.get("PoP", {}).get("time", [{}])[0]
        min_t = elements.get("MinT", {}).get("time", [{}])[0]
        max_t = elements.get("MaxT", {}).get("time", [{}])[0]
        ci = elements.get("CI", {}).get("time", [{}])[0]

        wx_desc = wx.get("parameter", {}).get("parameterName", "?")
        pop_pct = pop.get("parameter", {}).get("parameterName", "?")
        min_temp = min_t.get("parameter", {}).get("parameterName", "?")
        max_temp = max_t.get("parameter", {}).get("parameterName", "?")
        ci_desc = ci.get("parameter", {}).get("parameterName", "")
        start = wx.get("startTime", "")[:16].replace("T", " ")
        end = wx.get("endTime", "")[:16].replace("T", " ")

        return (
            f"🌤️ {target_city} {start} ~ {end}\n"
            f"  天氣：{wx_desc}\n"
            f"  降雨機率：{pop_pct}%\n"
            f"  氣溫：{min_temp}°C ~ {max_temp}°C\n"
            f"  舒適度：{ci_desc}\n\n"
            "（lite mode：CWA F-C0032-001 公開資料）"
        )
    except Exception as e:
        logger.info("_weather_taiwan failed: %s", e)
        return (
            "🌤️ 天氣抓取失敗\n"
            f"查 {target_city}：https://www.cwa.gov.tw/V8/C/W/County/index.html"
        )


# ── intent: 匯率 (frankfurter.app 免費)───────────────────────────────────────


_FOREX_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(美[元金]|USD|日[元圓]|JPY|人民幣|RMB|CNY|歐[元圓]|EUR|"
    r"英[鎊]|GBP|韓[元圜圓]|KRW|港[元幣]|HKD|加[幣元]|CAD|澳[幣元]|AUD)"
    r"[\s，,。\.]*"
    r"(?:換|兌|轉|to|是|=|等於|多少|幾|換成|兌換|換成多少|兌換成|換算成?)?"
    r"[\s，,。\.]*"
    r"(台[幣元]|TWD|美[元金]|USD|日[元圓]|JPY|人民幣|RMB|"
    r"CNY|歐[元圓]|EUR|英[鎊]|GBP|韓[元圜圓]|KRW|港[元幣]|HKD|加[幣元]|CAD|澳[幣元]|AUD)?"
)
_CURRENCY_MAP = {
    "美元": "USD", "美金": "USD", "USD": "USD",
    "日元": "JPY", "日圓": "JPY", "JPY": "JPY",
    "人民幣": "CNY", "RMB": "CNY", "CNY": "CNY",
    "歐元": "EUR", "歐圓": "EUR", "EUR": "EUR",
    "英鎊": "GBP", "GBP": "GBP",
    "韓元": "KRW", "韓圜": "KRW", "韓圓": "KRW", "KRW": "KRW",
    "港元": "HKD", "港幣": "HKD", "HKD": "HKD",
    "加幣": "CAD", "加元": "CAD", "CAD": "CAD",
    "澳幣": "AUD", "澳元": "AUD", "AUD": "AUD",
    "台幣": "TWD", "台元": "TWD", "TWD": "TWD",
}


def _try_forex(text: str) -> str | None:
    """『100 美金 = 多少台幣』『50 USD to JPY』。用 yfinance（支援 TWD）。"""
    m = _FOREX_RE.search(text)
    if not m:
        return None
    try:
        amount = float(m.group(1))
        from_cur_raw = m.group(2)
        to_cur_raw = m.group(3) or "TWD"  # 預設換台幣
        from_cur = None
        to_cur = None
        for k, v in _CURRENCY_MAP.items():
            if k in from_cur_raw:
                from_cur = v
                break
        for k, v in _CURRENCY_MAP.items():
            if k in to_cur_raw:
                to_cur = v
                break
        if not from_cur or not to_cur or from_cur == to_cur:
            return None

        # yfinance forex ticker pattern: USDTWD=X = 1 USD → X TWD
        import yfinance as yf
        ticker = f"{from_cur}{to_cur}=X"
        hist = yf.Ticker(ticker).history(period="2d", auto_adjust=False)
        if len(hist) == 0:
            return None
        rate = float(hist["Close"].iloc[-1])
        result = amount * rate
        last_date = hist.index[-1].strftime("%Y-%m-%d")
        return (
            f"💱 {amount:g} {from_cur} = {result:,.2f} {to_cur}\n"
            f"  （1 {from_cur} = {rate:.4f} {to_cur}，{last_date} yfinance）"
        )
    except Exception as e:
        logger.info("_try_forex failed: %s", e)
        return None


# ── intent: 倒數計時 / 日期距離 ──────────────────────────────────────────────


_COUNTDOWN_RE = re.compile(
    r"(?:離|距|還有|剩|to)\s*"
    r"(\d{1,4}[/\-月.]\d{1,2}[/\-日.]?\d{0,2}|"
    r"\d{1,2}\s*[/月]\s*\d{1,2}|"
    r"\d{1,2}號)"
    r".*?(?:還有|還剩|有|多少|幾天|days)"
)


def _try_countdown(text: str) -> str | None:
    """『離 6/15 還幾天』『距離 12 月 25 日多少天』。"""
    text = text.strip()
    # 簡單 regex 抓「日期 + 天數查詢」keyword
    if not any(k in text for k in ("還有", "還剩", "幾天", "多少天", "距離", "離")):
        return None
    if "天" not in text and "days" not in text.lower():
        return None

    # 抽日期
    patterns = [
        (r"(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})", lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (r"(\d{1,2})[/月](\d{1,2})", lambda m: (datetime.now().year, int(m.group(1)), int(m.group(2)))),
        (r"(\d{1,2})\s*月\s*(\d{1,2})", lambda m: (datetime.now().year, int(m.group(1)), int(m.group(2)))),
    ]
    target = None
    for pat, fn in patterns:
        m = re.search(pat, text)
        if m:
            try:
                y, mo, d = fn(m)
                target = datetime(y, mo, d).date()
                # 過去 → 推到下一年同日
                if target < datetime.now().date():
                    target = datetime(y + 1, mo, d).date()
                break
            except (ValueError, TypeError):
                continue
    if not target:
        return None
    delta = (target - datetime.now().date()).days
    if delta == 0:
        return f"🗓️ {target.strftime('%Y-%m-%d')} 就是**今天**！"
    elif delta == 1:
        return f"🗓️ 距離 {target.strftime('%Y-%m-%d')} 還有 **1 天**（明天）"
    return f"🗓️ 距離 {target.strftime('%Y-%m-%d')} 還有 **{delta} 天**"


# ── intent: 單位換算 ────────────────────────────────────────────────────────


_UNIT_CONVERSIONS = {
    # length (to meters)
    ("公里", "公尺"): 1000,
    ("公里", "米"): 1000,
    ("英里", "公里"): 1.60934,
    ("英呎", "公尺"): 0.3048,
    ("英尺", "公尺"): 0.3048,
    ("英寸", "公分"): 2.54,
    ("英吋", "公分"): 2.54,
    # weight
    ("公斤", "公克"): 1000,
    ("公斤", "克"): 1000,
    ("磅", "公斤"): 0.453592,
    ("斤", "公斤"): 0.6,  # 台斤
    # temperature handled separately
}


def _try_unit_convert(text: str) -> str | None:
    """『5 公里 = 幾公尺』『100 磅 是多少公斤』。"""
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(公里|公尺|米|英里|英[呎尺]|英[寸吋]|公斤|公克|克|磅|台?斤)\s*"
        r"(?:換算?|=|是|等於|to)?\s*"
        r"(?:多少|幾|多大)?\s*(公里|公尺|米|英里|英[呎尺]|英[寸吋]|公斤|公克|克|磅|台?斤)?",
        text,
    )
    if not m:
        return None
    try:
        val = float(m.group(1))
        from_u = m.group(2)
        to_u = m.group(3) or ""
    except (ValueError, IndexError):
        return None

    # 找對應 conversion
    for (a, b), factor in _UNIT_CONVERSIONS.items():
        if from_u == a and (to_u == b or not to_u):
            result = val * factor
            return f"📏 {val:g} {a} = {result:g} {b}"
        if from_u == b and (to_u == a or not to_u):
            result = val / factor
            return f"📏 {val:g} {b} = {result:g} {a}"
    return None


# ── intent: YouTube 影片資訊 (oEmbed)────────────────────────────────────────


_YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})"
)


def _youtube_info(text: str) -> str | None:
    """YouTube oEmbed API 拿影片標題 + 作者（不需要 API key）。"""
    m = _YOUTUBE_URL_RE.search(text)
    if not m:
        return None
    video_id = m.group(1)
    try:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        resp = requests.get(url, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return (
            f"🎬 {data.get('title', '?')}\n"
            f"  頻道：{data.get('author_name', '?')}\n"
            f"  https://youtu.be/{video_id}\n\n"
            "（lite mode：oEmbed metadata，無內容摘要）"
        )
    except Exception as e:
        logger.info("_youtube_info failed: %s", e)
        return None


# ── intent: Google search snippet（generic fallback，不穩） ──────────────────


def _google_search_snippet(query: str) -> str | None:
    """Google 首頁 scrape，抓第一個 result 的 snippet。

    不穩 — 容易被擋 IP / layout 改。當作「最後一擲」用。
    """
    if len(query) > 80:
        return None
    try:
        url = f"https://www.google.com/search?q={quote_plus(query)}&hl=zh-TW"
        from urllib.parse import quote_plus  # local import is_safe
        resp = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "zh-TW,zh;q=0.9"},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        # Google 不同 layout 試多個 class
        for cls in ("VwiC3b", "lEBKkf", "BNeawe", "yXK7lf", "MUxGbd"):
            el = soup.find(class_=cls)
            if el:
                snippet = el.get_text(" ", strip=True)[:300]
                if snippet and len(snippet) > 20:
                    return (
                        f"🔍 lite mode（Google 首頁 snippet）：\n"
                        f"{snippet}\n\n"
                        f"完整搜尋：{url}"
                    )
        return None
    except Exception as e:
        logger.info("_google_search_snippet failed: %s", e)
        return None


# ── 主路由 ──────────────────────────────────────────────────────────────────


def lite_reply(text: str) -> str | None:
    """總入口。回 str = 找到答案；回 None = 沒辦法（caller 決定要不要回 generic）。

    優先序（先具體後 generic）：
      1. YouTube URL → oEmbed 拿 title + 作者
      2. 一般 URL → BeautifulSoup 抓 title + meta
      3. 股票 ticker / 中文股名 → yfinance 即時
      4. 匯率換算 → frankfurter.app
      5. 簡單計算式 → eval safe
      6. 時間 / 日期 → datetime
      7. 倒數計時 / 日期距離 → datetime delta
      8. 單位換算 → 公里/公尺/磅/公斤 等
      9. 「___ 是什麼」 → 中文維基
      10. 天氣 → CWA F-C0032-001
      11. 其他知識性問題 → Google search snippet（不穩，最後一擲）
      12. 純閒聊 → None（沉默）
    """
    if not text:
        return None
    text = text.strip()
    if len(text) > 200:
        return None

    # 1. YouTube URL（先於一般 URL，因為 oEmbed 抓 metadata 比 scrape 乾淨）
    yt = _youtube_info(text)
    if yt:
        return yt

    # 2. 一般 URL
    urls = _URL_RE.findall(text)
    if urls:
        out = _summarize_url(urls[0])
        if out:
            return out

    # 3. 股票
    quotes = stock_quote.get_quotes_text(text)
    if quotes:
        return f"{quotes}\n\n（lite mode：Gemini 配額用完，純查 yfinance）"

    # 4. 匯率
    forex = _try_forex(text)
    if forex:
        return forex

    # 5. 簡單計算
    calc = _try_calculate(text)
    if calc:
        return calc

    # 6. 時間 / 日期
    if any(k in text for k in _TIME_KEYWORDS):
        return _now_time()
    if any(k in text for k in _DATE_KEYWORDS):
        return _today_date()

    # 7. 倒數計時
    countdown = _try_countdown(text)
    if countdown:
        return countdown

    # 8. 單位換算
    unit = _try_unit_convert(text)
    if unit:
        return unit

    # 9. 維基
    wiki_query = _extract_wiki_query(text)
    if wiki_query:
        out = _wiki_summary(wiki_query)
        if out:
            return out

    # 10. 天氣
    if any(k in text for k in _WEATHER_KEYWORDS):
        out = _weather_taiwan(text)
        if out:
            return out

    # 11. Generic Google snippet（最後一擲，不穩）
    if any(k in text for k in ("是什麼", "什麼是", "為什麼", "怎麼", "如何", "?", "？")):
        out = _google_search_snippet(text.rstrip("?？"))
        if out:
            return out

    # 12. 其他 → 沉默（避免亂答）
    return None


if __name__ == "__main__":
    # smoke test
    samples = [
        "https://example.com",
        "台積電 2330",
        "123 + 456",
        "現在幾點",
        "今天日期",
        "Python 是什麼",
        "什麼是 ETL",
        "今天天氣",
        "嗨你好",
        "為什麼天空是藍的",
    ]
    for s in samples:
        out = lite_reply(s)
        print(f"\n>>> {s}")
        print(out or "（None — 不會答）")
