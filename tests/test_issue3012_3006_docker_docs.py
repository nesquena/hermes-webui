from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = (REPO / "README.md").read_text(encoding="utf-8")
DOCKER_MD = (REPO / "docs" / "docker.md").read_text(encoding="utf-8")


def test_docker_docs_explain_host_localhost_for_api_urls():
    """#3012: container localhost is not the Docker host localhost."""
    assert "API base URL set to localhost fails from Docker" in DOCKER_MD
    assert "Inside a container, `localhost` means *that container*" in DOCKER_MD
    assert "host.docker.internal" in DOCKER_MD
    assert "host.containers.internal" in DOCKER_MD
    assert "host-gateway" in DOCKER_MD


def test_readme_common_failures_mentions_host_localhost():
    assert "Host API at `localhost` fails from WebUI" in README
    assert "Container `localhost` means the container" in README
    assert "host.docker.internal" in README


def test_docker_docs_warn_sudo_changes_home_bind_mount():
    """#3006: sudo can render ${HOME}/.hermes as /root/.hermes."""
    assert "`sudo docker compose up -d` can make `${HOME}` expand to the root user's home" in README
    assert "Docker mounts the wrong `.hermes` directory instead of your real `~/.hermes`" in README
    assert "HERMES_HOME=/home/you/.hermes" in README

    assert "sudo` often changes `$HOME` to `/root`" in DOCKER_MD
    assert "`${HERMES_HOME:-${HOME}/.hermes}` becomes `/root/.hermes`" in DOCKER_MD
    assert "HERMES_HOME=/home/youruser/.hermes" in DOCKER_MD
    assert "docker compose config" in DOCKER_MD


def test_related_issues_index_references_3012_and_3006():
    related = DOCKER_MD[DOCKER_MD.index("## Related issues"):]
    assert "#3012" in related
    assert "#3006" in related
