# Lightning AI Studios LoRA Fine-tune（Modal/Kaggle 第三備援）

Modal $30 + Kaggle 30hr/週 都用完時的後備。Lightning AI Studios **每月 22hr 24GB GPU 免費**，剛好 fit 14B 模型 4-bit LoRA。

## 三家對比

| | Modal | Kaggle | Lightning AI |
|---|---|---|---|
| 免費額度 | $30 一次性 | 30 hr / **週** 永久 | 22 hr / **月** 永久 |
| GPU | A10G / A100 / H100 | P100 / T4 (16GB) | L4 / A10G (24GB) |
| 模式 | 純 CLI 自動 (Modal SDK) | Web UI Notebook（Run All）| Web Studio（Python script）|
| 模型尺寸 | 任 (3B / 14B / 70B) | 3B（16GB 限）| **14B 4-bit** sweet spot |
| Wall-clock (1k pairs / 3ep) | ~30min H100 / ~2hr A10G | ~2-3hr P100 | ~4-6hr L4 |
| 上手成本 | 高（要懂 Modal SDK + Volume）| 中（要 dataset + notebook）| 低（拖檔 + python script）|
| 排程關掉瀏覽器繼續 | OK | OK（Run & Save Version）| OK（Studio idle 30min 暫停但保檔）|
| 真正限制 | 燒完 $30 就要付錢 | 30hr/週硬上限 | 22hr/月硬上限 |
| 適合場景 | 自動化每月例行訓練 | 大量 epoch / 慢慢跑 | 想跑 14B 的單次實驗 |

## 14B 4-bit on 24GB 估算

- Qwen2.5-14B fp16 載入 ~28 GB → 必須 `bnb_4bit_quant_type=nf4` → ~7 GB
- LoRA r=16 q/k/v/o trainable ~50 MB
- activation + gradient checkpointing working ~10 GB
- 總 footprint ~17-19 GB → 24 GB 留 5-7 GB buffer，**不會 OOM**

bs=2、grad_accum=8（effective bs=16）對齊 Modal/Kaggle，每 epoch ~1.5-2 hr，3 epoch ~5 hr，22hr/月 配額剛好可跑 4 次。

## Step-by-step

### (a) 印上手流程 [本機 CLI]
```bash
python finetune/lightning_setup.py             # 完整指引
python finetune/lightning_setup.py --check-data # 只查 distilled.jsonl 狀態
python finetune/lightning_setup.py --print-cmd  # 額外印 Studio 內單行指令
```

### (b) Lightning AI 開 Studio [web]
1. https://lightning.ai/sign-up（免信用卡）
2. New Studio → Template「Code (Python)」 或「PyTorch」
3. Machine → GPU → 選 **L4 (24GB)** 或 **A10G (24GB)**
4. 上傳 `distilled.jsonl` + `lightning_train.py`（拖檔 / git clone repo 二選一）

### (c) 跑訓練 [Studio terminal]
```bash
pip install -q transformers peft accelerate datasets bitsandbytes safetensors sentencepiece
python finetune/lightning_train.py
```
可關瀏覽器，背景跑。idle 30min 會暫停 Studio（檔案保留，不算 hr）。

### (d) 下載 adapter [Studio web]
訓練完 `./lora_out.zip` 出現在右側檔案 panel → Download → 本機解壓到 `finetune/adapters_lightning/`。

### (e) Acceptance gate [本機 CLI]
```bash
python finetune/acceptance_gate.py \
    --adapter finetune/adapters_lightning/<unzipped> \
    --baseline finetune/eval_results/baseline.json
```
4 條 gate（violation / chinese / rule0 / judge）全過 → 自動寫 `ACTIVE_ADAPTER` 進 `local_llm_config.py`，跟 Modal / Kaggle 流程一致。

## 跟其他兩家對齊的 hyperparams

| | Modal/Kaggle (3B) | Lightning AI (14B) |
|---|---|---|
| base | Qwen2.5-3B-Instruct | Qwen2.5-14B-Instruct |
| LoRA r / alpha | 16 / 32 | 16 / 32 |
| target modules | q/k/v/o_proj | q/k/v/o_proj |
| epochs | 3 | 3 |
| per-device batch | 4 | 2（model 大 4x，bs 砍半免 OOM） |
| grad_accum | 4 | 8（維持 effective bs=16）|
| lr | 2e-4 | 2e-4 |
| max_len | 2048 | 2048 |
| 4-bit quant | (Kaggle 是 nf4) | nf4 + bnb_8bit_paged_adamw |

## 注意事項

- **本機 inference 對應**：用 14B fine-tune 的 adapter，本機 `main.py` 也要能載 14B base model 才對得上。如果本機 GPU/Mac 載不動 14B，這條路不適合 production，只當實驗用。
- **不要**在本機裝 lightning SDK 跑 remote training（目前 Lightning AI 沒這個 API；都是去 web Studio 操作）。
- 22hr/月 idle 不算（暫停 = 0 hr），實際有效 GPU 時間用得很 lean。

## Troubleshooting

| 症狀 | 處理 |
|---|---|
| OOM 在載 model 階段 | 換 7B / 3B（改 `LIGHTNING_BASE_MODEL` env var）|
| OOM 在訓練步 | `BATCH_SIZE` 從 2 → 1，`GRAD_ACCUMULATION` 從 8 → 16 |
| 22hr 用完 | 等下個月 1 號重置；想加額度要付 Pro $25/月 |
| Studio idle 被暫停 | 點 Resume，從 last checkpoint 繼續（Trainer save 是 epoch 級）|
| 找不到 distilled.jsonl | 路徑優先序：`./distilled.jsonl` → `./data/distilled.jsonl` → `finetune/data/distilled.jsonl` → `/teamspace/studios/this_studio/distilled.jsonl` |
