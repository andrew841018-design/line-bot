"""Tests for media_cache CRUD in memory.py (Phase 1)."""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest


@pytest.fixture
def media_cache_memory(monkeypatch):
    """Isolate against tmp SQLite file by reloading memory module."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    from config import settings
    monkeypatch.setattr(settings, "sqlite_path", tmp.name)

    import memory
    importlib.reload(memory)

    yield memory

    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    importlib.reload(memory)


def test_compute_sha256_stable(media_cache_memory):
    m = media_cache_memory
    h1 = m.compute_sha256(b"hello world")
    h2 = m.compute_sha256(b"hello world")
    assert h1 == h2
    assert len(h1) == 64


def test_compute_sha256_different_for_different_bytes(media_cache_memory):
    m = media_cache_memory
    assert m.compute_sha256(b"a") != m.compute_sha256(b"b")


def test_lookup_returns_none_when_empty(media_cache_memory):
    m = media_cache_memory
    assert m.lookup_media_cache("g1", "image", "deadbeef") is None


def test_insert_then_lookup(media_cache_memory):
    m = media_cache_memory
    sha = m.compute_sha256(b"test")
    cache_id = m.insert_media_cache("g1", "image", sha, "desc text", "reply text")
    assert cache_id is not None
    hit = m.lookup_media_cache("g1", "image", sha)
    assert hit is not None
    assert hit["last_reply"] == "reply text"
    assert hit["description"] == "desc text"
    assert hit["seen_count"] == 1


def test_cross_group_isolation(media_cache_memory):
    """PK 含 group_id：同 sha 不同 group 兩筆獨立（防 cross-group leak）。"""
    m = media_cache_memory
    sha = m.compute_sha256(b"shared")
    m.insert_media_cache("g1", "image", sha, "d", "r1")

    assert m.lookup_media_cache("g2", "image", sha) is None
    assert m.lookup_media_cache("g1", "image", sha) is not None

    cache_id_g2 = m.insert_media_cache("g2", "image", sha, "d", "r2")
    assert cache_id_g2 is not None
    hit_g2 = m.lookup_media_cache("g2", "image", sha)
    assert hit_g2["last_reply"] == "r2"


def test_media_type_isolation(media_cache_memory):
    """image vs video 同 sha 獨立 cache。"""
    m = media_cache_memory
    sha = m.compute_sha256(b"x")
    m.insert_media_cache("g1", "image", sha, "d", "image reply")
    assert m.lookup_media_cache("g1", "video", sha) is None


def test_bump_seen_increments(media_cache_memory):
    m = media_cache_memory
    sha = m.compute_sha256(b"test")
    cache_id = m.insert_media_cache("g1", "image", sha, "d", "r")
    m.bump_media_cache_seen(cache_id)
    hit = m.lookup_media_cache("g1", "image", sha)
    assert hit["seen_count"] == 2

    m.bump_media_cache_seen(cache_id)
    hit = m.lookup_media_cache("g1", "image", sha)
    assert hit["seen_count"] == 3


def test_empty_reply_rejected(media_cache_memory):
    """空 reply 不該寫 cache (Phase 1 quality gate)。"""
    m = media_cache_memory
    sha = m.compute_sha256(b"x")
    assert m.insert_media_cache("g1", "image", sha, "d", "") is None
    assert m.insert_media_cache("g1", "image", sha, "d", "   ") is None
    assert m.lookup_media_cache("g1", "image", sha) is None


def test_insert_idempotent_on_duplicate(media_cache_memory):
    """同 (group_id, media_type, sha) 重複 insert：INSERT OR IGNORE，不爆。"""
    m = media_cache_memory
    sha = m.compute_sha256(b"x")
    m.insert_media_cache("g1", "image", sha, "d1", "r1")
    m.insert_media_cache("g1", "image", sha, "d2", "r2")
    hit = m.lookup_media_cache("g1", "image", sha)
    assert hit is not None
    assert hit["last_reply"] == "r1"  # 第一次寫入保留
