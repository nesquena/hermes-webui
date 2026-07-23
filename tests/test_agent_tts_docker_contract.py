"""Standalone Docker image contract for Agent-delegated TTS."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INIT = (ROOT / "docker_init.bash").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


def test_clean_container_installs_pinned_agent_package_when_source_is_absent():
    assert 'hermes-agent==0.19.0' in INIT
    assert 'uv pip install "$_agent_package_requirement"' in INIT
    assert "hermes-agent source not found" not in INIT
    assert "reduced functionality" not in INIT


def test_container_dependency_setup_verifies_agent_tts_imports():
    assert "from tools import tts_tool" in INIT
    assert "from hermes_cli import config, tools_config" in INIT
    assert 'error_exit "Hermes Agent TTS imports are unavailable"' in INIT


def test_webui_requirements_no_longer_advertise_direct_edge_engine():
    assert "Edge TTS speech engine" not in REQUIREMENTS
    assert "server-side Microsoft neural voices" not in REQUIREMENTS
