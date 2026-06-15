#!/usr/bin/env bash
set -uo pipefail

SERVICE_NAME="${QUEUE_BOT_SERVICE_NAME:-queue-bot.service}"
PROJECT_DIR="${QUEUE_BOT_PROJECT_DIR:-/home/pi/Queue_Bot}"
ENV_FILE="${QUEUE_BOT_ENV_FILE:-$PROJECT_DIR/.env}"
LOG_FILE="${QUEUE_BOT_WATCHDOG_LOG:-$PROJECT_DIR/logs/raspberry-watchdog.log}"
STATE_FILE="${QUEUE_BOT_WATCHDOG_STATE:-/var/tmp/queue-bot-watchdog.failures}"
HEALTH_URL="${QUEUE_BOT_HEALTH_URL:-http://127.0.0.1:8080/healthz}"
NETWORK_URL="${QUEUE_BOT_NETWORK_URL:-https://api.telegram.org}"
MAX_FAILURES="${QUEUE_BOT_WATCHDOG_MAX_FAILURES:-3}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
  printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE" >/dev/null
}

read_env_value() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 1
  fi
  grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | sed -E "s/^${key}=//; s/^['\"]//; s/['\"]$//"
}

telegram_alert() {
  local message="$1"
  local token admin_ids first_admin
  token="$(read_env_value TELEGRAM_BOT_TOKEN || true)"
  admin_ids="$(read_env_value ADMIN_USER_IDS || true)"
  first_admin="${admin_ids%%,*}"
  first_admin="${first_admin// /}"
  if [ -z "$token" ] || [ -z "$first_admin" ]; then
    return 0
  fi
  curl -fsS --max-time 10 \
    --data-urlencode "chat_id=$first_admin" \
    --data-urlencode "text=$message" \
    "https://api.telegram.org/bot${token}/sendMessage" >/dev/null 2>&1 || true
}

failure_count() {
  if [ -f "$STATE_FILE" ]; then
    cat "$STATE_FILE" 2>/dev/null || printf '0'
  else
    printf '0'
  fi
}

set_failure_count() {
  printf '%s' "$1" > "$STATE_FILE" 2>/dev/null || true
}

restart_network() {
  log "network recovery: restarting available network services"
  systemctl restart NetworkManager >/dev/null 2>&1 || true
  systemctl restart dhcpcd >/dev/null 2>&1 || true
  systemctl restart systemd-networkd >/dev/null 2>&1 || true
}

check_database() {
  local database_path
  database_path="$(read_env_value DATABASE_PATH || true)"
  [ -n "$database_path" ] || database_path="./data/bot.sqlite3"
  case "$database_path" in
    /*) ;;
    *) database_path="$PROJECT_DIR/$database_path" ;;
  esac
  if [ ! -f "$database_path" ]; then
    log "database check: missing $database_path"
    return 1
  fi
  if command -v sqlite3 >/dev/null 2>&1; then
    local result
    result="$(sqlite3 "$database_path" 'PRAGMA quick_check;' 2>&1 || true)"
    if [ "$result" != "ok" ]; then
      log "database check: quick_check failed: $result"
      return 1
    fi
  fi
  return 0
}

main() {
  local failed=0
  local reasons=()

  if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    reasons+=("service_inactive")
    log "service check: $SERVICE_NAME inactive, restarting"
    systemctl restart "$SERVICE_NAME" >/dev/null 2>&1 || true
    failed=1
  fi

  if ! curl -fsS --max-time 8 "$HEALTH_URL" >/dev/null 2>&1; then
    reasons+=("health_unreachable")
    log "health check: $HEALTH_URL unreachable, restarting $SERVICE_NAME"
    systemctl restart "$SERVICE_NAME" >/dev/null 2>&1 || true
    failed=1
  fi

  if ! curl -fsS --max-time 12 "$NETWORK_URL" >/dev/null 2>&1; then
    reasons+=("network_unreachable")
    restart_network
    failed=1
  fi

  if ! check_database; then
    reasons+=("database_problem")
    failed=1
  fi

  if [ "$failed" -eq 0 ]; then
    if [ "$(failure_count)" != "0" ]; then
      log "all checks healthy again"
    fi
    set_failure_count 0
    return 0
  fi

  local count
  count="$(failure_count)"
  case "$count" in
    ''|*[!0-9]*) count=0 ;;
  esac
  count=$((count + 1))
  set_failure_count "$count"
  log "watchdog failure #$count/$MAX_FAILURES reasons=${reasons[*]}"

  if [ "$count" -eq 1 ]; then
    telegram_alert "Queue Bot watchdog: rilevato problema su Raspberry (${reasons[*]}). Provo recovery automatica."
  fi

  if [ "$count" -ge "$MAX_FAILURES" ]; then
    log "failure threshold reached, rebooting Raspberry"
    telegram_alert "Queue Bot watchdog: recovery fallita ${count} volte. Riavvio il Raspberry ora."
    sync || true
    systemctl reboot --force
  fi
}

main "$@"
