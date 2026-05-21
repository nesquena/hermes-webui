"""Profile access policy helpers for authenticated WebUI sessions."""
from __future__ import annotations

from api.helpers import get_profile_cookie


class ProfileBoundError(PermissionError):
    """Raised when a login-bound session tries to access another profile."""

    def __init__(self, message: str = "Profile is managed by your login session"):
        super().__init__(message)
        self.status = 403


def session_bound_profile(handler) -> str | None:
    """Return the server-bound profile for the request's auth session, if any."""
    if not getattr(handler, 'headers', None):
        return None
    from api.auth import parse_cookie, session_profile

    cookie_val = parse_cookie(handler)
    return session_profile(cookie_val) if cookie_val else None


def request_profile(handler) -> str | None:
    """Return the effective request profile.

    A profile bound into the signed auth session takes priority over the
    client-controlled profile cookie. This preserves existing per-client
    profile switching while preventing username-bound sessions from hopping
    profiles by editing a cookie.
    """
    if not getattr(handler, 'headers', None):
        return None
    return session_bound_profile(handler) or get_profile_cookie(handler)


def require_unbound_or_profile(handler, requested_name: str, *, action: str) -> None:
    """Reject profile mutations that escape a login-bound profile."""
    bound = session_bound_profile(handler)
    if not bound:
        return
    if action in {'switch', 'create'} and requested_name == bound:
        return
    if action == 'delete' and requested_name == bound:
        raise ProfileBoundError("Cannot delete your login profile while signed in")
    raise ProfileBoundError()


def ensure_profile_exists(name: str) -> dict | None:
    """Create a clean named profile if it does not already exist."""
    from api.profiles import create_profile_api, list_profiles_api

    for profile in list_profiles_api():
        if profile.get('name') == name:
            return profile
    try:
        return create_profile_api(name)
    except FileExistsError:
        for profile in list_profiles_api():
            if profile.get('name') == name:
                return profile
        return None
