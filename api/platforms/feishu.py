"""Backend module for the Feishu (飞书 / Lark) messaging integration.

Pure functions — no HTTP/routing concerns (that lives in the routes layer).

Responsibilities:
  - ``get_config()``    read FEISHU_* from the active-profile .env, masking secrets
  - ``validate()``      probe Feishu credentials via the agent's ``probe_bot``
  - ``save()``          validate/normalize and persist FEISHU_* keys
  - ``restart_gateway()`` run ``hermes gateway restart`` for the active profile

Security rules (see Task 1 spec):
  - Never return FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN / FEISHU_ENCRYPT_KEY
    to the caller — only ``*_set: bool`` booleans.
  - On save, only write a secret when the client sent a real new value (non-empty,
    not the masked sentinel); otherwise leave the existing value intact.
  - The .env file stays 0600 (handled by ``_write_env_file``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# ── Reuse established WebUI helpers (do NOT reinvent) ───────────────────────
from api.profiles import get_active_hermes_home
from api.onboarding import _load_env_file
from api.providers import _write_env_file


# Placeholder the frontend echoes back for already-set secrets. Any value equal
# to this sentinel (or empty / whitespace) means "not provided, keep existing".
MASKED_SENTINEL = "__FEISHU_SECRET_SET__"

# Secret-style keys that must never be returned to the caller and are only
# written when a real new value is supplied.
_SECRET_KEYS = {
    "app_secret": "FEISHU_APP_SECRET",
    "verification_token": "FEISHU_VERIFICATION_TOKEN",
    "encrypt_key": "FEISHU_ENCRYPT_KEY",
}

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off", ""}


class FeishuConfigError(ValueError):
    """Raised on invalid Feishu configuration input."""


# ── Defensive agent import for the Feishu probe (pattern: api/streaming.py) ──
try:
    from gateway.platforms.feishu import probe_bot as _probe_bot  # type: ignore
except ImportError:
    _probe_bot = None


def _get_probe_bot():
    """Return the agent's ``probe_bot`` callable, retrying the import.

    The agent dir is appended to ``sys.path`` by ``api.config`` at import time,
    but the agent package may not be importable yet at first import (e.g. in
    Docker with a volume-mounted agent). Re-attempt lazily.
    """
    global _probe_bot
    if _probe_bot is None:
        try:
            from gateway.platforms.feishu import probe_bot as _fn  # noqa: PLC0415
            _probe_bot = _fn
        except ImportError:
            pass
    return _probe_bot


# ── Helpers ────────────────────────────────────────────────────────────────


def _env_path() -> Path:
    return get_active_hermes_home() / ".env"


def _parse_bool(value: object, default: bool) -> bool:
    """Lenient bool parse from a .env string value."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def _coerce_bool(value: object) -> bool:
    """Coerce a payload value to a strict bool (for save)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


def _secret_provided(value: object) -> bool:
    """True only when the client sent a real new secret value."""
    if value is None:
        return False
    text = str(value)
    if not text.strip():
        return False
    if text == MASKED_SENTINEL:
        return False
    return True


# ── Public API ──────────────────────────────────────────────────────────────


def get_config() -> dict:
    """Read FEISHU_* from the active-profile .env, masking secrets.

    Returns all fields (with documented defaults applied) so the frontend can
    render the full form. Secrets are never returned — only ``*_set`` booleans.
    """
    env = _load_env_file(_env_path())

    app_id = env.get("FEISHU_APP_ID", "") or ""
    app_secret_set = bool(env.get("FEISHU_APP_SECRET", "").strip())

    return {
        "configured": bool(app_id.strip()) and app_secret_set,
        "app_id": app_id,
        "app_secret_set": app_secret_set,
        "domain": env.get("FEISHU_DOMAIN", "feishu") or "feishu",
        "connection_mode": env.get("FEISHU_CONNECTION_MODE", "websocket")
        or "websocket",
        "webhook_host": env.get("FEISHU_WEBHOOK_HOST", "127.0.0.1") or "127.0.0.1",
        "webhook_port": env.get("FEISHU_WEBHOOK_PORT", "8765") or "8765",
        "webhook_path": env.get("FEISHU_WEBHOOK_PATH", "/feishu/webhook")
        or "/feishu/webhook",
        "verification_token_set": bool(
            env.get("FEISHU_VERIFICATION_TOKEN", "").strip()
        ),
        "encrypt_key_set": bool(env.get("FEISHU_ENCRYPT_KEY", "").strip()),
        "allow_all_users": _parse_bool(env.get("FEISHU_ALLOW_ALL_USERS"), False),
        "allowed_users": env.get("FEISHU_ALLOWED_USERS", "") or "",
        "group_policy": env.get("FEISHU_GROUP_POLICY", "open") or "open",
        "require_mention": _parse_bool(env.get("FEISHU_REQUIRE_MENTION"), True),
        "home_channel": env.get("FEISHU_HOME_CHANNEL", "") or "",
    }


def validate(app_id: str, app_secret: str, domain: str) -> dict:
    """Probe Feishu credentials via the agent's ``probe_bot``.

    Returns ``{ok: bool, bot_name?, bot_open_id?, error?}``. If the agent/probe
    is unavailable returns ``{ok: False, error: "agent unavailable"}``.
    """
    probe = _get_probe_bot()
    if probe is None:
        return {"ok": False, "error": "agent unavailable"}
    try:
        result = probe(app_id, app_secret, domain)
    except Exception as exc:  # map any probe failure to a structured error
        return {"ok": False, "error": str(exc)}
    if result is None:
        return {"ok": False, "error": "invalid credentials"}
    if not isinstance(result, dict):
        return {"ok": False, "error": "invalid probe response"}
    return {
        "ok": True,
        "bot_name": result.get("bot_name"),
        "bot_open_id": result.get("bot_open_id"),
    }


def save(payload: dict) -> dict:
    """Validate/normalize and persist FEISHU_* keys.

    Honors the secret-only-if-provided rule and writes webhook fields only in
    webhook mode. Returns ``{saved: True, fields: [<FEISHU_* keys written>]}``.
    Raises :class:`FeishuConfigError` on invalid input.
    """
    if not isinstance(payload, dict):
        raise FeishuConfigError("payload must be an object")

    existing = _load_env_file(_env_path())
    updates: dict[str, str] = {}

    # ── Required: app_id (present in payload OR already set on disk) ──
    app_id = payload.get("app_id")
    if app_id is not None:
        app_id = str(app_id).strip()
        if app_id:
            updates["FEISHU_APP_ID"] = app_id
    has_app_id = bool(app_id) or bool(existing.get("FEISHU_APP_ID", "").strip())
    if not has_app_id:
        raise FeishuConfigError("FEISHU_APP_ID is required")

    # ── Required: app_secret (new value OR already set on disk) ──
    if _secret_provided(payload.get("app_secret")):
        updates["FEISHU_APP_SECRET"] = str(payload["app_secret"]).strip()
    has_secret = (
        "FEISHU_APP_SECRET" in updates
        or bool(existing.get("FEISHU_APP_SECRET", "").strip())
    )
    if not has_secret:
        raise FeishuConfigError("FEISHU_APP_SECRET is required")

    # ── Enums ──
    domain = str(payload.get("domain", "feishu") or "feishu").strip().lower()
    if domain not in {"feishu", "lark"}:
        raise FeishuConfigError("domain must be 'feishu' or 'lark'")
    updates["FEISHU_DOMAIN"] = domain

    mode = str(
        payload.get("connection_mode", "websocket") or "websocket"
    ).strip().lower()
    if mode not in {"websocket", "webhook"}:
        raise FeishuConfigError("connection_mode must be 'websocket' or 'webhook'")
    updates["FEISHU_CONNECTION_MODE"] = mode

    group_policy = str(
        payload.get("group_policy", "open") or "open"
    ).strip().lower()
    if group_policy not in {"open", "disabled"}:
        raise FeishuConfigError("group_policy must be 'open' or 'disabled'")
    updates["FEISHU_GROUP_POLICY"] = group_policy

    # ── Bools (written as "true"/"false") ──
    updates["FEISHU_ALLOW_ALL_USERS"] = (
        "true" if _coerce_bool(payload.get("allow_all_users", False)) else "false"
    )
    updates["FEISHU_REQUIRE_MENTION"] = (
        "true" if _coerce_bool(payload.get("require_mention", True)) else "false"
    )

    # ── Optional plain fields ──
    if "allowed_users" in payload:
        updates["FEISHU_ALLOWED_USERS"] = str(payload.get("allowed_users") or "").strip()
    if "home_channel" in payload:
        updates["FEISHU_HOME_CHANNEL"] = str(payload.get("home_channel") or "").strip()

    # ── Webhook fields: only in webhook mode ──
    if mode == "webhook":
        updates["FEISHU_WEBHOOK_HOST"] = str(
            payload.get("webhook_host", "127.0.0.1") or "127.0.0.1"
        ).strip()
        updates["FEISHU_WEBHOOK_PORT"] = str(
            payload.get("webhook_port", "8765") or "8765"
        ).strip()
        updates["FEISHU_WEBHOOK_PATH"] = str(
            payload.get("webhook_path", "/feishu/webhook") or "/feishu/webhook"
        ).strip()
        if _secret_provided(payload.get("verification_token")):
            updates["FEISHU_VERIFICATION_TOKEN"] = str(
                payload["verification_token"]
            ).strip()
        if _secret_provided(payload.get("encrypt_key")):
            updates["FEISHU_ENCRYPT_KEY"] = str(payload["encrypt_key"]).strip()

    # Drop any empty values so we never blank out keys via empty strings.
    updates = {k: v for k, v in updates.items() if v != ""}

    _write_env_file(_env_path(), updates)
    return {"saved": True, "fields": sorted(updates.keys())}


def restart_gateway() -> dict:
    """Run ``hermes gateway restart`` targeting the active profile.

    Locates the CLI via PATH then ``~/.local/bin/hermes``. Never raises for an
    operational failure — always returns ``{ok, detail}``.
    """
    hermes = shutil.which("hermes")
    if not hermes:
        fallback = Path.home() / ".local" / "bin" / "hermes"
        if fallback.exists():
            hermes = str(fallback)
    if not hermes:
        return {"ok": False, "detail": "hermes CLI not found"}

    env = {**os.environ, "HERMES_HOME": str(get_active_hermes_home())}
    try:
        proc = subprocess.run(
            [hermes, "gateway", "restart"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "hermes gateway restart timed out"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}

    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return {"ok": proc.returncode == 0, "detail": detail}
