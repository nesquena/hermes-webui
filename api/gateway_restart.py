"""Helpers for restarting the active-profile Hermes gateway."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from api.agent_cli import source_agent_cli_invocation
from api.gateway_authority import (
    REMOTE_GATEWAY_CONTROL_ERROR_CODE,
    RemoteGatewayControlUnsupported,
    require_local_gateway_control,
)
from api.profiles import (
    _PROFILE_ID_RE,
    _is_root_profile,
    get_active_hermes_home,
    get_active_profile_name,
    get_hermes_home_for_profile,
)

logger = logging.getLogger(__name__)

_GATEWAY_RESTART_LOCK = threading.Lock()


def _resolve_hermes_invocation() -> tuple[list[str], Path | None]:
    """Resolve the CLI invocation used for active-profile gateway restarts."""
    hermes_cmd = shutil.which("hermes")
    if hermes_cmd:
        return [hermes_cmd], None

    sibling = Path(sys.executable).parent / "hermes"
    if sibling.exists():
        return [str(sibling)], None
    try:
        return source_agent_cli_invocation()
    except FileNotFoundError:
        return ["hermes"], None


def _consume_stream(stream) -> None:
    """Drain a subprocess stream to prevent stdout/stderr pipe deadlocks."""
    try:
        while stream and stream.read(4096):
            pass
    except Exception:
        pass


def _release_lock() -> None:
    try:
        _GATEWAY_RESTART_LOCK.release()
    except RuntimeError:
        # The lock may already have been released by another path.
        pass


def _gateway_restart_profile_context(profile: str | None = None) -> tuple[Path, str | None]:
    """Return the HERMES_HOME and CLI profile arg for a gateway restart."""
    if profile is None:
        raw_profile = str(get_active_profile_name() or "default").strip()
        active_home = Path(get_active_hermes_home())
    else:
        raw_profile = str(profile or "")
        if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile):
            raise ValueError(f"Invalid profile for gateway restart: {profile!r}")
        active_home = Path(get_hermes_home_for_profile(raw_profile))

    if (
        raw_profile == "default"
        and active_home.name == "default"
        and active_home.parent.name == "profiles"
    ):
        return active_home, None
    if not raw_profile or not _PROFILE_ID_RE.fullmatch(raw_profile) or _is_root_profile(raw_profile):
        return active_home, "default"
    return active_home, raw_profile


def restart_active_profile_gateway(
    *,
    profile: str | None = None,
    quick_timeout_seconds: float = 2.0,
    background_wait_seconds: float = 240.0,
) -> dict:
    """Run a non-blocking ``hermes gateway restart`` for the active profile.

    Returns a short status dict with these values:
    - completed: command finished quickly and succeeded.
    - in_progress: command did not finish within ``quick_timeout_seconds``.
    - failed: command finished quickly with non-zero exit status.
    - busy: restart already in progress from another caller.
    - unsupported: the configured gateway is owned by a remote runtime.
    """
    try:
        require_local_gateway_control()
    except RemoteGatewayControlUnsupported as exc:
        logger.info("Declining local gateway restart for remote gateway authority")
        return {
            "status": "unsupported",
            "message": str(exc),
            "error_code": REMOTE_GATEWAY_CONTROL_ERROR_CODE,
        }

    if not _GATEWAY_RESTART_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "message": "Restart already in progress. Please wait a moment and try again.",
        }

    try:
        active_home, cli_profile = _gateway_restart_profile_context(profile)
        require_local_gateway_control()
        env = os.environ.copy()
        env["HERMES_HOME"] = str(active_home)
        cmd, cli_cwd = _resolve_hermes_invocation()
        if cli_profile is not None:
            cmd.extend(["--profile", cli_profile])
        cmd.extend(["gateway", "restart"])

        if cli_profile is None:
            logger.info(
                "Restarting gateway service via CLI command: %s gateway restart (HERMES_HOME=%s)",
                cmd[:-2],
                active_home,
            )
        else:
            logger.info(
                "Restarting gateway service via CLI command: %s --profile %s gateway restart (HERMES_HOME=%s)",
                cmd[:-4],
                cli_profile,
                active_home,
            )
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": env,
        }
        if cli_cwd is not None:
            popen_kwargs["cwd"] = str(cli_cwd)
        proc = subprocess.Popen(cmd, **popen_kwargs)

        try:
            stdout, stderr = proc.communicate(timeout=quick_timeout_seconds)
            _release_lock()
            stdout = (stdout or "").strip()
            stderr = (stderr or "").strip()
            if proc.returncode == 0:
                logger.info("Gateway service restarted successfully: %s", stdout)
                return {
                    "status": "completed",
                    "message": "Gateway service restarted successfully",
                    "detail": stdout or stderr,
                }

            logger.error("Gateway service restart failed with code %s: %s", proc.returncode, stderr)
            return {
                "status": "failed",
                "message": f"Restart failed: {stderr or stdout}",
                "detail": stdout or stderr,
                "returncode": proc.returncode,
            }

        except subprocess.TimeoutExpired:
            logger.info(
                "Gateway restart is taking longer than %.1fs (likely draining in-flight runs);"
                " continuing in background",
                quick_timeout_seconds,
            )

            threading.Thread(target=_consume_stream, args=(proc.stdout,), daemon=True).start()
            threading.Thread(target=_consume_stream, args=(proc.stderr,), daemon=True).start()

            def _wait_and_release() -> None:
                try:
                    proc.wait(timeout=background_wait_seconds)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "Gateway restart process timed out after %.1fs. Terminating process.",
                        background_wait_seconds,
                    )
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            try:
                                proc.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                logger.error(
                                    "Gateway restart process refused to die even after SIGKILL.",
                                )
                    except Exception:
                        logger.exception("Failed to terminate timed out gateway restart process.")
                finally:
                    _release_lock()

            threading.Thread(target=_wait_and_release, daemon=True).start()
            return {
                "status": "in_progress",
                "message": "Gateway service restart initiated (in progress)",
            }
    except RemoteGatewayControlUnsupported as exc:
        _release_lock()
        logger.info("Declining local gateway restart for remote gateway deployment")
        return {
            "status": "unsupported",
            "message": str(exc),
            "error_code": REMOTE_GATEWAY_CONTROL_ERROR_CODE,
        }
    except Exception as exc:
        _release_lock()
        logger.exception("Failed to run gateway restart command")
        return {
            "status": "failed",
            "message": f"Internal error running restart: {type(exc).__name__}: {exc}",
        }
