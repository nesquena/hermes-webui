"""Coverage for the WebUI marker/respawn supervisor (container supervision).

Sandbox/container WebUIs died silently because ``python server.py`` was the
container's only service with no respawn. ``scripts/lib/webui_supervisor.sh``
now supervises the server as a directly waited-on child: marker files record
intent (``webui.stop`` / ``webui.pid`` / ``webui.status``), unexpected exits
respawn within seconds, TERM/INT shut down cleanly without respawning, and a
fast crash loop makes the supervisor give up so the outer restart policy sees
a genuinely broken install.
"""

import os
import pathlib
import re
import signal
import subprocess
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
SUPERVISOR = REPO_ROOT / "scripts" / "lib" / "webui_supervisor.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="bash supervisor is POSIX-only"
)


def _spawn(marker_dir, child_script, *, env_extra=None, pids_file=None):
    env = os.environ.copy()
    env.pop("HERMES_WEBUI_SUPERVISOR", None)
    env["HERMES_WEBUI_SUPERVISOR_RESPAWN_DELAY_S"] = "0"
    if pids_file is not None:
        env["SUPERVISOR_TEST_PIDS"] = str(pids_file)
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        ["bash", str(SUPERVISOR), str(marker_dir), "bash", "-c", child_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def _wait_for(predicate, *, timeout=10.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


def _read_pids(pids_file) -> list[int]:
    if not pids_file.exists():
        return []
    return [int(line) for line in pids_file.read_text().split() if line.strip()]


def _finish(proc, *, timeout=10.0) -> str:
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        raise AssertionError(f"supervisor did not exit; output so far:\n{out}")
    return out


def test_respawns_killed_child_within_seconds(tmp_path):
    """Acceptance #1 analog: kill the server child -> a new one within seconds."""
    marker_dir = tmp_path / "markers"
    pids_file = tmp_path / "pids"
    proc = _spawn(
        marker_dir,
        'echo $$ >> "$SUPERVISOR_TEST_PIDS"; exec sleep 30',
        pids_file=pids_file,
    )
    try:
        _wait_for(lambda: len(_read_pids(pids_file)) == 1, message="first child start")
        first_pid = _read_pids(pids_file)[0]
        assert (marker_dir / "webui.pid").read_text().strip() == str(first_pid)

        killed_at = time.monotonic()
        os.kill(first_pid, signal.SIGKILL)
        _wait_for(lambda: len(_read_pids(pids_file)) == 2, message="respawned child")
        respawn_seconds = time.monotonic() - killed_at
        assert respawn_seconds < 5, f"respawn took {respawn_seconds:.1f}s"

        second_pid = _read_pids(pids_file)[1]
        assert second_pid != first_pid
        assert (marker_dir / "webui.pid").read_text().strip() == str(second_pid)
        assert (marker_dir / "webui.status").read_text().startswith("running")
    finally:
        proc.send_signal(signal.SIGTERM)
        out = _finish(proc)

    # The TERM shutdown must not have respawned a third child.
    assert len(_read_pids(pids_file)) == 2, out
    assert "respawn #1" in out


def test_sigterm_stops_without_respawn(tmp_path):
    marker_dir = tmp_path / "markers"
    pids_file = tmp_path / "pids"
    proc = _spawn(
        marker_dir,
        'trap "exit 0" TERM; echo $$ >> "$SUPERVISOR_TEST_PIDS"; '
        "while :; do sleep 0.1; done",
        pids_file=pids_file,
    )
    _wait_for(lambda: len(_read_pids(pids_file)) == 1, message="child start")
    proc.send_signal(signal.SIGTERM)
    out = _finish(proc)

    assert proc.returncode == 0, out
    assert "stop requested" in out
    assert len(_read_pids(pids_file)) == 1, "TERM must not respawn the child"
    assert (marker_dir / "webui.stop").exists()
    assert (marker_dir / "webui.status").read_text().startswith("stopped")
    assert not (marker_dir / "webui.pid").exists()


def test_gives_up_after_consecutive_fast_failures(tmp_path):
    marker_dir = tmp_path / "markers"
    proc = _spawn(
        marker_dir,
        "exit 3",
        env_extra={
            "HERMES_WEBUI_SUPERVISOR_MIN_UPTIME_S": "5",
            "HERMES_WEBUI_SUPERVISOR_MAX_FAST_FAILS": "3",
        },
    )
    out = _finish(proc, timeout=15.0)

    assert proc.returncode == 3, out
    assert "giving up" in out
    assert (marker_dir / "webui.status").read_text().startswith("gave_up")
    # 3 fast failures means the child ran 3 times: 2 respawns, then give-up.
    assert "respawn #2" in out
    assert "respawn #3" not in out


def test_disabled_env_runs_single_shot(tmp_path):
    marker_dir = tmp_path / "markers"
    proc = _spawn(
        marker_dir,
        "exit 7",
        env_extra={"HERMES_WEBUI_SUPERVISOR": "0"},
    )
    out = _finish(proc)

    assert proc.returncode == 7, out
    assert "single shot" in out
    assert not (marker_dir / "webui.status").exists()


def test_stale_stop_marker_does_not_block_startup(tmp_path):
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    (marker_dir / "webui.stop").touch()
    pids_file = tmp_path / "pids"
    proc = _spawn(
        marker_dir,
        'echo $$ >> "$SUPERVISOR_TEST_PIDS"; exec sleep 30',
        pids_file=pids_file,
    )
    try:
        _wait_for(lambda: len(_read_pids(pids_file)) == 1, message="child start")
        assert not (marker_dir / "webui.stop").exists(), (
            "stale stop marker from a previous shutdown must be cleared on start"
        )
    finally:
        proc.send_signal(signal.SIGTERM)
        _finish(proc)


# ── Wiring: docker_init.bash + Dockerfile ────────────────────────────────────


def test_docker_init_launches_server_under_supervisor():
    src = (REPO_ROOT / "docker_init.bash").read_text(encoding="utf-8")
    assert "webui_supervisor.sh" in src, (
        "docker_init.bash must load the marker/respawn supervisor library"
    )
    assert re.search(r'hermes_webui_supervise\s+"\$itdir"\s+python server\.py', src), (
        "docker_init.bash must launch server.py through hermes_webui_supervise"
    )
    assert "cd /app; python server.py" not in src, (
        "the old single-shot (unsupervised) server launch must be gone"
    )


def _dockerfile_healthcheck_block() -> str:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile, "Dockerfile must declare a HEALTHCHECK"
    # The instruction plus its indented continuation lines.
    block_lines = []
    for line in dockerfile[dockerfile.index("HEALTHCHECK"):].splitlines():
        if block_lines and line and not line[0].isspace():
            break
        block_lines.append(line)
    return "\n".join(block_lines)


def test_dockerfile_healthcheck_curls_configured_port():
    block = _dockerfile_healthcheck_block()
    assert "curl" in block, "HEALTHCHECK must issue a real curl probe"
    assert "/health" in block
    assert "127.0.0.1" in block
    assert "${HERMES_WEBUI_PORT" in block, (
        "HEALTHCHECK must probe the configured HERMES_WEBUI_PORT, "
        "not a hardcoded port"
    )
    assert "health_probe.sh" in block, (
        "the TLS-aware health_probe.sh fallback must be kept for HTTPS deploys"
    )
