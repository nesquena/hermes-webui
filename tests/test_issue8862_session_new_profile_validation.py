"""#8862: POST /api/session/new rejects a non-string or ill-formed profile."""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import urlparse

import pytest

import api.routes as routes


def _post_session_new(body: dict, monkeypatch):
    cap = {}

    def _j(_handler, payload, *_, **__):
        cap["ok"] = payload
        return True

    def _bad(_handler, msg, code=400):
        cap["bad"] = (msg, code)
        return True

    handler = MagicMock()
    handler.command = "POST"
    handler.headers = {}

    monkeypatch.setattr(routes, "read_body", lambda _h: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _p: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)

    routes.handle_post(handler, urlparse("/api/session/new"))
    return cap


def _stub_new_session(monkeypatch, created):
    class _Session:
        def __init__(self, profile):
            self.session_id = "new8862"
            self.profile = profile
            self.messages = []

        def compact(self):
            return {"session_id": self.session_id, "profile": self.profile}

    def _new_session(**kwargs):
        s = _Session(kwargs.get("profile"))
        created["session"] = s
        created["kwargs"] = kwargs
        return s

    monkeypatch.setattr(routes, "new_session", _new_session)
    monkeypatch.setattr(
        routes,
        "_resolve_new_session_workspace",
        lambda *_a, **_k: "/tmp/ws",
    )
    monkeypatch.setattr(
        routes,
        "_session_model_state_from_request",
        lambda *_a, **_k: (None, None),
    )
    monkeypatch.setattr(
        routes,
        "_validate_session_toolsets_shape",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        routes,
        "_session_id_visible_to_request_profile",
        lambda *_a, **_k: True,
    )


@pytest.mark.parametrize(
    "profile",
    [123, True, ["default"], {"name": "default"}],
)
def test_session_new_rejects_non_string_profile(monkeypatch, profile):
    created = {}
    _stub_new_session(monkeypatch, created)
    cap = _post_session_new({"profile": profile}, monkeypatch)
    assert cap.get("bad") == ("invalid profile", 400)
    assert "session" not in created


@pytest.mark.parametrize(
    "profile",
    ["../evil", "No Such Profile", "Bad Name", ""],
)
def test_session_new_rejects_ill_formed_or_keeps_empty(monkeypatch, profile):
    created = {}
    _stub_new_session(monkeypatch, created)
    cap = _post_session_new({"profile": profile}, monkeypatch)
    if profile == "":
        # Empty string means active profile; creation proceeds.
        assert "bad" not in cap
        assert "ok" in cap
        assert created["kwargs"]["profile"] is None
    else:
        assert cap.get("bad") == ("invalid profile", 400)
        assert "session" not in created


def test_session_new_accepts_well_formed_profile(monkeypatch):
    created = {}
    _stub_new_session(monkeypatch, created)
    cap = _post_session_new({"profile": "work"}, monkeypatch)
    assert "bad" not in cap
    assert "ok" in cap
    assert created["kwargs"]["profile"] == "work"


def test_session_new_omitted_profile_unchanged(monkeypatch):
    created = {}
    _stub_new_session(monkeypatch, created)
    cap = _post_session_new({}, monkeypatch)
    assert "bad" not in cap
    assert "ok" in cap
    assert created["kwargs"]["profile"] is None
