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
from urllib.parse import unquote

from api.helpers import bad, j

# Mirrors the POSIX-ish shape the writer enforces; kept here only to reject
# obviously malformed names before touching the agent module. The writer's own
# ``validate_env_var_name_for_write`` is the authority.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvKeyProfileError(Exception):
    """The request has no provable profile home to act on (fail closed)."""

_ENV_PREFIX = "/api/env/keys"
_ENV_KEY_PREFIX = "/api/env/keys/"

# Keys the surrounding WebUI surfaces already own. Listing them here would let
# the generic manager clobber a richer page (Providers, Channels) — the
# dashboard excludes the same families from its Custom Keys section.
_RESERVED_ENV_PREFIXES = ("HERMES_",)
_RESERVED_ENV_EXACT = frozenset({"PATH", "HOME", "USER", "SHELL", "LANG", "TERM"})


def _authorized_profile() -> str:
    """Return the profile THIS request is authorized to act on.

    WebUI auth already establishes the active/bound profile per request
    (the ``hermes_profile`` cookie → thread-local, an isolated-profile
    deployment's own name, or the process-level default). That value is the
    authorization boundary: it is the home whose settings this browser
    session is allowed to read and mutate.

    The caller-supplied ``?profile=`` selector is deliberately NOT accepted.
    Resolving a valid profile is not the same as being authorized to act on
    it: with the query parameter, a request bound to profile A could read
    profile B's key names, value lengths and previews, and could write or
    delete B's credentials — an authorization mismatch, not a
    path-traversal issue (#7870 review). The dashboard's profile switcher
    is a UI affordance for an already-authenticated session; it is not an
    HTTP authorization mechanism.

    Raises ``EnvKeyProfileError`` when the active profile cannot be
    resolved: without a provable owner there is no home to act on, and
    guessing (or silently falling back to the launch/default profile)
    would mutate a profile the caller never proved they belong to.
    """
    try:
        from api.profiles import get_active_profile_name
    except Exception as exc:  # pragma: no cover - agent module unavailable
        raise EnvKeyProfileError(f"cannot resolve the active profile: {exc}") from exc
    name = get_active_profile_name()
    if not isinstance(name, str) or not name.strip():
        raise EnvKeyProfileError(
            "active profile is unresolved; refusing to act on an unknown home"
        )
    # Root aliases ("default" and any name that resolves to ~/.hermes) are
    # ONE home: a request bound to a renamed root profile must address the
    # same .env the dashboard would, not a second namespace that happens to
    # be spelled differently.
    return _canonical_profile_name(name.strip())


def _canonical_profile_name(name: str) -> str:
    """Collapse every spelling of the root profile onto ``'default'``."""
    if not name or name == "default":
        return "default"
    try:
        from api.profiles import _is_root_profile

        if _is_root_profile(name):
            return "default"
    except Exception:
        # Lookup unavailable: keep the concrete name. A wrong-but-concrete
        # profile is confined to that profile's home; collapsing every name
        # to the root on a failed lookup would cross-tenant them.
        pass
    return name


def _profile_scope(profile):
    """Enter ``profile``'s scope for the agent's env reader/writer.

    ``profile`` is always the AUTHORIZED profile for the request (see
    :func:`_authorized_profile`) — never a query parameter. A scope that
    cannot be constructed is an error the caller must see: the previous
    behaviour returned a no-op context, which let a request answer against
    the process's launch/home ``.env`` (i.e. the DEFAULT profile) instead of
    the caller's — a silent cross-profile mutation reported as success
    (#7870 review, "wrong-home default").

    Raises ``EnvKeyProfileError`` on any binding failure.
    """
    if not profile:
        raise EnvKeyProfileError("no profile to scope the .env access to")

    class _NoScope:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    try:
        from hermes_cli.web_server_profiles import _profile_scope as _scope
    except Exception:
        raise EnvKeyProfileError(
            "the agent profile binding is unavailable; refusing to touch a .env"
        ) from None
    try:
        return _scope(profile)
    except Exception as exc:
        raise EnvKeyProfileError(
            f"could not bind the {profile!r} profile scope: {exc}"
        ) from exc


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
    """Fully mask the secret.

    The previous preview revealed the first and last two characters of any
    value longer than eight. Two known plaintext characters of a credential is
    a real leak (many tokens are ``<prefix><random><suffix>`` with a shared
    family prefix/suffix), and the redaction was the only thing standing
    between a list response and the secret. A masked string of the same length
    still distinguishes keys — it carries the length, which is what a user
    needs to recognise "that's the long one".
    """
    text = str(value or "")
    return "*" * len(text)


def _is_reserved(name: str) -> bool:
    if name in _RESERVED_ENV_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _RESERVED_ENV_PREFIXES)


def _write_error(exc: Exception) -> str:
    """Map the writer's refusal to a message that names the rule, without
    leaking the value that tripped it."""
    message = str(exc)
    if "denylist" in message.lower():
        return "That environment variable is on the writer denylist."
    return f"Could not write the key: {message}"


def handle_env_keys_get(handler, parsed) -> bool:
    """GET /api/env/keys — list the profile's `.env` keys, redacted.

    Lists every key the authenticated profile's ``.env`` holds, each tagged
    ``managed_elsewhere`` when a richer settings page owns it (Providers,
    Channels, Hermes' own config) — the same label the dashboard's Custom Keys
    section uses. The tag is advisory only: nothing here is hidden, because a
    hidden entry cannot be reconciled against the page that owns it. The
    response never contains a value, only a same-length mask.
    """
    try:
        profile = _authorized_profile()
    except EnvKeyProfileError as exc:
        return bad(handler, f"Profile scope unavailable: {exc}", status=503)

    env_on_disk, error = _active_profile_env(profile)
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
        profile = _authorized_profile()
    except EnvKeyProfileError as exc:
        return bad(handler, f"Profile scope unavailable: {exc}", status=503)

    try:
        with _profile_scope(profile):
            # The installed writer signals a managed-.env refusal by returning
            # WITHOUT raising, and returns ``None`` on success too — so its
            # return value cannot distinguish "wrote it" from "declined", and
            # discarding it let a refused write answer ok:true (the reviewer's
            # fourth finding). Prove the mutation instead: read the AUTHORIZED
            # profile's ``.env`` back and require the key to be there. A
            # success response now means the write is observable on disk, not
            # merely that no exception escaped.
            save_env_value(name, value)
            if _active_profile_env(profile)[0] is None or name not in (
                _active_profile_env(profile)[0] or {}
            ):
                return bad(
                    handler,
                    f"the writer did not store {name} in the {profile!r} profile .env",
                    status=409,
                )
    except EnvKeyProfileError as exc:
        return bad(handler, f"Profile scope unavailable: {exc}", status=503)
    except Exception as exc:
        return bad(handler, _write_error(exc), status=400)

    return j(
        handler,
        {
            "ok": True,
            "profile": profile,
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
        profile = _authorized_profile()
    except EnvKeyProfileError as exc:
        return bad(handler, f"Profile scope unavailable: {exc}", status=503)

    try:
        with _profile_scope(profile):
            remove_env_value(name)
        # Prove the key is gone from the AUTHORIZED profile's .env rather than
        # trusting the writer's return (a managed refusal returns False, and
        # the old code answered ok:true anyway).
        after, _err = _active_profile_env(profile)
        if after is not None and name in after:
            return bad(
                handler,
                f"{name} is still present in the {profile!r} profile .env",
                status=409,
            )
    except EnvKeyProfileError as exc:
        return bad(handler, f"Profile scope unavailable: {exc}", status=503)
    except Exception as exc:
        return bad(handler, _write_error(exc), status=400)

    return j(handler, {"ok": True, "profile": profile, "deleted": name})
