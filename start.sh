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
  set -a
  # shellcheck source=/dev/null
  source <(grep -vE '^[[:space:]]*(export[[:space:]]+)?(UID|GID|EUID|EGID|PPID)=' "${REPO_ROOT}/.env")
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
