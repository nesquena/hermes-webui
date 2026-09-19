#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT/.venv"
REQ_FILE="$ROOT/requirements-dev.txt"

resolve_venv_python() {
  local candidate
  for candidate in "$VENV_DIR/bin/python" "$VENV_DIR/Scripts/python.exe"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

VENV_PY="$(resolve_venv_python || true)"

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

python_major_minor() {
  "$1" - <<'PY'
import sys
print(".".join(map(str, sys.version_info[:2])))
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

has_pip() {
  "$1" -m pip --version >/dev/null 2>&1
}

ensure_pip() {
  local py="$1"
  if ! has_pip "$py"; then
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

venv_guidance() {
  local base_py="${1:-}"
  if [[ -n "$base_py" ]]; then
    echo "Could not create a working .venv with $base_py ($(python_version "$base_py"))." >&2
  else
    echo "Could not create a working .venv for Hermes WebUI tests." >&2
  fi
  echo "Install the matching Python venv/ensurepip package (for example python3.x-venv on Debian/Ubuntu)" >&2
  echo "or set HERMES_WEBUI_TEST_PYTHON to a supported Python 3.11, 3.12, or 3.13 interpreter." >&2
}

create_or_rebuild_venv() {
  local base_py="$1"
  local mode="${2:-create}"
  local action="Creating"
  local venv_args=("$VENV_DIR")
  # Never create/clear a virtualenv *through* a symlink: `python -m venv --clear`
  # (and a dangling-symlink create) would empty/write the symlink's target, not a
  # repo-local .venv. A symlinked .venv that already works is used directly above,
  # before this function is reached, so refusing here only blocks the destructive path.
  if [[ -L "$VENV_DIR" ]]; then
    echo "$VENV_DIR is a symlink; refusing to create or clear a virtualenv through it." >&2
    echo "Remove the .venv symlink (or set HERMES_WEBUI_TEST_PYTHON to a supported Python 3.11-3.13) and rerun." >&2
    return 2
  fi
  if [[ "$mode" == "rebuild" ]]; then
    action="Rebuilding"
    venv_args=(--clear "$VENV_DIR")
  fi
  echo "$action .venv with $base_py ($(python_version "$base_py"))." >&2
  if ! "$base_py" -m venv "${venv_args[@]}"; then
    rm -rf "$VENV_DIR"
    venv_guidance "$base_py"
    return 2
  fi
  VENV_PY="$(resolve_venv_python || true)"
  if [[ ! -x "$VENV_PY" ]]; then
    rm -rf "$VENV_DIR"
    echo "$VENV_DIR was created but does not contain bin/python or Scripts/python.exe." >&2
    venv_guidance "$base_py"
    return 2
  fi
  if ! has_pip "$VENV_PY"; then
    rm -rf "$VENV_DIR"
    echo "$VENV_DIR was created but its Python cannot run pip." >&2
    venv_guidance "$base_py"
    return 2
  fi
}

select_python() {
  local requested="${HERMES_WEBUI_TEST_PYTHON:-}"
  local requested_path base_py
  local desired_major_minor current_major_minor

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
    base_py="$requested_path"
    desired_major_minor="$(python_major_minor "$base_py")"
  else
    base_py="$(find_supported_base_python || true)"
    if [[ -z "$base_py" ]]; then
      echo "No supported Python found for Hermes WebUI tests." >&2
      echo "Install Python 3.11, 3.12, or 3.13, then rerun ./scripts/test.sh." >&2
      return 2
    fi
    desired_major_minor=""
  fi

  VENV_PY="$(resolve_venv_python || true)"
  if [[ -x "$VENV_PY" ]]; then
    if is_supported_python "$VENV_PY"; then
      current_major_minor="$(python_major_minor "$VENV_PY")"
      if [[ -z "$desired_major_minor" || "$current_major_minor" == "$desired_major_minor" ]]; then
        if has_pip "$VENV_PY"; then
          printf '%s\n' "$VENV_PY"
          return 0
        fi
        echo "Existing .venv uses supported Python $(python_version "$VENV_PY") but cannot run pip; rebuilding." >&2
        create_or_rebuild_venv "$base_py" rebuild || return $?
        printf '%s\n' "$VENV_PY"
        return 0
      fi
      echo "Rebuilding .venv from Python $current_major_minor to requested $desired_major_minor." >&2
      create_or_rebuild_venv "$base_py" rebuild || return $?
      printf '%s\n' "$VENV_PY"
      return 0
    fi
    echo "Rebuilding unsupported .venv ($(python_version "$VENV_PY")) with $base_py ($(python_version "$base_py"))." >&2
    create_or_rebuild_venv "$base_py" rebuild || return $?
    printf '%s\n' "$VENV_PY"
    return 0
  fi

  if [[ -e "$VENV_DIR" && ! -x "$VENV_PY" ]]; then
    echo "$VENV_DIR exists but does not contain bin/python or Scripts/python.exe; rebuilding." >&2
    create_or_rebuild_venv "$base_py" rebuild || return $?
    printf '%s\n' "$VENV_PY"
    return 0
  fi

  create_or_rebuild_venv "$base_py" create || return $?
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
