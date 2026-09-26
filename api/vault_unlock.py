"""Unlock external password managers (Bitwarden / 1Password) from the WebUI.

The agent's vault tools (``browser_vault_*``) need an unlocked manager session.
The CLI and gateway surfaces register a masked prompt callback for that, but the
WebUI runs the agent in worker threads with no such callback, so an agent turn
that hits a locked manager can only report "locked" and stop.

These endpoints let the signed-in user unlock the manager directly. The master
password is handed to the backend's own ``unlock()`` (which passes it to the
manager CLI through a child-process environment) and is never logged, returned,
or persisted. The resulting session token lives only in this process's memory
(``agent.vault_backends.unlock``), scoped to the active profile's HERMES_HOME and
subject to the agent's normal idle TTL. Because agent turns run in this same
process, they see the unlocked session immediately.
"""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def _profile_home():
    """Scope backend config and session-token lookups to the active profile.

    Fails closed: without the agent's context-local home override we cannot
    guarantee the token is keyed to this profile, so the endpoints refuse.
    """
    from api.profiles import _resolve_hermes_home_override, get_active_hermes_home

    hc = _resolve_hermes_home_override()
    if hc is None:
        raise VaultUnavailable("This hermes-agent version does not support profile-scoped vault unlock")
    tok = hc.set_hermes_home_override(str(get_active_hermes_home()))
    try:
        yield
    finally:
        hc.reset_hermes_home_override(tok)


class VaultUnavailable(RuntimeError):
    pass


def _unlockable_backends():
    try:
        from agent.vault_backends import enabled_backends
    except ImportError:  # older hermes-agent without vault backends
        return []
    return [b for b in enabled_backends() if getattr(b, "needs_unlock", False)]


def _profile_name() -> str:
    from api.profiles import get_active_profile_name
    return get_active_profile_name() or "default"


def _profile_mismatch(expected):
    """Reject actions built from a panel rendered for another profile."""
    if expected is not None and str(expected) != _profile_name():
        return {"success": False, "error": "Active profile changed; reopen the panel"}, 409
    return None


def status() -> dict:
    with _profile_home():
        return {
            "profile": _profile_name(),
            "backends": [
                {
                    "name": b.name,
                    "display_name": b.display_name,
                    "unlocked": bool(b.is_unlocked()),
                }
                for b in _unlockable_backends()
            ]
        }


def unlock(name: str, master: str, profile=None) -> tuple[dict, int]:
    with _profile_home():
        stale = _profile_mismatch(profile)
        if stale:
            return stale
        backend = next((b for b in _unlockable_backends() if b.name == name), None)
        if backend is None:
            return {"success": False, "error": "Unknown or disabled vault backend"}, 404
        if not master:
            return {"success": False, "error": "Master password is required"}, 400
        try:
            backend.unlock(master)
        except Exception:
            # Never relay backend exception text: it could embed (part of)
            # the master password.
            return {"success": False, "error": "Unlock failed"}, 400
        finally:
            master = ""
        return {"success": True, "backend": backend.name,
                "unlocked": bool(backend.is_unlocked())}, 200


def lock(name: str | None, profile=None) -> tuple[dict, int]:
    with _profile_home():
        stale = _profile_mismatch(profile)
        if stale:
            return stale
        try:
            from agent.vault_backends import unlock as _session
        except ImportError as exc:  # older hermes-agent without vault backends
            raise VaultUnavailable("This hermes-agent version has no vault backends") from exc
        _session.lock(name or None)
        return {"success": True}, 200
