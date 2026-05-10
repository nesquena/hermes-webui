"""Regression coverage for issue #1908 Docker production hardening."""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
INIT_SCRIPT = (REPO / "docker_init.bash").read_text(encoding="utf-8")
DOCKER_DOCS = (REPO / "docs" / "docker.md").read_text(encoding="utf-8")


def _dockerfile_install_packages() -> str:
    match = re.search(
        r"apt-get install -y --no-install-recommends \\\n(?P<body>.*?)&& apt-get upgrade -y",
        DOCKERFILE,
        re.DOTALL,
    )
    assert match, "Could not find the production apt package install block"
    return match.group("body")


def test_production_dockerfile_does_not_grant_passwordless_sudo():
    """The production image must not install sudo or grant NOPASSWD root escalation."""
    packages = _dockerfile_install_packages()
    assert "sudo" not in packages, "production Dockerfile must not install sudo"
    assert "NOPASSWD" not in DOCKERFILE, "production Dockerfile must not grant passwordless sudo"
    assert "adduser hermeswebui sudo" not in DOCKERFILE
    assert "adduser hermeswebuitoo sudo" not in DOCKERFILE
    assert "hermeswebuitoo" not in DOCKERFILE, "production image should not keep a sudo-capable staging user"


def test_init_script_does_not_depend_on_sudo_at_runtime():
    """Runtime setup may start as root, but must drop privileges without sudo."""
    assert re.search(r"^if \[ \"A\$\{whoami\}\" == \"Aroot\" \]; then", INIT_SCRIPT, re.MULTILINE), (
        "docker_init.bash should perform privileged setup only in an explicit root init block"
    )
    assert "sudo " not in INIT_SCRIPT, "docker_init.bash must not invoke sudo in production"
    assert re.search(r"\bsu\b.*\bhermeswebui\b", INIT_SCRIPT), (
        "docker_init.bash must drop from root to hermeswebui before launching the server"
    )


def test_init_script_uses_private_scratch_permissions():
    """Init scratch paths under /tmp must be owner-only, not world-writable."""
    assert "chmod 777" not in INIT_SCRIPT
    assert "umask 0077" in INIT_SCRIPT
    assert re.search(r"chmod\s+700\s+\"?\$itdir\"?", INIT_SCRIPT), (
        "/tmp/hermeswebui_init should be mode 700"
    )
    assert re.search(r"chmod\s+600\s+\"?\$\{?tmpfile\}?\"?", INIT_SCRIPT), (
        "scratch files storing UID/GID/env data should be mode 600"
    )


def test_docker_docs_explain_production_privilege_model():
    """Docs must describe the production threat model rather than hiding the tradeoff."""
    hardening_section = DOCKER_DOCS[DOCKER_DOCS.find("## Production image security model") :]
    assert "## Production image security model" in DOCKER_DOCS
    assert "passwordless sudo" in hardening_section
    assert "root" in hardening_section and "hermeswebui" in hardening_section
    assert "single-tenant" in hardening_section
