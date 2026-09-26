"""Destination-sensitive Git trust checks use the transport Git will run."""
import functools
import shlex
import shutil
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from api import subprocess_utils as utils, updates, workspace_git
from tests.test_update_git_security import DIAGNOSTIC, _git, _make_bare_origin


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))


@pytest.mark.parametrize("selection", ["branch", "default", "fetch", "origin", "explicit", "pushurl"])
def test_push_guard_uses_effective_push_destination(tmp_path, isolated_home, selection):
    repo, _ = _make_bare_origin(tmp_path)
    backup = tmp_path / "backup.git"
    _git(tmp_path, "init", "--bare", "-q", str(backup))
    _git(repo, "remote", "add", "origin", "git://example.invalid/repo")
    _git(repo, "remote", "add", "backup", str(backup))
    _git(repo, "config", "core.gitProxy", "false for example.invalid")
    _git(repo, "config", "push.default", "current")
    _git(repo, "config", "branch.master.remote", "origin")
    args = ["push"]
    if selection == "branch":
        _git(repo, "config", "remote.pushDefault", "origin")
        _git(repo, "config", "branch.master.pushRemote", "backup")
    elif selection == "default":
        _git(repo, "config", "remote.pushDefault", "backup")
    elif selection == "fetch":
        _git(repo, "config", "branch.master.remote", "backup")
    elif selection == "origin":
        _git(repo, "config", "--unset", "branch.master.remote")
        _git(repo, "remote", "set-url", "origin", str(backup))
    elif selection == "explicit":
        args.append("backup")
    else:
        _git(repo, "config", "remote.origin.pushurl", str(backup))
    # A real push establishes that this exact configuration has a valid target.
    _git(repo, *args)
    assert not utils.repository_git_proxy_blocks(args, repo, utils.clean_git_env())
    result = workspace_git._run_git(repo, args)
    assert result.returncode == 0, result.stderr
    assert _git(backup, "rev-parse", "master") == _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("caller", ["updates", "workspace", "diagnostic"])
def test_custom_ssh_wrapper_without_shell_on_path(tmp_path, isolated_home, monkeypatch, caller):
    _, origin = _make_bare_origin(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", f"test-host:{origin}")
    wrapper = tmp_path / "custom-ssh"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys, shlex, subprocess\n"
        "if '-G' in sys.argv: raise SystemExit(0)\n"
        "raise SystemExit(subprocess.call(shlex.split(sys.argv[-1])))\n"
    )
    wrapper.chmod(0o755)
    _git(repo, "config", "--global", "core.sshCommand", str(wrapper))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ["git", "git-upload-pack"]:
        (bin_dir / name).symlink_to(shutil.which(name))
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("sh") is None
    # Git itself can run this command using its compiled/resolved shell.
    _git(repo, "fetch", "origin")
    if caller == "updates":
        output, ok = updates._run_git(["fetch", "origin"], repo)
        assert ok, output
    elif caller == "workspace":
        assert workspace_git.git_fetch(repo)["ok"]
    else:
        result = subprocess.run([sys.executable, str(DIAGNOSTIC), str(repo)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("caller", ["updates", "workspace", "diagnostic"])
@pytest.mark.parametrize("transport", ["local", "http"])
def test_non_ssh_destination_never_probes_ssh_wrapper(tmp_path, isolated_home, caller, transport):
    _, origin = _make_bare_origin(tmp_path)
    marker = tmp_path / "ssh-ran"
    wrapper = tmp_path / "custom-ssh"
    wrapper.write_text(f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\nexit 0\n")
    wrapper.chmod(0o755)
    _git(tmp_path, "config", "--global", "core.sshCommand", str(wrapper))
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(origin.parent)))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    remote = str(origin) if transport == "local" else f"http://127.0.0.1:{server.server_port}/origin.git"
    _git(repo, "remote", "add", "origin", remote)
    try:
        if caller == "updates":
            output, ok = updates._run_git(["fetch", "origin"], repo)
            assert ok, output
        elif caller == "workspace":
            assert workspace_git.git_fetch(repo)["ok"]
        else:
            result = subprocess.run([sys.executable, str(DIAGNOSTIC), str(repo)], capture_output=True, text=True, timeout=15)
            # Local origins are deliberately unsupported by this diagnostic.
            assert result.returncode == (1 if transport == "local" else 0), result.stderr
        assert not marker.exists(), "non-SSH operation executed the trusted SSH command"
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


def test_diagnostic_applies_original_checkout_proxy_guard(tmp_path, isolated_home, monkeypatch):
    from scripts import diagnose_update_git as diagnostic

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git://example.invalid/repo")
    _git(repo, "config", "core.gitProxy", "false for example.invalid")
    calls = []
    real_run = diagnostic._run

    def run(args, *rest):
        if args[0] == "ls-remote":
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "reachable", "")
        return real_run(args, *rest)

    monkeypatch.setattr(diagnostic, "_run", run)
    monkeypatch.setattr(sys, "argv", [str(DIAGNOSTIC), str(repo)])
    assert diagnostic.main() == 1
    assert not calls, "refused checkout must not reach the outside-checkout probe"


@pytest.mark.parametrize("url_key", ["pushurl", "url"])
def test_push_checks_every_destination_before_any_side_effect(tmp_path, url_key):
    repo, origin = _make_bare_origin(tmp_path)
    safe = tmp_path / "safe.git"
    _git(tmp_path, "init", "--bare", "-q", str(safe))
    _git(repo, "remote", "add", "origin", str(origin))
    if url_key == "url":
        _git(repo, "config", "--unset-all", "remote.origin.url")
    _git(repo, "config", "--add", f"remote.origin.{url_key}", str(safe))
    _git(repo, "config", "--add", f"remote.origin.{url_key}", "git://example.invalid/repo")
    marker = tmp_path / "proxy-ran"
    proxy = tmp_path / "proxy"
    proxy.write_text(f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\nexit 1\n")
    proxy.chmod(0o755)
    _git(repo, "config", "core.gitProxy", f"{proxy} for example.invalid")
    args = ["push", "-u", "origin", "master"]
    # Real Git reaches the second URL after updating the first one.
    oracle = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=15)
    assert oracle.returncode != 0
    assert marker.exists()
    assert _git(safe, "rev-parse", "master") == _git(repo, "rev-parse", "HEAD")
    marker.unlink()
    _git(repo, "commit", "--allow-empty", "-m", "not pushed")
    assert utils.repository_git_proxy_blocks(args, repo, utils.clean_git_env())
    with pytest.raises(workspace_git.GitWorkspaceError) as exc:
        workspace_git._run_git(repo, args)
    assert exc.value.code == "unsafe_git_config"
    assert not marker.exists()
    assert _git(safe, "rev-parse", "master") != _git(repo, "rev-parse", "HEAD")
    # Fetch/pull contact only the first ordinary URL, not the later push target.
    for command in ("fetch", "pull"):
        assert not utils.repository_git_proxy_blocks([command, "origin"], repo, utils.clean_git_env())
    _git(repo, "fetch", "origin")
    assert not marker.exists()


@pytest.mark.parametrize("url_key", ["pushurl", "url"])
def test_mixed_push_destinations_preserve_ssh_only_when_used(tmp_path, url_key):
    repo, origin = _make_bare_origin(tmp_path)
    local = tmp_path / "local.git"
    ssh = tmp_path / "ssh.git"
    for target in (local, ssh):
        _git(tmp_path, "init", "--bare", "-q", str(target))
    _git(repo, "remote", "add", "origin", str(origin))
    if url_key == "url":
        _git(repo, "config", "--unset-all", "remote.origin.url")
    _git(repo, "config", "--add", f"remote.origin.{url_key}", str(local))
    _git(repo, "config", "--add", f"remote.origin.{url_key}", f"test-host:{ssh}")
    marker = tmp_path / "ssh-ran"
    wrapper = tmp_path / "custom-ssh"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys, shlex, subprocess\n"
        "from pathlib import Path\n"
        f"with Path({str(marker)!r}).open('a') as log: log.write('probe\\n' if '-G' in sys.argv else 'transport\\n')\n"
        "if '-G' in sys.argv: raise SystemExit(0)\n"
        "raise SystemExit(subprocess.call(shlex.split(sys.argv[-1])))\n"
    )
    wrapper.chmod(0o755)
    _git(repo, "config", "--global", "core.sshCommand", str(wrapper))
    args = ["push", "origin", "master"]
    _git(repo, *args)
    assert "transport" in marker.read_text()
    marker.unlink()
    _git(repo, "commit", "--allow-empty", "-m", "through WebUI")
    result = workspace_git._run_git(repo, args)
    assert result.returncode == 0, result.stderr
    assert "probe" in marker.read_text()
    assert "transport" in marker.read_text()
    for target in (local, ssh):
        assert _git(target, "rev-parse", "master") == _git(repo, "rev-parse", "HEAD")
    marker.unlink()
    # The SSH URL is present but irrelevant to fetch/pull's first URL.
    for command in ("fetch", "pull"):
        utils.noninteractive_git_env(repo, utils.clean_git_env(), args=[command, "origin"])
    assert workspace_git._run_git(repo, ["fetch", "origin"]).returncode == 0
    assert not marker.exists()
    # Explicit pushurls override an ordinary SSH URL completely.
    _git(repo, "config", "--replace-all", "remote.origin.pushurl", str(local))
    assert workspace_git._run_git(repo, args).returncode == 0
    assert not marker.exists()
