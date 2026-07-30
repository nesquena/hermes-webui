"""Regression coverage for #2237 Docker startup chown on git object packs."""

from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[1]
INIT_SCRIPT = (REPO / "docker_init.bash").read_text(encoding="utf-8")


def test_home_chown_skips_hermes_agent_subtree():
    """The chown walk must skip the entire hermes-agent mount, not just
    .git/objects. The original #2237 issue was macOS bind mounts exposing
    read-only `.git/objects` packs; the post-v0.51.83 multi-container compose
    setup additionally mounts the whole hermes-agent source tree :ro on the
    WebUI side (#2470).  Either failure mode breaks the chown walk under
    `set -e`; pruning the parent path covers both."""
    assert "chown_home_hermeswebui()" in INIT_SCRIPT
    # The prune target should be the whole hermes-agent subtree, not just
    # the inner `.git/objects` directory. The old narrower prune was
    # insufficient once the entire mount became :ro.
    assert "-path \"/home/hermeswebui/.hermes/hermes-agent\" -prune" in INIT_SCRIPT, (
        "chown walk must prune the entire hermes-agent path (not just "
        ".git/objects) so a :ro multi-container mount doesn't EROFS-fail "
        "the chown."
    )
    assert 'chown -h "${WANTED_UID}:${WANTED_GID}"' in INIT_SCRIPT


def test_home_chown_helper_documents_readonly_mount_compat():
    """The prune comment must reference the :ro multi-container scenario so
    future maintainers don't narrow it back to just .git/objects (which would
    re-introduce the EROFS-on-startup failure for the multi-container setup)."""
    chown_fn_start = INIT_SCRIPT.index("chown_home_hermeswebui()")
    chown_fn_end = INIT_SCRIPT.index("\n}\n", chown_fn_start)
    fn_block = INIT_SCRIPT[chown_fn_start:chown_fn_end]

    assert "read-only" in fn_block.lower() or "ro" in fn_block.lower(), (
        "chown_home_hermeswebui must document why the entire hermes-agent "
        "path is pruned (the :ro mount made the previous narrower prune "
        "insufficient)."
    )


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
