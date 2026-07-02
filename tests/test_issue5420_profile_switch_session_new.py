"""Regression tests for #5420 profile-switch /api/session/new failures."""

from __future__ import annotations

import ast
import inspect
from unittest.mock import MagicMock
from urllib.parse import urlparse

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


def test_handle_post_does_not_shadow_get_active_profile_name():
    """handle_post must not re-import get_active_profile_name locally (#5420)."""
    src = inspect.getsource(routes.handle_post)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "api.profiles":
            for alias in node.names:
                assert alias.name != "get_active_profile_name", (
                    "local import shadows module-level get_active_profile_name"
                )


def test_session_new_succeeds_with_cross_profile_prev_session_id(monkeypatch):
    created = {}

    class _Session:
        def __init__(self):
            self.session_id = "new123"
            self.profile = "work"
            self.messages = []

        def compact(self):
            return {"session_id": self.session_id, "profile": self.profile}

    def _new_session(**_kwargs):
        s = _Session()
        created["session"] = s
        return s

    monkeypatch.setattr(routes, "new_session", _new_session)
    monkeypatch.setattr(
        routes,
        "_session_id_visible_to_request_profile",
        lambda *_a, **_k: False,
    )

    cap = _post_session_new(
        {"profile": "work", "prev_session_id": "old-from-default"},
        monkeypatch,
    )

    assert "bad" not in cap
    assert "ok" in cap
    assert cap["ok"]["session"]["session_id"] == "new123"
    assert created["session"].profile == "work"


def test_session_new_still_commits_same_profile_prev_session_id(monkeypatch):
    class _Session:
        def __init__(self):
            self.session_id = "new456"
            self.profile = "default"
            self.messages = []

        def compact(self):
            return {"session_id": self.session_id}

    monkeypatch.setattr(routes, "new_session", lambda **_k: _Session())
    monkeypatch.setattr(
        routes,
        "_session_id_visible_to_request_profile",
        lambda *_a, **_k: True,
    )

    commit_calls = []

    def _commit(prev_session_id, **kwargs):
        commit_calls.append(prev_session_id)

    import api.session_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "commit_session_memory", _commit)

    cap = _post_session_new({"prev_session_id": "same-profile-old"}, monkeypatch)

    assert "bad" not in cap
    assert commit_calls == ["same-profile-old"]
    assert cap["ok"]["session"]["session_id"] == "new456"
