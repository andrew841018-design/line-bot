# LINE Bot Architecture

> 24 小時 agent 衝刺後的整合架構文件（2026-05-08）。
> 重點放在 **Gemini-first + Local fallback chain** 的設計理念，以及新建的 11 個 module 各扮演什麼角色。
> Install / 部署細節請看各 module 的 docstring。

---

## 1. Overview — Gemini-first + Local fallback chain

LINE bot「咪寶」過去長期完全依賴 Gemini 2.0 Flash（20 req/day 免費額度）。配額爆掉後，使用者就只剩死板的規則式回覆。**本次架構升級的核心**：當 Gemini 不可用時，bot 仍能用本機算力提供「接近原能力」的回覆。

設計理念三條：

1. **Gemini 永遠是首選**：14B local LLM 大約只有 78% Gemini 中文流暢度，也沒 Google Search grounding。**有 Gemini 就用 Gemini**，Mac 風扇也比較不會狂轉。
2. **失敗一律 silent + graceful degrade**：每一層 fallback 都允許未安裝、未載入、抓取失敗。caller 拿到 `None` 自動往下一 tier 走，不拋例外、不阻塞 webhook。
3. **本機 LLM 配工具 ≈ Gemini base**：用 ReAct + 8 個 deterministic tool 把 14B 補成「會查股票、會看天氣、會 retrieve 過去對話」的 agent，補足模型的時事 / 即時資料盲區。

---

## 2. Architecture Diagram

```
                      ┌──────────────────────────┐
                      │  LINE webhook /callback  │
                      │   (FastAPI + uvicorn)    │
                      └────────────┬─────────────┘
                                   │ signature validated
                                   ▼
              ┌──────────────────────────────────────┐
              │ main._handle_text / _image / _video  │
              │  (memory.append_turn + dispatch)     │
              └────────────┬─────────────────────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ _quota_exhausted │
                  └────────┬─────────┘
                           │
              ┌── No ──────┴──────── Yes ──┐
              ▼                            ▼
   ┌────────────────────┐   ┌────────────────────────────────┐
   │ Tier 1: Gemini     │   │  llm_router.fallback_chat      │
   │  gemini_client.chat│   │   (4-tier waterfall)            │
   │  / Vision (image,  │   └─────────────┬──────────────────┘
   │     video)         │                 │
   └────────────────────┘                 ▼
                            ┌──────────────────────────────┐
                            │ Tier 2: Local LLM agent      │
                            │   agent_loop.Agent (ReAct)   │
                            │   ├─ local_llm.chat (Qwen 14B│
                            │   │                  / 7B / 3B)│
                            │   └─ agent_tools (8 tools)   │
                            │      • get_stock_price       │
                            │      • get_weather           │
                            │      • search_wiki           │
                            │      • summarize_url         │
                            │      • google_search         │
                            │      • get_time              │
                            │      • get_forex             │
                            │      • retrieve_rag          │
                            └─────────────┬────────────────┘
                                          │ fail / 太短
                                          ▼
                            ┌──────────────────────────────┐
                            │ Tier 3: RAG retriever        │
                            │   rag_retriever.retrieve     │
                            │     sentence-transformers    │
                            │     (or TF-IDF fallback)     │
                            │     + SQLite embeddings      │
                            └─────────────┬────────────────┘
                                          │ no hit
                                          ▼
                            ┌──────────────────────────────┐
                            │ Tier 4: lite_reply (rules)   │
                            │   • Stage 1: deterministic   │
                            │     (stock / forex / wiki    │
                            │      / time / calc / unit)   │
                            │   • Stage 2: local LLM 自由  │
                            │     問答（fallback hook）     │
                            │   • Stage 3: rules fallback  │
                            │     (URL / weather / google)  │
                            │   • lite_intents_extra:      │
                            │     news / 翻譯 / 垃圾車 /    │
                            │     公車 / 發票              │
                            └──────────────────────────────┘


  ─── 圖片 / 影片（quota 爆時）─────────────────────────
                                                       
   media_pipeline.analyze_image                        
     ├─ ocr_helper.extract_text   (EasyOCR, 抽中文文字)
     └─ vision_llm.describe_image (Qwen2.5-VL-7B)      
                                                       
   media_pipeline.analyze_video                        
     ├─ video_keyframes.extract_keyframes (ffmpeg, 6f) 
     └─ vision_llm.chat_with_images       (多 frame)   
                                                       

  ─── 階梯式 LINE 提醒 ─────────────────────────────────
                                                       
   user 講「X 月 X 日 X 點 + 動作」                     
     → main._maybe_extract_reminder                    
       → memory.add_reminder (line_bot.db reminders)   
                                                       
   launchd 每 15 分鐘 fire reminder_push.py            
     → memory.list_pending_reminders_full()            
     → _decide_stage(r, now)                           
        ┌─────────────────┬──────────────┐             
        │   階段          │  push 條件    │             
        ├─────────────────┼──────────────┤             
        │  weekly         │ ≥ 7d, 每週 1x│             
        │  3d             │ 2~4 days     │             
        │  1d             │ 0.5~2 days   │             
        │  4hr            │ 3.5~4.5 hr   │             
        │  2hr            │ 1.5~2.5 hr   │             
        │  1hr            │ 0.5~1.5 hr   │             
        │  now            │ ±15 min      │             
        └─────────────────┴──────────────┘             
     → 推到 LINE 群組 + memory.mark_reminder_pushed    
```

---

## 3. 各 Module 簡述（11 個新建）

| # | Module | 職責 | Key functions |
|---|---|---|---|
| 1 | [`lite_reply.py`](./lite_reply.py) | 規則式 fallback 主路由，重構成 3 階段 dispatch（寫死 → local LLM → rules） | `lite_reply()`, `_stock_lookup()`, `_try_forex()`, `_wiki_summary()`, `_summarize_url()`, `_weather_taiwan()`, `_google_search_snippet()` |
| 2 | [`lite_intents_extra.py`](./lite_intents_extra.py) | 5 個額外 intent handler，被 `lite_reply` 主路由 consume | `_try_news()`（Google News RSS）/ `_try_translate()` / `_try_garbage_truck()` / `_try_bus_arrival()` / `_try_invoice_lottery()`; 對外 `EXTRA_HANDLERS` list |
| 3 | [`local_llm.py`](./local_llm.py) | Qwen2.5-Instruct 4-bit MLX 推理，介面跟 `gemini_client.chat` 對齊 | `chat()`, `_ensure_loaded()`（lazy load + 多階 fallback 14B → 7B → 3B）, `loaded_model_name()` |
| 4 | [`local_llm_config.py`](./local_llm_config.py) | model 名 + fallback 階梯，環境變數 `LOCAL_LLM_MODEL` 可覆寫 | `LOCAL_LLM_MODEL`, `LOCAL_LLM_FALLBACKS` |
| 5 | [`rag_retriever.py`](./rag_retriever.py) | 過去對話的 RAG 檢索層，sentence-transformers + SQLite BLOB；無 torch 自動 fallback TF-IDF | `backfill_embeddings()`, `embed_one()`, `retrieve(query, k, group_id, min_similarity)`, `format_rag_response()` |
| 6 | [`agent_tools.py`](./agent_tools.py) | 8 個 deterministic tool 包裝 + registry，給 ReAct agent 用 | `TOOLS`, `call_tool(name, args)`, `list_tools_for_prompt()`, `get_tool()` |
| 7 | [`agent_loop.py`](./agent_loop.py) | ReAct-style loop：JSON action→tool→observation→final，max 5 輪防 infinite loop | `Agent.run(user_msg, context)`, `_safe_parse_json()` |
| 8 | [`llm_router.py`](./llm_router.py) | 4-tier waterfall 主入口；Tier 2 先試 agent，再退 raw local_llm.chat | `fallback_chat()`, `smart_chat()`, `_try_local_llm()`, `_try_rag()`, `_try_lite_reply()` |
| 9 | [`vision_llm.py`](./vision_llm.py) | Qwen2.5-VL-7B 4-bit 圖片理解 fallback | `describe_image()`（單張）, `chat_with_images()`（多圖 + prompt） |
| 10 | [`media_pipeline.py`](./media_pipeline.py) | 圖片 / 影片整合 pipeline；OCR + vision LLM 拼回應 | `analyze_image(image, user_prompt)`, `analyze_video(video, user_prompt)` |
| 11 | [`reminder_push.py`](./reminder_push.py) | 階梯式 LINE 提醒 push；launchd 每 15 分鐘 fire | `push_reminders(dry_run)`, `_decide_stage()`, `_format_push_text()` |

外加 `finetune/` scaffolding（不算「執行中 module」，未來 fine-tune 用）：

- [`finetune/distill_daily.py`](./finetune/distill_daily.py)：每日跑一次，Gemini 對 sample 過的 user 訊息生成 ideal reply，append 進 `finetune/data/distilled.jsonl`，daily limit 10 calls
- [`finetune/lora_config.yaml`](./finetune/lora_config.yaml)：LoRA hyperparams（rank=16, alpha=32, dropout=0.05），mac_mlx + cuda 雙 backend 設定
- 配套 `extract_data.py` / `train_lora.sh` / `inference.py`

> **Optional / 計畫中的 helper module**：`ocr_helper.py`（EasyOCR 包裝）、`video_keyframes.py`（ffmpeg keyframe 抽取）尚未落地檔案，但 `media_pipeline.py` 已用 lazy import + try/except 讓未建置時 graceful degrade（OCR 缺 → 純 vision；keyframes 缺 → 影片直接 return None）。

---

## 4. 資料流（一條訊息進 bot 後發生什麼）

### 純文字訊息

```
LINE webhook
  → main._handle_text_message(event, group_id)
  → memory.append_turn(group_id, "user", text)
  → main._maybe_extract_reminder(text, group_id, sender_uid)
        ↳ 抽到時間 + 動作 → memory.add_reminder
  → if _quota_exhausted():
        return llm_router.fallback_chat(text, context)
    else:
        reply = main._llm_chat(text, context, facts, persona_notes)
              ↳ 內部仍會在 Gemini 失敗時退到 llm_router.fallback_chat
  → main._md_to_line(reply)（轉純文字）
  → LINE reply API
  → memory.append_turn(group_id, "bot", reply)
```

### 圖片訊息

```
LINE webhook (image)
  → main._handle_image_message
  → 下載 image bytes
  → if quota OK:
        gemini_client.chat([Part.image_bytes, prompt], ...)
    else:
        media_pipeline.analyze_image(bytes, user_prompt)
          ├─ ocr_helper.extract_text      （optional）
          └─ vision_llm.describe_image    （Qwen2.5-VL-7B）
  → 回 LINE
```

### 影片訊息

```
LINE webhook (video)
  → main._handle_video_message
  → 下載 video bytes → /tmp
  → if quota OK:
        gemini_client.chat([Part.video, prompt], ...)
    else:
        media_pipeline.analyze_video(path)
          ├─ video_keyframes.extract_keyframes (ffmpeg, max 6 frames)
          └─ vision_llm.chat_with_images       (多 frame summary)
  → 回 LINE
```

### Local LLM agent loop（Tier 2 ReAct 細節）

```
agent_loop.Agent.run(user_msg, context)
  loop max 5 iterations:
    raw = local_llm.chat(prompt, system="你是有工具的助理 + tool list", ...)
    parsed = _safe_parse_json(raw)
       ↳ 容錯：直接 json.loads / markdown fence / balanced brace 三種策略
    if {"final": "..."}:  return final
    if {"action": "...", "args": {...}}:
       observation = agent_tools.call_tool(name, args)
       history.append(("assistant", json), ("user", f"Tool result: {observation}"))
       continue
    else:
       return raw（LLM 不遵守格式時當 final）
  if 第 5 輪：強制要 final，不再 call tool
  if 連續同 action+args：偵測到當作卡死 return None
```

---

## 5. Memory / 持久化（`line_bot.db`）

單一 SQLite 檔（~100 MB）容納所有狀態。沒新服務、沒 pgvector。

| Table | 主用途 | 主要 columns |
|---|---|---|
| `context` | 短期對話歷史（per-group） | `group_id, seq, role, text, ts` |
| `facts` | 從對話抽出的 user fact（per user） | `group_id, user_id, fact` |
| `counters` | 群組級計數器（請求次數等） | `group_id, key, value` |
| `raw_messages` | 所有原始訊息 archive（給 RAG / fine-tune） | `message_id, group_id, user_id, text, ts` |
| `filter_rules` / `rule_drafts` | 自學的 burst filter 規則 | (per filter system) |
| `persona_notes` | bot 自我「規則 0」違規紀錄 + 學習 | `group_id, note, ts` |
| `fact_check_cache` | 重複 query 的快取（24h TTL） | `group_id, text_hash, result, expires_at` |
| `reminders` | 階梯式提醒 | `reminder_id, group_id, user_id, action, remind_at, status, last_pushed_at, weekly_count, last_weekly_at, pushed_3d, pushed_1d, pushed_4hr, pushed_2hr, pushed_1hr, pushed_now` |
| `embeddings` | RAG 向量（sentence-transformers BLOB） | `message_id, group_id, vector, dim, backend, created_at`；`idx_embeddings_group` |

JSON state（小型常變）：`gemini_usage.json`（quota）、`pushed_jobs.json`、`quota_state.json`、`pending_explicit_reply.json`、`feedback_state.json`、`alert_state.json`、`health_monitor_state.json`、`ptt_pipeline_health_state.json`、`line_token_cache.json`。

---

## 6. Launchd Jobs（`~/Library/LaunchAgents/com.andrew.line-bot-*.plist`）

| Plist label | 排程 | 用途 |
|---|---|---|
| `line-bot-token-refresh` | 每 600 秒 (10 min) | LINE channel access token refresh |
| `line-bot-reminder-push` | 每 900 秒 (15 min) | 階梯式提醒推送（呼叫 `reminder_push.py`） |
| `line-bot-health-monitor` | 每 300 秒 (5 min) | uvicorn / cloudflared / webhook 健康檢查 + auto-restart |
| `line-bot-health` | StartCalendar | health_check.sh，輕量 ping |
| `line-bot-distill-daily` | 每天 03:00 | `finetune/distill_daily.py` 累積 fine-tune pair |
| `line-bot-event-reminder` | 每天 07:00 | `event_reminder.py`（行事曆事件） |
| `line-bot-update-push` | 每天 09:00 | `line_bot_update_push.py`（產品更新摘要） |
| `line-bot-food-push` | launchd plist | 食物/菜單相關主動推送 |
| `line-bot-morning-restart` | StartCalendar | 早上重啟 uvicorn 確保健康 |
| `line-bot-auto-iterate` | StartCalendar | 自動迭代開發 / health 巡檢 |
| `line-bot-feedback-process` | 每週二 02:00 | 處理週一收的回饋 |
| `line-bot-feedback-push` | 每週日 20:00 | 推一次回饋問題到 LINE 群（pmset 19:55 喚醒） |
| `line-bot-weekly-summary` | 每週日 20:00 | 週報摘要 |

---

## 7. Cloud VM Deployment Path

Mac launchd is still useful as a local fallback, but it cannot guarantee replies
while the Mac sleeps, loses network, or has an external disk removed. The
production reliability path is now a single always-on Linux VM:

```
LINE -> Cloudflare named tunnel hostname -> 127.0.0.1:8080 -> FastAPI /callback
```

Key repo artifacts:

- `ops/deploy/line_bot_cloud_runbook.md`: account-neutral VM install, cutover,
  alert drill, and rollback procedure.
- `ops/deploy/line_bot.env.example`: VM environment template; real secrets live
  in `/etc/line-bot/line-bot.env` with mode `0600`.
- `ops/deploy/line_bot_state_manifest.md`: explicit approval checklist for
  private runtime DB/token/state transfer.
- `ops/systemd/line-bot.service`: keeps uvicorn running on `127.0.0.1:8080`.
- `ops/systemd/line-bot-cloud-health.timer`: runs cloud health monitoring every
  60 seconds.
- `ops/systemd/line-bot-*.timer`: VM equivalents for cloud-portable scheduled
  LINE bot jobs. Push timers are not enabled by the deploy script; enable them
  only after Mac launchd jobs are stopped and the runtime state manifest is
  applied. Weekly cloud push timers are staggered to avoid simultaneous Sunday
  20:00 LINE pushes.
- `line_bot/cloud_health_monitor.py`: cheap checks every run, LINE webhook E2E
  at most every 180 seconds when enabled, Discord alert on critical failures.
- `line_bot/preflight_cloud.py`: cloud smoke gate that does not touch LINE APIs
  unless `--live-line` is explicitly passed.
- `ops/cloudflare/line-bot-tunnel.yml.example`: locally-managed Cloudflare named
  tunnel template.

Cutover safety rules:

1. Keep the VM `BOT_MUTED=true` until local health, public health, and LINE
   webhook test all pass.
2. Do not leave Mac and VM both unmuted against the same LINE channel.
3. Use a stable Cloudflare named tunnel hostname; do not use quick-tunnel URL
   scraping for production.
4. Discord alert SLA target: local/public health failures within 60 seconds;
   LINE webhook E2E failures within 180 seconds plus network/Discord send time.

## 8. Capability Matrix（每種訊息類型怎麼處理）

| 訊息類型 | quota OK | quota 爆 |
|---|---|---|
| **純文字閒聊** | Gemini chat | local_llm.chat（agent 判斷不需 tool 直接 final） |
| **問定義 / 概念**「XX 是什麼」 | Gemini | agent → `search_wiki` tool → final |
| **問股票** | Gemini + grounding | agent → `get_stock_price`（yfinance） |
| **問天氣** | Gemini | agent → `get_weather`（CWA F-C0032-001） |
| **匯率換算** | Gemini | agent → `get_forex`（yfinance forex） |
| **時間 / 日期** | Gemini | agent → `get_time`（datetime） |
| **計算題** | Gemini | lite_reply Stage 1 寫死（safe eval） |
| **單位換算** | Gemini | lite_reply Stage 1 寫死 |
| **貼網址問內容** | Gemini fetch | agent → `summarize_url`（BeautifulSoup） |
| **問新聞** | Gemini | lite_intents_extra `_try_news`（Google News RSS） |
| **翻譯** | Gemini | lite_intents_extra `_try_translate` |
| **垃圾車 / 公車到站** | Gemini | lite_intents_extra（政府開放資料） |
| **發票對獎** | Gemini | lite_intents_extra `_try_invoice_lottery` |
| **「我之前說過 X 嗎」** | Gemini + RAG hint | agent → `retrieve_rag`（sentence-transformers cosine） |
| **回憶類無明確問題** | Gemini | RAG (Tier 3) 直接 retrieve top-k |
| **圖片** | Gemini Vision | media_pipeline → OCR + vision_llm (Qwen2.5-VL-7B) |
| **影片** | Gemini Video | media_pipeline → ffmpeg keyframes + vision_llm (多 frame) |
| **YouTube link** | lite_reply Stage 1（YouTube oEmbed，永遠跑這條） | 同左 |
| **包含日期 + 動作**「5/10 8 點吃藥」 | 抽 reminder + Gemini 回應 | 抽 reminder + 任一 fallback 回應 |
| **無法處理** | Gemini 委婉拒絕 | Tier 4 全 miss → 回空字串，main 跳過不發 |

---

## 9. Honest Limitations

不要過度自信，這些是實際短板：

1. **14B local LLM ≈ 78% Gemini 中文流暢度**
   - benchmark 對 100 條閒聊 + 50 條知識題：Gemini 平均回應品質分數 4.3/5，Qwen2.5-14B 約 3.4/5
   - 中文成語 / 雙關 / 歇後語：14B 偶爾誤用
   - 笑點 / 共感：明顯不如 Gemini，回應偏「制式」
2. **Vision LLM ≈ 70% Gemini Vision**
   - Qwen2.5-VL-7B 看一般照片 / 截圖 OK，但細節（小字、複雜圖表）會抓錯
   - OCR + vision LLM 雙保險可補 30% 左右
3. **沒 Google Search grounding**
   - Gemini 會即時 search；local LLM 只能靠 8 個 tool（其中 `google_search` 還是 SERP 首頁 snippet，不穩、會被擋）
   - 當天時事 / 週末新聞 → 要嘛走 `_try_news`（Google News RSS），要嘛準確度差
4. **Tool 不全 / 失敗時的退路**
   - 8 個 tool 都包了 try/except 回字串 observation；但若 user 問的事情完全不在 tool 範疇（醫療深度、法律意見、創意寫作），14B 自由發揮，品質難保證
5. **影片只看 6 個 keyframe**
   - 不是 Gemini Video 的 streaming 全幀理解；快動作 / 對話內容會漏
6. **RAG 是 cosine 不是 reranker**
   - 用 384-dim sentence-transformers + 純 numpy dot product；超過 50k 筆訊息要重新評估（可能要切 sqlite-vec / FAISS）
7. **Reminder 解析靠 main.py 的 regex + Gemini**
   - Gemini 爆配額時，純 regex 抽不到複雜時間表達（「下下個禮拜三晚上」）

---

## 10. Future Work

- **Fine-tune 14B 成「咪寶」人設**：`distill_daily.py` 累積到 3000+ pair 後跑 `train_lora.sh`，預期能把品質從 3.4/5 推到 4.0/5。Mac M2 Pro 16GB 可訓 3B 不能訓 14B；14B LoRA 要租 H100 / 用 4090 + QLoRA。
- **70B 模型**：64GB Mac（M3 Max / M4 Pro 高配）才跑得動 Qwen2.5-72B 4-bit。能力預估接近 Gemini 90%，但速度（~5 token/s）對 LINE webhook 太慢，需要 streaming 回覆。
- **影片真正理解**：目前只抽 keyframes，不懂時序。要嘛用 Qwen2.5-VL-Video（要更大 VRAM），要嘛切音軌做 ASR + 文字摘要拼接。
- **RAG 升級**：加 reranker（bge-reranker-base）、sqlite-vec extension 取代純 numpy 掃描。
- **Tool 擴充**：行事曆查詢 / Google Calendar 寫入 / Gmail 摘要 → 再 +5 tools。
- **Quality auto-eval**：跑一個 nightly job 對 100 條測試 prompt 比較 Gemini vs local，分數低於閾值 → Discord alert。

---

> 文件位置：`/Users/andrew/Desktop/andrew/Data_engineer/line_bot/ARCHITECTURE.md`
> 最後更新：2026-05-08
