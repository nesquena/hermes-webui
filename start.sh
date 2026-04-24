#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${REPO_ROOT}/.env"
  set +a
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

exec "${PYTHON}" "${REPO_ROOT}/bootstrap.py" --no-browser "$@"
