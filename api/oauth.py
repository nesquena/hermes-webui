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

_ALLOWED_ONBOARDING_OAUTH_PROVIDERS = {"openai-codex", "anthropic", "claude", "claude-code"}
_ANTHROPIC_PROVIDER_ALIASES = {"anthropic", "claude", "claude-code"}
_REJECTED_ONBOARDING_OAUTH_PROVIDERS = {
    "nous",
    "qwen-oauth",
    "gemini-cli",
    "google-gemini-cli",
    "minimax",
    "minimax-oauth",
    "copilot",
    "copilot-acp",
}

ANTHROPIC_CREDENTIAL_POLL_SECONDS = 5
ANTHROPIC_FLOW_MAX_WAIT_SECONDS = 15 * 60
ANTHROPIC_PUBLIC_LINK_ERROR = "Claude Code credential linking failed. Check server logs."

_OAUTH_FLOWS: dict[str, dict[str, Any]] = {}
_OAUTH_FLOWS_LOCK = threading.Lock()
_ANTHROPIC_ENV_KEYS = ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY")


def _clear_process_anthropic_env_values() -> None:
    """Clear Anthropic process env fallbacks under the streaming env lock."""
    from api.streaming import _ENV_LOCK

    with _ENV_LOCK:
        for key in _ANTHROPIC_ENV_KEYS:
            os.environ.pop(key, None)


def resolve_runtime_provider_with_anthropic_env_lock(resolver, *args, **kwargs):
    """Resolve runtime credentials under the Anthropic onboarding env lock.

    Request paths must resolve Anthropic env fallbacks per outbound request,
    not cache ANTHROPIC_TOKEN or ANTHROPIC_API_KEY across onboarding. Sharing
    the process-env lock prevents a chat stream from observing one stale
    Anthropic env value while onboarding has already cleared the other.
    """
    from api.streaming import _ENV_LOCK

    with _ENV_LOCK:
        return resolver(*args, **kwargs)


def _normalize_onboarding_oauth_provider(provider: str) -> str:
    provider = str(provider or "").strip().lower()
    if provider in _ANTHROPIC_PROVIDER_ALIASES:
        return "anthropic"
    return provider or "openai-codex"


def _get_active_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home())
    except Exception as exc:
        # Per Opus advisor on stage-296: log the silent fallback so a corrupt
        # profile state ending up writing tokens to ~/.hermes (instead of the
        # active profile) is observable in logs rather than failing silently.
        logger.warning(
            "Falling back to ~/.hermes for OAuth credential storage: "
            "active-profile resolution failed: %s",
            exc,
        )
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
    # Per Opus advisor on stage-296: also accept the legacy `source ==
    # "oauth_device"` value so users with prior Codex OAuth credentials
    # (written by older WebUI versions before this PR's source-key change)
    # get their existing entry updated in-place rather than accumulating a
    # stale duplicate pool entry.
    _accept_sources = {"manual:device_code", "oauth_device"}
    for candidate in entries:
        if isinstance(candidate, dict) and candidate.get("source") in _accept_sources:
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


# ── Anthropic / Claude Code credential linking ─────────────────────────────

def _read_claude_code_credentials() -> dict[str, Any] | None:
    """Read Claude Code OAuth credentials from the host without exposing them.

    Delegates to the agent adapter which knows about ~/.claude/.credentials.json
    and macOS Keychain. Returns the credential dict or None.
    """
    try:
        from agent.anthropic_adapter import (
            is_claude_code_token_valid,
            read_claude_code_credentials,
        )

        creds = read_claude_code_credentials()
        if creds and (
            is_claude_code_token_valid(creds) or bool(creds.get("refreshToken"))
        ):
            return creds
    except Exception as exc:
        logger.debug("Could not read Claude Code credentials: %s", exc)
    return None


def _clear_anthropic_env_values(hermes_home: Path) -> None:
    """Clear Anthropic API/setup-token env values in the active profile only.

    The .env write path already clears os.environ while holding the streaming
    env lock. Keep a locked process-env clear here too so import/write failures
    cannot leave or partially clear stale Anthropic fallbacks.
    """
    try:
        from api.providers import _write_env_file

        _write_env_file(
            Path(hermes_home) / ".env",
            {key: None for key in _ANTHROPIC_ENV_KEYS},
        )
    except Exception as exc:
        logger.warning("Failed to clear Anthropic env values: %s", exc)
    _clear_process_anthropic_env_values()


def _link_anthropic_credentials(hermes_home: Path) -> None:
    """Link Hermes to use Claude Code's credential store.

    Clears ANTHROPIC_TOKEN and ANTHROPIC_API_KEY from the Hermes .env so
    that resolve_anthropic_token() falls through to reading Claude Code's
    ~/.claude/.credentials.json directly — the same thing the CLI's
    ``use_anthropic_claude_code_credentials()`` does.

    Also writes a marker entry in auth.json credential_pool so that
    ``_provider_oauth_authenticated("anthropic", ...)`` can detect the
    linked state without touching the actual credential files.
    """
    _clear_anthropic_env_values(hermes_home)

    # Write a pool marker (no secrets) so onboarding status can detect linkage.
    auth_path = Path(hermes_home) / "auth.json"
    auth = _read_auth_json(auth_path)
    auth.setdefault("version", 1)
    pool = auth.setdefault("credential_pool", {})
    if not isinstance(pool, dict):
        pool = {}
        auth["credential_pool"] = pool
    entries = pool.setdefault("anthropic", [])
    if not isinstance(entries, list):
        entries = []
        pool["anthropic"] = entries

    now = _now_iso()
    entry = None
    for candidate in entries:
        if isinstance(candidate, dict) and candidate.get("source") == "claude_code_linked":
            entry = candidate
            break
    if entry is None:
        entry = {
            "id": "anthropic-claude-code-" + uuid.uuid4().hex[:12],
            "label": "Claude Code (linked)",
            "auth_type": "oauth",
            "priority": 0,
            "source": "claude_code_linked",
            "created_at": now,
        }
        entries.insert(0, entry)

    entry.update({
        "label": "Claude Code (linked)",
        "auth_type": "oauth",
        "priority": 0,
        "source": "claude_code_linked",
        "updated_at": now,
    })
    auth["updated_at"] = now
    _write_auth_json(auth, auth_path)

    try:
        from api.config import invalidate_credential_pool_cache
        invalidate_credential_pool_cache("anthropic")
    except Exception:
        logger.debug("Failed to invalidate anthropic credential cache", exc_info=True)


def _anthropic_public_start_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "provider": "anthropic",
        "flow_id": flow_id,
        "status": flow.get("status", "pending"),
        "poll_interval_seconds": flow.get("poll_interval_seconds", ANTHROPIC_CREDENTIAL_POLL_SECONDS),
    }
    if flow.get("status") == "pending":
        payload["action_required"] = (
            "Claude Code credentials were not found on this server. "
            "Please run 'claude login' or 'claude setup-token' in a terminal "
            "on the host, then return here — this page will detect the credentials automatically."
        )
    if flow.get("expires_at"):
        payload["expires_at"] = flow["expires_at"]
    return payload


def _anthropic_public_status_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "provider": "anthropic",
        "flow_id": flow_id,
        "status": flow.get("status", "error"),
    }
    if flow.get("status") == "error" and flow.get("error"):
        payload["error"] = ANTHROPIC_PUBLIC_LINK_ERROR
    return payload


def _spawn_anthropic_credential_worker(flow_id: str) -> None:
    worker = threading.Thread(
        target=_run_anthropic_credential_worker, args=(flow_id,), daemon=True,
    )
    worker.start()


def _run_anthropic_credential_worker(flow_id: str) -> None:
    """Poll for Claude Code credential appearance until found, cancelled, or expired."""
    while True:
        with _OAUTH_FLOWS_LOCK:
            flow = dict(_OAUTH_FLOWS.get(flow_id) or {})
        if not flow:
            return
        if flow.get("status") != "pending":
            return
        if float(flow.get("expires_at") or 0) <= time.time():
            _set_flow_status(flow_id, "expired")
            return

        time.sleep(max(1, int(flow.get("poll_interval_seconds") or ANTHROPIC_CREDENTIAL_POLL_SECONDS)))

        # Re-check status under lock (cancel may have arrived during sleep)
        with _OAUTH_FLOWS_LOCK:
            live = _OAUTH_FLOWS.get(flow_id)
            if not live or live.get("status") != "pending":
                return

        try:
            creds = _read_claude_code_credentials()
            if creds is None:
                continue

            # Re-check status under lock before linking — cancel must win
            with _OAUTH_FLOWS_LOCK:
                current = _OAUTH_FLOWS.get(flow_id)
                if not current or current.get("status") != "pending":
                    return

            hermes_home = Path(flow["hermes_home"])
            _link_anthropic_credentials(hermes_home)
            with _OAUTH_FLOWS_LOCK:
                current = _OAUTH_FLOWS.get(flow_id)
                if not current or current.get("status") != "pending":
                    cancelled = bool(current and current.get("status") == "cancelled")
                else:
                    current["status"] = "success"
                    current["updated_at"] = time.time()
                    _drop_sensitive_flow_fields(current)
                    cancelled = False
            if cancelled:
                _remove_anthropic_link_marker(hermes_home)
            return
        except Exception as exc:
            logger.warning("Anthropic credential polling failed: %s", exc)
            with _OAUTH_FLOWS_LOCK:
                current = _OAUTH_FLOWS.get(flow_id)
                if current and current.get("status") == "pending":
                    current["status"] = "error"
                    current["updated_at"] = time.time()
                    current["error"] = str(exc)
                    _drop_sensitive_flow_fields(current)
            return


def _remove_anthropic_link_marker(hermes_home: Path) -> None:
    """Remove the secret-free Claude Code linked marker after a cancelled race."""
    auth_path = Path(hermes_home) / "auth.json"
    auth = _read_auth_json(auth_path)
    pool = auth.get("credential_pool")
    if not isinstance(pool, dict):
        return
    entries = pool.get("anthropic")
    if not isinstance(entries, list):
        return
    kept = [entry for entry in entries if not (isinstance(entry, dict) and entry.get("source") == "claude_code_linked")]
    if len(kept) == len(entries):
        return
    if kept:
        pool["anthropic"] = kept
    else:
        pool.pop("anthropic", None)
    auth["updated_at"] = _now_iso()
    _write_auth_json(auth, auth_path)
    try:
        from api.config import invalidate_credential_pool_cache
        invalidate_credential_pool_cache("anthropic")
    except Exception:
        logger.debug("Failed to invalidate anthropic credential cache", exc_info=True)


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


def _codex_public_start_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
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


def _codex_public_status_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "ok": True,
        "provider": "openai-codex",
        "flow_id": flow_id,
        "status": flow.get("status", "error"),
    }
    if flow.get("status") == "error" and flow.get("error"):
        payload["error"] = str(flow.get("error"))[:200]
    return payload


def _public_start_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    provider = flow.get("provider", "openai-codex")
    if provider == "anthropic":
        return _anthropic_public_start_payload(flow_id, flow)
    return _codex_public_start_payload(flow_id, flow)


def _public_status_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    provider = flow.get("provider", "openai-codex")
    if provider == "anthropic":
        return _anthropic_public_status_payload(flow_id, flow)
    return _codex_public_status_payload(flow_id, flow)


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
            # Re-check status under lock before persisting: a cancel/expire that
            # raced with the device-token + token-exchange network calls must
            # win, so we don't persist credentials the user explicitly aborted.
            with _OAUTH_FLOWS_LOCK:
                current = _OAUTH_FLOWS.get(flow_id)
                if not current or current.get("status") != "pending":
                    return
            _persist_codex_credentials(Path(live["hermes_home"]), tokens)
            _set_flow_status(flow_id, "success")
            return
        except Exception as exc:
            logger.warning("Codex OAuth onboarding flow failed: %s", exc)
            _set_flow_status(flow_id, "error", error=str(exc))
            return


def _start_anthropic_flow(hermes_home: Path) -> dict[str, Any]:
    """Start or immediately complete the Anthropic credential-linking flow."""
    creds = _read_claude_code_credentials()
    flow_id = uuid.uuid4().hex

    if creds:
        # Credentials already exist — link and return success immediately.
        _link_anthropic_credentials(hermes_home)
        flow = {
            "provider": "anthropic",
            "status": "success",
            "hermes_home": str(hermes_home),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with _OAUTH_FLOWS_LOCK:
            _OAUTH_FLOWS[flow_id] = flow
        return _public_start_payload(flow_id, flow)

    # No credentials found — create a pending flow that polls for them.
    expires_at = time.time() + ANTHROPIC_FLOW_MAX_WAIT_SECONDS
    flow = {
        "provider": "anthropic",
        "status": "pending",
        "expires_at": expires_at,
        "poll_interval_seconds": ANTHROPIC_CREDENTIAL_POLL_SECONDS,
        "hermes_home": str(hermes_home),
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _OAUTH_FLOWS_LOCK:
        _OAUTH_FLOWS[flow_id] = flow
    _spawn_anthropic_credential_worker(flow_id)
    return _public_start_payload(flow_id, flow)


def start_onboarding_oauth_flow(body: dict[str, Any] | None) -> dict[str, Any]:
    """Start the supported onboarding OAuth flow.

    Supports OpenAI Codex (device-code flow) and Anthropic/Claude Code
    (credential-linking flow). Other providers are rejected.
    """
    _cleanup_oauth_flows()
    provider = str((body or {}).get("provider") or "").strip().lower()
    if provider not in _ALLOWED_ONBOARDING_OAUTH_PROVIDERS:
        if provider in _REJECTED_ONBOARDING_OAUTH_PROVIDERS or provider:
            raise ValueError(
                "Only OpenAI Codex and Anthropic/Claude OAuth are supported "
                "in WebUI onboarding right now"
            )
        raise ValueError("provider is required")

    # Normalize Claude aliases to canonical "anthropic"
    if provider in _ANTHROPIC_PROVIDER_ALIASES:
        return _start_anthropic_flow(_get_active_hermes_home())

    # Codex flow
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
    requested_provider = _normalize_onboarding_oauth_provider(str((body or {}).get("provider") or ""))
    if requested_provider not in {"openai-codex", "anthropic"}:
        requested_provider = "openai-codex"
    with _OAUTH_FLOWS_LOCK:
        flow = _OAUTH_FLOWS.get(fid)
        if not flow:
            return {"ok": True, "provider": requested_provider, "flow_id": fid, "status": "cancelled"}
        if flow.get("status") == "pending":
            flow["status"] = "cancelled"
            flow["updated_at"] = time.time()
            _drop_sensitive_flow_fields(flow)
        result = _public_status_payload(fid, dict(flow))
    return result


# Backward-compatible names from the abandoned spike. They intentionally do not
# expose provider device secrets to callers anymore.
def start_codex_device_code():
    return start_onboarding_oauth_flow({"provider": "openai-codex"})


def poll_codex_token(device_code, interval=5):
    yield {"status": "error", "error": "Use /api/onboarding/oauth/poll with flow_id"}
