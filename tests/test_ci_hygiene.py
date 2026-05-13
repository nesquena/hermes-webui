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


def test_live_model_success_log_is_debug_not_default_console_log():
    ui = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "console.debug('[hermes] Live models loaded" in ui
    assert "console.log('[hermes] Live models loaded" not in ui
