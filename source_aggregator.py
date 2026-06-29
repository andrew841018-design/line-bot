"""V4 Pipeline Step 3 — multi-query × multi-engine source aggregation.

100% 本機爬蟲。沒有任何雲端 API。

對 caller（main.py / agent_loop.py / lite_reply.py）：

    from source_aggregator import aggregate_sources
    sources = aggregate_sources(queries, total_max=18)
    # → list[{title, url, snippet, source_query, authority_score, domain}]

Pipeline：
  1. 對每 query 平行打 DDG + Google news（thread pool）
  2. 加 Wiki en + Wiki zh（用 queries[0] 當 title）
  3. 用 canonical URL 去重（去掉 utm/fbclid/gclid/ref/source 等追蹤 params）
  4. 算 authority_score（.gov / .edu / 知名外媒 / Wiki / fact-check / 中文主流）
  5. 排序：authority_score 降冪
  6. 截斷到 total_max

設計原則：
- 整段失敗回 []，個別 query / engine 失敗忽略不 raise
- 不寫 disk / 不打 LLM / 不動其他模組
- 純 stateless，可重複呼叫

依賴：
- web_scraper.search_duckduckgo / search_google_news / search_wiki_full
- concurrent.futures.ThreadPoolExecutor（標準庫）
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import web_scraper

logger = logging.getLogger("source_aggregator")


# ─────────────────────────────────────────────────────────────────────────────
# 1. URL canonical（去 tracking params）
# ─────────────────────────────────────────────────────────────────────────────

# 要剝掉的 query string keys（小寫比對）
_TRACKING_PARAM_PREFIXES: tuple[str, ...] = (
    "utm_",  # utm_source, utm_medium, utm_campaign, utm_term, utm_content
)
_TRACKING_PARAM_EXACT: frozenset[str] = frozenset(
    {
        "fbclid",  # Facebook click ID
        "gclid",  # Google click ID
        "gclsrc",
        "dclid",  # DoubleClick
        "msclkid",  # Microsoft / Bing
        "ref",  # generic referral
        "ref_src",
        "source",  # 一些 CMS 的追蹤 param
        "_ga",
        "yclid",  # Yandex
        "mc_cid",
        "mc_eid",  # Mailchimp
        "ocid",  # Microsoft news
    }
)


def _canonical_url(url: str) -> str:
    """Canonicalize URL：去掉追蹤參數、normalize host (小寫)、去掉 fragment。

    用來 dedup：兩個 URL 只差 utm/fbclid → 視為同一篇。
    失敗（empty / 非 http(s)）→ 回原 string。
    """
    if not url or not isinstance(url, str):
        return url or ""
    s = url.strip()
    if not s:
        return ""

    try:
        parsed = urlparse(s)
    except Exception:
        return s

    # 只處理 http(s)；其他 scheme（mailto, javascript:）原樣返
    if parsed.scheme not in ("http", "https"):
        return s

    # Host：小寫
    host = (parsed.netloc or "").lower()
    # 去掉預設 port（:80 for http, :443 for https）
    if host.endswith(":80") and parsed.scheme == "http":
        host = host[:-3]
    elif host.endswith(":443") and parsed.scheme == "https":
        host = host[:-4]

    # Path：移除 trailing slash（除非是根 /）
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Query：filter 掉 tracking param
    cleaned_query = ""
    if parsed.query:
        kept: list[tuple[str, str]] = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            kl = k.lower()
            if kl in _TRACKING_PARAM_EXACT:
                continue
            if any(kl.startswith(p) for p in _TRACKING_PARAM_PREFIXES):
                continue
            kept.append((k, v))
        if kept:
            cleaned_query = urlencode(kept, doseq=True)

    # Fragment 一律丟（同一篇文章的不同 anchor 視為同一篇）
    return urlunparse(
        (parsed.scheme, host, path, parsed.params, cleaned_query, "")
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Authority scoring
# ─────────────────────────────────────────────────────────────────────────────

# 知名西文新聞 / 通訊社 → +20
_TIER_INTL_NEWS: frozenset[str] = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "bbc.com",
        "bbc.co.uk",
        "cnn.com",
        "foxnews.com",
        "nytimes.com",
        "wsj.com",
        "bloomberg.com",
        "theguardian.com",
    }
)

# 中文主流媒體 → +10
_TIER_ZH_NEWS: frozenset[str] = frozenset(
    {
        "udn.com",
        "cna.com.tw",
        "chinatimes.com",
        "ltn.com.tw",
        "tvbs.com.tw",
        "ettoday.net",
        "yahoo.com",
        "tw.yahoo.com",
        "news.yahoo.com",
    }
)

# Fact check → +25
_TIER_FACT_CHECK: frozenset[str] = frozenset(
    {
        "snopes.com",
        "politifact.com",
        "factcheck.org",
    }
)

# Wikipedia → +15
_WIKI_HOST_RE = re.compile(r"(?:^|\.)wikipedia\.org$", re.IGNORECASE)

# .gov / .edu / .mil → +30（含子網域，如 cdc.gov.tw / mit.edu / nasa.gov）
_GOV_EDU_RE = re.compile(
    r"(?:^|\.)(?:gov|edu|mil)(?:\.[a-z]{2,3})?$", re.IGNORECASE
)

# 內容農場 / 不知名 → -10（小一份不完整黑名單，conservative）
_TIER_LOW_QUALITY: frozenset[str] = frozenset(
    {
        "kknews.cc",
        "every.tw",
        "buzzhand.com",
        "buzzlife.com.tw",
        "coco01.today",
        "life.tw",
        "secretchina.com",
    }
)


def _domain_of(url: str) -> str:
    """Return lowercased registrable host (host minus :port).

    For non-URL or empty → returns ''.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def _domain_authority(url: str) -> int:
    """Score URL by authority. Higher = more trustworthy.

    Layered logic（取 max 命中的那一條，互斥優先序）：
      .gov / .edu / .mil       → +30
      fact-check 站            → +25
      knwon intl 外媒          → +20
      Wikipedia                → +15
      中文主流                 → +10
      未匹配                   → 0
      已知低品質黑名單         → -10
    """
    host = _domain_of(url)
    if not host:
        return 0

    # 黑名單先檢（互斥於上面）
    if host in _TIER_LOW_QUALITY:
        return -10
    # 處理 host 含 sub-domain 黑名單（e.g. xx.kknews.cc）
    for low in _TIER_LOW_QUALITY:
        if host == low or host.endswith("." + low):
            return -10

    # gov / edu / mil（含 .gov.tw / .edu.tw）
    # 拆 dot 部件，再從尾巴最多 3 段比對
    parts = host.split(".")
    # 看尾段：'gov' / 'edu' / 'mil' 或 'gov.tw' / 'edu.tw'
    if len(parts) >= 2:
        last1 = parts[-1].lower()
        last2 = ".".join(parts[-2:]).lower() if len(parts) >= 2 else ""
        if last1 in ("gov", "edu", "mil"):
            return 30
        if last2 in (
            "gov.tw",
            "edu.tw",
            "gov.uk",
            "ac.uk",
            "edu.cn",
            "gov.cn",
            "gov.jp",
            "ac.jp",
            "edu.au",
            "gov.au",
            "edu.hk",
            "gov.hk",
        ):
            return 30

    # Fact check
    for fc in _TIER_FACT_CHECK:
        if host == fc or host.endswith("." + fc):
            return 25

    # 國際外媒
    for intl in _TIER_INTL_NEWS:
        if host == intl or host.endswith("." + intl):
            return 20

    # Wikipedia
    if _WIKI_HOST_RE.search(host):
        return 15

    # 中文主流
    for zh in _TIER_ZH_NEWS:
        if host == zh or host.endswith("." + zh):
            return 10

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Per-engine fetchers — 純 wrap web_scraper，加 source_query tag
# ─────────────────────────────────────────────────────────────────────────────


def _fetch_ddg(query: str, k: int) -> list[dict]:
    try:
        rows = web_scraper.search_duckduckgo(query, k=k) or []
    except Exception as e:  # noqa: BLE001
        logger.info("DDG search failed (%s): %s", query, e)
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        out.append(
            {
                "title": r.get("title") or "",
                "url": url,
                "snippet": r.get("snippet") or "",
                "source_query": query,
                "engine": "ddg",
            }
        )
    return out


def _fetch_gnews(query: str, k: int) -> list[dict]:
    try:
        rows = web_scraper.search_google_news(query, k=k) or []
    except Exception as e:  # noqa: BLE001
        logger.info("GoogleNews search failed (%s): %s", query, e)
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        if not url:
            continue
        # gnews 沒 snippet；用 source 名稱湊（caller 自己決定要不要用）
        snippet = r.get("snippet") or r.get("source") or ""
        out.append(
            {
                "title": r.get("title") or "",
                "url": url,
                "snippet": snippet,
                "source_query": query,
                "engine": "gnews",
            }
        )
    return out


def _fetch_wiki(title: str, lang: str) -> Optional[dict]:
    try:
        data = web_scraper.search_wiki_full(title, lang=lang)
    except Exception as e:  # noqa: BLE001
        logger.info("Wiki search failed (%s/%s): %s", title, lang, e)
        return None
    if not data:
        return None
    url = data.get("url") or ""
    if not url:
        return None
    return {
        "title": data.get("title") or title,
        "url": url,
        "snippet": (data.get("extract") or data.get("description") or "")[:280],
        "source_query": title,
        "engine": f"wiki_{lang}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Public API
# ─────────────────────────────────────────────────────────────────────────────


def aggregate_sources(
    queries: list[str], total_max: int = 18
) -> list[dict]:
    """V4 Step 3 entry point.

    Args:
        queries: 1..N 個 search query（已被 query expander 擴展過）
        total_max: 最終最多回幾筆（預設 18，>= 15 是合理上限）

    Returns:
        list of dicts，每筆 {
            'title': str,
            'url': str,        # canonical (utm/fbclid 已去除)
            'snippet': str,
            'source_query': str,  # 來自哪個 query
            'authority_score': int,
            'domain': str,
        }
        排序：authority_score 降冪。同分時保留發現順序（stable sort）。

    失敗（queries 空 / 全部 engine 都炸）→ []。
    """
    if not queries or not isinstance(queries, list):
        return []

    # 過濾空 query
    qs: list[str] = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if not qs:
        return []

    # 1. 平行打：每 (query, engine) 一個 task
    tasks: list[tuple[str, str, int]] = []  # (kind, arg, k)
    for q in qs:
        tasks.append(("ddg", q, 4))
        tasks.append(("gnews", q, 4))

    # Wiki 只用第一個 query（避免每 query 都打 wiki）
    primary_q = qs[0]

    raw_results: list[dict] = []

    # ThreadPool — 多 query × 2 engine + 2 wiki
    max_workers = max(4, min(16, len(tasks) + 2))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = []
        for kind, arg, k in tasks:
            if kind == "ddg":
                futures.append(ex.submit(_fetch_ddg, arg, k))
            elif kind == "gnews":
                futures.append(ex.submit(_fetch_gnews, arg, k))
        # Wiki en + zh
        futures.append(ex.submit(_fetch_wiki, primary_q, "en"))
        futures.append(ex.submit(_fetch_wiki, primary_q, "zh"))

        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                logger.info("aggregate task failed: %s", e)
                continue
            if not res:
                continue
            if isinstance(res, list):
                raw_results.extend(res)
            elif isinstance(res, dict):
                raw_results.append(res)

    if not raw_results:
        return []

    # 2. Canonical + dedup（host+path+cleaned-query 視為相同 → 留第一個）
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in raw_results:
        url = item.get("url") or ""
        canon = _canonical_url(url)
        if not canon:
            continue
        if canon in seen:
            continue
        seen.add(canon)
        # 寫回 canonical url 以供下游使用
        item["url"] = canon
        item["domain"] = _domain_of(canon)
        item["authority_score"] = _domain_authority(canon)
        # 移除 internal 欄位
        item.pop("engine", None)
        deduped.append(item)

    # 3. Sort by authority_score desc, stable
    deduped.sort(key=lambda r: r.get("authority_score", 0), reverse=True)

    # 4. Truncate
    if total_max and total_max > 0:
        deduped = deduped[:total_max]

    return deduped


__all__ = [
    "aggregate_sources",
    "_canonical_url",
    "_domain_authority",
    "_domain_of",
]
