#!/usr/bin/env bash
# ============================================================
# Hermes Web UI -- portable bootstrap
# Usage: ./start.sh [port]
#
# One-command startup. Discovers your Hermes install, sets up
# a local virtualenv if needed, installs dependencies, then
# launches the server and prints everything you need to know.
#
# Override any step with environment variables:
#   HERMES_WEBUI_AGENT_DIR   path to hermes-agent checkout
#   HERMES_WEBUI_PYTHON      python executable to use
#   HERMES_WEBUI_PORT        port to listen on (default: 8787)
#   HERMES_WEBUI_HOST        bind address (default: 127.0.0.1)
#   HERMES_HOME              override ~/.hermes base
#   HERMES_WEBUI_STATE_DIR   override state directory
# ============================================================

set -euo pipefail

# ── Load .env if present (machine-local overrides, not committed) ─────────────
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${_SCRIPT_DIR}/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${_SCRIPT_DIR}/.env"
    set +a
fi

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}[ok]${RESET} $*"; }
warn() { echo -e "${YELLOW}[!!]${RESET} $*"; }
die()  { echo -e "${RED}[XX]${RESET} $*" >&2; exit 1; }
info() { echo -e "${CYAN}[--]${RESET} $*"; }
hdr()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── Resolve repo root (the directory this script lives in) ───────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Repo root: ${REPO_ROOT}"

# ── Port ─────────────────────────────────────────────────────────────────────
PORT="${1:-${HERMES_WEBUI_PORT:-8787}}"
export HERMES_WEBUI_PORT="${PORT}"

# ── Python discovery ─────────────────────────────────────────────────────────
hdr "Discovering Python..."

_find_python() {
    # 1. Explicit env var
    if [[ -n "${HERMES_WEBUI_PYTHON:-}" ]]; then
        echo "${HERMES_WEBUI_PYTHON}"; return
    fi

    # 2. Agent venv (discovered below -- call again after agent dir found)
    # (handled after agent dir discovery)

    # 3. Local .venv in repo
    if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        echo "${REPO_ROOT}/.venv/bin/python"; return
    fi

    # 4. System python3
    if command -v python3 &>/dev/null; then
        echo "$(command -v python3)"; return
    fi

    echo ""
}

PYTHON="$(_find_python)"

# ── Hermes agent discovery ────────────────────────────────────────────────────
hdr "Discovering Hermes agent..."

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
AGENT_DIR=""

_find_agent() {
    local candidates=(
        "${HERMES_WEBUI_AGENT_DIR:-}"
        "${HERMES_HOME}/hermes-agent"
        "${REPO_ROOT}/../hermes-agent"
        "${HOME}/.hermes/hermes-agent"
        "${HOME}/hermes-agent"
    )

    for d in "${candidates[@]}"; do
        [[ -z "$d" ]] && continue
        d="$(cd "${d}" 2>/dev/null && pwd || true)"
        if [[ -n "$d" && -f "${d}/run_agent.py" ]]; then
            echo "$d"; return
        fi
    done
    echo ""
}

AGENT_DIR="$(_find_agent)"

if [[ -n "${AGENT_DIR}" ]]; then
    ok "Hermes agent: ${AGENT_DIR}"
    export HERMES_WEBUI_AGENT_DIR="${AGENT_DIR}"

    # Now that we have agent dir, prefer its venv if we don't already have a python
    if [[ -z "${HERMES_WEBUI_PYTHON:-}" && -x "${AGENT_DIR}/venv/bin/python" ]]; then
        PYTHON="${AGENT_DIR}/venv/bin/python"
    fi
else
    warn "Hermes agent not found. Agent features will not work."
    warn "Fix with: export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent"
fi

if [[ -n "${PYTHON}" ]]; then
    ok "Python: ${PYTHON}  ($(${PYTHON} --version 2>&1))"
else
    warn "No Python found. Attempting to install..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3 python3-venv python3-pip
    elif command -v brew &>/dev/null; then
        brew install python3
    else
        die "Could not find or install Python. Please install Python 3.8+ and re-run."
    fi
    PYTHON="$(command -v python3)"
    ok "Python installed: ${PYTHON}"
fi

# ── Minimum Python version check ─────────────────────────────────────────────
PY_VER="$(${PYTHON} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(echo "${PY_VER}" | cut -d. -f1)"
PY_MINOR="$(echo "${PY_VER}" | cut -d. -f2)"
if [[ "${PY_MAJOR}" -lt 3 || ( "${PY_MAJOR}" -eq 3 && "${PY_MINOR}" -lt 8 ) ]]; then
    die "Python 3.8+ required. Found: ${PY_VER}"
fi

# ── Dependency check / local venv setup ──────────────────────────────────────
hdr "Checking dependencies..."

VENV_NEEDED=false
VENV_PATH="${REPO_ROOT}/.venv"

# If the chosen python is already the agent venv, its deps are already installed.
# If it is a system python, check if we can import the webui deps, create a local
# .venv if not.
_check_deps() {
    "${PYTHON}" -c "import yaml" 2>/dev/null
}

if ! _check_deps; then
    info "PyYAML not found in ${PYTHON}. Creating local .venv..."

    if [[ ! -d "${VENV_PATH}" ]]; then
        "${PYTHON}" -m venv "${VENV_PATH}" || die "Failed to create virtualenv at ${VENV_PATH}"
    fi

    VENV_PY="${VENV_PATH}/bin/python"
    "${VENV_PY}" -m pip install --quiet --upgrade pip

    if [[ -f "${REPO_ROOT}/requirements.txt" ]]; then
        info "Installing from requirements.txt..."
        "${VENV_PY}" -m pip install --quiet -r "${REPO_ROOT}/requirements.txt"
    else
        info "Installing minimal deps (pyyaml)..."
        "${VENV_PY}" -m pip install --quiet pyyaml
    fi

    PYTHON="${VENV_PY}"
    ok "Local venv ready: ${VENV_PATH}"
else
    ok "Dependencies satisfied."
fi

# ── Kill any stale instance on the same port ─────────────────────────────────
hdr "Checking for existing instances..."

EXISTING=$(lsof -ti tcp:"${PORT}" 2>/dev/null || true)
if [[ -n "${EXISTING}" ]]; then
    warn "Killing existing process on port ${PORT} (PID ${EXISTING})"
    kill "${EXISTING}" 2>/dev/null || true
    sleep 0.5
fi

# Also kill any server.py process from this repo
pkill -f "${REPO_ROOT}/server.py" 2>/dev/null || true

# ── Set up working directory for Hermes imports ───────────────────────────────
# server.py / api/config.py inject agent dir into sys.path at import time,
# but we also cd into the agent dir so relative imports in run_agent work.
if [[ -n "${AGENT_DIR}" ]]; then
    WORKDIR="${AGENT_DIR}"
else
    WORKDIR="${REPO_ROOT}"
fi

# ── Launch ───────────────────────────────────────────────────────────────────
hdr "Starting Hermes Web UI..."

LOG="/tmp/hermes-webui-${PORT}.log"
export HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-127.0.0.1}"
export HERMES_WEBUI_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-${HERMES_HOME}/webui}"

nohup "${PYTHON}" "${REPO_ROOT}/server.py" \
    > "${LOG}" 2>&1 &
PID=$!

echo -e "\n${CYAN}  PID ${PID} starting...${RESET}"
sleep 1.5

# ── Health check ─────────────────────────────────────────────────────────────
HEALTH_URL="http://${HERMES_WEBUI_HOST:-127.0.0.1}:${PORT}/health"
MAX_WAIT=15
ELAPSED=0
while [[ $ELAPSED -lt $MAX_WAIT ]]; do
    if curl -sf "${HEALTH_URL}" | grep -q '"status"' 2>/dev/null; then
        break
    fi
    sleep 0.5
    ELAPSED=$((ELAPSED + 1))
done

if ! curl -sf "${HEALTH_URL}" | grep -q '"status"' 2>/dev/null; then
    warn "Health check did not pass within ${MAX_WAIT}s. Check log:"
    tail -20 "${LOG}"
    echo ""
    warn "Server may still be starting. Try: curl ${HEALTH_URL}"
else
    ok "Server is healthy."
fi

# ── Print access instructions ─────────────────────────────────────────────────
BIND_HOST="${HERMES_WEBUI_HOST:-127.0.0.1}"

echo ""
echo -e "${BOLD}========================================${RESET}"
echo -e "${GREEN}  Hermes Web UI is running${RESET}"
echo -e "${BOLD}========================================${RESET}"
echo ""

if [[ "${BIND_HOST}" == "127.0.0.1" || "${BIND_HOST}" == "localhost" ]]; then
    # Server is bound to loopback -- detect if we are on a remote machine
    # by checking if $SSH_CLIENT or $SSH_TTY is set
    if [[ -n "${SSH_CLIENT:-}" || -n "${SSH_TTY:-}" ]]; then
        SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<your-server-ip>")"
        echo -e "  You are on a remote machine. To access from your local browser:"
        echo ""
        echo -e "  ${CYAN}ssh -N -L ${PORT}:127.0.0.1:${PORT} \$(whoami)@${SERVER_IP}${RESET}"
        echo ""
        echo -e "  Then open: ${BOLD}http://localhost:${PORT}${RESET}"
    else
        echo -e "  Open: ${BOLD}http://localhost:${PORT}${RESET}"
    fi
else
    echo -e "  Open: ${BOLD}http://${BIND_HOST}:${PORT}${RESET}"
fi

echo ""
echo -e "  Log:  ${LOG}"
echo -e "  PID:  ${PID}"
echo ""
