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
