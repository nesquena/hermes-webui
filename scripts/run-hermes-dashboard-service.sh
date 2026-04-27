#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${REPO_ROOT}/.env"
  set +a
fi

if [[ -n "${HERMES_DASHBOARD_NODE_BIN:-}" ]]; then
  export PATH="${HERMES_DASHBOARD_NODE_BIN}:${PATH}"
elif [[ -x "${HOME}/.local/share/fnm/node-versions/v24.14.1/installation/bin/npm" ]]; then
  export PATH="${HOME}/.local/share/fnm/node-versions/v24.14.1/installation/bin:${PATH}"
fi

resolve_hermes() {
  local candidate

  if [[ -n "${HERMES_DASHBOARD_BIN:-}" ]]; then
    printf '%s\n' "${HERMES_DASHBOARD_BIN}"
    return 0
  fi

  if command -v hermes >/dev/null 2>&1; then
    command -v hermes
    return 0
  fi

  for candidate in \
    "${HOME}/.local/bin/hermes" \
    "${HOME}/.hermes/hermes-agent/venv/bin/hermes"
  do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  echo "[XX] Could not find 'hermes' on PATH. Set HERMES_DASHBOARD_BIN." >&2
  return 1
}

HERMES_BIN="$(resolve_hermes)"
HOST="${HERMES_DASHBOARD_HOST:-0.0.0.0}"
PORT="${HERMES_DASHBOARD_PORT:-9119}"
ALLOW_INSECURE="${HERMES_DASHBOARD_ALLOW_INSECURE:-1}"

args=(
  dashboard
  --no-open
  --host "${HOST}"
  --port "${PORT}"
)

case "${HOST}" in
  127.0.0.1|localhost|::1)
    ;;
  *)
    if [[ "${ALLOW_INSECURE}" != "1" ]]; then
      echo "[XX] Refusing non-localhost bind without HERMES_DASHBOARD_ALLOW_INSECURE=1." >&2
      exit 1
    fi
    args+=(--insecure)
    ;;
esac

exec "${HERMES_BIN}" "${args[@]}"
