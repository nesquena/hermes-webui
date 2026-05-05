#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Neo WebUI launcher

Usage:
  ./neo.sh [options]

Options:
  --isolated        Use an isolated /tmp state dir for UI validation.
  --foreground      Run server.py in the foreground instead of bootstrapping in background.
  --no-browser      Do not open a browser tab when using bootstrap mode.
  --host HOST       Bind host. Default: 127.0.0.1
  --port PORT       Bind port. Default: 8787
  -h, --help        Show this help.

Examples:
  ./neo.sh
  ./neo.sh --port 8788
  ./neo.sh --isolated
  ./neo.sh --foreground
EOF
}

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${REPO_ROOT}/.env"
  set +a
fi

HOST="${HERMES_WEBUI_HOST:-127.0.0.1}"
PORT="${HERMES_WEBUI_PORT:-8787}"
ISOLATED=0
FOREGROUND=0
NO_BROWSER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --isolated)
      ISOLATED=1
      shift
      ;;
    --foreground)
      FOREGROUND=1
      shift
      ;;
    --no-browser)
      NO_BROWSER=1
      shift
      ;;
    --host)
      HOST="${2:-}"
      if [[ -z "${HOST}" ]]; then
        echo "[neo] --host requires a value" >&2
        exit 2
      fi
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      if [[ -z "${PORT}" ]]; then
        echo "[neo] --port requires a value" >&2
        exit 2
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[neo] Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PYTHON="${HERMES_WEBUI_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
  else
    echo "[neo] Python 3 is required." >&2
    exit 1
  fi
fi

export HERMES_WEBUI_BOT_NAME="${HERMES_WEBUI_BOT_NAME:-Neo}"
export HERMES_WEBUI_DEFAULT_SKIN="${HERMES_WEBUI_DEFAULT_SKIN:-neo}"
export HERMES_WEBUI_LOCALE="${HERMES_WEBUI_LOCALE:-pt-BR}"
export HERMES_WEBUI_HOST="${HOST}"
export HERMES_WEBUI_PORT="${PORT}"

if [[ "${ISOLATED}" -eq 1 ]]; then
  ISO_DIR="${NEO_WEBUI_ISOLATED_DIR:-/tmp/neo-webui-ui}"
  mkdir -p "${ISO_DIR}"
  export HERMES_WEBUI_STATE_DIR="${ISO_DIR}"
  export HERMES_HOME="${ISO_DIR}"
  export HERMES_BASE_HOME="${ISO_DIR}"
fi

echo "[neo] Bot      : ${HERMES_WEBUI_BOT_NAME}"
echo "[neo] Skin     : ${HERMES_WEBUI_DEFAULT_SKIN}"
echo "[neo] Locale   : ${HERMES_WEBUI_LOCALE}"
echo "[neo] URL      : http://${HOST}:${PORT}"
if [[ "${ISOLATED}" -eq 1 ]]; then
  echo "[neo] State    : ${HERMES_WEBUI_STATE_DIR} (isolated)"
else
  echo "[neo] State    : ${HERMES_WEBUI_STATE_DIR:-~/.hermes/webui}"
fi

if [[ "${FOREGROUND}" -eq 1 ]]; then
  echo "[neo] Mode     : foreground"
  exec "${PYTHON}" "${REPO_ROOT}/server.py"
fi

BOOTSTRAP_ARGS=("--host" "${HOST}")
if [[ "${NO_BROWSER}" -eq 1 ]]; then
  BOOTSTRAP_ARGS+=("--no-browser")
fi
BOOTSTRAP_ARGS+=("${PORT}")

echo "[neo] Mode     : bootstrap"
exec "${PYTHON}" "${REPO_ROOT}/bootstrap.py" "${BOOTSTRAP_ARGS[@]}"
