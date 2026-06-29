"""Lite reply 模式 — 第二批 intent handlers（2026-05-03 建立）。

擴充 `lite_reply.py`，新增 5 個規則式 handler，當 Gemini 配額爆時提供額外資訊。

每個 handler signature:
    handler(text: str) -> str | None

回 str 表示找到答案、回 None 表示不適用 / 抓取失敗（一律 silent）。

設計原則同 lite_reply.py：
- 失敗 silent，回 None 不要拋例外
- HTTP 5s timeout 防卡死
- 重型 import（feedparser / xml 等）放 function 內 local import
- module level 只 import 標準庫 + requests + datetime + re + logger

預期被 lite_reply.lite_reply() 主路由 consume —
  router 後段插：
      for h in lite_intents_extra.EXTRA_HANDLERS:
          out = h(text)
          if out:
              return out
"""

from __future__ import annotations

import logging
import re

import requests

# 台灣政府網站常缺 Subject Key Identifier（SSL chain 不全），SSL 驗證會掛。
# 對這些網站走 verify=False，但要把 InsecureRequestWarning 關掉避免噴 log。
try:  # pragma: no cover - import side effect
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

logger = logging.getLogger("lite_intents_extra")

_HTTP_TIMEOUT = 5.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ── intent 1: 新聞（Google News RSS）────────────────────────────────────────


_NEWS_TRIGGERS = ("新聞", "最新消息", "news")
_NEWS_QUERY_PATTERNS = (
    r"^(.+?)\s*的新聞$",
    r"^最近\s*(.+?)$",
    r"^(.+?)\s*最新消息$",
    r"^(.+?)\s+news$",
    r"^news\s+(?:about\s+|on\s+)?(.+)$",
    r"^新聞\s*(.+)$",
)


def _extract_news_query(text: str) -> str | None:
    """從『X 的新聞』『最近 X』『news on Y』抽 query。"""
    text = text.strip().rstrip("?？。.")
    for pat in _NEWS_QUERY_PATTERNS:
        m = re.match(pat, text, re.IGNORECASE)
        if m:
            q = m.group(1).strip()
            if q and 1 < len(q) <= 40:
                return q
    # 純「新聞」/「最新消息」/「news」一個字 → 用「頭條」當 query
    if text.lower() in ("新聞", "最新消息", "news", "今日新聞", "頭條"):
        return "頭條"
    return None


def _try_news(text: str) -> str | None:
    """『台積電 的新聞』『最近 烏俄戰爭』『news on AI』。

    用 Google News RSS（無需 API key），取近期 5 則。
    format: 標題 + 來源 + URL
    """
    if not any(k in text.lower() for k in ("新聞", "最新消息", "news", "最近")):
        return None
    query = _extract_news_query(text)
    if not query:
        return None
    try:
        from urllib.parse import quote_plus
        import xml.etree.ElementTree as ET

        url = (
            f"https://news.google.com/rss/search?q={quote_plus(query)}"
            "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        )
        resp = requests.get(
            url,
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200 or not resp.text:
            return None
        root = ET.fromstring(resp.text)
        items = root.findall(".//item")
        if not items:
            return None

        lines = [f"📰 {query} 新聞（近期 5 則）"]
        for item in items[:5]:
            title_el = item.find("title")
            link_el = item.find("link")
            source_el = item.find("source")
            title = (title_el.text or "").strip() if title_el is not None else ""
            link = (link_el.text or "").strip() if link_el is not None else ""
            source = (source_el.text or "").strip() if source_el is not None else ""
            if not title:
                continue
            # 標題裡常含「 - 來源」尾巴，砍掉避免重複
            if source and title.endswith(f" - {source}"):
                title = title[: -(len(source) + 3)]
            lines.append(f"\n• {title[:80]}")
            if source:
                lines.append(f"  來源：{source}")
            if link:
                lines.append(f"  {link[:120]}")
        if len(lines) <= 1:
            return None
        lines.append("\n\n（來源：Google News RSS）")
        return "\n".join(lines)
    except Exception as e:
        logger.info("_try_news failed (%s): %s", query, e)
        return None


# ── intent 2: 翻譯（Google Translate 免費端點）──────────────────────────────


_TRANSLATE_TRIGGERS = ("翻譯", "translate", "翻成", "翻為", "的英文", "的日文", "的韓文", "的中文")
_LANG_KEYWORDS = {
    "英文": "en", "english": "en", "英": "en",
    "中文": "zh-TW", "chinese": "zh-TW", "繁中": "zh-TW", "繁體": "zh-TW",
    "簡中": "zh-CN", "簡體": "zh-CN",
    "日文": "ja", "japanese": "ja", "日": "ja",
    "韓文": "ko", "korean": "ko", "韓": "ko",
    "法文": "fr", "french": "fr",
    "德文": "de", "german": "de",
    "西文": "es", "spanish": "es",
}


def _extract_translate_request(text: str) -> tuple[str, str] | None:
    """回 (要翻的內容, 目標語言代碼)，無法解析則 None。

    支援格式：
      『翻譯：你好』                 → 預設目標 en（中→英）
      『hello 翻成中文』             → en→zh-TW
      『你好 的英文』『你好的日文』   → zh→en/ja
      『translate 你好 to japanese』 → →ja
    """
    text = text.strip().rstrip("。.?？")

    # 『X 翻成 Y』『X 翻為 Y』
    m = re.match(r"^(.+?)\s*翻[成為譯]\s*(.+)$", text)
    if m:
        content = m.group(1).strip()
        lang_part = m.group(2).strip()
        for kw, code in _LANG_KEYWORDS.items():
            if kw in lang_part.lower():
                return content, code
        # 預設譯為英文
        return content, "en"

    # 『X 的英文/日文/...』
    m = re.match(r"^(.+?)\s*的\s*(英文|日文|韓文|中文|法文|德文|西文)$", text)
    if m:
        content = m.group(1).strip()
        lang_kw = m.group(2)
        return content, _LANG_KEYWORDS.get(lang_kw, "en")

    # 『翻譯 X』『翻譯：X』
    m = re.match(r"^翻譯[:：\s]+(.+)$", text)
    if m:
        content = m.group(1).strip()
        # 預設依語言判斷：含 ASCII 偏多 → 翻中文，否則翻英文
        ascii_ratio = sum(1 for c in content if c.isascii()) / max(len(content), 1)
        return content, "zh-TW" if ascii_ratio > 0.6 else "en"

    # 『translate X (to Y)?』
    m = re.match(r"^translate\s+(.+?)(?:\s+to\s+(\w+))?$", text, re.IGNORECASE)
    if m:
        content = m.group(1).strip()
        target = m.group(2)
        if target:
            for kw, code in _LANG_KEYWORDS.items():
                if kw in target.lower():
                    return content, code
        return content, "zh-TW"

    return None


def _try_translate(text: str) -> str | None:
    """免費 Google Translate 端點（translate.googleapis.com/translate_a/single client=gtx）。

    支援中翻英 / 英翻中 / 中翻日 / 中翻韓 等。
    """
    if not any(k in text.lower() for k in _TRANSLATE_TRIGGERS):
        return None
    parsed = _extract_translate_request(text)
    if not parsed:
        return None
    content, target = parsed
    if not content or len(content) > 500:
        return None

    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": target,
            "dt": "t",
            "q": content,
        }
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        # data[0] 是 segments list，每個 segment[0] 是譯文
        if not data or not isinstance(data, list) or not data[0]:
            return None
        translated_parts: list[str] = []
        for seg in data[0]:
            if seg and seg[0]:
                translated_parts.append(seg[0])
        translated = "".join(translated_parts).strip()
        if not translated:
            return None
        # 偵測來源語言
        src_lang = ""
        if len(data) > 2 and isinstance(data[2], str):
            src_lang = data[2]
        return (
            f"🌐 翻譯（{src_lang or 'auto'} → {target}）\n\n"
            f"原文：{content[:200]}\n"
            f"譯文：{translated[:400]}\n\n"
            "（來源：Google Translate gtx）"
        )
    except Exception as e:
        logger.info("_try_translate failed: %s", e)
        return None


# ── intent 3: 倒垃圾 / 垃圾車（台北市開放資料）──────────────────────────────


_GARBAGE_TRIGGERS = ("倒垃圾", "垃圾車", "資源回收")


def _try_garbage_time(text: str) -> str | None:
    """台北市垃圾車路線/時間 — 開放資料平台 ID 8efd9a0a-a559-4da3-8c93-1bff3e9dec48。

    嘗試匹配文字裡的「里」名稱，找到就回該里近期到車時間；沒指定就回前 5 個 sample。
    """
    if not any(k in text for k in _GARBAGE_TRIGGERS):
        return None

    # 抽里名（『XX 里』）
    m_li = re.search(r"([一-龥]{1,8}里)", text)
    target_li = m_li.group(1) if m_li else None

    try:
        url = (
            "https://data.taipei/api/v1/dataset/"
            "8efd9a0a-a559-4da3-8c93-1bff3e9dec48"
        )
        params = {"scope": "resourceAquire", "limit": 1000}
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
            verify=False,  # data.taipei 缺 Subject Key Identifier
        )
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        # data.taipei 兩種 envelope：{"result":{"results":[...]}}  或  []（空集合）
        if isinstance(data, list):
            results = data
        else:
            results = data.get("result", {}).get("results", []) if isinstance(data, dict) else []
        if not results:
            # endpoint 還活著但 dataset ID 已失效 / 規格改變 — 給使用者導引
            return (
                "🗑️ 垃圾車路線查詢\n"
                "data.taipei 該資料集目前回空集合，可能 dataset ID 已調整。\n\n"
                "請改查：https://www-ws.gov.taipei/Default.aspx?tabid=85\n"
                "（北市環保局垃圾車 GPS 即時點位）\n\n"
                "（開放資料 endpoint 暫時失效）"
            )

        if target_li:
            matches = [
                r for r in results
                if target_li in (r.get("villag") or r.get("village") or r.get("Village") or "")
            ]
            if not matches:
                # 退而求其次：搜尋整個 record 字串
                matches = [
                    r for r in results
                    if target_li in str(r)
                ]
            if not matches:
                return f"🗑️ 找不到「{target_li}」的垃圾車資訊（可能里名拼寫不同）。"
            lines = [f"🗑️ {target_li} 垃圾車（前 5 筆）"]
            for r in matches[:5]:
                # 欄位名隨資料集版本變動，盡量擷取
                addr = r.get("address") or r.get("Address") or r.get("Location") or ""
                t1 = r.get("time") or r.get("Time") or r.get("CarTime") or r.get("car_time") or ""
                line = r.get("line") or r.get("Line") or ""
                lines.append(f"\n• {addr or line}")
                if t1:
                    lines.append(f"  時間：{t1}")
            lines.append("\n\n（來源：data.taipei 開放資料）")
            return "\n".join(lines)

        # 沒指定里 → 給前 5 個 sample
        lines = ["🗑️ 台北市垃圾車路線（前 5 筆，請指定里以縮小範圍）"]
        for r in results[:5]:
            village = r.get("villag") or r.get("village") or ""
            addr = r.get("address") or r.get("Address") or r.get("Location") or ""
            t1 = r.get("time") or r.get("Time") or r.get("CarTime") or ""
            label = " / ".join([x for x in [village, addr] if x])
            lines.append(f"\n• {label[:60]}")
            if t1:
                lines.append(f"  時間：{t1}")
        lines.append("\n\n💡 提示：問『XX 里 倒垃圾』可查特定里")
        lines.append("\n（來源：data.taipei 開放資料）")
        return "\n".join(lines)
    except Exception as e:
        logger.info("_try_garbage_time failed: %s", e)
        return None


# ── intent 4: 公車到站（台北 PDA 5284 / TDX fallback）─────────────────────


_BUS_TRIGGERS = ("到站", "公車", "還有多久", "幾分鐘到", "公車幾分鐘", "公車多久", "幾分鐘來")

# pda.5284.gov.taipei/MQS/* 是 ebus.gov.taipei 內部用的 mobile 入口，無需任何 key。
# 原 spec 寫的 ebus.gov.taipei/Stop/StopMap 與 /Route/StopsOfRoute 實測 (2026-05-08) 皆 404 / 500，
# 走 PDA 5284 是同源 fallback，可拿到真實 JSON ETA。
_PDA_BASE = "https://pda.5284.gov.taipei/MQS"

# RouteDyna n1 字串第 8 欄 (idx 7) 是 ETA 秒數；特殊代碼見 TTEMap (來源：MQS/route.jsp 內嵌 JS)
_BUS_ETA_TTE_MAP = {
    "0": "進站中",
    "": "未發車",
    "-1": "未發車",
    "-2": "交管不停",
    "-3": "末班已過",
    "-4": "今日未營運",
}


def _bus_extract_route_and_stop(text: str) -> tuple[str, str] | None:
    """從訊息抽 (route_name, stop_name); 兩者皆需才回非 None。

    支援格式：
      『藍 7 公車 還多久到 市政府站』
      『307 公車 到 台大醫院 還多久』
      『公車 235 到 公館 幾分鐘』
      『棕 1 到 動物園 還多久』
    """
    # 抽 route — 數字、藍/紅/棕/綠/橘/黃 + 數字（可帶區/直）、XX幹線
    route_pat = re.compile(
        r"(?:^|[^\w])"
        r"("
        r"[藍紅棕綠橘黃]\s?\d{1,3}[區直]?"
        r"|\d{1,4}[A-Za-z]?[區直]?"
        r"|[一-龥]{2,8}幹線"
        r")"
    )
    m_route = route_pat.search(text)
    route_name = None
    if m_route:
        cand = m_route.group(1).strip().replace(" ", "")
        # 過濾年份
        if not (cand.isdigit() and 1900 <= int(cand) <= 2100):
            route_name = cand

    if not route_name:
        return None

    # 抽 stop — 先找『到/至/在 + 站名 + (還/要/多久/幾分鐘/結尾)』
    stop_name = None
    m_stop = re.search(
        r"(?:到|至|在|去)\s*([一-龥A-Za-z0-9·]{2,15}?)\s*站?(?:還|要|多久|幾分鐘|了|嗎|呢|$)",
        text,
    )
    if m_stop:
        stop_name = m_stop.group(1).strip().rstrip("站牌")
    if not stop_name:
        # 再退一步找『XX站』或『XX站牌』
        m_stop = re.search(r"([一-龥A-Za-z0-9·]{2,12})站(?:牌)?", text)
        if m_stop:
            stop_name = m_stop.group(1).strip()

    if not stop_name or len(stop_name) < 2:
        return None
    # 防止把 route 本身當成站名
    if stop_name == route_name or stop_name in route_name:
        return None
    return route_name, stop_name


def _bus_find_route_id(route_name: str) -> str | None:
    """從 PDA routelist.jsp 的 `<option value=rid>routeName</option>` 找 exact 匹配。"""
    try:
        resp = requests.get(
            f"{_PDA_BASE}/routelist.jsp",
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
            verify=False,
        )
        if resp.status_code != 200:
            return None
        import html as _html

        matches = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]+)</option>', resp.text)
        # exact match (decoded)
        for rid, name in matches:
            if _html.unescape(name).strip() == route_name:
                return rid
        # 寬鬆 match — 去空格再比
        rn_norm = route_name.replace(" ", "")
        for rid, name in matches:
            if _html.unescape(name).strip().replace(" ", "") == rn_norm:
                return rid
        return None
    except Exception as e:
        logger.info("_bus_find_route_id failed (%s): %s", route_name, e)
        return None


def _bus_find_stop_sids(route_id: str, stop_name: str) -> list[tuple[str, str]]:
    """從 route.jsp?rid=N 抓 (sid, displayName) list；stop_name substring 匹配。"""
    try:
        resp = requests.get(
            f"{_PDA_BASE}/route.jsp?rid={route_id}",
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
            verify=False,
        )
        if resp.status_code != 200:
            return []
        import html as _html

        matches = re.findall(r'href="stop\.jsp\?sid=(\d+)">([^<]+)</a>', resp.text)
        found = []
        seen = set()
        for sid, name in matches:
            if sid in seen:
                continue
            seen.add(sid)
            decoded = _html.unescape(name).strip()
            if stop_name in decoded:
                found.append((sid, decoded))
        return found
    except Exception as e:
        logger.info("_bus_find_stop_sids failed (%s,%s): %s", route_id, stop_name, e)
        return []


def _bus_format_eta(eta_raw: str) -> str:
    """ETA 秒數 / 特殊代碼 → 顯示字串。"""
    if eta_raw in _BUS_ETA_TTE_MAP:
        return _BUS_ETA_TTE_MAP[eta_raw]
    try:
        eta_sec = int(eta_raw)
    except ValueError:
        return "未知"
    if eta_sec < 0:
        return "未發車"
    if eta_sec < 180:
        return "即將進站"
    return f"{eta_sec // 60} 分鐘"


def _bus_get_eta(route_id: str, sid: str) -> str | None:
    """從 RouteDyna?routeid=N 抓該 sid 的 ETA。"""
    try:
        resp = requests.get(
            f"{_PDA_BASE}/RouteDyna?routeid={route_id}",
            headers={
                "User-Agent": _UA,
                "Referer": f"{_PDA_BASE}/route.jsp?rid={route_id}",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            timeout=_HTTP_TIMEOUT,
            verify=False,
        )
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        sid_str = str(sid)
        for stop in data.get("Stop", []) or []:
            n1 = stop.get("n1") or ""
            parts = n1.split(",")
            # n1 = "N1,stopId,routeId,depTime,prevId,nextId,direction,etaSec,seq,..."
            if len(parts) < 8 or parts[1] != sid_str:
                continue
            return _bus_format_eta(parts[7])
        return None
    except Exception as e:
        logger.info("_bus_get_eta failed (%s,%s): %s", route_id, sid, e)
        return None


def _try_bus_arrival(text: str) -> str | None:
    """『藍 7 公車 還多久到 市政府站』『307 還多久到 台大醫院』。

    Pipeline:
        1. regex 抽 route_name + stop_name (兩者都要)
        2. PDA 5284 routelist.jsp → route_id
        3. route.jsp?rid → matching sids
        4. RouteDyna?routeid → ETA per sid (n1[7] 秒數)

    若 PDA 拿不到 (非台北市路線 / parse 失敗) → fallback TDX (需 .env 設
    TDX_CLIENT_ID / TDX_CLIENT_SECRET)；沒設環境變數則 silent return None。
    """
    if not any(k in text for k in _BUS_TRIGGERS):
        return None

    parsed = _bus_extract_route_and_stop(text)
    if not parsed:
        return None
    route_name, stop_name = parsed

    # ── Path 1: 台北 PDA 5284 (免 key) ────────────────────────────────────────
    try:
        route_id = _bus_find_route_id(route_name)
        if route_id:
            stops = _bus_find_stop_sids(route_id, stop_name)
            if stops:
                lines = [f"🚌 公車 {route_name} 到 {stop_name}"]
                for sid, full_name in stops[:3]:
                    eta = _bus_get_eta(route_id, sid)
                    if eta is None:
                        continue
                    lines.append(f"  - {full_name}：預估 {eta}")
                if len(lines) > 1:
                    lines.append("\n（來源：PDA 5284 即時動態）")
                    return "\n".join(lines)
    except Exception as e:
        logger.info("_try_bus_arrival PDA path failed: %s", e)

    # ── Path 2: TDX fallback (需 .env 設 TDX_CLIENT_ID / TDX_CLIENT_SECRET) ──
    try:
        import os

        tdx_id = os.environ.get("TDX_CLIENT_ID")
        tdx_secret = os.environ.get("TDX_CLIENT_SECRET")
        if not tdx_id or not tdx_secret:
            return None  # 沒 key 就 silent；走下個 fallback handler

        # OAuth2 client credentials → access_token
        token_resp = requests.post(
            "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": tdx_id,
                "client_secret": tdx_secret,
            },
            timeout=_HTTP_TIMEOUT,
        )
        if token_resp.status_code != 200:
            return None
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return None

        # 試 Taipei
        api_url = (
            f"https://tdx.transportdata.tw/api/basic/v2/Bus/EstimatedTimeOfArrival/"
            f"City/Taipei/{route_name}?$format=JSON"
        )
        resp = requests.get(
            api_url,
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        items = resp.json() or []
        # 找站名包含 stop_name 的條目
        matches = [
            it for it in items
            if stop_name in (it.get("StopName", {}).get("Zh_tw") or "")
        ]
        if not matches:
            return None
        lines = [f"🚌 公車 {route_name} 到 {stop_name}"]
        for it in matches[:3]:
            full_name = it.get("StopName", {}).get("Zh_tw", stop_name)
            est = it.get("EstimateTime")
            if est is None:
                continue
            if est < 60:
                eta = "即將進站"
            else:
                eta = f"{est // 60} 分鐘"
            lines.append(f"  - {full_name}：預估 {eta}")
        if len(lines) > 1:
            lines.append("\n（來源：TDX 即時 API）")
            return "\n".join(lines)
        return None
    except Exception as e:
        logger.info("_try_bus_arrival TDX path failed: %s", e)
        return None


# ── intent 5: 統一發票對獎（財政部開放資料）─────────────────────────────────


_INVOICE_TRIGGERS = ("發票對獎", "中獎號碼", "統一發票", "發票中了沒", "發票號碼")


def _try_invoice_lottery(text: str) -> str | None:
    """財政部統一發票中獎號碼 — invoice.etax.nat.gov.tw/invoice.xml。

    抓最近一期，回特獎 / 頭獎 / 二獎 / 三獎 / 四獎 / 五獎 / 六獎 號碼。
    """
    if not any(k in text for k in _INVOICE_TRIGGERS):
        return None

    try:
        import xml.etree.ElementTree as ET

        url = "https://invoice.etax.nat.gov.tw/invoice.xml"
        resp = requests.get(
            url,
            headers={"User-Agent": _UA},
            timeout=_HTTP_TIMEOUT,
            verify=False,  # 財政部憑證缺 Subject Key Identifier
        )
        if resp.status_code != 200 or not resp.text:
            return None
        # invoice.xml 是 RSS 樣式
        root = ET.fromstring(resp.text)
        items = root.findall(".//item")
        if not items:
            return None
        item = items[0]  # 最近一期
        title_el = item.find("title")
        desc_el = item.find("description")
        title = (title_el.text or "").strip() if title_el is not None else "最近一期"
        desc_html = (desc_el.text or "") if desc_el is not None else ""

        # description 是 HTML 字串，需要 parse；用 BeautifulSoup（lite_reply 已在用）
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(desc_html, "html.parser")
            text_block = soup.get_text("\n", strip=True)
        except Exception:
            # 沒 bs4 時退而求其次用 regex 砍 tag
            text_block = re.sub(r"<[^>]+>", "\n", desc_html)
            text_block = re.sub(r"\n+", "\n", text_block).strip()

        if not text_block:
            return None

        # 試著抽出各獎別號碼
        # 常見 pattern：『特別獎 12345678』『特獎 87654321』『頭獎 ...、...、...』
        prize_lines = []
        prize_patterns = [
            ("特別獎", r"特別獎[\s:：]*([0-9、，,\s]+)"),
            ("特獎",   r"(?<!別)特獎[\s:：]*([0-9、，,\s]+)"),
            ("頭獎",   r"頭獎[\s:：]*([0-9、，,\s]+)"),
            ("二獎",   r"(?:增開)?二獎[\s:：]*([0-9、，,\s]+)"),
            ("三獎",   r"(?:增開)?三獎[\s:：]*([0-9、，,\s]+)"),
            ("四獎",   r"(?:增開)?四獎[\s:：]*([0-9、，,\s]+)"),
            ("五獎",   r"(?:增開)?五獎[\s:：]*([0-9、，,\s]+)"),
            ("六獎",   r"(?:增開)?六獎[\s:：]*([0-9、，,\s]+)"),
            ("增開六獎", r"增開六獎[\s:：]*([0-9、，,\s]+)"),
        ]
        seen_prizes: set[str] = set()
        for name, pat in prize_patterns:
            if name in seen_prizes:
                continue
            m = re.search(pat, text_block)
            if m:
                nums = re.findall(r"\d{3,8}", m.group(1))
                if nums:
                    prize_lines.append(f"  {name}：{'、'.join(nums)}")
                    seen_prizes.add(name)

        if not prize_lines:
            # 抽不到 → 直接顯示 description 純文字（截斷）
            return (
                f"🧾 統一發票 {title}\n\n"
                f"{text_block[:400]}\n\n"
                f"來源：{url}\n"
                "（來源：財政部開放資料）"
            )

        return (
            f"🧾 統一發票 {title}\n\n"
            + "\n".join(prize_lines)
            + f"\n\n來源：{url}\n"
            "（來源：財政部開放資料）"
        )
    except Exception as e:
        logger.info("_try_invoice_lottery failed: %s", e)
        return None


# ── 對外 export ─────────────────────────────────────────────────────────────


EXTRA_HANDLERS = [
    _try_news,
    _try_translate,
    _try_garbage_time,
    _try_bus_arrival,
    _try_invoice_lottery,
]


# ── smoke test ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    samples = {
        "_try_news": [
            "台積電 的新聞",
            "最近 烏俄戰爭",
            "AI news",
            "新聞",
        ],
        "_try_translate": [
            "hello 翻成中文",
            "你好 的英文",
            "我愛你 翻成日文",
            "謝謝 的韓文",
            "translate good morning to japanese",
        ],
        "_try_garbage_time": [
            "民生里 倒垃圾",
            "垃圾車什麼時候來",
            "資源回收",
        ],
        "_try_bus_arrival": [
            "藍 7 公車 還多久到 市政府站",
            "307 公車 到 台大醫院 還多久",
            "公車 235 到 公館 幾分鐘",
            "公車",                # 缺路線 → None
            "307 還有多久",        # 缺站名 → None
        ],
        "_try_invoice_lottery": [
            "統一發票",
            "發票中獎號碼",
            "發票對獎",
        ],
    }

    name_to_fn = {
        "_try_news": _try_news,
        "_try_translate": _try_translate,
        "_try_garbage_time": _try_garbage_time,
        "_try_bus_arrival": _try_bus_arrival,
        "_try_invoice_lottery": _try_invoice_lottery,
    }

    for fn_name, inputs in samples.items():
        fn = name_to_fn[fn_name]
        print(f"\n{'='*60}")
        print(f"  {fn_name}")
        print("=" * 60)
        for s in inputs:
            try:
                out = fn(s)
            except Exception as e:
                out = f"<EXCEPTION: {e}>"
            head = (out or "（None — 不適用）")
            head = head if len(head) <= 200 else head[:200] + "...(截斷)"
            print(f"\n>>> {s}")
            print(head)

    print("\n\n=== EXTRA_HANDLERS callable 檢查 ===")
    for h in EXTRA_HANDLERS:
        print(f"  {h.__name__}: callable={callable(h)}")
