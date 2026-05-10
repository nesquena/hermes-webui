"""Regression tests for #926 Hindsight dependency in Docker WebUI venv."""

import pathlib


REPO_ROOT = pathlib.Path(__file__).parent.parent
INIT_SH = (REPO_ROOT / "docker_init.bash").read_text(encoding="utf-8")
REQUIREMENTS_TXT = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")


def test_926_docker_init_installs_hindsight_distribution():
    """Docker init must install the PyPI distribution named hindsight-client."""
    assert "uv pip show hindsight-client" in INIT_SH
    assert '"hindsight-client>=0.4.22"' in INIT_SH
    assert 'uv pip install "${_hindsight_client_requirement}"' in INIT_SH


def test_926_hindsight_install_runs_after_fast_restart_guard():
    """Existing Docker venvs with .deps_installed must still get hindsight-client."""
    deps_guard_pos = INIT_SH.find("if [ -f /app/venv/.deps_installed ]; then")
    assert deps_guard_pos != -1, ".deps_installed fast-restart guard not found"

    expected_sequence = "\nfi\n\nensure_hindsight_client_docker_dependency\n"
    call_after_guard_pos = INIT_SH.find(expected_sequence, deps_guard_pos)
    assert call_after_guard_pos != -1, (
        "hindsight-client install check must run outside the .deps_installed guard "
        "so old Docker venvs self-heal on fast restart"
    )


def test_926_hindsight_dependency_stays_docker_specific():
    """Local non-Docker bootstrap should not install optional memory clients."""
    assert "hindsight-client" not in REQUIREMENTS_TXT
