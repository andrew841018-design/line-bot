# Spark vs Python pipeline — 何時用哪一個

兩支 SFT 抽取 pipeline：

| 檔案                        | runtime | 適合資料量    | 適合場景 |
| --------------------------- | ------- | ------------- | -------- |
| `extract_data.py`           | Python + sqlite3 | < 100K rows | 日常迭代、快速 debug、CI、小資料集 |
| `spark_pipeline.py`         | PySpark local[*] | > 100K rows | future-proof 規模、需要 join/window 平行運算、parquet 產出給下游 ETL |

## 何時切換到 Spark

切去 `spark_pipeline.py` 的指標（任一達到即可）：

1. **資料量** — `context` 表 ≥ 100K rows，或 `raw_messages` ≥ 1M
2. **記憶體** — Python 版 `iter_pairs_from_context` 在 `fetchall()` 階段超過 driver RAM
3. **多 source merge** — 要把 `context` 跟 `raw_messages` 跟外部 parquet join
4. **下游需要 parquet** — 要餵 Spark MLlib / Delta Lake / 其他 Spark job
5. **平行 enrich** — jieba tokenize / classifier inference 要靠 multi-core scale

## 何時繼續用 Python

繼續用 `extract_data.py` 的時候：

- 資料還小（目前 30 rows，到 10K 都還很 OK）
- 在 CI / Kaggle notebook 跑（不想拖 200MB pyspark）
- 只是要看一眼 sample 對話
- 邏輯還在快速改（Spark UDF debug 痛苦）

## 用法

```bash
# Python（小資料）
python finetune/extract_data.py --db line_bot.db --out finetune/data/sft.jsonl

# Spark（大資料 / 未來）
python finetune/spark_pipeline.py --input line_bot.db --output finetune/data/spark_pairs.parquet
```

Spark 版輸出：
- `finetune/data/spark_pairs.parquet/` — Spark partitioned parquet（snappy）
- `finetune/data/spark_pairs.jsonl` — 跟 `extract_data.py` 同 schema 的 jsonl，可直接餵 train_lora.py

## Pipeline 邏輯對照

兩支同樣做：
1. 讀 `context (group_id, seq, role, text)`
2. partitionBy `group_id` orderBy `seq` 找 adjacent (user, bot) pair
3. clean_text（去 `[burst]` prefix、strip）
4. quality filter：min_user_len=3, min_bot_len=5, 跳系統訊息
5. dedup（Spark 用 sha2(user||||bot)，Python 用 set）
6. 輸出 messages-format jsonl

Spark 版額外：
- `pair_hash` 欄位（sha2-256）
- `user_tokens` / `bot_tokens`（jieba list）
- `user_token_est` / `bot_token_est` / `total_token_est`（cost 估算）
- 寫成 parquet 給下游 Spark / pandas / DuckDB 讀

## Spark local 設定

```python
SparkSession.builder \
    .master("local[*]") \
    .config("spark.driver.memory", "4g") \
    .config("spark.sql.shuffle.partitions", "8") \
    .getOrCreate()
```

- `local[*]` — 用所有 CPU core，不要起 cluster
- `spark.driver.memory=4g` — local mode 沒 executor，driver = 全部
- `shuffle.partitions=8` — local 不需要預設的 200，會拖慢
- 沒裝 Hadoop binary（`HADOOP_HOME` 不用設）— Spark 4.x 自帶 Java IO，純 local 不會 winutils 報錯

## 真實效能對比

跑 `python finetune/spark_pipeline.py` 跟 `python finetune/extract_data.py` 對同一個 `line_bot.db`：

- Python 版：適合 cold-start，<10K rows 通常 < 1 秒
- Spark 版：local 啟動 SparkSession 約 3-5 秒固定 overhead，但對 100K+ rows 的 window/group 運算會贏

實際數字以 `bench_results` 為準（執行 spark_pipeline.py 跟 extract_data.py 並計時）。

## 已知限制

- SQLite 沒 official Spark JDBC，所以走 `pandas.read_sql` → `createDataFrame`。資料量 > 5M rows 時要切 chunk 讀，或先 dump 成 parquet。
- jieba UDF 是 Python UDF，不是 vectorized；scale 到 cluster 時要改 pandas_udf。
- jsonl 輸出走 `toPandas()`，>1M rows 要改 `enriched.write.json()`（partitioned）。
