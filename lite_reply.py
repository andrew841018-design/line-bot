"""Lite reply 模式 — Gemini 配額爆時的 fallback（2026-05-08 建立，2026-05-03 重構）。

架構：兩階段 dispatch — 「事實查詢寫死」+「自由問答走 local LLM」+「規則式 fallback」。

Stage 1：寫死 deterministic handlers（這些事實查詢 LLM 不會比較準）
  - YouTube oEmbed
  - 股票即時報價 (yfinance)
  - 匯率 (yfinance forex)
  - 簡單計算 (eval)
  - 時間 / 日期 (datetime)
  - 倒數計時 / 日期距離 (datetime delta)
  - 單位換算
  - 維基百科 API

Stage 2：local LLM 生成式（自由問答、閒聊、解釋類）
  - 原本寫死的「___ 是什麼」「為什麼...」走這個
  - 失敗（沒裝 / 出錯）graceful degrade 到 Stage 3

Stage 3：規則式 fallback（最後 safety net，避免完全沒回應）
  - URL 摘要（BeautifulSoup title + meta）
  - 天氣（CWA F-C0032-001）
  - Google 首頁 snippet（最不穩，最後一擲）

設計原則：
- 失敗一律 silent（None），caller 自己決定要不要回 fallback 訊息
- 每個 handler 有 timeout 防卡死
- 不接受太長輸入（> 500 字直接拒絕，避免被 inject）
- local LLM 不存在時不 import error，graceful degrade
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

_LOCAL_LLM_OPINION_SYSTEM_PROMPT = """你是 LINE 群組助理咪寶。遇到影片、文章、案例、健康做法、投資策略、生活方法等值得評論的分享時，不可以只附和或摘要。

回覆必須使用繁體中文，且固定包含：
1. 第一句直接給你的核心判斷，不要 echo 使用者。
2. 「正方：」列 1-2 點支持或合理之處。
3. 「反方：」列 1-2 點風險、限制或需要查證之處。
4. 「整合：」給統一見解與具體建議。

你是 local fallback，通常沒有即時查證能力；不要假裝有來源或數據。沒有來源時就用「需要查證」或「不能只憑影片判斷」誠實標註。

請先回顧使用者提供的原始素材（含 URL 抓到的影片標題、描述、字幕或引用段落）中能讀到的主張，再針對那個主張回答。不能只回「很鼓舞人心 / 很有道理」等泛泛情緒句。若素材不足，先說明「目前抓不到原始主張」並要求補充原文。控制在 180-320 個中文字。
請只使用你看到的素材做推理：若證據缺漏，請明確點出缺哪一段，而不是替代為空泛結論。"""

_LITE_OPINION_TOPIC_HINTS = (
    "影片", "YouTube", "youtube", "影片標題", "youtube 影片", "故事",
    "人物", "角色", "個案", "案例", "新聞", "報導", "貼文", "研究", "論文",
    "方法", "做法", "策略", "觀點", "心得", "健康", "養生", "人生", "財富自由",
    "貧窮", "致富", "努力", "上進", "小知足", "逆襲", "手工皂", "商業皂",
    "化學成分", "皮膚", "投資", "理財",
)
_LITE_OPINION_SIGNAL_HINTS = (
    "分享", "分享了", "看起來", "很實用", "很有趣", "可以避免", "更友善",
    "值得", "值得學習", "值得借鏡", "值得參考", "值得反思", "值得警惕",
    "可參考", "啟發", "有感", "有感觸", "鼓舞", "鼓舞人心", "通過", "出發",
    "貢獻", "努力", "起點", "逆轉", "突破", "比較", "值得",
    "比較", "值得", "好處", "壞處", "風險", "建議", "主張", "認為",
)
_LITE_PRO_MARKERS = (
    "正方", "同意的部分", "支持的部分", "好的部分", "支持理由", "贊成",
)
_LITE_CON_MARKERS = (
    "反方", "反對的部分", "質疑的部分", "壞的部分", "反對理由", "風險",
)
_LITE_SUMMARY_MARKERS = (
    "整合", "綜合", "結論", "統一見解", "我的看法", "最終建議", "判斷",
)


def _context_blobs_for_local_opinion(context: list | None, max_chars: int = 1200) -> str:
    if not context:
        return ""
    parts: list[str] = []
    consumed = 0
    # 只看近期對話，避免把整段歷史誤判；預設 1200 字為夠用上限。
    for item in reversed(context[-6:]):
        text: str | None = None
        if isinstance(item, str):
            text = item
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            candidate = item[1]
            if isinstance(candidate, str):
                text = candidate
        elif isinstance(item, dict):
            for key in ("text", "content", "summary", "message"):
                candidate = item.get(key)
                if isinstance(candidate, str):
                    text = candidate
                    break
        if not text:
            continue
        clean = text.strip()
        if not clean:
            continue
        remain = max_chars - consumed
        if remain <= 0:
            break
        parts.append(clean[:remain])
        consumed += len(clean[:remain])
    if not parts:
        return ""
    return "\n".join(reversed(parts))


def _opinion_context_contains_material(blob: str) -> bool:
    """判斷 blob 是否有足夠素材可支撐正反方論述（避免盲目抓網路）。"""
    if not blob:
        return False
    if len(blob) < 120:
        return False
    material_markers = (
        "標題：",
        "上傳者：",
        "作者：",
        "描述：",
        "觀點",
        "論點",
        "研究",
        "資料",
        "字幕內容",
        "--- 影片資訊",
        "影片資訊",
    )
    return any(m in blob for m in material_markers)


def _google_search_snippet_raw(query: str) -> str | None:
    """不預設要求問句詞的 Google snippet 抓取，作為 opinion fallback 補充資料。"""
    query = (query or "").strip()
    if not query or len(query) < 12:
        return None
    if len(query) > 140:
        query = query[:140]
    try:
        url = f"https://www.google.com/search?q={quote_plus(query)}&hl=zh-TW"
        resp = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "zh-TW,zh;q=0.9"},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for cls in ("VwiC3b", "lEBKkf", "BNeawe", "yXK7lf", "MUxGbd"):
            el = soup.find(class_=cls)
            if el:
                snippet = el.get_text(" ", strip=True)[:300]
                if snippet and len(snippet) > 20:
                    return (
                        f"🔍 Google 搜尋片段：\n"
                        f"{snippet}\n\n"
                        f"完整搜尋：{url}"
                    )
        return None
    except Exception as e:
        logger.info("_google_search_snippet_raw failed: %s", e)
        # fallthrough to duckduckgo fallback

    # 如果 Google 版面無 snippet（常見反爬），退回 DuckDuckGo HTML 搜尋片段。
    return _duckduckgo_search_snippet_raw(query)


def _duckduckgo_search_snippet_raw(query: str) -> str | None:
    query = (query or "").strip()
    if not query or len(query) < 12:
        return None
    if len(query) > 140:
        query = query[:140]
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        resp = requests.get(
            url,
            headers={"User-Agent": _UA, "Accept-Language": "zh-TW,zh;q=0.9"},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for cls in ("result__snippet", "result__body", "result__a"):
            el = soup.find(class_=cls)
            if not el:
                continue
            snippet = el.get_text(" ", strip=True)[:300]
            if snippet and len(snippet) > 20:
                return (
                    f"🔍 DuckDuckGo 搜尋片段：\n"
                    f"{snippet}\n\n"
                    f"完整搜尋：{url}"
                )
        return None
    except Exception as e:
        logger.info("_duckduckgo_search_snippet_raw failed: %s", e)
        return None


def _collect_opinion_reference_context(text: str, context: list | None = None) -> str | None:
    """缺素材時先抓一段網路 snippets 作為補充，避免只講空泛情緒。"""
    blob = _context_blobs_for_local_opinion(context)
    if _opinion_context_contains_material(blob):
        return None
    # 直接用去除 URL 後的文字下 keyword 搜尋
    query = re.sub(r"https?://\S+", "", (text or "")).strip()
    if len(query) < 12:
        return None
    return _google_search_snippet_raw(query)


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
        out.append("\n\n（網頁摘要）")
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
        out += "\n\n（來源：維基百科）"
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
            "（來源：CWA F-C0032-001 公開資料）"
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
            "（來源：oEmbed metadata，無內容摘要）"
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
                        f"🔍 Google 搜尋片段：\n"
                        f"{snippet}\n\n"
                        f"完整搜尋：{url}"
                    )
        return None
    except Exception as e:
        logger.info("_google_search_snippet failed: %s", e)
        return None


# ── handler wrappers（把 main router 內 inline 邏輯包成獨立 _try_xxx）─────


def _try_youtube_info(text: str) -> str | None:
    """YouTube URL → oEmbed 拿 title + 作者。"""
    return _youtube_info(text)


def _try_url_summary(text: str) -> str | None:
    """一般 URL → BeautifulSoup 抓 title + meta。"""
    urls = _URL_RE.findall(text)
    if not urls:
        return None
    return _summarize_url(urls[0])


def _try_stock(text: str, context: list | None = None) -> str | None:
    """股票 ticker / 中文股名 → yfinance 即時。"""
    taiex_technical = stock_quote.get_taiex_month_line_text(text)
    if taiex_technical:
        return f"{taiex_technical}\n\n（來源：公開報價/日線）"
    quotes = stock_quote.get_contextual_quotes_text(text, context=context)
    if quotes:
        return f"{quotes}\n\n（來源：公開報價）"
    return None


def _try_time_date(text: str) -> str | None:
    """時間 / 日期 → datetime."""
    if any(k in text for k in _TIME_KEYWORDS):
        return _now_time()
    if any(k in text for k in _DATE_KEYWORDS):
        return _today_date()
    return None


def _try_wiki_lookup(text: str) -> str | None:
    """『___ 是什麼』『什麼是 ___』『解釋一下 ___』→ 中文維基。"""
    query = _extract_wiki_query(text)
    if not query:
        return None
    return _wiki_summary(query)


def _try_weather(text: str) -> str | None:
    """天氣 → CWA F-C0032-001。"""
    if not any(k in text for k in _WEATHER_KEYWORDS):
        return None
    return _weather_taiwan(text)


def _try_google_snippet(text: str) -> str | None:
    """通用問句 → Google 首頁 snippet（最不穩，最後一擲）。"""
    if not any(k in text for k in ("是什麼", "什麼是", "為什麼", "怎麼", "如何", "?", "？")):
        return None
    return _google_search_snippet(text.rstrip("?？"))


# ── 生成式 helper（local LLM）───────────────────────────────────────────────


def _requires_lite_opinion_structure(text: str, context: list | None = None) -> bool:
    """影片/文章/案例等分享型內容，local fallback 也必須給正反方與整合見解。"""
    t = (text or "").strip()
    context_blob = _context_blobs_for_local_opinion(context)
    combined = f"{t}\n{context_blob}".strip()
    if len(combined) < 12:
        return False
    try:
        from gemini_client import _NEWS_CASE_TOPIC_HINTS
        professional_hints = tuple(_NEWS_CASE_TOPIC_HINTS)
    except Exception:
        professional_hints = ()
    if any(h in combined for h in professional_hints):
        return True
    has_topic = any(h in combined for h in _LITE_OPINION_TOPIC_HINTS)
    has_signal = any(h in combined for h in _LITE_OPINION_SIGNAL_HINTS)
    return has_topic and has_signal


def _has_lite_opinion_structure(reply: str) -> bool:
    """Minimum local fallback shape: pro + con + integrated take."""
    r = (reply or "").strip()
    if not r:
        return False
    has_pro = any(m in r for m in _LITE_PRO_MARKERS)
    has_con = any(m in r for m in _LITE_CON_MARKERS)
    has_summary = any(m in r for m in _LITE_SUMMARY_MARKERS)
    return has_pro and has_con and has_summary


def _try_local_llm(text: str, context: list | None = None) -> str | None:
    """走 local LLM。失敗 / 不可用 回 None（graceful degrade）。

    本函式刻意 lazy import + 寬鬆例外處理：
    - local_llm 模組不存在 → ImportError → return None
    - local_llm 任何 runtime error → return None
    - 回應太短（< 6 chars）→ return None
    """
    try:
        from local_llm import chat as _llm_chat  # noqa: WPS433  (lazy import)
    except ImportError:
        return None
    except Exception:  # pragma: no cover - defensive
        return None

    try:
        kwargs = {}
        if _requires_lite_opinion_structure(text, context=context):
            material_blob = _context_blobs_for_local_opinion(context)
            has_material = _opinion_context_contains_material(material_blob)
            evidence = None if has_material else _collect_opinion_reference_context(text, context=context)
            if not has_material and not evidence:
                return None
            resolved_context: list = list(context) if context else []
            if evidence:
                resolved_context.append(("research", f"補充資料（可能可作為論證依據）：\n{evidence}"))
            kwargs = {
                "system_prompt": _LOCAL_LLM_OPINION_SYSTEM_PROMPT,
                "max_tokens": 700,
            }
            context = resolved_context
        response = _llm_chat(text, context=context, **kwargs)
    except Exception as e:
        logger.info("_try_local_llm runtime error: %s", e)
        return None

    if response and isinstance(response, str) and len(response.strip()) > 5:
        return response.strip()
    return None


# ── 主路由 ──────────────────────────────────────────────────────────────────


# Stage 1：寫死 deterministic handlers（事實查詢，LLM 不會比較準）
_STAGE1_HANDLERS = (
    _try_youtube_info,
    _try_countdown,
    _try_stock,
    _try_forex,
    _try_calculate,
    _try_time_date,
    _try_unit_convert,
    _try_wiki_lookup,
)

# Stage 3：規則式 fallback handlers（local LLM 失敗才走）
_STAGE3_HANDLERS = (
    _try_url_summary,
    _try_weather,
    _try_google_snippet,
)


def _intent_to_handler(intent: str):
    """chinese_nlp intent → lite_reply handler mapping。"""
    mapping = {
        "stock": _try_stock,
        "weather": _try_weather,
        "datetime": _try_time_date,
        "translation": None,  # 沒對應 stage1 handler，走預設
        "transport": None,
        "image_gen": None,    # 由 main.py 處理，lite_reply 不接
    }
    return mapping.get(intent)


def _call_handler(handler, text: str, context: list | None = None):
    if handler is _try_stock:
        return handler(text, context=context)
    return handler(text)


def _passes_helpfulness_gate(reply: str, text: str, context: list | None = None) -> bool:
    """非確定性回覆（local LLM / search snippet）的有用性閘門。

    Andrew rule（feedback_bot_reply_helpful_or_defer）：substantive 題寧可不回，也不要回
    「restate 問題 +『應該妥善處理 / 明確確認』」這種沒具體內容的空泛 lecture。

    刻意**抑制偏向**：誤殺 borderline（→ 上層 defer 到 pending，訊息不丟）可接受，
    漏放空話才是大忌。只對 substantive 題（命中 _NEWS_CASE_TOPIC_HINTS，如房地產 / 投資 /
    醫療 / 法律）嚴管；閒聊 / 輕量回覆一律放行，不誤殺日常對話。
    """
    r = (reply or "").strip()
    if not r:
        return False
    if _requires_lite_opinion_structure(text, context=context) and not _has_lite_opinion_structure(r):
        return False
    # substantive = 金融 / 房產 / 法律 / 醫療等「沒 grounding 容易空話」的主題。
    # 既有 _NEWS_CASE_TOPIC_HINTS（投資 / 房地產 / 醫療…）字面 substring 比對抓不到
    # 「購屋款」這種說法（不含「房地產」四字），故補一組家庭常見高風險主題；
    # 且對 text 與 reply 都比對（問題沒帶關鍵詞、但答案露餡時仍能攔）。
    _SUBSTANTIVE_EXTRA = (
        "購屋", "買房", "賣房", "房貸", "頭期", "貸款", "出資", "過戶", "贈與",
        "繼承", "遺產", "報稅", "合約", "契約", "借款", "債務", "理賠",
        "手術", "診斷", "症狀", "副作用", "訴訟", "官司", "監護", "離婚",
    )
    blob = (text or "") + " " + r
    try:
        from gemini_client import _NEWS_CASE_TOPIC_HINTS
        hints = tuple(_NEWS_CASE_TOPIC_HINTS) + _SUBSTANTIVE_EXTRA
    except Exception:
        hints = _SUBSTANTIVE_EXTRA
    substantive = any(h in blob for h in hints)
    if not substantive:
        return True  # 閒聊 / 輕量 → 放行

    # 複用 gemini_client 既有品質檢查（echo opener / empty phrase blacklist）
    try:
        from gemini_client import _violates_quality
        bad, _reason = _violates_quality(r, text)
        if bad:
            return False
    except Exception:
        pass

    # 平台式空話啟發式：建議語氣 + 零具體（無數字）+ 偏短 → 視為空話抑制。
    # （刻意寬抑制：substantive 題的有用回覆通常帶具體數字 / 名稱 / 步驟。）
    _ADVICE_MARKERS = (
        "應該", "建議", "最好", "需要", "妥善", "謹慎", "注意", "確認",
        "尋求專業", "諮詢專業", "視情況", "依情況", "因人而異", "多加",
    )
    has_advice_modal = any(m in r for m in _ADVICE_MARKERS)
    has_specifics = bool(re.search(r"\d", r))  # 數字 / 年份 / 百分比 / 金額
    if has_advice_modal and not has_specifics and len(r) < 120:
        return False
    return True


def lite_reply(text: str, context: list | None = None) -> str | None:
    """總入口。回 str = 找到答案；回 None = 沒辦法（caller 決定要不要回 generic）。

    架構（2026-05-03 重構）：
      Stage 1 — 寫死 deterministic：股票/匯率/計算/時間/倒數/單位/維基/YouTube
                這些事實查詢規則式準確度遠高於 LLM。
      Stage 2 — local LLM 生成式：自由問答 / 解釋 / 閒聊。
                local_llm 沒裝 → graceful degrade 到 Stage 3。
      Stage 3 — 規則式 fallback：URL 摘要 / 天氣 / Google snippet。
                當作 safety net，避免完全沉默。
    """
    if not text:
        return None
    text = text.strip()
    if not text or len(text) > 500:
        return None

    # ─ Stage 0: chinese_nlp intent hint（純本機 jieba + rules，無 LLM call）─
    # 命中 intent → 優先跑對應 handler；未命中走預設順序
    try:
        import chinese_nlp
        intent_info = chinese_nlp.classify_intent(text)
        intent = intent_info.get("intent", "general") if intent_info else "general"
        if intent != "general":
            priority_handler = _intent_to_handler(intent)
            if priority_handler:
                try:
                    out = _call_handler(priority_handler, text, context=context)
                    if out:
                        return out
                except Exception as e:
                    logger.info("intent priority handler %s raised: %s", intent, e)
    except (ImportError, Exception) as e:
        logger.debug("chinese_nlp intent hint skipped: %s", e)

    # ─ Stage 1: 寫死 deterministic（事實查詢，LLM 不準）─
    for handler in _STAGE1_HANDLERS:
        try:
            out = _call_handler(handler, text, context=context)
        except Exception as e:  # pragma: no cover - defensive
            logger.info("Stage1 handler %s raised: %s", handler.__name__, e)
            continue
        if out:
            return out

    # ─ Stage 2: 生成式（local LLM；自由問答 / 解釋）─
    # 過 helpfulness gate：substantive 題的空泛 lecture 不送，往下 fall through 到
    # Stage 3 網搜；都不行則最終 return None → 上層存 pending（help-or-defer）。
    out = _try_local_llm(text, context=context)
    if out and _passes_helpfulness_gate(out, text, context=context):
        return out

    # ─ Stage 3: 規則式 fallback（最後 safety net）─
    for handler in _STAGE3_HANDLERS:
        try:
            out = handler(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.info("Stage3 handler %s raised: %s", handler.__name__, e)
            continue
        if not out:
            continue
        # _try_google_snippet 是非確定性 web snippet → 同樣過 gate；
        # _try_url_summary / _try_weather 是確定性事實 → 直接放行。
        if handler is _try_google_snippet and not _passes_helpfulness_gate(out, text, context=context):
            continue
        return out

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
