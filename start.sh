#!/usr/bin/env bash
set -euo pipefail

# If invoked as root (e.g. via `sudo ./start.sh` or accidental root shell
# inside the container), re-exec as the unprivileged hermeswebui user so the
# WebUI process never owns root-only file modes on bind-mounted state.
# Outside containers the EUID==0 case is rare; inside the production image
# the entrypoint drops to hermeswebui itself, so this is a defensive guard.
# Sourced from PR #1686 (@binhpt310) — Cluster 1 (operational hardening),
# extracted to a focused follow-up after the parent PR was deferred over a
# separate sibling-repo build-context concern unrelated to this fix.
#
# Four preconditions to fire (all must hold):
#   - EUID == 0
#   - hermeswebui user actually exists (id lookup)
#   - sudo is on PATH (production image does not ship sudo, so this is the
#     load-bearing no-op guard for the canonical container path)
#   - sudo -u hermeswebui passes without prompting (NOPASSWD precheck)
# The NOPASSWD precheck via `sudo -n -u hermeswebui true` makes this a silent
# fall-through on host machines where the developer's hermeswebui user
# requires a password — better than exiting non-zero with `sudo: a password
# is required` and surprising the user who didn't ask for sudo behavior.
if [[ ${EUID:-$(id -u)} -eq 0 ]] && id hermeswebui >/dev/null 2>&1 \
        && command -v sudo >/dev/null 2>&1 \
        && sudo -n -u hermeswebui true 2>/dev/null; then
  exec sudo -n -u hermeswebui "$0" "$@"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  # Filter out shell-readonly vars (UID, GID, EUID, EGID, PPID) before
  # `source`ing.  docker-compose.yml's macOS instructions document
  # `echo "UID=$(id -u)" >> .env` to set host UID/GID, which then crashes
  # `start.sh` with "UID: readonly variable" when bash tries to assign to
  # those names.  Filtering them out lets the .env file carry those entries
  # for docker-compose's variable substitution while keeping local invocation
  # of start.sh working.  The regression guard at
  # tests/test_bootstrap_dotenv.py:181 still passes — the line below contains
  # both `source` and `.env`.
  # Sourced from PR #1686 (@binhpt310) — Cluster 1 (operational hardening),
  # extracted to a focused follow-up after the parent PR was deferred.
  _hermes_env_filtered="$(mktemp "${TMPDIR:-/tmp}/hermes-webui-env.XXXXXX")"
  grep -vE '^[[:space:]]*(export[[:space:]]+)?(UID|GID|EUID|EGID|PPID)=' "${REPO_ROOT}/.env" > "${_hermes_env_filtered}" || true
  set -a
  # shellcheck source=/dev/null
  source "${_hermes_env_filtered}"
  set +a
  rm -f "${_hermes_env_filtered}"
  unset _hermes_env_filtered
fi

PYTHON="${HERMES_WEBUI_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
  else
    echo "[XX] Python 3 is required to run bootstrap.py" >&2
    exit 1
  fi
fi

# Pre-flight: detect an already-running server before launching bootstrap.py.
#
# bootstrap.py's detached (non-foreground) path spawns server.py, then probes
# /health and reports success once *anything* answers. If a server is already
# bound to this host:port, the freshly spawned child fails to bind and dies,
# but the EXISTING (often orphaned) server answers the /health probe — so
# bootstrap.py prints "ready" and exits 0 without having started anything. The
# user just keeps re-confirming the old instance on every run.
#
# To avoid that, probe /health here first. If a server is already up, tell the
# user plainly that nothing was (re)started and how to restart, then exit 0.
# ctl.sh already refuses to double-start via PID/state tracking; this brings
# the detached start.sh path to parity using a health probe (start.sh keeps no
# PID file of its own).
#
# Resolve host/port the same way bootstrap.py does: HERMES_WEBUI_HOST /
# HERMES_WEBUI_PORT (possibly just sourced from .env above), else the
# bootstrap.py defaults of 127.0.0.1 / 8787. A 0.0.0.0 / :: bind is probed via
# loopback, matching server.py's _abort_if_already_serving.
_hermes_host="${HERMES_WEBUI_HOST:-127.0.0.1}"
_hermes_port="${HERMES_WEBUI_PORT:-8787}"

# CLI args override the env/defaults exactly as bootstrap.py's argparse does
# (`port` is the first bare numeric positional; `--host VALUE` / `--host=VALUE`).
# Without this, `./start.sh <port>` or `--host X` would probe the wrong endpoint
# and could falsely report "already running" against a different instance.
_hermes_args=("$@")
_hermes_i=0
while [[ ${_hermes_i} -lt ${#_hermes_args[@]} ]]; do
  _hermes_arg="${_hermes_args[${_hermes_i}]}"
  case "${_hermes_arg}" in
    --host)
      _hermes_next=$(( _hermes_i + 1 ))
      if [[ ${_hermes_next} -lt ${#_hermes_args[@]} ]]; then
        _hermes_host="${_hermes_args[${_hermes_next}]}"
        _hermes_i=${_hermes_next}
      fi
      ;;
    --host=*)
      _hermes_host="${_hermes_arg#--host=}"
      ;;
    --*)
      : # other flags (e.g. --no-browser) carry no positional value here
      ;;
    *)
      # First bare numeric positional is the port (bootstrap.py: nargs="?").
      if [[ "${_hermes_arg}" =~ ^[0-9]+$ ]]; then
        _hermes_port="${_hermes_arg}"
      fi
      ;;
  esac
  _hermes_i=$(( _hermes_i + 1 ))
done

case "${_hermes_host}" in
  0.0.0.0|""|::|"[::]") _hermes_probe_host="127.0.0.1" ;;
  *) _hermes_probe_host="${_hermes_host}" ;;
esac
_hermes_health_url="http://${_hermes_probe_host}:${_hermes_port}/health"

# Best-effort probe. If neither curl nor wget is present we skip the check and
# fall through to the normal launch (unchanged behavior). Short 2s timeout so a
# normal cold start is not delayed.
_hermes_already_up=""
if command -v curl >/dev/null 2>&1; then
  _hermes_already_up="$(curl -fsS --max-time 2 "${_hermes_health_url}" 2>/dev/null || true)"
elif command -v wget >/dev/null 2>&1; then
  _hermes_already_up="$(wget -qO- --timeout=2 --tries=1 "${_hermes_health_url}" 2>/dev/null || true)"
fi

if [[ -n "${_hermes_already_up}" ]]; then
  cat >&2 <<EOF
[==] Hermes WebUI is already running at http://${_hermes_probe_host}:${_hermes_port}
     The server was NOT started again (start.sh does not double-start).

     If you need to restart the server, do the following:

     Preferred — use the daemon controller:
       ./ctl.sh restart

     Otherwise, stop the running server and start it again manually:
       1. Find the process listening on port ${_hermes_port}:
            lsof -iTCP:${_hermes_port} -sTCP:LISTEN      # macOS / Linux
       2. Stop it (use -9 only if it ignores a normal stop):
            kill <PID>
       3. Start it again:
            ./start.sh
EOF
  exit 0
fi

exec "${PYTHON}" "${REPO_ROOT}/bootstrap.py" --no-browser "$@"
