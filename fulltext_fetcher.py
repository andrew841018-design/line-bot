"""V4 pipeline Step 4 — parallel full-text fetcher with SQLite caching.

對 top-N authority sources 平行抽完整正文（trafilatura），給 LLM 用全文不只
snippet。

公開 API：
  - fetch_top_sources(sources, top_n=5, max_chars_per=3000) -> list[dict]
      每個 source dict 加 `full_text` field（fetch 失敗則 fallback 原 snippet）

Caching：
  - SQLite at `finetune/data/web_text_cache.db`
  - schema: (url PRIMARY KEY, fetched_at INTEGER, content TEXT)
  - 24h 內 same URL 直接讀 cache
  - WAL mode + atomic INSERT OR REPLACE 確保 concurrent safe

設計：
  - ThreadPoolExecutor max_workers=5、per-task timeout 12s
  - 失敗 / timeout 一律 graceful fallback to snippet（不 raise）
  - cache hit → 不打 web_scraper.fetch_full_text
  - 不動 web_scraper.py
"""
from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeoutError
from pathlib import Path
from typing import Any, Optional

import web_scraper

logger = logging.getLogger("fulltext_fetcher")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
DEFAULT_CACHE_DB = HERE / "finetune" / "data" / "web_text_cache.db"

# 24 小時內 same URL 視為 cache 有效
_CACHE_TTL_SEC = 24 * 60 * 60
# 平行 worker 數
_MAX_WORKERS = 5
# 每條 fetch 的硬 timeout（秒）
_PER_TASK_TIMEOUT = 12.0


# ─────────────────────────────────────────────────────────────────────────────
# SQLite cache layer
# ─────────────────────────────────────────────────────────────────────────────
def _connect(db_path: Path | str) -> sqlite3.Connection:
    """Open / create the SQLite cache DB. Auto-creates parent dir + schema.

    WAL mode for concurrent read tolerance（multi-thread fetch 可能同時讀寫）。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=10.0)
    # WAL: 多 writer 衝突容忍度高
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        # 部分檔系統不支援 WAL（e.g. NFS） — 忽略，回 default journal
        pass
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS web_text_cache (
            url TEXT PRIMARY KEY,
            fetched_at INTEGER NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    con.commit()
    return con


def _cache_lookup(
    db_path: Path | str, url: str, ttl_sec: int = _CACHE_TTL_SEC
) -> Optional[str]:
    """Return cached content if URL hit within TTL, else None."""
    if not url:
        return None
    try:
        con = _connect(db_path)
    except sqlite3.Error as e:
        logger.info("cache connect failed: %s", e)
        return None
    try:
        cur = con.execute(
            "SELECT fetched_at, content FROM web_text_cache WHERE url = ?",
            (url,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        fetched_at, content = row
        try:
            fetched_at = int(fetched_at)
        except (TypeError, ValueError):
            return None
        if (int(time.time()) - fetched_at) > ttl_sec:
            return None
        return content if content else None
    except sqlite3.Error as e:
        logger.info("cache lookup error: %s", e)
        return None
    finally:
        con.close()


def _cache_store(db_path: Path | str, url: str, content: str) -> None:
    """Atomic INSERT OR REPLACE — 用 transaction 確保 multi-thread 安全。"""
    if not url or not content:
        return
    try:
        con = _connect(db_path)
    except sqlite3.Error as e:
        logger.info("cache connect-for-write failed: %s", e)
        return
    try:
        con.execute(
            "INSERT OR REPLACE INTO web_text_cache (url, fetched_at, content) "
            "VALUES (?, ?, ?)",
            (url, int(time.time()), content),
        )
        con.commit()
    except sqlite3.Error as e:
        logger.info("cache store error: %s", e)
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Per-source fetch（會被 ThreadPoolExecutor 包起來平行）
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_one(
    url: str,
    max_chars: int,
    db_path: Path | str,
) -> Optional[str]:
    """Single-URL fetch：先查 cache，cache miss 才打 web_scraper。

    成功 → 回 truncated text（並寫 cache）
    失敗 → 回 None（caller fallback 到 snippet）
    """
    if not url or not url.strip():
        return None

    # Cache hit first
    cached = _cache_lookup(db_path, url)
    if cached:
        # cache 內存的可能是更長版本 — 重新 cap 一次
        if len(cached) > max_chars:
            return cached[:max_chars]
        return cached

    # Cache miss — call web_scraper
    try:
        text = web_scraper.fetch_full_text(url, max_chars=max_chars)
    except Exception as e:
        # web_scraper 內部已吞例外回 None，但 defense-in-depth 包一層
        logger.info("fetch_full_text raised (%s): %s", url, e)
        return None

    if not text:
        return None
    # Persist to cache（即使後續 caller cap 也存 full version 內 max_chars）
    _cache_store(db_path, url, text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def fetch_top_sources(
    sources: list[dict],
    top_n: int = 5,
    max_chars_per: int = 3000,
    cache_db: Path | str | None = None,
    timeout_per_task: float = _PER_TASK_TIMEOUT,
    max_workers: int = _MAX_WORKERS,
) -> list[dict]:
    """Parallel full-text enrichment for the top-N sources.

    Args:
      sources: 已經按 authority sort 的 list[dict]，每筆需有 `url`，可有 `snippet`。
      top_n: 只對前 N 條打全文（其餘維持原樣，不加 full_text）。
      max_chars_per: 每篇 cap 的字數上限。
      cache_db: SQLite 檔路徑，default `finetune/data/web_text_cache.db`。
      timeout_per_task: 單條 fetch 的 hard timeout（秒）。
      max_workers: ThreadPoolExecutor pool size。

    Returns:
      原 list 的淺 copy（每筆 dict 也是淺 copy），前 top_n 個會多一個
      `full_text` field：
        - fetch 成功 → 全文（max_chars_per 內）
        - fetch 失敗 → fallback 該 source 的 `snippet`（沒有則空字串）

    錯誤處理：
      - 整體不 raise；任何 thread 例外 / timeout → 該 source fallback snippet
      - sources 為空或非 list → 回 []
    """
    if not sources or not isinstance(sources, list):
        return []

    db_path = Path(cache_db) if cache_db is not None else DEFAULT_CACHE_DB

    # 淺 copy 原 list（避免 mutate caller 物件）
    out: list[dict] = []
    for item in sources:
        if isinstance(item, dict):
            out.append(dict(item))
        else:
            out.append({"_invalid": True, "raw": item})

    # 切 head / tail
    head = out[:top_n]
    # 只對前 top_n 抓全文；後面的不動

    # 平行打 head
    if not head:
        return out

    urls: list[tuple[int, str]] = []
    for idx, src in enumerate(head):
        url = src.get("url") if isinstance(src, dict) else None
        if isinstance(url, str) and url.strip():
            urls.append((idx, url.strip()))

    if not urls:
        # 沒有任何可抓的 URL → 對 head 全部 fallback snippet
        for src in head:
            src["full_text"] = src.get("snippet", "") or ""
        return out

    # 啟 thread pool
    results: dict[int, Optional[str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_fetch_one, url, max_chars_per, db_path): idx
            for idx, url in urls
        }
        for fut in list(future_to_idx.keys()):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result(timeout=timeout_per_task)
            except FutTimeoutError:
                logger.info("fetch timeout for idx=%d", idx)
                # 嘗試 cancel（無法 hard-kill 但會 mark）
                fut.cancel()
                results[idx] = None
            except Exception as e:
                logger.info("fetch worker error idx=%d: %s", idx, e)
                results[idx] = None

    # 寫回 head — 失敗 fallback snippet
    for idx, src in enumerate(head):
        text = results.get(idx)
        if text:
            src["full_text"] = text
        else:
            src["full_text"] = src.get("snippet", "") or ""

    return out


__all__ = [
    "fetch_top_sources",
    "DEFAULT_CACHE_DB",
]
