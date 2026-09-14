#!/usr/bin/env bash
# Marker-file + respawn-loop supervision for the WebUI server process,
# mirroring the gateway-supervisor pattern (marker files record intent, a
# waited-on loop respawns the service): the server runs as a direct child of
# the supervising shell — never a nohup/setsid daemonized orphan — so a crash,
# an OOM kill, or the in-process overflow watchdog's exit(1) respawns it
# within seconds instead of silently leaving a dead container/sandbox.
#
# Marker files (all inside the caller-provided marker dir):
#   webui.stop    intent marker — while the loop runs, its presence means
#                 "do not respawn". Written by the TERM/INT trap; an operator
#                 can also `touch` it before stopping the server for
#                 maintenance. Cleared on supervisor startup so a marker left
#                 over from a previous shutdown never blocks a fresh start.
#   webui.pid     PID of the currently running server child (removed on exit).
#   webui.status  one line of best-effort telemetry:
#                 "<state> respawns=<n> ts=<epoch>" where state is one of
#                 running | respawning | stopped | gave_up.
#
# Environment knobs:
#   HERMES_WEBUI_SUPERVISOR=0                 bypass the loop entirely (run the
#                                             server once, legacy behavior)
#   HERMES_WEBUI_SUPERVISOR_RESPAWN_DELAY_S   delay before each respawn (default 1)
#   HERMES_WEBUI_SUPERVISOR_MIN_UPTIME_S      child uptime below this counts as
#                                             a fast failure (default 5)
#   HERMES_WEBUI_SUPERVISOR_MAX_FAST_FAILS    consecutive fast failures before
#                                             the loop gives up and exits
#                                             non-zero, letting the outer
#                                             restart policy / operator see a
#                                             genuinely broken install instead
#                                             of an infinite hot crash loop
#                                             (default 5)
#
# This file is safe to `source` (defines functions only) and is also runnable
# directly as a standalone supervisor:
#   bash scripts/lib/webui_supervisor.sh <marker_dir> <command> [args...]
#
# Kept bash 3.2 compatible and safe under callers running `set -e` / `set -u`
# (docker_init.bash sources this under `set -e`).

_HERMES_WEBUI_SUPERVISOR_CHILD_PID=""
_HERMES_WEBUI_SUPERVISOR_MARKER_DIR=""
_HERMES_WEBUI_SUPERVISOR_STOPPING=0

hermes_webui_supervisor_enabled() {
  case "${HERMES_WEBUI_SUPERVISOR:-1}" in
    0 | false | FALSE | False | no | NO | off | OFF) return 1 ;;
    *) return 0 ;;
  esac
}

_hermes_webui_supervisor_log() {
  echo "[webui-supervisor] $*"
}

_hermes_webui_supervisor_status() {
  # $1 = state, $2 = respawn count. Telemetry only — never fails the caller.
  [ -n "${_HERMES_WEBUI_SUPERVISOR_MARKER_DIR}" ] || return 0
  printf '%s respawns=%s ts=%s\n' "$1" "${2:-0}" "$(date +%s)" \
    > "${_HERMES_WEBUI_SUPERVISOR_MARKER_DIR}/webui.status" 2>/dev/null || true
}

_hermes_webui_supervisor_request_stop() {
  # TERM/INT trap: record intent via the stop marker, forward TERM to the
  # server child (which runs its own graceful-shutdown path), and let the
  # main loop observe the marker and exit instead of respawning.
  _HERMES_WEBUI_SUPERVISOR_STOPPING=1
  if [ -n "${_HERMES_WEBUI_SUPERVISOR_MARKER_DIR}" ]; then
    : > "${_HERMES_WEBUI_SUPERVISOR_MARKER_DIR}/webui.stop" 2>/dev/null || true
  fi
  if [ -n "${_HERMES_WEBUI_SUPERVISOR_CHILD_PID}" ]; then
    kill -TERM "${_HERMES_WEBUI_SUPERVISOR_CHILD_PID}" 2>/dev/null || true
  fi
}

# hermes_webui_supervise <marker_dir> <command> [args...]
# Runs <command> as a waited-on child, respawning it on unexpected exit.
# Returns the child's final exit code once a stop was requested (TERM/INT or
# stop marker), or non-zero after giving up on a fast crash loop.
hermes_webui_supervise() {
  if [ $# -lt 2 ]; then
    echo "usage: hermes_webui_supervise <marker_dir> <command> [args...]" >&2
    return 2
  fi
  local marker_dir="$1"
  shift

  if ! hermes_webui_supervisor_enabled; then
    _hermes_webui_supervisor_log "HERMES_WEBUI_SUPERVISOR=0 — running unsupervised (single shot)"
    "$@"
    return $?
  fi

  mkdir -p "${marker_dir}" 2>/dev/null || true
  _HERMES_WEBUI_SUPERVISOR_MARKER_DIR="${marker_dir}"
  local stop_marker="${marker_dir}/webui.stop"
  local pid_marker="${marker_dir}/webui.pid"

  # Stale intent from a previous shutdown must not block a fresh start.
  rm -f "${stop_marker}" 2>/dev/null || true

  local respawn_delay="${HERMES_WEBUI_SUPERVISOR_RESPAWN_DELAY_S:-1}"
  local min_uptime="${HERMES_WEBUI_SUPERVISOR_MIN_UPTIME_S:-5}"
  local max_fast_fails="${HERMES_WEBUI_SUPERVISOR_MAX_FAST_FAILS:-5}"
  local fast_fails=0 respawns=0 started_at=0 uptime=0 rc=0 sleep_pid=""

  _HERMES_WEBUI_SUPERVISOR_STOPPING=0
  trap '_hermes_webui_supervisor_request_stop' TERM INT

  while :; do
    if [ -f "${stop_marker}" ] || [ "${_HERMES_WEBUI_SUPERVISOR_STOPPING}" = "1" ]; then
      break
    fi

    started_at=$(date +%s)
    "$@" &
    _HERMES_WEBUI_SUPERVISOR_CHILD_PID=$!
    printf '%s\n' "${_HERMES_WEBUI_SUPERVISOR_CHILD_PID}" > "${pid_marker}" 2>/dev/null || true
    _hermes_webui_supervisor_status running "${respawns}"
    _hermes_webui_supervisor_log "server started: pid=${_HERMES_WEBUI_SUPERVISOR_CHILD_PID} respawns=${respawns}"

    rc=0
    wait "${_HERMES_WEBUI_SUPERVISOR_CHILD_PID}" && rc=0 || rc=$?
    # A trap interrupting `wait` makes it return 128+signum while the child is
    # still alive — re-wait until the child has actually exited.
    while kill -0 "${_HERMES_WEBUI_SUPERVISOR_CHILD_PID}" 2>/dev/null; do
      wait "${_HERMES_WEBUI_SUPERVISOR_CHILD_PID}" && rc=0 || rc=$?
    done
    _HERMES_WEBUI_SUPERVISOR_CHILD_PID=""
    rm -f "${pid_marker}" 2>/dev/null || true
    uptime=$(( $(date +%s) - started_at ))

    if [ -f "${stop_marker}" ] || [ "${_HERMES_WEBUI_SUPERVISOR_STOPPING}" = "1" ]; then
      break
    fi

    if [ "${uptime}" -lt "${min_uptime}" ]; then
      fast_fails=$(( fast_fails + 1 ))
    else
      fast_fails=0
    fi
    if [ "${fast_fails}" -ge "${max_fast_fails}" ]; then
      _hermes_webui_supervisor_log "giving up: ${fast_fails} consecutive exits within ${min_uptime}s (last exit code ${rc})" >&2
      _hermes_webui_supervisor_status gave_up "${respawns}"
      trap - TERM INT
      [ "${rc}" -ne 0 ] || rc=1
      return "${rc}"
    fi

    respawns=$(( respawns + 1 ))
    _hermes_webui_supervisor_log "server exited unexpectedly (code ${rc}, uptime ${uptime}s) — respawning in ${respawn_delay}s (respawn #${respawns})"
    _hermes_webui_supervisor_status respawning "${respawns}"
    # Sleep as a waited-on background child: `wait` is interruptible by the
    # TERM/INT trap, so a stop request during the backoff is honored
    # immediately instead of after the delay.
    sleep "${respawn_delay}" &
    sleep_pid=$!
    wait "${sleep_pid}" 2>/dev/null || true
  done

  _hermes_webui_supervisor_log "stop requested — not respawning (last exit code ${rc})"
  _hermes_webui_supervisor_status stopped "${respawns}"
  trap - TERM INT
  return "${rc}"
}

# When executed directly (not sourced), run the supervisor.
if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
  if [[ $# -lt 2 ]]; then
    echo "usage: webui_supervisor.sh <marker_dir> <command> [args...]" >&2
    exit 2
  fi
  hermes_webui_supervise "$@"
  exit $?
fi
