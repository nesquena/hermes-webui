"""Safe access to arbitrary user-owned entries in the WebUI .env file.

This is a thin wrapper around ``api.providers._write_env_file``, which already
provides the safety properties we want:

* atomic write via tempfile + ``os.replace`` (cross-process leg of #1164)
* ``_ENV_LOCK`` held for the entire load -> modify -> write cycle (#1164)
* ``fsync`` of the temp file before rename
* ``chmod 0o600`` on the temp file before rename
* preservation of comments, blank lines, and original key ordering
* rejection of embedded ``\\n`` / ``\\r`` characters

This module layers two safety rules on top:

1. **Key validation.** Client-supplied names must match
   ``^[A-Z_][A-Z0-9_]*$`` and be 1-128 chars long. Names that belong to
   Hermes itself (``HERMES_HOME``, anything starting with ``HERMES_WEBUI_``
   or any ``HERMES_*`` runtime key) or to the shell / process environment
   (``PATH``, ``HOME``, ``LD_PRELOAD``...) are rejected even if they would
   otherwise pass the syntax check. This prevents a browser request from
   rewriting authentication, executable lookup, interpreter startup, or
   other process-wide controls through the Settings UI.

2. **No value echo.** ``list_env_keys`` returns names only. The POST
   endpoint returns ``restart_required`` plus the validated key name; the
   value is never placed on the wire back to the client. ``quote_value``
   is provided so callers who need to materialise a value as a literal
   ``.env`` line (not currently exposed) can do so safely.

The expected runtime location of the file is ``$HERMES_HOME/.env``
(falling back to ``~/.hermes/.env``). Tests patch ``_get_hermes_home`` to
isolate from any operator state.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from api import providers as _providers


KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")

MAX_KEY_LENGTH = 128
MAX_VALUE_LENGTH = 4096

# Names that affect shell startup, executable lookup, dynamic loading,
# interpreter startup, or terminal behaviour. These are deliberately denied
# even when the regex would accept them, because this endpoint is an
# editor for application-owned settings — not a general-purpose process
# environment editor.
SHELL_RESERVED_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "IFS",
        "PS4",
        "TERM",
        "LANG",
        "LC_ALL",
        "DISPLAY",
        "EDITOR",
        "PWD",
        "SHLVL",
        "BASH_ENV",
        "ENV",
        "HOSTNAME",
        "MAIL",
        "LOGNAME",
        "OLDPWD",
        "_",
    }
)


def _get_hermes_home() -> Path:
    """Resolve the active Hermes home through the provider path helper."""
    return _providers._get_hermes_home()


def _load_env_file(env_path: Path) -> dict[str, str]:
    """Delegate .env parsing to the established provider implementation."""
    return _providers._load_env_file(env_path)


def _write_env_file(env_path: Path, updates: Mapping[str, str | None]) -> None:
    """Delegate atomic, locked .env writes to the provider implementation."""
    _providers._write_env_file(env_path, dict(updates))


def env_path() -> Path:
    """Return the canonical .env path for the active Hermes home."""
    return _get_hermes_home() / ".env"


def validate_key(key: str) -> str:
    """Validate the syntax of a client-supplied environment variable name.

    Returns the canonicalised key on success. Raises ``ValueError`` with a
    caller-safe message (no filesystem paths, no full .env contents) on
    failure. The same rules apply whether the key is being added or
    deleted — denying malformed keys to the deleter keeps error messages
    consistent across endpoints.
    """
    if not isinstance(key, str):
        raise ValueError("Environment key must be a string.")
    if not key:
        raise ValueError("Environment key is required.")
    if len(key) > MAX_KEY_LENGTH:
        raise ValueError(
            f"Environment key must be {MAX_KEY_LENGTH} characters or fewer."
        )
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError(
            "Environment key must match ^[A-Z_][A-Z0-9_]*$ (uppercase letters, "
            "digits, and underscores; must start with a letter or underscore)."
        )
    return key


def is_reserved_key(key: str) -> bool:
    """Return True for names that this editor must never accept."""
    if not isinstance(key, str):
        return True
    if key in SHELL_RESERVED_KEYS:
        return True
    if key == "HERMES_HOME":
        return True
    if key.startswith("HERMES_WEBUI_"):
        return True
    return False


def require_editable_key(key: str) -> str:
    """Validate both the syntax and the reservation policy of ``key``."""
    canonical = validate_key(key)
    if is_reserved_key(canonical):
        raise ValueError(
            "This environment key is reserved for Hermes or the process "
            "runtime and cannot be edited from the Settings UI."
        )
    return canonical


def quote_value(value: str) -> str:
    """Return a single-line ``.env``-safe literal for ``value``.

    Wraps the value in double quotes and escapes the four POSIX-special
    characters (``\\``, ``"``, ``$``, and backtick) plus any literal
    newline / carriage return (which are still rejected elsewhere, but
    escaping them here makes the function total). Use this if you ever
    need to emit a literal value to logs or to a serialization path;
    the writer itself stores raw values, so this is for callers that
    need a printable representation.
    """
    if not isinstance(value, str):
        raise ValueError("Environment value must be a string.")
    if "\n" in value or "\r" in value:
        raise ValueError("Environment value must not contain newline characters.")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def validate_value(value: object) -> str:
    """Validate a client-supplied value and return it as a plain ``str``."""
    if not isinstance(value, str):
        raise ValueError("Environment value must be a string.")
    if not value.strip():
        raise ValueError("Environment value must not be empty.")
    if len(value) > MAX_VALUE_LENGTH:
        raise ValueError(
            f"Environment value must be {MAX_VALUE_LENGTH} characters or fewer."
        )
    if "\n" in value or "\r" in value:
        raise ValueError("Environment value must not contain newline characters.")
    return value


def list_env_keys(path: Path | None = None) -> list[str]:
    """Return names present in the .env file, never their values."""
    return list(_load_env_file(path or env_path()).keys())


def list_env_keys_with_state(path: Path | None = None) -> list:
    """Return ``[{"name": ..., "set": bool}, ...]`` for the GET endpoint.

    Every key in the .env file is reported with ``set: True``. The ``set``
    flag exists so the wire format can grow to include unset-but-known
    keys without a breaking change later.
    """
    target = path or env_path()
    present = set(_load_env_file(target).keys())
    result: list = []
    for name in sorted(present):
        result.append({"name": name, "set": True})
    return result


def upsert_env_entry(
    path: Path | None,
    key: str,
    value: object,
    *,
    extra_updates: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    """Atomically write ``key=value`` into ``path`` and report the outcome.

    The value is validated for type, emptiness, length, and embedded
    newlines before any write happens. ``key`` must pass both
    ``validate_key`` and the reservation policy. Returns a small
    structured response that the POST endpoint can pass through to the
    client.

    ``extra_updates`` is an escape hatch for callers that need to do
    related edits in the same atomic cycle (for example, removing a key
    alongside an upsert). It is not exposed to HTTP callers today.
    """
    canonical_key = require_editable_key(key)
    canonical_value = validate_value(value)
    target = path or env_path()
    updates: dict[str, str | None] = {canonical_key: canonical_value}
    if extra_updates:
        for extra_key, extra_value in extra_updates.items():
            updates[require_editable_key(extra_key)] = (
                validate_value(extra_value) if extra_value is not None else None
            )
    _write_env_file(target, updates)
    # Settings UI edits always require a process restart: this endpoint
    # only writes to disk, it does not mutate the live ``os.environ``
    # for already-loaded modules (and even where _write_env_file does
    # update os.environ, any consumer that imported os.environ at boot
    # time won't re-read it until restart).
    return {
        "status": "saved",
        "key": canonical_key,
        "requires_restart": True,
    }


def delete_env_entry(path: Path | None, key: str) -> dict[str, object]:
    """Atomically remove ``key`` from ``path``."""
    canonical_key = require_editable_key(key)
    target = path or env_path()
    _write_env_file(target, {canonical_key: None})
    return {
        "status": "deleted",
        "key": canonical_key,
        "requires_restart": True,
    }


__all__ = [
    "KEY_PATTERN",
    "MAX_KEY_LENGTH",
    "MAX_VALUE_LENGTH",
    "SHELL_RESERVED_KEYS",
    "delete_env_entry",
    "env_path",
    "is_reserved_key",
    "list_env_keys",
    "list_env_keys_with_state",
    "quote_value",
    "require_editable_key",
    "upsert_env_entry",
    "validate_key",
    "validate_value",
]