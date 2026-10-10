"""Regression cases from the Git trust-layer compatibility gate."""
import shlex
import shutil
import subprocess

import pytest

from api import subprocess_utils as utils, updates
from api.workspace_git import GitWorkspaceError, git_fetch
from tests.test_update_git_security import _git, _make_bare_origin


@pytest.mark.parametrize("caller", ["updates", "workspace"])
def test_old_git_still_blocks_repository_proxy(tmp_path, isolated_home, monkeypatch, caller):
    import os

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    marker = tmp_path / "proxy-ran"
    helper = repo / "proxy"
    helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    helper.chmod(0o755)
    _git(repo, "config", "core.gitProxy", str(helper))
    _git(repo, "remote", "add", "origin", "git://example.invalid/repo")
    real_git = shutil.which("git")
    wrapper = tmp_path / "git"
    wrapper.write_text(
        '#!/bin/sh\nfor arg do [ "$arg" != "--show-scope" ] || exit 129; done\n'
        f'exec {shlex.quote(real_git)} "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    if caller == "updates":
        _, ok = updates._run_git(["fetch", "origin"], repo)
        assert not ok
    else:
        with pytest.raises(GitWorkspaceError):
            git_fetch(repo)
    assert not marker.exists()


def test_diagnostic_accepts_builtin_git_transport():
    assert utils.is_safe_diagnostic_remote("git://example.invalid/repo.git")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    return home


@pytest.mark.parametrize("caller", ["updates", "workspace"])
def test_custom_named_openssh_wrapper(tmp_path, isolated_home, caller):
    _, origin = _make_bare_origin(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", f"test-host:{origin}")
    marker = tmp_path / "invocations"
    wrapper = tmp_path / "my-ssh"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, shlex, subprocess\n"
        f"with pathlib.Path({str(marker)!r}).open('a') as f: f.write(repr(sys.argv[1:])+'\\n')\n"
        "if '-G' in sys.argv: raise SystemExit(0)\n"
        "assert '-oBatchMode=yes' in sys.argv\n"
        "raise SystemExit(subprocess.call(shlex.split(sys.argv[-1])))\n"
    )
    wrapper.chmod(0o755)
    _git(repo, "config", "--global", "core.sshCommand", f"env WRAPPER=yes {shlex.quote(str(wrapper))}")
    if caller == "updates":
        output, ok = updates._run_git(["fetch", "origin"], repo)
        assert ok, output
    else:
        assert git_fetch(repo)["ok"]
    assert "-G" in marker.read_text()
    assert _git(repo, "rev-parse", "refs/remotes/origin/master").strip()


def test_wrapper_probe_timeout_reaps_child(tmp_path, isolated_home):
    import time

    marker = tmp_path / "probe-child"
    wrapper = tmp_path / "slow-ssh"
    wrapper.write_text(
        "#!/bin/sh\n"
        "sleep 6\n"
        f"touch {shlex.quote(str(marker))}\n"
    )
    wrapper.chmod(0o755)
    _git(tmp_path, "config", "--global", "core.sshCommand", str(wrapper))
    started = time.monotonic()
    env = utils.noninteractive_git_env(tmp_path, utils.clean_git_env())
    assert time.monotonic() - started < 6
    assert env["GIT_SSH_VARIANT"] == "simple"
    time.sleep(1.2)
    assert not marker.exists()


@pytest.mark.parametrize("option", ["-o 'BatchMode no'", "-o'BatchMode NO'", "-o 'bAtChMoDe\tNo'", "-o BatchMode=No", "-oBatchMode=false"])
def test_batchmode_whitespace_fails_closed(tmp_path, isolated_home, option):
    if not shutil.which("ssh"):
        pytest.skip("OpenSSH required")
    # Real OpenSSH demonstrates why appending cannot override the first value.
    if "false" not in option:
        raw = subprocess.run(["ssh", "-G", *shlex.split(option), "-oBatchMode=yes", "example.invalid"], capture_output=True, text=True)
        assert "batchmode no" in raw.stdout
    _git(tmp_path, "config", "--global", "core.sshCommand", f"ssh {option}")
    env = utils.noninteractive_git_env(tmp_path, utils.clean_git_env())
    assert env["GIT_SSH_COMMAND"] == "git-ssh-command-disables-batch-mode"


@pytest.mark.parametrize("command", [r"C:\Tools\PuTTY\plink.exe -P 22", r'"C:\Program Files\PuTTY\plink.exe" -P 22'])
def test_windows_unquoted_plink_path(tmp_path, isolated_home, monkeypatch, command):
    _git(tmp_path, "config", "--global", "core.sshCommand", command)
    # Only parsing is platform-specific; leave pathlib and subprocess on the host.
    monkeypatch.setattr(utils.sys, "platform", "win32")
    env = utils.noninteractive_git_env(tmp_path, utils.clean_git_env())
    assert env["GIT_SSH_VARIANT"] == "plink"
    assert env["GIT_SSH_COMMAND"] == command + " -batch"


@pytest.mark.parametrize("caller", ["updates", "workspace"])
@pytest.mark.parametrize("key", ["core.sshCommand", "credential.helper", "credential.http://127.0.0.1.helper"])
def test_checkout_include_does_not_execute(tmp_path, isolated_home, caller, key):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    marker = tmp_path / "executed"
    helper = repo / "helper"
    helper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    helper.chmod(0o755)
    included = repo / "included.config"
    _git(repo, "config", "--file", str(included), key, str(helper))
    _git(repo, "config", "--global", f"includeIf.gitdir:{repo}/.path", str(included))
    assert str(helper) in _git(repo, "config", "--get", key)
    if key == "core.sshCommand":
        # The vulnerable head needs an explicit variant to execute this helper.
        _git(repo, "config", "--global", "ssh.variant", "ssh")
        remote = "ssh://127.0.0.1:1/missing"
    else:
        # Local HTTP 401 reliably triggers credential selection.
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        remote = f"http://127.0.0.1:{server.server_port}/missing"
        if key != "credential.helper":
            _git(repo, "config", "--file", str(included), "--unset", key)
            _git(repo, "config", "--file", str(included), f"credential.http://127.0.0.1:{server.server_port}.helper", str(helper))
    _git(repo, "remote", "add", "origin", remote)
    try:
        if caller == "updates":
            _, ok = updates._run_git(["fetch", "origin"], repo)
            assert not ok
        else:
            with pytest.raises(GitWorkspaceError):
                git_fetch(repo)
    finally:
        if key != "core.sshCommand":
            server.shutdown()
            thread.join(5)
            server.server_close()
    assert not marker.exists()
