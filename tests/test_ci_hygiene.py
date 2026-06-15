"""Small hygiene regression checks for CI and frontend console noise."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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

    assert "python3.13 python3.12 python3.11 python3" in runner
    assert "requirements-dev.txt" in runner
    assert 'HERMES_WEBUI_TEST_PYTHON' in runner
    assert '[[ -x "$candidate" ]] && printf' in runner
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
    assert '[[ ! -x "$VENV_PY" ]]' in create_body
    assert 'if ! has_pip "$VENV_PY"; then' in create_body
    assert 'venv_guidance "$base_py"' in create_body
    assert 'printf \'%s\\n\' "$requested_path"' not in select_body
    assert 'base_py="$requested_path"' in select_body
    assert 'desired_major_minor="$(python_major_minor "$base_py")"' in select_body
    assert 'if has_pip "$VENV_PY"; then' in select_body
    assert 'create_or_rebuild_venv "$base_py" rebuild' in select_body


def test_live_model_success_log_is_debug_not_default_console_log():
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "console.debug('[hermes] Live models loaded" in ui
    assert "console.log('[hermes] Live models loaded" not in ui
