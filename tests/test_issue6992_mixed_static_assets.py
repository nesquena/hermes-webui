"""Regression tests for #6992 — in-place updates must not serve mixed-revision static assets.

Bug shape: ``api.updates._schedule_restart()`` mutates the WebUI checkout in
place (git stash/pull/pop, or checkout/clean/reset --hard for force updates)
before re-exec'ing the server. During that window the old process keeps serving
``/static/*`` from the changing working tree, and ``_serve_static()`` marked
every non-empty ``?v=`` response ``Cache-Control: immutable`` for a year —
without any check that the served bytes actually belong to the revision the
token claims. One page load could therefore receive JS from the new revision
and CSS from the old one under the SAME versioned URL (the reported ``Refine``
button with none of its new CSS), and the mismatched pair was cached
indefinitely because the client/server skew check saw a token match.

Fix (this PR):
  1. While an in-place WebUI update is being applied (flag set by
     ``api.updates`` under ``_apply_lock``), ``_serve_static()`` refuses to
     serve static bytes: 503 + no-store, so nothing mixed is ever served or
     cached. The client's post-update reload lands on the restarted server,
     which serves one coherent revision.
  2. ``immutable`` is only claimed when the token provably matches the revision
     this process was started on (``v == WEBUI_VERSION``); any other token is
     served with a short ``max-age=300`` instead of a year-long immutable cache.
"""

import urllib.parse

import pytest

from api import routes


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler (pattern from
    tests/test_extension_status_endpoint.py)."""

    def __init__(self):
        self.status = None
        self.headers = {}
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None


@pytest.fixture(autouse=True)
def _reset_update_flag():
    """Ensure the update-freeze flag never leaks between tests."""
    import api.updates as upd

    upd.clear_update_in_progress()
    yield
    upd.clear_update_in_progress()


def _serve(path: str):
    """Call routes._serve_static() directly with a fake handler."""
    handler = FakeHandler()
    parsed = urllib.parse.urlsplit(path)
    routes._serve_static(handler, parsed)
    return handler


# ── Freeze during the in-place update window ───────────────────────────────


def test_static_requests_frozen_503_during_update_window():
    """While an update is in progress, /static/* must not be served at all."""
    import api.updates as upd

    upd.set_update_in_progress()
    handler = _serve("/static/style.css?v=v-test-new")

    assert handler.status == 503
    cache_control = handler.header("Cache-Control")
    assert cache_control == "no-store"
    assert "immutable" not in (cache_control or "")
    assert b"error" in handler.body


def test_no_mixed_revision_pair_served_during_update_window():
    """The issue's source-level repro: with the tree mid-mutation, JS and CSS
    requested under the SAME version token must never both be served — the
    window serves nothing, so a mixed (new JS + old CSS) pair cannot occur."""
    import api.updates as upd

    upd.set_update_in_progress()
    js_handler = _serve("/static/messages.js?v=v-test-new")
    css_handler = _serve("/static/style.css?v=v-test-new")

    assert js_handler.status == 503
    assert css_handler.status == 503
    assert js_handler.header("Cache-Control") == "no-store"
    assert css_handler.header("Cache-Control") == "no-store"


def test_static_serving_resumes_after_update_window():
    """Once the update attempt finishes (flag cleared), serving works again."""
    import api.updates as upd

    upd.set_update_in_progress()
    assert _serve("/static/style.css?v=v-test-new").status == 503
    upd.clear_update_in_progress()

    handler = _serve("/static/style.css?v=v-test-new")
    assert handler.status == 200
    assert len(handler.body) > 0


# ── immutable only when the token→bytes binding is provable ────────────────


def test_static_immutable_for_proven_version_token(monkeypatch):
    """v == WEBUI_VERSION (the revision this process started on) is the only
    token the server can bind to the bytes it serves → immutable is fine."""
    import api.updates as upd

    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    handler = _serve("/static/messages.js?v=v-test-1")

    assert handler.status == 200
    assert "immutable" in handler.header("Cache-Control")


def test_static_not_immutable_for_unproven_token(monkeypatch):
    """A non-empty v that does NOT equal the process's revision cannot be
    verified against the served bytes → short cache, never immutable."""
    import api.updates as upd

    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    handler = _serve("/static/messages.js?v=v-test-2")

    assert handler.status == 200
    cache_control = handler.header("Cache-Control")
    assert "immutable" not in cache_control
    assert cache_control == "public, max-age=300"


def test_static_unversioned_request_keeps_short_cache(monkeypatch):
    """No v token at all → unchanged short cache (no immutable regression)."""
    import api.updates as upd

    monkeypatch.setattr(upd, "WEBUI_VERSION", "v-test-1")
    handler = _serve("/static/messages.js")

    assert handler.status == 200
    cache_control = handler.header("Cache-Control")
    assert "immutable" not in cache_control
    assert cache_control == "public, max-age=300"


# ── The freeze flag lifecycle around the apply functions ───────────────────


def test_update_progress_flag_helpers():
    import api.updates as upd

    upd.clear_update_in_progress()
    assert upd.update_in_progress() is False
    upd.set_update_in_progress()
    assert upd.update_in_progress() is True
    upd.clear_update_in_progress()
    assert upd.update_in_progress() is False


def test_apply_update_freezes_static_during_apply(monkeypatch):
    """apply_update('webui') must hold the freeze flag while the checkout is
    being mutated and release it once the attempt finishes."""
    import api.updates as upd

    observed = {}

    def fake_inner(target, channel):
        observed["target"] = target
        observed["flag_during_apply"] = upd.update_in_progress()
        return {"ok": True, "message": "fake ok", "target": target}

    monkeypatch.setattr(upd, "_apply_update_inner", fake_inner)

    resp = upd.apply_update("webui")
    assert resp["ok"] is True
    assert observed["target"] == "webui"
    assert observed["flag_during_apply"] is True
    assert upd.update_in_progress() is False


def test_apply_update_agent_does_not_freeze_webui_static(monkeypatch):
    """Agent-target updates do not mutate the WebUI checkout, so the freeze
    flag must stay clear."""
    import api.updates as upd

    observed = {}

    def fake_inner(target, channel):
        observed["flag_during_apply"] = upd.update_in_progress()
        return {"ok": True, "message": "fake ok", "target": target}

    monkeypatch.setattr(upd, "_apply_update_inner", fake_inner)

    resp = upd.apply_update("agent")
    assert resp["ok"] is True
    assert observed["flag_during_apply"] is False
    assert upd.update_in_progress() is False


def test_apply_force_update_freezes_static_during_apply(monkeypatch):
    """apply_force_update('webui') must hold the freeze flag through the
    checkout/clean/reset mutation and release it afterwards."""
    import api.updates as upd

    observed = {}

    def fake_run_git(args, cwd, timeout=10):
        if args and args[0] == "reset":
            observed["flag_during_reset"] = upd.update_in_progress()
        return ("", True)

    monkeypatch.setattr(upd, "_run_git", fake_run_git)
    monkeypatch.setattr(upd, "_select_apply_compare_ref", lambda path, channel, target: "origin/master")
    monkeypatch.setattr(upd, "_head_contains_ref", lambda path, ref: False)
    monkeypatch.setattr(upd, "_can_fast_forward_to", lambda path, ref: True)
    monkeypatch.setattr(upd, "_schedule_restart", lambda *a, **k: None)

    resp = upd.apply_force_update("webui")

    assert resp["ok"] is True
    assert resp.get("restart_scheduled") is True
    assert observed.get("flag_during_reset") is True
    assert upd.update_in_progress() is False
