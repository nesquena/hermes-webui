"""Regression tests for the Docker docs+readonly hygiene PR (post v0.51.83).

Pins three invariants:

1. The `hermes-agent-src` named volume is mounted READ-ONLY on the WebUI
   service in both multi-container compose files. The WebUI only reads it to
   install agent Python deps at startup; this is defence-in-depth against a
   compromised WebUI writing into the agent's source tree (Concern raised by
   RustyLopez on #2453 and #1416).

2. The workspace bind-mount default uses `${HOME}/workspace` (not `~/workspace`)
   in both multi-container compose files, matching the single-container
   convention so `~`/`${HOME}` doesn't disagree across Linux, macOS, WSL2, and
   Docker Desktop on Windows.

3. `docs/docker.md` documents the agent-image upgrade procedure (`docker volume
   rm hermes-agent-src`) — the root cause of #1416.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# ── 1: hermes-agent-src must be read-only on the WebUI mount ────────────────


def test_two_container_webui_mounts_agent_src_readonly():
    """The WebUI only reads the agent source to install Python deps. Mounting
    read-only enforces that at the kernel layer — a compromised WebUI process
    cannot rewrite the agent source it then imports."""
    src = (REPO / "docker-compose.two-container.yml").read_text(encoding="utf-8")
    assert (
        "hermes-agent-src:/home/hermeswebui/.hermes/hermes-agent:ro" in src
    ), (
        "two-container: the WebUI must mount hermes-agent-src with :ro. "
        "Without :ro, a compromised WebUI process can rewrite the agent's "
        "Python source tree."
    )


def test_three_container_webui_mounts_agent_src_readonly():
    src = (REPO / "docker-compose.three-container.yml").read_text(encoding="utf-8")
    assert (
        "hermes-agent-src:/home/hermeswebui/.hermes/hermes-agent:ro" in src
    ), (
        "three-container: the WebUI must mount hermes-agent-src with :ro."
    )


def test_agent_service_keeps_writable_agent_src_mount():
    """The agent SERVICE writes the source tree to the volume on first up.
    It must stay read-write — only the WebUI side is read-only."""
    for fn in ("docker-compose.two-container.yml", "docker-compose.three-container.yml"):
        src = (REPO / fn).read_text(encoding="utf-8")
        # The agent's mount is `hermes-agent-src:/opt/hermes` (no :ro suffix).
        # Look for the line that has /opt/hermes without :ro.
        agent_lines = [
            line for line in src.splitlines()
            if "hermes-agent-src:/opt/hermes" in line
        ]
        assert agent_lines, f"{fn}: agent must mount hermes-agent-src at /opt/hermes"
        for line in agent_lines:
            assert not line.rstrip().endswith(":ro"), (
                f"{fn}: agent's hermes-agent-src mount must be writable "
                f"(it populates /opt/hermes on first run): {line!r}"
            )


# ── 2: ${HOME} (not ~) in workspace bind defaults ───────────────────────────


def test_two_container_workspace_uses_home_env_var():
    """Compose v2 expands `~` differently than `${HOME}` under sudo, on Docker
    Desktop on Windows, and on some NAS appliances. Use `${HOME}` to match the
    single-container `docker-compose.yml` and avoid platform drift."""
    src = (REPO / "docker-compose.two-container.yml").read_text(encoding="utf-8")
    assert "${HERMES_WORKSPACE:-${HOME}/workspace}:/workspace" in src, (
        "two-container: workspace default must use ${HOME}/workspace, not ~/workspace, "
        "to match docker-compose.yml's single-container convention."
    )
    assert "${HERMES_WORKSPACE:-~/workspace}" not in src, (
        "two-container: tilde-form workspace default still present — change to ${HOME}/workspace."
    )


def test_three_container_workspace_uses_home_env_var():
    src = (REPO / "docker-compose.three-container.yml").read_text(encoding="utf-8")
    assert "${HERMES_WORKSPACE:-${HOME}/workspace}:/workspace" in src, (
        "three-container: workspace default must use ${HOME}/workspace, not ~/workspace."
    )
    assert "${HERMES_WORKSPACE:-~/workspace}" not in src


def test_single_container_workspace_already_uses_home_env_var():
    """Sanity: the single-container file has used ${HOME} all along; pin it
    so it doesn't drift back."""
    src = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${HERMES_WORKSPACE:-${HOME}/workspace}:/workspace" in src


# ── 3: docs/docker.md documents the agent-image upgrade procedure ──────────


def test_docker_md_documents_agent_image_upgrade():
    """The `hermes-agent-src` named volume caches the agent source on first
    `up` and is reused verbatim on every subsequent `up`, even after a fresh
    `docker pull` of the agent image. This is the root cause of #1416. The
    docs must give users the explicit `docker volume rm` recipe so they don't
    misdiagnose 'missing entrypoint' errors."""
    docs = (REPO / "docs" / "docker.md").read_text(encoding="utf-8")
    assert "Upgrading the agent container" in docs, (
        "docs/docker.md must have an 'Upgrading the agent container' section."
    )
    assert "docker volume rm" in docs, (
        "docs/docker.md must show the `docker volume rm` step in the upgrade recipe."
    )
    assert "hermes-agent-src" in docs
    # Cross-reference to the original issue so users searching for the
    # symptom land in the right place
    assert "#1416" in docs


def test_compose_files_point_to_docker_md_for_upgrades():
    """Both multi-container compose files should reference docs/docker.md
    near the named-volumes block so anyone reading the compose file directly
    finds the upgrade procedure."""
    for fn in ("docker-compose.two-container.yml", "docker-compose.three-container.yml"):
        src = (REPO / fn).read_text(encoding="utf-8")
        assert "docs/docker.md" in src, (
            f"{fn}: must reference docs/docker.md so users reading the compose "
            f"file see the agent upgrade pointer."
        )
        assert "docker volume rm" in src, (
            f"{fn}: must show the `docker volume rm` upgrade step inline."
        )


# ── 4: docs/docker.md frames the isolation model honestly ──────────────────


def test_docker_md_documents_isolation_model():
    """The multi-container setups give process + network + resource isolation
    but NOT filesystem isolation. Document that explicitly so users don't
    reach for multi-container expecting a trust boundary it doesn't provide
    (RustyLopez's concern on #2453)."""
    docs = (REPO / "docs" / "docker.md").read_text(encoding="utf-8")
    assert "What the multi-container setup isolates" in docs, (
        "docs/docker.md must have a section calibrating multi-container "
        "isolation expectations — process/network/resource isolation, NOT "
        "filesystem isolation."
    )


# ── 5: docker_init.bash stages agent source to a writable build dir ─────────
#
# The :ro mount fixed in PR #2470 broke a second, less obvious surface:
# Dependency resolution can need to update uv.lock for an older or locally
# modified checkout. On a `:ro` mount that returns `EROFS` and (under `set -e`)
# kills container startup. The fix: copy the source tree into a writable tmpfs
# build dir, export its dependency set there, then clean up.
#
# This was caught the first time the Docker smoke gate ran on its own PR — a
# real regression that 5800+ source-level pytests had no way to surface
# because none of them invoked `docker_init.bash` against a real :ro mount.


def test_docker_init_exports_agent_dependencies_without_building_agent_wheel():
    """Docker must install deps without building the mounted agent project.

    Current hermes-agent releases intentionally reject wheel/sdist builds, so
    ``uv pip install <agent>[all]`` is no longer a valid dependency-install
    mechanism.  Export the curated, locked ``all`` dependency set while
    omitting the project itself, then install that exported requirements file.
    """
    src = (REPO / "docker_init.bash").read_text(encoding="utf-8")

    assert "_stage_src=" in src, (
        "docker_init.bash must stage the potentially read-only agent source "
        "before asking uv to inspect its project metadata."
    )

    stage_idx = src.index("_stage_src=")
    stage_block_end = src.index("\n  else\n", stage_idx)
    install_block = src[stage_idx:stage_block_end]

    assert 'uv export --project "$_stage_src"' in install_block
    assert "--extra all" in install_block
    assert "--locked" in install_block
    assert "--no-emit-project" in install_block
    assert 'uv pip install -r "$_agent_requirements"' in install_block
    assert 'uv pip install "$_stage_src[all]"' not in install_block


def test_docker_init_excludes_egg_info_during_staging():
    """The staging copy must exclude pre-baked *.egg-info / build / dist
    directories. setuptools takes a different (timestamp-update) code path
    when one is already present in the source tree, which itself hits the
    :ro mount through stat/utime calls. Excluding them keeps the build
    happily on the fresh-build code path.

    Tight assertions on both the rsync and cp-fallback paths — a loose
    `"egg-info" in src` check would pass on a stray comment mention, so
    we require the actual exclusion mechanics to be present.
    """
    src = (REPO / "docker_init.bash").read_text(encoding="utf-8")

    # Find the staging block: rsync invocation OR cp-fallback. Both must
    # actually exclude *.egg-info — a comment mention is not enough.
    stage_idx = src.index("_stage_src=")
    install_idx = src.index("uv pip install", stage_idx)
    stage_block = src[stage_idx:install_idx]

    # Rsync path must carry --exclude='*.egg-info'.
    assert "--exclude='*.egg-info'" in stage_block, (
        "docker_init.bash rsync invocation must include "
        "--exclude='*.egg-info' so setuptools' timestamp-update code path "
        "doesn't fire (which itself hits the :ro mount through stat/utime)."
    )

    # cp-fallback path must explicitly rm the egg-info dir after copy
    # (cp -a has no --exclude flag, so the cleanup happens post-copy).
    assert "*.egg-info" in stage_block, (
        "docker_init.bash cp-fallback must remove $_stage_src/*.egg-info "
        "after copy so the install runs on the fresh-build code path."
    )

    # Both build and dist must also be excluded — setuptools touches them
    # under different conditions but the failure mode is identical.
    assert "--exclude='build'" in stage_block, (
        "rsync staging must --exclude='build' (setuptools build artifacts)."
    )
    assert "--exclude='dist'" in stage_block, (
        "rsync staging must --exclude='dist' (setuptools build artifacts)."
    )
    assert "--exclude='__pycache__'" in stage_block, (
        "rsync staging must --exclude='__pycache__' to keep the copy minimal."
    )

    assert "--exclude='.playwright'" in stage_block, (
        "rsync staging must --exclude='.playwright' so unreadable Playwright "
        "browser dependency files (e.g. deb.deps, rpm.deps) don't cause "
        "rsync error code 23 and kill container startup."
    )
    assert ".playwright" in stage_block, (
        "Both rsync (--exclude) and cp-fallback (rm -rf) must handle "
        ".playwright so the build doesn't choke on unreadable browser files."
    )


def test_docker_init_makes_staged_dir_writable_after_ro_mount_copy():
    """The staged checkout must be writable for dependency-export fallback.

    `rsync -a` / `cp -a` preserve the source tree's mode bits, so a hermes-agent
    source mounted mode 555 leaves the staged copy also mode 555 even though the
    staging dir itself was created writable by hermeswebui. The unlocked export
    fallback then cannot refresh uv.lock. Keep `chmod -R u+w` after both copy
    paths and before dependency export/install without making the tree
    world-writable.
    """
    src = (REPO / "docker_init.bash").read_text(encoding="utf-8")

    stage_idx = src.index("_stage_src=")
    install_idx = src.index("uv pip install", stage_idx)
    stage_block = src[stage_idx:install_idx]

    # The fix MUST add owner-write to the staged tree after the rsync/cp copy.
    # A naked `chmod` call that strips 0555 (or equivalent) would also work,
    # but `u+w` is the minimal-permission form and matches the rest of the
    # docker-init.bash style (never widen perms beyond what the operator needs).
    assert re.search(r"chmod\s+-R\s+u\+w\s+\"?\\?\$?_stage_src\"?", stage_block), (
        "docker_init.bash staging block must `chmod -R u+w \"$_stage_src\"` "
        "after the rsync/cp copy. Without it, a :ro hermes-agent mount leaves "
        "the staged tree mode 555 and the unlocked dependency export fallback "
        "cannot refresh uv.lock."
    )

    # The chmod must live in the SHARED tail (after `fi`), not inside either
    # branch — otherwise dropping back to cp -a (when rsync is missing) would
    # silently re-introduce the bug.
    assert "chmod -R u+w \"$_stage_src\"" in stage_block, (
        "the post-staging chmod must use the exact `chmod -R u+w \"$_stage_src\"` "
        "form so the substring-check pattern below matches both code paths."
    )

    # Both copy branches (rsync and cp -a) close with `fi`; the chmod must
    # come AFTER the closing `fi` so it covers whichever path was taken.
    copy_if_idx = stage_block.index("if command -v rsync")
    fi_idx = stage_block.index("\n    fi\n", copy_if_idx)
    chmod_idx = stage_block.index("chmod -R u+w")
    assert chmod_idx > fi_idx, (
        "chmod must live AFTER the rsync/cp if/else closes — putting it "
        "inside one branch means the other copy path skips it and the "
        ":ro mount perm leak returns."
    )

