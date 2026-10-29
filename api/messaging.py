"""Profile-scoped messaging-channel settings for WebUI Settings (#8071).

WebUI-only users can create profiles but had no surface to attach a messaging
bot to one: profile cloning deliberately strips channel credentials (so two
gateways never fight over one bot), and the only other entry points were the
CLI (``hermes -p <p> setup gateway``) or the dashboard's Messaging page — both
unavailable to an end user who only has the WebUI.

This module is the Telegram-first port of that dashboard surface, scoped to the
ACTIVE profile. It is deliberately NOT a generic ``.env`` editor:

* **Write-mostly.** Only ``configured`` / ``enabled`` status and the allowlist
  are ever returned. Bot tokens are never read back into an HTTP response.
* **Candidate-first.** A token is validated (``getMe``) BEFORE it is persisted,
  so an unvalidated secret never reaches a profile's ``.env``.
* **Single-owner.** A token already configured on another profile on this host
  is refused: only one poller can hold a Telegram bot token, so a duplicate is
  a silent conflict rather than a working second bot.
* **Profile-local.** Writes land only in the target profile's own ``.env``, and
  an apply targets that profile's gateway — never a shared multiplexer restart
  that would disturb the other profiles.

Env-var names mirror the Agent contract (``gateway/config.py``
``PLATFORM_TOKEN_ENV_NAMES``, and the ``<PLATFORM>_ALLOWED_USERS`` allowlist
convention) so the value written here is exactly what the Gateway reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Candidate-token validation talks to the real Telegram API; keep it short so a
# hung DNS/TLS path can't stall the settings request.
_TELEGRAM_GETME_TIMEOUT = 10.0
_TELEGRAM_API_BASE = "https://api.telegram.org"


class PlatformSpec:
    """Static, platform-specific configuration for one messaging channel."""

    __slots__ = ("id", "label", "token_env", "allowed_users_env", "allow_all_env")

    def __init__(
        self,
        platform_id: str,
        label: str,
        token_env: str,
        allowed_users_env: str,
        allow_all_env: str,
    ):
        self.id = platform_id
        self.label = label
        self.token_env = token_env
        self.allowed_users_env = allowed_users_env
        self.allow_all_env = allow_all_env


# Telegram-first (#8071). Other platforms follow the same shape: add a spec and
# a `<platform>_get_me`-equivalent validator.
PLATFORMS: dict[str, PlatformSpec] = {
    "telegram": PlatformSpec(
        platform_id="telegram",
        label="Telegram",
        token_env="TELEGRAM_BOT_TOKEN",
        allowed_users_env="TELEGRAM_ALLOWED_USERS",
        allow_all_env="TELEGRAM_ALLOW_ALL_USERS",
    ),
}

# Telegram user IDs are numeric; `@username` form is also accepted. Reject
# anything else so the allowlist cannot smuggle a newline/comma into the .env.
_ALLOWED_USER_RE = re.compile(r"^(?:\d+|@[A-Za-z0-9_]{1,64})$")


class MessagingError(Exception):
    """Operator-visible failure carrying the HTTP status to return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ── env reading ──────────────────────────────────────────────────────────────


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse a ``.env`` file into a mapping (best effort, order-independent)."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def read_profile_env(profile_home: Path) -> dict[str, str]:
    """Read a profile's ``.env`` into a mapping. Missing file → empty mapping."""
    env_path = Path(profile_home) / ".env"
    try:
        return _parse_env_text(env_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("Failed to read %s", env_path, exc_info=True)
        return {}


def _active_profile_name() -> str:
    from api.profiles import get_active_profile_name

    return str(get_active_profile_name() or "default")


def _profile_home(profile: str):
    from api.profiles import get_hermes_home_for_profile

    return Path(get_hermes_home_for_profile(profile))


def _profile_names() -> list[str]:
    """Every profile known to this WebUI, best effort."""
    try:
        from api.profiles import list_profiles_api

        names: list[str] = []
        for row in list_profiles_api() or []:
            name = row.get("name") if isinstance(row, dict) else row
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        if names:
            return names
    except Exception:
        logger.debug("Profile enumeration failed for duplicate-token check", exc_info=True)
    return [_active_profile_name()]


# ── candidate validation ─────────────────────────────────────────────────────


def _telegram_get_me(token: str) -> tuple[bool, str]:
    """Validate an UNSAVED candidate bot token via Telegram ``getMe``.

    Returns ``(ok, detail)``. ``detail`` is safe to surface to the operator and
    never contains the token.
    """
    url = f"{_TELEGRAM_API_BASE}/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=_TELEGRAM_GETME_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # 404 == unknown token; 401 == revoked. Both mean "not a usable bot".
        if exc.code in (401, 403, 404):
            return False, "Telegram rejected this bot token."
        return False, f"Telegram validation failed (HTTP {exc.code})."
    except urllib.error.URLError:
        return False, "Could not reach Telegram to validate this bot token."
    except Exception:
        logger.debug("Telegram candidate validation failed", exc_info=True)
        return False, "Could not validate this bot token."

    if not isinstance(payload, dict) or not payload.get("ok"):
        return False, "Telegram rejected this bot token."
    result = payload.get("result")
    username = ""
    if isinstance(result, dict):
        username = str(result.get("username") or "").strip()
    return True, f"@{username}" if username else "Telegram accepted this bot token."


# Validators are registered BY NAME and resolved through the module namespace at
# call time. Holding the function object here would bind the import-time
# reference, so the seam every test and future caller patches
# (``messaging._telegram_get_me``) would be bypassed and the real network
# validator would run instead.
_VALIDATORS = {"telegram": "_telegram_get_me"}


# ── single-owner / duplicate-token check ─────────────────────────────────────


def find_token_owner(
    token: str, *, exclude_profile: str, token_env: str = "TELEGRAM_BOT_TOKEN"
) -> str | None:
    """Return the name of another profile already polling ``token``, if any.

    A bot token admits exactly one long-polling consumer, so a token
    configured on two profiles on the same host means one bot silently loses
    its updates. Refusing the duplicate is safer than writing it.

    ``token_env`` is the platform's own credential key, so adding a second
    platform cannot silently keep scanning the Telegram key.
    """
    if not token:
        return None
    for name in _profile_names():
        if name == exclude_profile:
            continue
        try:
            home = _profile_home(name)
        except Exception:
            logger.debug("Skipping unresolvable profile %s in ownership check", name, exc_info=True)
            continue
        configured = read_profile_env(home).get(token_env, "").strip()
        if configured and _constant_time_equals(configured, token):
            return name
    return None


def _constant_time_equals(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


# ── status (write-mostly: never returns token values) ─────────────────────────


def _explicitly_disabled(profile_home: Path, platform_id: str) -> bool:
    """Honour an explicit ``platforms.<id>.enabled: false`` in config.yaml."""
    # PyYAML is not guaranteed in the managed runtime (ruamel-only); the
    # compat shim provides safe_load either way. #yaml-ruamel-fallback
    from api import yaml_compat as _yaml

    config_path = Path(profile_home) / "config.yaml"
    try:
        payload = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    platforms = payload.get("platforms")
    if not isinstance(platforms, dict):
        return False
    entry = platforms.get(platform_id)
    if not isinstance(entry, dict):
        return False
    return entry.get("enabled") is False


def _parse_allowed_users(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def platform_status(spec: PlatformSpec, profile_home: Path) -> dict:
    """Report channel status for one profile WITHOUT exposing the token."""
    env = read_profile_env(profile_home)
    token = env.get(spec.token_env, "").strip()
    configured = bool(token)
    return {
        "id": spec.id,
        "label": spec.label,
        "configured": configured,
        "enabled": configured and not _explicitly_disabled(profile_home, spec.id),
        "allowed_users": _parse_allowed_users(env.get(spec.allowed_users_env, "")),
        "allow_all_users": str(env.get(spec.allow_all_env, "")).strip().lower()
        in {"1", "true", "yes"},
    }


def list_platforms(profile: str | None = None) -> dict:
    """List every supported channel's status for ``profile`` (default: active)."""
    name = profile or _active_profile_name()
    home = _profile_home(name)
    return {
        "profile": name,
        "platforms": [platform_status(spec, home) for spec in PLATFORMS.values()],
    }


# ── write path ───────────────────────────────────────────────────────────────


def _normalise_allowed_users(values: Iterable | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        candidates = [part.strip() for part in values.split(",")]
    else:
        candidates = [str(item).strip() for item in values]
    cleaned: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        if "\n" in candidate or "\r" in candidate:
            raise MessagingError("Allowed users must not contain newline characters.")
        if not _ALLOWED_USER_RE.match(candidate):
            raise MessagingError(
                f"'{candidate}' is not a valid Telegram user id or @username."
            )
        if candidate not in cleaned:
            cleaned.append(candidate)
    return cleaned


def update_platform(
    platform_id: str,
    token: str | None,
    *,
    allowed_users: Iterable | None = None,
    allow_all_users: bool | None = None,
    apply: bool = False,
    profile: str | None = None,
) -> dict:
    """Validate then persist a channel config for one profile.

    Order matters: structural checks → single-owner check → candidate
    validation → persist. Nothing is written until the token has been proven
    usable and unclaimed.
    """
    key = str(platform_id or "").strip().lower()
    spec = PLATFORMS.get(key)
    if spec is None:
        raise MessagingError(f"Unsupported messaging platform: {platform_id!r}", status=404)

    candidate = "" if token is None else str(token).strip()
    if not candidate:
        raise MessagingError(f"A {spec.label} bot token is required.")
    # A newline would let a candidate smuggle a second env key past validation.
    if "\n" in candidate or "\r" in candidate:
        raise MessagingError("Bot token must not contain newline characters.")

    users = _normalise_allowed_users(allowed_users)

    name = profile or _active_profile_name()
    home = _profile_home(name)

    # Ownership before validation: no point spending a network round-trip on a
    # token this host already knows it cannot use.
    owner = find_token_owner(candidate, exclude_profile=name, token_env=spec.token_env)
    if owner is not None:
        raise MessagingError(
            f"This bot token is already configured for profile '{owner}'. "
            "Only one profile can poll a given bot token.",
            status=409,
        )

    validator_name = _VALIDATORS.get(spec.id)
    validator = globals().get(validator_name) if validator_name else None
    if validator is None:  # pragma: no cover - defensive
        raise MessagingError(f"No validator registered for platform {spec.id!r}")
    ok, detail = validator(candidate)
    if not ok:
        raise MessagingError(f"{spec.label}: {detail}", status=400)

    updates: dict[str, str | None] = {spec.token_env: candidate}
    if allowed_users is not None:
        updates[spec.allowed_users_env] = ",".join(users) or None
    if allow_all_users is not None:
        updates[spec.allow_all_env] = "true" if allow_all_users else None

    # Reuse the providers' env writer: same lock, same atomic replace, same
    # comment/order preservation, same newline rejection and 0600 tightening.
    from api.providers import _write_env_file

    _write_env_file(Path(home) / ".env", updates)

    # Make the value visible to this process' dotenv without re-execing the
    # server, mirroring what _reload_dotenv does on profile switch.
    for key_name, value in updates.items():
        if value is None:
            os.environ.pop(key_name, None)
        else:
            os.environ[key_name] = value

    result = {
        "profile": name,
        "platform": platform_status(spec, home),
        "validated": detail,
        "applied": False,
    }

    if apply:
        from api.gateway_restart import restart_active_profile_gateway

        # Profile-scoped apply: targets this profile's gateway only.
        restart = restart_active_profile_gateway(profile=name)
        result["applied"] = True
        result["gateway_restart"] = {
            "status": str(restart.get("status") or ""),
            "message": str(restart.get("message") or ""),
        }

    return result