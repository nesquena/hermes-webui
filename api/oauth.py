"""In-app OAuth flow implementations for onboarding.

The browser receives only WebUI-local flow metadata (flow_id, user_code,
verification_uri, high-level status). Provider device/auth codes and OAuth
tokens stay server-side and are persisted to the active Hermes profile's
``auth.json`` credential_pool.
"""

from __future__ import annotations

import contextlib
import inspect
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
# auth.openai.com rejects urllib's default "Python-urllib/x.y" User-Agent with
# HTTP 530; same value the Codex usage probe sends (api/providers.py).
CODEX_USER_AGENT = "codex_cli_rs/0.0.0 (Hermes WebUI)"
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
_OAUTH_START_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_OAUTH_START_LOCKS_LOCK = threading.Lock()
_ANTHROPIC_ENV_KEYS = ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY")


def _agent_auth_lock_timeout_seconds() -> float:
    """How long to wait for the Agent auth lock.

    The Agent's Codex refresh holds that lock for up to its refresh timeout
    (``HERMES_CODEX_REFRESH_TIMEOUT_SECONDS``, 20 s by default) + 5 s and waits
    that long itself; the lock's 15 s default would fail a login that finishes
    during a slow refresh. Same rule as the Agent.
    """
    try:
        refresh_timeout = float(os.environ.get("HERMES_CODEX_REFRESH_TIMEOUT_SECONDS") or 20.0)
    except ValueError:
        refresh_timeout = 20.0
    return max(15.0, refresh_timeout + 5.0)


def _oauth_start_key(provider: str, hermes_home: Path) -> tuple[str, str]:
    """Return the canonical single-flight key for onboarding OAuth starts."""
    return (_normalize_onboarding_oauth_provider(provider), str(Path(hermes_home)))


def _oauth_start_lock(provider: str, hermes_home: Path) -> threading.Lock:
    """Return the per provider/profile lock that serializes OAuth flow creation."""
    key = _oauth_start_key(provider, hermes_home)
    with _OAUTH_START_LOCKS_LOCK:
        lock = _OAUTH_START_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _OAUTH_START_LOCKS[key] = lock
        return lock


def _pending_oauth_flow_for_locked(provider: str, hermes_home: Path) -> tuple[str, dict[str, Any]] | None:
    """Return an existing live onboarding OAuth flow for the provider/profile.

    Onboarding OAuth starts are unauthenticated while first-run auth is disabled.
    Keep them single-flight per provider/profile so repeated start requests reuse
    the existing worker instead of accumulating pending flows and daemon threads.

    The caller must hold _OAUTH_FLOWS_LOCK so the check can be paired atomically
    with flow insertion on start paths.
    """
    canonical_provider = _normalize_onboarding_oauth_provider(provider)
    canonical_home = str(Path(hermes_home))
    now = time.time()
    for flow_id, flow in _OAUTH_FLOWS.items():
        if flow.get("provider") != canonical_provider:
            continue
        if str(flow.get("hermes_home") or "") != canonical_home:
            continue
        # A committed flow already used its code; a new start gets a new flow.
        if not _flow_is_open(flow):
            continue
        if float(flow.get("expires_at") or 0) <= now:
            flow["status"] = "expired"
            flow["updated_at"] = now
            _drop_sensitive_flow_fields(flow)
            continue
        return flow_id, dict(flow)
    return None


def _pending_oauth_flow_for(provider: str, hermes_home: Path) -> tuple[str, dict[str, Any]] | None:
    """Return an existing live onboarding OAuth flow for the provider/profile, if any."""
    with _OAUTH_FLOWS_LOCK:
        return _pending_oauth_flow_for_locked(provider, hermes_home)


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
    """Normalize Anthropic aliases (claude, claude-code) to 'anthropic'; defaults to 'openai-codex' when blank."""
    provider = str(provider or "").strip().lower()
    if provider in _ANTHROPIC_PROVIDER_ALIASES:
        return "anthropic"
    return provider or "openai-codex"


def _get_active_profile_name() -> str | None:
    """Return the active profile name for this request, or None if unknown."""
    try:
        from api.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        logger.debug("Could not resolve the active profile name", exc_info=True)
        return None


def _check_expected_profile(body: dict[str, Any] | None) -> None:
    """Refuse a start whose page shows another profile than this request's.

    The profile cookie is shared by every tab: after another tab switches it,
    a card still showing the old profile would log in to the new one. The page
    sends the profile it shows; a start without one keeps the old behaviour.
    """
    from api.profiles import _profiles_match

    expected = str((body or {}).get("profile") or "").strip()[:64]
    if not expected:
        return
    active = _get_active_profile_name()
    if active is None:
        # The request's profile could not be resolved; nothing to compare with.
        return
    if not _profiles_match(expected, active):
        raise ValueError(
            f"This page shows profile '{expected}', but the browser is now on profile "
            f"'{active}' (switched in another tab). Reload the page and start the login again."
        )


@contextlib.contextmanager
def _worker_profile_scope(profile: str | None):
    """Bind ``profile`` to this worker thread's request-profile slot.

    Profile-keyed caches (the credential-pool cache) resolve the profile from
    the request thread-local; the OAuth worker thread has none and would clear
    the server default profile's entry instead of the flow's. Restores the
    thread's previous profile on exit, like ``profile_scope_for_detached_worker``.
    """
    if not profile:
        yield
        return
    try:
        from api import profiles
    except ImportError:
        yield
        return
    previous = getattr(profiles._tls, "profile", None)
    profiles.set_request_profile(profile)
    try:
        yield
    finally:
        if previous is None:
            profiles.clear_request_profile()
        else:
            profiles.set_request_profile(previous)


def _get_active_hermes_home() -> Path:
    """Return the active Hermes profile home directory, falling back to ~/.hermes when profile resolution fails."""
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
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())  # durable before rename: no truncated auth.json on power loss
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
    """Return the current UTC time as an ISO-8601 string ending in Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _invalidate_provider_state_caches(provider: str) -> None:
    """Invalidate caches derived from provider credential state."""
    try:
        from api.config import invalidate_credential_pool_cache

        invalidate_credential_pool_cache(provider)
    except Exception:
        logger.debug("Failed to invalidate %s credential cache", provider, exc_info=True)

    try:
        from api.providers import invalidate_providers_cache

        invalidate_providers_cache()
    except Exception:
        logger.debug("Failed to invalidate providers cache after %s credential change", provider, exc_info=True)


@contextlib.contextmanager
def _agent_auth_store_lock(auth_path: Path):
    """Hold the Agent's cross-process auth.json lock for ``auth_path``.

    The Agent refreshes Codex tokens under ``_auth_store_lock``; a WebUI
    read-modify-write outside it can replace a refresh committed in between,
    and refresh tokens are single-use. Without an importable Agent, or with one
    whose lock cannot target a profile's auth.json (``target_path``, added
    after the lock itself), nothing is written: an unlocked write would bring
    that loss back, so the login fails with a message instead.
    """
    try:
        from hermes_cli.auth import _auth_store_lock
    except ImportError as exc:
        reason = str(exc)
    else:
        reason = ""
        try:
            params = inspect.signature(_auth_store_lock).parameters
        except (TypeError, ValueError):
            params = {}
        if not {"timeout_seconds", "target_path"} <= set(params):
            reason = "this Agent's _auth_store_lock has no timeout_seconds/target_path"
    if reason:
        logger.warning(
            "Codex credentials not saved to %s: Agent auth store lock unavailable (%s)",
            auth_path,
            reason,
        )
        raise RuntimeError(
            "This Hermes Agent cannot lock the saved credentials, so the login was not saved. "
            "Update Hermes Agent and start the login again."
        )

    lock = _auth_store_lock(timeout_seconds=_agent_auth_lock_timeout_seconds(), target_path=auth_path)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(lock)
        except TimeoutError as exc:
            # The Agent's message names the lock path and the holder's pid;
            # keep those in the log, not in the browser.
            logger.warning("Codex credentials not saved: %s", exc)
            raise RuntimeError(
                "Another Hermes process is updating the saved credentials. Start the login again."
            ) from exc
        except OSError as exc:
            # e.g. no permission on the lock file; its path stays in the log.
            logger.warning("Codex credentials not saved: %s", exc)
            raise RuntimeError(
                "The saved credentials could not be locked, so the login was not saved. Check the server logs."
            ) from exc
        yield


# Same fields, cleared the same way, as the Agent's ``hermes auth reset``.
_CLEARED_POOL_STATUS = {
    "last_status": None,
    "last_status_at": None,
    "last_error_code": None,
    "last_error_reason": None,
    "last_error_message": None,
    "last_error_reset_at": None,
}


def _is_webui_codex_entry(entry: Any) -> bool:
    """True for pool entries WebUI created itself.

    The Agent CLI (``hermes auth add openai-codex``) also writes
    ``source: manual:device_code``, so the source alone does not tell whose
    entry it is. WebUI ids have always started with ``codex-oauth-``,
    including the older ``source: oauth_device`` entries.
    """
    return (
        isinstance(entry, dict)
        and str(entry.get("id") or "").startswith("codex-oauth-")
        and entry.get("source") in {"manual:device_code", "oauth_device"}
    )


def _persist_codex_credentials(
    hermes_home: Path, token_data: dict[str, Any], flow_id: str | None = None
) -> Path | None:
    """Persist Codex OAuth credentials to active-profile auth.json.

    With ``flow_id``, the write happens only if that flow is still open, and in
    the same step the flow is marked committed so a later cancel or expiry
    cannot move it; returns None when a cancel or expiry got there first. The
    flow becomes ``success`` only after the provider caches are cleared (for
    the flow's profile), so the refresh the browser starts on success reads the
    new credential. Lock order: the Agent auth lock first (it can wait up to
    25 s), ``_OAUTH_FLOWS_LOCK`` second, so cancel and poll are not blocked
    during that wait; no lock is held while clearing the caches, which can wait
    behind a model rebuild.
    """
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    if not access_token:
        raise RuntimeError("Codex token exchange did not return an access_token")

    auth_path = Path(hermes_home) / "auth.json"
    profile = None
    with _agent_auth_store_lock(auth_path), (
        _OAUTH_FLOWS_LOCK if flow_id else contextlib.nullcontext()
    ):
        if flow_id:
            flow = _OAUTH_FLOWS.get(flow_id)
            if not _flow_is_open(flow):
                return None
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
        entry = next((candidate for candidate in entries if _is_webui_codex_entry(candidate)), None)
        if entry is not None:
            # Fresh tokens make a dead or rate-limited entry usable again; the
            # Agent's pools only pick that up from ``status_cleared_at`` newer
            # than the old status, the marker ``hermes auth reset`` writes.
            entry.update(_CLEARED_POOL_STATUS)
            entry.pop("model_cooldowns", None)
            entry.pop("failure_reason", None)
            entry["status_cleared_at"] = time.time()
        else:
            # Accounts added with the Agent CLI stay untouched; WebUI's own
            # credential goes after them, like a CLI add would.
            priorities = [
                c.get("priority") for c in entries
                if isinstance(c, dict) and isinstance(c.get("priority"), int)
            ]
            entry = {
                "id": "codex-oauth-" + uuid.uuid4().hex[:12],
                "priority": max(priorities, default=-1) + 1,
                "created_at": now,
            }
            entries.append(entry)

        entry.update(
            {
                "label": "Codex OAuth",
                "auth_type": "oauth",
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
        if flow_id:
            # Inline: _set_flow_status would take _OAUTH_FLOWS_LOCK again.
            flow["_committed"] = True
            flow["updated_at"] = time.time()
            _drop_sensitive_flow_fields(flow)
            profile = flow.get("profile")

    try:
        with _worker_profile_scope(profile):
            _invalidate_provider_state_caches("openai-codex")
    finally:
        if flow_id:
            # The credential is saved; a committed flow can only end in success.
            _set_flow_status(flow_id, "success")

    return path


# Backward-compatible wrapper used by older code/tests.
def _save_codex_credentials(token_data):
    """Backward-compatible wrapper: persist Codex OAuth tokens to the active-profile auth.json."""
    return _persist_codex_credentials(_get_active_hermes_home(), token_data)


# ── Anthropic / Claude Code credential linking ─────────────────────────────

def _read_claude_code_credentials() -> dict[str, Any] | None:
    """Read Claude Code OAuth credentials from the host without exposing them.

    Delegates to the agent adapter which knows about ~/.claude/.credentials.json
    and macOS Keychain. Returns the credential dict or None.
    """
    try:
        from api.agent_compat import agent_attr

        is_claude_code_token_valid = agent_attr(
            "agent.anthropic_adapter", "is_claude_code_token_valid", "agent.anthropic_credentials"
        )
        read_claude_code_credentials = agent_attr(
            "agent.anthropic_adapter", "read_claude_code_credentials", "agent.anthropic_credentials"
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
    _invalidate_provider_state_caches("anthropic")


def _anthropic_public_start_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    """Build the browser-safe start payload for an Anthropic credential-linking flow, omitting server-side secrets."""
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
    """Build the browser-safe status payload for an Anthropic flow, replacing internal error with a safe string."""
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
    """Launch a daemon thread that polls for Claude Code credentials and transitions the Anthropic flow to success."""
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
    _invalidate_provider_state_caches("anthropic")


# ── Codex protocol ──────────────────────────────────────────────────────────

def _json_request(url: str, payload: dict[str, Any], *, form: bool = False) -> dict[str, Any]:
    """POST a JSON or form-encoded payload to url and return the parsed JSON response."""
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
        headers={
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": CODEX_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _request_codex_user_code() -> dict[str, Any]:
    """Request a new device-auth user code and device_auth_id from the Codex endpoint."""
    return _json_request(CODEX_USER_CODE_URL, {"client_id": CODEX_CLIENT_ID})


def _poll_codex_authorization(device_auth_id: str, user_code: str) -> dict[str, Any] | None:
    """Poll the Codex device token endpoint; returns None on 403/404 (not yet authorized) or raises otherwise."""
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
    """Exchange a Codex authorization code and PKCE verifier for access/refresh tokens."""
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
    """Build the browser-safe start payload for a Codex device-code flow, including user_code and verification_uri."""
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
    """Build the browser-safe status payload for a Codex flow, capping error strings at 200 characters."""
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
    """Dispatch to the provider-specific start payload builder based on flow['provider']."""
    provider = flow.get("provider", "openai-codex")
    if provider == "anthropic":
        return _anthropic_public_start_payload(flow_id, flow)
    return _codex_public_start_payload(flow_id, flow)


def _public_status_payload(flow_id: str, flow: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to the provider-specific status payload builder based on flow['provider']."""
    provider = flow.get("provider", "openai-codex")
    if provider == "anthropic":
        return _anthropic_public_status_payload(flow_id, flow)
    return _codex_public_status_payload(flow_id, flow)


def _drop_sensitive_flow_fields(flow: dict[str, Any]) -> None:
    """Remove device codes, authorization codes, and token material from a flow dict in place."""
    for key in (
        "device_auth_id",
        "authorization_code",
        "code_verifier",
        "access_token",
        "refresh_token",
        "token_data",
    ):
        flow.pop(key, None)


def _flow_is_open(flow: dict[str, Any] | None) -> bool:
    """True while a flow can still be cancelled, expired or failed.

    A Codex flow whose credential is already written stays ``pending`` until
    the provider caches are cleared, then becomes ``success``; in that window
    it is decided and nothing may move it elsewhere.
    """
    return bool(flow) and flow.get("status") == "pending" and not flow.get("_committed")


def _cleanup_oauth_flows(now: float | None = None) -> None:
    """Expire pending flows past their deadline and purge terminal flows older than 300 seconds from memory."""
    now = now or time.time()
    cutoff = now - 300
    with _OAUTH_FLOWS_LOCK:
        for fid, flow in list(_OAUTH_FLOWS.items()):
            status = flow.get("status")
            if _flow_is_open(flow) and float(flow.get("expires_at") or 0) <= now:
                flow["status"] = "expired"
                _drop_sensitive_flow_fields(flow)
            if status in {"success", "expired", "cancelled", "error"} and float(flow.get("updated_at") or 0) < cutoff:
                _OAUTH_FLOWS.pop(fid, None)


def _spawn_codex_oauth_worker(flow_id: str) -> None:
    """Launch a daemon thread that drives the Codex device-code polling and token exchange loop."""
    worker = threading.Thread(target=_run_codex_oauth_worker, args=(flow_id,), daemon=True)
    worker.start()


def _set_flow_status(flow_id: str, status: str, **fields: Any) -> None:
    """Update a flow's status under the lock, then strip sensitive fields on terminal transitions."""
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
    """Drive the Codex device-code polling loop until the user authorizes, the flow cancels, or it expires."""
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
            # A cancel/expire that raced with the network calls, or with the wait
            # for the Agent auth lock, must win: persist re-checks the flow and
            # writes in one step, so aborted credentials are never saved.
            _persist_codex_credentials(Path(live["hermes_home"]), tokens, flow_id=flow_id)
            return
        except Exception as exc:
            logger.warning("Codex OAuth onboarding flow failed: %s", exc)
            with _OAUTH_FLOWS_LOCK:
                current = _OAUTH_FLOWS.get(flow_id)
                if _flow_is_open(current):
                    current["status"] = "error"
                    current["updated_at"] = time.time()
                    current["error"] = str(exc)
                    _drop_sensitive_flow_fields(current)
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
        existing = _pending_oauth_flow_for_locked("anthropic", hermes_home)
        if existing is not None:
            flow_id, flow = existing
            return _public_start_payload(flow_id, flow)
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
    _check_expected_profile(body)
    hermes_home = _get_active_hermes_home()
    # Serialize check -> device-code request -> flow insertion -> worker spawn
    # for this provider/profile. The global flow lock is still held only for
    # short in-memory checks/inserts, so unrelated polling/status/cleanup paths
    # are not blocked by the slow provider request.
    with _oauth_start_lock("openai-codex", hermes_home):
        with _OAUTH_FLOWS_LOCK:
            existing = _pending_oauth_flow_for_locked("openai-codex", hermes_home)
            if existing is not None:
                flow_id, flow = existing
                return _public_start_payload(flow_id, flow)

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
            "profile": _get_active_profile_name(),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with _OAUTH_FLOWS_LOCK:
            existing = _pending_oauth_flow_for_locked("openai-codex", hermes_home)
            if existing is not None:
                flow_id, flow = existing
                return _public_start_payload(flow_id, flow)
            _OAUTH_FLOWS[flow_id] = flow

        _spawn_codex_oauth_worker(flow_id)
        return _public_start_payload(flow_id, flow)


def poll_onboarding_oauth_flow(flow_id: str) -> dict[str, Any]:
    """Return the current browser-safe status for an in-flight OAuth flow, expiring it if past its deadline."""
    _cleanup_oauth_flows()
    fid = str(flow_id or "").strip()
    if not fid:
        raise ValueError("flow_id is required")
    with _OAUTH_FLOWS_LOCK:
        flow = _OAUTH_FLOWS.get(fid)
        if not flow:
            raise KeyError("OAuth flow not found")
        if _flow_is_open(flow) and float(flow.get("expires_at") or 0) <= time.time():
            flow["status"] = "expired"
            flow["updated_at"] = time.time()
            _drop_sensitive_flow_fields(flow)
        return _public_status_payload(fid, dict(flow))


def cancel_onboarding_oauth_flow(body: dict[str, Any] | None) -> dict[str, Any]:
    """Cancel a pending OAuth flow by flow_id and return the final status payload."""
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
        if _flow_is_open(flow):
            flow["status"] = "cancelled"
            flow["updated_at"] = time.time()
            _drop_sensitive_flow_fields(flow)
        result = _public_status_payload(fid, dict(flow))
    return result


# Backward-compatible names from the abandoned spike. They intentionally do not
# expose provider device secrets to callers anymore.
def start_codex_device_code():
    """Backward-compatible shim: start a Codex device-code flow via start_onboarding_oauth_flow."""
    return start_onboarding_oauth_flow({"provider": "openai-codex"})


def poll_codex_token(device_code, interval=5):
    """Backward-compatible stub that always yields an error directing callers to the /api/onboarding/oauth/poll endpoint."""
    yield {"status": "error", "error": "Use /api/onboarding/oauth/poll with flow_id"}
