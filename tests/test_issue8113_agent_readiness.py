"""#8113 acceptance coverage the interpreter fix alone does not provide.

The maintainer authorised a repair covering four areas: fresh *and reused*
incompatible venvs, interpreter selection, critical-import readiness, and
read-only staged-source handling. ``test_issue8113_docker_agent_venv_python.py``
covers the first two; this file covers the last two.

*Critical-import readiness* — the container used to start healthy and serve
HTTPS while ``run_agent`` was unimportable, so the only signal was a cosmetic
looking warning. Startup must now verify the critical import against the
interpreter actually in the venv and say so loudly when it fails.

*Read-only staged-source handling* — ``auto_install_agent_deps()`` targeted the
read-only agent mount (``_agent_dir()``), so under the documented ``:ro``
compose mount it died with ``Read-only file system`` and could never recover
the very breakage #8113 reports.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO / "scripts" / "docker_venv_python.py"

_spec = importlib.util.spec_from_file_location("docker_venv_python_readiness", HELPER_PATH)
dvp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dvp)

INIT_SH = (REPO / "docker_init.bash").read_text(encoding="utf-8")


# ── critical-import readiness ────────────────────────────────────────────────


def _fake_venv(tmp_path: Path, *, has_dotenv: bool) -> Path:
    """A venv-ish tree whose probe reports whether ``dotenv`` imports.

    ``dotenv`` is the dependency #8113 dropped first: ``run_agent`` ->
    ``hermes_cli.env_loader`` -> ``import dotenv`` at module scope, so its
    absence is the earliest observable symptom of the wrong interpreter.

    ``bin/python3`` is a real interpreter so the probe genuinely executes.

    The ``pyvenv.cfg`` must be *complete* or the fixture is not hermetic. A
    config without ``home`` leaves CPython unable to locate a base
    installation, so it falls back to the interpreter's own ``base_prefix``
    and keeps user site-packages enabled — the fake venv then inherits
    whatever the *host* happens to have installed. CI runners install
    ``python-dotenv``, so a ``has_dotenv=False`` venv would still import it,
    the dropped-dependency case would silently stop reproducing, and
    ``test_cli_verify_exits_nonzero_when_the_import_is_dropped`` would fail in
    CI while passing locally. ``home`` plus
    ``include-system-site-packages = false`` isolates the fixture; the module
    then goes in the venv's own ``site-packages``, which is where pip puts it.
    """
    major, minor = sys.version_info[:2]
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / f"python{major}.{minor}" / "site-packages"
    site_packages.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\n"
        "include-system-site-packages = false\n"
        f"version = {major}.{minor}.{sys.version_info.micro}\n"
        f"version_info = {major}.{minor}\n",
        encoding="utf-8",
    )
    (venv / "bin" / "python3").symlink_to(sys.executable)
    if has_dotenv:
        (site_packages / "dotenv.py").write_text("", encoding="utf-8")
    return venv


def test_fake_venv_is_hermetic_and_sees_no_ambient_packages(tmp_path):
    """Guard the fixture itself.

    If the fake venv can reach the host interpreter's site-packages, the
    dropped-dependency case stops reproducing and the tests below pass for the
    wrong reason. Assert the isolation directly rather than trusting it.
    """
    venv = _fake_venv(tmp_path, has_dotenv=False)
    result = subprocess.run(
        [str(venv / "bin" / "python3"), "-c",
         "import json,site,sys;"
         "print(json.dumps({'prefix': sys.prefix,"
         "'user_site': site.ENABLE_USER_SITE,"
         "'paths': sys.path}))"],
        cwd=str(venv),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    info = json.loads(result.stdout)
    assert info["prefix"] == str(venv), f"fixture did not become the prefix: {info}"
    assert info["user_site"] is False, f"user site-packages leaked in: {info}"
    foreign = [
        p for p in info["paths"]
        if "packages" in p and not p.startswith(str(venv))
    ]
    assert not foreign, f"ambient site-packages leaked into the fixture: {foreign}"


def test_readiness_passes_when_the_gated_dependency_imports(tmp_path):
    venv = _fake_venv(tmp_path, has_dotenv=True)
    ok, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    assert ok is True
    assert detail == ""


def test_readiness_fails_and_names_the_missing_module(tmp_path):
    """The report must name the missing module, not just say "not ready"."""
    venv = _fake_venv(tmp_path, has_dotenv=False)
    ok, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    assert ok is False
    assert "dotenv" in detail


def test_readiness_reports_the_interpreter_it_actually_used(tmp_path):
    """The whole fault is an interpreter mismatch — the report must show it.

    Asserted against the running interpreter rather than a literal ``3.12``:
    CI fans out 3.11/3.12/3.13 shards, and this fixture is a real interpreter,
    so a hardcoded version would fail on every shard but one.
    """
    venv = _fake_venv(tmp_path, has_dotenv=False)
    _, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    major, minor = sys.version_info[:2]
    assert f"{major}.{minor}" in detail
    assert "3.14" in detail


def test_readiness_treats_a_missing_venv_as_not_ready(tmp_path):
    ok, detail = dvp.check_critical_imports(tmp_path / "absent", expected=(3, 14))
    assert ok is False
    assert detail != ""


def test_readiness_never_raises_on_a_broken_interpreter(tmp_path):
    """A venv with no runnable python must report, not crash the container."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version_info = 3.12.15\n", encoding="utf-8")
    ok, detail = dvp.check_critical_imports(venv, expected=(3, 14))
    assert ok is False
    assert isinstance(detail, str) and detail


def test_cli_verdict_is_printed_for_the_shell(tmp_path):
    """`docker_init.bash` gates on the exit status, so it must be real."""
    venv = _fake_venv(tmp_path, has_dotenv=True)
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), "--verify", str(venv), "3.14"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_cli_verify_exits_nonzero_when_the_import_is_dropped(tmp_path):
    """The shell branch keys on this status; a green exit would hide the fault."""
    venv = _fake_venv(tmp_path, has_dotenv=False)
    result = subprocess.run(
        [sys.executable, str(HELPER_PATH), "--verify", str(venv), "3.14"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "dotenv" in result.stdout + result.stderr


# ── docker_init.bash must run the readiness check ────────────────────────────


def test_docker_init_verifies_the_critical_import_after_install():
    assert "--verify" in INIT_SH, "startup must verify the agent's critical import"


def test_docker_init_readiness_check_runs_after_the_venv_is_active():
    """Checking before activation would probe the base interpreter, not the venv."""
    activate_pos = INIT_SH.find("source /app/venv/bin/activate")
    verify_pos = INIT_SH.find("--verify")
    assert activate_pos != -1 and verify_pos != -1
    assert verify_pos > activate_pos, "readiness must be checked with the venv active"


def test_docker_init_readiness_runs_after_the_dependency_install():
    """Checking before the install would report the pre-install state."""
    install_pos = INIT_SH.find("uv pip install -e")
    verify_pos = INIT_SH.find("--verify")
    assert install_pos != -1, "agent editable install not found"
    assert verify_pos > install_pos, "readiness must be checked after the install"


# ── read-only staged-source handling ─────────────────────────────────────────

# Written by `write_agent_stage_manifest` in docker_init.bash next to the
# staged copy; `_agent_install_dir()` reads it to bind the stage to the Agent.
STAGE_MANIFEST = ".hermes-webui-stage-source"
STAGE_ENV = "HERMES_WEBUI_AGENT_STAGE_DIR"


@pytest.fixture(params=[0o022, 0o002], ids=["umask022", "umask002"])
def ambient_umask(request):
    """Run a positive fixture under both common umasks.

    CI and most hosts use 0022, but 0002 (per-user groups) is common too and
    makes a plain ``mkdir`` 0775 — which the real trust check correctly rejects
    as group-writable. Positive fixtures must not depend on which one is set.
    """
    previous = os.umask(request.param)
    try:
        yield request.param
    finally:
        os.umask(previous)


def _agent(tmp_path: Path, name: str, *, reqs: bool = True, mode: int = 0o755) -> Path:
    """An Agent-shaped source directory with an explicit, trusted mode.

    The install input keeps whatever mode the ambient umask produced — under
    umask 002 that is 0664, the shape a `git pull` under umask 002 leaves in a
    real checkout. The producer (docker_init.bash / ``_staged_copy``) is what
    must make the *staged* copy private (#8472); pinning the source input to
    0644 here would hide that producer-to-installer path.
    """
    d = tmp_path / name
    d.mkdir()
    d.chmod(mode)
    if reqs:
        input_file = d / "requirements.txt"
        input_file.write_text("python-dotenv==1.2.2\n", encoding="utf-8")
    else:
        input_file = d / "pyproject.toml"
        input_file.write_text('[project]\nname="hermes-agent"\n', encoding="utf-8")
    return d


def _publish_stage(source: Path, stage: Path) -> None:
    """The manifest docker_init.bash publishes for a stage copied from ``source``."""
    lines = [f"source={source.resolve()}"]
    for name in ("pyproject.toml", "requirements.txt"):
        path = source / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "-"
        lines.append(f"{name}={digest}")
    manifest = stage / STAGE_MANIFEST
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest.chmod(0o644)


def _staged_copy(tmp_path: Path, source: Path, *, name: str = "hermes-agent-src", mode: int = 0o755) -> Path:
    """A staged copy of ``source`` as docker_init.bash leaves it.

    ``shutil.copytree`` preserves mode bits the way ``rsync -a`` / ``cp -a``
    do; the producer then strips group/world write from the install inputs
    (``secure_stage_install_inputs`` in docker_init.bash) before publishing
    the manifest, so a 0664 source input lands in the stage as 0644 (#8472).
    """
    stage = tmp_path / name
    shutil.copytree(source, stage)
    stage.chmod(mode)
    for _f in ("pyproject.toml", "requirements.txt", "setup.py"):
        p = stage / _f
        if p.is_file():
            p.chmod(stat.S_IMODE(p.stat().st_mode) & ~0o022)
    _publish_stage(source, stage)
    return stage


@pytest.mark.parametrize("use_alias", [False, True], ids=["same-path", "symlink"])
def test_auto_install_same_stage_as_agent_has_no_warning(tmp_path, ambient_umask, capsys, use_alias):
    """#8555: Docker already selected the stage, whose manifest names the mount."""
    from api.startup import auto_install_agent_deps

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    stage_path = staged
    if use_alias:
        stage_path = tmp_path / "stage-link"
        stage_path.symlink_to(staged, target_is_directory=True)
    env = {
        "HERMES_WEBUI_AGENT_DIR": str(staged),
        STAGE_ENV: str(stage_path),
        "HERMES_WEBUI_AUTO_INSTALL": "1",
    }
    with patch.dict("os.environ", env, clear=False), patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stderr="")
        assert auto_install_agent_deps(expected_agent_dir=staged) is True
    assert str(staged / "requirements.txt") in run.call_args[0][0]
    assert "Ignoring staged agent copy" not in capsys.readouterr().out


def test_auto_install_targets_the_writable_staged_copy(tmp_path, ambient_umask):
    """The `:ro` mount can never satisfy the install that would fix #8113."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == staged.resolve()


def test_auto_install_still_rejects_a_group_writable_stage(tmp_path):
    """Explicit fixture modes must not paper over the production trust check."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount, mode=0o775)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_rejects_an_unrelated_trusted_stage(tmp_path):
    """Owner and mode prove the stage is ours, not that it is a copy of *this* Agent."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    unrelated = _agent(tmp_path, "unrelated-src")
    (unrelated / "requirements.txt").write_text("unrelated-pkg==6.6.6\n", encoding="utf-8")
    with patch.dict("os.environ", {STAGE_ENV: str(unrelated)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_rejects_a_stage_published_for_a_different_agent(tmp_path):
    """Identical install inputs are not identity: the stage must name this Agent."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    other = _agent(tmp_path, "other-agent")
    staged = _staged_copy(tmp_path, other)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_rejects_a_stale_stage(tmp_path):
    """The Agent moved on after staging (upgrade / host checkout pull)."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    (mount / "requirements.txt").write_text("python-dotenv==1.3.0\n", encoding="utf-8")
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_rejects_a_stage_changed_after_publication(tmp_path):
    """The manifest vouches for what was copied, not for whatever is there now."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    (staged / "requirements.txt").write_text("unrelated-pkg==6.6.6\n", encoding="utf-8")
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_falls_back_to_the_mount_without_a_stage_dir(tmp_path):
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    with patch.dict("os.environ", {"HERMES_WEBUI_AGENT_STAGE_DIR": ""}, clear=False):
        assert _agent_install_dir(mount) == mount


def test_auto_install_never_returns_a_path_swapped_in_after_validation(tmp_path, ambient_umask):
    """The path that was checked is the path that is returned.

    Resolving the env value only after the trust and manifest checks let a
    symlink swapped in between redirect the install to an unchecked,
    world-writable directory.
    """
    import api.startup as startup

    mount = _agent(tmp_path, "hermes-agent")
    good = _staged_copy(tmp_path, mount)
    evil = tmp_path / "evil"
    evil.mkdir()
    evil.chmod(0o777)
    link = tmp_path / "stage-link"
    link.symlink_to(good)
    real_matches = startup._stage_matches_agent

    def swap_after_validation(staged, agent_dir):
        verdict = real_matches(staged, agent_dir)
        link.unlink()
        link.symlink_to(evil)  # the attacker's swap, deterministically mid-call
        return verdict

    with patch.dict("os.environ", {STAGE_ENV: str(link)}, clear=False), \
            patch.object(startup, "_stage_matches_agent", swap_after_validation):
        chosen = startup._agent_install_dir(mount.resolve())
    assert chosen == good.resolve()
    assert chosen != evil.resolve()


@pytest.mark.parametrize("value", ["~hermes-no-such-user-8360", "~hermes-no-such-user-8360/stage"])
def test_auto_install_survives_an_unexpandable_stage_value(tmp_path, value):
    """`~unknown-user` raises in expanduser(); startup must fall back, not abort."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    with patch.dict("os.environ", {STAGE_ENV: value}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership and modes")
def test_auto_install_rejects_a_stage_under_a_replaceable_parent(tmp_path, ambient_umask):
    """A writable, non-sticky parent lets anyone swap the validated stage out."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    parent = tmp_path / "shared"
    parent.mkdir()
    staged = _staged_copy(parent, mount)
    parent.chmod(0o777)
    try:
        with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
            assert _agent_install_dir(mount.resolve()) == mount.resolve()
    finally:
        parent.chmod(0o755)


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership and modes")
def test_auto_install_accepts_a_stage_under_a_sticky_parent(tmp_path, ambient_umask):
    """The image's /app is 1777: sticky, so only the stage's owner can replace it."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    parent = tmp_path / "app"
    parent.mkdir()
    staged = _staged_copy(parent, mount)
    parent.chmod(0o1777)
    try:
        with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
            assert _agent_install_dir(mount.resolve()) == staged.resolve()
    finally:
        parent.chmod(0o755)


def test_auto_install_ignores_a_staged_path_that_does_not_exist(tmp_path):
    """A stale env var must not redirect the install into nowhere."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    env = {"HERMES_WEBUI_AGENT_STAGE_DIR": str(tmp_path / "gone")}
    with patch.dict("os.environ", env, clear=False):
        assert _agent_install_dir(mount) == mount


def test_auto_install_ignores_a_staged_path_that_fails_the_trust_check(tmp_path):
    """Fail closed: an untrusted staged path must not become the install target."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _agent(tmp_path, "hermes-agent-src")
    env = {"HERMES_WEBUI_AGENT_STAGE_DIR": str(staged)}
    with patch.dict("os.environ", env, clear=False):
        with patch("api.startup._trusted_agent_dir", return_value=False):
            assert _agent_install_dir(mount) == mount


def test_auto_install_runs_pip_against_the_staged_copy(tmp_path):
    """End-to-end through auto_install_agent_deps, not just the helper."""
    from api.startup import auto_install_agent_deps

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    env = {
        "HERMES_WEBUI_AGENT_DIR": str(mount),
        "HERMES_WEBUI_AGENT_STAGE_DIR": str(staged),
        "HERMES_WEBUI_AUTO_INSTALL": "1",
    }
    with patch.dict("os.environ", env, clear=False):
        with patch("api.startup._trusted_agent_dir", return_value=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="")
                assert auto_install_agent_deps(expected_agent_dir=mount) is True
    args = mock_run.call_args[0][0]
    assert any(str(staged) in str(a) for a in args), f"install did not target the staged copy: {args}"


def test_auto_install_installs_the_authorized_agent_not_an_unrelated_stage(tmp_path):
    """Two owned 0755 sources, each with its own requirements: install the authorized one.

    Uses the real trust check (no patch): the unrelated directory passes it, so
    only the stage binding can keep its requirements away from pip.
    """
    from api.startup import auto_install_agent_deps

    mount = _agent(tmp_path, "hermes-agent")
    unrelated = _agent(tmp_path, "unrelated-src")
    (unrelated / "requirements.txt").write_text("unrelated-pkg==6.6.6\n", encoding="utf-8")
    env = {
        "HERMES_WEBUI_AGENT_DIR": str(mount),
        "HERMES_WEBUI_AGENT_STAGE_DIR": str(unrelated),
        "HERMES_WEBUI_AUTO_INSTALL": "1",
    }
    with patch.dict("os.environ", env, clear=False):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            assert auto_install_agent_deps(expected_agent_dir=mount) is True
    args = [str(a) for a in mock_run.call_args[0][0]]
    assert str(mount.resolve() / "requirements.txt") in args, args
    assert not any(str(unrelated) in a for a in args), f"install used the unrelated stage: {args}"


def _bash_function(src: str, name: str) -> str:
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + len("\n}\n")
    return src[start:end]


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sha256sum") is None,
    reason="bash and sha256sum are needed to run the docker_init.bash publisher",
)
def test_docker_init_stage_manifest_is_accepted_by_the_recovery_path(tmp_path):
    """The real bash producer (securer + publisher) and the Python reader must agree.

    Runs `secure_stage_install_inputs` and `write_agent_stage_manifest` from
    docker_init.bash on a staged copy, then checks that `_agent_install_dir()`
    accepts it — and stops accepting it once the Agent's install inputs change
    underneath the stage.
    """
    from api.startup import _agent_install_dir

    producer = _bash_function(INIT_SH, "secure_stage_install_inputs") + \
        _bash_function(INIT_SH, "write_agent_stage_manifest")
    mount = _agent(tmp_path, "hermes-agent", reqs=False)
    staged = tmp_path / "hermes-agent-src"
    shutil.copytree(mount, staged)
    staged.chmod(0o755)
    subprocess.run(
        ["bash", "-c", producer + '\nsecure_stage_install_inputs "$2"\nwrite_agent_stage_manifest "$1" "$2"',
         "bash", str(mount), str(staged)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )

    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == staged.resolve()
        (mount / "pyproject.toml").write_text(
            '[project]\nname="hermes-agent"\nversion="0.22.0"\n', encoding="utf-8"
        )
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_docker_init_publishes_the_stage_manifest_between_copy_and_install():
    """The binding must describe the finished copy, before anything installs from it."""
    call = 'write_agent_stage_manifest "$_agent_src" "$_stage_src"'
    copy_pos = INIT_SH.find('chmod -R u+w "$_stage_src"')
    call_pos = INIT_SH.find(call)
    install_pos = INIT_SH.find('install_staged_agent "$_stage_src"')  # the install (#8524)
    assert call_pos != -1, "docker_init.bash must publish the stage manifest"
    assert copy_pos != -1 and install_pos != -1
    assert copy_pos < call_pos < install_pos


def test_docker_init_exports_the_staged_dir_for_the_recovery_path():
    assert "HERMES_WEBUI_AGENT_STAGE_DIR" in INIT_SH


def test_docker_init_exports_the_stage_dir_before_the_server_starts():
    """`docker_init.bash` exports it; `server.py` consumes it on failure."""
    export_pos = INIT_SH.find("HERMES_WEBUI_AGENT_STAGE_DIR")
    server_pos = INIT_SH.find("python server.py")
    assert export_pos != -1, "staged dir must be exported to the recovery path"
    assert server_pos != -1
    assert export_pos < server_pos


# ── install-input hygiene (#8472) ─────────────────────────────────────────────

# The trust checks above prove the stage *directory* is ours, but say nothing
# about the input *files* inside it. A world-writable requirements.txt in a
# valid stage passes validation, then its contents can change before pip reads
# it. Each install input must be a regular file (not a symlink) owned by the
# current user and not group- or world-writable.


def test_auto_install_rejects_a_stage_with_a_world_writable_requirements_file(tmp_path):
    """The manifest digest is of contents, not permissions; mode must be checked."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    (staged / "requirements.txt").chmod(0o666)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_rejects_a_stage_with_a_group_writable_requirements_file(tmp_path):
    """Group-writable is as dangerous as world-writable: any group member can edit."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    (staged / "requirements.txt").chmod(0o664)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_rejects_a_stage_with_a_world_writable_pyproject(tmp_path):
    """Same bar for the pyproject.toml install path."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent", reqs=False)
    staged = _staged_copy(tmp_path, mount)
    (staged / "pyproject.toml").chmod(0o666)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_rejects_a_stage_with_a_symlinked_requirements_file(tmp_path):
    """A symlink can be repointed after validation; only a real file is trusted.

    The digest check alone is not enough here the link's target is the very
    file whose digest the manifest recorded, so contents match today.
    """
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    req = staged / "requirements.txt"
    req.unlink()
    req.symlink_to(mount / "requirements.txt")  # same content, via a link
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership and modes")
def test_auto_install_rejects_a_stage_with_input_owned_by_another_user(tmp_path):
    """Not our file: its contents can be changed by whoever owns it (#8472)."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    other_uid = 0 if os.getuid() != 0 else 65534  # root has no "other user" above it
    try:
        os.chown(staged / "requirements.txt", other_uid, -1)
    except OSError:
        # PermissionError, and EINVAL from a sandbox without CAP_CHOWN.
        pytest.skip("cannot chown in this environment; ownership check not exercised")
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_accepts_a_stage_with_trusted_private_input_files(tmp_path, ambient_umask):
    """Happy path: an owned regular file, mode 0644 or stricter, is accepted."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    req = staged / "requirements.txt"
    assert req.is_file() and not req.is_symlink()
    req.chmod(0o600)  # stricter than 0644, still fine
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == staged.resolve()


def test_auto_install_installs_the_authorized_agent_when_a_stage_input_is_unsafe(tmp_path):
    """Consumer level: an unsafe input redirects the install, it does not cancel it.

    ``_agent_install_dir`` falls back to the authorized Agent directory when a
    stage fails validation, so ``auto_install_agent_deps`` still runs pip —
    from the Agent. The pip arguments, not the absence of a call, are what
    prove the unsafe stage stayed out of the install (#8472). The real trust
    checks run here: the world-writable input is the only thing wrong with the
    stage, so only the file-level check can reject it.
    """
    from api.startup import auto_install_agent_deps

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    (staged / "requirements.txt").chmod(0o666)
    env = {
        "HERMES_WEBUI_AGENT_DIR": str(mount),
        "HERMES_WEBUI_AGENT_STAGE_DIR": str(staged),
        "HERMES_WEBUI_AUTO_INSTALL": "1",
    }
    with patch.dict("os.environ", env, clear=False):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            assert auto_install_agent_deps(expected_agent_dir=mount) is True
    assert mock_run.call_count == 1, "rejecting the stage must not cancel the install"
    args = [str(a) for a in mock_run.call_args[0][0]]
    assert str(mount.resolve() / "requirements.txt") in args, args
    assert not any(str(staged) in a for a in args), f"install used the unsafe stage: {args}"


def test_auto_install_rejects_a_stage_when_an_input_lstat_fails(tmp_path, monkeypatch):
    """Fail closed when an input cannot even be stat'ed.

    ``FileNotFoundError`` means pip never reads the input and is skipped; any
    other ``OSError`` (EACCES, a vanishing mount, …) must reject the stage
    rather than wave the input through unvalidated.
    """
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    real_lstat = Path.lstat

    def lstat_that_fails(self):
        if self.name in ("requirements.txt", "pyproject.toml"):
            raise PermissionError("simulated lstat failure")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat_that_fails)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership and modes")
def test_trusted_install_inputs_rejects_inputs_the_current_user_does_not_own(tmp_path, monkeypatch):
    """The ownership branch must be covered where a real chown is not permitted.

    The selector-level case above performs a real ``os.chown`` and skips on an
    unprivileged runner (as this one does), leaving the branch unexercised;
    faking ``getuid`` covers it everywhere. The helper is called directly so
    the fake stays away from ``_trusted_ancestry``, which reads ``getuid`` too.
    """
    from api.startup import _trusted_install_inputs

    staged = _agent(tmp_path, "hermes-agent")
    (staged / "requirements.txt").chmod(0o644)
    assert _trusted_install_inputs(staged) is True  # control: ours, regular, private
    real_getuid = os.getuid
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)
    assert _trusted_install_inputs(staged) is False


# ── producer re-secures the staged inputs (#8472) ────────────────────────────


def test_docker_init_recovery_accepts_a_group_writable_source_input(tmp_path):
    """A 0664 source input (a git pull under umask 002) must not break recovery.

    ``rsync -a``/``cp -a`` preserve mode bits, so the plain copied stage of a
    0664 input is itself 0664 and the private-file check rejects it. The
    producer's ``secure_stage_install_inputs`` strips group/world write from
    the staged inputs before the manifest is published, so the recovery path
    accepts the stage instead of falling back to the read-only mount (where
    pip fails with EROFS).
    """
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    (mount / "requirements.txt").chmod(0o664)  # umask-002 checkout shape
    staged = tmp_path / "hermes-agent-src"
    shutil.copytree(mount, staged)
    staged.chmod(0o755)
    producer = _bash_function(INIT_SH, "secure_stage_install_inputs") + \
        _bash_function(INIT_SH, "write_agent_stage_manifest")
    subprocess.run(
        ["bash", "-c", producer + '\nsecure_stage_install_inputs "$2"\nwrite_agent_stage_manifest "$1" "$2"',
         "bash", str(mount), str(staged)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert stat.S_IMODE((staged / "requirements.txt").stat().st_mode) & 0o022 == 0
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == staged.resolve()


def test_docker_init_secures_the_staged_inputs_before_publishing_the_manifest():
    """go-w must land between the copy and the manifest, which vouches for the
    copy and names the exact files the recovery path re-checks."""
    copy_pos = INIT_SH.find('chmod -R u+w "$_stage_src"')
    secure_pos = INIT_SH.find('secure_stage_install_inputs "$_stage_src"')
    call_pos = INIT_SH.find('write_agent_stage_manifest "$_agent_src" "$_stage_src"')
    assert secure_pos != -1, "docker_init.bash must re-secure the staged install inputs"
    assert copy_pos != -1 and call_pos != -1
    assert copy_pos < secure_pos < call_pos


def test_docker_init_resecures_an_existing_stage_on_fast_restart():
    """A stage published before the go-w step stays unsecured across restarts;
    the fast-restart branch must re-secure it or recovery rejects it."""
    assert 'secure_stage_install_inputs "$_agent_stage"' in INIT_SH


def test_auto_install_rejects_a_stage_with_a_world_writable_setup_py(tmp_path):
    """setuptools executes setup.py on the pyproject build path (#8472).

    The real Agent ships pyproject.toml plus setup.py and no requirements.txt,
    so ``pip install <stage>`` runs setup.py — a writable one is code injection.
    """
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent", reqs=False)
    (mount / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    staged = _staged_copy(tmp_path, mount)
    (staged / "setup.py").chmod(0o666)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()


def test_auto_install_accepts_a_stage_with_a_private_setup_py(tmp_path, ambient_umask):
    """Happy path: a private setup.py beside a trusted pyproject.toml is fine."""
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent", reqs=False)
    (mount / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    staged = _staged_copy(tmp_path, mount)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == staged.resolve()


def test_auto_install_names_a_stage_rejected_for_an_unsafe_input(tmp_path, capsys):
    """The fallback to the mount must name the stage and the reason (#8472).

    Rejecting silently leaves startup with an EROFS pip failure and no clue
    which stage was skipped and why.
    """
    from api.startup import _agent_install_dir

    mount = _agent(tmp_path, "hermes-agent")
    staged = _staged_copy(tmp_path, mount)
    (staged / "requirements.txt").chmod(0o666)
    with patch.dict("os.environ", {STAGE_ENV: str(staged)}, clear=False):
        assert _agent_install_dir(mount.resolve()) == mount.resolve()
    out = capsys.readouterr().out
    assert "[!!]" in out
    assert str(staged) in out
    assert "private-regular-file" in out