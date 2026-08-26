"""Gateway supervisor for single-container Docker deployments.

This module provides automatic gateway process supervision when Hermes WebUI
runs in a single-container configuration. In multi-container deployments with
s6-overlay, the external supervisor handles gateway lifecycle and this module
automatically disables itself.

The supervisor monitors gateway health and automatically restarts the process
when crashes are detected, using exponential backoff to prevent rapid restart
loops.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

# States that indicate the gateway should be (re)started automatically.
# Matches container_boot.py's _AUTOSTART_STATES plus transient running states.
_AUTOSTART_STATES = frozenset({"running"})
_TRANSIENT_RUNNING_STATES = frozenset({"draining", "degraded"})

# Module-level singleton for integration with server.py
_supervisor: "GatewaySupervisor | None" = None
_supervisor_lock = threading.Lock()


def _find_supervised_profiles() -> list[tuple[str, Path]]:
    """Find profiles whose gateway should be supervised.

    Unlike ``get_active_profile_name()`` (which requires a request context
    via cookie), this works at server startup by scanning the profiles
    directory.  Returns a list of ``(profile_name, profile_home)`` tuples
    for every profile whose ``gateway_state.json`` indicates the gateway
    was running.

    In single-container mode there is typically exactly one such profile.
    Multi-profile multiplexing is handled by the agent itself; we just
    ensure the running one stays alive.
    """
    results: list[tuple[str, Path]] = []
    try:
        # The agent home root (where profiles/ lives) is HERMES_REAL_HOME/.hermes
        # in the container. HERMES_WEBUI_STATE_DIR points at the WebUI's own
        # state subdirectory (.hermes/webui), NOT the agent root.
        real_home = os.environ.get("HERMES_REAL_HOME", "")
        candidates = []
        if real_home:
            candidates.append(Path(real_home) / ".hermes")
        # Also check $HOME/.hermes for non-container dev environments
        home_dir = os.environ.get("HOME", "")
        if home_dir:
            candidates.append(Path(home_dir) / ".hermes")
        # Fall back to the standard location
        candidates.append(Path("/home/hermeswebui/.hermes"))

        profiles_root = None
        for candidate in candidates:
            if (candidate / "profiles").is_dir():
                profiles_root = candidate / "profiles"
                break

        if profiles_root is None:
            return results

        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            state_file = entry / "gateway_state.json"
            if not state_file.exists():
                continue
            try:
                data = json.loads(state_file.read_text())
                state = data.get("desired_state") or data.get("gateway_state")
                if state in _AUTOSTART_STATES or state in _TRANSIENT_RUNNING_STATES:
                    results.append((entry.name, entry))
            except (json.JSONDecodeError, OSError):
                continue
    except Exception:
        logger.debug("Error scanning profiles for supervision", exc_info=True)
    return results


def should_enable_supervisor() -> bool:
    """Check if gateway supervisor should be enabled.
    
    Returns:
        True if supervisor should run, False otherwise.
        
    Logic:
        1. If HERMES_GATEWAY_SUPERVISOR_ENABLED is set, respect it explicitly.
        2. If running under s6-overlay (multi-container), disable supervisor.
        3. Default to True in single-container environments.
    """
    env_val = os.environ.get("HERMES_GATEWAY_SUPERVISOR_ENABLED", "").strip().lower()
    
    # Explicit enable/disable via environment variable
    if env_val in ("true", "1", "yes"):
        return True
    if env_val in ("false", "0", "no"):
        return False
    
    # Auto-detect s6-overlay presence (multi-container mode)
    if Path("/run/service").exists():
        logger.debug("s6-overlay detected via /run/service - disabling supervisor")
        return False
    
    if os.environ.get("S6_STAGE2_HOOK"):
        logger.debug("s6-overlay detected via S6_STAGE2_HOOK - disabling supervisor")
        return False
    
    # Default: enable in single-container mode
    return True


class CrashLoopBackoff:
    """Exponential backoff tracker for gateway crash recovery.
    
    Tracks consecutive failures and calculates exponentially increasing
    wait times to prevent rapid restart loops while still recovering
    quickly from transient failures.
    """
    
    def __init__(self) -> None:
        self.consecutive_failures: int = 0
        self.backoff_seconds: float = 5.0
        self.last_start_time: float | None = None
        self._min_backoff: float = 5.0
        self._max_backoff: float = 300.0
        self._successful_run_threshold: float = 600.0
    
    def record_start(self) -> None:
        """Record a successful gateway start."""
        self.last_start_time = time.time()
    
    def record_crash(self) -> float:
        """Record a gateway crash and return the backoff wait time in seconds.
        
        Returns:
            Number of seconds to wait before next restart attempt.
        """
        # Reset backoff if the gateway ran successfully for a while
        if self.last_start_time is not None:
            uptime = time.time() - self.last_start_time
            if uptime > self._successful_run_threshold:
                logger.info(
                    "Gateway ran for %.1fs before crash - resetting backoff",
                    uptime
                )
                self.consecutive_failures = 0
                self.backoff_seconds = self._min_backoff
        
        self.consecutive_failures += 1
        current_backoff = self.backoff_seconds
        
        # Double the backoff for next time, capped at max
        self.backoff_seconds = min(self.backoff_seconds * 2, self._max_backoff)
        
        logger.warning(
            "Gateway crash #%d - waiting %.1fs before restart (next backoff: %.1fs)",
            self.consecutive_failures,
            current_backoff,
            self.backoff_seconds
        )
        
        return current_backoff


class GatewaySupervisor:
    """Daemon thread that supervises the Hermes gateway process.
    
    Monitors gateway health via HTTP health checks and automatically
    restarts the process when failures are detected, coordinating with
    the WebUI's restart lock to prevent conflicts.
    """
    
    def __init__(self, check_interval: float = 30.0) -> None:
        """Initialize the gateway supervisor.
        
        Args:
            check_interval: Seconds between health checks (default 30s)
        """
        self._check_interval = check_interval
        self._stop_event = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._backoff = CrashLoopBackoff()
    
    def start(self) -> None:
        """Start the supervisor daemon thread."""
        if self._supervisor_thread is not None:
            logger.warning("Supervisor already started")
            return
        
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="gateway-supervisor",
            daemon=True
        )
        self._supervisor_thread.start()
        logger.info("Gateway supervisor started (check_interval=%.1fs)", self._check_interval)
    
    def stop(self) -> None:
        """Stop the supervisor daemon thread."""
        self._stop_event.set()
        if self._supervisor_thread is not None:
            self._supervisor_thread.join(timeout=5.0)
    
    def _supervisor_loop(self) -> None:
        """Main supervisor loop: check health and restart on failure."""
        try:
            # Check if we should auto-start the gateway on supervisor initialization
            if self._should_auto_start():
                logger.info("Gateway auto-start enabled - checking initial health")
                if not self._is_gateway_healthy():
                    logger.info("Gateway not healthy on startup - initiating start")
                    self._start_gateway()
            else:
                logger.info("Gateway auto-start disabled or stopped state detected")
            
            # Main monitoring loop
            while not self._stop_event.wait(self._check_interval):
                if not self._is_gateway_healthy():
                    logger.warning("Gateway health check failed")
                    wait_seconds = self._backoff.record_crash()
                    
                    # Wait with ability to be interrupted by stop event
                    if self._stop_event.wait(wait_seconds):
                        break
                    
                    self._start_gateway()
        except Exception:
            logger.exception("Gateway supervisor loop crashed")
    
    def _should_auto_start(self) -> bool:
        """Check if any profile's gateway should be auto-started.

        Scans the profiles directory for gateway_state.json files rather
        than relying on request-context profile resolution, so this works
        during server startup (no cookie / no request thread).

        Returns:
            True if at least one profile has gateway_state == running.
        """
        profiles = _find_supervised_profiles()
        if profiles:
            names = ", ".join(name for name, _ in profiles)
            logger.info("Found %d profile(s) with running gateway: %s", len(profiles), names)
            return True
        return False
    
    def _is_gateway_healthy(self) -> bool:
        """Check if the gateway process is healthy via HTTP health check.
        
        Returns:
            True if gateway responds with HTTP 200, False otherwise.
        """
        try:
            req = Request("http://127.0.0.1:8642/health")
            with urlopen(req, timeout=5.0) as response:
                return response.status == 200
        except URLError:
            return False
        except Exception:
            logger.debug("Gateway health check error", exc_info=True)
            return False
    
    def _start_gateway(self) -> None:
        """Start the gateway process for the first running profile.

        Scans profiles (works without request context), acquires the
        restart lock to coordinate with WebUI-initiated restarts, then
        launches ``hermes --profile <name> gateway run --no-supervise``
        as a detached process.
        """
        try:
            from api.gateway_restart import (
                _GATEWAY_RESTART_LOCK,
                _resolve_hermes_command,
                _consume_stream,
            )

            # Find the profile whose gateway should be running.
            profiles = _find_supervised_profiles()
            if not profiles:
                logger.info("No profiles with running gateway state - not starting")
                return

            profile_name, profile_home = profiles[0]

            # Try to acquire restart lock - skip if WebUI is restarting
            if not _GATEWAY_RESTART_LOCK.acquire(blocking=False):
                logger.info("Restart lock held by WebUI - skipping supervisor restart")
                return

            try:
                hermes_cmd = _resolve_hermes_command()
                cmd = [hermes_cmd, "--profile", profile_name, "gateway", "run", "--no-supervise"]

                env = os.environ.copy()
                env["HERMES_HOME"] = str(profile_home)

                logger.info(
                    "Starting gateway: %s (HERMES_HOME=%s)",
                    " ".join(cmd),
                    profile_home,
                )

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )

                time.sleep(3.0)
                returncode = proc.poll()

                if returncode is None:
                    self._backoff.record_start()
                    logger.info("Gateway process started successfully (pid=%s)", proc.pid)

                    threading.Thread(
                        target=_consume_stream, args=(proc.stdout,), daemon=True,
                    ).start()
                    threading.Thread(
                        target=_consume_stream, args=(proc.stderr,), daemon=True,
                    ).start()
                else:
                    stdout = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
                    stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                    logger.error(
                        "Gateway exited immediately (code=%s)\nstdout: %s\nstderr: %s",
                        returncode, stdout, stderr,
                    )
            finally:
                try:
                    _GATEWAY_RESTART_LOCK.release()
                except RuntimeError:
                    pass

        except Exception:
            logger.exception("Failed to start gateway process")


def stop_supervisor() -> None:
    """Stop the global supervisor instance if running.
    
    Called from server.py shutdown sequence.
    """
    global _supervisor
    with _supervisor_lock:
        if _supervisor is not None:
            _supervisor.stop()
            _supervisor = None


def start_supervisor() -> None:
    """Start the global supervisor instance.
    
    Called from server.py startup sequence.
    """
    global _supervisor
    with _supervisor_lock:
        if _supervisor is None:
            _supervisor = GatewaySupervisor()
            _supervisor.start()
