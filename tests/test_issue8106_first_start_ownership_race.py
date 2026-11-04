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
    """The inline `sh -c` body that the ownership walks run per batch."""
    match = re.search(r"^_chown_tolerating_vanished='(.*?)'$", INIT_SCRIPT, re.S | re.M)
    assert match, "the ownership walks must run chown through the inline sh helper"
    return match.group(1)


def _function(name: str) -> str:
    start = INIT_SCRIPT.index(f"{name}() {{")
    end = INIT_SCRIPT.index("\n}\n", start)
    return INIT_SCRIPT[start : end + 2]


def _run_find_wrapper(tmp_path, stub_body: str):
    """Run find_tolerating_vanished with `find` replaced by a stub."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "find"
    stub.write_text("#!/bin/sh\n" + stub_body + "\n", encoding="utf-8")
    stub.chmod(0o755)
    script = _function("find_tolerating_vanished") + '\nfind_tolerating_vanished /h -print\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )


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


def test_explicit_walks_tolerate_vanished_entries():
    wrapper = _function("find_tolerating_vanished")
    assert 'find "$root" -ignore_readdir_race' in wrapper
    for name in ("chown_home_hermeswebui", "migrate_home_old_uid"):
        body = _function(name)
        assert "find_tolerating_vanished /home/hermeswebui" in body
        assert '-exec sh -c "$_chown_tolerating_vanished"' in body


def test_find_wrapper_accepts_only_vanished_directory_diagnostics(tmp_path):
    (tmp_path / "vanished").mkdir()
    vanished = _run_find_wrapper(
        tmp_path / "vanished",
        "echo \"find: '/h/x': No such file or directory\" >&2; exit 1",
    )
    assert vanished.returncode == 0, vanished.stderr

    for case, body in {
        "denied": "echo \"find: '/h/x': Permission denied\" >&2; exit 1",
        "chown": "echo \"find: '/h/x': No such file or directory\" >&2; "
        "echo \"chown: changing ownership of '/h/y': Read-only file system\" >&2; exit 1",
        "silent": "exit 1",
        "root": "echo \"find: '/h': No such file or directory\" >&2; exit 1",
        "exec": "echo \"find: 'sh': No such file or directory\" >&2; exit 1",
    }.items():
        (tmp_path / case).mkdir()
        result = _run_find_wrapper(tmp_path / case, body)
        assert result.returncode != 0, case
        if case != "silent":
            assert "find: " in result.stderr or "chown: " in result.stderr, case


def test_uid_change_migrates_pruned_subtrees_from_old_uid():
    """Skipping usermod's walk must not leave .git metadata or a writable
    agent checkout owned by the previous UID; a read-only agent mount stays
    untouched."""
    root = _root_init_section()
    assert root.index("_old_hermeswebui_uid=$(id -u hermeswebui)") < root.index(
        'usermod -o -u "${WANTED_UID}"'
    )
    assert root.index("chown_home_hermeswebui || error_exit") < root.index(
        'migrate_home_old_uid "$_old_hermeswebui_uid" || error_exit'
    )
    migrate = _function("migrate_home_old_uid")
    assert '[ -w "$agent" ] || skip_agent=(-path "$agent" -prune -o)' in migrate
    assert '-uid "$old_uid"' in migrate
    assert 'migrate_home_old_uid "${WANTED_UID}" {} +' in migrate


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
