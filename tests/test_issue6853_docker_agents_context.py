"""Regression coverage for #6853: keep repository instructions out of the image."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]


def _dockerignore_rules() -> set[str]:
    return {
        line.strip()
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _excluded_by_root_rule(path: str, rules: set[str]) -> bool:
    """Model the exact root-only rule used for this context boundary."""
    return "/AGENTS.md" in rules and path == "AGENTS.md"


def test_repository_agents_file_is_excluded_from_docker_context():
    """The repository instruction file must not enter the runtime image."""
    assert _excluded_by_root_rule("AGENTS.md", _dockerignore_rules())


def test_workspace_agents_file_is_not_recursively_excluded():
    """A selected workspace may still provide its own Agent instructions."""
    assert not _excluded_by_root_rule("workspace/AGENTS.md", _dockerignore_rules())


def test_dockerfile_copies_context_to_apptoo_before_runtime_seed():
    """The image source and runtime seed source must share the filtered tree."""
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    copy_lines = {
        line.strip()
        for line in dockerfile.splitlines()
        if line.lstrip().startswith("COPY ")
    }

    assert "COPY --chown=root:root . /apptoo" in copy_lines
    assert "COPY --chown=root:root . /app" not in copy_lines


def test_docker_init_has_both_filtered_context_seed_paths():
    """Both startup modes must seed /app from the Dockerfile context copy."""
    init = (REPO / "docker_init.bash").read_text(encoding="utf-8")

    assert "rsync -av --chown=hermeswebui:hermeswebui /apptoo/ /app/" in init
    assert "cp -a /apptoo/. /app/" in init
    assert "HERMES_WEBUI_DEFAULT_WORKSPACE=\"/workspace\"" in init
    assert "cd /app" in init


def _docker_runtime_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    result = subprocess.run(
        [docker, "info"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0


@pytest.mark.skipif(
    not os.environ.get("HERMES_RUN_DOCKER_RUNTIME_TESTS") or not _docker_runtime_available(),
    reason="Docker runtime proof is opt-in for the CI integration job",
)
@pytest.mark.timeout(900)
def test_docker_runtime_keeps_app_clean_in_both_seed_modes_and_workspace_intact(tmp_path):
    """Build the image and exercise the exact normal/rootless seed destinations."""
    docker = shutil.which("docker")
    assert docker is not None
    tag = f"hermes-webui-pr-6853-{os.getpid()}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("workspace instructions\n", encoding="utf-8")

    try:
        subprocess.run(
            [docker, "build", "--progress=plain", "-t", tag, "."],
            check=True,
            timeout=900,
        )
        for user in (None, "1000:1000"):
            command = (
                "set -eu; rm -rf /app/* /app/.[!.]* /app/..?*; "
                + (
                    "rsync -a --chown=hermeswebui:hermeswebui /apptoo/ /app/"
                    if user is None
                    else "cp -a /apptoo/. /app/"
                )
                + "; test -f /app/server.py; test ! -e /app/AGENTS.md; "
                "test -f /workspace/AGENTS.md"
            )
            args = [docker, "run", "--rm", "--mount", f"type=bind,source={workspace},target=/workspace"]
            if user is not None:
                args.extend(["--user", user])
            args.extend(["--entrypoint", "bash", tag, "-c", command])
            subprocess.run(args, check=True, timeout=60)
    finally:
        subprocess.run([docker, "rmi", "-f", tag], check=False, timeout=60)
