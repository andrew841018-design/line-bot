# Fine-tune scaffolding for LINE Bot (咪寶)

> Status: **scaffold + docs only** — 沒實際 train，沒灌依賴。給未來想自訓 agent 的 starting point。

## TL;DR — 該做還是不該做？

**先不要急著 fine-tune。** 看數字決定：

| LINE 對話累積筆數 | 建議方案 | 為什麼 |
|---|---|---|
| < 500 | 純 Gemini + prompt 工程 | 資料太少，fine-tune 必 over-fit |
| 500 – 3,000 | **RAG**（見 `../rag_retriever.py` 規劃） | 資料量邊緣；RAG 不需 train、改即時生效 |
| 3,000 – 10,000 | RAG + persona_notes（自我學習） | fine-tune 會開始有效但 hit rate ≈ 50–60%（vs Gemini 90%）|
| 10,000+ | RAG **加上** LoRA fine-tune | hit rate 可拉到 75–80%，值得花成本 |

當前 `line_bot.db` 抽出來只有 **十幾筆 paired conversation**（context 表 15 對 + raw 補幾百筆雜訊）。**現階段直接 fine-tune 是浪費時間**。建議：

1. 短期：繼續用 Gemini，靠 `_RULE_NEWS_CASE` / `規則 0` post-check / `persona_notes` 自我學習
2. 中期：先把 RAG 做起來（拿 raw_messages 全文索引 + 查回相關歷史片段塞 prompt）
3. 長期：累積 1 萬筆對話再考慮 fine-tune（用本目錄的 scaffold）

---

## 為什麼還是建這個 scaffold？

1. **Quota fallback option**：Gemini 免費層 20 req/day 太緊，付費 pay-as-you-go 也會抖。本地 fine-tuned model 是備援
2. **隱私**：私人對話資料給雲 LLM 看不見得舒服
3. **風格鎖定**：Gemini 升 model 版本後語氣會跑掉；自己訓的能凍結風格
4. **學習目的**：作為 ML/LLM 知識實作練習

但這 4 個動機都**不該在 < 3000 筆資料時行動**——資料不夠 fine-tune 出來只是噪音。

---

## 檔案結構

```
finetune/
├── README.md          # 你正在看的
├── extract_data.py    # 從 line_bot.db 抽 SFT JSONL（可實跑）
├── lora_config.yaml   # LoRA 超參，mac mlx + cuda 兩套
├── train_lora.sh      # 訓練 shell 範本（不真跑，print 命令）
├── inference.py       # FineTunedAgent stub（介面對齊 gemini_client.chat）
└── data/
    └── sft.jsonl      # extract_data.py 輸出（首次跑前不存在）
```

---

## 完整流程（未來真要訓的時候）

### 步驟 1：抽資料

```bash
cd /Users/andrew/Desktop/andrew/Data_engineer/line_bot
python finetune/extract_data.py
# 或加 fallback source：
python finetune/extract_data.py --include-raw
```

預期輸出：

```
[OK] wrote N pairs to finetune/data/sft.jsonl
  source breakdown: {'context': N1, 'raw_messages': N2}
  user msg length: avg=...
  bot msg length:  avg=...
  quality flags: very_short_user(<5)=...
[INFO] N pairs — fine-tune 資料量充足/不足
```

過濾規則（在 `extract_data.py` 內可調）：
- user 訊息 < 3 字 → 跳
- 純 emoji / 純 URL / 純標點 → 跳
- bot 內部訊息（quota 用完、系統提示）→ 跳
- bot 回覆 < 5 字 → 跳
- 同 (user, bot) pair 去重

格式（每行一個 JSON）：

```json
{"messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}
```

### 步驟 2：裝依賴

⚠️ **本 scaffold 不自動裝**，請看你跑哪個 backend：

**Mac (Apple Silicon)**：
```bash
pip install mlx-lm        # ≈ 50MB 不含 weights
```

**Linux + CUDA**：
```bash
pip install torch transformers peft accelerate datasets bitsandbytes trl
accelerate config         # 第一次需互動設定
```

或考慮直接用 [axolotl](https://github.com/axolotl-ai-cloud/axolotl)（吃 yaml 比較不痛苦）。

### 步驟 3：訓

```bash
cd finetune
bash train_lora.sh                  # 先看 print 出的命令
BACKEND=mac bash train_lora.sh       # 強制 mac
BACKEND=cuda bash train_lora.sh      # 強制 cuda
```

**目前 `train_lora.sh` 只 print 命令不真跑**——複製出來自己貼上 terminal 執行（或把 `cat <<'EOF'` 改掉）。這樣設計是因為訓練要真跑才會吃 GPU/時間，scaffold 階段不該誤觸發。

### 步驟 4：評估

```bash
# 簡單 perplexity（mlx_lm 內建）
mlx_lm.lora --model ... --test --data data --adapter-path outputs/llama32-3b-lora

# 真實品質：寫 eval 腳本，拿 100 個 hold-out user message 跑 inference + 人工評分
# 評分標準（建議）：
#   1. 人設一致性（是不是「咪寶」口氣）
#   2. 回答正確性（事實有沒有錯）
#   3. 規則 0 合規（第一句具體判斷句、無 echo opener）
```

### 步驟 5：部署

訓完後 `outputs/llama32-3b-lora/adapters.safetensors` 會生出來。

去 `inference.py` 解掉 `_load_model()` 的 `NotImplementedError`，照註解填入 mlx_lm.load / peft.PeftModel.from_pretrained。

`main.py` swap 範本（**還沒實作，等你自己加**）：

```python
# main.py 對話路由處
try:
    from finetune import inference as ft_inference
    ft_agent = ft_inference.FineTunedAgent()
    if ft_agent.ready:
        reply = ft_agent.chat(user_input, ctx, facts)
    else:
        reply = gemini_client.chat(user_input, ctx, facts)
except Exception:
    reply = gemini_client.chat(user_input, ctx, facts)
```

⚠️ 建議**先 shadow mode 跑一陣子**：兩邊都生回覆，比對 + log，確認本地 model 沒爆才真切換。

---

## 誠實成本（2026 年硬體與雲價格估算）

| 配置 | dataset 大小 | wall-clock | 直接成本 |
|---|---|---|---|
| Mac M2 Pro 16GB / Llama-3.2-3B / 4-bit | 1,000 pairs | 4–8 hr | 電費 NT$10 |
| Mac M3 Max 36GB / Llama-3.2-3B | 1,000 pairs | 2–4 hr | 電費 NT$15 |
| RTX 4090 24GB 本地 / Qwen2.5-7B QLoRA | 1,000 pairs | 2–4 hr | 電費 NT$30 |
| H100 80GB spot (Lambda/RunPod) / Qwen2.5-7B | 1,000 pairs | 30–90 min | $3 USD（$2/hr × 1.5 hr） |
| H100 / 10,000 pairs / 5 epochs | 10K pairs | 4–6 hr | $10 USD |

加上**試錯成本**：第一次幾乎一定要重訓（hyperparam 沒調好、data filter 沒清乾淨、chat_template 對不上）。預算抓 **3 倍**比較實在。

---

## 誠實品質預期

以**對話風格 hit rate**作為 proxy（人工評估「這句像不像咪寶」）：

| dataset 大小 | 預期 hit rate | vs Gemini (90%) | vs 純 RAG (~70%) |
|---|---|---|---|
| 500 pairs | 30–40% | 嚴重退步 | 比 RAG 還差 |
| 1,000 pairs | 45–55% | 退步 | 跟 RAG 差不多 |
| 3,000 pairs | 55–65% | 仍退步 | 略好 |
| 10,000 pairs | 70–80% | 接近但仍輸 | 明顯好 |
| 30,000 pairs | 80–88% | 持平 | 顯著好 |

**為什麼 fine-tune 即使大量 data 也很難贏 Gemini？**

1. Gemini 是 100B+ 參數，3B/7B 本質上知識量輸
2. Gemini 有 web search / multimodal 能力，本地 model 沒有
3. fine-tune 容易 over-fit 你的口頭禪、忘記常識（「災難性遺忘」）
4. 自己準備的 SFT data 雜訊比 commercial 訓練 corpus 高很多

**真的要做 fine-tune 也建議用 LoRA 而不是 full fine-tune**：能保留 base model 的常識，只動風格層。

---

## 連結到其他選擇

- **更輕量**：純 prompt 工程 + persona_notes 自我學習（已實作於 `gemini_client.py`）
- **中等成本**：RAG（規劃中：拿 raw_messages 全文檢索 + 注 prompt）
  - 連結：等 rag_retriever.py 實作後補
- **更大投入**：本目錄的 LoRA fine-tune（建議資料 10K+ 再啟動）

---

## 驗收 / Smoke test

```bash
cd /Users/andrew/Desktop/andrew/Data_engineer/line_bot

# 1. 5 個檔案都存在
ls finetune/

# 2. extract_data.py 能跑
python finetune/extract_data.py

# 3. inference.py import 不爆
python -c "from finetune.inference import FineTunedAgent; a = FineTunedAgent(); print('ready=', a.ready)"

# 4. lora_config.yaml 是合法 YAML
python -c "import yaml; print(list(yaml.safe_load(open('finetune/lora_config.yaml'))))"

# 5. train_lora.sh 是合法 bash
bash -n finetune/train_lora.sh
```

預期：5 個都 pass、`ready=False`（沒 train 過很合理）、`extract_data.py` 報統計。
