"""Behavior regressions for unattended Git authentication and transports."""

from __future__ import annotations

import base64
import functools
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from api import updates
from api.subprocess_utils import is_safe_diagnostic_remote
from api.workspace_git import GitWorkspaceError, git_fetch


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC = ROOT / "scripts" / "diagnose_update_git.py"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout


def _make_bare_origin(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "master")
    _git(source, "config", "user.name", "Hermes Tests")
    _git(source, "config", "user.email", "hermes-tests@example.invalid")
    (source / "tracked.txt").write_text("authenticated fetch\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-q", "-m", "initial")

    origin = tmp_path / "http-root" / "origin.git"
    origin.parent.mkdir()
    _git(origin.parent, "init", "--bare", "-q", str(origin))
    _git(source, "push", "-q", str(origin), "master")
    _git(origin.parent, "--git-dir", str(origin), "update-server-info")
    return source, origin


class _AuthenticatedGitHandler(SimpleHTTPRequestHandler):
    expected_authorization = ""

    def do_GET(self):  # noqa: N802 - stdlib handler API
        if self.headers.get("Authorization") != self.expected_authorization:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="hermes-test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, format, *args):
        del format, args


@pytest.mark.parametrize("caller", ["updates", "workspace"])
@pytest.mark.parametrize("helper_scope", ["generic", "url"])
@pytest.mark.parametrize("old_git", [False, True])
def test_authenticated_fetch_uses_trusted_global_helper_not_repo_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    helper_scope: str,
    old_git: bool,
) -> None:
    """Trusted user helpers must work while checkout-controlled helpers stay inert."""
    if os.name == "nt":
        pytest.skip("executable credential helper setup is POSIX-only")

    _, origin = _make_bare_origin(tmp_path)
    username = "trusted-user"
    password = "trusted-password"
    authorization = base64.b64encode(f"{username}:{password}".encode()).decode()
    _AuthenticatedGitHandler.expected_authorization = f"Basic {authorization}"
    handler = functools.partial(
        _AuthenticatedGitHandler,
        directory=str(origin.parent),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    home = tmp_path / "home"
    home.mkdir()
    trusted_marker = tmp_path / f"{caller}-trusted-helper-ran"
    repo_marker = tmp_path / f"{caller}-repo-helper-ran"
    helper = tmp_path / "credential_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "marker, username, password = sys.argv[1:4]\n"
        "pathlib.Path(marker).write_text('ran', encoding='utf-8')\n"
        "request = sys.stdin.read()\n"
        "if 'get' not in sys.argv[-1:]:\n"
        "    pass\n"
        "print(f'username={username}')\n"
        "print(f'password={password}')\n"
        "print()\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(home)
    repo = tmp_path / f"{caller}-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    url = f"http://127.0.0.1:{server.server_port}/origin.git"
    _git(repo, "remote", "add", "origin", url)
    helper_key = (
        "credential.helper"
        if helper_scope == "generic"
        else f"credential.http://127.0.0.1:{server.server_port}.helper"
    )
    subprocess.run(
        [
            "git", "config", "--global", helper_key,
            f"!{sys.executable} {helper} {trusted_marker} {username} {password}",
        ],
        env=env,
        check=True,
        timeout=20,
    )
    monkeypatch.setenv("HOME", str(home))
    _git(
        repo,
        "config",
        "credential.helper",
        f"!{sys.executable} -c 'from pathlib import Path; Path(\"{repo_marker}\").touch()'",
    )

    if old_git:
        real_git = shutil.which("git")
        wrapper = tmp_path / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            'for arg do [ "$arg" != "--show-scope" ] || exit 129; done\n'
            f"exec {shlex.quote(real_git)} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    try:
        if caller == "updates":
            output, ok = updates._run_git(["fetch", "origin"], repo, timeout=30)
            assert ok, output
        else:
            result = git_fetch(repo)
            assert result["ok"] is True
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert trusted_marker.exists()
    assert not repo_marker.exists()
    assert _git(repo, "rev-parse", "refs/remotes/origin/master").strip()


@pytest.mark.parametrize("caller", ["updates", "workspace"])
@pytest.mark.parametrize(
    ("ssh_variant", "batch_option"),
    [("ssh", "-oBatchMode=yes"), ("plink", "-batch")],
)
def test_ssh_fetch_uses_trusted_global_command_with_batch_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    ssh_variant: str,
    batch_option: str,
) -> None:
    """A trusted SSH wrapper remains usable without allowing terminal prompts."""
    if os.name == "nt":
        pytest.skip("executable SSH wrapper setup is POSIX-only")

    _, origin = _make_bare_origin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / f"{caller}-trusted-ssh-ran"
    repo_marker = tmp_path / f"{caller}-repo-ssh-ran"
    wrapper = tmp_path / "ssh_wrapper.py"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, shlex, subprocess, sys\n"
        "marker, *args = sys.argv[1:]\n"
        "pathlib.Path(marker).write_text('\\n'.join(args), encoding='utf-8')\n"
        f"args = [value for value in args if value not in ({batch_option!r}, '-oBatchMode=no')]\n"
        "command = args[-1]\n"
        "raise SystemExit(subprocess.call(shlex.split(command)))\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(home)
    trusted_command = " ".join(
        (
            "TEST_SSH_PREFIX=preserved",
            shlex.quote(str(wrapper)),
            shlex.quote(str(marker)),
        )
    )
    subprocess.run(
        ["git", "config", "--global", "core.sshCommand", trusted_command],
        env=env,
        check=True,
        timeout=20,
    )
    subprocess.run(
        ["git", "config", "--global", "ssh.variant", ssh_variant],
        env=env,
        check=True,
        timeout=20,
    )
    monkeypatch.setenv("HOME", str(home))

    repo = tmp_path / f"{caller}-ssh-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", f"test-host:{origin}")
    _git(
        repo,
        "config",
        "core.sshCommand",
        f"{sys.executable} -c 'from pathlib import Path; Path(\"{repo_marker}\").touch()'",
    )

    if caller == "updates":
        output, ok = updates._run_git(["fetch", "origin"], repo, timeout=30)
        assert ok, output
    else:
        assert git_fetch(repo)["ok"] is True

    assert marker.exists()
    ssh_args = marker.read_text(encoding="utf-8").splitlines()
    assert batch_option in ssh_args
    assert not repo_marker.exists()
    assert _git(repo, "rev-parse", "refs/remotes/origin/master").strip()


def _unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize("caller", ["updates", "workspace"])
@pytest.mark.parametrize("proxy_value", ["none", "false for example.invalid"])
def test_fetch_allows_git_protocol_without_applicable_repo_proxy(
    tmp_path: Path, caller: str, proxy_value: str,
) -> None:
    """Unrelated host-scoped proxies must preserve legitimate git:// remotes."""
    _, origin = _make_bare_origin(tmp_path)
    port = _unused_local_port()
    daemon = subprocess.Popen(
        [
            "git", "daemon", "--verbose", "--reuseaddr", "--export-all",
            f"--base-path={origin.parent}", "--listen=127.0.0.1",
            f"--port={port}", str(origin.parent),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = threading.Event()

        def read_readiness():
            for line in daemon.stderr:
                if "Ready to rumble" in line:
                    ready.set()
                    return

        reader = threading.Thread(target=read_readiness, daemon=True)
        reader.start()
        assert ready.wait(5), "git daemon did not become ready"
        reader.join(timeout=1)

        repo = tmp_path / "workspace"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", f"git://127.0.0.1:{port}/origin.git")
        _git(repo, "config", "core.gitProxy", proxy_value)
        if caller == "updates":
            output, ok = updates._run_git(["fetch", "origin"], repo, timeout=30)
            assert ok, output
        else:
            assert git_fetch(repo)["ok"] is True
        assert _git(repo, "rev-parse", "refs/remotes/origin/master").strip()
    finally:
        daemon.terminate()
        daemon.wait(timeout=5)


@pytest.mark.parametrize("caller", ["updates", "workspace"])
@pytest.mark.parametrize("host_scope", [False, True])
def test_fetch_does_not_run_applicable_repo_git_proxy(
    tmp_path: Path, caller: str, host_scope: bool,
) -> None:
    """A checkout-controlled core.gitProxy must not execute for git:// fetch."""
    if os.name == "nt":
        pytest.skip("executable proxy setup is POSIX-only")

    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q")
    marker = tmp_path / "repo-proxy-ran"
    helper = tmp_path / "proxy.sh"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    helper.chmod(0o755)
    _git(repo, "remote", "add", "origin", "git://example.invalid/origin.git")
    proxy_value = f"{helper} for example.invalid" if host_scope else str(helper)
    _git(repo, "config", "core.gitProxy", proxy_value)

    if caller == "updates":
        output, ok = updates._run_git(["fetch", "origin"], repo, timeout=10)
        assert ok is False, output
    else:
        with pytest.raises(GitWorkspaceError):
            git_fetch(repo)

    assert not marker.exists()


@pytest.mark.parametrize("caller", ["updates", "workspace"])
@pytest.mark.parametrize(
    ("remote", "proxy_values"),
    [
        ("git://127.0.0.1:9/origin.git", ("{helper} for 127.0.0.1:9",)),
        ("git://[::1]:9/origin.git", ("{helper} for [::1]:9",)),
        ("git://EXAMPLE.INVALID/origin.git", ("{helper} for EXAMPLE.INVALID",)),
        (
            "git://127.0.0.1:9/origin.git",
            ("{helper} for 127.0.0.1:9", "none"),
        ),
    ],
    ids=["port", "ipv6-brackets", "case-sensitive-host", "first-applicable-entry"],
)
def test_fetch_does_not_run_repo_proxy_selected_by_git_hostandport(
    tmp_path: Path,
    caller: str,
    remote: str,
    proxy_values: tuple[str, ...],
) -> None:
    """The guard must make the same proxy selection as Git before Git runs it."""
    if os.name == "nt":
        pytest.skip("executable proxy setup is POSIX-only")

    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", remote)
    marker = tmp_path / "repo-proxy-ran"
    helper = tmp_path / "proxy.sh"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    helper.chmod(0o755)
    for proxy_value in proxy_values:
        _git(
            repo,
            "config",
            "--add",
            "core.gitProxy",
            proxy_value.format(helper=helper),
        )

    if caller == "updates":
        output, ok = updates._run_git(["fetch", "origin"], repo, timeout=10)
        assert ok is False, output
    else:
        with pytest.raises(GitWorkspaceError):
            git_fetch(repo)

    assert not marker.exists()


@pytest.mark.parametrize("caller", ["updates", "workspace"])
def test_fetch_blocks_included_proxy_after_repo_url_rewrite(
    tmp_path: Path, caller: str,
) -> None:
    """Included repo config and insteadOf must not hide a git:// proxy execution."""
    if os.name == "nt":
        pytest.skip("executable proxy setup is POSIX-only")

    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q")
    marker = tmp_path / "included-proxy-ran"
    helper = tmp_path / "included-proxy.sh"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    helper.chmod(0o755)
    included = repo / "proxy.config"
    included.write_text(
        "[core]\n"
        f"\tgitProxy = {helper}\n"
        '[url "git://example.invalid/"]\n'
        "\tinsteadOf = https://example.invalid/\n",
        encoding="utf-8",
    )
    _git(repo, "config", "include.path", "../proxy.config")
    _git(repo, "remote", "add", "origin", "https://example.invalid/origin.git")

    if caller == "updates":
        output, ok = updates._run_git(["fetch", "origin"], repo, timeout=10)
        assert ok is False, output
    else:
        with pytest.raises(GitWorkspaceError):
            git_fetch(repo)

    assert not marker.exists()


def test_update_git_diagnostic_succeeds_for_safe_http_origin(tmp_path: Path) -> None:
    _, origin = _make_bare_origin(tmp_path)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(origin.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    repo = tmp_path / "diagnostic-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        f"http://127.0.0.1:{server.server_port}/origin.git",
    )

    try:
        result = subprocess.run(
            [sys.executable, str(DIAGNOSTIC), str(repo)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "origin is reachable without prompting" in result.stdout


def test_update_git_diagnostic_redacts_checkout_origin_and_credentials(tmp_path: Path) -> None:
    repo = tmp_path / "private" / "checkout"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    secret_url = "https://private-user:private-password@example.invalid/private/repo.git?token=query-secret"
    _git(repo, "remote", "add", "origin", secret_url)

    result = subprocess.run(
        [sys.executable, str(DIAGNOSTIC), str(repo)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert str(repo) not in output
    assert "private-user" not in output
    assert "private-password" not in output
    assert "query-secret" not in output
    assert "/private/repo.git" not in output


def test_update_git_diagnostic_redacts_invalid_checkout_path(tmp_path: Path) -> None:
    checkout = tmp_path / "private" / "missing-checkout"

    result = subprocess.run(
        [sys.executable, str(DIAGNOSTIC), str(checkout)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 1
    assert str(checkout) not in result.stderr
    assert "<redacted-path>" in result.stderr


def test_update_git_diagnostic_rejects_malformed_origin_without_traceback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "private" / "checkout"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    private_origin_fragment = "private-malformed/private-path"
    _git(repo, "remote", "add", "origin", f"https://[{private_origin_fragment}")

    result = subprocess.run(
        [sys.executable, str(DIAGNOSTIC), str(repo)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert "origin uses an unsupported or malformed transport" in output
    assert "Traceback" not in output
    assert str(repo) not in output
    assert private_origin_fragment not in output


@pytest.mark.parametrize("remote", ["host:path", "user@[::1]:path"])
def test_update_git_diagnostic_probes_builtin_scp_syntax(
    tmp_path: Path,
    remote: str,
) -> None:
    if os.name == "nt":
        pytest.skip("executable Git wrapper setup is POSIX-only")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", remote)
    probe_marker = tmp_path / "remote-probe-ran"
    real_git = shutil.which("git")
    assert real_git is not None
    git_wrapper = tmp_path / "git"
    git_wrapper.write_text(
        "#!/bin/sh\n"
        f"case \" $* \" in *\" ls-remote {remote} \"*) touch '{probe_marker}'; exit 0 ;; esac\n"
        f"exec '{real_git}' \"$@\"\n",
        encoding="utf-8",
    )
    git_wrapper.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [sys.executable, str(DIAGNOSTIC), str(repo)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert probe_marker.exists()
    assert is_safe_diagnostic_remote(remote) is True


@pytest.mark.parametrize(
    "remote",
    [
        "host::path",
        "user@[::1]::path",
        "evil::attacker-controlled",
        "ssh::attacker-controlled",
        "https::attacker-controlled",
        "user@@host:path",
        "host:",
        "https://[private-malformed",
    ],
)
def test_diagnostic_remote_rejects_scp_and_helper_near_misses(remote: str) -> None:
    assert is_safe_diagnostic_remote(remote) is False


@pytest.mark.parametrize(
    ("remote", "helper_names"),
    [
        ("evil::attacker-controlled", ("git-remote-evil",)),
        ("ssh::attacker-controlled", ("git-remote-ssh", "ssh")),
        ("https::attacker-controlled", ("git-remote-https",)),
    ],
)
def test_update_git_diagnostic_rejects_remote_helper_without_invoking_it(
    tmp_path: Path, remote: str, helper_names: tuple[str, ...],
) -> None:
    if os.name == "nt":
        pytest.skip("executable remote helper setup is POSIX-only")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", remote)
    marker = tmp_path / "remote-helper-ran"
    probe_marker = tmp_path / "remote-probe-ran"
    for helper_name in helper_names:
        helper = tmp_path / helper_name
        helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
        helper.chmod(0o755)
    real_git = shutil.which("git")
    assert real_git is not None
    git_wrapper = tmp_path / "git"
    git_wrapper.write_text(
        "#!/bin/sh\n"
        f"case \" $* \" in *\" ls-remote {remote} \"*) touch '{probe_marker}' ;; esac\n"
        f"exec '{real_git}' \"$@\"\n",
        encoding="utf-8",
    )
    git_wrapper.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [sys.executable, str(DIAGNOSTIC), str(repo)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 1
    assert not probe_marker.exists(), result.stdout + result.stderr
    assert not marker.exists(), result.stdout + result.stderr


def test_update_git_diagnostic_does_not_relay_credential_helper_stderr(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("executable credential helper setup is POSIX-only")

    _, origin = _make_bare_origin(tmp_path)
    _AuthenticatedGitHandler.expected_authorization = "Basic deliberately-wrong"
    handler = functools.partial(_AuthenticatedGitHandler, directory=str(origin.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    home = tmp_path / "home"
    home.mkdir()
    private_path = "/private/credential-helper/location"
    secret = "plain-secret-value"
    helper = tmp_path / "credential_helper.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print('password={secret} {private_path}', file=sys.stderr)\n"
        "print('username=user')\n"
        "print('password=wrong-password')\n"
        "print()\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(home)
    subprocess.run(
        ["git", "config", "--global", "credential.helper", f"!{sys.executable} {helper}"],
        env=env,
        check=True,
        timeout=20,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        f"http://127.0.0.1:{server.server_port}/origin.git",
    )
    try:
        result = subprocess.run(
            [sys.executable, str(DIAGNOSTIC), str(repo)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert secret not in output
    assert private_path not in output
    assert "origin is unreachable or authentication failed" in output
