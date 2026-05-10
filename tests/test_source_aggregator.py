"""Tests for source_aggregator.py — pure-mock，不打網路。

12 條 test，cover：
  1. 多 query 合併（每 query × DDG + gnews 都跑）
  2. URL canonical 去重（utm / fbclid 不同被視為同篇）
  3. authority sort：.gov 排第一
  4. authority sort：知名外媒（reuters）次之
  5. authority sort：不知名最後
  6. Wikipedia +15
  7. fact-check +25
  8. 中文主流 +10
  9. 低品質黑名單 -10
  10. queries 空 / 全空字串 → []
  11. Wiki 用 queries[0]，且 en / zh 都打
  12. total_max 截斷
  13. canonical url 去 utm_source / fbclid / gclid / ref / utm_*
  14. canonical url 不影響 fragment-less 純 URL
  15. engine 失敗（raise）整段不炸 → 仍回剩餘 source
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

# Ensure project root importable（同 test_web_scraper 的 pattern）
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import source_aggregator  # noqa: E402
from source_aggregator import (  # noqa: E402
    _canonical_url,
    _domain_authority,
    _domain_of,
    aggregate_sources,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _ddg_fake(query, k=4):
    """Stub for search_duckduckgo. Returns 1 result tagged with the query."""
    return [
        {
            "title": f"DDG result for {query}",
            "url": f"https://example.com/ddg/{query.replace(' ', '_')}",
            "snippet": f"snippet about {query}",
        }
    ]


def _gnews_fake(query, k=4):
    return [
        {
            "title": f"GNews result for {query}",
            "url": f"https://news.example.com/gnews/{query.replace(' ', '_')}",
            "published": "Mon, 01 Jan 2026 00:00:00 GMT",
            "source": "Example News",
        }
    ]


def _wiki_fake(title, lang="zh"):
    return {
        "title": title,
        "description": f"wiki desc for {title}",
        "extract": f"wiki extract content for {title}",
        "url": f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}",
    }


def _empty(*args, **kwargs):
    return []


def _none(*args, **kwargs):
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. 多 query 合併
# ─────────────────────────────────────────────────────────────────────────────


def test_aggregate_multiple_queries_calls_each_engine_per_query():
    """多 query → 每 query 都打 DDG + gnews（× len(queries)），wiki 只打 1 次（用 q[0]）"""
    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=_ddg_fake) as m_ddg, \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=_gnews_fake) as m_gn, \
         patch("source_aggregator.web_scraper.search_wiki_full", side_effect=_wiki_fake) as m_wiki:
        out = aggregate_sources(["foo", "bar", "baz"], total_max=20)

    # DDG 被 3 個 query 各打 1 次
    assert m_ddg.call_count == 3
    assert m_gn.call_count == 3
    # Wiki en + zh 各 1 次（總 2）
    assert m_wiki.call_count == 2
    # 結果含 9 個獨立 URL（3 q × (DDG + gnews) + 2 wiki）= 8
    assert len(out) == 8
    # 每筆都有必要欄位
    for r in out:
        assert "title" in r
        assert "url" in r
        assert "snippet" in r
        assert "source_query" in r
        assert "authority_score" in r
        assert "domain" in r


# ─────────────────────────────────────────────────────────────────────────────
# 2. Canonical URL 去重
# ─────────────────────────────────────────────────────────────────────────────


def test_dedup_by_canonical_strips_utm_and_fbclid():
    """同 host + path 但帶不同 utm / fbclid → 只保留一筆"""
    def ddg(query, k=4):
        return [
            {
                "title": "Article",
                "url": "https://news.com/story?utm_source=fb&utm_medium=cpc",
                "snippet": "x",
            }
        ]

    def gnews(query, k=4):
        return [
            {
                "title": "Same Article",
                "url": "https://news.com/story?fbclid=abc123&gclid=def",
                "published": "",
                "source": "",
            }
        ]

    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=ddg), \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=gnews), \
         patch("source_aggregator.web_scraper.search_wiki_full", return_value=None):
        out = aggregate_sources(["q1"], total_max=10)

    # 同 host+path → 視為同一篇，只保留第一個
    news_rows = [r for r in out if r["domain"] == "news.com"]
    assert len(news_rows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. Authority sort：.gov 排第一
# ─────────────────────────────────────────────────────────────────────────────


def test_gov_domain_ranked_first():
    """`.gov` URL 應排在所有其他 source 之前。"""
    def ddg(query, k=4):
        return [
            {"title": "Random blog", "url": "https://randomblog.io/x", "snippet": ""},
            {"title": "CDC", "url": "https://www.cdc.gov/notes/abc", "snippet": ""},
        ]

    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=ddg), \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=_empty), \
         patch("source_aggregator.web_scraper.search_wiki_full", return_value=None):
        out = aggregate_sources(["health"], total_max=5)

    assert len(out) >= 2
    assert out[0]["domain"] == "www.cdc.gov"
    assert out[0]["authority_score"] == 30


# ─────────────────────────────────────────────────────────────────────────────
# 4. 知名外媒中游
# ─────────────────────────────────────────────────────────────────────────────


def test_intl_news_ranked_above_unknown_below_gov():
    def ddg(query, k=4):
        return [
            {"title": "Blog", "url": "https://nobody.example.com/", "snippet": ""},
            {"title": "Reuters", "url": "https://www.reuters.com/world/", "snippet": ""},
            {"title": "EDU", "url": "https://www.mit.edu/research", "snippet": ""},
        ]

    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=ddg), \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=_empty), \
         patch("source_aggregator.web_scraper.search_wiki_full", return_value=None):
        out = aggregate_sources(["q"], total_max=5)

    domains = [r["domain"] for r in out]
    # MIT EDU (+30) > Reuters (+20) > unknown blog (0)
    assert domains[0] == "www.mit.edu"
    assert domains[1] == "www.reuters.com"
    assert domains[-1] == "nobody.example.com"


# ─────────────────────────────────────────────────────────────────────────────
# 5. 不知名最後
# ─────────────────────────────────────────────────────────────────────────────


def test_unknown_domain_zero_score_ranked_last():
    def ddg(query, k=4):
        return [
            {"title": "Yahoo TW", "url": "https://tw.yahoo.com/news/abc", "snippet": ""},
            {"title": "Random", "url": "https://www.somerandomsite.org/post", "snippet": ""},
        ]

    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=ddg), \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=_empty), \
         patch("source_aggregator.web_scraper.search_wiki_full", return_value=None):
        out = aggregate_sources(["q"], total_max=5)

    assert out[0]["domain"] == "tw.yahoo.com"  # +10
    assert out[0]["authority_score"] == 10
    assert out[-1]["domain"] == "www.somerandomsite.org"
    assert out[-1]["authority_score"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Wikipedia +15
# ─────────────────────────────────────────────────────────────────────────────


def test_wikipedia_score_15():
    assert _domain_authority("https://en.wikipedia.org/wiki/Foo") == 15
    assert _domain_authority("https://zh.wikipedia.org/wiki/Bar") == 15


# ─────────────────────────────────────────────────────────────────────────────
# 7. Fact-check +25
# ─────────────────────────────────────────────────────────────────────────────


def test_fact_check_score_25():
    assert _domain_authority("https://www.snopes.com/foo") == 25
    assert _domain_authority("https://www.politifact.com/check/x") == 25
    assert _domain_authority("https://www.factcheck.org/y") == 25


# ─────────────────────────────────────────────────────────────────────────────
# 8. 中文主流 +10
# ─────────────────────────────────────────────────────────────────────────────


def test_zh_mainstream_score_10():
    assert _domain_authority("https://udn.com/news/story/x") == 10
    assert _domain_authority("https://www.cna.com.tw/news/y") == 10
    assert _domain_authority("https://news.ltn.com.tw/article/123") == 10
    assert _domain_authority("https://tw.yahoo.com/news/abc") == 10


# ─────────────────────────────────────────────────────────────────────────────
# 9. 低品質黑名單 -10
# ─────────────────────────────────────────────────────────────────────────────


def test_low_quality_negative_score():
    assert _domain_authority("https://kknews.cc/post/abc") == -10
    assert _domain_authority("https://www.buzzhand.com/post/123") == -10
    assert _domain_authority("https://en.kknews.cc/something") == -10


# ─────────────────────────────────────────────────────────────────────────────
# 10. queries 空 → []
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_queries_returns_empty():
    assert aggregate_sources([]) == []
    assert aggregate_sources(["", "  ", "\t"]) == []
    assert aggregate_sources(None) == []  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# 11. Wiki 用 queries[0]，en / zh 都打
# ─────────────────────────────────────────────────────────────────────────────


def test_wiki_uses_first_query_both_langs():
    captured: list[tuple[str, str]] = []

    def wiki_capture(title, lang="zh"):
        captured.append((title, lang))
        return _wiki_fake(title, lang=lang)

    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=_empty), \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=_empty), \
         patch("source_aggregator.web_scraper.search_wiki_full", side_effect=wiki_capture):
        aggregate_sources(["primary topic", "second q"], total_max=10)

    titles = [t for t, _l in captured]
    langs = sorted([l for _t, l in captured])
    assert all(t == "primary topic" for t in titles)
    assert langs == ["en", "zh"]


# ─────────────────────────────────────────────────────────────────────────────
# 12. total_max 截斷
# ─────────────────────────────────────────────────────────────────────────────


def test_total_max_truncates():
    def ddg(query, k=4):
        return [
            {"title": f"r{i}", "url": f"https://site{i}.com/x", "snippet": ""}
            for i in range(10)
        ]

    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=ddg), \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=_empty), \
         patch("source_aggregator.web_scraper.search_wiki_full", return_value=None):
        out = aggregate_sources(["q1", "q2"], total_max=5)

    assert len(out) == 5


# ─────────────────────────────────────────────────────────────────────────────
# 13. _canonical_url 去 tracking params
# ─────────────────────────────────────────────────────────────────────────────


def test_canonical_url_strips_tracking_params():
    base = "https://example.com/article"
    cases = [
        f"{base}?utm_source=fb",
        f"{base}?utm_medium=cpc&utm_campaign=x",
        f"{base}?fbclid=123",
        f"{base}?gclid=abc",
        f"{base}?ref=newsletter",
        f"{base}?utm_source=fb&fbclid=abc&gclid=def",
        f"{base}#section1",
        f"{base}/",  # trailing slash 應 normalize
        base,
    ]
    canonicals = {_canonical_url(u) for u in cases}
    # 全部都應該 collapse 到同一個 canonical
    assert len(canonicals) == 1


def test_canonical_url_keeps_real_query_params():
    """非追蹤 param（如 ?id=123）必須保留。"""
    url = "https://example.com/page?id=123&utm_source=fb"
    canon = _canonical_url(url)
    assert "id=123" in canon
    assert "utm_source" not in canon


# ─────────────────────────────────────────────────────────────────────────────
# 14. _domain_of helper 正確性
# ─────────────────────────────────────────────────────────────────────────────


def test_domain_of_helper():
    assert _domain_of("https://www.cdc.gov/path") == "www.cdc.gov"
    assert _domain_of("HTTPS://EXAMPLE.COM/A") == "example.com"
    assert _domain_of("https://example.com:8080/x") == "example.com"
    assert _domain_of("") == ""
    assert _domain_of("not a url") == ""


# ─────────────────────────────────────────────────────────────────────────────
# 15. Engine raise 不炸整段
# ─────────────────────────────────────────────────────────────────────────────


def test_engine_exception_does_not_break_aggregation():
    """DDG raise 時 → 仍回 gnews + wiki 的結果。"""
    def ddg_raise(query, k=4):
        raise RuntimeError("DDG broke")

    with patch("source_aggregator.web_scraper.search_duckduckgo", side_effect=ddg_raise), \
         patch("source_aggregator.web_scraper.search_google_news", side_effect=_gnews_fake), \
         patch("source_aggregator.web_scraper.search_wiki_full", side_effect=_wiki_fake):
        out = aggregate_sources(["topic"], total_max=10)

    # gnews + wiki en + wiki zh = 3 sources
    assert len(out) == 3
    # 沒有 DDG 那筆
    assert all("ddg/" not in r["url"] for r in out)
