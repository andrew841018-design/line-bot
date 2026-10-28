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
RESTART_LOCK_LIB="$BOT_DIR/restart_lock.sh"

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

if [ ! -r "$RESTART_LOCK_LIB" ]; then
    say "morning_restart 失敗，restart lock helper 不存在"
    exit 1
fi
# shellcheck source=restart_lock.sh
source "$RESTART_LOCK_LIB"

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

# 2026-10-24: a restart kills whatever is being handled right now -- a burst waiting in its 8 s
# window, a model call, a reply on its way -- and the family gets no answer.  Give those a moment:
# at most $1 seconds (default 25) of waiting for inbound events started in the last $2 seconds
# (default 90) to finish.  It never fails the restart.  rc 0 = nothing in flight; rc 1 = still in
# flight when the wait ran out; rc 2 = could not tell (the reason goes to the log: an error type,
# never row data or error text).
# 2026-10-29: the count runs on the bot's own Python.  macOS's /usr/bin/sqlite3 -readonly (and
# /usr/bin/python3) cannot open the WAL database while no connection holds its -wal/-shm files,
# which is most of the time, so the old check failed silently and the wait never happened.
wait_for_inflight_messages() {
    local max_wait="${1:-25}" recent="${2:-90}" db py deadline n py_rc
    db="${LINE_BOT_DB:-${BOT_DIR:-.}/line_bot.db}"
    py="${BOT_DIR:-.}/.venv/bin/python"
    case "$max_wait$recent" in ''|*[!0-9]*) say "inflight check skipped: bad arguments"; return 2 ;; esac
    if [ ! -r "$db" ]; then say "inflight check skipped: no readable database"; return 2; fi
    if [ ! -x "$py" ]; then say "inflight check skipped: no bot Python"; return 2; fi
    deadline=$((SECONDS + max_wait))
    while :; do
        n=$("$py" -I -S -c '
try:
    import pathlib, signal, sqlite3, sys, time
    signal.signal(signal.SIGALRM, signal.SIG_DFL)  # an inherited "ignore" would disarm the alarm
    signal.alarm(5)
    conn = sqlite3.connect(pathlib.Path(sys.argv[1]).absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
    print(conn.execute(
        "SELECT COUNT(*) FROM inbound_events WHERE status IN (?, ?, ?) AND updated_at > ?",
        ("processing", "media_processing", "reply_sending", int(time.time()) - int(sys.argv[2])),
    ).fetchone()[0])
except Exception as exc:
    print("ERR", type(exc).__name__, getattr(exc, "sqlite_errorname", ""))
' "$db" "$recent" 2>/dev/null)
        py_rc=$?
        case "$n" in
            ''|*[!0-9]*)
                [ "$py_rc" -eq 142 ] && n="ERR Timeout"  # killed by its own 5 s alarm (128 + SIGALRM)
                say "inflight check failed: ${n:-no output}"
                return 2 ;;
        esac
        [ "$n" -eq 0 ] && return 0
        [ "$SECONDS" -ge "$deadline" ] && return 1
        sleep 1
    done
}

if [ "${MORNING_RESTART_SOURCE_ONLY:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

say "morning_restart 開始"

if ! acquire_restart_lock; then
    if [ "$RESTART_LOCK_ERROR_KIND" = "busy" ]; then
        say "morning_restart 失敗，restart lock busy"
    else
        say "morning_restart 失敗，restart lock setup/validation error"
    fi
    exit 1
fi
trap release_restart_lock EXIT

# 1. 由 launchd 管理 uvicorn，避免 morning wrapper exit 時帶掉 child process。
if ! ensure_uvicorn_service; then
    say "morning_restart 失敗，uvicorn launchd service 無法載入"
    exit 1
fi
# Let the messages being handled right now finish (at most 25 s; never blocks the restart longer).
inflight_started=$SECONDS
wait_for_inflight_messages 25 90
inflight_rc=$?
say "inflight wait rc=$inflight_rc waited=$((SECONDS - inflight_started))s"
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
