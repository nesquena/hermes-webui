"""Regression coverage for the Docker image smoke-test harness."""
import pathlib

REPO = pathlib.Path(__file__).parent.parent
SMOKE_SCRIPT = (REPO / "scripts" / "smoke" / "smoke_stable_image.sh").read_text(encoding="utf-8")


def test_smoke_container_sets_required_webui_state_env():
    """The smoke container must pass the env required by docker_init.bash."""
    docker_run_start = SMOKE_SCRIPT.find("docker run -d --rm --name")
    docker_run_block = SMOKE_SCRIPT[
        docker_run_start:
        SMOKE_SCRIPT.find('"${IMAGE}" >/dev/null', docker_run_start)
    ]
    assert "HERMES_HOME" in docker_run_block
    assert "/home/hermeswebui/.hermes" in docker_run_block
    assert "HERMES_WEBUI_STATE_DIR" in docker_run_block
    assert "/home/hermeswebui/.hermes/webui" in docker_run_block
