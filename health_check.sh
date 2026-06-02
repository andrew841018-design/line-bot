#!/usr/bin/env bash
# line_bot daily health check — run by launchd at 15:00 TW
# Restarts uvicorn and/or cloudflared if down; auto-updates LINE webhook URL on cloudflared restart.

set -u

HOME="${HOME:-/Users/andrew}"
BOT_DIR="/Users/andrew/Desktop/andrew/Data_engineer/line_bot"
HC_LOG="$BOT_DIR/health_check.log"
UVICORN_LOG="$BOT_DIR/uvicorn.log"
PORT=8080
MAX_429_PER_DAY=10
READY_TIMEOUT_SEC="${READY_TIMEOUT_SEC:-75}"
UVICORN_LABEL="com.andrew.line-bot-uvicorn"
UVICORN_PLIST="$HOME/Library/LaunchAgents/${UVICORN_LABEL}.plist"
CLOUDFLARED_LABEL="com.andrew.line-bot-cloudflared"
CLOUDFLARED_PLIST="$HOME/Library/LaunchAgents/${CLOUDFLARED_LABEL}.plist"
CLOUDFLARED_URL_FILE="/tmp/cloudflared_line_bot_url.txt"
RESTART_LOCK_DIR="/tmp/line_bot_restart.lockdir"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
say() { echo "[$(ts)] $*" >> "$HC_LOG"; }

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

ensure_cloudflared_service() {
  local domain="gui/$(id -u)"
  if launchctl print "$domain/$CLOUDFLARED_LABEL" >/dev/null 2>&1; then
    return 0
  fi
  if [ ! -f "$CLOUDFLARED_PLIST" ]; then
    say "ERR missing cloudflared plist: $CLOUDFLARED_PLIST"
    return 1
  fi
  launchctl bootstrap "$domain" "$CLOUDFLARED_PLIST"
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

{
  echo ""
  echo "=========================================="
} >> "$HC_LOG"
say "Health check start"

UVICORN_UP=0
CF_UP=0
pgrep -f "uvicorn.*main:app" >/dev/null 2>&1 && UVICORN_UP=1
pgrep -f "cloudflared tunnel" >/dev/null 2>&1 && CF_UP=1

# 殭屍偵測：pgrep 活但 /health 死（import error / hung）→ 強制走重啟分支
# 純 pgrep 抓不到「process 活但 listen 卡住」這類隱性故障
if [ $UVICORN_UP -eq 1 ]; then
  HC=$(curl -s -o /dev/null -w "%{http_code}" --interface lo0 --max-time 3 \
    http://127.0.0.1:$PORT/health 2>/dev/null || echo "000")
  if [ "$HC" != "200" ]; then
    say "uvicorn pgrep UP but /health=$HC → treating as DOWN (zombie)"
    UVICORN_UP=0
  fi
fi

say "uvicorn=$([ $UVICORN_UP -eq 1 ] && echo UP || echo DOWN)  cloudflared=$([ $CF_UP -eq 1 ] && echo UP || echo DOWN)"

PT_TODAY=$(TZ='America/Los_Angeles' date '+%m-%d')
COUNT_429=0
if [ -f "$UVICORN_LOG" ]; then
  COUNT_429=$(grep -c "^$PT_TODAY .*429" "$UVICORN_LOG" 2>/dev/null || echo 0)
fi
say "Gemini 429 today (PT $PT_TODAY): $COUNT_429"

ACTION="no_action_needed"

if [ $UVICORN_UP -eq 0 ]; then
    say "uvicorn DOWN; attempting restart (Gemini quota never suppresses receiver restart)"
    cd "$BOT_DIR" || { say "ERR cd failed"; exit 1; }

    if [ ! -x .venv/bin/uvicorn ]; then
      say "ERR .venv/bin/uvicorn not found or not executable"
      ACTION="restart_failed_no_venv"
    elif [ ! -f .env ]; then
      say "ERR .env missing"
      ACTION="restart_failed_no_env"
    elif ! acquire_restart_lock; then
      say "ERR restart lock timeout (another restart is running)"
      ACTION="restart_deferred_lock_busy"
    else
      if ! ensure_uvicorn_service; then
        ACTION="restart_failed_launchd_service"
      else
        launchctl kickstart -k "gui/$(id -u)/$UVICORN_LABEL"
        say "kickstarted $UVICORN_LABEL"
        if wait_for_health; then
          say "post-restart /health HTTP=200; running preflight"
          "$BOT_DIR/.venv/bin/python" "$BOT_DIR/preflight_check.py" --force >> "$HC_LOG" 2>&1
          PREFLIGHT_EXIT=$?
          say "uvicorn restart preflight exit=$PREFLIGHT_EXIT"
          case "$PREFLIGHT_EXIT" in
            0|2) ACTION="restart_success_http_200_preflight_${PREFLIGHT_EXIT}" ;;
            *)   ACTION="restart_failed_preflight_${PREFLIGHT_EXIT}" ;;
          esac
        else
          say "post-restart /health timeout after ${READY_TIMEOUT_SEC}s"
          ACTION="restart_failed_health_timeout"
        fi
      fi
      release_restart_lock
    fi
fi

if [ $CF_UP -eq 0 ]; then
  say "cloudflared DOWN; attempting launchd restart..."
  if ! ensure_cloudflared_service; then
    CF_ACTION="cf_restart_failed_launchd_service"
  else
    launchctl kickstart -k "gui/$(id -u)/$CLOUDFLARED_LABEL"
    say "kickstarted $CLOUDFLARED_LABEL, waiting for URL stash..."
    NEW_URL=""
    for i in $(seq 1 90); do
      sleep 1
      if [ -s "$CLOUDFLARED_URL_FILE" ]; then
        NEW_URL=$(cat "$CLOUDFLARED_URL_FILE")
      fi
      [ -n "$NEW_URL" ] && break
    done

    if [ -z "$NEW_URL" ]; then
      say "ERR cloudflared URL not found after 90s"
      CF_ACTION="cf_restart_failed_no_url"
    else
      say "cloudflared new URL=$NEW_URL; running preflight to align LINE webhook"
      "$BOT_DIR/.venv/bin/python" "$BOT_DIR/preflight_check.py" --force >> "$HC_LOG" 2>&1
      PREFLIGHT_EXIT=$?
      say "cloudflared restart preflight exit=$PREFLIGHT_EXIT"
      if [ "$PREFLIGHT_EXIT" = "0" ] || [ "$PREFLIGHT_EXIT" = "2" ]; then
        CF_ACTION="cf_restart_success_preflight_${PREFLIGHT_EXIT} url=${NEW_URL}/callback"
      else
        CF_ACTION="cf_restart_failed_preflight_${PREFLIGHT_EXIT}"
      fi
    fi
  fi
  say "cf_action=$CF_ACTION"
fi

say "action=$ACTION"
say "Health check end"
