"""Tests for embedding_recall — semantic memory retrieval.

Uses a temp SQLite file per test so we never touch the production line_bot.db.
ST model is loaded lazily — these tests will trigger an actual model load on
first call; expect ~3-5s on the first test, cached after.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def er(monkeypatch, tmp_path):
    """Isolated DB + freshly-initialized schema for each test."""
    db = tmp_path / "test_recall.db"

    import memory
    import embedding_recall

    monkeypatch.setattr(memory, "_DB_PATH", db)
    monkeypatch.setattr(embedding_recall, "_DB_PATH", db)
    memory._init_db()
    return embedding_recall


# ── index_message ──────────────────────────────────────────────────────────


def test_index_skips_short_text(er):
    assert er.index_message("M1", "G1", "hi") is False
    assert er.index_message("M2", "G1", "") is False
    assert er.index_message("M3", "G1", "   ") is False


def test_index_skips_placeholders(er):
    for ph in ("[圖片]", "[影片]", "[音訊]", "[語音留言]", "[sticker]"):
        assert er.index_message(f"M_{ph}", "G1", ph) is False


def test_index_skips_when_ids_missing(er):
    assert er.index_message("", "G1", "夠長的測試訊息內容") is False
    assert er.index_message("M1", "", "夠長的測試訊息內容") is False


def test_index_writes_with_model_tag_and_dim(er):
    ok = er.index_message("M1", "G1", "今天天氣很好啊我來說個故事")
    assert ok is True
    c = sqlite3.connect(str(er._DB_PATH))
    row = c.execute(
        "SELECT model_name, dim, is_bot, backend FROM embeddings WHERE message_id='M1'"
    ).fetchone()
    assert row[0] == er.MODEL_TAG
    assert row[1] == er.DIM
    assert row[2] == 0
    assert row[3] == "sentence-transformers"


def test_index_marks_bot_flag(er):
    er.index_message("B1", "G1", "咪寶的回覆內容夠長一些", is_bot=True)
    c = sqlite3.connect(str(er._DB_PATH))
    row = c.execute("SELECT is_bot FROM embeddings WHERE message_id='B1'").fetchone()
    assert row[0] == 1


# ── retrieve ───────────────────────────────────────────────────────────────


def test_retrieve_filters_out_legacy_tfidf_rows(er):
    """Old tfidf rows (model_name='') must not be returned by new ST retrieve."""
    c = sqlite3.connect(str(er._DB_PATH))
    c.execute(
        "INSERT INTO embeddings"
        "(message_id, group_id, text, embedding, backend, dim, "
        " created_at, model_name, is_bot) "
        "VALUES (?, ?, ?, ?, 'tfidf', 100, 1000, '', 0)",
        ("OLD1", "G1", "舊的訊息內容老舊不該命中", b"\x00" * 400),
    )
    c.close()
    er.index_message("NEW1", "G1", "今天討論機器學習很有趣")
    hits = er.retrieve("G1", "機器學習")
    assert all(h["message_id"] != "OLD1" for h in hits)


def test_retrieve_bot_only_filters(er):
    er.index_message("U1", "G1", "今天討論保險的問題", is_bot=False)
    er.index_message("B1", "G1", "保險條款本質上是合約", is_bot=True)
    hits_bot = er.retrieve("G1", "保險條款", bot_only=True, threshold=0.0)
    ids = {h["message_id"] for h in hits_bot}
    assert "U1" not in ids
    assert "B1" in ids


def test_retrieve_respects_topk(er):
    for i, text in enumerate([
        "保險條款設計問題討論",
        "投資 ETF 風險心得",
        "醫療糾紛賠償案例",
        "保險爭議案例分析",
        "投資理財心得分享",
    ]):
        er.index_message(f"M{i}", "G1", text)
    hits = er.retrieve("G1", "保險", k=2, threshold=0.0)
    assert len(hits) <= 2


def test_retrieve_threshold_filters_low_scores(er):
    er.index_message("M1", "G1", "完全無關的隨機文字內容陳述")
    hits = er.retrieve("G1", "保險條款", threshold=0.95)  # impossibly high
    assert len(hits) == 0


def test_retrieve_group_partition(er):
    er.index_message("M1", "G1", "群組一的訊息內容")
    er.index_message("M2", "G2", "群組二的訊息內容")
    hits_g1 = er.retrieve("G1", "群組", threshold=0.0)
    assert all(h["message_id"] != "M2" for h in hits_g1)


def test_retrieve_empty_query_returns_empty(er):
    er.index_message("M1", "G1", "夠長的測試訊息內容")
    assert er.retrieve("G1", "") == []
    assert er.retrieve("G1", "   ") == []


# ── retrieve_case_pairs ────────────────────────────────────────────────────


def test_retrieve_case_pairs_links_user_to_next_bot_reply(er):
    """Past similar user query → return that user msg + the bot's next reply."""
    import memory
    memory.log_raw_message("G1", "M1", "user1", "請問保險條款怎麼看才對")
    memory.log_raw_message("G1", "M2", "__bot__", "保險條款要看除外責任跟給付條件兩塊")
    er.index_message("M1", "G1", "請問保險條款怎麼看才對", is_bot=False)
    er.index_message("M2", "G1", "保險條款要看除外責任跟給付條件兩塊", is_bot=True)
    pairs = er.retrieve_case_pairs("G1", "保險條款怎麼選看不懂幫幫忙", threshold=0.0)
    assert len(pairs) >= 1
    assert "保險條款" in pairs[0]["user_query"]
    assert "除外責任" in pairs[0]["bot_reply"]


def test_retrieve_case_pairs_skips_user_without_bot_followup(er):
    """User asked but bot never replied → pair filtered out."""
    import memory
    memory.log_raw_message("G1", "M1", "user1", "群組裡沒被回答的孤兒問題內容")
    er.index_message("M1", "G1", "群組裡沒被回答的孤兒問題內容", is_bot=False)
    pairs = er.retrieve_case_pairs("G1", "孤兒問題", threshold=0.0)
    assert pairs == []


# ── format helpers ─────────────────────────────────────────────────────────


def test_format_recall_block_empty(er):
    assert er.format_recall_block([]) == ""


def test_format_recall_block_renders(er):
    block = er.format_recall_block([
        {"text": "保險條款很重要", "is_bot": False, "created_at": 1000, "score": 0.6, "message_id": "M1"},
        {"text": "我之前提過 ETF", "is_bot": True, "created_at": 2000, "score": 0.5, "message_id": "M2"},
    ])
    assert "相關歷史對話" in block
    assert "群組之前有人說" in block
    assert "咪寶之前" in block
    assert "保險條款很重要" in block


def test_format_case_block_renders(er):
    block = er.format_case_block([
        {
            "user_query": "保險條款怎麼看",
            "bot_reply": "看除外責任跟給付條件",
            "score": 0.6,
            "created_at": 1000,
        },
    ])
    assert "類似 case" in block
    assert "user 當時問" in block
    assert "咪寶當時答" in block
