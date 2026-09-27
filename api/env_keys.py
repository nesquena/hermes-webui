"""Hermes Web UI -- custom `.env` key management for WebUI-only users.

A WebUI-only user (no shell, no dashboard) previously had no in-app way to
store a credential a skill needs: the only paths were pasting the secret into
the chat — which puts it in session history and sends it to the model
provider, the exact opposite of what the agent itself recommends — or asking
an operator with shell access to edit `.env` by hand.

This module ports the dashboard's *Custom Keys* section to the WebUI as a
write-mostly API: list the active profile's `.env` keys with **redacted**
previews only, add/replace/delete through the agent's own write path, and
never return a plaintext value.

The agent's writer is the single authority, so the denylist (``PATH``,
``LD_PRELOAD``, ``HERMES_YOLO_MODE``, ...), the name validation, the
non-ASCII credential check, the file-permission preservation and the profile
scoping behave **exactly** as on the dashboard/CLI:

- ``hermes_cli.config.load_env`` reads the active profile's `.env`
- ``hermes_cli.config.save_env_value`` / ``remove_env_value`` write it

Deliberately NOT ported here: ``POST /api/env/reveal`` (the dashboard's
plaintext read). This API is write-mostly by design — a redacted preview is
enough to manage a key, so the reveal surface is left to the dashboard until
there is a reason to add a re-authentication gate for it (#7815).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote

from api.helpers import bad, j

# Mirrors the POSIX-ish shape the writer enforces; kept here only to reject
# obviously malformed names before touching the agent module. The writer's own
# ``validate_env_var_name_for_write`` is the authority.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ENV_PREFIX = "/api/env/keys"
_ENV_KEY_PREFIX = "/api/env/keys/"

# Keys the surrounding WebUI surfaces already own. Listing them here would let
# the generic manager clobber a richer page (Providers, Channels) — the
# dashboard excludes the same families from its Custom Keys section.
_RESERVED_ENV_PREFIXES = ("HERMES_",)
_RESERVED_ENV_EXACT = frozenset({"PATH", "HOME", "USER", "SHELL", "LANG", "TERM"})


def _profile_scope(profile):
    """Enter the requested profile's scope for the agent's env reader/writer.

    Same binding the dashboard uses (``hermes_cli.web_server_profiles``), so a
    WebUI write lands in the SAME per-profile ``.env`` the dashboard would
    touch. An unavailable binding degrades to a no-op scope rather than
    failing the request — the launch profile is still the documented default.
    """
    class _NoScope:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    if not profile:
        return _NoScope()
    try:
        from hermes_cli.web_server_profiles import _profile_scope as _scope
    except Exception:
        return _NoScope()
    try:
        return _scope(profile)
    except Exception:
        return _NoScope()


def _active_profile_env(profile=None):
    """Return ``(env_on_disk, error_response)`` for the requested profile.

    The profile scope belongs to the surrounding request, so the caller passes
    it in; this helper only owns the agent import and its failure mapping.
    """
    try:
        from hermes_cli.config import load_env
    except Exception as exc:  # pragma: no cover - agent module unavailable
        return None, ("import", str(exc))
    try:
        with _profile_scope(profile):
            return load_env(), None
    except Exception as exc:
        return None, ("load", str(exc))


def _redacted(value: str) -> str:
    """Mask the middle of a secret, keeping the first and last 2 chars — enough
    to tell two keys apart, never enough to use."""
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def _is_reserved(name: str) -> bool:
    if name in _RESERVED_ENV_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _RESERVED_ENV_PREFIXES)


def _profile_from_query(parsed) -> str | None:
    query = parse_qs(parsed.query or "")
    values = query.get("profile") or []
    return str(values[0]).strip() if values and str(values[0]).strip() else None


def _write_error(exc: Exception) -> str:
    """Map the writer's refusal to a message that names the rule, without
    leaking the value that tripped it."""
    message = str(exc)
    if "denylist" in message.lower():
        return "That environment variable is on the writer denylist."
    return f"Could not write the key: {message}"


def handle_env_keys_get(handler, parsed) -> bool:
    """GET /api/env/keys — list the profile's `.env` keys, redacted.

    Managed families (provider/channel credentials, Hermes' own config) are
    filtered out: they belong to the Providers/Channels pages, exactly as the
    dashboard excludes them from Custom Keys.
    """
    env_on_disk, error = _active_profile_env(_profile_from_query(parsed))
    if env_on_disk is None:
        kind, detail = error
        return bad(handler, f"Failed to read .env ({kind}): {detail}", status=500)

    keys = []
    for name in sorted(env_on_disk):
        value = env_on_disk.get(name) or ""
        keys.append(
            {
                "name": name,
                "is_set": bool(value),
                "redacted_value": _redacted(value),
                "managed_elsewhere": _is_reserved(name),
            }
        )
    return j(handler, {"keys": keys})


def handle_env_keys_put(handler, parsed, body: dict) -> bool:
    """PUT /api/env/keys — add or replace one key.

    Body: ``{"name": "<NAME>", "value": "<secret>"}``. The value goes browser
    → server → `.env` and is never echoed back: the response carries the same
    redacted shape a GET would.
    """
    name = str(body.get("name") or "").strip()
    value = body.get("value")
    if not name:
        return bad(handler, "name is required")
    if not isinstance(value, str) or value == "":
        return bad(handler, "value is required and must be a non-empty string")
    if not _ENV_NAME_RE.match(name):
        return bad(
            handler,
            "Invalid environment variable name: use letters, digits and "
            "underscores, starting with a letter or underscore.",
        )
    if _is_reserved(name):
        return bad(
            handler,
            f"{name} is managed by another settings page; edit it there.",
            status=409,
        )

    try:
        from hermes_cli.config import save_env_value
    except Exception as exc:  # pragma: no cover - agent module unavailable
        return bad(handler, f"Failed to import the .env writer: {exc}", status=500)
    try:
        with _profile_scope(_profile_from_query(parsed)):
            save_env_value(name, value)
    except Exception as exc:
        return bad(handler, _write_error(exc), status=400)

    return j(
        handler,
        {
            "ok": True,
            "key": {
                "name": name,
                "is_set": True,
                "redacted_value": _redacted(value),
                "managed_elsewhere": False,
            },
        },
    )


def handle_env_key_delete(handler, name: str, parsed=None) -> bool:
    """DELETE /api/env/keys/<name> — remove one key.

    Unknown names are a no-op success: the user's intent (this key must not
    exist) already holds, and a 404 here would only race a second click.
    """
    name = unquote(name or "").strip()
    if not name:
        return bad(handler, "name is required")
    if not _ENV_NAME_RE.match(name):
        return bad(handler, "Invalid environment variable name.")
    if _is_reserved(name):
        return bad(
            handler,
            f"{name} is managed by another settings page; edit it there.",
            status=409,
        )

    try:
        from hermes_cli.config import remove_env_value
    except Exception as exc:  # pragma: no cover - agent module unavailable
        return bad(handler, f"Failed to import the .env writer: {exc}", status=500)
    try:
        with _profile_scope(_profile_from_query(parsed) if parsed is not None else None):
            remove_env_value(name)
    except Exception as exc:
        return bad(handler, _write_error(exc), status=400)

    return j(handler, {"ok": True, "deleted": name})
