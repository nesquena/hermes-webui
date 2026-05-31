#!/usr/bin/env python3
"""Hermes desktop supervisor.

Single process the macOS .app launches and owns. It:

  1. Makes sure the Hermes Agent is installed (runs the official installer on
     first launch when `hermes` / the agent dir is missing).
  2. Starts the WebUI server (which runs the agent in-process) as a child in
     its OWN process group, so the whole tree can be torn down atomically.
  3. Waits for the server's /health endpoint, then prints a machine-readable
     ``HERMES-READY port=<n> url=<u>`` line on stdout so the native shell knows
     when to load the page.
  4. Blocks until it is asked to stop — via SIGTERM/SIGINT (sent by the app on
     quit) OR via the watchdog (the app process dying / re-parenting to launchd).
  5. On stop, shuts EVERYTHING down in order: graceful SIGTERM to the WebUI
     process group, `hermes gateway stop` to reap any background gateway the
     agent installer may have started, then SIGKILL as a backstop.

The design goal is the user's requirement: closing the app reliably stops the
frontend, the backend, AND the Hermes agent — with no lingering processes.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# --- Configuration (overridable via environment) -----------------------------

HOST = os.environ.get("HERMES_WEBUI_HOST", "127.0.0.1")
PORT = int(os.environ.get("HERMES_WEBUI_PORT", "8787"))
HEALTH_URL = f"http://{HOST}:{PORT}/health"
READY_TIMEOUT = float(os.environ.get("HERMES_SUPERVISOR_READY_TIMEOUT", "240"))
INSTALLER_URL = (
    "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh"
)

# WebUI repo root. In a bundled .app this is .../Contents/Resources/webui; in a
# dev checkout it is the repo containing this file (../../ from packaging/macos).
WEBUI_DIR = Path(
    os.environ.get("HERMES_WEBUI_DIR", "")
    or (Path(__file__).resolve().parents[2])
).resolve()

_stop_requested = False
_child: subprocess.Popen | None = None


def log(msg: str) -> None:
    print(f"[supervisor] {msg}", flush=True)


_last_progress = 0.0


def progress(msg: str, *, force: bool = False) -> None:
    """Emit a user-facing status line the native shell shows on the loading view.

    Throttled to ~2/sec so streaming a noisy installer doesn't spam, except for
    `force` phase markers which always go through.
    """
    global _last_progress
    now = time.time()
    if force or now - _last_progress > 0.45:
        _last_progress = now
        # One line only; the shell renders the latest as the status text.
        print(f"HERMES-PROGRESS {msg.strip()[:160]}", flush=True)


def _stream_subprocess(cmd: list[str], **popen_kwargs) -> int:
    """Run a command, forwarding its output live as throttled progress lines."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, **popen_kwargs,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)             # full detail → Console.app
            progress(line)        # latest line → loading screen
    return proc.wait()


def hermes_available() -> bool:
    if shutil.which("hermes"):
        return True
    # The installer drops the CLI in ~/.local/bin which may not be on PATH yet.
    return (Path.home() / ".local" / "bin" / "hermes").exists()


def ensure_agent_installed() -> None:
    """Run the official Hermes Agent installer when no agent is present."""
    progress("Checking for Hermes agent…", force=True)
    if hermes_available():
        log("Hermes agent already present.")
        return
    progress("Installing Hermes agent — first run downloads ~300 MB, "
             "this can take several minutes…", force=True)
    log(f"Hermes agent not found — installing via {INSTALLER_URL}")
    # Stream the installer's output so the loading screen shows live progress.
    rc = _stream_subprocess(["/bin/bash", "-lc", f"curl -fsSL {INSTALLER_URL} | bash"])
    if rc != 0:
        raise RuntimeError(f"agent installer exited with code {rc}")
    # Make the freshly-installed CLI reachable for this process and children.
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
    progress("Hermes agent installed.", force=True)
    log("Hermes agent install finished.")


def start_webui() -> subprocess.Popen:
    """Start the WebUI server in its own process group (new session)."""
    python_exe = os.environ.get("HERMES_WEBUI_PYTHON") or sys.executable
    bootstrap = WEBUI_DIR / "bootstrap.py"
    if not bootstrap.exists():
        raise FileNotFoundError(f"bootstrap.py not found under {WEBUI_DIR}")
    env = os.environ.copy()
    env["HERMES_WEBUI_HOST"] = HOST
    env["HERMES_WEBUI_PORT"] = str(PORT)
    log(f"Starting WebUI: {python_exe} {bootstrap} (port {PORT})")
    # --foreground keeps bootstrap attached as the running server; --no-browser
    # because the native shell renders the UI itself. start_new_session=True puts
    # the server + any agent/terminal subprocesses in one killable process group.
    return subprocess.Popen(
        [python_exe, str(bootstrap), "--no-browser", "--foreground",
         "--host", HOST, str(PORT)],
        cwd=str(WEBUI_DIR),
        env=env,
        start_new_session=True,
    )


def wait_for_health(deadline: float) -> bool:
    while time.time() < deadline:
        if _stop_requested:
            return False
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def stop_everything() -> None:
    """Tear down the WebUI process group and any background gateway."""
    global _child
    log("Shutting down…")

    # 1) Graceful: SIGTERM the WebUI process group.
    if _child and _child.poll() is None:
        try:
            pgid = os.getpgid(_child.pid)
            os.killpg(pgid, signal.SIGTERM)
            log(f"Sent SIGTERM to WebUI process group {pgid}")
        except ProcessLookupError:
            pass

    # 2) Stop any background agent gateway the installer/agent may have started
    #    (launchd-managed) so nothing Hermes lingers after the app quits.
    if hermes_available():
        hermes = shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
        try:
            subprocess.run([hermes, "gateway", "stop"], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("Requested `hermes gateway stop`.")
        except Exception as exc:
            log(f"gateway stop note: {exc}")

    # 3) Give the WebUI up to 8s to exit, then SIGKILL the group as a backstop.
    if _child:
        for _ in range(80):
            if _child.poll() is not None:
                break
            time.sleep(0.1)
        if _child.poll() is None:
            try:
                os.killpg(os.getpgid(_child.pid), signal.SIGKILL)
                log("Force-killed WebUI process group.")
            except ProcessLookupError:
                pass
    log("Shutdown complete.")


def _handle_signal(signum, _frame):
    global _stop_requested
    _stop_requested = True
    log(f"Received signal {signum}.")


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    parent_pid = os.getppid()  # the native app; if it dies we self-terminate
    log(f"Supervisor up (pid={os.getpid()}, parent={parent_pid}, webui_dir={WEBUI_DIR})")

    try:
        ensure_agent_installed()
    except Exception as exc:
        log(f"Agent install failed: {exc}")
        return 2

    global _child
    progress("Starting Hermes backend…", force=True)
    _child = start_webui()

    progress("Waiting for the server to come up…", force=True)
    if wait_for_health(time.time() + READY_TIMEOUT):
        print(f"HERMES-READY port={PORT} url=http://{HOST}:{PORT}", flush=True)
        log("WebUI is healthy.")
    else:
        if not _stop_requested:
            log("WebUI did not become healthy in time.")
            stop_everything()
            return 3

    # Main loop: exit when asked to stop, the WebUI dies, or the parent app dies.
    while not _stop_requested:
        if _child.poll() is not None:
            log(f"WebUI exited (code={_child.returncode}); shutting down.")
            break
        if os.getppid() != parent_pid:  # watchdog: app was force-quit / re-parented
            log("Parent app exited; shutting down.")
            break
        time.sleep(0.5)

    stop_everything()
    return 0


if __name__ == "__main__":
    sys.exit(main())
