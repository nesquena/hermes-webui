"""
Plugin subprocess lifecycle manager.

    Spawns, monitors, restarts, and kills plugin subprocesses.
    Each plugin gets its own subprocess with resource limits and
    a minimal environment.
"""

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_PROCESSES: dict[str, subprocess.Popen] = {}
_PROCESS_LOCK = threading.Lock()
_PIPE_LOCKS: dict[str, threading.Lock] = {}

# Environment variable prefixes allowed in plugin subprocess
_ENV_ALLOWED_PREFIXES = {
    "HERMES_HOME",
    "HERMES_PLUGIN_",
    "HERMES_WEBUI_PLUGIN_",
    "HOME",
    "LANG",
    "LC_",
    "PATH",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "USER",
}


def _build_plugin_env(plugin_name: str, plugin_dir: Path) -> dict:
    env = {}
    for k, v in os.environ.items():
        for prefix in _ENV_ALLOWED_PREFIXES:
            if k == prefix or k.startswith(prefix):
                env[k] = v
                break
    env["HERMES_PLUGIN_NAME"] = plugin_name
    env["HERMES_PLUGIN_DIR"] = str(plugin_dir)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def get_plugin_pipe_lock(plugin_name: str) -> threading.Lock:
    with _PROCESS_LOCK:
        if plugin_name not in _PIPE_LOCKS:
            _PIPE_LOCKS[plugin_name] = threading.Lock()
        return _PIPE_LOCKS[plugin_name]


def _read_stderr(proc: subprocess.Popen, plugin_name: str):
    if proc.stderr is None:
        return
    try:
        for line in proc.stderr:
            line_str = line.decode("utf-8", errors="replace")[:2000]  # cap per-line
            line_str = line_str.rstrip()
            if line_str:
                logger.warning("[plugin:%s] %s", plugin_name, line_str)
    except Exception:
        pass


def spawn_plugin(plugin_name: str, plugin_dir: Path) -> subprocess.Popen | None:
    """Spawn a plugin subprocess if not already running.

    Args:
        plugin_name: e.g. 'my-plugin'
        plugin_dir: Path to plugin root (contains __init__.py)

    Returns the subprocess.Popen handle, or None on failure.
    """
    plugin_dir = plugin_dir.resolve()
    runner = Path(__file__).parent / "plugin_runner.py"

    with _PROCESS_LOCK:
        existing = PLUGIN_PROCESSES.get(plugin_name)
        if existing and existing.poll() is None:
            return existing  # already running

        # Kill stale process
        if existing:
            try:
                existing.kill()
                existing.wait(timeout=2)
            except Exception:
                pass
            PLUGIN_PROCESSES.pop(plugin_name, None)

        env = _build_plugin_env(plugin_name, plugin_dir)

        try:
            proc = subprocess.Popen(
                [sys.executable, str(runner)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=False,
            )

            # Wait for ready signal from the subprocess
            if proc.stdout is not None:
                try:
                    import json as _json
                    import select
                    ready, _, _ = select.select([proc.stdout], [], [], 10)
                    if not ready:
                        logger.warning("Plugin %s: no ready signal within 10s, killing", plugin_name)
                        proc.kill()
                        proc.wait()
                        return None
                    ready_line = proc.stdout.readline()
                    if ready_line:
                        ready_msg = _json.loads(ready_line.decode("utf-8").strip())
                        if ready_msg.get("ready"):
                            logger.info(
                                "Plugin %s ready (pid=%d, routes=%s)",
                                plugin_name, proc.pid, ready_msg.get("routes", []),
                            )
                        else:
                            logger.warning("Plugin %s: unexpected startup message", plugin_name)
                            proc.kill(); proc.wait(); return None
                    else:
                        logger.warning("Plugin %s: no startup message, process may have exited", plugin_name)
                        proc.kill(); proc.wait(); return None
                except Exception:
                    logger.exception("Plugin %s: startup failed, killing", plugin_name)
                    proc.kill()
                    proc.wait()
                    return None

            PLUGIN_PROCESSES[plugin_name] = proc
            # Start stderr reader thread
            threading.Thread(
                target=_read_stderr, args=(proc, plugin_name), daemon=True,
            ).start()
            return proc
        except Exception:
            logger.exception("Failed to spawn plugin subprocess: %s", plugin_name)
            return None


def get_plugin_process(plugin_name: str) -> subprocess.Popen | None:
    with _PROCESS_LOCK:
        proc = PLUGIN_PROCESSES.get(plugin_name)
    if proc is None:
        return None
    if proc.poll() is not None:
        logger.warning(
            "Plugin subprocess %s exited (code=%d)", plugin_name, proc.returncode,
        )
        with _PROCESS_LOCK:
            if PLUGIN_PROCESSES.get(plugin_name) is proc:
                PLUGIN_PROCESSES.pop(plugin_name, None)
        return None
    return proc


def kill_plugin(plugin_name: str, timeout: float = 5.0):
    with _PROCESS_LOCK:
        proc = PLUGIN_PROCESSES.pop(plugin_name, None)
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    logger.info("Terminated plugin subprocess: %s", plugin_name)


def kill_all_plugins():
    for name in list(PLUGIN_PROCESSES):
        kill_plugin(name)
