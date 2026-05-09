"""Pure-local web scraper — replaces cloud search APIs (Tavily/Brave/Jina).

零雲端 API 設計：DuckDuckGo HTML / 維基 / Google news RSS / arXiv API /
政府開放資料平台都是純 HTTP scraping（沒有 API key 概念）。

公開介面（給 agent_tools / lite_reply 整合）：
  - search_duckduckgo(query, k=5)         → list[{title, url, snippet}]
  - search_google_news(query, k=10)       → list[{title, url, published, source}]
  - fetch_full_text(url, max_chars=5000)  → str | None
  - search_wiki_full(title, lang='zh')    → dict | None
  - search_arxiv(query, k=5)              → list[{title, authors, abstract, pdf_url}]
  - scrape_taiwan_gov(dataset_id)         → dict | list | None

Helper：
  - web_search_pipeline(query, k=3)       → list[{source, content}]

設計原則：
- 失敗一律回 None / [] / {} 而非 raise（caller 可組多源 fallback）
- 每個 source 內建 0.5s delay 避免被 ban
- User-Agent 設一個正常 browser-like 的（不要默認 python-requests/X.X）
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("web_scraper")

# Browser-like UA — DuckDuckGo / Google news / 一般站點都接受
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
_HTTP_TIMEOUT = 10
# 每 source 的 rate-limit delay（秒）— 避免短時間多打被 ban
_RATE_LIMIT_DELAY = 0.5


def _polite_sleep() -> None:
    """每次成功 / 失敗請求後 sleep，避免被視為 bot 連發。"""
    try:
        time.sleep(_RATE_LIMIT_DELAY)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. DuckDuckGo HTML scraping
# ─────────────────────────────────────────────────────────────────────────────
def search_duckduckgo(query: str, k: int = 5) -> list[dict]:
    """Scrape DuckDuckGo HTML 結果頁，回前 k 筆 {title, url, snippet}。

    DDG HTML lite 端點（no-JS 版）的結構（dev 過幾次才知道）：
      <div class="result results_links results_links_deep web-result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=<encoded_url>">title</a>
        <a class="result__snippet">snippet text</a>

    `result__a` 的 href 不是直接的 URL，是 DDG 的 redirector，要 parse `uddg`
    參數拿真 URL。snippet 偶爾沒有，要容忍 None。

    失敗 → 回 []。
    """
    if not query or not query.strip():
        return []

    q = query.strip()
    url = "https://html.duckduckgo.com/html/"
    try:
        resp = requests.post(
            url,
            data={"q": q},
            headers=_DEFAULT_HEADERS,
            timeout=_HTTP_TIMEOUT,
        )
    except Exception as e:
        logger.info("DDG request failed: %s", e)
        _polite_sleep()
        return []

    _polite_sleep()
    if resp.status_code != 200 or not resp.text:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[dict] = []
    # `result` class 是個 root container；裡面包 `result__a`(title link) +
    # `result__snippet`(摘要) + `result__url`(顯示用 url)
    for div in soup.select("div.result"):
        a = div.select_one("a.result__a")
        if not a:
            continue
        title = a.get_text(strip=True)
        raw_href = a.get("href") or ""
        # Parse DDG redirector：/l/?uddg=<encoded>&rut=...
        real_url = _decode_ddg_redirect(raw_href)
        snip_node = div.select_one("a.result__snippet") or div.select_one(
            ".result__snippet"
        )
        snippet = snip_node.get_text(" ", strip=True) if snip_node else ""

        if not title or not real_url:
            continue
        out.append({"title": title, "url": real_url, "snippet": snippet})
        if len(out) >= k:
            break
    return out


def _decode_ddg_redirect(href: str) -> str:
    """DDG `result__a` 的 href 形如 //duckduckgo.com/l/?uddg=ENCODED&rut=...
    抽 uddg 參數還原為真實 URL。若已是 http(s) 直連則直接回。
    """
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        # 但 DDG 也可能直接給 redirector https URL（含 host）— 同樣 parse
        try:
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            if "uddg" in qs and qs["uddg"]:
                return unquote(qs["uddg"][0])
        except Exception:
            pass
        return href
    # 形如 //duckduckgo.com/l/?...
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return unquote(qs["uddg"][0])
    except Exception as e:
        logger.info("DDG redirect decode failed: %s", e)
    return href


# ─────────────────────────────────────────────────────────────────────────────
# 2. Google News RSS
# ─────────────────────────────────────────────────────────────────────────────
def search_google_news(query: str, k: int = 10) -> list[dict]:
    """Google News RSS — 純 HTTP，無 API key。

    回傳：list[{title, url, published, source}]
    """
    if not query or not query.strip():
        return []

    q = quote_plus(query.strip())
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        logger.info("Google news RSS request failed: %s", e)
        _polite_sleep()
        return []
    _polite_sleep()
    if resp.status_code != 200 or not resp.text:
        return []

    out: list[dict] = []
    try:
        root = ET.fromstring(resp.text)
        # RSS 2.0 結構：rss/channel/item*
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            src_node = item.find("source")
            source = (src_node.text or "").strip() if src_node is not None else ""
            if not title or not link:
                continue
            out.append(
                {
                    "title": title,
                    "url": link,
                    "published": pub,
                    "source": source,
                }
            )
            if len(out) >= k:
                break
    except ET.ParseError as e:
        logger.info("Google news RSS parse failed: %s", e)
        return []
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 3. Full-text fetch + clean (trafilatura primary, BS4 fallback)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_full_text(url: str, max_chars: int = 5000) -> Optional[str]:
    """抽乾淨正文（去 nav / footer / ads）。

    主：trafilatura.extract（boilerpipe-like，文章類最準）
    退：BeautifulSoup get_text（html2text 級的退路）
    cap max_chars。

    失敗 → None。
    """
    if not url or not url.strip():
        return None

    try:
        resp = requests.get(
            url, headers=_DEFAULT_HEADERS, timeout=_HTTP_TIMEOUT, allow_redirects=True
        )
    except Exception as e:
        logger.info("fetch_full_text request failed (%s): %s", url, e)
        _polite_sleep()
        return None

    _polite_sleep()
    if resp.status_code != 200 or not resp.text:
        return None

    html = resp.text

    # 主：trafilatura
    try:
        import trafilatura  # lazy
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        if extracted and len(extracted.strip()) >= 50:
            text = extracted.strip()
            if len(text) > max_chars:
                text = text[:max_chars]
            return text
    except Exception as e:
        logger.info("trafilatura extract failed (%s): %s", url, e)

    # 退路：BS4
    try:
        soup = BeautifulSoup(html, "html.parser")
        # 砍 noise tag
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        body = soup.find("body") or soup
        text = body.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
        if len(text) > max_chars:
            text = text[:max_chars]
        return text
    except Exception as e:
        logger.info("fetch_full_text BS4 fallback failed (%s): %s", url, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Wikipedia full summary (含可選 article body)
# ─────────────────────────────────────────────────────────────────────────────
def search_wiki_full(
    title: str, lang: str = "zh", with_full_text: bool = False
) -> Optional[dict]:
    """Wiki REST v1 summary + 可選的 article body。

    回 {title, description, extract, url, full_text?}。失敗 → None。
    """
    if not title or not title.strip():
        return None
    t = title.strip()
    api_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote_plus(t)}"
    try:
        resp = requests.get(api_url, headers=_DEFAULT_HEADERS, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        logger.info("wiki request failed (%s): %s", t, e)
        _polite_sleep()
        return None
    _polite_sleep()
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception as e:
        logger.info("wiki json decode failed (%s): %s", t, e)
        return None

    extract = (data.get("extract") or "").strip()
    if not extract:
        return None
    page_url = (data.get("content_urls") or {}).get("desktop", {}).get("page") or ""
    out = {
        "title": data.get("title") or t,
        "description": (data.get("description") or "").strip(),
        "extract": extract,
        "url": page_url,
    }
    if with_full_text and page_url:
        full = fetch_full_text(page_url, max_chars=5000)
        if full:
            out["full_text"] = full
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 5. arXiv search (Atom feed)
# ─────────────────────────────────────────────────────────────────────────────
def search_arxiv(query: str, k: int = 5) -> list[dict]:
    """arXiv Atom API — 純 HTTP，無 API key。

    回傳：list[{title, authors, abstract, pdf_url}]
    """
    if not query or not query.strip():
        return []
    q = quote_plus(query.strip())
    url = f"http://export.arxiv.org/api/query?search_query={q}&max_results={k}"
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        logger.info("arxiv request failed: %s", e)
        _polite_sleep()
        return []
    _polite_sleep()
    if resp.status_code != 200 or not resp.text:
        return []

    out: list[dict] = []
    # Atom feed namespace
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(resp.text)
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            abstract = (
                entry.findtext("atom:summary", default="", namespaces=ns) or ""
            ).strip()
            authors = []
            for au in entry.findall("atom:author", ns):
                name = au.findtext("atom:name", default="", namespaces=ns)
                if name:
                    authors.append(name.strip())
            pdf_url = ""
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                    pdf_url = link.get("href") or ""
                    break
            if not pdf_url:
                # fallback: id 是 abs URL，把 abs → pdf
                arxiv_id = (
                    entry.findtext("atom:id", default="", namespaces=ns) or ""
                ).strip()
                if arxiv_id:
                    pdf_url = arxiv_id.replace("/abs/", "/pdf/")
            if not title:
                continue
            out.append(
                {
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "pdf_url": pdf_url,
                }
            )
            if len(out) >= k:
                break
    except ET.ParseError as e:
        logger.info("arxiv parse failed: %s", e)
        return []
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 6. Taiwan gov open data
# ─────────────────────────────────────────────────────────────────────────────
def scrape_taiwan_gov(dataset_id: str) -> Optional[dict | list]:
    """政府開放資料平台 — 純 HTTP，無 key。

    回 JSON（dict / list 視 dataset 格式而定）。失敗 → None。
    """
    if not dataset_id or not str(dataset_id).strip():
        return None
    ds = str(dataset_id).strip()
    url = f"https://data.gov.tw/api/v1/data/{ds}"
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_HTTP_TIMEOUT)
    except Exception as e:
        logger.info("gov dataset request failed (%s): %s", ds, e)
        _polite_sleep()
        return None
    _polite_sleep()
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception as e:
        logger.info("gov dataset json decode failed (%s): %s", ds, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 7. 整合 helper：multi-source pipeline
# ─────────────────────────────────────────────────────────────────────────────
_NEWS_TRIGGERS = ("新聞", "即時", "最新", "今天", "昨天", "今日")
_WIKI_TRIGGERS = ("什麼是", "是什麼", "介紹", "定義", "what is", "what's")


def web_search_pipeline(query: str, k: int = 3) -> list[dict]:
    """主 pipeline — multi-source aggregation。

    流程：
      1. 主：DuckDuckGo top-k
      2. 補：Google news（query 含 新聞 / 即時 / 最新）
      3. 補：wiki summary（query 含 什麼是 / 介紹 / 定義）
      4. 對 top-k DDG urls 各 fetch_full_text 拿乾淨正文
      5. 回 [{source, content}]，content 是 clean text

    Source 標籤：
      - "duckduckgo" / "google_news" / "wikipedia"
    """
    if not query or not query.strip():
        return []
    q = query.strip()
    q_low = q.lower()
    out: list[dict] = []

    # 1. 主源 DuckDuckGo
    ddg = search_duckduckgo(q, k=k)
    for item in ddg[:k]:
        url = item.get("url", "")
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        full = fetch_full_text(url, max_chars=5000) if url else None
        content_parts = [f"標題：{title}", f"URL：{url}"]
        if snippet:
            content_parts.append(f"摘要：{snippet}")
        if full:
            content_parts.append(f"正文：\n{full}")
        out.append(
            {
                "source": "duckduckgo",
                "content": "\n".join(content_parts),
            }
        )

    # 2. 補：news（trigger 命中時補抓）
    if any(t in q for t in _NEWS_TRIGGERS):
        news = search_google_news(q, k=5)
        for item in news[:5]:
            content = (
                f"標題：{item.get('title', '')}\n"
                f"來源：{item.get('source', '')}\n"
                f"發布：{item.get('published', '')}\n"
                f"URL：{item.get('url', '')}"
            )
            out.append({"source": "google_news", "content": content})

    # 3. 補：wiki（trigger 命中時補抓）
    if any(t in q_low for t in _WIKI_TRIGGERS) or any(t in q for t in _WIKI_TRIGGERS):
        # 抽 wiki query — 簡單版：去掉 trigger 後當 title
        wq = q
        for trig in ("什麼是", "是什麼", "介紹", "定義"):
            wq = wq.replace(trig, "").strip()
        wq = wq.strip("?？.。 ")
        if wq:
            w = search_wiki_full(wq, lang="zh")
            if w:
                content = (
                    f"條目：{w.get('title', '')}\n"
                    f"描述：{w.get('description', '')}\n"
                    f"摘要：{w.get('extract', '')}\n"
                    f"URL：{w.get('url', '')}"
                )
                out.append({"source": "wikipedia", "content": content})

    return out


__all__ = [
    "search_duckduckgo",
    "search_google_news",
    "fetch_full_text",
    "search_wiki_full",
    "search_arxiv",
    "scrape_taiwan_gov",
    "web_search_pipeline",
]
