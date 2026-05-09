# Kaggle 免費 GPU LoRA Fine-tune（Modal 替代）

Modal $30 用完後的備援。Kaggle **永久每週 30hr P100/T4 免費**。

## 對比

| | Modal | Kaggle |
|---|---|---|
| 免費 | $30 一次性 | 30hr/週**永久** |
| GPU | A10G/A100/H100 | P100/T4 (16GB) |
| 模式 | 純 CLI 自動 | web UI（手動 Run All） |
| Wall-clock | ~30min (H100) | ~2-3hr (P100) |

**Trade-off**：Kaggle Notebook 是 web-based，**無法純 CLI 自動跑訓練**。下面 (a)(d)(e) 是 CLI，(b)(c) 是 web UI。

## Step-by-step

### (a) 上傳資料 [CLI]
```bash
pip install kaggle  # 首次
python finetune/kaggle_upload.py  # kaggle.json 沒設會印教學 URL
```
首次 create dataset，後續 version-bump → `https://www.kaggle.com/datasets/<you>/linebot-distilled`

### (b) Kaggle 開新 Notebook [web]
1. https://www.kaggle.com/code → **New Notebook**
2. Settings → Accelerator → **GPU P100**
3. Add Data → 搜 `linebot-distilled` → Add
4. File → Import Notebook → 上傳 `finetune/kaggle_train.ipynb`

### (c) 跑訓練 [web]
**Save Version → Save & Run All (Commit)**。可關瀏覽器，不中斷。跑完務必 publish。
預期：1000 pairs / 3 epochs / P100 ≈ 2-3hr。

### (d) 下載 adapter [CLI]
```bash
# notebook id 在網址：https://www.kaggle.com/code/<user>/<slug>
python finetune/kaggle_download_adapter.py --notebook <user>/<slug>
```
解壓到 `finetune/adapters_kaggle/`。

### (e) Acceptance gate [CLI]
```bash
python finetune/kaggle_download_adapter.py \
    --notebook <user>/<slug> --run-gate \
    --baseline finetune/eval_results/baseline.json
```
4 條 gate 全過 → 自動寫 `ACTIVE_ADAPTER` 進 `local_llm_config.py`。失敗不動 config。

## Troubleshooting
- `kaggle.json` 沒設 → 兩個 py 報錯都會印教學
- OOM → 把 cell 5 `per_device_train_batch_size` 從 4 降到 2
- 30hr/wk 用完 → 等下週一 GMT 重置
