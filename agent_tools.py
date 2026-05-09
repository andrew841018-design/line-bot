"""Function calling tools — 給 local LLM 用的工具集（純本機，無雲端 LLM）。

設計目標：把 line_bot 既有的 deterministic handler（即時資料、查詢類）包成
ReAct-style 工具，讓 14B 級 local LLM 也能在 quota 爆時做到「Gemini + 工具」級
能力。

每個 tool 提供：
  - name (str)            tool 在 prompt 中的識別名
  - description (str)      餵給 LLM 做 routing decision 的人類可讀說明
  - args (dict[str, str])  arg name → type hint（純文字描述，給 LLM 看）
  - call (Callable)        真正執行的函式：args dict → str（observation）

ReAct flow：
  1. LLM 看到 user msg + tool list（list_tools_for_prompt）
  2. LLM 輸出 JSON：{"action": "TOOL_NAME", "args": {...}}
  3. 路由層 parse JSON → call_tool(name, args) → 拿 observation 字串
  4. observation 餵回 LLM 第二輪 → 產 final answer

設計原則：
- tool 失敗（exception / None）一律回**字串**而非 raise，避免打斷 ReAct loop
- tool result 控制長度（< 1500 chars），太長 LLM context 吃緊
- lazy import 重套件（yfinance / requests）— 主流程只 import agent_tools 不動
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("agent_tools")

# 結果字串硬上限，避免大量 HTML / 巨量 RAG hit 把 context 吃爆
_MAX_RESULT_CHARS = 1500


def _truncate(s: str | None, limit: int = _MAX_RESULT_CHARS) -> str:
    """空值 / 太長都正規化為字串。"""
    if s is None:
        return "（無結果）"
    s = str(s).strip()
    if not s:
        return "（無結果）"
    if len(s) > limit:
        return s[:limit] + f"…（已截斷，原長 {len(s)}）"
    return s


# ── tool callables ───────────────────────────────────────────────────────────


def _tool_get_stock_price(args: dict) -> str:
    """股價查詢 — 包 stock_quote.get_quotes_text。"""
    text = args.get("text") or args.get("query") or ""
    if not text:
        return "（缺 text 參數）"
    try:
        import stock_quote  # lazy
        out = stock_quote.get_quotes_text(text)
        return _truncate(out) if out else "（找不到股價，可能是 ticker 拼錯或非交易時間）"
    except Exception as e:
        logger.info("get_stock_price failed: %s", e)
        return f"（股價查詢失敗：{e}）"


def _tool_get_weather(args: dict) -> str:
    """天氣查詢 — 包 lite_reply._weather_taiwan。
    accept `city` 或 `text`，為了讓 LLM 兩種 schema 都行。
    """
    text = args.get("city") or args.get("text") or ""
    try:
        import lite_reply  # lazy
        # _weather_taiwan 從 text 抓縣市名；若 LLM 直接給 city 名也 OK
        out = lite_reply._weather_taiwan(text or "台北")
        return _truncate(out) if out else "（天氣抓取失敗）"
    except Exception as e:
        logger.info("get_weather failed: %s", e)
        return f"（天氣查詢失敗：{e}）"


def _tool_search_wiki(args: dict) -> str:
    """中文維基查詢 — 包 lite_reply._wiki_summary。"""
    query = args.get("query") or args.get("text") or ""
    if not query:
        return "（缺 query 參數）"
    try:
        import lite_reply
        out = lite_reply._wiki_summary(query)
        return _truncate(out) if out else "（維基沒這個條目，或內容太短）"
    except Exception as e:
        logger.info("search_wiki failed: %s", e)
        return f"（維基查詢失敗：{e}）"


def _tool_summarize_url(args: dict) -> str:
    """URL 摘要 — 包 lite_reply._summarize_url。"""
    url = args.get("url") or ""
    if not url:
        return "（缺 url 參數）"
    try:
        import lite_reply
        out = lite_reply._summarize_url(url)
        return _truncate(out) if out else "（網頁抓取失敗或無內容）"
    except Exception as e:
        logger.info("summarize_url failed: %s", e)
        return f"（URL 摘要失敗：{e}）"


def _tool_google_search(args: dict) -> str:
    """Web 搜尋 — 純本機爬蟲（DuckDuckGo HTML + Wiki + Google News，無雲端 API）。

    優先走 web_scraper.search_duckduckgo（純 requests + BeautifulSoup，多筆編號
    結果方便 ReAct citation）；失敗 fallback lite_reply._google_search_snippet 單筆。
    """
    query = args.get("query") or args.get("text") or ""
    if not query:
        return "（缺 query 參數）"

    try:
        import web_scraper  # lazy
        results = web_scraper.search_duckduckgo(query, k=5)
        if results:
            blocks = []
            for i, r in enumerate(results, 1):
                title = (r.get("title") or "").strip()
                url = (r.get("url") or "").strip()
                snip = (r.get("snippet") or "").strip()
                blocks.append(f"[{i}] {title} - {url}\n  {snip}")
            return _truncate("\n".join(blocks))
    except ImportError:
        pass
    except Exception as e:
        logger.info("web_scraper.search_duckduckgo failed: %s", e)

    # Fallback：單筆 lite_reply Google snippet
    try:
        import lite_reply
        snippet_out = lite_reply._google_search_snippet(query)
    except Exception as e:
        logger.info("google_search fallback failed: %s", e)
        return f"（Google search 失敗：{e}）"

    if not snippet_out:
        return "（Google search 沒結果或被擋）"
    return _truncate(snippet_out)


def _tool_get_time(args: dict) -> str:
    """現在時間 / 日期 — 純 datetime，無外部依賴，最 deterministic。"""
    now = datetime.now()
    weekday = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    return now.strftime("%Y-%m-%d %H:%M:%S") + f"（週{weekday}）"


def _tool_retrieve_rag(args: dict) -> str:
    """從過去對話 retrieve 相似 message — 包 rag_retriever.retrieve。"""
    query = args.get("query") or args.get("text") or ""
    k = int(args.get("k") or 3)
    if not query:
        return "（缺 query 參數）"
    try:
        import rag_retriever
        hits = rag_retriever.retrieve(query, k=k)
        if not hits:
            return "（沒有過去類似訊息）"
        lines = []
        for h in hits[:k]:
            text = (h.get("text") or "").replace("\n", " ").strip()
            sim = h.get("similarity", 0.0)
            if len(text) > 100:
                text = text[:100] + "…"
            lines.append(f"- 「{text}」(相似度 {sim:.2f})")
        return _truncate("\n".join(lines))
    except Exception as e:
        logger.info("retrieve_rag failed: %s", e)
        return f"（RAG 查詢失敗：{e}）"


def _tool_get_forex(args: dict) -> str:
    """匯率查詢 — 包 lite_reply._try_forex（內含 yfinance）。
    accept text（如 "100 美金換台幣"）。
    """
    text = args.get("text") or args.get("query") or ""
    if not text:
        return "（缺 text 參數）"
    try:
        import lite_reply
        out = lite_reply._try_forex(text)
        return _truncate(out) if out else "（匯率解析失敗：建議格式『100 美金換台幣』）"
    except Exception as e:
        logger.info("get_forex failed: %s", e)
        return f"（匯率查詢失敗：{e}）"


# ── tool registry ────────────────────────────────────────────────────────────


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_stock_price",
        "description": (
            "查股票/ETF/指數即時價格。"
            "輸入是含中文股名或 ticker 的句子，例：『台積電』『SOXL』『^SOX 費半』。"
            "用於使用者問股價、漲跌、報價時。"
        ),
        "args": {"text": "str（含股名或 ticker 的句子）"},
        "call": _tool_get_stock_price,
    },
    {
        "name": "get_weather",
        "description": (
            "查台灣縣市天氣（CWA 36 小時預報）。"
            "city 例：『台北市』『高雄市』；缺省 = 台北。"
            "用於『今天天氣』『會下雨嗎』。"
        ),
        "args": {"city": "str（台灣縣市名，可選）"},
        "call": _tool_get_weather,
    },
    {
        "name": "search_wiki",
        "description": (
            "中文維基百科 summary（不超過 30 字 query）。"
            "用於『XXX 是什麼』『解釋 XXX』這種定義 / 概念類問題。"
        ),
        "args": {"query": "str（短關鍵字，2-30 字）"},
        "call": _tool_search_wiki,
    },
    {
        "name": "summarize_url",
        "description": (
            "抓 URL 取 title + meta description。"
            "用於使用者貼網址問『這篇在講什麼』『幫我總結』。"
        ),
        "args": {"url": "str（http(s):// 開頭的網址）"},
        "call": _tool_summarize_url,
    },
    {
        "name": "google_search",
        "description": (
            "Google snippet 查詢（單筆純文字，純本機 lite_reply 路徑）。"
            "用於需要外部知識補充時的最後手段。"
        ),
        "args": {"query": "str（搜尋關鍵字）"},
        "call": _tool_google_search,
    },
    {
        "name": "get_time",
        "description": (
            "回傳現在時間 / 日期 / 星期幾。"
            "用於『現在幾點』『今天幾號』『今天禮拜幾』。無 args。"
        ),
        "args": {},
        "call": _tool_get_time,
    },
    {
        "name": "retrieve_rag",
        "description": (
            "從過去對話 retrieve 與 query 相似的 top-k 訊息。"
            "用於『我之前有沒有說過 XX』『上次討論的 YY 細節』這種記憶查詢。"
        ),
        "args": {"query": "str（要找的主題）", "k": "int（top-k，預設 3）"},
        "call": _tool_retrieve_rag,
    },
    {
        "name": "get_forex",
        "description": (
            "查匯率（用 yfinance）。"
            "輸入文字含金額 + 幣別，例：『100 美金換台幣』『50 USD to JPY』。"
        ),
        "args": {"text": "str（含金額與幣別的句子）"},
        "call": _tool_get_forex,
    },
]


def get_tool(name: str) -> dict | None:
    """依 name 找 tool spec dict（不存在回 None）。"""
    if not name:
        return None
    for t in TOOLS:
        if t["name"] == name:
            return t
    return None


def list_tools_for_prompt() -> str:
    """產生 LLM 可讀的 tool list 字串（塞進 system prompt 用）。

    格式：
        - tool_name(arg1, arg2): description
    """
    lines = []
    for t in TOOLS:
        if t["args"]:
            arg_str = ", ".join(t["args"].keys())
        else:
            arg_str = ""
        lines.append(f"- {t['name']}({arg_str}): {t['description']}")
    return "\n".join(lines)


def call_tool(name: str, args: dict | None = None) -> str:
    """執行 tool。回字串 observation（必非 None — 失敗也回錯誤訊息字串）。"""
    args = args or {}
    spec = get_tool(name)
    if spec is None:
        return f"（未知 tool：{name}。可用 tools：{', '.join(t['name'] for t in TOOLS)}）"
    try:
        result = spec["call"](args)
    except Exception as e:
        logger.warning("call_tool %s raised: %s", name, e)
        return f"（tool {name} 執行失敗：{e}）"
    if result is None:
        return f"（tool {name} 無結果）"
    return str(result)


__all__ = [
    "TOOLS",
    "get_tool",
    "list_tools_for_prompt",
    "call_tool",
]
