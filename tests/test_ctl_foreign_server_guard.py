"""ctl.sh must not double-start against a foreign/supervised WebUI instance.

Field failure this pins down: a systemd-supervised WebUI (Restart=always) was
serving :8787 while ctl.sh's PID file was stale. `ctl.sh stop` said "stopped",
`ctl.sh restart` spawned a bootstrap that died ~2s later on server.py's
"already responding" startup check — AFTER ctl.sh's old 0.15s aliveness gate
had printed "Started" and recorded the doomed PID. Killing the foreign server
by hand then put systemd's auto-restart into a race with ctl.sh's own start,
ending in a permanent 5s crash loop (each systemd attempt aborting against the
ctl.sh-started server).

Guards under test:
- start refuses when anything already answers HTTP(S) on the target port.
- start refuses when the hermes-webui systemd unit is active on our port or
  mid-auto-restart (activating), instead of racing its next respawn.
- start reports failure (and cleans the PID file) when the spawned server dies
  during the startup window instead of printing success after 0.15s.
- status/stop surface a running unmanaged instance instead of claiming
  "stopped" and never touch a process ctl.sh does not own.
"""

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.test_ctl_script import (
    bash_path,
    run_ctl,
    write_fake_python,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_dummy_http_server(port: int, status: int = 200) -> subprocess.Popen:
    """A minimal HTTP server answering every request with `status`."""
    code = textwrap.dedent(
        f"""
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response({status})
                body = b'{{"status": "ok", "sessions": 0, "active_streams": 0}}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        HTTPServer(("127.0.0.1", {port}), H).serve_forever()
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc
        except OSError:
            time.sleep(0.05)
    proc.terminate()
    raise AssertionError("dummy HTTP server did not come up")


def _stop_proc(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def _write_fake_systemctl(
    fake_bin: Path,
    active_state: str,
    main_pid: int = 0,
    environment: str = "",
    exec_start: str = "",
) -> None:
    """Fake systemctl answering `show -p <prop> --value <unit>` for the
    ActiveState/MainPID/Environment/ExecStart properties the guard inspects."""
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env bash
            for arg in "$@"; do
              case "${{arg}}" in
                *ActiveState*) echo "{active_state}"; exit 0 ;;
                *MainPID*) echo "{main_pid}"; exit 0 ;;
                *Environment*) echo "{environment}"; exit 0 ;;
                *ExecStart*) echo "{exec_start}"; exit 0 ;;
              esac
            done
            exit 0
            """
        ).lstrip(),
        encoding="utf-8",
    )
    systemctl.chmod(0o755)


def _write_fake_lsof_with_real_and_semantics(fake_bin: Path) -> None:
    """Fake lsof mimicking real -a semantics: without -a, `-p PID -iTCP:PORT`
    OR-combines its selectors (matching whenever EITHER hits); with -a they
    AND. Exit codes come from FAKE_LSOF_OR_EXIT / FAKE_LSOF_AND_EXIT so a test
    can model 'the pid is alive with sockets, but nothing of it listens on the
    requested port' — the case a missing -a falsely reports as a conflict."""
    lsof = fake_bin / "lsof"
    lsof.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env bash
            has_a=0
            for arg in "$@"; do
              [[ "${arg}" == "-a" ]] && has_a=1
            done
            if [[ "${has_a}" == 1 ]]; then
              exit "${FAKE_LSOF_AND_EXIT:-1}"
            fi
            exit "${FAKE_LSOF_OR_EXIT:-0}"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    lsof.chmod(0o755)


def _write_fake_port_tools(fake_bin: Path, pid_listens: bool) -> None:
    """Fake lsof/ss so _pid_listens_on_port resolves deterministically."""
    lsof = fake_bin / "lsof"
    lsof.write_text(
        "#!/usr/bin/env bash\n" + ("exit 0\n" if pid_listens else "exit 1\n"),
        encoding="utf-8",
    )
    lsof.chmod(0o755)
    # _pid_listens_on_port prefers lsof; keep a fake ss anyway so the shim
    # works on hosts without lsof, where the ss branch runs instead.
    ss = fake_bin / "ss"
    if pid_listens:
        ss.write_text(
            '#!/usr/bin/env bash\n'
            'echo "LISTEN 0 64 0.0.0.0:8787 0.0.0.0:* users:((\\"python\\",pid=4242,fd=7))"\n',
            encoding="utf-8",
        )
    else:
        ss.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    ss.chmod(0o755)


def _guard_env(fake_bin: Path | None = None, **extra: str) -> dict[str, str]:
    env = {
        "HERMES_WEBUI_CTL_ALLOW_LAUNCHD_CONFLICT": "1",
    }
    if fake_bin is not None:
        env["PATH"] = f"{bash_path(fake_bin)}{os.pathsep}{os.environ.get('PATH', '')}"
    env.update(extra)
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_start_refuses_when_port_already_serving(tmp_path):
    port = _free_port()
    server = _start_dummy_http_server(port)
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT="1",
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 2, combined
        assert "already responding" in combined
        assert not (tmp_path / ".hermes" / "webui.pid").exists()
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_start_refuses_even_when_squatter_answers_http_errors(tmp_path):
    """server.py treats ANY response bytes as a conflict; the guard must match
    (a 404 from a foreign app squatting the port still dooms our server)."""
    port = _free_port()
    server = _start_dummy_http_server(port, status=404)
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT="1",
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 2, combined
        assert "already responding" in combined
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="systemd is a Linux/POSIX path")
def test_start_refuses_when_systemd_unit_is_auto_restarting(tmp_path):
    """ActiveState=activating means the unit is between Restart= attempts: the
    port is briefly silent, but starting now traps the unit in a crash loop."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_systemctl(fake_bin, "activating")

    result = run_ctl(
        tmp_path,
        "start",
        env=_guard_env(fake_bin),
        timeout=15,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert "activating" in combined
    assert "systemctl" in combined
    assert not (tmp_path / ".hermes" / "webui.pid").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="systemd is a Linux/POSIX path")
def test_start_refuses_when_systemd_unit_active_on_our_port(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_systemctl(fake_bin, "active", main_pid=4242)
    _write_fake_port_tools(fake_bin, pid_listens=True)

    result = run_ctl(
        tmp_path,
        "start",
        env=_guard_env(fake_bin),
        timeout=15,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert "4242" in combined
    assert not (tmp_path / ".hermes" / "webui.pid").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="systemd is a Linux/POSIX path")
def test_start_allows_alternate_port_while_systemd_unit_auto_restarts(tmp_path):
    """Mirror of the launchd #3291 over-block fix: an auto-restarting default
    unit must not block a test instance on a different port."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_systemctl(fake_bin, "activating")

    fake_python = tmp_path / "fake-python"
    fake_log = tmp_path / "fake-python.log"
    write_fake_python(fake_python)

    port = _free_port()
    started_pid = None
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                fake_bin,
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_PYTHON=str(fake_python),
                FAKE_PYTHON_LOG=str(fake_log),
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert "Refusing to start" not in combined, combined
        assert result.returncode == 0, combined
        pid_file = tmp_path / ".hermes" / "webui.pid"
        if pid_file.exists():
            started_pid = int(pid_file.read_text().strip())
    finally:
        if started_pid:
            try:
                os.kill(started_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_systemd_pid_port_check_requires_and_semantics(tmp_path):
    """lsof without -a OR-combines -p/-i, so an active unit whose MainPID has
    sockets but does NOT listen on the requested port must not be mistaken
    for a conflict (12.07 re-gate finding 2)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_systemctl(fake_bin, "active", main_pid=4242)
    _write_fake_lsof_with_real_and_semantics(fake_bin)

    fake_python = tmp_path / "fake-python"
    fake_log = tmp_path / "fake-python.log"
    write_fake_python(fake_python)

    port = _free_port()
    started_pid = None
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                fake_bin,
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_PYTHON=str(fake_python),
                FAKE_PYTHON_LOG=str(fake_log),
                FAKE_LSOF_OR_EXIT="0",
                FAKE_LSOF_AND_EXIT="1",
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert "Refusing to start" not in combined, combined
        assert result.returncode == 0, combined
        pid_file = tmp_path / ".hermes" / "webui.pid"
        if pid_file.exists():
            started_pid = int(pid_file.read_text().strip())
    finally:
        if started_pid:
            try:
                os.kill(started_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_start_on_default_port_allowed_when_unit_configured_elsewhere(tmp_path):
    """Reverse alternate-port case (12.07 re-gate finding 3): a unit whose
    CONFIGURED binding is a non-default port must not block a start on 8787
    just because its ownership cannot be attributed via MainPID."""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8787)) == 0:
            pytest.skip("default port 8787 already occupied on this host")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_systemctl(
        fake_bin, "activating", environment="HERMES_WEBUI_PORT=9999"
    )

    fake_python = tmp_path / "fake-python"
    fake_log = tmp_path / "fake-python.log"
    write_fake_python(fake_python)

    started_pid = None
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                fake_bin,
                HERMES_WEBUI_PORT="8787",
                HERMES_WEBUI_PYTHON=str(fake_python),
                FAKE_PYTHON_LOG=str(fake_log),
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert "Refusing to start" not in combined, combined
        assert result.returncode == 0, combined
        pid_file = tmp_path / ".hermes" / "webui.pid"
        if pid_file.exists():
            started_pid = int(pid_file.read_text().strip())
    finally:
        if started_pid:
            try:
                os.kill(started_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_start_refuses_when_unit_configured_for_requested_alternate_port(tmp_path):
    """Counterpart of the reverse case: when the unit's configured binding
    matches the requested (non-default) port, the conflict must be reported."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    port = _free_port()
    _write_fake_systemctl(
        fake_bin, "activating", environment=f"HERMES_WEBUI_PORT={port}"
    )

    result = run_ctl(
        tmp_path,
        "start",
        env=_guard_env(fake_bin, HERMES_WEBUI_PORT=str(port)),
        timeout=15,
    )
    combined = result.stdout + result.stderr
    assert "Refusing to start" in combined, combined
    assert result.returncode != 0, combined
    assert str(port) in combined


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
@pytest.mark.parametrize(
    ("command", "expected_output"),
    [
        ("status", "running (not managed by ctl.sh)"),
        ("stop", "NOT managed by ctl.sh"),
    ],
)
def test_unmanaged_instance_commands_survive_inherit_errexit(
    tmp_path, command, expected_output
):
    """12.07 re-gate finding 1: with errexit inherited into command
    substitutions (BASHOPTS=inherit_errexit from the invoking shell), the
    no-match listener diagnostics must still not abort status/stop."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_port_tools(fake_bin, pid_listens=False)
    port = _free_port()
    server = _start_dummy_http_server(port)
    try:
        result = run_ctl(
            tmp_path,
            command,
            env=_guard_env(
                fake_bin,
                HERMES_WEBUI_PORT=str(port),
                BASHOPTS="inherit_errexit",
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert expected_output in combined
        assert server.poll() is None, "ctl.sh must leave a foreign server untouched"
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_start_proceeds_when_systemd_unit_inactive(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_systemctl(fake_bin, "inactive")

    fake_python = tmp_path / "fake-python"
    fake_log = tmp_path / "fake-python.log"
    write_fake_python(fake_python)

    port = _free_port()
    started_pid = None
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                fake_bin,
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_PYTHON=str(fake_python),
                FAKE_PYTHON_LOG=str(fake_log),
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert "Refusing to start" not in combined, combined
        assert result.returncode == 0, combined
        started_pid = int((tmp_path / ".hermes" / "webui.pid").read_text().strip())
    finally:
        if started_pid:
            try:
                os.kill(started_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_start_reports_failure_when_server_dies_during_startup(tmp_path):
    """A server that exits ~0.5s in (import error, stolen port) must yield a
    failure and no stale PID file — not '[ctl] Started'."""
    dying_python = tmp_path / "dying-python"
    dying_python.write_text(
        "#!/usr/bin/env bash\nsleep 0.5\nexit 1\n",
        encoding="utf-8",
    )
    dying_python.chmod(0o755)

    port = _free_port()
    result = run_ctl(
        tmp_path,
        "start",
        env=_guard_env(
            HERMES_WEBUI_PORT=str(port),
            HERMES_WEBUI_PYTHON=str(dying_python),
            HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT="1",
        ),
        timeout=15,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "failed to stay running" in combined
    assert "Started Hermes WebUI" not in combined
    assert not (tmp_path / ".hermes" / "webui.pid").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_status_reports_unmanaged_running_instance(tmp_path):
    port = _free_port()
    server = _start_dummy_http_server(port)
    try:
        result = run_ctl(
            tmp_path,
            "status",
            env=_guard_env(HERMES_WEBUI_PORT=str(port)),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "running (not managed by ctl.sh)" in combined
        assert "stopped" not in combined
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
@pytest.mark.parametrize(
    ("command", "expected_output"),
    [
        ("status", "running (not managed by ctl.sh)"),
        ("stop", "NOT managed by ctl.sh"),
    ],
)
def test_unmanaged_instance_commands_survive_missing_listener_diagnostics(
    tmp_path, command, expected_output
):
    """Best-effort listener diagnostics must not abort status/stop under set -e
    when ss/lsof are unavailable or return no matching listener row."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_port_tools(fake_bin, pid_listens=False)
    port = _free_port()
    server = _start_dummy_http_server(port)
    try:
        result = run_ctl(
            tmp_path,
            command,
            env=_guard_env(fake_bin, HERMES_WEBUI_PORT=str(port)),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert expected_output in combined
        assert "Listener:" not in combined
        assert server.poll() is None, "ctl.sh must leave a foreign server untouched"
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_stop_warns_about_unmanaged_instance_and_leaves_it_alone(tmp_path):
    port = _free_port()
    server = _start_dummy_http_server(port)
    try:
        result = run_ctl(
            tmp_path,
            "stop",
            env=_guard_env(HERMES_WEBUI_PORT=str(port)),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "NOT managed by ctl.sh" in combined
        assert server.poll() is None, "ctl.sh stop must not kill a foreign server"
        # The foreign server must still answer after stop returned.
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_port_guard_ignores_http_proxy_env(tmp_path):
    """The ownership probe must force a direct connection: a configured
    http_proxy would report the proxy, not the local port (Greptile P1 on
    #5944) — a dead proxy hides an occupied port and start proceeds doomed."""
    port = _free_port()
    server = _start_dummy_http_server(port)
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT="1",
                # Dead proxy: any probe routed through it sees no responder.
                http_proxy="http://127.0.0.1:1",
                https_proxy="http://127.0.0.1:1",
                HTTP_PROXY="http://127.0.0.1:1",
                HTTPS_PROXY="http://127.0.0.1:1",
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 2, combined
        assert "already responding" in combined
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_start_health_probe_ignores_all_proxy_env(tmp_path):
    """The startup watch must reach its own local health server directly.

    Unlike the preflight ownership guard above, this drives the post-launch
    health loop: a dead ALL_PROXY must not make ctl wait out its grace period
    and print the misleading health-timeout note.
    """
    port = _free_port()
    health_server = tmp_path / "health-server"
    health_server.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import sys
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = b'{"status": "ok"}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            HTTPServer(("127.0.0.1", int(sys.argv[-1])), HealthHandler).serve_forever()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    health_server.chmod(0o755)

    started_pid = None
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_PYTHON=str(health_server),
                HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT="1",
                # A live local server must still win when both proxy spellings
                # route clients to a dead endpoint.
                ALL_PROXY="http://127.0.0.1:1",
                all_proxy="http://127.0.0.1:1",
                NO_PROXY="",
                no_proxy="",
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        pid_file = tmp_path / ".hermes" / "webui.pid"
        if pid_file.exists():
            started_pid = int(pid_file.read_text().strip())
        assert result.returncode == 0, combined
        assert "/health did not respond" not in combined
    finally:
        if started_pid:
            try:
                os.kill(started_pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_ipv6_host_probe_builds_bracketed_url(tmp_path):
    """HERMES_WEBUI_HOST='::1' must probe http://[::1]:port — the unbracketed
    literal is rejected by curl/wget and the running instance is missed."""
    if not socket.has_ipv6:
        pytest.skip("no IPv6 support")
    port = _free_port()
    code = textwrap.dedent(
        f"""
        import socket
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class V6Server(HTTPServer):
            address_family = socket.AF_INET6

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{{"status": "ok"}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        V6Server(("::1", {port}), H).serve_forever()
        """
    )
    try:
        server = subprocess.Popen([sys.executable, "-c", code])
    except OSError:
        pytest.skip("cannot bind ::1")
    try:
        deadline = time.time() + 5
        up = False
        while time.time() < deadline:
            if server.poll() is not None:
                pytest.skip("IPv6 loopback unavailable")
            try:
                with socket.create_connection(("::1", port), timeout=0.2):
                    up = True
                    break
            except OSError:
                time.sleep(0.05)
        assert up, "IPv6 dummy server did not come up"

        result = run_ctl(
            tmp_path,
            "start",
            env=_guard_env(
                HERMES_WEBUI_HOST="::1",
                HERMES_WEBUI_PORT=str(port),
                HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT="1",
            ),
            timeout=15,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 2, combined
        assert "already responding" in combined
    finally:
        _stop_proc(server)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_zero_start_grace_still_monitors_startup(tmp_path):
    """HERMES_WEBUI_START_GRACE=0 must not disable the startup watch — that
    would restore the exact stale-PID-on-early-death behavior being fixed."""
    dying_python = tmp_path / "dying-python"
    dying_python.write_text(
        "#!/usr/bin/env bash\nsleep 0.5\nexit 1\n",
        encoding="utf-8",
    )
    dying_python.chmod(0o755)

    port = _free_port()
    result = run_ctl(
        tmp_path,
        "start",
        env=_guard_env(
            HERMES_WEBUI_PORT=str(port),
            HERMES_WEBUI_PYTHON=str(dying_python),
            HERMES_WEBUI_CTL_ALLOW_SYSTEMD_CONFLICT="1",
            HERMES_WEBUI_START_GRACE="0",
        ),
        timeout=15,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "failed to stay running" in combined
    assert not (tmp_path / ".hermes" / "webui.pid").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon guards")
def test_stop_warns_using_saved_binding_from_state_file(tmp_path):
    """With no PID file but a saved off-default binding in the state file,
    stop must probe THAT binding (before deleting the file) and still warn
    about the unmanaged instance."""
    port = _free_port()
    server = _start_dummy_http_server(port)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "webui.ctl.env").write_text(
        f"PID=999999\nHOST=127.0.0.1\nPORT={port}\n",
        encoding="utf-8",
    )
    try:
        # No HERMES_WEBUI_PORT in the environment: the probe target must come
        # from the saved state file.
        result = run_ctl(tmp_path, "stop", env=_guard_env(), timeout=15)
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "NOT managed by ctl.sh" in combined
        assert server.poll() is None
    finally:
        _stop_proc(server)
