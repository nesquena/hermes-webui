"""Backend module for the WeCom (企业微信 / WeChat Work) messaging integration.

Pure functions — no HTTP/routing concerns (that lives in the routes layer). This
mirrors ``api/platforms/feishu.py`` field-for-field; WeCom just carries two
distinct connection modes (a WebSocket smart-bot and a callback self-built app),
each with its own group of ``WECOM_*`` keys.

Responsibilities:
  - ``get_config()``    read WECOM_* from the active-profile .env, masking secrets
  - ``validate()``      probe WeCom credentials via the agent's ``probe_bot`` if
                        present; otherwise fall back to non-empty/format checks
  - ``save()``          validate/normalize and persist the active mode's WECOM_* keys
  - ``restart_gateway()`` run ``hermes gateway restart`` for the active profile

Security rules (mirror Feishu, Task 1 spec):
  - Never return WECOM_SECRET / WECOM_CALLBACK_CORP_SECRET / WECOM_CALLBACK_TOKEN /
    WECOM_CALLBACK_ENCODING_AES_KEY to the caller — only ``*_set: bool`` booleans.
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
MASKED_SENTINEL = "__WECOM_SECRET_SET__"

# Valid connection modes (mirrors Feishu's connection_mode enum).
_MODES = {"wecom", "wecom_callback"}

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off", ""}


class WecomConfigError(ValueError):
    """Raised on invalid WeCom configuration input."""


# ── Defensive agent import for the WeCom probe (pattern: api/streaming.py) ──
# The agent's wecom adapter has no credential-probe helper today; we import
# defensively in case one is added later. When absent, ``validate`` falls back
# to a basic non-empty/format check (never fabricates a live WeCom API call).
try:
    from gateway.platforms.wecom import probe_bot as _probe_bot  # type: ignore
except ImportError:
    _probe_bot = None


def _get_probe_bot():
    """Return the agent's WeCom ``probe_bot`` callable if one exists.

    The agent dir is appended to ``sys.path`` by ``api.config`` at import time,
    but the agent package may not be importable yet at first import (e.g. in
    Docker with a volume-mounted agent). Re-attempt lazily. Returns ``None`` when
    no probe is available (the common case — WeCom has none yet).
    """
    global _probe_bot
    if _probe_bot is None:
        try:
            from gateway.platforms.wecom import probe_bot as _fn  # noqa: PLC0415
            _probe_bot = _fn
        except ImportError:
            pass
    return _probe_bot


# ── Helpers ────────────────────────────────────────────────────────────────


def _env_path() -> Path:
    return get_active_hermes_home() / ".env"


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
    """Read WECOM_* from the active-profile .env, masking secrets.

    Returns all fields for both modes (with documented defaults applied) so the
    frontend can render the full form. Secrets are never returned — only
    ``*_set`` booleans. ``mode`` reflects the active connection mode: ``wecom``
    (WebSocket bot) or ``wecom_callback`` (callback self-built app).
    """
    env = _load_env_file(_env_path())

    # ── Mode A: WebSocket smart bot ──
    bot_id = env.get("WECOM_BOT_ID", "") or ""
    secret_set = bool(env.get("WECOM_SECRET", "").strip())

    # ── Mode B: callback self-built app ──
    cb_corp_id = env.get("WECOM_CALLBACK_CORP_ID", "") or ""
    cb_corp_secret_set = bool(env.get("WECOM_CALLBACK_CORP_SECRET", "").strip())
    cb_agent_id = env.get("WECOM_CALLBACK_AGENT_ID", "") or ""
    cb_token_set = bool(env.get("WECOM_CALLBACK_TOKEN", "").strip())
    cb_aes_set = bool(env.get("WECOM_CALLBACK_ENCODING_AES_KEY", "").strip())

    # The active mode is whichever group has its required identity populated;
    # default to the WebSocket bot (recommended) when neither / both are set.
    stored_mode = (env.get("WECOM_MODE", "") or "").strip().lower()
    if stored_mode in _MODES:
        mode = stored_mode
    elif cb_corp_id.strip() and not bot_id.strip():
        mode = "wecom_callback"
    else:
        mode = "wecom"

    ws_configured = bool(bot_id.strip()) and secret_set
    cb_configured = (
        bool(cb_corp_id.strip())
        and cb_corp_secret_set
        and bool(cb_agent_id.strip())
        and cb_token_set
        and cb_aes_set
    )
    configured = cb_configured if mode == "wecom_callback" else ws_configured

    return {
        "configured": configured,
        "mode": mode,
        # ── Mode A: WebSocket bot ──
        "bot_id": bot_id,
        "secret_set": secret_set,
        "websocket_url": env.get("WECOM_WEBSOCKET_URL", "wss://openws.work.weixin.qq.com")
        or "wss://openws.work.weixin.qq.com",
        "dm_policy": env.get("WECOM_DM_POLICY", "open") or "open",
        "allowed_users": env.get("WECOM_ALLOWED_USERS", "") or "",
        "group_policy": env.get("WECOM_GROUP_POLICY", "open") or "open",
        "home_channel": env.get("WECOM_HOME_CHANNEL", "") or "",
        # ── Mode B: callback self-built app ──
        "callback_corp_id": cb_corp_id,
        "callback_corp_secret_set": cb_corp_secret_set,
        "callback_agent_id": cb_agent_id,
        "callback_token_set": cb_token_set,
        "callback_encoding_aes_key_set": cb_aes_set,
        "callback_host": env.get("WECOM_CALLBACK_HOST", "0.0.0.0") or "0.0.0.0",
        "callback_port": env.get("WECOM_CALLBACK_PORT", "8645") or "8645",
    }


def validate(payload: dict) -> dict:
    """Validate WeCom credentials for the requested mode.

    Probes via the agent's ``probe_bot`` if one is available; otherwise performs
    a basic non-empty/format check (the agent's WeCom adapter ships no credential
    probe, so we do NOT fabricate a live WeCom API call). Returns
    ``{ok: bool, error?: str}``.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be an object"}

    mode = str(payload.get("mode", "wecom") or "wecom").strip().lower()
    if mode not in _MODES:
        return {"ok": False, "error": "mode must be 'wecom' or 'wecom_callback'"}

    # ── Prefer a real agent probe when one exists (future-proofing) ──
    probe = _get_probe_bot()
    if probe is not None:
        try:
            result = probe(payload)
        except Exception as exc:  # map any probe failure to a structured error
            return {"ok": False, "error": str(exc)}
        if result is None:
            return {"ok": False, "error": "invalid credentials"}
        if not isinstance(result, dict):
            return {"ok": False, "error": "invalid probe response"}
        return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}

    # ── Fallback: basic non-empty checks for the active mode's required fields ──
    if mode == "wecom":
        if not str(payload.get("bot_id", "") or "").strip():
            return {"ok": False, "error": "WECOM_BOT_ID is required"}
        if not _secret_provided(payload.get("secret")):
            return {"ok": False, "error": "WECOM_SECRET is required"}
    else:  # wecom_callback
        if not str(payload.get("callback_corp_id", "") or "").strip():
            return {"ok": False, "error": "WECOM_CALLBACK_CORP_ID is required"}
        if not _secret_provided(payload.get("callback_corp_secret")):
            return {"ok": False, "error": "WECOM_CALLBACK_CORP_SECRET is required"}
        if not str(payload.get("callback_agent_id", "") or "").strip():
            return {"ok": False, "error": "WECOM_CALLBACK_AGENT_ID is required"}
        if not _secret_provided(payload.get("callback_token")):
            return {"ok": False, "error": "WECOM_CALLBACK_TOKEN is required"}
        if not _secret_provided(payload.get("callback_encoding_aes_key")):
            return {"ok": False, "error": "WECOM_CALLBACK_ENCODING_AES_KEY is required"}
    return {"ok": True}


def save(payload: dict) -> dict:
    """Validate/normalize and persist the active mode's WECOM_* keys.

    Honors the secret-only-if-provided rule and writes only the keys for the
    selected ``mode``. Returns ``{saved: True, fields: [<WECOM_* keys written>]}``.
    Raises :class:`WecomConfigError` on invalid input.
    """
    if not isinstance(payload, dict):
        raise WecomConfigError("payload must be an object")

    existing = _load_env_file(_env_path())
    updates: dict[str, str] = {}

    mode = str(payload.get("mode", "wecom") or "wecom").strip().lower()
    if mode not in _MODES:
        raise WecomConfigError("mode must be 'wecom' or 'wecom_callback'")
    updates["WECOM_MODE"] = mode

    if mode == "wecom":
        # ── Required: bot_id (present in payload OR already set on disk) ──
        bot_id = payload.get("bot_id")
        if bot_id is not None:
            bot_id = str(bot_id).strip()
            if bot_id:
                updates["WECOM_BOT_ID"] = bot_id
        has_bot_id = bool(bot_id) or bool(existing.get("WECOM_BOT_ID", "").strip())
        if not has_bot_id:
            raise WecomConfigError("WECOM_BOT_ID is required")

        # ── Required: secret (new value OR already set on disk) ──
        if _secret_provided(payload.get("secret")):
            updates["WECOM_SECRET"] = str(payload["secret"]).strip()
        has_secret = (
            "WECOM_SECRET" in updates
            or bool(existing.get("WECOM_SECRET", "").strip())
        )
        if not has_secret:
            raise WecomConfigError("WECOM_SECRET is required")

        updates["WECOM_WEBSOCKET_URL"] = str(
            payload.get("websocket_url", "wss://openws.work.weixin.qq.com")
            or "wss://openws.work.weixin.qq.com"
        ).strip()

        dm_policy = str(payload.get("dm_policy", "open") or "open").strip().lower()
        if dm_policy not in {"open", "allowlist", "disabled", "pairing"}:
            raise WecomConfigError(
                "dm_policy must be 'open', 'allowlist', 'disabled' or 'pairing'"
            )
        updates["WECOM_DM_POLICY"] = dm_policy

        group_policy = str(
            payload.get("group_policy", "open") or "open"
        ).strip().lower()
        if group_policy not in {"open", "allowlist", "disabled"}:
            raise WecomConfigError(
                "group_policy must be 'open', 'allowlist' or 'disabled'"
            )
        updates["WECOM_GROUP_POLICY"] = group_policy

        if "allowed_users" in payload:
            updates["WECOM_ALLOWED_USERS"] = str(
                payload.get("allowed_users") or ""
            ).strip()
        if "home_channel" in payload:
            updates["WECOM_HOME_CHANNEL"] = str(
                payload.get("home_channel") or ""
            ).strip()
    else:  # ── Mode B: callback self-built app ──
        corp_id = payload.get("callback_corp_id")
        if corp_id is not None:
            corp_id = str(corp_id).strip()
            if corp_id:
                updates["WECOM_CALLBACK_CORP_ID"] = corp_id
        has_corp_id = bool(corp_id) or bool(
            existing.get("WECOM_CALLBACK_CORP_ID", "").strip()
        )
        if not has_corp_id:
            raise WecomConfigError("WECOM_CALLBACK_CORP_ID is required")

        agent_id = payload.get("callback_agent_id")
        if agent_id is not None:
            agent_id = str(agent_id).strip()
            if agent_id:
                updates["WECOM_CALLBACK_AGENT_ID"] = agent_id
        has_agent_id = bool(agent_id) or bool(
            existing.get("WECOM_CALLBACK_AGENT_ID", "").strip()
        )
        if not has_agent_id:
            raise WecomConfigError("WECOM_CALLBACK_AGENT_ID is required")

        # ── Secrets: corp_secret / token / encoding_aes_key ──
        if _secret_provided(payload.get("callback_corp_secret")):
            updates["WECOM_CALLBACK_CORP_SECRET"] = str(
                payload["callback_corp_secret"]
            ).strip()
        if not (
            "WECOM_CALLBACK_CORP_SECRET" in updates
            or bool(existing.get("WECOM_CALLBACK_CORP_SECRET", "").strip())
        ):
            raise WecomConfigError("WECOM_CALLBACK_CORP_SECRET is required")

        if _secret_provided(payload.get("callback_token")):
            updates["WECOM_CALLBACK_TOKEN"] = str(payload["callback_token"]).strip()
        if not (
            "WECOM_CALLBACK_TOKEN" in updates
            or bool(existing.get("WECOM_CALLBACK_TOKEN", "").strip())
        ):
            raise WecomConfigError("WECOM_CALLBACK_TOKEN is required")

        if _secret_provided(payload.get("callback_encoding_aes_key")):
            updates["WECOM_CALLBACK_ENCODING_AES_KEY"] = str(
                payload["callback_encoding_aes_key"]
            ).strip()
        if not (
            "WECOM_CALLBACK_ENCODING_AES_KEY" in updates
            or bool(existing.get("WECOM_CALLBACK_ENCODING_AES_KEY", "").strip())
        ):
            raise WecomConfigError("WECOM_CALLBACK_ENCODING_AES_KEY is required")

        updates["WECOM_CALLBACK_HOST"] = str(
            payload.get("callback_host", "0.0.0.0") or "0.0.0.0"
        ).strip()
        updates["WECOM_CALLBACK_PORT"] = str(
            payload.get("callback_port", "8645") or "8645"
        ).strip()

    # Drop any empty values so we never blank out keys via empty strings.
    # (WECOM_ALLOWED_USERS / WECOM_HOME_CHANNEL are legitimately optional and
    # may be empty — those keys are simply not persisted when blank.)
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
