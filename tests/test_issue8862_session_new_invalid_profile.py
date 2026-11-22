"""Regression tests for #8862: /api/session/new must refuse a malformed profile.

The route accepted ``body["profile"]`` unvalidated and forwarded it to
``_resolve_new_session_workspace`` and ``new_session``.  A forged request could
therefore create a session whose profile is not a profile name at all
(non-string JSON, path-like strings, names with spaces/case).  These tests pin
the 400 refusal and prove nothing was cached.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

import api.models as models
import api.routes as routes


def _post_session_new(body: dict, monkeypatch):
    """Drive POST /api/session/new with stubbed transport and a new_session spy."""
    cap = {"created": []}

    def _j(_handler, payload, *_, **__):
        cap["ok"] = payload
        return True

    def _bad(_handler, msg, code=400):
        cap["bad"] = (msg, code)
        return True

    class _Session:
        def __init__(self, profile):
            self.session_id = "new8862"
            self.profile = profile
            self.messages = []

        def compact(self):
            return {"session_id": self.session_id, "profile": self.profile}

    def _new_session(**kwargs):
        cap["created"].append(kwargs)
        return _Session(kwargs.get("profile"))

    handler = type("_Handler", (), {})()
    handler.command = "POST"
    handler.headers = {}

    monkeypatch.setattr(routes, "read_body", lambda _h: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _p: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "new_session", _new_session)
    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)

    cached_before = frozenset(models.SESSIONS)
    routes.handle_post(handler, urlparse("/api/session/new"))
    cap["cached_after"] = frozenset(models.SESSIONS)
    cap["cached_before"] = cached_before
    return cap


@pytest.mark.parametrize(
    "profile",
    [
        123,
        True,
        ["default"],
        {"name": "default"},
        "../evil",
        "No Such Profile",
    ],
    ids=[
        "int",
        "bool",
        "list",
        "dict",
        "path-like",
        "spaces-and-case",
    ],
)
def test_session_new_refuses_malformed_profile_without_caching(profile, monkeypatch):
    cap = _post_session_new({"profile": profile}, monkeypatch)

    assert cap.get("bad") == ("invalid profile", 400)
    assert "ok" not in cap
    # Nothing may be created or cached: the refusal happens before both paths
    # that receive ``profile`` and before any worktree/session write.
    assert cap["created"] == []
    assert cap["cached_after"] == cap["cached_before"]


def test_session_new_still_accepts_a_well_formed_profile(monkeypatch):
    cap = _post_session_new({"profile": "default"}, monkeypatch)

    assert "bad" not in cap
    assert cap["ok"]["session"]["profile"] == "default"
    assert [kw.get("profile") for kw in cap["created"]] == ["default"]


def test_session_new_without_profile_keeps_the_active_profile(monkeypatch):
    cap = _post_session_new({}, monkeypatch)

    assert "bad" not in cap
    assert "ok" in cap
    assert [kw.get("profile") for kw in cap["created"]] == [None]


def test_session_new_accepts_an_unknown_but_well_formed_profile_as_before(monkeypatch):
    """``ghost`` is well-formed, so #8862 leaves it alone (product decision).

    Pinned so the 400 above never widens into "unknown profile" rejection
    without an explicit decision: today the request succeeds and the session
    is created with that name.
    """
    cap = _post_session_new({"profile": "ghost"}, monkeypatch)

    assert "bad" not in cap
    assert "ok" in cap
    assert [kw.get("profile") for kw in cap["created"]] == ["ghost"]
