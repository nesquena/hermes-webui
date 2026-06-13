#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"
REQ_FILE="$ROOT/requirements-dev.txt"

is_supported_python() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)
PY
}

python_version() {
  "$1" - <<'PY'
import sys
print(".".join(map(str, sys.version_info[:3])))
PY
}

resolve_executable() {
  local candidate="$1"
  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] && printf '%s\n' "$candidate" || true
  else
    command -v "$candidate" 2>/dev/null || true
  fi
}

find_supported_base_python() {
  local candidate path
  for candidate in python3.13 python3.12 python3.11 python3; do
    path="$(resolve_executable "$candidate")"
    if [[ -n "$path" ]] && is_supported_python "$path"; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

ensure_pip() {
  local py="$1"
  if ! "$py" -m pip --version >/dev/null 2>&1; then
    "$py" -m ensurepip --upgrade
  fi
}

missing_dev_deps() {
  "$1" - <<'PY'
import importlib.util

modules = [
    "cryptography",
    "mcp",
    "pytest",
    "pytest_asyncio",
    "pytest_shard",
    "pytest_timeout",
    "ruff",
    "yaml",
]
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if missing:
    print(", ".join(missing))
    raise SystemExit(1)
PY
}

select_python() {
  local requested="${HERMES_WEBUI_TEST_PYTHON:-}"
  local requested_path base_py

  if [[ -n "$requested" ]]; then
    requested_path="$(resolve_executable "$requested")"
    if [[ -z "$requested_path" || ! -x "$requested_path" ]]; then
      echo "HERMES_WEBUI_TEST_PYTHON does not point to an executable: $requested" >&2
      return 2
    fi
    if ! is_supported_python "$requested_path"; then
      echo "Unsupported Python for Hermes WebUI tests: $requested_path ($(python_version "$requested_path"))" >&2
      echo "Use Python 3.11, 3.12, or 3.13." >&2
      return 2
    fi
    printf '%s\n' "$requested_path"
    return 0
  fi

  if [[ -x "$VENV_PY" ]] && is_supported_python "$VENV_PY"; then
    printf '%s\n' "$VENV_PY"
    return 0
  fi

  base_py="$(find_supported_base_python || true)"
  if [[ -z "$base_py" ]]; then
    echo "No supported Python found for Hermes WebUI tests." >&2
    echo "Install Python 3.11, 3.12, or 3.13, then rerun ./scripts/test.sh." >&2
    return 2
  fi

  if [[ -e "$VENV_DIR" && ! -x "$VENV_PY" ]]; then
    echo "$VENV_DIR exists but does not contain bin/python; set HERMES_WEBUI_TEST_PYTHON or fix the venv." >&2
    return 2
  fi

  if [[ -x "$VENV_PY" ]]; then
    echo "Rebuilding unsupported .venv ($(python_version "$VENV_PY")) with $base_py ($(python_version "$base_py"))." >&2
    "$base_py" -m venv --clear "$VENV_DIR"
  else
    echo "Creating .venv with $base_py ($(python_version "$base_py"))." >&2
    "$base_py" -m venv "$VENV_DIR"
  fi

  printf '%s\n' "$VENV_PY"
}

PYTHON_BIN="$(select_python)" || exit $?

if [[ ! -f "$REQ_FILE" ]]; then
  echo "Missing $REQ_FILE" >&2
  exit 2
fi

if missing="$(missing_dev_deps "$PYTHON_BIN" 2>/dev/null)"; then
  :
else
  echo "Installing missing Hermes WebUI test dependencies in $PYTHON_BIN ($(python_version "$PYTHON_BIN"))." >&2
  if [[ -n "${missing:-}" ]]; then
    echo "Missing modules: $missing" >&2
  fi
  ensure_pip "$PYTHON_BIN"
  "$PYTHON_BIN" -m pip install --upgrade pip
  "$PYTHON_BIN" -m pip install -r "$REQ_FILE"
fi

if [[ $# -eq 0 ]]; then
  set -- tests/ -v --timeout=60
fi

exec "$PYTHON_BIN" -m pytest "$@"
