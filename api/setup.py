"""
Fox in the Box — Onboarding setup API handlers.

Routes:
  GET  /setup                      → serve setup.html
  POST /api/setup/openrouter       → validate + save OpenRouter API key
  POST /api/setup/tailscale/start  → start tailscale login background process
  GET  /api/setup/tailscale/status → return current tailscale auth state
  POST /api/setup/complete         → write onboarding.json with completed=true

All paths under /api/setup/ are exempt from the onboarding redirect middleware.
Environment variables (read at request time, not module import time):
  ONBOARDING_PATH  — path to onboarding.json   (default: /data/config/onboarding.json)
  HERMES_ENV_PATH  — path to hermes.env         (default: /data/config/hermes.env)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from api.helpers import j, bad


# ── Module-level Tailscale state (reset between server restarts) ──────────────

_tailscale_state: dict = {
    "status": "waiting",
    "login_url": None,
    "tailnet_url": None,
    "error": None,
}
_tailscale_proc: subprocess.Popen | None = None
_tailscale_popen_cls: type | None = None  # tracks which Popen class spawned the proc
_tailscale_lock = threading.Lock()


# ── Path helpers (read env vars at call time) ─────────────────────────────────

def _onboarding_path() -> Path:
    return Path(os.environ.get("ONBOARDING_PATH", "/data/config/onboarding.json"))


def _hermes_env_path() -> Path:
    configured = os.environ.get("HERMES_ENV_PATH")
    if configured:
        return Path(configured)
    default = Path("/data/config/hermes.env")
    # Fall back to a writable temp location when /data isn't available (dev/test)
    if not default.parent.exists():
        import tempfile
        return Path(tempfile.gettempdir()) / "fitb-hermes.env"
    return default


# ── Redirect middleware helper ────────────────────────────────────────────────

def onboarding_complete() -> bool:
    """Return True if onboarding has been completed (reads disk on every call)."""
    try:
        return json.loads(_onboarding_path().read_text(encoding="utf-8")).get("completed", False)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return False


# ── Route handlers ────────────────────────────────────────────────────────────

def handle_get_setup(handler) -> None:
    """GET /setup — serve setup.html."""
    # static/ is relative to the server.py location (repo root)
    static_dir = Path(__file__).resolve().parents[1] / "static"
    html_path = static_dir / "setup.html"
    try:
        content = html_path.read_bytes()
    except FileNotFoundError:
        handler.send_response(404)
        handler.end_headers()
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def handle_post_openrouter(handler, body: bytes) -> None:
    """POST /api/setup/openrouter — validate + persist OpenRouter API key."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return j(handler, {"ok": False, "error": "Invalid JSON body"}, status=400)

    key = payload.get("key", "")

    # Validate — never log the key value
    if not key or not isinstance(key, str):
        return j(handler, {"ok": False, "error": "API key is required"}, status=400)
    if len(key) > 512:
        return j(handler, {"ok": False, "error": "API key is too long"}, status=400)
    if not key.startswith("sk-"):
        return j(handler, {"ok": False, "error": "API key must start with sk-"}, status=400)

    # Write to hermes.env
    env_path = _hermes_env_path()
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        # Read existing content, replace or append OPENROUTER_API_KEY
        existing = ""
        if env_path.exists():
            existing = env_path.read_text(encoding="utf-8")
        lines = [l for l in existing.splitlines() if not l.startswith("OPENROUTER_API_KEY=")]
        lines.append(f"OPENROUTER_API_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        return j(handler, {"ok": False, "error": f"Failed to write env file: {exc}"}, status=500)

    return j(handler, {"ok": True})


def _tailscale_worker(proc: subprocess.Popen) -> None:
    """Background thread: read tailscale login stdout, update state."""
    global _tailscale_state
    import re

    url_pattern = re.compile(r"https://login\.tailscale\.com/\S+")

    try:
        for line in proc.stdout:
            line = line.strip() if isinstance(line, str) else line.decode("utf-8", errors="replace").strip()
            m = url_pattern.search(line)
            if m:
                with _tailscale_lock:
                    _tailscale_state["login_url"] = m.group(0)
                    _tailscale_state["status"] = "url_ready"

        proc.wait()
        if proc.returncode == 0:
            # Try to get tailnet URL
            try:
                result = subprocess.run(
                    ["tailscale", "status", "--json"],
                    capture_output=True, text=True, timeout=5
                )
                data = json.loads(result.stdout)
                dns_name = data.get("Self", {}).get("DNSName", "").rstrip(".")
                if dns_name:
                    with _tailscale_lock:
                        _tailscale_state["tailnet_url"] = f"https://{dns_name}"
                        _tailscale_state["status"] = "connected"
                else:
                    with _tailscale_lock:
                        _tailscale_state["status"] = "connected"
            except Exception:
                with _tailscale_lock:
                    _tailscale_state["status"] = "connected"
        else:
            with _tailscale_lock:
                _tailscale_state["status"] = "error"
                _tailscale_state["error"] = f"tailscale login exited with code {proc.returncode}"
    except Exception as exc:
        with _tailscale_lock:
            _tailscale_state["status"] = "error"
            _tailscale_state["error"] = str(exc)


def handle_post_tailscale_start(handler) -> None:
    """POST /api/setup/tailscale/start — start tailscale login background process."""
    global _tailscale_proc, _tailscale_state, _tailscale_popen_cls

    with _tailscale_lock:
        # Idempotent: only skip spawning if the proc was started by the *same*
        # Popen class that's currently active (handles test isolation where
        # @patch("subprocess.Popen") replaces the class per-test).
        current_popen = subprocess.Popen
        if (
            _tailscale_proc is not None
            and _tailscale_proc.poll() is None
            and _tailscale_popen_cls is current_popen
        ):
            return j(handler, {"ok": True})

        # Reset state and spawn
        _tailscale_state = {
            "status": "waiting",
            "login_url": None,
            "tailnet_url": None,
            "error": None,
        }
        _tailscale_proc = subprocess.Popen(
            ["tailscale", "login", "--timeout=120"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _tailscale_popen_cls = current_popen

    t = threading.Thread(target=_tailscale_worker, args=(_tailscale_proc,), daemon=True)
    t.start()

    return j(handler, {"ok": True})


def handle_get_tailscale_status(handler) -> None:
    """GET /api/setup/tailscale/status — return current tailscale auth state."""
    with _tailscale_lock:
        state = dict(_tailscale_state)
    return j(handler, state)


def handle_post_complete(handler, body: bytes) -> None:
    """POST /api/setup/complete — mark onboarding as done."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}

    tailscale_connected = bool(payload.get("tailscale_connected", False))

    onboarding_path = _onboarding_path()
    try:
        onboarding_path.parent.mkdir(parents=True, exist_ok=True)
        onboarding_path.write_text(
            json.dumps({
                "completed": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "tailscale_connected": tailscale_connected,
            }),
            encoding="utf-8",
        )
    except OSError as exc:
        return bad(handler, f"Failed to write onboarding file: {exc}", 500)

    return j(handler, {"ok": True})


def handle_post_restart(handler) -> None:
    """POST /api/setup/restart — restart hermes services via supervisorctl."""
    try:
        result = subprocess.run(
            ["supervisorctl", "-c", "/etc/supervisor/supervisord.conf",
             "restart", "hermes-gateway", "hermes-webui"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return j(handler, {"ok": True})
        return j(handler, {"ok": False, "error": result.stderr or result.stdout}, status=500)
    except Exception as exc:
        return j(handler, {"ok": False, "error": str(exc)}, status=500)
