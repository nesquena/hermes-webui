from types import SimpleNamespace

import pytest

from api import auth
from api import profile_policy


def _handler(cookie_header: str = ""):
    return SimpleNamespace(headers=SimpleNamespace(get=lambda key, default="": cookie_header if key == "Cookie" else default))


def setup_function():
    auth._sessions.clear()


def test_request_profile_prefers_session_bound_profile_over_cookie():
    cookie = auth.create_session(username="alice", profile="alice")
    handler = _handler(f"hermes_profile=bob; hermes_session={cookie}")

    assert profile_policy.request_profile(handler) == "alice"


def test_request_profile_falls_back_to_profile_cookie_for_unbound_sessions():
    cookie = auth.create_session()
    handler = _handler(f"hermes_profile=bob; hermes_session={cookie}")

    assert profile_policy.request_profile(handler) == "bob"


def test_bound_session_cannot_switch_to_another_profile():
    cookie = auth.create_session(username="alice", profile="alice")
    handler = _handler(f"hermes_session={cookie}")

    with pytest.raises(profile_policy.ProfileBoundError):
        profile_policy.require_unbound_or_profile(handler, "bob", action="switch")


def test_bound_session_can_switch_to_its_own_profile():
    cookie = auth.create_session(username="alice", profile="alice")
    handler = _handler(f"hermes_session={cookie}")

    profile_policy.require_unbound_or_profile(handler, "alice", action="switch")


def test_bound_session_cannot_delete_its_login_profile():
    cookie = auth.create_session(username="alice", profile="alice")
    handler = _handler(f"hermes_session={cookie}")

    with pytest.raises(profile_policy.ProfileBoundError) as exc:
        profile_policy.require_unbound_or_profile(handler, "alice", action="delete")
    assert "Cannot delete" in str(exc.value)


def test_ensure_profile_exists_creates_clean_profile_without_cloning(monkeypatch):
    calls = []

    monkeypatch.setattr("api.profiles.list_profiles_api", lambda: [])

    def fake_create(name, **kwargs):
        calls.append((name, kwargs))
        return {"name": name}

    monkeypatch.setattr("api.profiles.create_profile_api", fake_create)

    assert profile_policy.ensure_profile_exists("alice") == {"name": "alice"}
    assert calls == [("alice", {})]
