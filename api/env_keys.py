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
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import unquote

from api.helpers import bad, j

# Mirrors the POSIX-ish shape the writer enforces; kept here only to reject
# obviously malformed names before touching the agent module. The writer's own
# ``validate_env_var_name_for_write`` is the authority.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A value carrying NUL or a control character (CR/LF included) survives the
# name checks, gets written verbatim into the profile's ``.env``, and then
# breaks the reload: a CR can truncate the line the agent later parses and a
# NUL truncates the value at the reader. Reject them here, before the writer
# sees them (#7870 review).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


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
    """Enter ``profile``'s HOME scope for the agent's env reader/writer.

    ``profile`` is always the AUTHORIZED profile for the request (see
    :func:`_authorized_profile`) — never a query parameter.

    The scope is the task-local ``HERMES_HOME`` override in
    ``hermes_constants``: the reader/writer both resolve their target through
    ``get_env_path() -> get_hermes_home()`` at call time, so the override
    reaches them without touching any process-level state.

    The obvious alternative — ``hermes_cli.web_server_profiles._profile_scope``
    — is unusable here. Its ``_config_profile_scope`` calls
    ``activate_multi_profile_hosting()`` for any non-launch home, which is a
    **process-global, one-way** switch: once a single WebUI request for another
    profile's home flips it, ``agent.secret_scope.get_secret`` fails closed and
    every concurrent unscoped read raises ``UnscopedSecretError`` — including
    the chat turns this very server is serving. It also swaps the skills
    modules' module-level ``HERMES_HOME``/``SKILLS_DIR`` under a lock. The
    WebUI never enables multiplexing on master, so a custom-key admin request
    must not be able to flip it for the whole process.

    The requested profile is NEVER the launch profile by construction
    (:func:`_authorized_profile` renames root to ``default`` and refuses an
    unresolvable binding), so the override only ever redirects a ``.env`` read
    or write into the caller's own home.

    A scope that cannot be constructed is an error the caller must see: the
    previous behaviour returned a no-op context, which let a request answer
    against the process's launch/home ``.env`` (i.e. the DEFAULT profile)
    instead of the caller's — a silent cross-profile mutation reported as
    success (#7870 review, "wrong-home default").

    Raises ``EnvKeyProfileError`` on any binding failure.
    """
    if not profile:
        raise EnvKeyProfileError("no profile to scope the .env access to")

    try:
        from api.profiles import get_hermes_home_for_profile
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
    except Exception as exc:
        raise EnvKeyProfileError(
            f"the agent profile binding is unavailable; refusing to touch a .env: {exc}"
        ) from exc

    try:
        home = Path(get_hermes_home_for_profile(profile)).expanduser().resolve()
    except Exception as exc:
        raise EnvKeyProfileError(
            f"could not resolve the {profile!r} profile home: {exc}"
        ) from exc

    # Fail closed on a home that is not provably THIS profile's own.
    #
    # ``get_hermes_home_for_profile`` falls back to the base (default) home for
    # a name it cannot resolve — correct for config *reads* that want "best
    # effort", fatal here: the write would land in the DEFAULT profile's .env
    # and be reported as the caller's. That is the exact "wrong-home default"
    # failure this PR already fixed once, now reachable again through the new
    # resolver, so it has to be rejected rather than trusted.
    #
    # The check is structural, not a comparison against the process home: a
    # named profile's home lives under ``profiles/<name>`` by construction, so
    # the name must be the last path component of the resolved home. A fallback
    # to the base home therefore fails here, and so does a resolver that maps
    # two names onto one directory.
    if _canonical_profile_name(profile) != "default":
        if home.name != _canonical_profile_name(profile):
            raise EnvKeyProfileError(
                f"the {profile!r} profile resolved to {home}, which is not its "
                f"own home; refusing to touch a .env"
            )
    if not str(home) or home == Path(home.anchor):
        raise EnvKeyProfileError(
            f"the {profile!r} profile resolved to an unusable home {home!r}"
        )

    @contextmanager
    def _scoped_home():
        token = set_hermes_home_override(str(home))
        try:
            yield home
        finally:
            reset_hermes_home_override(token)

    return _scoped_home()


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


def _env_lock():
    """Serialise the read-modify-write, matching the provider-key writer.

    Two concurrent custom-key PUTs (or a PUT racing the provider-key writer)
    would otherwise read the same ``.env``, apply their own mutation and
    write back, and the second write would silently drop the first key.
    ``api.streaming._ENV_LOCK`` is the lock the provider-key routes already
    hold for exactly this reason, so the two writers cannot interleave.

    The lock is a plain ``threading.Lock`` and is NOT reentrant: it must be
    taken once per request, around the whole mutation **and** its read-back,
    never nested.

    Falls back to a no-op context when the lock cannot be imported (the
    agent module is absent, as in parts of CI): a request that still
    verifies its own write beats a request that fails on a missing import.
    """
    try:
        from api.streaming import _ENV_LOCK
    except Exception:  # pragma: no cover - agent module unavailable
        return nullcontext()
    return _ENV_LOCK


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
    if _CONTROL_CHAR_RE.search(value):
        # NUL and control characters (CR/LF included) are written verbatim
        # into the .env and break the reload that follows, so they are
        # rejected before the writer ever sees them.
        return bad(
            handler,
            "value must not contain control characters (including newlines "
            "or carriage returns)",
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
        # The whole mutation AND its read-back run under the same lock the
        # provider-key writer uses, so two concurrent custom-key PUTs cannot
        # interleave their read-modify-write and drop each other's key. The
        # lock is not reentrant — it is taken exactly once, here.
        with _env_lock(), _profile_scope(profile):
            # The installed writer signals a managed-.env refusal by returning
            # WITHOUT raising, and returns ``None`` on success too — so its
            # return value cannot distinguish "wrote it" from "declined", and
            # discarding it let a refused write answer ok:true (the reviewer's
            # fourth finding). Prove the mutation instead: read the AUTHORIZED
            # profile's ``.env`` back and require the key to be there. A
            # success response now means the write is observable on disk, not
            # merely that no exception escaped.
            save_env_value(name, value)
            # The read-back MUST stay inside the scope. ``_active_profile_env``
            # re-enters the profile's home itself, but doing it here means the
            # verification observes the SAME ``.env`` the writer just touched
            # even if the scope's home override is ever narrowed — and it keeps
            # the proof adjacent to the write it has to vouch for.
            stored, _read_err = _active_profile_env(profile)
            if stored is None or name not in stored:
                return bad(
                    handler,
                    f"the writer did not store {name} in the {profile!r} profile .env",
                    status=409,
                )
            if stored.get(name) != value:
                # The key exists but with a DIFFERENT value: a managed writer
                # refused the replacement and left the previous one in place.
                # Answering ok:true here would report the new secret as stored
                # while the old one is still live.
                return bad(
                    handler,
                    f"{name} was not updated in the {profile!r} profile .env",
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
        # Same lock, same rule as PUT: the removal and its read-back are one
        # read-modify-write, and the provider-key writer holds this lock too.
        with _env_lock(), _profile_scope(profile):
            remove_env_value(name)
            # Prove the key is gone from the AUTHORIZED profile's .env rather
            # than trusting the writer's return (a managed refusal returns
            # False, and the old code answered ok:true anyway). The read-back
            # stays inside the scope so it observes the same file the writer
            # just touched.
            after, _read_err = _active_profile_env(profile)
            if after is None:
                # The read-back itself failed: the delete is UNVERIFIED. A
                # read failure used to fall through to ok:true, reporting a
                # removal nobody confirmed.
                return bad(
                    handler,
                    f"could not verify {name} was removed from the "
                    f"{profile!r} profile .env",
                    status=409,
                )
            if name in after:
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
