#!/usr/bin/env bash
set -euo pipefail

PYTHON="${HERMES_WEBUI_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
  else
    echo "[ctl] Python 3 is required to run Hermes WebUI" >&2
    exit 1
  fi
fi

exec "${PYTHON}" -m hermes_webui.cli "$@"
