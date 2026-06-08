#!/usr/bin/env bash
set -euo pipefail

# WSL-friendly autostart launcher for Hermes WebUI.
#
# Safe defaults:
# - derives the repo from this script location, override with HERMES_WEBUI_REPO
# - uses a lock + pid file to avoid duplicate starts
# - treats a healthy /health endpoint as "already running"
# - writes logs under ~/.hermes/webui/logs unless HERMES_WEBUI_LOG_DIR is set

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HERMES_WEBUI_REPO="${HERMES_WEBUI_REPO:-${DEFAULT_REPO}}"
HERMES_WEBUI_LOG_DIR="${HERMES_WEBUI_LOG_DIR:-${HOME}/.hermes/webui/logs}"
HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-127.0.0.1}"
HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8787}"
HERMES_WEBUI_HEALTH_HOST="${HERMES_WEBUI_HEALTH_HOST:-127.0.0.1}"
HERMES_WEBUI_HEALTH_URL="${HERMES_WEBUI_HEALTH_URL:-http://${HERMES_WEBUI_HEALTH_HOST}:${HERMES_WEBUI_PORT}/health}"
HERMES_WEBUI_PID_FILE="${HERMES_WEBUI_PID_FILE:-${HERMES_WEBUI_LOG_DIR}/hermes-webui.pid}"
HERMES_WEBUI_LOCK_FILE="${HERMES_WEBUI_LOCK_FILE:-/tmp/hermes-webui-autostart.lock}"
AUTOSTART_LOG="${HERMES_WEBUI_LOG_DIR}/webui_autostart.log"
WEBUI_LOG="${HERMES_WEBUI_LOG_DIR}/hermes_webui.log"

# Make the WSL launcher knobs visible to start.sh/bootstrap.py.
export HERMES_WEBUI_HOST HERMES_WEBUI_PORT

mkdir -p "${HERMES_WEBUI_LOG_DIR}"
chmod 700 "${HERMES_WEBUI_LOG_DIR}" 2>/dev/null || true

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" | tee -a "${AUTOSTART_LOG}"
}

webui_healthy() {
  command -v curl >/dev/null 2>&1 \
    && curl -fsS --max-time 3 "${HERMES_WEBUI_HEALTH_URL}" >/dev/null 2>&1
}

pid_is_alive() {
  [[ -s "${HERMES_WEBUI_PID_FILE}" ]] || return 1
  local pid
  pid="$(cat "${HERMES_WEBUI_PID_FILE}" 2>/dev/null || true)"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" >/dev/null 2>&1
}

validate_repo() {
  if [[ ! -d "${HERMES_WEBUI_REPO}" ]]; then
    log "Hermes WebUI repo not found: ${HERMES_WEBUI_REPO}"
    exit 1
  fi
  if [[ ! -f "${HERMES_WEBUI_REPO}/start.sh" ]]; then
    log "start.sh not found under HERMES_WEBUI_REPO=${HERMES_WEBUI_REPO}"
    exit 1
  fi
}

maybe_require_agent_process() {
  # Hermes WebUI usually launches the agent in-process, so this check is opt-in.
  # Set HERMES_WEBUI_REQUIRE_AGENT_PROCESS=1 only if your setup depends on a
  # separately running Hermes gateway/agent before WebUI starts.
  if [[ "${HERMES_WEBUI_REQUIRE_AGENT_PROCESS:-0}" != "1" ]]; then
    return 0
  fi
  if ! pgrep -f "hermes" >/dev/null 2>&1; then
    log "HERMES_WEBUI_REQUIRE_AGENT_PROCESS=1 but no Hermes process is running; skipping start"
    exit 1
  fi
}

acquire_lock() {
  exec 9>"${HERMES_WEBUI_LOCK_FILE}"
  if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
      log "Autostart already running; lock held at ${HERMES_WEBUI_LOCK_FILE}"
      exit 0
    fi
  else
    log "flock not found; continuing without lock-based duplicate protection"
  fi
}

start_webui() {
  validate_repo
  maybe_require_agent_process

  if webui_healthy; then
    log "Hermes WebUI already running at ${HERMES_WEBUI_HEALTH_URL}"
    exit 0
  fi

  if pid_is_alive; then
    log "Hermes WebUI already running with pid $(cat "${HERMES_WEBUI_PID_FILE}")"
    exit 0
  fi

  rm -f "${HERMES_WEBUI_PID_FILE}"
  log "Starting Hermes WebUI from ${HERMES_WEBUI_REPO} on ${HERMES_WEBUI_HOST}:${HERMES_WEBUI_PORT}"

  (
    cd "${HERMES_WEBUI_REPO}"
    nohup bash "${HERMES_WEBUI_REPO}/start.sh" --foreground >>"${WEBUI_LOG}" 2>&1 &
    printf '%s\n' "$!" >"${HERMES_WEBUI_PID_FILE}"
  )

  sleep "${HERMES_WEBUI_STARTUP_GRACE_SECONDS:-2}"
  if webui_healthy; then
    log "Hermes WebUI started and passed health check"
    exit 0
  fi

  if pid_is_alive; then
    log "Hermes WebUI process started with pid $(cat "${HERMES_WEBUI_PID_FILE}"); health check not ready yet"
    exit 0
  fi

  log "Hermes WebUI failed to stay running; see ${WEBUI_LOG}"
  exit 1
}

acquire_lock
start_webui
