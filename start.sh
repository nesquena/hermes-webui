#!/usr/bin/env bash
set -euo pipefail

# If invoked as root (e.g., via sudo), re-exec as hermeswebui to avoid
# permission issues with bind-mounted .hermes directory.
if [[ $EUID -eq 0 ]]; then
  exec sudo -n -u hermeswebui "$0" "$@"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# When running inside the pre-built Docker container, use the pre-created venv
if [[ -f "/.within_container" && -x "/app/venv/bin/python" ]]; then
  export HERMES_WEBUI_PYTHON="/app/venv/bin/python"
  exec "/app/venv/bin/python" "${REPO_ROOT}/bootstrap.py" --no-browser "$@"
fi

if [[ -f "${REPO_ROOT}/.env" ]]; then
  # Parse .env manually to avoid assigning to readonly vars (UID, GID, etc.)
  # This mirrors bootstrap.py's _load_repo_dotenv() logic
  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    # Strip leading/trailing whitespace
    line="${raw_line#"${raw_line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    # Skip empty lines and comments
    [[ -z "$line" || "$line" == \#* ]] && continue
    # Split on first '='
    [[ "$line" == *"="* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    # Skip readonly shell variables
    case "$key" in
      UID|GID|EUID|EGID|PPID|PID|_) continue ;;
    esac
    # Strip optional 'export' prefix and surrounding quotes
    if [[ "$key" == export* ]]; then
      key="${key#export}"
      key="${key#"${key%%[![:space:]]*}"}"
    fi
    value="${value%"${value##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    export "$key=$value"
  done < "${REPO_ROOT}/.env"
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
