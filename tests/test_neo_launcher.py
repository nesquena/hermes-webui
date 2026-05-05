from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent


def test_neo_launcher_loads_dotenv_before_host_port_defaults():
    src = (REPO_ROOT / "neo.sh").read_text(encoding="utf-8")

    source_pos = src.find('source "${REPO_ROOT}/.env"')
    host_pos = src.find('HOST="${HERMES_WEBUI_HOST:-127.0.0.1}"')
    port_pos = src.find('PORT="${HERMES_WEBUI_PORT:-8787}"')

    assert source_pos != -1, "neo.sh must source repo .env"
    assert host_pos != -1, "neo.sh must define HOST from HERMES_WEBUI_HOST"
    assert port_pos != -1, "neo.sh must define PORT from HERMES_WEBUI_PORT"
    assert source_pos < host_pos
    assert source_pos < port_pos
