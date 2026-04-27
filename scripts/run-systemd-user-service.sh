#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${REPO_ROOT}/.env"
  set +a
fi

find_agent_dir() {
  local home_dir hermes_home candidate
  home_dir="${HOME}"
  hermes_home="${HERMES_HOME:-${home_dir}/.hermes}"

  for candidate in \
    "${HERMES_WEBUI_AGENT_DIR:-}" \
    "${hermes_home}/hermes-agent" \
    "${REPO_ROOT}/../hermes-agent" \
    "${home_dir}/.hermes/hermes-agent" \
    "${home_dir}/hermes-agent"
  do
    [[ -n "${candidate}" ]] || continue
    if [[ -f "${candidate}/run_agent.py" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  return 1
}

resolve_python() {
  local candidate agent_dir

  if [[ -n "${HERMES_WEBUI_PYTHON:-}" ]]; then
    printf '%s\n' "${HERMES_WEBUI_PYTHON}"
    return 0
  fi

  candidate="${REPO_ROOT}/.venv/bin/python"
  if [[ -x "${candidate}" ]]; then
    printf '%s\n' "${candidate}"
    return 0
  fi

  if agent_dir="$(find_agent_dir)"; then
    candidate="${agent_dir}/venv/bin/python"
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  echo "[XX] Could not find a Python interpreter for Hermes Web UI." >&2
  return 1
}

PYTHON="$(resolve_python)"

export HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-0.0.0.0}"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-8787}"
export HERMES_WEBUI_STATE_DIR="${HERMES_WEBUI_STATE_DIR:-${HOME}/.hermes/webui-mvp}"

cd "${REPO_ROOT}"
exec "${PYTHON}" "${REPO_ROOT}/server.py"
