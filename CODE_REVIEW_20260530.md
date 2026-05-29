# LINE bot 「咪寶」Code Review — 2026-05-30

> ## ✅ 實作狀態（2026-05-30 fix 回合）
> 已修 8 條並寫入磁碟：**C1 / I1 / I2 / I3 / I7 / N1 / N3 / N5**。
> **靜態**：main.py / gemini_client.py / memory.py 三檔 `ast.parse` OK；`ruff F,E9` exit 0；`import main` OK。
> **動態（TDD）**：新增 `test_pending_concurrency_fix.py`（C1+I1 重現）5 條——改前 5/5 FAIL → 改後 5/5 PASS。
> **回歸**：pending/drain/handler 核心 suite `test_pending_concurrency_fix + test_drain_quota_exhaustion + test_piggyback_drain + test_silent_drop + test_handlers` = **49 passed**；全量 `pytest` 結果見檔尾。
> **N1/I2 額外動態驗證**：中文逗號相鄰 URL 現算 2 域（修前 1）；`main.py` 已無 `import llm_router`。
> **⛔ 尚未做**：重啟 uvicorn + curl /health（**未重啟 production bot** —— 等 Andrew 點頭再動正式服務）；commit/push（等確認）。
>
> 未修（需 Andrew 拍板，設計決策/不可逆）：**I4** 刪 EXTRA_HANDLERS 死碼、**I5/I6** webhook 改 async、**I8** redelivery burst token（影響最低，純 log noise）。
>
> ### 改動檔案
> - `main.py`：C1（`_save_pending_any`→`pending_store.add`、`_clear_pending_explicit`→`clear_group`、`_commit_pending_removal`→逐筆 `remove_by_message_id`、drain 例外→`replace_group`、piggyback 三段→`_commit_pending_removal`）、I1（`_save_quota_state` mkstemp）、I2（audio fallback 改 `_llm_chat`）、I7（catch-all 補 `log_raw_message`）、N3（decode errors=replace）、N5（raise...from None）
> - `gemini_client.py`：I1（`_save_usage` mkstemp）、N1（`_URL_DOMAIN_RE` 排除中文標點）
> - `memory.py`：I3（`_conn` 加 `PRAGMA busy_timeout=5000`）
> - `test_pending_concurrency_fix.py`：新增（C1+I1 重現 test，5 條）

---


> 範圍:核心熱路徑(webhook 入口 → quota 判斷 → pending 持久化/drain → Gemini → reply → memory + fallback)
> 方法:6 個 general-purpose reviewer 平行掃 + 主線 verify-don't-trust 逐條開原檔/動態確認
> Reviewer 限定 Claude-only(此 code 含家族對話資料 + 咪寶 persona 寫作風格 → 不送 codex/gemini)
> 處理:**report-only**,未動任何 code(等 Andrew 決定改哪些)

---

## 摘要

| 嚴重度 | 數量 | 一句話 |
|---|---|---|
| Critical | 1 | pending JSON 兩段式 RMW 繞過 pending_store 安全鎖 → 家人訊息可能靜默遺失/重複回覆 |
| Important | 9 | state 檔固定 tmp 名跨 process race、llm_router 缺檔致語音 fallback 永遠沉默、memory 連線未關+無 busy_timeout、EXTRA_HANDLERS 死碼、event loop 阻塞等 |
| Nit | 5 | 規則0 domain 計數誤判、咪寶子字串誤觸發、decode 早於驗章等 |

動態驗證:`pytest --collect-only` = 748 test 可收集;熱路徑 `test_silent_drop / test_drain_quota_exhaustion / test_piggyback_drain` = **28 passed**;ruff F/B/E9 熱路徑乾淨(僅 B904 + E402)。

---

## 🔴 CRITICAL

### C1. pending 佇列兩段式 read-modify-write → 並發 lost update(家人訊息靜默遺失)

**事實(已驗證)**:`pending_store.py` 已提供 cross-process 安全的單-lock 方法 —— `add()` / `remove_by_message_id()` / `clear_group()` / `replace_group()`,每個都在**一次 `fcntl.flock`** 內完成 load+mutate+save(crash-safe tmp+os.replace)。

但 `main.py` **完全沒用這些**(`grep` 證實:`add`=0、`remove_by_message_id`=0、`clear_group`=0 次呼叫),全部走自己的兩段式:
```
data = _load_pending_explicit()   # pending_store.load() → 取鎖讀快照 → 放鎖
... (中間可能夾網路下載 _download_content,窗口被拉長) ...
data[group_id].append(entry)
_save_pending_explicit_raw(data)  # pending_store.save_full(整個 dict) → 重新取鎖寫 → 放鎖
```
**load 與 save_full 各自獨立取放鎖,中間鎖是放開的。** save_full 寫的是開頭的快照,期間任何其他 writer 的變更會被整段覆蓋。

**6 個受影響 call site**:`main.py` 2909(`_save_pending_any`)、3355(`_drain_pending_for_group` 用開頭舊快照 `remaining` 回寫)、3554、4570、4663(`_pop_pending_for_piggyback`)、2815(wrapper)。

**真實觸發路徑(已確認並發來源)**:
- **跨 process**:獨立 cron `jobs/process_pending_media.py` 走安全的 `remove_by_message_id`(單鎖)刪掉一筆;同時 main.py 的 `save_full` 拿舊快照整段寫回 → **剛被刪的 entry 復活 → 重複回覆**。
- **跨 thread(同 process)**:`_MEDIA_EXECUTOR` / `_PIGGYBACK_EXECUTOR` / retry worker thread 與 event-loop thread 各跑一份 load→save_full → 後寫者覆蓋前寫者 → **某人的訊息那筆 append 被蓋掉 → 永久遺失**。
- **mid-drain 再爆**:drain(慢,中途 Gemini 逐 group push)期間 quota 又爆,新訊息經 `_save_pending_any` 進來,drain 結尾用舊 `remaining` 覆寫該 group → 新訊息遺失。

**後果**:直接違反專案鐵律「任何留言都要回覆」(silent drop),或重複回覆。

**修法**:把 6 處兩段式全部換成 `pending_store` 既有的單-lock API:
- `_save_pending_any` 結尾 → `pending_store.add(group_id, entry)`
- 移除單筆 → `pending_store.remove_by_message_id(group_id, msg_id)`
- 清整組 → `pending_store.clear_group(group_id)`
- drain 部分回寫 → `pending_store.replace_group(group_id, remaining)`(仍有 ABA 風險,最好改逐筆 remove 已處理 msg_id)
低風險、機械式替換,且與 cron 走同一條安全路徑。

---

## 🟠 IMPORTANT

### I1. `_save_usage` / `_save_quota_state` 用固定共享 tmp 名 → 跨 process os.replace 交錯損毀 state(主線校準 + R2)
**(自我修正:我先 grep 後讀原檔,改掉初稿「沒有原子寫」的錯誤判斷)** 兩者**都有** tmp+os.replace,但 tmp 名是**固定共享**的:`_save_usage`(gemini_client.py:78)`tmp = f"{_USAGE_FILE}.tmp"`、`_save_quota_state`(main.py:2745)`tmp = f"{_QUOTA_STATE_FILE}.tmp"` —— 非 per-process unique。對照 `pending_store._save_raw` 用 `tempfile.mkstemp`(每次唯一)才正確。
- 觸發:uvicorn handler thread 與獨立 cron process(`process_pending_media` / `reminder_push` 等)幾乎同時寫同一檔,兩者都 `open(同一 .tmp,"w")` 交錯寫 → 一個 process 的 `os.replace` 把另一個寫到一半的 tmp 搬成正式檔 → JSON 截斷/混合 → 解析失敗走 except → usage 計數歸 0(bot 以為額度全滿,狂打已爆 Gemini)或 `exhausted_until` 歸 0(誤判 quota 已恢復)。
- 校準:R2 把 quota_state 這條列 critical;我降為 important —— except 都有接住、損毀後下次成功寫即自癒、且單 process 部署根本不觸發。但 cron 是獨立 process,跨 process 窗口真實存在。
- 修法:兩處都改用 `tempfile.mkstemp(dir=同目錄)` 產生唯一 tmp 再 `os.replace`,比照 pending_store。

### I2. `import llm_router` 已死檔 → 語音 fallback 永遠沉默(主線 runtime 驗證)
`main.py:2257`(`_audio_asr_fallback`)`import llm_router` → **`ModuleNotFoundError`**(`llm_router.py` 2026-05-18 已刪,只剩 `__pycache__/*.pyc`;runtime 實測 import 失敗)。被 2256-2267 的 try/except 接住 → silent return。
- 觸發:quota 爆 + 收到語音 + 經 quote 路徑 → `_audio_asr_fallback` → import 炸 → **語音轉文字後永遠不回覆**。
- 修法:改走 `lite_reply` / 本機路徑(比照文字鏈),或移除此死路徑。另:`jobs/process_pending_media.py:4` docstring 仍寫「imports ... llm_router」是過時,該檔實際已不 import,順手修。

### I3. memory.py 連線未關 + 無 busy_timeout(R6,已驗證)
`_conn()`(memory.py:32)每次 `sqlite3.connect()` 開新連線,36 處 `with _conn()`、**0 處 `.close()`**。實測 sqlite3 的 `with conn` **只 commit 不 close**(與 file object 不同),靠 GC 回收。
- 校準:CPython refcount 在函數 return 即回收,**不會無界洩漏**(故非 critical);真實成本是每次記憶操作開新連線 + 2 條 PRAGMA 的 overhead,且依賴 GC(PyPy / 例外持幀時脆弱)。
- **更該修的是無 `busy_timeout`**:`reminder_push.py` / cron 是獨立 process 寫同一 db,跨 process 併發寫會直接 `database is locked` 拋錯。
- 修法:`_conn()` 加 `conn.execute("PRAGMA busy_timeout=5000")`;連線改顯式 close(包一層 contextmanager)或用單一共享連線。

### I4. `EXTRA_HANDLERS` 5 個 fallback intent 是死碼(R5,已驗證)
`lite_intents_extra.EXTRA_HANDLERS`(新聞 RSS / 翻譯 / 垃圾車 / 公車到站 / 發票對獎)除自身 `__main__` 外**零引用**;`lite_reply.py` 的 Stage1/Stage3 router 從不 import 它。
- 觸發:quota 爆時家人問「新聞」「翻譯這句」→ 走不到專用 handler,只能落到泛用回覆。`ARCHITECTURE.md` 宣稱「被 lite_reply consume」與事實不符。
- 修法:把 `EXTRA_HANDLERS` 接進 `lite_reply` Stage 3,或刪掉 + 更新文件。

### I5. webhook 同步阻塞 event loop(R1)
`callback` 是 `async def`(987),但 `_handle_event`→`_handle_text_message`→`_llm_chat`→`_reply` 全程同步、無 `await`/`to_thread`(1022)。一個事件處理期間(Gemini 可數秒~30s,媒體 join 達 50s)整個 uvicorn event loop 卡住。
- 校準:家族 bot QPS 低,實務影響有限,但架構上真實(LINE 5xx 後 redeliver 會加重)。
- 修法:`await asyncio.to_thread(_handle_event, event)` 或把 callback 改 sync def 讓 Starlette 自動丟 threadpool。

### I6. `_reply` 把 piggyback drain 塞進送出路徑(R1)
`main.py:4845` `_reply` 內最多跑 4 次 `_pop_pending_for_piggyback`(每次本機 LLM render,且可能觸發 `media_pipeline.analyze_image` 50s join),全同步在回覆送出前。
- 觸發:quota 未爆但有 pending 時,任何正常回覆都先序列跑 4 次本機推理 → reply_token(~1 分鐘 TTL)可能在生成中過期 → reply_message 失敗 → 走 push fallback(耗月配額)。
- 修法:drain 移出 `_reply` 同步路徑,只在送出前做 cheap text pop。

### I7. 未知訊息類型 catch-all 沒 log_raw_message → redelivery 重複回覆(R1)
`main.py:1148` 貼圖/位置等 catch-all 有 reply + 存 pending,但**漏了** `memory.log_raw_message`(text 1120 / image 1129 / video 1134 / audio 1139 都有)。
- 觸發:redelivery 去重(1058)靠 raw_messages,貼圖類 redelivery 被當「沒收過」→ 重複回覆;且引用該貼圖問後續問題時查不到原文。
- 修法:catch-all 在 reply 前補 `memory.log_raw_message(group_id, msg.id, sender_user_id, "[未知類型]")`。

### I8. redelivery 純文字進 burst 帶過期 token(R1)
`main.py:1451` redelivered 事件的閒聊文字仍進 `burst_filter.add_to_burst(..., event.reply_token)`,token 早過期;Timer flush 時 `_reply` 必失敗再 fallback push。
- 修法:`_handle_event` 偵測 `is_redelivery` 純文字直接走 push-only 或跳過 burst。

### I9. drain 結尾 stale-snapshot 回寫(R2,C1 的子案)
`main.py:3354-3355` `data[group_id] = remaining; _save_pending_explicit_raw(data)` 用函數開頭舊快照算的 `remaining` 整段覆寫。屬 C1 同根因,但特指「drain 期間有新訊息進同 group」的窗口。修法同 C1(逐筆 remove 已處理者,別整段覆寫)。

> 註:`_drain_pending_for_group` 的 **lock 釋放是正確的**(3266 try / 3367 finally / 3368 `slot.release()`),無 lock 洩漏 —— 此項已驗證**不是** bug,不列入。

---

## 🟡 NIT

- **N1** `gemini_client._count_unique_domains`(878):regex `[^/\s)]+` 不排除中文標點(，。、),URL 後緊接中文逗號(無空格,中文很常見)時兩個 URL 併成假 domain token → 規則0 品質 gate domain 計數誤判(false +/-)。修:regex 排除 `，。、]）` 等。(R4)
- **N2** 咪寶名字觸發用子字串 `if name in t` + `replace(name,"",1)`(~4331),任何**提到**咪寶的句子(「剛剛咪寶有回我」)都會觸發完整 Gemini 回覆,燒 20/day 額度。修:限句首 `startswith` 或要求後接問句。(R1)
- **N3** `callback`(988)`body.decode("utf-8")` 早於驗章;非 UTF-8 垃圾請求 → `UnicodeDecodeError` 500(應 400)。修:`errors="replace"` 或 try/except。(R1)
- **N4** `_next_gemini_reset_tw`(2701)今天/明天前綴邏輯可讀性差(reset 恆未來),無實害。(R2)
- **N5** `callback`(1004)`raise HTTPException` 無 `from`(ruff B904),例外鏈可讀性。(ruff)

---

## ✅ 已查證「不是 bug」(不重報)
- HMAC signature 驗證正確(`_parser.parse` → `InvalidSignatureError` → 400;缺 header → 400)
- 每事件例外隔離正確(1021-1024,單壞事件不拖垮整批)
- drain per-group lock 釋放正確(try/finally,無洩漏)
- `_try_acquire_drain_slot` factory-lock + double-check 正確
- Gemini 分組 index alignment 一致(over filtered items)
- ruff F/B/E9 熱路徑乾淨(僅 B904 + E402)
- PII:raw-body / event-dump 都 gate 在 env flag 後,預設只印 sha256
- pending peek-then-confirm 設計本身健全(問題在 C1 的繞鎖,非設計)

---

## 建議修復順序
1. **C1**(資料遺失,鐵律)→ 2. **I1**(quota 損毀自我惡化)→ 3. **I2**(語音永遠沉默)→ 4. **I3 busy_timeout**(跨 process 鎖死)→ 5. I4-I9 → 6. nits

> 每條修復前請走 LINE bot CLAUDE.md 的開發流程(TDD:先寫重現 test FAIL → 修 → PASS → 重啟 uvicorn + curl /health → review → commit/push)。

---

## 全量驗證結果（2026-05-30 fix 回合收尾）
- **全量 pytest：753 passed, 0 failed, 0 error**（115.6s）。修前 748 + 新增 5 條並發/race 重現 test。
- `ruff F,E9` 三檔 exit 0；`ast.parse` 三檔 OK；`import main` OK。
- 副產物佐證：舊固定 tmp 殘留檔 `gemini_usage.json.tmp` 在改用 `mkstemp` 後消失（git 顯示 deleted）→ I1 修對。
- ⛔ 未重啟 production uvicorn（等 Andrew 確認再動正式服務）；未 commit/push。
