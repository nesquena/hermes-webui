"""Opt-in guarded restart support for the Hermes WebUI server.

The WebUI process cannot reliably prove its own restart completed after it has
been replaced or stopped by a supervisor. This module therefore persists a small
semantic status record under the WebUI state directory. A freshly-started server
can read that record and report that the process is reachable again without
exposing local commands or supervisor details to the browser.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_BOOT_TIME = time.time()
_IDLE_WAIT_TIMEOUT_SECONDS = 300
_IDLE_POLL_SECONDS = 1.0
_COMMAND_TIMEOUT_SECONDS = 120
_HEALTH_CONFIRM_TIMEOUT_SECONDS = 120
_IN_PROGRESS_STATUSES = {"waiting_idle", "restarting", "checking_health"}
_RESTART_LOCK = threading.Lock()


def _state_dir() -> Path:
    raw = os.getenv("HERMES_WEBUI_STATE_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    try:
        from api.config import STATE_DIR
        return Path(STATE_DIR).expanduser().resolve()
    except Exception:
        return (Path.home() / ".hermes" / "webui").resolve()


def _status_dir() -> Path:
    return _state_dir() / "server_restart"


def _status_path(restart_id: str = "latest") -> Path:
    safe = "".join(ch for ch in str(restart_id or "latest") if ch.isalnum() or ch in {"-", "_"})
    return _status_dir() / f"{safe or 'latest'}.json"


def _now() -> float:
    return time.time()


def _elapsed(started_at: Any) -> int:
    try:
        return max(0, int(round(_now() - float(started_at))))
    except Exception:
        return 0


def _restart_command() -> list[str] | None:
    raw = (os.getenv("HERMES_WEBUI_RESTART_COMMAND") or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    return parts or None


def restart_config() -> dict[str, Any]:
    if not _restart_command():
        return {"enabled": False, "reason": "not_configured"}
    return {"enabled": True, "strategy": "command"}


def restart_safety_snapshot() -> dict[str, int]:
    active_streams = 0
    active_runs = 0
    try:
        from api import config as cfg
        with cfg.STREAMS_LOCK:
            active_streams = len(cfg.STREAMS or {})
        with cfg.ACTIVE_RUNS_LOCK:
            active_runs = len(cfg.ACTIVE_RUNS or {})
    except Exception:
        active_streams = 0
        active_runs = 0
    return {"active_streams": int(active_streams), "active_runs": int(active_runs)}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_status(payload: dict[str, Any], *, restart_id: str | None = None) -> dict[str, Any]:
    rid = restart_id or str(payload.get("restart_id") or "latest")
    payload = dict(payload)
    payload.setdefault("restart_id", rid)
    payload.setdefault("updated_at", _now())
    payload["elapsed_seconds"] = _elapsed(payload.get("started_at") or payload.get("updated_at"))
    directory = _status_dir()
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    current_path = directory / f"{rid}.json"
    latest_path = directory / "latest.json"
    _atomic_write(current_path, text)
    _atomic_write(latest_path, text)
    return payload


def read_restart_status(restart_id: str = "latest") -> dict[str, Any]:
    path = _status_path(restart_id or "latest")
    if not path.exists() and restart_id != "latest":
        path = _status_path("latest")
    if not path.exists():
        return {"status": "idle", "elapsed_seconds": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "failed", "error": "status_unreadable", "elapsed_seconds": 0}
    if not isinstance(payload, dict):
        return {"status": "failed", "error": "status_invalid", "elapsed_seconds": 0}
    payload["elapsed_seconds"] = _elapsed(payload.get("started_at") or payload.get("updated_at"))
    # If a new WebUI process reads an in-progress status written before this
    # process booted, the browser has reached the replacement process. Promote
    # that old status to the terminal semantic state. The original process never
    # auto-promotes its own in-progress status just because a timer elapsed.
    try:
        started_at = float(payload.get("started_at") or 0.0)
    except Exception:
        started_at = 0.0
    if payload.get("status") in {"restarting", "checking_health"} and started_at and started_at < _BOOT_TIME:
        payload["status"] = "succeeded"
        payload["ok"] = True
        payload["updated_at"] = _now()
        payload = _write_status(payload, restart_id=str(payload.get("restart_id") or restart_id or "latest"))
    elif payload.get("status") == "waiting_idle" and started_at and started_at < _BOOT_TIME:
        payload["status"] = "failed"
        payload["ok"] = False
        payload["error"] = "restart_interrupted_before_idle"
        payload["updated_at"] = _now()
        payload = _write_status(payload, restart_id=str(payload.get("restart_id") or restart_id or "latest"))
    elif payload.get("status") == "checking_health":
        checking_started_at = payload.get("checking_started_at") or payload.get("updated_at") or payload.get("started_at")
        if _elapsed(checking_started_at) > _HEALTH_CONFIRM_TIMEOUT_SECONDS:
            payload["status"] = "failed"
            payload["ok"] = False
            payload["error"] = "restart_not_observed"
            payload["updated_at"] = _now()
            payload = _write_status(payload, restart_id=str(payload.get("restart_id") or restart_id or "latest"))
    return payload


def _run_restart_command(restart_id: str) -> None:
    status = read_restart_status(restart_id)
    command = _restart_command()
    if not command:
        status.update({"ok": False, "status": "unavailable", "reason": "not_configured", "updated_at": _now()})
        _write_status(status, restart_id=restart_id)
        return
    status.update({"status": "restarting", "command_started_at": _now(), "updated_at": _now()})
    _write_status(status, restart_id=restart_id)
    try:
        proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_COMMAND_TIMEOUT_SECONDS, check=False)
    except Exception as exc:
        status.update({"ok": False, "status": "failed", "error": type(exc).__name__, "updated_at": _now()})
        _write_status(status, restart_id=restart_id)
        return
    if proc.returncode == 0:
        # A zero exit code means the configured action accepted the restart
        # request. It does not prove that this WebUI process was replaced.
        # Keep the status non-terminal until a freshly booted process promotes
        # it, or until read_restart_status() marks the restart unobserved.
        now = _now()
        status.update({"ok": True, "status": "checking_health", "exit_code": 0, "checking_started_at": now, "updated_at": now})
    else:
        status.update({"ok": False, "status": "failed", "exit_code": int(proc.returncode), "updated_at": _now()})
    _write_status(status, restart_id=restart_id)


def _wait_until_idle(restart_id: str, *, timeout_seconds: int = _IDLE_WAIT_TIMEOUT_SECONDS) -> bool:
    deadline = _now() + max(1, int(timeout_seconds))
    while True:
        snapshot = restart_safety_snapshot()
        status = read_restart_status(restart_id)
        status.update({"status": "waiting_idle", "updated_at": _now(), **snapshot})
        _write_status(status, restart_id=restart_id)
        if not int(snapshot.get("active_runs") or 0) and not int(snapshot.get("active_streams") or 0):
            return True
        if _now() >= deadline:
            status.update({"ok": False, "status": "failed", "error": "idle_timeout", "updated_at": _now(), **snapshot})
            _write_status(status, restart_id=restart_id)
            return False
        time.sleep(_IDLE_POLL_SECONDS)


def _restart_worker(restart_id: str) -> None:
    status = read_restart_status(restart_id)
    if status.get("status") == "waiting_idle" and not _wait_until_idle(restart_id):
        return
    status = read_restart_status(restart_id)
    status.update({"ok": True, "status": "restarting", "updated_at": _now()})
    _write_status(status, restart_id=restart_id)
    _run_restart_command(restart_id)


def _spawn_restart_worker(restart_id: str) -> None:
    thread = threading.Thread(target=_restart_worker, args=(restart_id,), daemon=True)
    thread.start()


def _current_in_progress_status() -> dict[str, Any] | None:
    status = read_restart_status("latest")
    if status.get("status") in _IN_PROGRESS_STATUSES and status.get("restart_id"):
        status["ok"] = True
        status["already_in_progress"] = True
        return status
    return None


def start_guarded_restart(*, reason: str = "settings") -> dict[str, Any]:
    cfg = restart_config()
    if not cfg.get("enabled"):
        return {"ok": False, "status": "unavailable", "reason": cfg.get("reason") or "not_configured"}

    with _RESTART_LOCK:
        current = _current_in_progress_status()
        if current:
            return current

        snapshot = restart_safety_snapshot()
        if int(snapshot.get("active_runs") or 0) or int(snapshot.get("active_streams") or 0):
            restart_id = str(uuid.uuid4())
            payload = {
                "ok": True,
                "restart_id": restart_id,
                "status": "waiting_idle",
                "reason": reason,
                "started_at": _now(),
                **snapshot,
            }
            _write_status(payload, restart_id=restart_id)
            _spawn_restart_worker(restart_id)
            return read_restart_status(restart_id)

        restart_id = str(uuid.uuid4())
        payload = {
            "ok": True,
            "restart_id": restart_id,
            "status": "restarting",
            "reason": reason,
            "started_at": _now(),
            **snapshot,
        }
        _write_status(payload, restart_id=restart_id)
        _spawn_restart_worker(restart_id)
        return read_restart_status(restart_id)
