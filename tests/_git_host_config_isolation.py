"""Keep the git children of the git-credential tests off the host's git config.

WebUI deliberately drops ``GIT_CONFIG_SYSTEM``, ``GIT_CONFIG_NOSYSTEM``,
``GIT_CONFIG_GLOBAL`` and the other config redirections from every git child
(``api/subprocess_utils.GIT_ENV_SCRUB_KEYS``), then re-applies credential helpers
it reads from the trusted system and global scopes with ``git config --system`` /
``--global`` (``trusted_git_credential_config``). In a test that means every git
child re-reads the developer's real system config. Homebrew git's
``/opt/homebrew/etc/gitconfig`` sets ``credential.helper=osxkeychain``, so the
tests' authenticated fetches against their 127.0.0.1 HTTP servers stored test
credentials in the real login keychain, or hung on a locked one (#8931).

A test cannot fix this by exporting ``GIT_CONFIG_NOSYSTEM`` because the scrub
removes it, and ``GIT_CONFIG_NOSYSTEM`` would not be enough anyway: an explicit
``git config --system`` read, which is how the trusted helpers are discovered,
still reads the system file. Instead the ``isolated_host_git_config`` fixture
(tests/conftest.py) puts a test-owned ``git`` shim first on ``PATH``. Every
production path these tests drive resolves git through ``PATH`` (``"git"`` or
``shutil.which("git")``), so the shim runs *after* the scrub: production still
scrubs exactly as before, and the trusted-helper discovery still runs for real,
but the system scope it reads is a test-owned empty file. The global scope is a
test-owned empty file too while the child's ``HOME`` is still the one the test
started with (the developer's real home). A test that points ``HOME`` at its own
directory keeps normal global-config behaviour for that home, without the host's
``XDG_CONFIG_HOME``.

``emulate_host_with_sentinel_helpers`` stands in for a developer's machine on any
POSIX host, without touching /etc or /opt: a host ``git`` whose built-in system
config and whose user's global config both name a sentinel credential helper.
The guard tests run real production flows on top of it and require the sentinel
never to run.
"""

from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

# Value for ``@pytest.mark.parametrize("host_git", [...], indirect=True)``.
EMULATED_HOST_WITH_SENTINEL_HELPERS = "emulated-host-with-sentinel-helpers"

# GIT_CONFIG_SYSTEM / GIT_CONFIG_GLOBAL arrived in Git 2.32.
_MIN_GIT_FOR_CONFIG_REDIRECTS = (2, 32)


@dataclass(frozen=True)
class HostGit:
    """The git a test's children would run without the shim."""

    executable: str
    # Set only for the emulated host: every sentinel helper call appends a line.
    sentinel_log: Path | None = None
    sentinel_helpers: tuple[str, ...] = ()

    def sentinel_calls(self) -> list[str]:
        if self.sentinel_log is None or not self.sentinel_log.exists():
            return []
        return self.sentinel_log.read_text(encoding="utf-8").splitlines()


def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def git_supports_config_redirects(executable: str) -> bool:
    return _git_version_supports_redirects(executable)


@functools.lru_cache(maxsize=None)
def _git_version_supports_redirects(executable: str) -> bool:
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=20,
    )
    match = re.search(r"(\d+)\.(\d+)", result.stdout or "")
    return bool(match) and (int(match[1]), int(match[2])) >= _MIN_GIT_FOR_CONFIG_REDIRECTS


def require_config_redirects(executable: str) -> None:
    """Skip, rather than run, when Git can't be kept off the host's config.

    Git older than 2.32 ignores ``GIT_CONFIG_SYSTEM``/``GIT_CONFIG_GLOBAL``, and
    ``GIT_CONFIG_NOSYSTEM`` does not stop production's explicit
    ``git config --system`` read. Running the opted-in tests there would hand the
    host's credential helpers (a keychain, a credential manager) the tests'
    credentials, which is exactly what this isolation exists to prevent.
    """
    if not git_supports_config_redirects(executable):
        pytest.skip(
            "Git older than 2.32 ignores GIT_CONFIG_SYSTEM/GIT_CONFIG_GLOBAL, so these "
            "git-credential tests can't be kept off the host's credential helpers"
        )


def resolve_host_git() -> HostGit | None:
    executable = shutil.which("git")
    if executable is None:
        return None
    return HostGit(executable=os.path.abspath(executable))


def emulate_host_with_sentinel_helpers(monkeypatch, root: Path) -> HostGit:
    """Make the host's system and global git config name a sentinel helper.

    A ``git`` shim placed first on ``PATH`` defaults ``GIT_CONFIG_SYSTEM`` to a
    sentinel system config when the variable is unset, which is exactly how a git
    build reads its compiled-in system config (Homebrew's
    ``/opt/homebrew/etc/gitconfig``) once WebUI's scrub has removed the
    variable. ``HOME`` moves to a host home whose ``.gitconfig`` names a second
    sentinel helper. Both helpers append to ``sentinel_log`` and exit non-zero.
    """
    real = resolve_host_git()
    assert real is not None, "git is required"
    root.mkdir(parents=True, exist_ok=True)
    log = root / "sentinel-helper-calls.log"
    helpers = []
    for scope in ("system", "global"):
        helper = _write_executable(
            root / f"sentinel-{scope}-credential-helper",
            "#!/bin/sh\n"
            f'printf \'{scope} %s\\n\' "$*" >> {shlex.quote(str(log))}\n'
            "cat >/dev/null\n"
            "exit 1\n",
        )
        helpers.append(f"!{shlex.quote(str(helper))}")
    system_config = root / "etc-gitconfig"
    system_config.write_text(f"[credential]\n\thelper = {helpers[0]}\n", encoding="utf-8")
    host_home = root / "host-home"
    host_home.mkdir()
    (host_home / ".gitconfig").write_text(
        f"[credential]\n\thelper = {helpers[1]}\n", encoding="utf-8",
    )
    host_bin = root / "host-bin"
    host_bin.mkdir()
    _write_executable(
        host_bin / "git",
        "#!/bin/sh\n"
        "# Emulated host git: its built-in system config is the sentinel config.\n"
        f"GIT_CONFIG_SYSTEM=${{GIT_CONFIG_SYSTEM-{shlex.quote(str(system_config))}}}\n"
        "export GIT_CONFIG_SYSTEM\n"
        f'exec {shlex.quote(real.executable)} "$@"\n',
    )
    monkeypatch.setenv("PATH", f"{host_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return HostGit(
        executable=str(host_bin / "git"),
        sentinel_log=log,
        sentinel_helpers=tuple(helpers),
    )


def isolate_git_from_host_config(monkeypatch, root: Path, host_git: HostGit) -> Path:
    """Put a git shim first on PATH that hides the host system and global config."""
    root.mkdir(parents=True, exist_ok=True)
    empty_system = root / "system-gitconfig"
    empty_system.write_text("", encoding="utf-8")
    empty_global = root / "global-gitconfig"
    empty_global.write_text("", encoding="utf-8")
    host_home = os.environ.get("HOME", "")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    host_home_pattern = f"|{shlex.quote(host_home)}" if host_home else ""
    shim = _write_executable(
        bin_dir / "git",
        "#!/bin/sh\n"
        "# Test-only git shim (tests/_git_host_config_isolation.py, #8931). It runs\n"
        "# after WebUI's GIT_CONFIG_* scrub, so production still scrubs.\n"
        "GIT_CONFIG_NOSYSTEM=1\n"
        f"GIT_CONFIG_SYSTEM={shlex.quote(str(empty_system))}\n"
        'case "${HOME:-}" in\n'
        f"    ''{host_home_pattern}) GIT_CONFIG_GLOBAL={shlex.quote(str(empty_global))} ;;\n"
        '    *) GIT_CONFIG_GLOBAL="$HOME/.gitconfig" ;;\n'
        "esac\n"
        "export GIT_CONFIG_NOSYSTEM GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL\n"
        f'exec {shlex.quote(host_git.executable)} "$@"\n',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return shim


def credential_helpers_seen(executable: str, cwd: Path, env: dict[str, str]) -> list[str]:
    """``scope<TAB>helper`` lines a git child reports for ``credential.helper``."""
    result = subprocess.run(
        [executable, "config", "--show-scope", "--get-all", "credential.helper"],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=20,
    )
    return (result.stdout or "").splitlines()
