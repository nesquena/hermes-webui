#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -eq 0 ]] && id hermeswebui >/dev/null 2>&1 \
        && command -v sudo >/dev/null 2>&1 \
        && sudo -n -u hermeswebui true 2>/dev/null; then
  exec sudo -n -u hermeswebui "$0" "$@"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON="${HERMES_WEBUI_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
  else
    echo "[XX] Python 3 is required to run Hermes WebUI" >&2
    exit 1
  fi
fi

exec "${PYTHON}" -m hermes_webui.cli web --no-browser "$@"
