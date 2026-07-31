"""WebUI-owned profile gateway processes for container deployments.

The WebUI image has no systemd/launchd/s6 service manager. This module runs
only profiles explicitly enabled by the Channels UI and keeps each process
pinned to its own HERMES_HOME.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from api.gateway_restart import _gateway_restart_profile_context, _resolve_hermes_command
from api.profiles import (
    _PROFILE_ID_RE,
    _profile_secret_env_names,
    filter_runtime_env_for_gateway_parity,
    get_profile_runtime_env,
    list_profiles_api,
)

logger = logging.getLogger(__name__)
_PROCESSES: dict[str, subprocess.Popen] = {}
_FAILURES: dict[str, dict] = {}
_LOCK = threading.RLock()
_MONITOR_STOP = threading.Event()
_MONITOR_THREAD: threading.Thread | None = None
_ENABLE_KEY = "HERMES_WEBUI_MATRIX_GATEWAY_ENABLED"
_SECRET_NAME_PARTS = ("TOKEN", "PASSWORD", "SECRET", "CREDENTIAL", "API_KEY", "COOKIE")


def _validate_profile(profile: str) -> str:
    value = str(profile or "")
    if not value or not _PROFILE_ID_RE.fullmatch(value):
        raise ValueError("invalid profile")
    return value


def _env_enabled(home: Path) -> bool:
    path = home / ".env"
    if not path.exists():
        return False
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith(f"{_ENABLE_KEY}="):
                return raw.split("=", 1)[1].strip().lower() in {"1", "true", "yes", "on"}
    except OSError:
        return False
    return False


def _public_status(profile: str, status: str, managed: bool) -> dict:
    return {"profile": profile, "status": status, "managed": managed}


def _profile_context(profile: str) -> tuple[Path, str | None, str]:
    """Resolve aliases to the one process identity owned by a profile home."""
    home, cli_profile = _gateway_restart_profile_context(profile)
    home = Path(home).expanduser().resolve()
    return home, cli_profile, str(home)


def _profile_child_env(home: Path) -> dict[str, str]:
    """Build a child env without inheriting another profile/operator's secrets."""
    env = os.environ.copy()
    explicit_secret_names = _profile_secret_env_names(home) | {
        "MATRIX_ACCESS_TOKEN",
        "MATRIX_PASSWORD",
        "MATRIX_USER_ID",
        "MATRIX_HOMESERVER",
    }
    for key in list(env):
        upper = key.upper()
        if key in explicit_secret_names or any(part in upper for part in _SECRET_NAME_PARTS):
            env.pop(key, None)
    selected = filter_runtime_env_for_gateway_parity(get_profile_runtime_env(home))
    env.update(selected)
    env["HERMES_HOME"] = str(home)
    return env


def get_profile_gateway_status(profile: str) -> dict:
    profile = _validate_profile(profile)
    _home, _cli_profile, process_key = _profile_context(profile)
    with _LOCK:
        proc = _PROCESSES.get(process_key)
        if proc is not None and proc.poll() is None:
            return _public_status(profile, "running", True)
        if proc is not None:
            code = proc.poll()
            _PROCESSES.pop(process_key, None)
            _FAILURES[process_key] = {"fatal": code == 78, "at": time.monotonic()}
            return _public_status(profile, "failed", False)
        if process_key in _FAILURES:
            return _public_status(profile, "failed", False)
    return _public_status(profile, "stopped", False)


def start_profile_gateway(profile: str, *, require_enabled: bool = False) -> dict:
    profile = _validate_profile(profile)
    home, cli_profile, process_key = _profile_context(profile)
    with _LOCK:
        if not require_enabled:
            _FAILURES.pop(process_key, None)
        current = _PROCESSES.get(process_key)
        if current is not None and current.poll() is None:
            return _public_status(profile, "running", True)
        if current is not None:
            code = current.poll()
            _PROCESSES.pop(process_key, None)
            _FAILURES[process_key] = {"fatal": code == 78, "at": time.monotonic()}
        failure = _FAILURES.get(process_key)
        if require_enabled and failure:
            if failure.get("fatal") or time.monotonic() - failure.get("at", 0) < 30:
                return _public_status(profile, "failed", False)

        if require_enabled and not _env_enabled(home):
            return _public_status(profile, "stopped", False)
        home.mkdir(parents=True, exist_ok=True)
        cmd = [_resolve_hermes_command()]
        if cli_profile is not None:
            cmd.extend(["--profile", cli_profile])
        cmd.extend(["gateway", "run", "--external-supervisor"])
        env = _profile_child_env(home)

        log_path = home / "gateway-webui.log"
        log_handle = log_path.open("ab", buffering=0)
        try:
            os.chmod(log_path, 0o600)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(home),
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_handle.close()
        _PROCESSES[process_key] = proc
        time.sleep(0.25)
        if proc.poll() is not None:
            code = proc.poll()
            _PROCESSES.pop(process_key, None)
            _FAILURES[process_key] = {"fatal": code == 78, "at": time.monotonic()}
            logger.warning("Gateway for profile %s exited during startup (code %s)", profile, code)
            return _public_status(profile, "failed", False)
        _FAILURES.pop(process_key, None)
        logger.info("Started WebUI-managed gateway for profile %s", profile)
        return _public_status(profile, "running", True)


def _terminate_process(proc: subprocess.Popen) -> None:
    """Terminate the gateway session, falling back to portable process methods."""
    terminated_group = False
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            terminated_group = True
        except (AttributeError, OSError):
            pass
    if not terminated_group:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        killed_group = False
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                killed_group = True
            except (AttributeError, OSError):
                pass
        if not killed_group:
            proc.kill()
        proc.wait(timeout=5)


def stop_profile_gateway(profile: str) -> dict:
    profile = _validate_profile(profile)
    _home, _cli_profile, process_key = _profile_context(profile)
    with _LOCK:
        proc = _PROCESSES.pop(process_key, None)
        _FAILURES.pop(process_key, None)
        if proc is None or proc.poll() is not None:
            return _public_status(profile, "stopped", False)
        _terminate_process(proc)
    logger.info("Stopped WebUI-managed gateway for profile %s", profile)
    return _public_status(profile, "stopped", False)


def restart_profile_gateway(profile: str) -> dict:
    profile = _validate_profile(profile)
    with _LOCK:
        stop_profile_gateway(profile)
        return start_profile_gateway(profile)


def start_enabled_gateways() -> list[dict]:
    results = []
    for row in list_profiles_api():
        try:
            profile = _validate_profile(row.get("name"))
            home = Path(row.get("path") or "")
            if home.is_dir() and _env_enabled(home):
                results.append(start_profile_gateway(profile, require_enabled=True))
        except Exception as exc:
            logger.warning("Could not start configured profile gateway: %s", exc)
    return results


def _monitor_loop() -> None:
    while not _MONITOR_STOP.wait(10):
        try:
            start_enabled_gateways()
        except Exception:
            logger.exception("Profile gateway monitor iteration failed")


def start_gateway_supervisor() -> None:
    global _MONITOR_THREAD
    start_enabled_gateways()
    with _LOCK:
        if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
            return
        _MONITOR_STOP.clear()
        _MONITOR_THREAD = threading.Thread(
            target=_monitor_loop, name="channel-gateway-supervisor", daemon=True
        )
        _MONITOR_THREAD.start()


def stop_gateway_supervisor() -> None:
    global _MONITOR_THREAD
    _MONITOR_STOP.set()
    thread = _MONITOR_THREAD
    if thread is not None:
        thread.join(timeout=3)
    _MONITOR_THREAD = None
    with _LOCK:
        processes = list(_PROCESSES.values())
        _PROCESSES.clear()
        _FAILURES.clear()
        for proc in processes:
            try:
                if proc.poll() is None:
                    _terminate_process(proc)
            except Exception:
                logger.exception("Failed stopping profile gateway")
