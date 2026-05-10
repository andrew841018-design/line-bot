"""Tests for message_classifier — pure functional, no Gemini call (mocked)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import message_classifier as mc


# ── classify_rule（命中關鍵字直接回）──────────────────────────────────────


def test_rule_video_youtube():
    assert mc.classify_rule("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "影片"


def test_rule_video_tiktok():
    assert mc.classify_rule("看這個 https://www.tiktok.com/@user/video/123") == "影片"


def test_rule_video_instagram_reel():
    assert mc.classify_rule("https://www.instagram.com/reel/xxx/") == "影片"


def test_rule_scam_keyword():
    assert mc.classify_rule("這個簡訊看起來是詐騙，不要點") == "警示"


def test_rule_scam_line_group():
    assert mc.classify_rule("有人加我 LINE 群叫我投資加密幣，假投資") == "警示"


def test_rule_finance_tsmc():
    assert mc.classify_rule("TSMC 漲到 1080 創新高") == "財經"


def test_rule_finance_etf():
    assert mc.classify_rule("0050 月配息要重新檢視") == "財經"


def test_rule_finance_btc():
    assert mc.classify_rule("比特幣再次站上 100k") == "財經"


def test_rule_health_blood_pressure():
    assert mc.classify_rule("血壓 140/90 該看醫生嗎") == "健康"


def test_rule_health_cold():
    assert mc.classify_rule("感冒喉嚨痛吃普拿疼") == "健康"


def test_rule_schedule_date_slash():
    assert mc.classify_rule("5/15 中午聚餐") == "行程"


def test_rule_schedule_next_week():
    assert mc.classify_rule("下週四下午 3 點開會") == "行程"


def test_rule_no_match_returns_None():
    assert mc.classify_rule("今天天氣很好") is None


def test_rule_empty():
    assert mc.classify_rule("") is None
    assert mc.classify_rule(None) is None  # type: ignore


# ── 優先順序：影片 > 警示 > 財經 > 健康 > 行程 ────────────────────────────


def test_priority_video_over_finance():
    """訊息含影片連結 + 財經詞，影片應優先。"""
    text = "TSMC 大漲 https://www.youtube.com/watch?v=abc"
    assert mc.classify_rule(text) == "影片"


def test_priority_scam_over_finance():
    """假投資 + 股票詞，警示應優先（防漏抓詐騙）。"""
    text = "詐騙集團 line 群叫我買股票"
    assert mc.classify_rule(text) == "警示"


# ── classify (main entry, rule first → fallback) ───────────────────────


def test_classify_rule_hit_skips_gemini():
    """Rule 命中時不應呼 Gemini。"""
    with patch.object(mc, "classify_gemini") as mock_gemini:
        result = mc.classify("TSMC 漲了")
    assert result == "財經"
    mock_gemini.assert_not_called()


def test_classify_no_rule_calls_gemini():
    """Rule 不命中時 fall through 到 Gemini。"""
    with patch.object(mc, "classify_gemini", return_value="健康") as mock_gemini:
        result = mc.classify("最近不太舒服")
    mock_gemini.assert_called_once()
    assert result == "健康"


def test_classify_gemini_returns_none_falls_to_default():
    """Rule 不中 + Gemini 回 None → 預設「其他」。"""
    with patch.object(mc, "classify_gemini", return_value=None):
        result = mc.classify("一般閒聊隨口說")
    assert result == "其他"


def test_classify_empty_returns_default():
    assert mc.classify("") == "其他"
    assert mc.classify(None) == "其他"  # type: ignore
    assert mc.classify("a") == "其他"  # too short


def test_classify_gemini_invalid_response_falls_to_default():
    """Gemini 回不在 CATEGORIES 的字串 → None → 預設「其他」。"""
    class FakeResp:
        text = "我不知道"
    with patch("gemini_client._lite_or_main", return_value=FakeResp()):
        # classify_gemini 應 None；classify 預設「其他」
        result = mc.classify("一般閒聊隨口說")
    assert result == "其他"


def test_classify_gemini_valid_response():
    class FakeResp:
        text = "財經"
    with patch("gemini_client._lite_or_main", return_value=FakeResp()):
        result = mc.classify_gemini("某個含糊的描述")
    assert result == "財經"


def test_classify_gemini_exception_returns_None():
    with patch("gemini_client._lite_or_main", side_effect=RuntimeError("oops")):
        result = mc.classify_gemini("某個訊息")
    assert result is None


# ── DB schema migration + update_category（用 tmp DB） ───────────────────


@pytest.fixture
def tmp_db(tmp_path):
    """Synthetic raw_messages DB（與 prod schema 一致）。"""
    db = tmp_path / "test_line_bot.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE raw_messages (
            group_id    TEXT NOT NULL,
            message_id  TEXT NOT NULL,
            user_id     TEXT,
            text        TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            PRIMARY KEY (group_id, message_id)
        )
    """)
    # Insert 4 rows: 2 today (財經 + 健康), 1 today no-cat, 1 yesterday (財經)
    now = int(time.time())
    yesterday = now - 86400 - 3600  # 25h ago to be safe
    rows = [
        ("g1", "m1", "u1", "TSMC 漲", now - 100),
        ("g1", "m2", "u1", "感冒了", now - 50),
        ("g1", "m3", "u1", "閒聊", now - 25),
        ("g1", "m4", "u1", "昨天 0050 配息", yesterday),
    ]
    conn.executemany(
        "INSERT INTO raw_messages VALUES (?,?,?,?,?)", rows
    )
    conn.commit()
    conn.close()
    return db


def test_ensure_schema_adds_category_column(tmp_db):
    """初次 ensure_schema 應加 category 欄位 + index。"""
    # Reset module-level cache so this test isolates schema migration
    mc._SCHEMA_ENSURED = False
    mc.ensure_schema(tmp_db)
    conn = sqlite3.connect(str(tmp_db))
    cur = conn.execute("PRAGMA table_info(raw_messages)")
    cols = [row[1] for row in cur.fetchall()]
    assert "category" in cols
    # idx exists
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_raw_messages_category'"
    )
    assert cur.fetchone() is not None
    conn.close()


def test_ensure_schema_idempotent(tmp_db):
    """跑兩次不應 raise。"""
    mc._SCHEMA_ENSURED = False
    mc.ensure_schema(tmp_db)
    mc.ensure_schema(tmp_db)  # should be no-op


def test_update_category_writes_db(tmp_db):
    mc._SCHEMA_ENSURED = False
    mc.ensure_schema(tmp_db)
    mc.update_category("g1", "m1", "財經", db_path=tmp_db)
    conn = sqlite3.connect(str(tmp_db))
    cur = conn.execute("SELECT category FROM raw_messages WHERE message_id='m1'")
    assert cur.fetchone()[0] == "財經"
    conn.close()


def test_update_category_unknown_message_silent(tmp_db):
    """寫不存在的 row → silent，不 raise。"""
    mc._SCHEMA_ENSURED = False
    mc.update_category("g1", "nonexistent", "財經", db_path=tmp_db)


# ── list_today_in_category / today_category_counts ──────────────────────


def test_list_today_in_category(tmp_db):
    mc._SCHEMA_ENSURED = False
    mc.ensure_schema(tmp_db)
    mc.update_category("g1", "m1", "財經", db_path=tmp_db)
    mc.update_category("g1", "m2", "健康", db_path=tmp_db)
    mc.update_category("g1", "m4", "財經", db_path=tmp_db)  # yesterday
    rows = mc.list_today_in_category("g1", "財經", db_path=tmp_db)
    # m4 是 yesterday，不該算今日
    assert len(rows) == 1
    assert rows[0]["message_id"] == "m1"
    assert rows[0]["text"] == "TSMC 漲"


def test_list_today_invalid_category_empty(tmp_db):
    rows = mc.list_today_in_category("g1", "不存在的類別", db_path=tmp_db)
    assert rows == []


def test_today_category_counts(tmp_db):
    mc._SCHEMA_ENSURED = False
    mc.ensure_schema(tmp_db)
    mc.update_category("g1", "m1", "財經", db_path=tmp_db)
    mc.update_category("g1", "m2", "健康", db_path=tmp_db)
    mc.update_category("g1", "m4", "財經", db_path=tmp_db)  # yesterday
    counts = mc.today_category_counts("g1", db_path=tmp_db)
    assert counts == {"財經": 1, "健康": 1}  # m4 yesterday excluded


# ── classify_async（thread fire-and-forget） ────────────────────────────


def test_classify_async_runs_in_thread(tmp_db, monkeypatch):
    """async 呼叫不阻塞，最終 DB 應寫入 category。"""
    monkeypatch.setattr(mc, "_DB_PATH", tmp_db)
    mc._SCHEMA_ENSURED = False

    mc.classify_async("g1", "m3", "TSMC 大漲")
    # wait for background thread
    time.sleep(0.5)
    conn = sqlite3.connect(str(tmp_db))
    cur = conn.execute("SELECT category FROM raw_messages WHERE message_id='m3'")
    cat = cur.fetchone()[0]
    conn.close()
    assert cat == "財經"


def test_classify_async_empty_inputs_silent():
    mc.classify_async("", "m1", "text")
    mc.classify_async("g1", "", "text")
    mc.classify_async("g1", "m1", "")
    mc.classify_async("g1", "m1", None)  # type: ignore
    # No exception expected
