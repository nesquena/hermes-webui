"""Behavioral coverage for the unattended update Git diagnostic."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC = ROOT / "scripts" / "diagnose_update_git.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_diagnostic_refuses_the_ext_transport(tmp_path: Path) -> None:
    """An ext:: origin runs a command named by the URL, so the diagnostic must
    refuse the transport before probing it."""
    if os.name == "nt":
        pytest.skip("executable marker setup is POSIX-only")

    marker = tmp_path / "ext-was-invoked"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "protocol.ext.allow", "always")
    # Bare command form: git executes this with the shell, so it creates the
    # marker. (A quoted ``sh -c '...'`` here does not run as written, which would
    # make this test pass without proving anything.)
    _git(repo, "remote", "add", "origin", f"ext::touch {marker}")

    result = subprocess.run(
        [sys.executable, str(DIAGNOSTIC), str(repo)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert not marker.exists(), (
        f"the diagnostic executed an ext:: origin command: {result.stderr!r}"
    )
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("proxy_source", ["environment", "repository"])
def test_diagnostic_never_launches_external_git_proxy(
    tmp_path: Path, proxy_source: str,
) -> None:
    """The documented diagnostic must share production's proxy hardening."""
    if os.name == "nt":
        pytest.skip("executable proxy marker setup is POSIX-only")

    marker = tmp_path / f"{proxy_source}-proxy-was-invoked"
    helper = tmp_path / f"{proxy_source}-proxy.sh"
    helper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8")
    helper.chmod(0o755)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git://example.invalid/origin.git")

    env = os.environ.copy()
    if proxy_source == "environment":
        env["GIT_PROXY_COMMAND"] = str(helper)
    else:
        _git(repo, "config", "core.gitProxy", str(helper))

    result = subprocess.run(
        [sys.executable, str(DIAGNOSTIC), str(repo)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert not marker.exists(), (
        f"the diagnostic launched the {proxy_source} Git proxy: {result.stderr!r}"
    )
