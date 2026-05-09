#!/usr/bin/env python3
"""
spark_pipeline.py — PySpark 版的 SFT pair 抽取 pipeline（local mode）。

設計目標：
- future-proof 大資料量（>100K rows）— 用 Spark Window function 平行做 adjacent pair 配對
- 100% 本機跑（local[*]，driver memory 4g），不依賴 Hadoop / S3 / cluster
- 與既有 `extract_data.py` 邏輯對齊（context 表 → user/bot adjacent pair → 品質 filter → dedup）

Pipeline 步驟：
1. SparkSession（local[*], driver 4g）
2. JDBC 讀 SQLite `context` 表（fallback：sqlite3 → pandas → spark createDataFrame）
3. Window partitionBy(group_id) orderBy(seq) — lead() 拿下一 row 的 role/text/seq 做 adjacent pair
4. Filter 出 (user, bot) seq 連續 pair
5. clean_text + quality filter（min_user_len / min_bot_len）
6. dedup hash(user_text || "" || bot_text) — Spark sha2
7. jieba enrich — UDF 算 user_tokens / bot_tokens / token_count_estimate
8. 輸出 parquet（snappy）+ jsonl

CLI：
    python finetune/spark_pipeline.py --input line_bot.db --output finetune/data/spark_pairs.parquet
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from typing import Optional

# Spark imports — defer 到實際用到時才 import，避免 module-level import 在沒裝 pyspark 的環境炸掉
# （測試 import spark_pipeline 時還能讀到 helpers）

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.normpath(os.path.join(HERE, "..", "line_bot.db"))
DEFAULT_OUT = os.path.join(HERE, "data", "spark_pairs.parquet")

# 品質規則（對齊 extract_data.py）
BOT_INTERNAL_PATTERNS = [
    r"Gemini.*額度.*用完",
    r"免費層.*已經用完",
    r"^\(系統\)",
    r"^\[系統\]",
    r"請等到明天早上",
    r"aistudio\.google\.com",
    r"^✅.*restart",
]
BOT_INTERNAL_RE = re.compile("|".join(BOT_INTERNAL_PATTERNS), re.IGNORECASE)
EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    r"\U0001F0A0-\U0001F0FF✀-➿]+"
)
URL_RE = re.compile(r"https?://\S+|www\.\S+")
PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$")
BURST_PREFIX_RE = re.compile(r"^\[burst\]\s*", re.IGNORECASE)


# -------- 純 Python helpers（給 UDF 跟單元測試直接呼叫） --------


def clean_text(text: Optional[str]) -> str:
    """去 burst marker + strip。None 安全。"""
    if text is None:
        return ""
    return BURST_PREFIX_RE.sub("", text).strip()


def is_quality_user(text: str, min_len: int = 3) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < min_len:
        return False
    stripped = EMOJI_RE.sub("", t)
    stripped = URL_RE.sub("", stripped)
    if len(stripped.strip()) < min_len:
        return False
    if PUNCT_ONLY_RE.match(t):
        return False
    return True


def is_quality_bot(text: str, min_len: int = 5) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < min_len:
        return False
    if BOT_INTERNAL_RE.search(t):
        return False
    return True


def jieba_tokens(text: str) -> list[str]:
    """jieba cut → list[str]。lazy import，jieba 沒裝時 fallback split。"""
    if not text:
        return []
    try:
        import jieba

        return [t for t in jieba.lcut(text, cut_all=False) if t.strip()]
    except Exception:
        # fallback：粗略 char-level
        return [c for c in text if c.strip()]


def estimate_token_count(text: str) -> int:
    """估 LLM token 數（粗）— Chinese ≈ 1 char/token，English ≈ 0.25 word/token。
    這只是 heuristic，給 batch / cost 估算用。"""
    if not text:
        return 0
    # 中文字元
    cn = sum(1 for c in text if "一" <= c <= "鿿")
    # 其他 = ascii word-ish
    other = max(0, len(text) - cn)
    return cn + max(1, other // 4)


# -------- Spark pipeline --------


def build_spark(driver_memory: str = "4g", app_name: str = "line_bot_sft_pipeline"):
    """建 local SparkSession。每個呼叫都重用 active session（getOrCreate）。

    會 addPyFile(__file__)，這樣 worker 反序列化 UDF 時找得到本模組。
    """
    from pyspark.sql import SparkSession

    spark = (
        SparkSession.builder.appName(app_name)
        .master("local[*]")
        .config("spark.driver.memory", driver_memory)
        .config("spark.sql.shuffle.partitions", "8")  # local 不需要 200
        .config("spark.ui.showConsoleProgress", "false")
        .config("spark.log.level", "WARN")
        .getOrCreate()
    )
    _ensure_module_on_workers(spark)
    return spark


def _ensure_module_on_workers(spark) -> None:
    """讓 worker 找得到本模組（UDF pickle 會帶 module 名）。

    對 local[*] 走 addPyFile(__file__) 就夠；走 cluster 也是同樣 API。
    """
    try:
        spark.sparkContext.addPyFile(os.path.abspath(__file__))
    except Exception:
        # addPyFile 失敗（例如測試 fixture 自己建 SparkSession 已經 add 過）— 忽略
        pass


def load_context_df(spark, db_path: str):
    """讀 SQLite context 表 → Spark DataFrame。

    SQLite 沒 official Spark JDBC，所以走 sqlite3 → pandas → createDataFrame。
    對 100M 級資料量還 OK；超過要切 chunk 或改 Postgres。
    """
    import pandas as pd
    from pyspark.sql.types import (
        IntegerType,
        StringType,
        StructField,
        StructType,
    )

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        pdf = pd.read_sql_query(
            "SELECT group_id, seq, role, text FROM context", conn
        )
    finally:
        conn.close()

    schema = StructType(
        [
            StructField("group_id", StringType(), nullable=False),
            StructField("seq", IntegerType(), nullable=False),
            StructField("role", StringType(), nullable=False),
            StructField("text", StringType(), nullable=True),
        ]
    )
    # pandas dtype 對齊
    pdf["group_id"] = pdf["group_id"].astype(str)
    pdf["seq"] = pdf["seq"].astype(int)
    pdf["role"] = pdf["role"].astype(str)
    pdf["text"] = pdf["text"].fillna("").astype(str)
    return spark.createDataFrame(pdf, schema=schema)


def build_pairs_df(df):
    """
    用 Window function 配 adjacent (user, bot) pair。

    partitionBy(group_id) orderBy(seq) → lead(role/text/seq) 拿下一筆。
    pair 條件：role='user' AND next_role='bot' AND next_seq = seq + 1。
    """
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    w = Window.partitionBy("group_id").orderBy("seq")

    enriched = (
        df.withColumn("next_role", F.lead("role", 1).over(w))
        .withColumn("next_text", F.lead("text", 1).over(w))
        .withColumn("next_seq", F.lead("seq", 1).over(w))
    )

    pairs = enriched.filter(
        (F.col("role") == F.lit("user"))
        & (F.col("next_role") == F.lit("bot"))
        & (F.col("next_seq") == F.col("seq") + 1)
    ).select(
        F.col("group_id"),
        F.col("seq").alias("user_seq"),
        F.col("text").alias("user_text_raw"),
        F.col("next_text").alias("bot_text_raw"),
    )
    return pairs


def apply_quality_and_clean(pairs, min_user_len: int = 3, min_bot_len: int = 5):
    """clean + quality filter + dedup。回傳 cleaned DF。"""
    from pyspark.sql import SparkSession
    from pyspark.sql import functions as F
    from pyspark.sql.types import BooleanType, StringType

    _ensure_module_on_workers(SparkSession.getActiveSession() or pairs.sparkSession)

    clean_udf = F.udf(clean_text, StringType())
    user_ok_udf = F.udf(lambda t: is_quality_user(t or "", min_user_len), BooleanType())
    bot_ok_udf = F.udf(lambda t: is_quality_bot(t or "", min_bot_len), BooleanType())

    cleaned = (
        pairs.withColumn("user_text", clean_udf(F.col("user_text_raw")))
        .withColumn("bot_text", clean_udf(F.col("bot_text_raw")))
        .filter(user_ok_udf(F.col("user_text")))
        .filter(bot_ok_udf(F.col("bot_text")))
        .withColumn(
            "pair_hash",
            F.sha2(
                F.concat_ws("", F.col("user_text"), F.col("bot_text")), 256
            ),
        )
        .dropDuplicates(["pair_hash"])
        .select(
            "group_id",
            "user_seq",
            "user_text",
            "bot_text",
            "pair_hash",
        )
    )
    return cleaned


def enrich_with_jieba(cleaned):
    """加 jieba tokens 跟 token count estimate。"""
    from pyspark.sql import SparkSession
    from pyspark.sql import functions as F
    from pyspark.sql.types import ArrayType, IntegerType, StringType

    _ensure_module_on_workers(SparkSession.getActiveSession() or cleaned.sparkSession)

    tok_udf = F.udf(jieba_tokens, ArrayType(StringType()))
    count_udf = F.udf(estimate_token_count, IntegerType())

    return (
        cleaned.withColumn("user_tokens", tok_udf(F.col("user_text")))
        .withColumn("bot_tokens", tok_udf(F.col("bot_text")))
        .withColumn("user_token_est", count_udf(F.col("user_text")))
        .withColumn("bot_token_est", count_udf(F.col("bot_text")))
        .withColumn(
            "total_token_est",
            F.col("user_token_est") + F.col("bot_token_est"),
        )
    )


def write_outputs(enriched, parquet_path: str, jsonl_path: Optional[str] = None):
    """
    寫 parquet（snappy 壓縮）+ jsonl。

    Spark 預設寫成 directory（part-files）；對 future-proof 大資料量這是對的，
    不要強制 coalesce(1)。jsonl 走 pandas 寫單檔（local 場景方便消費）。
    """
    parquet_dir = parquet_path
    os.makedirs(os.path.dirname(parquet_dir) or ".", exist_ok=True)

    (
        enriched.write.mode("overwrite")
        .option("compression", "snappy")
        .parquet(parquet_dir)
    )

    if jsonl_path is None:
        jsonl_path = parquet_path.rstrip("/").rstrip(".parquet") + ".jsonl"

    # jsonl：toPandas 對 local + <100K 資料 OK；超過要 enriched.write.json()
    pdf = enriched.select("user_text", "bot_text").toPandas()
    os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        import json as _json

        for _, row in pdf.iterrows():
            obj = {
                "messages": [
                    {"role": "user", "content": row["user_text"]},
                    {"role": "assistant", "content": row["bot_text"]},
                ]
            }
            f.write(_json.dumps(obj, ensure_ascii=False) + "\n")

    return parquet_dir, jsonl_path


def run_pipeline(
    db_path: str,
    out_parquet: str,
    out_jsonl: Optional[str] = None,
    driver_memory: str = "4g",
    min_user_len: int = 3,
    min_bot_len: int = 5,
    spark=None,
):
    """End-to-end runner。回傳 (rows_in, rows_out, elapsed_sec, parquet_path, jsonl_path)。"""
    own_spark = spark is None
    if own_spark:
        spark = build_spark(driver_memory=driver_memory)
    else:
        _ensure_module_on_workers(spark)
    t0 = time.perf_counter()
    try:
        df = load_context_df(spark, db_path)
        rows_in = df.count()
        pairs = build_pairs_df(df)
        cleaned = apply_quality_and_clean(pairs, min_user_len, min_bot_len)
        enriched = enrich_with_jieba(cleaned)
        rows_out = enriched.count()
        parquet_path, jsonl_path = write_outputs(enriched, out_parquet, out_jsonl)
        elapsed = time.perf_counter() - t0
        return rows_in, rows_out, elapsed, parquet_path, jsonl_path
    finally:
        if own_spark:
            spark.stop()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="PySpark SFT pair extraction")
    p.add_argument("--input", default=DEFAULT_DB, help="SQLite DB path")
    p.add_argument(
        "--output", default=DEFAULT_OUT, help="Parquet output dir"
    )
    p.add_argument(
        "--jsonl",
        default=None,
        help="JSONL output path（預設：output 改 .jsonl）",
    )
    p.add_argument("--driver-memory", default="4g")
    p.add_argument("--min-user-len", type=int, default=3)
    p.add_argument("--min-bot-len", type=int, default=5)
    args = p.parse_args(argv)

    rows_in, rows_out, elapsed, parquet_path, jsonl_path = run_pipeline(
        db_path=args.input,
        out_parquet=args.output,
        out_jsonl=args.jsonl,
        driver_memory=args.driver_memory,
        min_user_len=args.min_user_len,
        min_bot_len=args.min_bot_len,
    )

    print("=== Spark pipeline summary ===")
    print(f"input rows : {rows_in}")
    print(f"output rows: {rows_out}")
    print(f"elapsed    : {elapsed:.2f}s")
    print(f"parquet    : {parquet_path}")
    print(f"jsonl      : {jsonl_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
