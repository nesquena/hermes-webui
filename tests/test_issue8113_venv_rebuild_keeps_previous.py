"""#8113: rebuilding the agent venv must never leave the container worse off.

When the staged Agent needs an interpreter the existing ``/app/venv`` cannot
provide, ``docker_init.bash`` rebuilds it. A rebuild that deletes the old venv
before the new one is fully installed turns an offline restart (or one with
``UV_PYTHON_DOWNLOADS=never``) from "WebUI boots, the Agent is reported
unimportable" into a failed start. ``rebuild_agent_venv`` is therefore a
transaction: the old venv and staged Agent source are moved aside, and deleted
only once the replacement exists, runs an admissible interpreter and has every
dependency installed. Any failure puts them back. A journal written before
anything moves records the transaction, so a container stopped mid-way is
rolled back, or its commit finished, on the next boot, while a ``.previous``
directory this script did not create is never trusted or deleted.

These tests run the real bash functions from ``docker_init.bash`` with a fake
``uv`` on ``PATH``, the real ``scripts/docker_venv_python.py --admits`` check,
and stand-ins for the dependency installers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INIT_SH = (REPO / "docker_init.bash").read_text(encoding="utf-8")
HELPER = REPO / "scripts" / "docker_venv_python.py"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bash and python3 are needed to run docker_init.bash code",
)

AGENT_PYPROJECT = """
[project]
name = "hermes-agent"
requires-python = ">=3.11,<3.15"
dependencies = [
  "python-dotenv==1.2.2; python_version >= '3.14'",
  "click>=8.1",
]
"""

FAKE_UV = """#!/bin/bash
# fake uv: only `uv venv <dir>` is supported
[ "$1" = venv ] || exit 64
printf '%s\\n' "${UV_PYTHON:--}" >> "$FAKE_UV_LOG"
mkdir -p "$2/bin"
if [ "${FAKE_UV_FAIL:-0}" = 1 ]; then
  echo partial > "$2/partial"
  exit 1
fi
echo new > "$2/marker"
: > "$2/bin/python3"
printf 'home = /usr/local/bin\\nversion_info = %s.final.0\\n' "${FAKE_UV_VERSION:-3.14.2}" > "$2/pyvenv.cfg"
"""

# Stand-ins for the real installers: the real ones need a network and an Agent.
STUBS = """
install_venv_dependencies() {
  [ "${FAKE_DEPS_FAIL:-0}" = 1 ] && return 1
  mkdir -p "$_agent_stage" && echo new > "$_agent_stage/marker"
  if [ "${FAKE_STAGE_READONLY_FAIL:-0}" = 1 ]; then
    chmod -R a-w "$_agent_stage"  # a copy of the :ro source that failed before its chmod -R u+w
    return 1
  fi
  touch "$_agent_venv/.deps_installed"
}
ensure_hindsight_client_docker_dependency() {
  [ "$1" = soft ] || return 99
  [ "${FAKE_HINDSIGHT_FAIL:-0}" = 1 ] && return 1
  return 0
}
error_exit() { echo "ERROR_EXIT: $*"; exit 1; }
"""

FUNCTIONS = (
    "write_agent_venv_journal",
    "restore_previous_agent_venv",
    "commit_agent_venv_rebuild",
    "recover_interrupted_agent_venv_rebuild",
    "rebuild_agent_venv",
)


def _bash_function(src: str, name: str) -> str:
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + len("\n}\n")
    return src[start:end]


def _layout(root: Path) -> dict[str, Path]:
    app = root / "app"
    venv = app / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").write_text("old interpreter\n", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = /usr/local/bin\nversion_info = 3.12.13.final.0\n", encoding="utf-8")
    (venv / "marker").write_text("old\n", encoding="utf-8")
    (venv / ".deps_installed").write_text("", encoding="utf-8")
    stage = app / "hermes-agent-src"
    stage.mkdir()
    (stage / "marker").write_text("old\n", encoding="utf-8")
    pyproject = root / "agent" / "pyproject.toml"
    pyproject.parent.mkdir()
    pyproject.write_text(AGENT_PYPROJECT, encoding="utf-8")
    return {
        "app": app,
        "venv": venv,
        "venv_prev": app / "venv.previous",
        "stage": stage,
        "stage_prev": app / "hermes-agent-src.previous",
        "journal": app / ".agent-venv-rebuild",
        "pyproject": pyproject,
    }


def _run(tmp_path: Path, paths: dict[str, Path], call: str, **fake: str) -> tuple[str, str]:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    uv = bindir / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    log = tmp_path / "uv.log"
    script = "\n".join(
        [
            "set -e",
            f'_agent_venv="{paths["venv"]}"',
            f'_agent_venv_prev="{paths["venv_prev"]}"',
            f'_agent_stage="{paths["stage"]}"',
            f'_agent_stage_prev="{paths["stage_prev"]}"',
            f'_agent_venv_journal="{paths["journal"]}"',
            f'_venv_helper="{HELPER}"',
            STUBS,
            *(_bash_function(INIT_SH, name) for name in FUNCTIONS),
            call,
        ]
    )
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_UV_LOG": str(log),
        "HOME": str(tmp_path),
        **fake,
    }
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout, log.read_text(encoding="utf-8") if log.exists() else ""


def _rebuild(tmp_path, paths, python="3.14", **fake):
    call = f'rc=0; rebuild_agent_venv "{python}" "{paths["pyproject"]}" || rc=$?; echo "rc=$rc"'
    out, log = _run(tmp_path, paths, call, **fake)
    return int(out.strip().rsplit("rc=", 1)[1]), out, log


def _assert_old_venv_intact(paths):
    venv = paths["venv"]
    assert (venv / "marker").read_text(encoding="utf-8") == "old\n"
    assert (venv / "bin" / "python3").read_text(encoding="utf-8") == "old interpreter\n"
    assert (venv / ".deps_installed").exists(), "the restored venv keeps its fast-restart marker"
    assert not (venv / "partial").exists(), "nothing from the failed attempt may remain"
    assert (paths["stage"] / "marker").read_text(encoding="utf-8") == "old\n", "the old staged source came back"
    assert not paths["venv_prev"].exists() and not paths["stage_prev"].exists()
    assert not paths["journal"].exists()


def test_a_completed_rebuild_replaces_the_venv_and_removes_the_old_copies(tmp_path):
    paths = _layout(tmp_path)
    rc, _, log = _rebuild(tmp_path, paths)
    assert rc == 0
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "new\n"
    assert (paths["venv"] / ".deps_installed").exists()
    assert (paths["stage"] / "marker").read_text(encoding="utf-8") == "new\n"
    assert not paths["venv_prev"].exists() and not paths["stage_prev"].exists()
    assert not paths["journal"].exists()
    assert log.splitlines() == ["3.14"], "the rebuild must name the selected interpreter"


def test_a_rebuild_without_a_named_interpreter_uses_uvs_default(tmp_path):
    paths = _layout(tmp_path)
    rc, _, log = _rebuild(tmp_path, paths, python="-")
    assert rc == 0
    assert log.splitlines() == ["-"], "UV_PYTHON must not be set when no interpreter is selected"


@pytest.mark.parametrize(
    "fake",
    [
        {"FAKE_UV_FAIL": "1"},
        {"FAKE_DEPS_FAIL": "1"},
        {"FAKE_HINDSIGHT_FAIL": "1"},
        {"FAKE_UV_VERSION": "3.13.4"},
        {"FAKE_STAGE_READONLY_FAIL": "1"},
    ],
    ids=[
        "uv-venv-fails",
        "dependency-install-fails",
        "hindsight-install-fails",
        "uv-picks-an-inadmissible-python",
        "read-only-half-copied-stage",
    ],
)
def test_any_failure_before_the_commit_restores_the_working_venv(tmp_path, fake):
    """The #8360 gate's offline case: `uv venv` succeeds, the dependency install does not."""
    paths = _layout(tmp_path)
    rc, out, _ = _rebuild(tmp_path, paths, **fake)
    assert rc == 1, out
    _assert_old_venv_intact(paths)


def test_a_venv_that_cannot_be_moved_aside_is_left_untouched(tmp_path):
    paths = _layout(tmp_path)
    paths["venv_prev"].mkdir()
    (paths["venv_prev"] / "keep").write_text("leftover\n", encoding="utf-8")
    rc, out, log = _rebuild(tmp_path, paths)
    assert rc == 1, out
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "old\n"
    assert (paths["venv_prev"] / "keep").exists(), "a leftover copy is never deleted to make room"
    assert log == "", "uv must not run when the venv could not be moved aside"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_failed_rename_never_deletes_the_venv(tmp_path):
    paths = _layout(tmp_path)
    paths["app"].chmod(0o555)  # rename(2) inside /app now fails, like a mount point
    try:
        rc, out, log = _rebuild(tmp_path, paths)
    finally:
        paths["app"].chmod(0o755)
    assert rc == 1, out
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "old\n"
    assert (paths["venv"] / ".deps_installed").exists()
    assert log == ""


def test_an_interrupted_rebuild_is_rolled_back_on_the_next_boot(tmp_path):
    paths = _layout(tmp_path)
    paths["venv"].rename(paths["venv_prev"])
    paths["stage"].rename(paths["stage_prev"])
    (paths["venv"] / "bin").mkdir(parents=True)
    (paths["venv"] / ".deps_installed").write_text("", encoding="utf-8")  # installed but never committed
    paths["stage"].mkdir()
    paths["journal"].write_text("pending\n", encoding="utf-8")
    out, _ = _run(tmp_path, paths, "recover_interrupted_agent_venv_rebuild")
    assert "Restoring" in out
    _assert_old_venv_intact(paths)


def test_a_committed_rebuild_is_finished_on_the_next_boot(tmp_path):
    paths = _layout(tmp_path)
    paths["venv"].rename(paths["venv_prev"])
    paths["stage"].rename(paths["stage_prev"])
    (paths["venv"] / "bin").mkdir(parents=True)
    (paths["venv"] / "marker").write_text("new\n", encoding="utf-8")
    paths["stage"].mkdir()
    paths["journal"].write_text("committed\n", encoding="utf-8")
    out, _ = _run(tmp_path, paths, "recover_interrupted_agent_venv_rebuild")
    assert "Finishing" in out
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "new\n"
    assert not paths["venv_prev"].exists() and not paths["stage_prev"].exists()
    assert not paths["journal"].exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_leftover_this_script_did_not_create_is_never_trusted(tmp_path):
    """The #8360 fold-check BRICK: a permission-blocked stale backup with no journal.

    Recovery used to treat any .previous directory as an interrupted rebuild,
    delete the working venv and move the unusable copy into its place.
    """
    paths = _layout(tmp_path)
    stale = paths["venv_prev"] / "locked"
    stale.mkdir(parents=True)
    (stale / "file").write_text("x\n", encoding="utf-8")
    stale.chmod(0o500)
    try:
        out, _ = _run(tmp_path, paths, "recover_interrupted_agent_venv_rebuild")
        assert "no rebuild of this script left it" in out
        rc, out2, log = _rebuild(tmp_path, paths)
    finally:
        stale.chmod(0o700)
    assert rc == 1, out2
    assert log == "", "no rebuild may start next to an unrecognised leftover"
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "old\n"
    assert (paths["venv"] / ".deps_installed").exists()
    assert (stale / "file").exists(), "a leftover this script did not create is never deleted"


def test_a_pending_journal_without_a_moved_venv_leaves_the_venv_alone(tmp_path):
    """Stopped after the journal was written but before anything moved."""
    paths = _layout(tmp_path)
    paths["journal"].write_text("pending\n", encoding="utf-8")
    _run(tmp_path, paths, "recover_interrupted_agent_venv_rebuild")
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "old\n"
    assert (paths["stage"] / "marker").read_text(encoding="utf-8") == "old\n"
    assert not paths["journal"].exists()


def test_a_pending_rebuild_never_restores_an_unusable_copy(tmp_path):
    paths = _layout(tmp_path)
    paths["venv_prev"].mkdir()
    (paths["venv_prev"] / "junk").write_text("x\n", encoding="utf-8")
    paths["journal"].write_text("pending\n", encoding="utf-8")
    out, _ = _run(tmp_path, paths, "recover_interrupted_agent_venv_rebuild")
    assert "not usable" in out
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "old\n"


def test_recovery_is_a_no_op_without_leftovers(tmp_path):
    paths = _layout(tmp_path)
    out, _ = _run(tmp_path, paths, "recover_interrupted_agent_venv_rebuild")
    assert out == ""
    assert (paths["venv"] / "marker").read_text(encoding="utf-8") == "old\n"


def test_docker_init_wires_the_transaction():
    recover_call = INIT_SH.index("\nrecover_interrupted_agent_venv_rebuild\n")
    plan = INIT_SH.index('_venv_plan="$(python3 /app/scripts/docker_venv_python.py')
    branch_start = INIT_SH.index('if [ "$_venv_action" = "rebuild" ]; then')
    branch = INIT_SH[branch_start : INIT_SH.index("\nfi\n", branch_start)]
    guard = INIT_SH.index("if [ -f /app/venv/.deps_installed ]; then")
    assert INIT_SH.index("rebuild_agent_venv() {") < recover_call < plan < branch_start < guard
    assert 'rebuild_agent_venv "$_venv_python"' in branch
    assert "rm -rf /app/venv" not in branch, "the rebuild branch must never delete the venv itself"
    assert "error_exit" in branch, "a venv that could not be put back must still stop startup"
    assert '_venv_action="reuse"' in branch, "a rolled-back rebuild boots on the old venv"
    # The dependency install inside the transaction cannot exit the script.
    install = _bash_function(INIT_SH, "install_venv_dependencies")
    assert "error_exit" not in install
    assert 'install_venv_dependencies || error_exit "Failed to install hermes-webui dependencies"' in INIT_SH
    rebuild = _bash_function(INIT_SH, "rebuild_agent_venv")
    assert "ensure_hindsight_client_docker_dependency soft" in rebuild
    assert "--admits" in rebuild
    assert rebuild.index("write_agent_venv_journal pending") < rebuild.index('mv -T "$_agent_venv" "$_agent_venv_prev"'), (
        "the journal must exist before anything is moved"
    )
