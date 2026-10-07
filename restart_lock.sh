#!/usr/bin/env bash
# Shared crash-safe uvicorn restart lock for LINE bot maintenance jobs.
# A dedicated Python child owns the non-inheritable lock fd. It exits when its
# direct shell parent exits, so unrelated shell children cannot retain the lock.

RESTART_LOCK_TIMEOUT_SEC="${RESTART_LOCK_TIMEOUT_SEC:-30}"
RESTART_LOCK_FILE="${LINE_BOT_RESTART_LOCK_FILE:-${BOT_DIR}/state/uvicorn_restart.lock}"
RESTART_LOCK_PYTHON="${LINE_BOT_RESTART_LOCK_PYTHON:-${BOT_DIR}/.venv/bin/python}"
RESTART_LOCK_HOLDER="${LINE_BOT_RESTART_LOCK_HOLDER:-${BOT_DIR}/restart_lock_holder.py}"
RESTART_LOCK_HELD=0
RESTART_LOCK_ERROR_KIND=""
RESTART_LOCK_HOLDER_PID=""
RESTART_LOCK_STATUS_FILE=""

_restart_lock_stat_uid() {
  stat -f '%u' "$1" 2>/dev/null || stat -c '%u' "$1" 2>/dev/null
}

_restart_lock_stat_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null
}

_restart_lock_parent_is_private() {
  local parent owner mode
  parent=${RESTART_LOCK_FILE%/*}
  [ "$parent" != "$RESTART_LOCK_FILE" ] || return 1
  [ -d "$parent" ] && [ ! -L "$parent" ] || return 1
  owner=$(_restart_lock_stat_uid "$parent") || return 1
  mode=$(_restart_lock_stat_mode "$parent") || return 1
  [ "$owner" = "$(id -u)" ] && [ "$mode" = "700" ]
}

acquire_restart_lock() {
  local parent old_umask status deadline holder_rc
  RESTART_LOCK_ERROR_KIND="error"
  case "$RESTART_LOCK_TIMEOUT_SEC" in
    ''|*[!0-9]*) return 2 ;;
  esac
  _restart_lock_parent_is_private || return 2
  [ -x "$RESTART_LOCK_PYTHON" ] && [ -r "$RESTART_LOCK_HOLDER" ] || return 2
  parent=${RESTART_LOCK_FILE%/*}
  old_umask=$(umask)
  umask 077
  RESTART_LOCK_STATUS_FILE=$(mktemp "$parent/.restart-lock-status.XXXXXX") || {
    umask "$old_umask"
    return 2
  }
  umask "$old_umask"
  "$RESTART_LOCK_PYTHON" "$RESTART_LOCK_HOLDER" \
    --status-file "$RESTART_LOCK_STATUS_FILE" \
    --lock-file "$RESTART_LOCK_FILE" \
    --parent-pid "${BASHPID:-$$}" \
    --timeout "$RESTART_LOCK_TIMEOUT_SEC" &
  RESTART_LOCK_HOLDER_PID=$!
  deadline=$((SECONDS + RESTART_LOCK_TIMEOUT_SEC + 5))
  status=""
  while [ "$SECONDS" -le "$deadline" ]; do
    status=$(head -n 1 "$RESTART_LOCK_STATUS_FILE" 2>/dev/null || true)
    case "$status" in
      acquired)
        if kill -0 "$RESTART_LOCK_HOLDER_PID" 2>/dev/null; then
          rm -f "$RESTART_LOCK_STATUS_FILE"
          RESTART_LOCK_STATUS_FILE=""
          RESTART_LOCK_HELD=1
          RESTART_LOCK_ERROR_KIND=""
          return 0
        fi
        ;;
      busy|error) break ;;
    esac
    if ! kill -0 "$RESTART_LOCK_HOLDER_PID" 2>/dev/null; then
      break
    fi
    sleep 0.02
  done
  wait "$RESTART_LOCK_HOLDER_PID" 2>/dev/null
  holder_rc=$?
  RESTART_LOCK_HOLDER_PID=""
  # The holder may write its final status and exit between the loop's read and
  # liveness check. Re-read after wait so rc=75 contention stays distinguishable.
  status=$(head -n 1 "$RESTART_LOCK_STATUS_FILE" 2>/dev/null || true)
  rm -f "$RESTART_LOCK_STATUS_FILE"
  RESTART_LOCK_STATUS_FILE=""
  if [ "$status" = "busy" ] && [ "$holder_rc" -eq 75 ]; then
    RESTART_LOCK_ERROR_KIND="busy"
    return 1
  fi
  return 2
}

release_restart_lock() {
  if [ "$RESTART_LOCK_HELD" -eq 1 ]; then
    kill "$RESTART_LOCK_HOLDER_PID" 2>/dev/null || true
    wait "$RESTART_LOCK_HOLDER_PID" 2>/dev/null || true
    RESTART_LOCK_HELD=0
    RESTART_LOCK_HOLDER_PID=""
  fi
  if [ -n "$RESTART_LOCK_STATUS_FILE" ]; then
    rm -f "$RESTART_LOCK_STATUS_FILE"
    RESTART_LOCK_STATUS_FILE=""
  fi
}
