"""In-app OAuth flow implementations for onboarding.

The browser gets only an opaque polling token plus user-facing verification
fields. Provider-owned device IDs, authorization codes, and OAuth tokens stay
server-side and are persisted through Hermes Agent's existing auth.json helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Backward-compatible read helper used by the streaming layer's credential
# self-heal logic (#1401). Prefer Hermes Agent helpers for writes.
AUTH_JSON_PATH = Path.home() / ".hermes" / "auth.json"

_CODEX_ISSUER = "https://auth.openai.com"
_CODEX_VERIFICATION_URL = f"{_CODEX_ISSUER}/codex/device"
_CODEX_DEVICE_CODE_URL = f"{_CODEX_ISSUER}/api/accounts/deviceauth/usercode"
_CODEX_DEVICE_POLL_URL = f"{_CODEX_ISSUER}/api/accounts/deviceauth/token"
_CODEX_TOKEN_URL_FALLBACK = f"{_CODEX_ISSUER}/oauth/token"
_CODEX_CLIENT_ID_FALLBACK = "pdlLIX2Y72MIl2rhLhTE9VV9bN905kBh"
_CODEX_DEFAULT_BASE_URL_FALLBACK = "https://chatgpt.com/backend-api/codex"
_CODEX_MAX_EXPIRES_SECONDS = 15 * 60
_SUPPORTED_WEB_OAUTH_PROVIDERS = {"openai-codex"}
_TERMINAL_STATES = {"success", "expired", "cancelled", "error"}


@dataclass
class _OAuthFlow:
    provider: str
    device_auth_id: str
    user_code: str
    verification_url: str
    interval: int
    expires_at: float
    status: str = "pending"
    error: str = ""


_OAUTH_FLOW_LOCK = threading.Lock()
_OAUTH_FLOWS: dict[str, _OAuthFlow] = {}


def _now() -> float:
    return time.time()


def _read_auth_json():
    """Read auth.json and return parsed dict, or empty dict."""
    if AUTH_JSON_PATH.exists():
        try:
            return json.loads(AUTH_JSON_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse %s: %s", AUTH_JSON_PATH, exc)
            return {}
    return {}


def read_auth_json():
    """Public wrapper for _read_auth_json."""
    return _read_auth_json()


def _write_auth_json(data: dict[str, Any]) -> None:
    """Atomically write auth.json with private permissions for OAuth tokens."""
    AUTH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_JSON_PATH.with_suffix(AUTH_JSON_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(AUTH_JSON_PATH)


def _codex_client_id() -> str:
    try:
        from hermes_cli import auth as auth_mod

        return str(getattr(auth_mod, "CODEX_OAUTH_CLIENT_ID", "") or _CODEX_CLIENT_ID_FALLBACK)
    except Exception:
        return _CODEX_CLIENT_ID_FALLBACK


def _codex_token_url() -> str:
    try:
        from hermes_cli import auth as auth_mod

        return str(getattr(auth_mod, "CODEX_OAUTH_TOKEN_URL", "") or _CODEX_TOKEN_URL_FALLBACK)
    except Exception:
        return _CODEX_TOKEN_URL_FALLBACK


def _codex_base_url() -> str:
    try:
        from hermes_cli import auth as auth_mod

        default_base = str(getattr(auth_mod, "DEFAULT_CODEX_BASE_URL", "") or _CODEX_DEFAULT_BASE_URL_FALLBACK)
    except Exception:
        default_base = _CODEX_DEFAULT_BASE_URL_FALLBACK
    return os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/") or default_base


def _json_post(url: str, *, json_body: dict[str, Any] | None = None, form_body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
    else:
        data = urllib.parse.urlencode(form_body or {}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return int(resp.status), json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {"error": raw[:200]}
        return int(exc.code), payload


def _codex_request_device_authorization() -> dict[str, Any]:
    status, payload = _json_post(
        _CODEX_DEVICE_CODE_URL,
        json_body={"client_id": _codex_client_id()},
    )
    if status != 200:
        raise RuntimeError(f"Codex device authorization failed with status {status}.")

    user_code = str(payload.get("user_code") or "").strip()
    device_auth_id = str(payload.get("device_auth_id") or "").strip()
    if not user_code or not device_auth_id:
        raise RuntimeError("Codex device authorization response was missing required fields.")

    try:
        interval = max(1, int(payload.get("interval") or 5))
    except Exception:
        interval = 5
    try:
        expires_in = int(payload.get("expires_in") or payload.get("expires") or _CODEX_MAX_EXPIRES_SECONDS)
    except Exception:
        expires_in = _CODEX_MAX_EXPIRES_SECONDS

    return {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "verification_url": _CODEX_VERIFICATION_URL,
        "interval": interval,
        "expires_in": max(1, min(expires_in, _CODEX_MAX_EXPIRES_SECONDS)),
    }


def _codex_poll_device_authorization(flow: _OAuthFlow) -> dict[str, Any] | None:
    status, payload = _json_post(
        _CODEX_DEVICE_POLL_URL,
        json_body={"device_auth_id": flow.device_auth_id, "user_code": flow.user_code},
    )
    if status == 200:
        authorization_code = str(payload.get("authorization_code") or "").strip()
        code_verifier = str(payload.get("code_verifier") or "").strip()
        if not authorization_code or not code_verifier:
            raise RuntimeError("Codex authorization response was missing required fields.")
        return {"authorization_code": authorization_code, "code_verifier": code_verifier}

    if status in (403, 404):
        return None

    raise RuntimeError(f"Codex device authorization poll failed with status {status}.")


def _codex_exchange_authorization_code(authorization_code: str, code_verifier: str) -> dict[str, Any]:
    status, payload = _json_post(
        _codex_token_url(),
        form_body={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": f"{_CODEX_ISSUER}/deviceauth/callback",
            "client_id": _codex_client_id(),
            "code_verifier": code_verifier,
        },
    )
    if status != 200:
        raise RuntimeError(f"Codex token exchange failed with status {status}.")
    if not str(payload.get("access_token") or "").strip():
        raise RuntimeError("Codex token exchange response was missing access_token.")
    return payload


def _persist_codex_tokens(tokens: dict[str, Any]) -> None:
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError("Codex token response was missing access_token.")

    persisted_tokens = {"access_token": access_token, "refresh_token": refresh_token}
    last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        from hermes_cli import auth as auth_mod

        auth_mod._save_codex_tokens(persisted_tokens, last_refresh)
    except Exception:
        store = _read_auth_json()
        providers = store.setdefault("providers", {})
        state = providers.setdefault("openai-codex", {})
        state["tokens"] = persisted_tokens
        state["last_refresh"] = last_refresh
        state["auth_mode"] = "chatgpt"
        _write_auth_json(store)

    # Seed/update the credential pool immediately when that helper is available;
    # load_pool() would also derive it from providers.openai-codex on demand.
    try:
        from agent.credential_pool import AUTH_TYPE_OAUTH, PooledCredential, label_from_token, load_pool

        pool = load_pool("openai-codex")
        entries = pool.entries()
        existing = next((entry for entry in entries if entry.source in {"device_code", "manual:device_code"}), None)
        label = label_from_token(access_token, "device_code")
        if existing is None:
            entry = PooledCredential(
                provider="openai-codex",
                id=secrets.token_hex(3),
                label=label,
                auth_type=AUTH_TYPE_OAUTH,
                priority=0,
                source="device_code",
                access_token=access_token,
                refresh_token=refresh_token,
                base_url=_codex_base_url(),
                last_refresh=last_refresh,
            )
            pool.add_entry(entry)
    except Exception:
        logger.debug("Codex credential_pool seeding skipped", exc_info=True)


def _cleanup_expired_locked(now: float | None = None) -> None:
    current = _now() if now is None else now
    for token, flow in list(_OAUTH_FLOWS.items()):
        if flow.status in _TERMINAL_STATES or current >= flow.expires_at:
            _OAUTH_FLOWS.pop(token, None)


def start_oauth_flow(provider: str) -> dict[str, Any]:
    provider = (provider or "").strip().lower()
    if provider not in _SUPPORTED_WEB_OAUTH_PROVIDERS:
        raise ValueError(f"Unsupported OAuth provider: {provider or '(missing)'}")

    device = _codex_request_device_authorization()
    expires_in = max(1, min(int(device.get("expires_in") or _CODEX_MAX_EXPIRES_SECONDS), _CODEX_MAX_EXPIRES_SECONDS))
    polling_token = secrets.token_urlsafe(32)
    flow = _OAuthFlow(
        provider=provider,
        device_auth_id=str(device["device_auth_id"]),
        user_code=str(device["user_code"]),
        verification_url=str(device.get("verification_url") or _CODEX_VERIFICATION_URL),
        interval=max(1, int(device.get("interval") or 5)),
        expires_at=_now() + expires_in,
    )
    with _OAUTH_FLOW_LOCK:
        _cleanup_expired_locked()
        _OAUTH_FLOWS[polling_token] = flow

    return {
        "ok": True,
        "provider": provider,
        "status": "pending",
        "polling_token": polling_token,
        "verification_url": flow.verification_url,
        "user_code": flow.user_code,
        "expires_in": expires_in,
        "interval": flow.interval,
    }


def _status_payload(flow: _OAuthFlow, status: str | None = None) -> dict[str, Any]:
    payload = {"ok": True, "provider": flow.provider, "status": status or flow.status}
    if payload["status"] == "error" and flow.error:
        payload["error"] = flow.error
    return payload


def poll_oauth_flow(polling_token: str) -> dict[str, Any]:
    token = (polling_token or "").strip()
    if not token:
        raise ValueError("polling_token is required")

    with _OAUTH_FLOW_LOCK:
        flow = _OAUTH_FLOWS.get(token)
        if flow is None:
            raise ValueError("Unknown or expired OAuth polling token")
        if flow.status in {"cancelled", "error"}:
            return _status_payload(flow)
        if _now() >= flow.expires_at:
            flow.status = "expired"
            _OAUTH_FLOWS.pop(token, None)
            return _status_payload(flow)
        if flow.status == "success":
            return _status_payload(flow)

    try:
        authorization = _codex_poll_device_authorization(flow)
        if authorization is None:
            return _status_payload(flow, "pending")
        token_payload = _codex_exchange_authorization_code(
            str(authorization["authorization_code"]),
            str(authorization["code_verifier"]),
        )
        _persist_codex_tokens(token_payload)
        with _OAUTH_FLOW_LOCK:
            flow.status = "success"
            _OAUTH_FLOWS.pop(token, None)
        return _status_payload(flow)
    except Exception as exc:
        logger.warning("Codex OAuth polling failed: %s", exc)
        with _OAUTH_FLOW_LOCK:
            flow.status = "error"
            flow.error = "Codex OAuth login failed. Please try again."
        return _status_payload(flow)


def cancel_oauth_flow(polling_token: str) -> dict[str, Any]:
    token = (polling_token or "").strip()
    if not token:
        raise ValueError("polling_token is required")
    with _OAUTH_FLOW_LOCK:
        flow = _OAUTH_FLOWS.get(token)
        if flow is None:
            raise ValueError("Unknown or expired OAuth polling token")
        flow.status = "cancelled"
        return _status_payload(flow)
