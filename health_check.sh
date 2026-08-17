#!/usr/bin/env bash
# line_bot daily health check — run by launchd at 15:00 TW
# Restarts uvicorn/cloudflared if down and always validates LINE → cloudflared → /callback.
# A live cloudflared process is not enough: quick-tunnel URLs can go stale while pgrep stays UP.

set -u

HOME="${HOME:-/Users/andrew}"
BOT_DIR="${BOT_DIR:-/Users/andrew/Desktop/andrew/Data_engineer/line_bot}"
HC_LOG="${HC_LOG:-$BOT_DIR/health_check.log}"
UVICORN_LOG="$BOT_DIR/uvicorn.log"
PORT=8080
MAX_429_PER_DAY=10
READY_TIMEOUT_SEC="${READY_TIMEOUT_SEC:-75}"
UVICORN_LABEL="com.andrew.line-bot-uvicorn"
UVICORN_PLIST="$HOME/Library/LaunchAgents/${UVICORN_LABEL}.plist"
CLOUDFLARED_URL_FILE="${CLOUDFLARED_URL_FILE:-/tmp/cloudflared_line_bot_url.txt}"
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

PREFLIGHT_RAN=0
PREFLIGHT_EXIT=""

run_preflight() {
  local reason="$1"
  PREFLIGHT_RAN=1
  if [ ! -x "$BOT_DIR/.venv/bin/python" ]; then
    PREFLIGHT_EXIT=127
    say "ERR preflight python not found: $BOT_DIR/.venv/bin/python"
    return 0
  fi

  say "$reason; running full-path preflight"
  "$BOT_DIR/.venv/bin/python" "$BOT_DIR/preflight_check.py" --force >> "$HC_LOG" 2>&1
  PREFLIGHT_EXIT=$?
  say "$reason preflight exit=$PREFLIGHT_EXIT"
  return 0
}

preflight_is_acceptable() {
  [ "$1" = "0" ] || [ "$1" = "2" ]
}

quick_tunnel_api_dns_ready() {
  "$BOT_DIR/.venv/bin/python" "$BOT_DIR/quick_tunnel_dns.py" --timeout 5 \
    >/dev/null 2>&1
}

recover_cloudflared() {
  say "cloudflared DOWN; delegating restart + fresh generation + webhook E2E to preflight..."
  if ! quick_tunnel_api_dns_ready; then
    say "ERR cloudflared restart blocked: system resolver cannot resolve api.trycloudflare.com; VPN/private DNS may be blocking it"
    CF_ACTION="cf_restart_blocked_dns"
  else
    PREVIOUS_URL=""
    if [ -s "$CLOUDFLARED_URL_FILE" ]; then
      PREVIOUS_URL=$(cat "$CLOUDFLARED_URL_FILE")
    fi
    # preflight is the sole recovery owner: it acquires the shared lock and
    # keeps restart -> fresh stash -> generation-fenced PUT -> E2E atomic.
    run_preflight "cloudflared down recovery"
    CANDIDATE_URL=""
    if [ -s "$CLOUDFLARED_URL_FILE" ]; then
      CANDIDATE_URL=$(cat "$CLOUDFLARED_URL_FILE")
    fi
    if preflight_is_acceptable "$PREFLIGHT_EXIT" \
      && [[ "$CANDIDATE_URL" =~ ^https://[a-z0-9-]+\.trycloudflare\.com$ ]] \
      && [ "$CANDIDATE_URL" != "https://api.trycloudflare.com" ] \
      && [ "$CANDIDATE_URL" != "$PREVIOUS_URL" ]; then
      CF_UP=1
      CF_ACTION="cf_restart_success_preflight_${PREFLIGHT_EXIT} url=${CANDIDATE_URL}/callback"
    elif preflight_is_acceptable "$PREFLIGHT_EXIT"; then
      say "ERR preflight returned success without a fresh valid tunnel generation"
      CF_ACTION="cf_restart_failed_no_fresh_url"
    else
      CF_ACTION="cf_restart_failed_preflight_${PREFLIGHT_EXIT}"
    fi
  fi
  say "cf_action=$CF_ACTION"
}

if [ "${HEALTH_CHECK_SOURCE_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

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
  COUNT_429=$(grep -c "^$PT_TODAY .*429" "$UVICORN_LOG" 2>/dev/null || true)
  COUNT_429=${COUNT_429:-0}
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
          UVICORN_UP=1
          if [ $CF_UP -eq 1 ]; then
            run_preflight "uvicorn restart"
            if preflight_is_acceptable "$PREFLIGHT_EXIT"; then
              ACTION="restart_success_http_200_preflight_${PREFLIGHT_EXIT}"
            else
              ACTION="restart_failed_preflight_${PREFLIGHT_EXIT}"
            fi
          else
            say "uvicorn restarted; defer full-path preflight until cloudflared is ready"
            ACTION="restart_success_http_200_preflight_deferred_cf_down"
          fi
        else
          say "post-restart /health timeout after ${READY_TIMEOUT_SEC}s"
          ACTION="restart_failed_health_timeout"
        fi
      fi
      release_restart_lock
    fi
fi

if [ $CF_UP -eq 0 ]; then
  recover_cloudflared
fi

if [ "$PREFLIGHT_RAN" -eq 0 ] && [ $UVICORN_UP -eq 1 ] && [ $CF_UP -eq 1 ]; then
  run_preflight "processes UP verification"
  if preflight_is_acceptable "$PREFLIGHT_EXIT"; then
    ACTION="processes_up_preflight_${PREFLIGHT_EXIT}"
  else
    ACTION="processes_up_preflight_failed_${PREFLIGHT_EXIT}"
  fi
elif [ "$PREFLIGHT_RAN" -eq 0 ]; then
  say "skip full-path preflight because process status is not ready: uvicorn=$UVICORN_UP cloudflared=$CF_UP"
fi

say "action=$ACTION"
say "Health check end"
