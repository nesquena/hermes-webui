"""Regression coverage for #8106: first-start ownership walks racing atomic renames."""

import os
from pathlib import Path
import re
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
INIT_SCRIPT = (REPO / "docker_init.bash").read_text(encoding="utf-8")


def _root_init_section() -> str:
    start = INIT_SCRIPT.index('if [ "A${whoami}" == "Aroot" ]; then')
    end = INIT_SCRIPT.index("exec su", start)
    return INIT_SCRIPT[start:end]


def _chown_helper_script() -> str:
    """The inline `sh -c` body that chown_home_hermeswebui runs per batch."""
    fn_start = INIT_SCRIPT.index("chown_home_hermeswebui()")
    fn_end = INIT_SCRIPT.index("\n}\n", fn_start)
    match = re.search(r"-exec sh -c '(.*?)' chown_home_hermeswebui", INIT_SCRIPT[fn_start:fn_end], re.S)
    assert match, "chown_home_hermeswebui must run chown through the inline sh helper"
    return match.group(1)


def test_usermod_skips_its_implicit_home_walk():
    """usermod -u walks the home when it is owned by the old or new UID and
    aborts on the first vanished entry. The UID change must point the home at
    a nonexistent path for that call and restore the real home right after."""
    root = _root_init_section()
    assert 'usermod -o -u "${WANTED_UID}" -d "$_usermod_nohome" hermeswebui' in root
    assert '[ ! -e "$_usermod_nohome" ]' in root
    assert root.index('usermod -o -u "${WANTED_UID}"') < root.index(
        "usermod -d /home/hermeswebui hermeswebui"
    ) < root.index("chown_home_hermeswebui || error_exit")
    assert 'usermod -o -u "${WANTED_UID}" hermeswebui' not in root


def test_explicit_walk_ignores_entries_vanishing_between_readdir_and_stat():
    fn_start = INIT_SCRIPT.index("chown_home_hermeswebui()")
    fn_end = INIT_SCRIPT.index("\n}\n", fn_start)
    assert "find /home/hermeswebui -ignore_readdir_race" in INIT_SCRIPT[fn_start:fn_end]


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_chown_helper_tolerates_only_vanished_entries(tmp_path):
    helper = _chown_helper_script()
    own = f"{os.getuid()}:{os.getgid()}"
    present = tmp_path / "present"
    present.write_text("x", encoding="utf-8")
    vanished = tmp_path / "vanished"

    ok = subprocess.run(
        ["sh", "-c", helper, "chown_home_hermeswebui", own, str(present), str(vanished)],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr

    # A non-root user cannot give a file away: EPERM on an existing entry must
    # still fail the walk instead of being mistaken for a vanished file.
    denied = subprocess.run(
        ["sh", "-c", helper, "chown_home_hermeswebui", "0:0", str(present), str(vanished)],
        capture_output=True,
        text=True,
    )
    assert denied.returncode != 0
    assert str(present) in denied.stderr

    # A path that cannot be examined for a reason other than ENOENT (here
    # ENOTDIR) is not a vanished file either, even though `[ -e ]` is false.
    not_a_dir = present / "child"
    unreadable = subprocess.run(
        ["sh", "-c", helper, "chown_home_hermeswebui", own, str(not_a_dir)],
        capture_output=True,
        text=True,
    )
    assert unreadable.returncode != 0
    assert "Not a directory" in unreadable.stderr


def test_docker_smoke_runs_first_start_race_proof():
    workflow = (REPO / ".github" / "workflows" / "docker-smoke.yml").read_text(encoding="utf-8")
    assert "scripts/docker_first_start_race.sh ghcr.io/nesquena/hermes-webui:latest" in workflow
    script = REPO / "scripts" / "docker_first_start_race.sh"
    assert os.access(script, os.X_OK)
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
