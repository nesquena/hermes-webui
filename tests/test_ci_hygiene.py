"""Small hygiene regression checks for CI and frontend console noise."""

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _make_executable(path):
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_github_actions_quotes_pyyaml_version_specifier():
    """Unquoted `pyyaml>=6.0` is parsed by the shell as stdout redirection."""
    workflow = ROOT / ".github" / "workflows" / "tests.yml"
    text = workflow.read_text(encoding="utf-8")

    assert '"pyyaml>=6.0"' in text or "'pyyaml>=6.0'" in text
    assert "pip install pyyaml>=6.0" not in text


def test_pytest_integration_marker_is_registered():
    config = ROOT / "pytest.ini"
    text = config.read_text(encoding="utf-8")

    assert "markers" in text
    assert "integration:" in text


def test_local_test_runner_uses_supported_venv_before_pytest_collection():
    runner = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    resolve_body = runner.split("resolve_venv_python() {", 1)[1].split("\n}\n\nVENV_PY", 1)[0]

    assert "python3.13 python3.12 python3.11 python3" in runner
    assert "requirements-dev.txt" in runner
    assert 'HERMES_WEBUI_TEST_PYTHON' in runner
    assert "resolve_venv_python()" in runner
    assert '"$VENV_DIR/bin/python" "$VENV_DIR/Scripts/python.exe"' in runner
    assert 'if [[ -x "$candidate" ]]; then' in resolve_body
    assert 'printf \'%s\\n\' "$candidate"' in resolve_body
    assert "exec \"$PYTHON_BIN\" -m pytest" in runner
    # Destructive-fs guard: never create/clear a virtualenv through a symlinked .venv
    # (`python -m venv --clear` would empty the symlink's target).
    assert '-L "$VENV_DIR"' in runner
    assert "Hermes WebUI tests require Python 3.11, 3.12, or 3.13" in conftest
    assert "Run ./scripts/test.sh" in conftest


def test_local_test_runner_bootstrap_handles_broken_venvs_safely():
    runner = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
    select_body = runner.split("select_python() {", 1)[1].split("\n}\n\nPYTHON_BIN", 1)[0]
    create_body = runner.split("create_or_rebuild_venv() {", 1)[1].split("\n}\n\nselect_python", 1)[0]

    assert "has_pip()" in runner
    assert 'if ! "$base_py" -m venv "${venv_args[@]}"; then' in create_body
    assert 'rm -rf "$VENV_DIR"' in create_body
    assert 'VENV_PY="$(resolve_venv_python || true)"' in create_body
    assert 'does not contain bin/python or Scripts/python.exe' in create_body
    assert 'if ! has_pip "$VENV_PY"; then' in create_body
    assert 'venv_guidance "$base_py"' in create_body
    assert 'printf \'%s\\n\' "$requested_path"' not in select_body
    assert 'base_py="$requested_path"' in select_body
    assert 'desired_major_minor="$(python_major_minor "$base_py")"' in select_body
    assert 'VENV_PY="$(resolve_venv_python || true)"' in select_body
    assert 'if has_pip "$VENV_PY"; then' in select_body
    assert 'does not contain bin/python or Scripts/python.exe; rebuilding.' in select_body
    assert 'create_or_rebuild_venv "$base_py" rebuild' in select_body


def test_local_test_runner_accepts_windows_layout_venv_from_base_python(tmp_path):
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "test.sh", scripts_dir / "test.sh")
    _make_executable(scripts_dir / "test.sh")
    (repo / "requirements-dev.txt").write_text("", encoding="utf-8")

    fake_venv_python_source = repo / "fake-venv-python-source"
    fake_venv_python_source.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-" ]]; then
          program="$(cat)"
          if [[ "$program" == *"sys.version_info[:3]"* ]]; then
            echo "3.11.15"
          elif [[ "$program" == *"sys.version_info[:2]"* ]]; then
            echo "3.11"
          fi
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "pip" && "${3:-}" == "--version" ]]; then
          echo "pip 24.0"
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "pytest" ]]; then
          printf '%s\\n' "$0" > "$PYTEST_PROOF_FILE"
          printf '%s\\n' "$@" >> "$PYTEST_PROOF_FILE"
          exit 0
        fi
        exit 99
        """), encoding="utf-8")
    _make_executable(fake_venv_python_source)

    fake_base_python = repo / "fake-python"
    fake_base_python.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-" ]]; then
          program="$(cat)"
          if [[ "$program" == *"sys.version_info[:3]"* ]]; then
            echo "3.11.15"
          elif [[ "$program" == *"sys.version_info[:2]"* ]]; then
            echo "3.11"
          fi
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
          target="${@: -1}"
          mkdir -p "$target/Scripts"
          cp "$FAKE_VENV_PY_SOURCE" "$target/Scripts/python.exe"
          chmod +x "$target/Scripts/python.exe"
          exit 0
        fi
        exit 99
        """), encoding="utf-8")
    _make_executable(fake_base_python)

    proof = repo / "pytest-proof.txt"
    env = os.environ.copy()
    env["HERMES_WEBUI_TEST_PYTHON"] = "./fake-python"
    env["FAKE_VENV_PY_SOURCE"] = "./fake-venv-python-source"
    env["PYTEST_PROOF_FILE"] = str(proof)

    result = subprocess.run(
        ["bash", "scripts/test.sh", "tests/example_test.py", "-v", "--timeout=60"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (repo / ".venv" / "Scripts" / "python.exe").exists()
    proof_lines = proof.read_text(encoding="utf-8").splitlines()
    assert proof_lines[0].replace("\\", "/").endswith("/.venv/Scripts/python.exe")
    assert proof_lines[1:] == ["-m", "pytest", "tests/example_test.py", "-v", "--timeout=60"]

def test_local_test_runner_rejects_venv_without_accepted_python_path(tmp_path):
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "test.sh", scripts_dir / "test.sh")
    _make_executable(scripts_dir / "test.sh")
    (repo / "requirements-dev.txt").write_text("", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8")

    fake_base_python = repo / "fake-python"
    fake_base_python.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "-" ]]; then
          program="$(cat)"
          if [[ "$program" == *"sys.version_info[:3]"* ]]; then
            echo "3.11.15"
          elif [[ "$program" == *"sys.version_info[:2]"* ]]; then
            echo "3.11"
          fi
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
          target="${@: -1}"
          mkdir -p "$target"
          touch "$target/pyvenv.cfg"
          exit 0
        fi
        exit 99
        """), encoding="utf-8")
    _make_executable(fake_base_python)

    proof = repo / "pytest-proof.txt"
    env = os.environ.copy()
    env["HERMES_WEBUI_TEST_PYTHON"] = "./fake-python"
    env["PYTEST_PROOF_FILE"] = str(proof)

    result = subprocess.run(
        ["bash", "scripts/test.sh", "tests/example_test.py", "-v", "--timeout=60"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 2
    assert "does not contain bin/python or Scripts/python.exe" in result.stderr
    assert not proof.exists()
    assert not (repo / ".venv").exists()

def test_live_model_success_log_is_debug_not_default_console_log():
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "console.debug('[hermes] Live models loaded" in ui
    assert "console.log('[hermes] Live models loaded" not in ui
