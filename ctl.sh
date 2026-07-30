#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/health_probe.sh
. "${REPO_ROOT}/scripts/lib/health_probe.sh"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
PID_FILE="${HERMES_WEBUI_PID_FILE:-${HERMES_HOME}/webui.pid}"
LOG_FILE="${HERMES_WEBUI_LOG_FILE:-${HERMES_HOME}/webui.log}"
STATE_FILE="${HERMES_WEBUI_CTL_STATE_FILE:-${HERMES_HOME}/webui.ctl.env}"
DEFAULT_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-${HERMES_HOME}/webui}"
DEFAULT_LAUNCHD_LABEL="${HERMES_WEBUI_LAUNCHD_LABEL:-com.parantoux.hermes-webui}"

usage() {
  cat <<'EOF'
Usage: ./ctl.sh <command> [args]

Commands:
  start [bootstrap args...]   Start Hermes WebUI as a background daemon
  stop                        Stop the daemon started by ctl.sh
  restart [bootstrap args...] Stop, then start again
  status                      Show daemon, host/port, log, and health status
  logs [--lines N] [--follow|--no-follow]
                              Show the daemon log (defaults to tail -n 100 -f)
EOF
}

ensure_home() {
  mkdir -p "${HERMES_HOME}" "${DEFAULT_STATE_DIR}"
}

_apply_env_file_safely() {
  local env_file="$1"
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#${line%%[![:space:]]*}}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    if [[ "${line}" =~ ^export[[:space:]]+(.+)$ ]]; then
      line="${BASH_REMATCH[1]}"
      line="${line#${line%%[![:space:]]*}}"
    fi
    [[ "${line}" == *=* ]] || continue

    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    case "${key}" in
      UID | GID | EUID | EGID | PPID) continue ;;
    esac

    value="${value#${value%%[![:space:]]*}}"
    if [[ "${value}" =~ ^\"(([^\"\\]|\\.)*)\"([[:space:]]*\#.*)?[[:space:]]*$ ]]; then
      value="${BASH_REMATCH[1]}"
      value="$(printf '%s' "$value" | awk '{
        i = 1
        len = length($0)
        while (i <= len) {
          c = substr($0, i, 1)
          if (c == "\\" && i < len) {
            nc = substr($0, i+1, 1)
            if (nc == "n") printf "\n"
            else if (nc == "r") printf "\r"
            else if (nc == "t") printf "\t"
            else if (nc == "\"") printf "\""
            else if (nc == "\\") printf "\\"
            else { printf "\\%s", nc }
            i += 2
          } else {
            printf "%s", c
            i++
          }
        }
      }')"
    elif [[ "${value}" =~ ^\'([^\']*)\'([[:space:]]*\#.*)?[[:space:]]*$ ]]; then
      value="${BASH_REMATCH[1]}"
    else
      value="${value%%[[:space:]]\#*}"
      value="${value%${value##*[![:space:]]}}"
    fi

    export "${key}=${value}"
  done < "${env_file}"
}

_load_repo_dotenv_preserving_env() {
  [[ "${HERMES_WEBUI_NO_DOTENV:-0}" == "1" ]] && return 0
  local env_file="${REPO_ROOT}/.env"
  [[ -f "${env_file}" ]] || return 0

  local -a preserved=()
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#${line%%[![:space:]]*}}"
    [[ -z "${line}" || "${line}" == \#* || "${line}" != *=* ]] && continue
    key="${line%%=*}"
    if [[ "${key}" =~ ^export[[:space:]]+(.+)$ ]]; then
      key="${BASH_REMATCH[1]}"
    fi
    key="${key//[[:space:]]/}"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    # Skip shell-readonly names (UID/GID/EUID/EGID/PPID); re-exporting them
    # below would abort under `set -euo pipefail` with "readonly variable".
    case "${key}" in
      UID | GID | EUID | EGID | PPID) continue ;;
    esac
    if [[ -n "${!key+x}" ]]; then
      value="${!key}"
      preserved+=("${key}=${value}")
    fi
  done < "${env_file}"

  _apply_env_file_safely "${env_file}"

  local assignment
  if [[ ${#preserved[@]} -gt 0 ]]; then
    for assignment in "${preserved[@]}"; do
      export "${assignment}"
    done
  fi
}

_load_hermes_dotenv() {
  # Also load ~/.hermes/.env so that ${VAR} references in config.yaml can
  # resolve against provider credentials defined in the Hermes env file.
  # Repo .env takes precedence (loaded above); variables already exported
  # into the shell environment (including those just set by repo .env) are
  # captured in preserved[] before _apply_env_file_safely runs and are
  # restored afterwards, so this acts as a fallback source for vars the
  # repo .env did not define.
  [[ "${HERMES_WEBUI_NO_DOTENV:-0}" == "1" ]] && return 0
  local hermes_home="${HERMES_HOME:-${HOME}/.hermes}"
  local hermes_env="${hermes_home}/.env"
  [[ -f "${hermes_env}" ]] || return 0

  local -a preserved=()
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#${line%%[![:space:]]*}}"
    [[ -z "${line}" || "${line}" == \#* || "${line}" != *=* ]] && continue
    key="${line%%=*}"
    if [[ "${key}" =~ ^export[[:space:]]+(.+)$ ]]; then
      key="${BASH_REMATCH[1]}"
    fi
    key="${key//[[:space:]]/}"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    case "${key}" in
      UID | GID | EUID | EGID | PPID) continue ;;
    esac
    if [[ -n "${!key+x}" ]]; then
      value="${!key}"
      preserved+=("${key}=${value}")
    fi
  done < "${hermes_env}"

  _apply_env_file_safely "${hermes_env}"

  local assignment
  if [[ ${#preserved[@]} -gt 0 ]]; then
    for assignment in "${preserved[@]}"; do
      export "${assignment}"
    done
  fi
}

_find_python() {
  if [[ -n "${HERMES_WEBUI_PYTHON:-}" ]]; then
    printf '%s\n' "${HERMES_WEBUI_PYTHON}"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    echo "[ctl] Python 3 is required to run bootstrap.py" >&2
    return 1
  fi
}

_parse_launch_binding() {
  CTL_HOST="${HERMES_WEBUI_HOST:-127.0.0.1}"
  CTL_PORT="${HERMES_WEBUI_PORT:-8787}"
  local arg next_is_host=0 saw_port=0
  for arg in "$@"; do
    if (( next_is_host )); then
      CTL_HOST="${arg}"
      next_is_host=0
      continue
    fi
    case "${arg}" in
      --host)
        next_is_host=1
        ;;
      --host=*)
        CTL_HOST="${arg#--host=}"
        ;;
      --*)
        ;;
      *)
        if (( ! saw_port )) && [[ "${arg}" =~ ^[0-9]+$ ]]; then
          CTL_PORT="${arg}"
          saw_port=1
        fi
        ;;
    esac
  done
}

_build_bootstrap_args() {
  CTL_BOOTSTRAP_ARGS=()
  local arg next_is_host=0 saw_port=0
  for arg in "$@"; do
    if (( next_is_host )); then
      next_is_host=0
      continue
    fi
    case "${arg}" in
      --host)
        next_is_host=1
        ;;
      --host=*)
        ;;
      --*)
        CTL_BOOTSTRAP_ARGS+=("${arg}")
        ;;
      *)
        if (( ! saw_port )) && [[ "${arg}" =~ ^[0-9]+$ ]]; then
          saw_port=1
        else
          CTL_BOOTSTRAP_ARGS+=("${arg}")
        fi
        ;;
    esac
  done
}

_write_state() {
  local pid="$1" host="$2" port="$3" python_exe="${4:-}"
  local state_dir="${HERMES_WEBUI_STATE_DIR:-${DEFAULT_STATE_DIR}}"
  {
    printf 'PID=%q\n' "${pid}"
    printf 'REPO_ROOT=%q\n' "${REPO_ROOT}"
    printf 'PYTHON_EXE=%q\n' "${python_exe}"
    printf 'HOST=%q\n' "${host}"
    printf 'PORT=%q\n' "${port}"
    printf 'LOG_FILE=%q\n' "${LOG_FILE}"
    printf 'STATE_DIR=%q\n' "${state_dir}"
    printf 'STARTED_AT=%q\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "${STATE_FILE}"
}

_load_state_if_present() {
  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${STATE_FILE}"
  fi
}

_pid_from_file() {
  [[ -f "${PID_FILE}" ]] || return 1
  local pid
  pid="$(tr -d '[:space:]' < "${PID_FILE}")"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${pid}"
}

_is_alive() {
  local pid="$1"
  kill -0 "${pid}" >/dev/null 2>&1
}

_is_windows_bash() {
  [[ "${OS:-}" == "Windows_NT" ]] && return 0
  case "$(uname -s 2>/dev/null || true)" in
    MINGW*|MSYS*|CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

_windows_bash_path() {
  local path="${1//\\//}" drive rest
  if [[ "${path}" =~ ^([A-Za-z]):(.*)$ ]]; then
    drive="${BASH_REMATCH[1],,}"
    rest="${BASH_REMATCH[2]}"
    printf '/%s%s\n' "${drive}" "${rest}"
    return
  fi
  printf '%s\n' "${path}"
}

_windows_pid_for_bash_pid() {
  local pid="$1"
  ps -p "${pid}" -l 2>/dev/null | awk 'NR == 2 { print $4 }'
}

_stop_webui_pid() {
  local pid="$1" signal="${2:-TERM}"
  if _is_windows_bash && command -v taskkill >/dev/null 2>&1; then
    local winpid
    winpid="$(_windows_pid_for_bash_pid "${pid}")"
    if [[ "${winpid}" =~ ^[0-9]+$ ]]; then
      taskkill //F //T //PID "${winpid}" >/dev/null 2>&1 || true
      return
    fi
  fi
  if [[ "${signal}" == "KILL" ]]; then
    kill -KILL "${pid}" >/dev/null 2>&1 || true
  else
    kill "${pid}" >/dev/null 2>&1 || true
  fi
}

_proc_args() {
  local pid="$1" args
  args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
  if [[ -n "${args}" ]]; then
    printf '%s\n' "${args}"
    return
  fi
  if _is_windows_bash; then
    local winpid
    winpid="$(_windows_pid_for_bash_pid "${pid}")"
    if [[ "${winpid}" =~ ^[0-9]+$ ]] && command -v wmic >/dev/null 2>&1; then
      args="$(wmic process where "ProcessId=${winpid}" get CommandLine //value 2>/dev/null | sed -n 's/^CommandLine=//p' | tr -d '\r')"
      if [[ -n "${args}" ]]; then
        printf '%s\n' "${args}"
        return
      fi
    fi
    ps -p "${pid}" -f 2>/dev/null | awk 'NR == 2 { for (i = 8; i <= NF; i++) printf "%s%s", (i == 8 ? "" : " "), $i; print "" }'
  fi
}

_is_owned_webui_pid() {
  local pid="$1" args args_slash state_repo="" state_repo_slash="" state_repo_win="" state_repo_win_slash="" state_python="" state_python_slash="" state_python_bash=""
  [[ -f "${STATE_FILE}" ]] || return 1
  _load_state_if_present
  state_repo="${REPO_ROOT:-}"
  state_python="${PYTHON_EXE:-}"
  state_repo_slash="${state_repo//\\//}"
  state_python_slash="${state_python//\\//}"
  if _is_windows_bash; then
    state_repo_win="$(cygpath -w "${state_repo}" 2>/dev/null || true)"
    state_repo_win_slash="${state_repo_win//\\//}"
  fi
  if [[ -n "${state_python}" ]] && _is_windows_bash; then
    state_python_bash="$(_windows_bash_path "${state_python}")"
  fi
  [[ "${state_repo}" == "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" ]] || return 1
  args="$(_proc_args "${pid}")"
  [[ -n "${args}" ]] || return 1
  args_slash="${args//\\//}"
  [[ "${args_slash}" == *"${state_repo_slash}/bootstrap.py"* ||
     "${args_slash}" == *"${state_repo_slash}/server.py"* ||
     "${args_slash}" == *"${state_repo_slash}/start.sh"* ||
     ( -n "${state_repo_win_slash}" && "${args_slash}" == *"${state_repo_win_slash}/bootstrap.py"* ) ||
     ( -n "${state_repo_win_slash}" && "${args_slash}" == *"${state_repo_win_slash}/server.py"* ) ||
     ( -n "${state_repo_win_slash}" && "${args_slash}" == *"${state_repo_win_slash}/start.sh"* ) ||
     ( -n "${state_python}" && "${args}" == *"${state_python}"* ) ||
     ( -n "${state_python_slash}" && "${args_slash}" == *"${state_python_slash}"* ) ||
     ( -n "${state_python_bash}" && "${args_slash}" == *"${state_python_bash}"* ) ]]
}

_current_pid() {
  local pid
  pid="$(_pid_from_file)" || return 1
  if _is_alive "${pid}" && _is_owned_webui_pid "${pid}"; then
    printf '%s\n' "${pid}"
    return 0
  fi
  return 1
}

_clear_stale_pid() {
  if [[ -f "${PID_FILE}" ]]; then
    rm -f "${PID_FILE}" "${STATE_FILE}"
    echo "[ctl] Removed stale PID file: ${PID_FILE}"
  fi
}

_pid_listens_on_port() {
  # Best-effort check that PID $1 has a listening socket on TCP port $2.
  # macOS (where launchd exists) ships lsof; if we can't determine ownership we
  # return 2 ("unknown") so the caller can fall back conservatively rather than
  # guess. Never blocks on a hard failure.
  local pid="$1" port="$2"
  [[ "${pid}" =~ ^[0-9]+$ && "${port}" =~ ^[0-9]+$ ]] || return 2
  if command -v lsof >/dev/null 2>&1; then
    if lsof -nP -a -p "${pid}" -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0   # PID is listening on that port → real conflict
    fi
    return 1     # PID is alive but NOT listening on that port → no conflict
  fi
  # Linux hosts (where the systemd guard runs) often lack lsof but ship ss.
  if command -v ss >/dev/null 2>&1; then
    local ss_out rows
    if ! ss_out="$(ss -tlnp 2>/dev/null)"; then
      return 2
    fi
    rows="$(printf '%s\n' "${ss_out}" | awk -v p="${port}" '$4 ~ (":" p "$")')"
    [[ -z "${rows}" ]] && return 1        # nothing listens on that port at all
    printf '%s\n' "${rows}" | grep -q "pid=${pid}," && return 0
    # Listener rows exist; when they carry pid= attribution and none is ours,
    # some OTHER process owns the port — not a conflict for this pid.
    printf '%s\n' "${rows}" | grep -q "pid=" && return 1
    return 2                              # listener present but unattributable
  fi
  return 2       # can't determine
}

_launchd_webui_pid() {
  [[ "${HERMES_WEBUI_CTL_ALLOW_LAUNCHD_CONFLICT:-0}" == "1" ]] && return 1
  command -v launchctl >/dev/null 2>&1 || return 1
  local label="${HERMES_WEBUI_LAUNCHD_LABEL:-${DEFAULT_LAUNCHD_LABEL}}"
  [[ -n "${label}" ]] || return 1
  local uid launchd_out pid
  uid="$(id -u)"
  launchd_out="$(launchctl print "gui/${uid}/${label}" 2>/dev/null)" || return 1
  pid="$(printf '%s\n' "${launchd_out}" | awk '/^[[:space:]]*pid = / {print $3; exit}')"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  (( pid > 0 )) || return 1
  _is_alive "${pid}" || return 1
  # Only treat the launchd job as a conflict for the port we are about to bind.
  # A second instance on a DIFFERENT port (e.g. HERMES_WEBUI_PORT=8788 for a
  # test build) does not collide with the launchd-managed default and must be
  # allowed to start (#3291 over-block fix). When port ownership can't be
  # determined (no lsof), fall back to the conservative previous behavior of
  # only guarding the default port so non-default ports are never wrongly blocked.
  local want_port="${CTL_PORT:-${HERMES_WEBUI_PORT:-8787}}"
  _pid_listens_on_port "${pid}" "${want_port}"
  case "$?" in
    0) printf '%s\n' "${pid}"; return 0 ;;   # launchd job listens on our port → block
    1) return 1 ;;                            # launchd job on a different port → allow
    *)                                        # unknown: only guard the default port
      if [[ "${want_port}" == "8787" ]]; then
        printf '%s\n' "${pid}"; return 0
      fi
      return 1 ;;
  esac
}

_probe_target_host() {
  # A bind-all host (0.0.0.0 / ::) is not a connectable probe target; probe
  # loopback instead (mirrors server.py _abort_if_already_serving). IPv6
  # literals must be bracketed for URL interpolation ("http://[::1]:8787"),
  # or curl/wget reject the URL and an existing instance is missed.
  local host="${1:-}"
  case "${host}" in
    0.0.0.0 | '' | ::) printf '127.0.0.1' ;;
    \[*\]) printf '%s' "${host}" ;;
    *:*) printf '[%s]' "${host}" ;;
    *) printf '%s' "${host}" ;;
  esac
}

_port_answers_http() {
  # True when ANYTHING answers an HTTP(S) request on host:port — mirrors
  # server.py's _abort_if_already_serving, which treats any response bytes
  # (including an error status from a foreign app squatting the port) as a
  # conflict. Deliberately broader than hermes_webui_probe_health, which
  # requires a 200 from /health.
  local host="$1" port="$2" url rc scheme
  for scheme in http https; do
    url="${scheme}://${host}:${port}/health"
    if command -v curl >/dev/null 2>&1; then
      # No -f: an HTTP error status is still a responder. -k: a self-signed
      # cert is still a responder. --noproxy: this is a LOCAL ownership
      # check — routed through an http(s)_proxy it would report the proxy,
      # not the port (false block or, with a dead proxy, a missed conflict).
      curl -sS -o /dev/null -k --noproxy '*' --max-time 2 "${url}" 2>/dev/null
      rc=$?
      [[ ${rc} -eq 0 ]] && return 0
    elif command -v wget >/dev/null 2>&1; then
      wget -qO /dev/null --no-check-certificate --no-proxy "--timeout=2" --tries=1 "${url}" 2>/dev/null
      rc=$?
      # 0 = OK; 8 = server answered with an error status — both are responders.
      [[ ${rc} -eq 0 || ${rc} -eq 8 ]] && return 0
    else
      return 1
    fi
  done
  return 1
}

_port_listener_diag() {
  # Best-effort one-liner describing what listens on TCP port $1. Used in
  # error/status messages only — never fails the caller.
  local port="$1" line=""
  # The || true keeps the assignments non-failing when errexit is inherited
  # into command substitutions (shopt inherit_errexit, or a BASHOPTS env from
  # the invoking shell) — without it a no-match awk/ss aborts status/stop.
  if command -v ss >/dev/null 2>&1; then
    line="$(ss -tlnp 2>/dev/null | awk -v p="${port}" '$4 ~ (":" p "$") {print; exit}')" || true
  fi
  if [[ -z "${line}" ]] && command -v lsof >/dev/null 2>&1; then
    line="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print; exit}')" || true
  fi
  if [[ -n "${line}" ]]; then
    printf '%s' "${line}"
  fi
  return 0
}

_systemd_unit_effective_port() {
  # Best-effort resolution of the port a systemd unit is configured to bind:
  # HERMES_WEBUI_PORT in its Environment=, then an explicit --port on its
  # ExecStart=. Prints nothing when the binding cannot be determined so the
  # caller can fall back to the conservative default-port guard.
  local scope="$1" unit="$2" env_block="" exec_block="" port=""
  env_block="$(systemctl "${scope}" show -p Environment --value "${unit}" 2>/dev/null)" || true
  if [[ "${env_block}" =~ HERMES_WEBUI_PORT=([0-9]+) ]]; then
    port="${BASH_REMATCH[1]}"
  fi
  if [[ -z "${port}" ]]; then
    exec_block="$(systemctl "${scope}" show -p ExecStart --value "${unit}" 2>/dev/null)" || true
    if [[ "${exec_block}" =~ --port[=[:space:]]([0-9]+) ]]; then
      port="${BASH_REMATCH[1]}"
    fi
  fi
  printf '%s' "${port}"
}

_systemd_webui_conflict() {
  # Linux analog of _launchd_webui_pid: echo a short conflict descriptor and
  # return 0 when a systemd unit (default hermes-webui.service, override via
  # HERMES_WEBUI_SYSTEMD_UNIT) effectively owns the instance we are about to
  # start. Two conflict shapes:
  #   - ActiveState=active and the unit's MainPID listens on our port.
  #   - ActiveState=activating/reloading (Restart= backoff between attempts):
  #     the port is briefly silent, but the unit WILL respawn and re-bind; a
  #     ctl.sh-started server would trap it in a permanent crash loop
  #     (each systemd attempt aborts on the "already responding" check, 5s
  #     later the next one dies the same way — observed in the field).
  # Port scoping mirrors the launchd #3291 fix: when the unit's port cannot
  # be determined, only the default port is guarded, so alternate-port test
  # instances are never wrongly blocked.
  [[ "${HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT:-0}" == "1" ]] && return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  local unit="${HERMES_WEBUI_SYSTEMD_UNIT:-hermes-webui.service}"
  [[ -n "${unit}" ]] || return 1
  local want_port="${CTL_PORT:-${HERMES_WEBUI_PORT:-8787}}"
  local scope state main_pid unit_port
  for scope in --system --user; do
    state="$(systemctl "${scope}" show -p ActiveState --value "${unit}" 2>/dev/null)" || continue
    case "${state}" in
      active)
        main_pid="$(systemctl "${scope}" show -p MainPID --value "${unit}" 2>/dev/null)"
        [[ "${main_pid}" =~ ^[0-9]+$ ]] || main_pid=0
        if (( main_pid > 0 )); then
          _pid_listens_on_port "${main_pid}" "${want_port}"
          case "$?" in
            0) printf 'unit %s is active (MainPID %s listens on port %s)' "${unit}" "${main_pid}" "${want_port}"; return 0 ;;
            1) continue ;;   # active on a different port → no conflict
          esac
        fi
        # Port ownership unknown via PID: resolve the unit's CONFIGURED
        # binding and refuse only on actual overlap. Only when the binding
        # cannot be determined fall back to guarding the default port
        # (see #3291) so alternate-port instances are never wrongly blocked.
        unit_port="$(_systemd_unit_effective_port "${scope}" "${unit}")" || true
        if [[ -n "${unit_port}" ]]; then
          if [[ "${unit_port}" == "${want_port}" ]]; then
            printf 'unit %s is active (configured for port %s)' "${unit}" "${unit_port}"
            return 0
          fi
        elif [[ "${want_port}" == "8787" ]]; then
          printf 'unit %s is active' "${unit}"
          return 0
        fi
        ;;
      activating | reloading)
        unit_port="$(_systemd_unit_effective_port "${scope}" "${unit}")" || true
        if [[ -n "${unit_port}" ]]; then
          if [[ "${unit_port}" == "${want_port}" ]]; then
            printf 'unit %s is %s (auto-restart pending on port %s)' "${unit}" "${state}" "${unit_port}"
            return 0
          fi
        elif [[ "${want_port}" == "8787" ]]; then
          printf 'unit %s is %s (auto-restart pending)' "${unit}" "${state}"
          return 0
        fi
        ;;
    esac
  done
  return 1
}

start_cmd() {
  ensure_home
  _load_repo_dotenv_preserving_env
  _load_hermes_dotenv
  export HERMES_WEBUI_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-${DEFAULT_STATE_DIR}}"
  mkdir -p "${HERMES_WEBUI_STATE_DIR}"
  _parse_launch_binding "$@"
  _build_bootstrap_args "$@"
  export HERMES_WEBUI_HOST="${CTL_HOST}"
  export HERMES_WEBUI_PORT="${CTL_PORT}"

  local existing_pid
  if existing_pid="$(_current_pid 2>/dev/null)"; then
    echo "[ctl] Hermes WebUI is already running (PID ${existing_pid})"
    return 0
  fi
  local launchd_pid
  if launchd_pid="$(_launchd_webui_pid 2>/dev/null)"; then
    echo "[ctl] Refusing to start a second Hermes WebUI while launchd job ${HERMES_WEBUI_LAUNCHD_LABEL:-${DEFAULT_LAUNCHD_LABEL}} is running (PID ${launchd_pid})." >&2
    echo "[ctl] Use launchctl kickstart -k gui/$(id -u)/${HERMES_WEBUI_LAUNCHD_LABEL:-${DEFAULT_LAUNCHD_LABEL}} or disable the launchd job before using ctl.sh start." >&2
    return 2
  fi
  local systemd_conflict
  if systemd_conflict="$(_systemd_webui_conflict 2>/dev/null)"; then
    echo "[ctl] Refusing to start a second Hermes WebUI: systemd ${systemd_conflict}." >&2
    echo "[ctl] Manage that instance with systemctl instead (e.g. sudo systemctl restart ${HERMES_WEBUI_SYSTEMD_UNIT:-hermes-webui.service}), or disable the unit before using ctl.sh start. Set HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT=1 to override." >&2
    return 2
  fi
  # Generic duplicate guard: whatever supervises it, if a live server already
  # answers on the target host:port, a second one is doomed — server.py aborts
  # on its "already responding" startup check a few seconds in, AFTER ctl.sh's
  # old 0.15s aliveness check had reported success and recorded a PID file
  # that goes stale on exit. Refuse up front instead.
  local probe_host
  probe_host="$(_probe_target_host "${CTL_HOST}")"
  if [[ "${HERMES_WEBUI_CTL_ALLOW_PORT_CONFLICT:-0}" != "1" ]] \
      && _port_answers_http "${probe_host}" "${CTL_PORT}"; then
    echo "[ctl] Refusing to start: a live server is already responding on ${probe_host}:${CTL_PORT}." >&2
    local listener_diag
    listener_diag="$(_port_listener_diag "${CTL_PORT}")"
    [[ -n "${listener_diag}" ]] && echo "[ctl]   listener: ${listener_diag}" >&2
    echo "[ctl] Stop that instance first (its own supervisor may restart it — check systemctl/launchctl)." >&2
    return 2
  fi
  _clear_stale_pid >/dev/null 2>&1 || true

  local python_exe pid
  python_exe="$(_find_python)"
  : >> "${LOG_FILE}"
  (
    cd "${REPO_ROOT}"
    trap '' HUP
    export HERMES_WEBUI_PRESERVE_ENV=1
    exec nohup "${python_exe}" "${REPO_ROOT}/bootstrap.py" --no-browser --foreground --host "${CTL_HOST}" "${CTL_PORT}" ${CTL_BOOTSTRAP_ARGS[@]+"${CTL_BOOTSTRAP_ARGS[@]}"}
  ) >> "${LOG_FILE}" 2>&1 &
  pid=$!

  printf '%s\n' "${pid}" > "${PID_FILE}"
  _write_state "${pid}" "${CTL_HOST}" "${CTL_PORT}" "${python_exe}"
  # Watch the child through its startup window instead of a single 0.15s
  # aliveness check. A server that dies during startup (bad venv, import
  # error, port stolen between guard and bind) exits after ~1-3s — after the
  # old check had already printed success and left a stale PID file behind.
  # Break early as soon as /health answers; report failure the moment the
  # process dies. HERMES_WEBUI_START_GRACE (integer seconds, default 3)
  # bounds the wait for servers that need longer before /health responds —
  # on timeout we keep the optimistic legacy behavior and say so.
  local grace="${HERMES_WEBUI_START_GRACE:-3}"
  [[ "${grace}" =~ ^[0-9]+$ ]] || grace=3
  # 0 would skip startup monitoring entirely and restore the stale-PID
  # behavior this window exists to prevent; treat it like any invalid value.
  (( grace > 0 )) || grace=3
  local grace_steps=$(( grace * 4 )) step=0 healthy=0
  while (( step < grace_steps )); do
    if ! _is_alive "${pid}"; then
      echo "[ctl] Hermes WebUI failed to stay running. Log: ${LOG_FILE}" >&2
      rm -f "${PID_FILE}" "${STATE_FILE}"
      return 1
    fi
    if hermes_webui_probe_health "${probe_host}" "${CTL_PORT}" "/health" 1 direct >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 0.25
    step=$(( step + 1 ))
  done
  if ! _is_alive "${pid}"; then
    echo "[ctl] Hermes WebUI failed to stay running. Log: ${LOG_FILE}" >&2
    rm -f "${PID_FILE}" "${STATE_FILE}"
    return 1
  fi
  echo "[ctl] Started Hermes WebUI (PID ${pid})"
  echo "[ctl] Bound: ${CTL_HOST}:${CTL_PORT}"
  echo "[ctl] Log: ${LOG_FILE}"
  if (( ! healthy )); then
    echo "[ctl] Note: /health did not respond within ${grace}s; check '$(basename "$0") status' shortly."
  fi
}

_warn_if_unmanaged_instance_serving() {
  # After stop concluded "nothing to do", check whether a server is STILL
  # answering on the configured port. Silently reporting "stopped" while a
  # foreign (e.g. systemd-supervised) instance keeps serving is how operators
  # end up starting doomed duplicates.
  _load_state_if_present
  local host="${HOST:-${HERMES_WEBUI_HOST:-127.0.0.1}}"
  local port="${PORT:-${HERMES_WEBUI_PORT:-8787}}"
  local probe_host
  probe_host="$(_probe_target_host "${host}")"
  if _port_answers_http "${probe_host}" "${port}"; then
    echo "[ctl] Warning: an instance NOT managed by ctl.sh is still serving ${probe_host}:${port} — not touching it." >&2
    local listener_diag
    listener_diag="$(_port_listener_diag "${port}")"
    [[ -n "${listener_diag}" ]] && echo "[ctl]   listener: ${listener_diag}" >&2
    echo "[ctl] If it is supervised (systemd/launchd), stop or restart it there instead." >&2
  fi
}

stop_cmd() {
  ensure_home
  local pid
  if ! pid="$(_pid_from_file 2>/dev/null)"; then
    echo "[ctl] Hermes WebUI is stopped"
    # Warn BEFORE deleting the state file: it carries the saved host/port
    # binding the probe needs when the instance was started off-default.
    _warn_if_unmanaged_instance_serving
    rm -f "${PID_FILE}" "${STATE_FILE}"
    return 0
  fi

  if ! _is_alive "${pid}" || ! _is_owned_webui_pid "${pid}"; then
    _warn_if_unmanaged_instance_serving
    _clear_stale_pid
    return 0
  fi

  echo "[ctl] Stopping Hermes WebUI (PID ${pid})"
  _stop_webui_pid "${pid}" TERM
  local i
  for i in {1..50}; do
    if ! _is_alive "${pid}"; then
      rm -f "${PID_FILE}" "${STATE_FILE}"
      echo "[ctl] Stopped"
      return 0
    fi
    sleep 0.1
  done

  echo "[ctl] Process did not exit after SIGTERM; sending SIGKILL" >&2
  _stop_webui_pid "${pid}" KILL
  rm -f "${PID_FILE}" "${STATE_FILE}"
}

_health_line() {
  local host="$1" port="$2" url scheme result
  scheme="$(hermes_webui_probe_scheme)"
  url="${scheme}://${host}:${port}/health"
  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    echo "unknown (curl/wget not found; ${url})"
    return 0
  fi
  if result="$(hermes_webui_probe_health "${host}" "${port}" "/health" 2)"; then
    if command -v python3 >/dev/null 2>&1; then
      printf '%s' "${result}" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
    sessions=data.get("sessions", data.get("session_count", "?"))
    active=data.get("active_streams", "?")
    status=data.get("status", "ok")
    print(f"ok ({sessions} sessions, {active} active streams)" if status == "ok" else status)
except Exception:
    print("ok")'
    else
      echo "ok"
    fi
  else
    echo "unreachable (${url})"
  fi
}

status_cmd() {
  ensure_home
  _load_repo_dotenv_preserving_env
  _load_hermes_dotenv
  _load_state_if_present
  local host="${HOST:-${HERMES_WEBUI_HOST:-127.0.0.1}}"
  local port="${PORT:-${HERMES_WEBUI_PORT:-8787}}"
  local log_path="${LOG_FILE}"
  local pid uptime health

  if pid="$(_current_pid 2>/dev/null)"; then
    uptime="$(ps -p "${pid}" -o etime= 2>/dev/null | sed 's/^ *//' || true)"
    health="$(_health_line "${host}" "${port}")"
    echo "● hermes-webui — running"
    echo "  PID:     ${pid}"
    echo "  Uptime:  ${uptime:-unknown}"
    echo "  Bound:   ${host}:${port}"
    echo "  Log:     ${log_path}"
    echo "  Health:  ${health}"
  else
    [[ -f "${PID_FILE}" ]] && _clear_stale_pid >/dev/null 2>&1 || true
    local probe_host
    probe_host="$(_probe_target_host "${host}")"
    if _port_answers_http "${probe_host}" "${port}"; then
      # Something serves the port but ctl.sh does not own it (systemd unit,
      # launchd job, manual run). Saying "stopped" here is what leads
      # operators to start a doomed duplicate.
      health="$(_health_line "${probe_host}" "${port}")"
      echo "● hermes-webui — running (not managed by ctl.sh)"
      local listener_diag
      listener_diag="$(_port_listener_diag "${port}")"
      echo "  PID:     -"
      [[ -n "${listener_diag}" ]] && echo "  Listener: ${listener_diag}"
      echo "  Bound:   ${host}:${port}"
      echo "  Log:     ${log_path}"
      echo "  Health:  ${health}"
      echo "  Note:    manage it via its own supervisor (systemctl/launchctl) or the process directly."
    else
      echo "● hermes-webui — stopped"
      echo "  PID:     -"
      echo "  Bound:   ${host}:${port}"
      echo "  Log:     ${log_path}"
      echo "  Health:  not checked"
    fi
  fi
}

logs_cmd() {
  ensure_home
  local lines=100 follow=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lines)
        shift
        lines="${1:-}"
        [[ "${lines}" =~ ^[0-9]+$ ]] || { echo "[ctl] --lines requires a number" >&2; return 2; }
        ;;
      --lines=*)
        lines="${1#--lines=}"
        [[ "${lines}" =~ ^[0-9]+$ ]] || { echo "[ctl] --lines requires a number" >&2; return 2; }
        ;;
      --follow|-f)
        follow=1
        ;;
      --no-follow)
        follow=0
        ;;
      *)
        echo "[ctl] Unknown logs option: $1" >&2
        return 2
        ;;
    esac
    shift
  done
  touch "${LOG_FILE}"
  if (( follow )); then
    tail -n "${lines}" -f "${LOG_FILE}"
  else
    tail -n "${lines}" "${LOG_FILE}"
  fi
}

cmd="${1:-}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "${cmd}" in
  start) start_cmd "$@" ;;
  stop) stop_cmd ;;
  restart) stop_cmd; start_cmd "$@" ;;
  status) status_cmd ;;
  logs) logs_cmd "$@" ;;
  -h|--help|help|"") usage ;;
  *) echo "[ctl] Unknown command: ${cmd}" >&2; usage >&2; exit 2 ;;
esac
