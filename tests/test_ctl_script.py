import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CTL = REPO_ROOT / "ctl.sh"


def run_ctl(
    home: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 5.0,
    repo_root: Path = REPO_ROOT,
):
    merged = os.environ.copy()
    for key in (
        "HERMES_WEBUI_HOST",
        "HERMES_WEBUI_PORT",
        "HERMES_WEBUI_PYTHON",
        "HERMES_WEBUI_STATE_DIR",
        "HERMES_WEBUI_PID_FILE",
        "HERMES_WEBUI_LOG_FILE",
        "HERMES_WEBUI_CTL_STATE_FILE",
    ):
        merged.pop(key, None)
    merged.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "PATH": os.environ.get("PATH", ""),
        }
    )
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(repo_root / "ctl.sh"), *args],
        cwd=repo_root,
        env=merged,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def write_fake_python(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env bash
            printf 'fake-python args:%s\n' "$*" >> "${FAKE_PYTHON_LOG}"
            printf 'host=%s port=%s state=%s\n' "${HERMES_WEBUI_HOST:-}" "${HERMES_WEBUI_PORT:-}" "${HERMES_WEBUI_STATE_DIR:-}" >> "${FAKE_PYTHON_LOG}"
            trap 'printf "terminated\n" >> "${FAKE_PYTHON_LOG}"; exit 0' TERM INT
            while true; do sleep 0.1; done
            """
        ).lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o755)


def wait_for_pid_file(pid_file: Path, timeout: float = 3.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid_file.exists():
            raw = pid_file.read_text(encoding="utf-8").strip()
            if raw:
                return int(raw)
        time.sleep(0.05)
    raise AssertionError(f"PID file was not written: {pid_file}")


def wait_for_file_text(path: Path, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text:
                return text
        time.sleep(0.05)
    raise AssertionError(f"File was not written: {path}")


def assert_process_exits(pid: int, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} did not exit")


def test_start_writes_pid_under_hermes_home_runs_foreground_no_browser_and_logs(tmp_path):
    fake_python = tmp_path / "fake-python"
    fake_log = tmp_path / "fake-python.log"
    write_fake_python(fake_python)

    result = run_ctl(
        tmp_path,
        "start",
        env={
            "HERMES_WEBUI_PYTHON": str(fake_python),
            "FAKE_PYTHON_LOG": str(fake_log),
            "HERMES_WEBUI_HOST": "0.0.0.0",
            "HERMES_WEBUI_PORT": "18991",
            "HERMES_WEBUI_CTL_ALLOW_LAUNCHD_CONFLICT": "1",
        },
    )

    assert result.returncode == 0, result.stderr + result.stdout
    hermes_home = tmp_path / ".hermes"
    pid_file = hermes_home / "webui.pid"
    log_file = hermes_home / "webui.log"
    pid = wait_for_pid_file(pid_file)
    try:
        assert pid > 1
        assert log_file.exists()
        fake_output = wait_for_file_text(fake_log)
        assert "bootstrap.py --no-browser --foreground" in fake_output
        assert "host=0.0.0.0 port=18991" in fake_output
        assert str(hermes_home / "webui") in fake_output
        status = run_ctl(tmp_path, "status")
        assert status.returncode == 0
        assert "running" in status.stdout
        assert f"PID:     {pid}" in status.stdout
        assert "Bound:   0.0.0.0:18991" in status.stdout
        assert f"Log:     {log_file}" in status.stdout
    finally:
        stop = run_ctl(tmp_path, "stop")
        assert stop.returncode == 0, stop.stderr + stop.stdout
        assert_process_exits(pid)
        assert not pid_file.exists()


def test_start_uses_nohup_so_daemon_survives_launcher_exit():
    ctl_text = CTL.read_text(encoding="utf-8")

    assert "trap '' HUP" in ctl_text
    assert 'exec nohup "${python_exe}"' in ctl_text


def test_start_loads_dotenv_but_inline_overrides_win(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    shutil.copy2(CTL, repo_root / "ctl.sh")
    (repo_root / "bootstrap.py").write_text("# fake bootstrap target\n", encoding="utf-8")

    fake_python = tmp_path / "fake-python"
    fake_log = tmp_path / "fake-python.log"
    write_fake_python(fake_python)
    (repo_root / ".env").write_text(
        "HERMES_WEBUI_HOST=127.9.9.9\nHERMES_WEBUI_PORT=18888\n",
        encoding="utf-8",
    )

    result = run_ctl(
        tmp_path,
        "start",
        env={
            "HERMES_WEBUI_PYTHON": str(fake_python),
            "FAKE_PYTHON_LOG": str(fake_log),
            "HERMES_WEBUI_HOST": "0.0.0.0",
            "HERMES_WEBUI_CTL_ALLOW_LAUNCHD_CONFLICT": "1",
        },
        repo_root=repo_root,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    pid = wait_for_pid_file(tmp_path / ".hermes" / "webui.pid")
    try:
        fake_output = wait_for_file_text(fake_log)
        assert "fake-python args:" in fake_output
        assert "host=0.0.0.0 port=18888" in fake_output
    finally:
        stop = run_ctl(tmp_path, "stop", repo_root=repo_root)
        assert stop.returncode == 0, stop.stderr + stop.stdout
        assert_process_exits(pid)


def test_stale_pid_file_is_removed_without_killing_unrelated_process(tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    pid_file = hermes_home / "webui.pid"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_file.write_text(str(sleeper.pid), encoding="utf-8")
        result = run_ctl(tmp_path, "stop")
        assert result.returncode == 0
        assert "stale" in (result.stdout + result.stderr).lower()
        assert sleeper.poll() is None, "ctl.sh must not kill unrelated PIDs"
        assert not pid_file.exists()
    finally:
        sleeper.terminate()
        try:
            sleeper.wait(timeout=3)
        except subprocess.TimeoutExpired:
            sleeper.kill()


def _write_fake_launchctl(fake_bin, pid):
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env bash
            if [[ "$1" == "print" ]]; then
              printf '\\tpid = {pid}\\n'
              exit 0
            fi
            exit 1
            """
        ).lstrip(),
        encoding="utf-8",
    )
    launchctl.chmod(0o755)


def _write_fake_lsof(fake_bin, listening):
    """Fake lsof: exit 0 (PID listens on the port) iff `listening` is True."""
    lsof = fake_bin / "lsof"
    lsof.write_text(
        "#!/usr/bin/env bash\n" + ("exit 0\n" if listening else "exit 1\n"),
        encoding="utf-8",
    )
    lsof.chmod(0o755)


def test_start_refuses_second_instance_when_launchd_job_owns_the_port(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    _write_fake_launchctl(fake_bin, sleeper.pid)
    # launchd-owned PID IS listening on the requested (default) port → real conflict.
    _write_fake_lsof(fake_bin, listening=True)

    try:
        result = run_ctl(
            tmp_path,
            "start",
            env={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "HERMES_WEBUI_LAUNCHD_LABEL": "com.parantoux.hermes-webui",
            },
        )
        assert result.returncode == 2
        combined = result.stdout + result.stderr
        assert "Refusing to start a second Hermes WebUI" in combined
        assert "launchctl kickstart -k" in combined
        assert not (tmp_path / ".hermes" / "webui.pid").exists()
    finally:
        sleeper.terminate()
        try:
            sleeper.wait(timeout=3)
        except subprocess.TimeoutExpired:
            sleeper.kill()


def test_start_allows_alternate_port_while_launchd_job_runs_on_default(tmp_path):
    """A second instance on a DIFFERENT port must not be blocked by the launchd
    guard, even while the launchd-managed default instance is alive (#3291 fix /
    Codex regression-gate finding for v0.51.191)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    _write_fake_launchctl(fake_bin, sleeper.pid)
    # launchd-owned PID is alive but NOT listening on our (alternate) port → no conflict.
    _write_fake_lsof(fake_bin, listening=False)

    # A fake python so `start` "launches" without needing the real server.
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "#!/usr/bin/env bash\ntrap 'exit 0' TERM INT\nwhile true; do sleep 1; done\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    started_pid = None
    try:
        result = run_ctl(
            tmp_path,
            "start",
            env={
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "HERMES_WEBUI_LAUNCHD_LABEL": "com.parantoux.hermes-webui",
                "HERMES_WEBUI_PORT": "18992",
                "HERMES_WEBUI_PYTHON": str(fake_python),
            },
        )
        combined = result.stdout + result.stderr
        assert "Refusing to start a second Hermes WebUI" not in combined, combined
        assert result.returncode == 0, combined
        pid_file = tmp_path / ".hermes" / "webui.pid"
        if pid_file.exists():
            started_pid = int(pid_file.read_text().strip())
    finally:
        if started_pid:
            try:
                os.kill(started_pid, 15)
            except ProcessLookupError:
                pass
        sleeper.terminate()
        try:
            sleeper.wait(timeout=3)
        except subprocess.TimeoutExpired:
            sleeper.kill()


def test_logs_supports_non_following_line_count(tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    log_file = hermes_home / "webui.log"
    log_file.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = run_ctl(tmp_path, "logs", "--lines", "2", "--no-follow")

    assert result.returncode == 0
    assert result.stdout == "two\nthree\n"
