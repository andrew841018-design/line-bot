#!/usr/bin/env bash
# 啟動 LINE bot：uvicorn 在 127.0.0.1:8080 + Cloudflare quick tunnel。
# Ctrl+C 會同時關閉這兩個背景程序。

set -euo pipefail
cd "$(dirname "$0")"

# ── 1. 檢查 .env ─────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "❌ 找不到 .env，請先 cp .env.example .env 並填入 3 個金鑰"
    exit 1
fi

# ── 2. 確認 venv 在 ──────────────────────────────────────────────────────
if [ ! -x .venv/bin/uvicorn ]; then
    echo "→ 初始化 venv..."
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
fi

# ── 3. 找 cloudflared ────────────────────────────────────────────────────
if command -v cloudflared >/dev/null 2>&1; then
    CF=$(command -v cloudflared)
elif [ -x "$HOME/.local/bin/cloudflared" ]; then
    CF="$HOME/.local/bin/cloudflared"
else
    echo "❌ 找不到 cloudflared 執行檔"
    exit 1
fi

# ── 4. 清空 log ──────────────────────────────────────────────────────────
: > uvicorn.log
: > cloudflared.log

# ── 5. 背景啟動 uvicorn ──────────────────────────────────────────────────
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 > uvicorn.log 2>&1 &
UVICORN_PID=$!
echo "→ uvicorn PID=$UVICORN_PID (log: uvicorn.log)"

# ── 6. 背景啟動 cloudflared quick tunnel ────────────────────────────────
"$CF" tunnel --url http://127.0.0.1:8080 > cloudflared.log 2>&1 &
CF_PID=$!
echo "→ cloudflared PID=$CF_PID (log: cloudflared.log)"

# ── 7. Ctrl+C 同時關兩個程序 ─────────────────────────────────────────────
cleanup() {
    echo ""
    echo "→ 關閉中..."
    kill "$UVICORN_PID" "$CF_PID" 2>/dev/null || true
    wait "$UVICORN_PID" "$CF_PID" 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM

# ── 8. 等 tunnel URL 冒出來（最多 30 秒）────────────────────────────────
URL=""
for i in $(seq 1 30); do
    sleep 1
    URL=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' cloudflared.log | head -n1 || true)
    [ -n "$URL" ] && break
done

if [ -z "$URL" ]; then
    echo "❌ 30 秒內沒從 cloudflared.log 看到 tunnel URL"
    echo "   tail -20 cloudflared.log："
    tail -20 cloudflared.log || true
    cleanup
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "✅ 服務上線"
echo ""
echo "   本機         : http://127.0.0.1:8080"
echo "   Health check : ${URL}/health"
echo "   Webhook URL  : ${URL}/callback"
echo ""
echo "   → 把 Webhook URL 貼進 LINE Developers Console"
echo "     Messaging API → Webhook URL → Verify → Enable webhooks"
echo ""
echo "   Ctrl+C 結束（會同時關閉 uvicorn 和 tunnel）"
echo "════════════════════════════════════════════════════════════════"

wait
