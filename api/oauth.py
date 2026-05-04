"""In-app OAuth flow implementations for onboarding.

The browser receives only WebUI-local flow metadata (flow_id, user_code,
verification_uri, high-level status). Provider device/auth codes and OAuth
tokens stay server-side and are persisted to the active Hermes profile's
``auth.json`` credential_pool.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Compatibility for older helper tests and self-heal code that import these.
AUTH_JSON_PATH = Path.home() / ".hermes" / "auth.json"

CODEX_ISSUER = "https://auth.openai.com"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_VERIFICATION_URI = f"{CODEX_ISSUER}/codex/device"
CODEX_USER_CODE_URL = f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = f"{CODEX_ISSUER}/api/accounts/deviceauth/token"
CODEX_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_REDIRECT_URI = f"{CODEX_ISSUER}/deviceauth/callback"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_FLOW_MAX_WAIT_SECONDS = 15 * 60

_ALLOWED_ONBOARDING_OAUTH_PROVIDERS = {"openai-codex"}
_REJECTED_ONBOARDING_OAUTH_PROVIDERS = {
    "anthropic",
    "claude",
    "claude-code",
    "nous",
    "qwen-oauth",
    "gemini-cli",
    "google-gemini-cli",
    "minimax",
    "minimax-oauth",
    "copilot",
    "copilot-acp",
}

_OAUTH_FLOWS: dict[str, dict[str, Any]] = {}
_OAUTH_FLOWS_LOCK = threading.Lock()


def _get_active_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


# ── legacy auth.json helpers ────────────────────────────────────────────────

def _read_auth_json(auth_path: Path | None = None) -> dict[str, Any]:
    """Read auth.json and return parsed dict, or an empty compatible store."""
    path = auth_path or AUTH_JSON_PATH
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse %s: %s", path, exc)
            return {}
    return {}


def read_auth_json():
    """Public wrapper for streaming credential self-heal code."""
    return _read_auth_json()


def _write_auth_json(data: dict[str, Any], auth_path: Path | None = None) -> Path:
    """Atomically write auth.json with owner-only permissions.

    OAuth access/refresh tokens live in this file. The temp file is chmod 0600
    before rename so the final path never inherits a permissive process umask.
    """
    path = auth_path or AUTH_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError as exc:
            logger.warning("Failed to chmod 0600 on %s: %s", tmp, exc)
        tmp.replace(path)
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return path
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _persist_codex_credentials(hermes_home: Path, token_data: dict[str, Any]) -> Path:
    """Persist Codex OAuth credentials to active-profile auth.json."""
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError("Codex token exchange did not return an access_token")

    auth_path = Path(hermes_home) / "auth.json"
    auth = _read_auth_json(auth_path)
    auth.setdefault("version", 1)
    pool = auth.setdefault("credential_pool", {})
    if not isinstance(pool, dict):
        pool = {}
        auth["credential_pool"] = pool
    entries = pool.setdefault("openai-codex", [])
    if not isinstance(entries, list):
        entries = []
        pool["openai-codex"] = entries

    now = _now_iso()
    entry = None
    for candidate in entries:
        if isinstance(candidate, dict) and candidate.get("source") == "manual:device_code":
            entry = candidate
            break
    if entry is None:
        entry = {
            "id": "codex-oauth-" + uuid.uuid4().hex[:12],
            "label": "Codex OAuth",
            "auth_type": "oauth",
            "priority": 0,
            "source": "manual:device_code",
            "base_url": CODEX_BASE_URL,
            "created_at": now,
        }
        entries.insert(0, entry)

    entry.update(
        {
            "label": "Codex OAuth",
            "auth_type": "oauth",
            "priority": 0,
            "source": "manual:device_code",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "base_url": CODEX_BASE_URL,
            "last_refresh": now,
            "updated_at": now,
        }
    )
    auth["updated_at"] = now
    path = _write_auth_json(auth, auth_path)

    try:
        from api.config import invalidate_credential_pool_cache

        invalidate_credential_pool_cache("openai-codex")
    except Exception:
        logger.debug("Failed to invalidate openai-codex credential cache", exc_info=True)

    return path


# Backward-compatible wrapper used by older code/tests.
def _save_codex_credentials(token_data):
    return _persist_codex_credentials(_get_active_hermes_home(), token_data)


# ── Codex protocol ──────────────────────────────────────────────────────────

def _json_request(url: str, payload: dict[str, Any], *, form: bool = False) -> dict[str, Any]:
    if form:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _request_codex_user_code() -> dict[str, Any]:
    return _json_request(CODEX_USER_CODE_URL, {"client_id": CODEX_CLIENT_ID})


def _poll_codex_authorization(device_auth_id: str, user_code: str) -> dict[str, Any] | None:
    try:
        return _json_request(
            CODEX_DEVICE_TOKEN_URL,
            {"device_auth_id": device_auth_id, "user_code": user_code},
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return None
        raise


def _exchange_codex_authorization(authorization_code: str, code_verifier: str) -> dict[str, Any]:
    return _json_request(
        CODEX_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": CODEX_REDIRECT_URI,
            "client_id": CODEX_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        form=True,
    )


def _public_start_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "provider": "openai-codex",
        "flow_id": flow_id,
        "status": flow.get("status", "pending"),
        "verification_uri": CODEX_VERIFICATION_URI,
        "user_code": flow.get("user_code", ""),
        "expires_at": flow.get("expires_at"),
        "poll_interval_seconds": flow.get("poll_interval_seconds", 5),
    }


def _public_status_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "ok": True,
        "provider": "openai-codex",
        "flow_id": flow_id,
        "status": flow.get("status", "error"),
    }
    if flow.get("status") == "error" and flow.get("error"):
        payload["error"] = str(flow.get("error"))[:200]
    return payload


def _drop_sensitive_flow_fields(flow: dict[str, Any]) -> None:
    for key in (
        "device_auth_id",
        "authorization_code",
        "code_verifier",
        "access_token",
        "refresh_token",
        "token_data",
    ):
        flow.pop(key, None)


def _cleanup_oauth_flows(now: float | None = None) -> None:
    now = now or time.time()
    cutoff = now - 300
    with _OAUTH_FLOWS_LOCK:
        for fid, flow in list(_OAUTH_FLOWS.items()):
            status = flow.get("status")
            if status == "pending" and float(flow.get("expires_at") or 0) <= now:
                flow["status"] = "expired"
                _drop_sensitive_flow_fields(flow)
            if status in {"success", "expired", "cancelled", "error"} and float(flow.get("updated_at") or 0) < cutoff:
                _OAUTH_FLOWS.pop(fid, None)


def _spawn_codex_oauth_worker(flow_id: str) -> None:
    worker = threading.Thread(target=_run_codex_oauth_worker, args=(flow_id,), daemon=True)
    worker.start()


def _set_flow_status(flow_id: str, status: str, **fields: Any) -> None:
    with _OAUTH_FLOWS_LOCK:
        flow = _OAUTH_FLOWS.get(flow_id)
        if not flow:
            return
        flow["status"] = status
        flow["updated_at"] = time.time()
        flow.update(fields)
        if status in {"success", "expired", "cancelled", "error"}:
            _drop_sensitive_flow_fields(flow)


def _run_codex_oauth_worker(flow_id: str) -> None:
    while True:
        with _OAUTH_FLOWS_LOCK:
            flow = dict(_OAUTH_FLOWS.get(flow_id) or {})
        if not flow:
            return
        status = flow.get("status")
        if status != "pending":
            return
        if float(flow.get("expires_at") or 0) <= time.time():
            _set_flow_status(flow_id, "expired")
            return

        time.sleep(max(1, int(flow.get("poll_interval_seconds") or 5)))

        with _OAUTH_FLOWS_LOCK:
            live = dict(_OAUTH_FLOWS.get(flow_id) or {})
        if live.get("status") != "pending":
            return
        try:
            code_resp = _poll_codex_authorization(
                str(live.get("device_auth_id") or ""),
                str(live.get("user_code") or ""),
            )
            if code_resp is None:
                continue
            authorization_code = str(code_resp.get("authorization_code") or "").strip()
            code_verifier = str(code_resp.get("code_verifier") or "").strip()
            if not authorization_code or not code_verifier:
                raise RuntimeError("Device auth response missing authorization_code or code_verifier")
            tokens = _exchange_codex_authorization(authorization_code, code_verifier)
            _persist_codex_credentials(Path(live["hermes_home"]), tokens)
            _set_flow_status(flow_id, "success")
            return
        except Exception as exc:
            logger.warning("Codex OAuth onboarding flow failed: %s", exc)
            _set_flow_status(flow_id, "error", error=str(exc))
            return


def start_onboarding_oauth_flow(body: dict[str, Any] | None) -> dict[str, Any]:
    """Start the supported onboarding OAuth flow.

    Currently v1 intentionally supports only OpenAI Codex. Other providers are
    rejected instead of silently falling back to terminal-first setup.
    """
    _cleanup_oauth_flows()
    provider = str((body or {}).get("provider") or "").strip().lower()
    if provider not in _ALLOWED_ONBOARDING_OAUTH_PROVIDERS:
        if provider in _REJECTED_ONBOARDING_OAUTH_PROVIDERS or provider:
            raise ValueError("Only OpenAI Codex OAuth is supported in WebUI onboarding right now")
        raise ValueError("provider is required")

    hermes_home = _get_active_hermes_home()
    try:
        device = _request_codex_user_code()
    except Exception as exc:
        raise RuntimeError(f"Failed to start Codex OAuth: {exc}") from exc

    user_code = str(device.get("user_code") or "").strip()
    device_auth_id = str(device.get("device_auth_id") or "").strip()
    if not user_code or not device_auth_id:
        raise RuntimeError("Device code response missing required fields")

    interval = max(3, int(device.get("interval") or 5))
    expires_in = int(device.get("expires_in") or CODEX_FLOW_MAX_WAIT_SECONDS)
    expires_at = time.time() + min(max(expires_in, 60), CODEX_FLOW_MAX_WAIT_SECONDS)
    flow_id = uuid.uuid4().hex
    flow = {
        "provider": "openai-codex",
        "status": "pending",
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "expires_at": expires_at,
        "poll_interval_seconds": interval,
        "hermes_home": str(hermes_home),
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _OAUTH_FLOWS_LOCK:
        _OAUTH_FLOWS[flow_id] = flow
    _spawn_codex_oauth_worker(flow_id)
    return _public_start_payload(flow_id, flow)


def poll_onboarding_oauth_flow(flow_id: str) -> dict[str, Any]:
    _cleanup_oauth_flows()
    fid = str(flow_id or "").strip()
    if not fid:
        raise ValueError("flow_id is required")
    with _OAUTH_FLOWS_LOCK:
        flow = _OAUTH_FLOWS.get(fid)
        if not flow:
            raise KeyError("OAuth flow not found")
        if flow.get("status") == "pending" and float(flow.get("expires_at") or 0) <= time.time():
            flow["status"] = "expired"
            flow["updated_at"] = time.time()
            _drop_sensitive_flow_fields(flow)
        return _public_status_payload(fid, dict(flow))


def cancel_onboarding_oauth_flow(body: dict[str, Any] | None) -> dict[str, Any]:
    fid = str((body or {}).get("flow_id") or "").strip()
    if not fid:
        raise ValueError("flow_id is required")
    with _OAUTH_FLOWS_LOCK:
        flow = _OAUTH_FLOWS.get(fid)
        if not flow:
            return {"ok": True, "provider": "openai-codex", "flow_id": fid, "status": "cancelled"}
        if flow.get("status") == "pending":
            flow["status"] = "cancelled"
            flow["updated_at"] = time.time()
            _drop_sensitive_flow_fields(flow)
        return _public_status_payload(fid, dict(flow))


# Backward-compatible names from the abandoned spike. They intentionally do not
# expose provider device secrets to callers anymore.
def start_codex_device_code():
    return start_onboarding_oauth_flow({"provider": "openai-codex"})


def poll_codex_token(device_code, interval=5):
    yield {"status": "error", "error": "Use /api/onboarding/oauth/poll with flow_id"}
