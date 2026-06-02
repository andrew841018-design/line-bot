#!/usr/bin/env bash
# 每天 08:05 TW 強制重啟 uvicorn，清掉 Gemini quota cache（UTC 00:00 = TW 08:00 quota 重置後）
set -u

HOME="${HOME:-/Users/andrew}"
BOT_DIR="/Users/andrew/Desktop/andrew/Data_engineer/line_bot"
PORT=8080
LOG="$HOME/Library/Logs/line_bot_morning_restart.log"
UVICORN_LOG="$HOME/Library/Logs/line_bot_uvicorn.log"
READY_TIMEOUT_SEC="${READY_TIMEOUT_SEC:-75}"
UVICORN_LABEL="com.andrew.line-bot-uvicorn"
UVICORN_PLIST="$HOME/Library/LaunchAgents/${UVICORN_LABEL}.plist"
RESTART_LOCK_DIR="/tmp/line_bot_restart.lockdir"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

wait_for_health() {
    local deadline=$((SECONDS + READY_TIMEOUT_SEC))
    local http_code
    while [ "$SECONDS" -lt "$deadline" ]; do
        http_code=$(curl -s -o /dev/null -w "%{http_code}" --interface lo0 --max-time 3 \
            "http://127.0.0.1:$PORT/health" 2>/dev/null || echo "000")
        if [ "$http_code" = "200" ]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

ensure_uvicorn_service() {
    local domain="gui/$(id -u)"
    if launchctl print "$domain/$UVICORN_LABEL" >/dev/null 2>&1; then
        return 0
    fi
    if [ ! -f "$UVICORN_PLIST" ]; then
        say "ERR missing uvicorn plist: $UVICORN_PLIST"
        return 1
    fi
    launchctl bootstrap "$domain" "$UVICORN_PLIST"
}

acquire_restart_lock() {
    local deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if mkdir "$RESTART_LOCK_DIR" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

release_restart_lock() {
    rmdir "$RESTART_LOCK_DIR" 2>/dev/null || true
}

say "morning_restart 開始"

if ! acquire_restart_lock; then
    say "morning_restart 失敗，restart lock busy"
    exit 1
fi
trap release_restart_lock EXIT

# 1. 由 launchd 管理 uvicorn，避免 morning wrapper exit 時帶掉 child process。
if ! ensure_uvicorn_service; then
    say "morning_restart 失敗，uvicorn launchd service 無法載入"
    exit 1
fi
launchctl kickstart -k "gui/$(id -u)/$UVICORN_LABEL"
say "kickstarted $UVICORN_LABEL"

# 2. 等 FastAPI startup 完成。pending drain / Gemini probe 偶爾會超過固定 sleep。
if ! wait_for_health; then
    say "morning_restart 失敗，/health 在 ${READY_TIMEOUT_SEC}s 內沒 ready"
    exit 1
fi
say "local /health ready"

# 3. preflight ship-gate (local → cloudflared → LINE)
"$BOT_DIR/.venv/bin/python" "$BOT_DIR/preflight_check.py" --force >> "$LOG" 2>&1
PREFLIGHT_EXIT=$?
say "morning_restart preflight exit=$PREFLIGHT_EXIT"
case "$PREFLIGHT_EXIT" in
    0|2) say "morning_restart 成功"; exit 0 ;;
    *)   say "morning_restart preflight 失敗 (exit=$PREFLIGHT_EXIT)"; exit 1 ;;
esac
