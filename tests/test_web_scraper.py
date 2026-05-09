"""Tests for web_scraper.py — pure-mock, no real network.

每個 source 至少一條 happy path + 至少一條 failure path。
共 14 條 test。
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Avoid global conftest.py loading main.py (which needs LINE env vars + heavy imports)
# Pytest auto-loads parent conftest, but since we set env in conftest itself it's OK.
# We only need web_scraper here.
import web_scraper  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Mock helpers
# ─────────────────────────────────────────────────────────────────────────────
def _mk_resp(status: int = 200, text: str = "", json_data=None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text
    if json_data is not None:
        r.json = MagicMock(return_value=json_data)
    else:
        r.json = MagicMock(side_effect=ValueError("no json"))
    return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Patch out the polite sleep so tests run fast."""
    monkeypatch.setattr(web_scraper.time, "sleep", lambda *_a, **_k: None)


# ─────────────────────────────────────────────────────────────────────────────
# 1. DuckDuckGo HTML scraping
# ─────────────────────────────────────────────────────────────────────────────
_DDG_HTML = """
<html><body>
<div class="result results_links results_links_deep web-result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">Title A</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Snippet A text</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">Title B</a>
  <a class="result__snippet">Snippet B text</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fc">Title C</a>
  <!-- no snippet -->
</div>
</body></html>
"""


def test_ddg_parses_top_k():
    with patch.object(web_scraper.requests, "post") as m_post:
        m_post.return_value = _mk_resp(200, _DDG_HTML)
        rows = web_scraper.search_duckduckgo("python", k=2)
    assert len(rows) == 2
    assert rows[0]["title"] == "Title A"
    assert rows[0]["url"] == "https://example.com/a"
    assert "Snippet A" in rows[0]["snippet"]
    assert rows[1]["title"] == "Title B"
    assert rows[1]["url"] == "https://example.com/b"


def test_ddg_handles_missing_snippet():
    with patch.object(web_scraper.requests, "post") as m_post:
        m_post.return_value = _mk_resp(200, _DDG_HTML)
        rows = web_scraper.search_duckduckgo("python", k=5)
    # 第三筆沒有 snippet → snippet=""
    third = [r for r in rows if r["title"] == "Title C"][0]
    assert third["snippet"] == ""


def test_ddg_empty_query_returns_empty():
    rows = web_scraper.search_duckduckgo("", k=5)
    assert rows == []


def test_ddg_http_error_returns_empty():
    with patch.object(web_scraper.requests, "post") as m_post:
        m_post.return_value = _mk_resp(503, "")
        rows = web_scraper.search_duckduckgo("xyz")
    assert rows == []


# ─────────────────────────────────────────────────────────────────────────────
# 2. Google News RSS
# ─────────────────────────────────────────────────────────────────────────────
_GNEWS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Google News</title>
  <item>
    <title>News headline 1</title>
    <link>https://news.example.com/1</link>
    <pubDate>Mon, 04 May 2026 10:00:00 GMT</pubDate>
    <source url="https://a.com">A News</source>
  </item>
  <item>
    <title>News headline 2</title>
    <link>https://news.example.com/2</link>
    <pubDate>Mon, 04 May 2026 11:00:00 GMT</pubDate>
    <source url="https://b.com">B News</source>
  </item>
</channel>
</rss>
"""


def test_gnews_parses_rss_items():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(200, _GNEWS_RSS)
        rows = web_scraper.search_google_news("台股", k=10)
    assert len(rows) == 2
    assert rows[0]["title"] == "News headline 1"
    assert rows[0]["url"] == "https://news.example.com/1"
    assert "May 2026" in rows[0]["published"]
    assert rows[0]["source"] == "A News"


def test_gnews_invalid_xml_returns_empty():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(200, "<not><valid></rss>")
        rows = web_scraper.search_google_news("xyz")
    assert rows == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. fetch_full_text — trafilatura primary + BS4 fallback
# ─────────────────────────────────────────────────────────────────────────────
_ARTICLE_HTML = """
<html><head><title>Sample</title></head>
<body>
  <nav>NAV LINKS</nav>
  <header>HEADER</header>
  <article>
    <h1>主標題</h1>
    <p>This is the first paragraph of the article body. It has substantive content
    that trafilatura should extract cleanly.</p>
    <p>Second paragraph with more meaningful text. The boilerplate on the side
    should be removed by the extractor.</p>
  </article>
  <footer>FOOTER ADS</footer>
</body></html>
"""


def test_fetch_full_text_uses_trafilatura():
    fake_extract = "主標題\nThis is the first paragraph...\nSecond paragraph..."
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(200, _ARTICLE_HTML)
        with patch("trafilatura.extract", return_value=fake_extract):
            text = web_scraper.fetch_full_text("https://x.com/article")
    assert text is not None
    assert "主標題" in text
    assert "first paragraph" in text


def test_fetch_full_text_bs4_fallback_when_trafilatura_returns_none():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(200, _ARTICLE_HTML)
        with patch("trafilatura.extract", return_value=None):
            text = web_scraper.fetch_full_text("https://x.com/article")
    # BS4 退路抽 body text，但會去除 nav/footer/header
    assert text is not None
    assert "first paragraph" in text
    assert "NAV LINKS" not in text
    assert "FOOTER ADS" not in text


def test_fetch_full_text_respects_max_chars():
    long_html = "<html><body><article>" + ("x" * 20000) + "</article></body></html>"
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(200, long_html)
        with patch("trafilatura.extract", return_value="x" * 20000):
            text = web_scraper.fetch_full_text("https://x.com", max_chars=500)
    assert text is not None
    assert len(text) == 500


def test_fetch_full_text_http_error_returns_none():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(404, "")
        text = web_scraper.fetch_full_text("https://x.com/missing")
    assert text is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Wikipedia summary
# ─────────────────────────────────────────────────────────────────────────────
_WIKI_JSON = {
    "title": "Python (程式語言)",
    "description": "高階通用程式語言",
    "extract": "Python 是一種廣泛使用的直譯式高階程式語言，由 Guido van Rossum 設計。",
    "content_urls": {
        "desktop": {"page": "https://zh.wikipedia.org/wiki/Python"}
    },
}


def test_wiki_summary_parses_json():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(200, "ok", json_data=_WIKI_JSON)
        out = web_scraper.search_wiki_full("Python", lang="zh")
    assert out is not None
    assert out["title"] == "Python (程式語言)"
    assert "直譯式" in out["extract"]
    assert out["url"] == "https://zh.wikipedia.org/wiki/Python"


def test_wiki_404_returns_none():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(404, "")
        out = web_scraper.search_wiki_full("不存在的詞")
    assert out is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. arXiv
# ─────────────────────────────────────────────────────────────────────────────
_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <title>Attention Is All You Need v2</title>
    <summary>We propose a new architecture...</summary>
    <author><name>Alice Author</name></author>
    <author><name>Bob Author</name></author>
    <link href="http://arxiv.org/abs/2401.12345v1" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/2401.12345v1" rel="related" type="application/pdf" title="pdf"/>
  </entry>
</feed>
"""


def test_arxiv_parses_atom_feed():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(200, _ARXIV_ATOM)
        rows = web_scraper.search_arxiv("transformer", k=5)
    assert len(rows) == 1
    r = rows[0]
    assert "Attention Is All You Need" in r["title"]
    assert r["authors"] == ["Alice Author", "Bob Author"]
    assert "new architecture" in r["abstract"]
    assert r["pdf_url"].endswith("/pdf/2401.12345v1")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Taiwan gov open data
# ─────────────────────────────────────────────────────────────────────────────
def test_gov_dataset_returns_json():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(
            200, "ok", json_data=[{"name": "AAA"}, {"name": "BBB"}]
        )
        out = web_scraper.scrape_taiwan_gov("12345")
    assert isinstance(out, list)
    assert out[0]["name"] == "AAA"


def test_gov_dataset_http_error_returns_none():
    with patch.object(web_scraper.requests, "get") as m_get:
        m_get.return_value = _mk_resp(500, "")
        out = web_scraper.scrape_taiwan_gov("12345")
    assert out is None


# ─────────────────────────────────────────────────────────────────────────────
# 7. Pipeline integration smoke
# ─────────────────────────────────────────────────────────────────────────────
def test_pipeline_combines_ddg_and_wiki(monkeypatch):
    monkeypatch.setattr(
        web_scraper,
        "search_duckduckgo",
        lambda q, k=5: [
            {"title": "T1", "url": "https://x.com/1", "snippet": "S1"}
        ],
    )
    monkeypatch.setattr(
        web_scraper, "fetch_full_text", lambda url, max_chars=5000: "FULL_BODY"
    )
    monkeypatch.setattr(
        web_scraper,
        "search_wiki_full",
        lambda title, lang="zh", with_full_text=False: {
            "title": "Foo",
            "description": "bar",
            "extract": "baz extract",
            "url": "https://wiki/Foo",
        },
    )
    out = web_scraper.web_search_pipeline("什麼是 Python", k=1)
    sources = [r["source"] for r in out]
    assert "duckduckgo" in sources
    assert "wikipedia" in sources
    ddg_row = next(r for r in out if r["source"] == "duckduckgo")
    assert "FULL_BODY" in ddg_row["content"]
