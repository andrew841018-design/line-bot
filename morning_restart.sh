#!/usr/bin/env bash
# 每天 08:05 TW 強制重啟 uvicorn，清掉 Gemini quota cache（UTC 00:00 = TW 08:00 quota 重置後）
set -u

BOT_DIR="/Users/andrew/Desktop/andrew/Data_engineer/line_bot"
PORT=8080
LOG="$BOT_DIR/uvicorn.log"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

echo "[$(ts)] morning_restart 開始" >> "$LOG"

# 1. kill 舊 uvicorn
pkill -f "uvicorn.*main:app" 2>/dev/null || true
sleep 2

# 2. 啟動新 uvicorn
cd "$BOT_DIR"
nohup .venv/bin/uvicorn main:app --host 127.0.0.1 --port "$PORT" >> "$LOG" 2>&1 &
sleep 3

# 3. 驗證活著
if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" | grep -qE "^(200|404|405)$"; then
    echo "[$(ts)] morning_restart 成功 PID=$(pgrep -f 'uvicorn.*main:app' | head -1)" >> "$LOG"
    exit 0
else
    echo "[$(ts)] morning_restart 失敗，uvicorn 沒活" >> "$LOG"
    exit 1
fi
