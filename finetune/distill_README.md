# Daily Gemini Distillation Pipeline

## 為什麼要 daily distill？

LINE bot 至今累積 ~178 對話 pair，距離 fine-tune 門檻（3000+ pair）還差很遠。
Gemini free quota 一天只有 20 calls，無法 batch 一次跑完，所以採取
**慢慢累積**策略：每天 distill 10 條 user 訊息為 ideal reply。

## 估算

| 累積週期 | 累積 pair 數 |
|---------|-------------|
| 1 週     | ~70         |
| 1 個月   | ~300        |
| 半年     | ~1825       |
| **1 年** | **~3650**   |

加上現有 178 pair，約 **10 個月即可達 fine-tune 門檻**。

## 啟用

```bash
launchctl load ~/Library/LaunchAgents/com.andrew.line-bot-distill-daily.plist
```

每天 03:00 TW 自動跑一次。Mac 必須是醒著或有插電；睡眠期間錯過的會跳過，
但累積週期長無妨。

## 暫停

```bash
launchctl unload ~/Library/LaunchAgents/com.andrew.line-bot-distill-daily.plist
```

## 手動執行

```bash
# Mock mode（不耗 Gemini quota，測試流程）
.venv/bin/python finetune/distill_daily.py --mock

# Real mode（會 call Gemini）
.venv/bin/python finetune/distill_daily.py
```

## 累積進度監控

```bash
# 看總 pair 數
wc -l finetune/data/distilled.jsonl

# 看最近一筆
tail -1 finetune/data/distilled.jsonl | python -m json.tool

# 看已處理 message_id 數
python -c "import json; print(len(json.load(open('finetune/data/distilled_seen.json'))))"

# 看 launchd 跑得怎樣
tail -50 ~/Library/Logs/line_bot_distill_stdout.log
tail -50 ~/Library/Logs/line_bot_distill_stderr.log
```

## 檔案說明

| 路徑 | 用途 |
|------|------|
| `distill_daily.py`             | 主程式 |
| `data/distilled.jsonl`         | 累積的 pair（每行一筆 chat-format JSON） |
| `data/distilled_seen.json`     | 已處理 message_id list（含失敗 / SKIP） |
| `~/Library/LaunchAgents/com.andrew.line-bot-distill-daily.plist` | launchd schedule |

## Quota 保護

- 每天 hard cap 10 calls（`DAILY_LIMIT`）
- 跑前檢查 `main._quota_exhausted()`，若爆掉直接 skip
- 同一條 message_id 不會 re-distill（seen.json 紀錄）
- 失敗 / `[SKIP]` 也記錄，不重試（避免無限消耗）

## JSONL 格式

```json
{
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "metadata": {
    "message_id": "...",
    "group_id": "...",
    "user_id": "...",
    "distilled_at": 1714694400,
    "mock": false
  }
}
```

可直接用於 OpenAI / Anthropic / Gemini fine-tune 格式（messages array）。

## TODO（未來）

- 可考慮 quality filter：Gemini 自評 reply 品質 < 7 就丟掉
- 可加 deduplication：相似度太高的 user message 不重複 distill
- 達 3000 pair 後跑一次 train_lora.sh 看效果
