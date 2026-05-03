# LINE Bot Project

## Tech Stack
- Python 3.11, FastAPI + uvicorn (port 8080, localhost only)
- LINE Messaging API (webhook `/callback`, signature validated)
- Gemini 2.0 Flash — 20 req/day free tier
- SQLite (`line_bot.db`) — per-user conversation memory, feedback
- PostgreSQL — PTT pipeline read-only queries
- Discord webhooks — daily briefing
- Deployed via launchd on macOS

## Key Files
- `main.py` — webhook handler, all LINE message logic, `_md_to_line()`
- `gemini_client.py` — Gemini API wrapper, quota tracking (`gemini_usage.json`)
- `daily_briefing_discord.py` — daily Discord push
- `ptt_alert.py` — hourly PTT hot-post alert to LINE group
- `memory.py` — per-user conversation memory (SQLite)
- `config.py` — env vars via python-dotenv

---

## 自動開發流程（無需手動下指令）

### 新功能 / 非顯然的改動
當 Andrew 說「幫我加 X」「實作 Y」，自動執行：

1. **Spec（需求確認）**
   - 列出正在假設的前提，請 Andrew 一次性確認或修正
   - 確認完才動手，不猜測需求

2. **Plan（拆工作）**
   - 把功能拆成小 task，每個 task 有明確驗收條件
   - 一次列給 Andrew 看，確認後開始實作

3. **Build（TDD 實作）**，每個 task：
   - 先寫失敗 test（必須看到 FAIL）
   - 再實作最小 code 讓 test 過
   - 跑完整 pytest 確認無 regression

4. **Verify（動態驗證）**
   - 重啟 uvicorn：`pkill -f "uvicorn main:app" && nohup .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 > /tmp/line_bot_restart.log 2>&1 &`
   - curl 確認活著：`curl --interface lo0 http://localhost:8080/health`

5. **Review（自動五軸掃描）**
   - 正確性、可讀性、架構、安全、效能各掃一遍
   - 有 Critical 問題先修，才進下一步

6. **Commit + Push（自動，不等確認）**
   - `git add <changed files> && git commit -m "feat/fix: ..." && git push`

7. **LINE 群推播判斷**
   - 查 `project_line_bot_pending_push.md`
   - 今天未推 → 整合累積項目推到 LINE 群
   - 今天已推 → 記錄到待推清單

### 修 Bug
1. 先寫重現 bug 的 test（必須 FAIL，否則還沒找到根因）
2. 確認 FAIL → 實作修復 → 確認 PASS
3. 跑全 suite → Verify → Review → Commit/Push → LINE 推播判斷

### Push 前 / 重大改動前（Ship）
自動平行召喚三個 reviewer：
- `code-reviewer`：五軸審查
- `security-auditor`：OWASP、token 處理、輸入驗證
- `test-engineer`：測試覆蓋率、edge case 缺口

合併報告，輸出 GO / NO-GO。有 Critical → 先修再 push。

### 小改動（單檔、顯然、非功能性）
Verify → Review（快速）→ Commit/Push → LINE 推播判斷
不需要完整 spec/plan/TDD 流程。

---

## 硬性規則
- 宣稱完成前必須跑動態驗證（pytest + curl），靜態檢查不算
- 所有 LINE 回覆文字必須過 `_md_to_line()` 轉純文字
- 每天 LINE 群最多推一次；當天後續改動記到待推清單
- Gemini 改 model 前先查 `project_gemini_model_selection.md`
- 所有 token/key 從 `.env` 讀，不 hardcode，不 log

---

## 已知坑：launchd push job「為什麼一陣子沒推」

### 2026-05-03 事件複盤

**症狀**：Andrew 反映「感覺有一陣子沒收到回饋問題」。今早 scheduled update 報告也顯示 `com.andrew.line-bot-feedback-push` exit=1。

**第一次誤判**：以為 LINE 月配額爆 + launchd 沒實際 fire 兩個原因混合。實際查 `feedback_push_stderr.log` 最後寫入 4/27 20:00（429 error）後就沒新內容；LINE quota API 顯示 200 額度只用 72，根本沒爆。

**真實根因**（看了 plist 才知道）：
1. `com.andrew.line-bot-feedback-push.plist` 早就改成**每週一 20:00**（不是每天）—— 文件 / 記憶都還停在「每天推」的舊版印象
2. 4/27 那唯一一次 fire 撞到當時 LINE 月配額剛爆（4 月底用爆），429 失敗 → 群組實際沒收到
3. 4/28~5/3 沒 fire 是週排程的正常 6 天 gap，不是 bug
4. `launchctl list` 的 last exit=1 是 4/27 的 sticky 緩存值，不是「最近一次失敗」

**用戶感受對應**：上一次真正成功推送是 4/20 週一，到 5/3 = 13 天沒問 → 確實「有一陣子沒問了」這個感覺成立。

### 排查清單（下次再被類似現象誤導前先跑這幾條）

```bash
# 1. 確認 plist 實際排程（不要靠記憶）
cat ~/Library/LaunchAgents/com.andrew.line-bot-feedback-push.plist | grep -A1 -E "Weekday|Hour|Minute"

# 2. 看 stderr 最後寫入時間（推測上次 fire 時點）
ls -la ~/Library/Logs/line_bot_feedback_push_stderr.log
tail -30 ~/Library/Logs/line_bot_feedback_push_stderr.log

# 3. LINE quota 實際狀態（不要假設爆了）
.venv/bin/python -c "
import os, requests
from dotenv import load_dotenv; load_dotenv()
t = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
print(requests.get('https://api.line.me/v2/bot/message/quota', headers={'Authorization': f'Bearer {t}'}).json())
print(requests.get('https://api.line.me/v2/bot/message/quota/consumption', headers={'Authorization': f'Bearer {t}'}).json())
"

# 4. quota_state.json ≠ LINE quota（是 Gemini 的，別搞混）
cat quota_state.json
```

### macOS launchd `StartCalendarInterval` 的睡眠陷阱

**Mac 在排程時間是 sleep 狀態 → launchd 直接跳過該觸發點，不會醒來補打**（不像 anacron）。
解法二選一：

| 方案 | 做法 | 適用 |
|---|---|---|
| 改時段到「Mac 必定醒著」的時間 | 編輯 plist `Hour`/`Minute`/`Weekday`，`launchctl unload + load` | Andrew 作息穩定的時段 |
| `pmset repeat wake` 強制喚醒 | `sudo pmset repeat wake <字母> HH:MM:SS` | 需固定時段又無法保證在用 Mac |

### pmset weekday 字母對照（容易踩雷）

| 字母 | 星期 |
|---|---|
| M | Monday |
| T | Tuesday |
| W | Wednesday |
| **R** | Thursday（不是 Th！T 已被佔）|
| F | Friday |
| **S** | **Saturday**（不是 Sunday！）|
| **U** | **Sunday** |

`pmset repeat` 只能設**一組**排程，新指令會覆蓋舊的；`pmset -g sched` 看目前狀態。需 sudo，**密碼絕對在自己 terminal 輸**，不要丟進 Claude Code chat。

### 當前設定（2026-05-03 更新）

- plist：每週日 20:00 推
- pmset：每週日 19:55 wake
- 下次 fire：5/10 週日 20:00
