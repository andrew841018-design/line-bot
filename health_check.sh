#!/usr/bin/env bash
# line_bot daily health check — run by launchd at 15:00 TW
# Only restarts uvicorn (never cloudflared, to preserve tunnel URL).

set -u

BOT_DIR="/Users/andrew/Desktop/andrew/Data_engineer/line_bot"
HC_LOG="$BOT_DIR/health_check.log"
UVICORN_LOG="$BOT_DIR/uvicorn.log"
PORT=8080
MAX_429_PER_DAY=10

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
say() { echo "[$(ts)] $*" >> "$HC_LOG"; }

{
  echo ""
  echo "=========================================="
} >> "$HC_LOG"
say "Health check start"

UVICORN_UP=0
CF_UP=0
pgrep -f "uvicorn.*main:app" >/dev/null 2>&1 && UVICORN_UP=1
pgrep -f "cloudflared tunnel" >/dev/null 2>&1 && CF_UP=1
say "uvicorn=$([ $UVICORN_UP -eq 1 ] && echo UP || echo DOWN)  cloudflared=$([ $CF_UP -eq 1 ] && echo UP || echo DOWN)"

PT_TODAY=$(TZ='America/Los_Angeles' date '+%m-%d')
COUNT_429=0
if [ -f "$UVICORN_LOG" ]; then
  COUNT_429=$(grep -c "^$PT_TODAY .*429" "$UVICORN_LOG" 2>/dev/null || echo 0)
fi
say "Gemini 429 today (PT $PT_TODAY): $COUNT_429"

ACTION="no_action_needed"

if [ $UVICORN_UP -eq 0 ]; then
  if [ "$COUNT_429" -ge "$MAX_429_PER_DAY" ]; then
    ACTION="skipped_restart_quota_likely_exhausted"
    say "uvicorn DOWN but 429>=$MAX_429_PER_DAY; skipping restart"
  else
    say "uvicorn DOWN; attempting restart (uvicorn only, cloudflared untouched)"
    cd "$BOT_DIR" || { say "ERR cd failed"; exit 1; }

    if [ ! -x .venv/bin/uvicorn ]; then
      say "ERR .venv/bin/uvicorn not found or not executable"
      ACTION="restart_failed_no_venv"
    elif [ ! -f .env ]; then
      say "ERR .env missing"
      ACTION="restart_failed_no_env"
    else
      nohup .venv/bin/uvicorn main:app --host 127.0.0.1 --port $PORT \
        >> "$UVICORN_LOG" 2>&1 &
      NEW_PID=$!
      disown $NEW_PID 2>/dev/null || true
      say "spawned uvicorn PID=$NEW_PID, waiting 5s..."
      sleep 5

      HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
        http://127.0.0.1:$PORT/ 2>/dev/null || echo "000")
      say "post-restart HTTP=$HTTP_CODE"

      case "$HTTP_CODE" in
        2*|3*|4*) ACTION="restart_success_http_$HTTP_CODE" ;;
        *)        ACTION="restart_failed_http_$HTTP_CODE" ;;
      esac
    fi
  fi
fi

if [ $CF_UP -eq 0 ]; then
  say "WARN cloudflared is DOWN — manual intervention required (tunnel URL will change on restart)"
fi

say "action=$ACTION"
say "Health check end"
