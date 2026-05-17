"""Regression coverage for #2237 Docker startup chown on git object packs."""

from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[1]
INIT_SCRIPT = (REPO / "docker_init.bash").read_text(encoding="utf-8")


def test_home_chown_skips_hermes_agent_git_objects():
    assert "chown_home_hermeswebui()" in INIT_SCRIPT
    assert "/home/hermeswebui/.hermes/hermes-agent/.git/objects" in INIT_SCRIPT
    assert "-prune" in INIT_SCRIPT
    assert 'chown -h "${WANTED_UID}:${WANTED_GID}"' in INIT_SCRIPT


def test_root_init_uses_git_object_safe_chown_helper():
    root_start = INIT_SCRIPT.index('if [ "A${whoami}" == "Aroot" ]; then')
    root_restart = INIT_SCRIPT.index("exec su", root_start)
    root_section = INIT_SCRIPT[root_start:root_restart]

    assert "chown_home_hermeswebui || error_exit" in root_section
    assert 'chown -R "${WANTED_UID}:${WANTED_GID}" /home/hermeswebui' not in root_section


def test_docker_init_bash_syntax_still_valid():
    result = subprocess.run(
        ["bash", "-n", str(REPO / "docker_init.bash")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
