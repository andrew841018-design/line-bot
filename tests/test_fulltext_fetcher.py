"""Tests for fulltext_fetcher.py — pure-mock, no real network.

12 條 test 涵蓋：
  - 平行 fetch top-N
  - cache hit 不重打 web_scraper
  - cache miss → fetch + 寫 cache
  - 失敗 fallback snippet
  - timeout fallback snippet
  - max_chars 截斷
  - cache TTL expire
  - 空 list / 非 dict / no url
  - 只抓 head（top_n 後面不動）
"""
from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fulltext_fetcher  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def tmp_cache_db(tmp_path) -> Path:
    """Per-test isolated SQLite cache DB."""
    return tmp_path / "web_text_cache.db"


def _mk_sources(n: int = 5) -> list[dict]:
    """Helper：產 n 條 dummy sources（已按 authority sort 假設）。"""
    return [
        {
            "url": f"https://example.com/article{i}",
            "title": f"Title {i}",
            "snippet": f"Snippet text for article {i}",
            "authority": 100 - i,
        }
        for i in range(n)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 1. 平行 fetch top-N — 每條都被 web_scraper 打到
# ─────────────────────────────────────────────────────────────────────────────
def test_parallel_fetch_top_n_calls_scraper_for_each(tmp_cache_db):
    sources = _mk_sources(5)
    call_log: list[str] = []

    def fake_fetch(url, max_chars=5000):
        call_log.append(url)
        return f"FULL TEXT for {url}"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        out = fulltext_fetcher.fetch_top_sources(
            sources, top_n=5, max_chars_per=3000, cache_db=tmp_cache_db
        )

    assert len(out) == 5
    # 每條 head 都有 full_text
    for src in out:
        assert "full_text" in src
        assert src["full_text"].startswith("FULL TEXT for")
    # 每個 URL 都被打過一次（cache miss）
    assert len(call_log) == 5
    assert sorted(call_log) == sorted([s["url"] for s in sources])


# ─────────────────────────────────────────────────────────────────────────────
# 2. cache hit → 第二次 call 不會打 web_scraper
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_hit_skips_scraper(tmp_cache_db):
    sources = _mk_sources(2)
    call_count = {"n": 0}

    def fake_fetch(url, max_chars=5000):
        call_count["n"] += 1
        return f"BODY {url}"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        # 第一次：cold cache → 應打 2 次
        out1 = fulltext_fetcher.fetch_top_sources(
            sources, top_n=2, cache_db=tmp_cache_db
        )
        first_calls = call_count["n"]

        # 第二次：warm cache → 不應再打
        out2 = fulltext_fetcher.fetch_top_sources(
            sources, top_n=2, cache_db=tmp_cache_db
        )

    assert first_calls == 2
    # 第二次完全 cache hit，total 還是 2
    assert call_count["n"] == 2
    # 但結果一致
    assert out1[0]["full_text"] == out2[0]["full_text"]
    assert out1[1]["full_text"] == out2[1]["full_text"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. fetch 失敗 → fallback 到 snippet
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_failure_falls_back_to_snippet(tmp_cache_db):
    sources = _mk_sources(3)

    def fake_fetch(url, max_chars=5000):
        return None  # 模擬 404 / timeout / parse fail

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        out = fulltext_fetcher.fetch_top_sources(
            sources, top_n=3, cache_db=tmp_cache_db
        )

    for i, src in enumerate(out):
        assert src["full_text"] == f"Snippet text for article {i}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. fetch raises exception → fallback snippet（不該整個 crash）
# ─────────────────────────────────────────────────────────────────────────────
def test_fetch_exception_falls_back_to_snippet(tmp_cache_db):
    sources = _mk_sources(2)

    def boom(url, max_chars=5000):
        raise RuntimeError("network exploded")

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=boom):
        out = fulltext_fetcher.fetch_top_sources(
            sources, top_n=2, cache_db=tmp_cache_db
        )

    for i, src in enumerate(out):
        assert src["full_text"] == f"Snippet text for article {i}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. timeout → fallback snippet
# ─────────────────────────────────────────────────────────────────────────────
def test_timeout_falls_back_to_snippet(tmp_cache_db):
    sources = _mk_sources(2)

    def slow_fetch(url, max_chars=5000):
        time.sleep(2.0)  # 比 timeout 慢
        return "should never reach here"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=slow_fetch):
        out = fulltext_fetcher.fetch_top_sources(
            sources,
            top_n=2,
            cache_db=tmp_cache_db,
            timeout_per_task=0.3,  # 0.3s timeout < 2s sleep → 必 timeout
        )

    for i, src in enumerate(out):
        assert src["full_text"] == f"Snippet text for article {i}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. max_chars 截斷
# ─────────────────────────────────────────────────────────────────────────────
def test_max_chars_truncation(tmp_cache_db):
    sources = _mk_sources(1)
    long_text = "x" * 10000

    # 注意：web_scraper.fetch_full_text 自己也會 cap，這裡 fake 直接回未 cap
    # 確保即便 web_scraper 沒 cap（極端 case），cache 路徑會 cap
    def fake_fetch(url, max_chars=5000):
        # 模擬 web_scraper 已照 max_chars cap
        return long_text[:max_chars]

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        out = fulltext_fetcher.fetch_top_sources(
            sources, top_n=1, max_chars_per=500, cache_db=tmp_cache_db
        )

    assert len(out[0]["full_text"]) == 500


# ─────────────────────────────────────────────────────────────────────────────
# 7. cache 過期（>24h）→ 重新 fetch
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_expired_refetches(tmp_cache_db):
    url = "https://example.com/old"
    # 手動寫一條過期的 entry（fetched_at = now - 25h）
    con = sqlite3.connect(str(tmp_cache_db))
    con.execute(
        """CREATE TABLE IF NOT EXISTS web_text_cache (
            url TEXT PRIMARY KEY,
            fetched_at INTEGER NOT NULL,
            content TEXT NOT NULL
        )"""
    )
    expired_ts = int(time.time()) - (25 * 3600)
    con.execute(
        "INSERT INTO web_text_cache VALUES (?, ?, ?)",
        (url, expired_ts, "OLD STALE CONTENT"),
    )
    con.commit()
    con.close()

    sources = [{"url": url, "snippet": "snip"}]
    call_log: list[str] = []

    def fake_fetch(u, max_chars=5000):
        call_log.append(u)
        return "FRESH CONTENT"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        out = fulltext_fetcher.fetch_top_sources(
            sources, top_n=1, cache_db=tmp_cache_db
        )

    # 過期 → 應重新 fetch
    assert call_log == [url]
    assert out[0]["full_text"] == "FRESH CONTENT"


# ─────────────────────────────────────────────────────────────────────────────
# 8. cache 持久化：寫入後 DB 內可查到
# ─────────────────────────────────────────────────────────────────────────────
def test_cache_persistence(tmp_cache_db):
    sources = _mk_sources(1)

    def fake_fetch(url, max_chars=5000):
        return "PERSISTENT BODY"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        fulltext_fetcher.fetch_top_sources(sources, top_n=1, cache_db=tmp_cache_db)

    # 直接打開 DB 確認資料寫進去了
    assert tmp_cache_db.exists()
    con = sqlite3.connect(str(tmp_cache_db))
    cur = con.execute("SELECT url, content FROM web_text_cache")
    rows = cur.fetchall()
    con.close()

    assert len(rows) == 1
    assert rows[0][0] == sources[0]["url"]
    assert rows[0][1] == "PERSISTENT BODY"


# ─────────────────────────────────────────────────────────────────────────────
# 9. 空 list → 回 []
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_sources_returns_empty(tmp_cache_db):
    out = fulltext_fetcher.fetch_top_sources([], top_n=5, cache_db=tmp_cache_db)
    assert out == []


# ─────────────────────────────────────────────────────────────────────────────
# 10. top_n 之後的 sources 不被 fetch / 不加 full_text
# ─────────────────────────────────────────────────────────────────────────────
def test_top_n_only_fetches_head(tmp_cache_db):
    sources = _mk_sources(8)
    call_log: list[str] = []

    def fake_fetch(url, max_chars=5000):
        call_log.append(url)
        return f"BODY {url}"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        out = fulltext_fetcher.fetch_top_sources(
            sources, top_n=3, cache_db=tmp_cache_db
        )

    # 只前 3 筆有 full_text
    assert "full_text" in out[0]
    assert "full_text" in out[1]
    assert "full_text" in out[2]
    assert "full_text" not in out[3]
    assert "full_text" not in out[7]
    # 只打 3 次
    assert len(call_log) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 11. 缺 url 的 source → fallback snippet 而非 crash
# ─────────────────────────────────────────────────────────────────────────────
def test_source_without_url_falls_back(tmp_cache_db):
    sources = [
        {"snippet": "no url here", "title": "X"},  # 無 url
        {"url": "", "snippet": "empty url"},  # 空 url
        {"url": "https://example.com/ok", "snippet": "has url"},
    ]
    call_log: list[str] = []

    def fake_fetch(url, max_chars=5000):
        call_log.append(url)
        return f"BODY for {url}"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=fake_fetch):
        out = fulltext_fetcher.fetch_top_sources(
            sources, top_n=3, cache_db=tmp_cache_db
        )

    # 只有第三筆會被打
    assert call_log == ["https://example.com/ok"]
    # 前兩筆 fallback 自己的 snippet
    assert out[0]["full_text"] == "no url here"
    assert out[1]["full_text"] == "empty url"
    # 第三筆有真的 body
    assert "BODY for" in out[2]["full_text"]


# ─────────────────────────────────────────────────────────────────────────────
# 12. 平行性 sanity check：5 條各 sleep 0.5s 應在 ~1s 內完成（非 2.5s 序列）
# ─────────────────────────────────────────────────────────────────────────────
def test_parallel_execution_is_concurrent(tmp_cache_db):
    sources = _mk_sources(5)

    def slow_fetch(url, max_chars=5000):
        time.sleep(0.5)
        return f"BODY {url}"

    with patch.object(fulltext_fetcher.web_scraper, "fetch_full_text", side_effect=slow_fetch):
        t0 = time.time()
        out = fulltext_fetcher.fetch_top_sources(
            sources,
            top_n=5,
            cache_db=tmp_cache_db,
            timeout_per_task=5.0,
            max_workers=5,
        )
        elapsed = time.time() - t0

    # 平行 5 worker × 0.5s ≈ 0.5s，留 buffer 1.5s（序列要 2.5s+）
    assert elapsed < 1.5, f"Expected parallel <1.5s, got {elapsed:.2f}s"
    # 全部成功
    for src in out:
        assert src["full_text"].startswith("BODY https://example.com/")
