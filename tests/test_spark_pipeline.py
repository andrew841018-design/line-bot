"""Tests for finetune/spark_pipeline.py — in-memory Spark + temp SQLite."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

# 確保 finetune/ 可被 import（layout：repo_root / finetune / spark_pipeline.py）
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FINETUNE = os.path.join(ROOT, "finetune")
if FINETUNE not in sys.path:
    sys.path.insert(0, FINETUNE)

pyspark = pytest.importorskip("pyspark")

from pyspark.sql import SparkSession  # noqa: E402

import spark_pipeline as sp  # noqa: E402


# ---------- fixtures ----------


@pytest.fixture(scope="module")
def spark():
    s = (
        SparkSession.builder.appName("test_spark_pipeline")
        .master("local[2]")
        .config("spark.driver.memory", "1g")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.ui.showConsoleProgress", "false")
        .getOrCreate()
    )
    yield s
    s.stop()


@pytest.fixture
def tiny_db(tmp_path):
    """建小 SQLite DB，含 user/bot adjacent pair + 邊界 case。"""
    db = tmp_path / "tiny.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE context (
            group_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            PRIMARY KEY (group_id, seq))"""
    )
    rows = [
        # 正常 pair
        ("g1", 1, "user", "今天天氣怎麼樣？"),
        ("g1", 2, "bot", "今天台北晴朗，氣溫25度左右。"),
        # 重複 pair（dedup 應該砍掉一份）
        ("g1", 3, "user", "今天天氣怎麼樣？"),
        ("g1", 4, "bot", "今天台北晴朗，氣溫25度左右。"),
        # bot 太短 — filter 掉
        ("g1", 5, "user", "你好"),
        ("g1", 6, "bot", "嗨"),
        # bot 內部訊息 — filter 掉
        ("g1", 7, "user", "幫我查股票"),
        ("g1", 8, "bot", "Gemini 額度用完，請等到明天早上"),
        # seq 不連續 — 不算 pair
        ("g1", 9, "user", "另一則訊息"),
        ("g1", 11, "bot", "另一則回覆要夠長才會被收"),
        # 不同 group 也要正常 pair
        ("g2", 1, "user", "[burst]\n群組訊息範例文字"),
        ("g2", 2, "bot", "好的，我看到您的訊息了。"),
        # bot→user 順序錯 — 不收
        ("g2", 3, "bot", "這是 bot 開頭，不能配對。"),
        ("g2", 4, "user", "這是 user 在後"),
    ]
    conn.executemany(
        "INSERT INTO context (group_id, seq, role, text) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(db)


# ---------- pure Python helpers ----------


def test_clean_text_strips_burst_prefix():
    assert sp.clean_text("[burst]\nhello") == "hello"
    assert sp.clean_text("  spaced  ") == "spaced"
    assert sp.clean_text(None) == ""


def test_quality_filters():
    assert sp.is_quality_user("今天天氣")
    assert not sp.is_quality_user("a")  # 太短
    assert not sp.is_quality_user("https://example.com")  # 純 URL
    assert not sp.is_quality_user("...")  # 純標點

    assert sp.is_quality_bot("這是夠長的回覆")
    assert not sp.is_quality_bot("嗨")  # 太短
    assert not sp.is_quality_bot("Gemini 免費層已經用完了")  # 內部訊息


def test_jieba_and_token_estimate():
    toks = sp.jieba_tokens("今天天氣很好")
    assert isinstance(toks, list) and len(toks) > 0
    assert sp.estimate_token_count("") == 0
    assert sp.estimate_token_count("中文五個字") >= 5


# ---------- Spark integration ----------


def test_load_context_df(spark, tiny_db):
    df = sp.load_context_df(spark, tiny_db)
    assert df.count() == 14
    assert set(df.columns) == {"group_id", "seq", "role", "text"}


def test_build_pairs_df_window_logic(spark, tiny_db):
    df = sp.load_context_df(spark, tiny_db)
    pairs = sp.build_pairs_df(df)
    pdf = pairs.toPandas()
    # 預期 4 個 adjacent (user→bot, seq 連續) pair：
    # g1: (1,2), (3,4), (5,6), (7,8) — quality filter 還沒下
    # g2: (1,2)
    # g1 (9,11) seq 跳號不算
    # g2 (3,4) bot→user 順序錯不算
    assert len(pdf) == 5
    assert set(pdf["group_id"].unique()) == {"g1", "g2"}


def test_full_pipeline_dedup_and_quality(spark, tiny_db, tmp_path):
    out_parquet = str(tmp_path / "out.parquet")
    rows_in, rows_out, elapsed, ppath, jpath = sp.run_pipeline(
        db_path=tiny_db,
        out_parquet=out_parquet,
        driver_memory="1g",
        spark=spark,
    )
    assert rows_in == 14
    # 有 5 個 raw pair，刪掉：1 重複、1 bot 太短、1 bot 內部訊息 → 剩 2
    # 但 g1 (3,4) 跟 (1,2) 文字一模一樣 → dedup 砍掉一個
    # 最終：g1 (1,2) + g2 (1,2) = 2
    assert rows_out == 2
    assert elapsed > 0
    assert os.path.isdir(ppath)
    assert os.path.isfile(jpath)

    # 驗 jsonl 內容
    import json

    with open(jpath, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 2
    for obj in lines:
        assert obj["messages"][0]["role"] == "user"
        assert obj["messages"][1]["role"] == "assistant"


def test_enrich_adds_jieba_columns(spark, tiny_db):
    df = sp.load_context_df(spark, tiny_db)
    pairs = sp.build_pairs_df(df)
    cleaned = sp.apply_quality_and_clean(pairs)
    enriched = sp.enrich_with_jieba(cleaned)
    cols = set(enriched.columns)
    for c in (
        "user_tokens",
        "bot_tokens",
        "user_token_est",
        "bot_token_est",
        "total_token_est",
        "pair_hash",
    ):
        assert c in cols
    row = enriched.first()
    assert row is not None
    assert row["total_token_est"] >= 1
    assert isinstance(row["user_tokens"], list)


def test_dedup_hash_stable(spark):
    """同樣 (user, bot) 文字應產生同樣 pair_hash。"""
    from pyspark.sql import Row

    rows = [
        Row(group_id="g", user_seq=1, user_text_raw="hi", bot_text_raw="hello there"),
        Row(group_id="g", user_seq=99, user_text_raw="hi", bot_text_raw="hello there"),
    ]
    pairs = spark.createDataFrame(rows)
    cleaned = sp.apply_quality_and_clean(pairs, min_user_len=2, min_bot_len=5)
    # 兩個 row 應 dedup 成 1
    assert cleaned.count() == 1
