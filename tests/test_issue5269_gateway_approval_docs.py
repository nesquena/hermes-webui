from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_advanced_chat_docs_name_gateway_approval_runs_api_opt_in():
    advanced = (ROOT / "docs" / "advanced-chat-setup.md").read_text(encoding="utf-8")

    assert "HERMES_WEBUI_CHAT_BACKEND=gateway" in advanced
    assert "HERMES_WEBUI_GATEWAY_USE_RUNS_API=true" in advanced
    assert "approval prompts" in advanced
    assert "approval card" in advanced
    assert "legacy chat-completions transport" in advanced


def test_docker_docs_name_webui_service_runs_api_opt_in_for_approval_cards():
    docker = (ROOT / "docs" / "docker.md").read_text(encoding="utf-8")

    assert "HERMES_WEBUI_CHAT_BACKEND=gateway" in docker
    assert "HERMES_WEBUI_GATEWAY_BASE_URL=http://hermes-agent:8642" in docker
    assert "HERMES_WEBUI_GATEWAY_USE_RUNS_API=true" in docker
    assert "approval cards" in docker
    assert "WebUI service" in docker
