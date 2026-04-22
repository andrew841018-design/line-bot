#!/usr/bin/env bash
# line_bot daily health check — run by launchd at 15:00 TW
# Restarts uvicorn and/or cloudflared if down; auto-updates LINE webhook URL on cloudflared restart.

set -u

BOT_DIR="/Users/andrew/Desktop/andrew/Data_engineer/line_bot"
HC_LOG="$BOT_DIR/health_check.log"
UVICORN_LOG="$BOT_DIR/uvicorn.log"
CF_LOG="$BOT_DIR/cloudflared.log"
CF_BIN="/Users/andrew/.local/bin/cloudflared"
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
  say "cloudflared DOWN; attempting restart..."
  cd "$BOT_DIR" || { say "ERR cd failed"; exit 1; }

  LINE_TOKEN=$(grep '^LINE_CHANNEL_ACCESS_TOKEN=' .env | cut -d= -f2-)
  if [ -z "$LINE_TOKEN" ]; then
    say "ERR LINE_CHANNEL_ACCESS_TOKEN not found in .env"
    CF_ACTION="cf_restart_failed_no_token"
  elif [ ! -x "$CF_BIN" ]; then
    say "ERR cloudflared binary not found at $CF_BIN"
    CF_ACTION="cf_restart_failed_no_binary"
  else
    : > "$CF_LOG"
    nohup "$CF_BIN" tunnel --url http://127.0.0.1:$PORT >> "$CF_LOG" 2>&1 &
    CF_NEW_PID=$!
    disown $CF_NEW_PID 2>/dev/null || true
    say "spawned cloudflared PID=$CF_NEW_PID, waiting for URL..."

    NEW_URL=""
    for i in $(seq 1 30); do
      sleep 1
      NEW_URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$CF_LOG" | head -n1 || true)
      [ -n "$NEW_URL" ] && break
    done

    if [ -z "$NEW_URL" ]; then
      say "ERR cloudflared URL not found after 30s"
      CF_ACTION="cf_restart_failed_no_url"
    else
      say "cloudflared new URL=$NEW_URL, verifying tunnel is ready..."
      TUNNEL_READY=0
      for i in $(seq 1 15); do
        sleep 2
        TUNNEL_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${NEW_URL}/health" 2>/dev/null || echo "000")
        say "tunnel verify attempt=$i HTTP=$TUNNEL_HTTP"
        [ "$TUNNEL_HTTP" = "200" ] && TUNNEL_READY=1 && break
      done

      if [ $TUNNEL_READY -eq 0 ]; then
        say "WARN tunnel URL obtained but /health unreachable after 30s, updating LINE webhook anyway"
      fi

      WEBHOOK_URL="${NEW_URL}/callback"
      HTTP_RESP=$(curl -s -o /tmp/line_webhook_resp.txt -w "%{http_code}" \
        -X PUT https://api.line.me/v2/bot/channel/webhook/endpoint \
        -H "Authorization: Bearer $LINE_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"endpoint\": \"$WEBHOOK_URL\"}" 2>/dev/null)
      [ -z "$HTTP_RESP" ] && HTTP_RESP="000"
      RESP_BODY=$(cat /tmp/line_webhook_resp.txt 2>/dev/null || echo "")
      say "LINE webhook update HTTP=$HTTP_RESP body=$RESP_BODY"

      if [ "$HTTP_RESP" = "200" ]; then
        CF_ACTION="cf_restart_success_webhook_updated url=$WEBHOOK_URL"
      else
        CF_ACTION="cf_restart_success_tunnel_up_webhook_failed HTTP=$HTTP_RESP"
        say "WARN tunnel is up but LINE webhook update failed"
      fi
    fi
  fi
  say "cf_action=$CF_ACTION"
fi

say "action=$ACTION"
say "Health check end"
