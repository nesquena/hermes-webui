"""Regression test for the #4067 cross-profile import_cli boundary.

The all-profiles session enumeration (#4067) must NOT relax the cross-profile
isolation boundary on the import_cli refresh path. Both release-gate advisors
(Codex CORE + Opus) reproduced the same leak on PR head b16586f0: the
already-imported branch of `_handle_session_import_cli` forced
`allow_all_profiles=True` whenever the (globally-stored) session carried a
profile, and had no `_session_visible_to_active_profile` gate — so an
unqualified request from profile A could read/refresh a session imported under
profile B's live `state.db` (Codex returned the foreign transcript + a planted
secret on an unqualified request).

This pins the fix: the existing-session branch is profile-scoped exactly like
the /api/session detail and /api/session/export endpoints.
"""

from __future__ import annotations

import io

import api.routes as routes


class _FakeHandler:
    def __init__(self, cookie_profile: str | None = None):
        self.status = None
        self.headers = {}
        if cookie_profile is not None:
            self.headers["Cookie"] = f"hermes_profile={cookie_profile}"
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _capture(monkeypatch):
    cap = {}
    monkeypatch.setattr(routes, "j", lambda h, o: (cap.__setitem__("ok", o), True)[1])
    monkeypatch.setattr(
        routes,
        "bad",
        lambda h, m, c=400: (cap.__setitem__("bad", (m, c)), True)[1],
    )
    return cap


class _FakeSession:
    """Minimal stand-in for a globally-stored Session imported under a profile."""

    def __init__(self, sid, profile):
        self.session_id = sid
        self.profile = profile
        self.messages = [{"role": "user", "content": "FOREIGN_SECRET"}]
        self.source_tag = "cli"
        self.raw_source = "cli"
        self.session_source = "cli"
        self.source_label = "CLI"
        self.parent_session_id = None

    def compact(self):
        return {"id": self.session_id, "profile": self.profile}

    def save(self, touch_updated_at=False):
        raise AssertionError("must not save/refresh a foreign-profile session")


def test_import_cli_existing_foreign_profile_unqualified_request_404(monkeypatch):
    """Unqualified request (active profile=default) for a session stored under a
    foreign profile must 404 — not read or refresh the foreign session."""
    foreign = _FakeSession("foreign_existing_001", "other")
    monkeypatch.setattr(routes.Session, "load", staticmethod(lambda sid: foreign))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    # If the gate fails open, these would be reached — make them loud.
    monkeypatch.setattr(
        routes, "get_cli_session_messages",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("foreign read attempted")),
    )
    cap = _capture(monkeypatch)

    routes._handle_session_import_cli(
        _FakeHandler(cookie_profile="default"),
        {"session_id": "foreign_existing_001"},
    )

    assert "bad" in cap, f"expected 404, got {cap}"
    assert cap["bad"][1] == 404


def test_import_cli_existing_same_profile_still_refreshes(monkeypatch):
    """A session stored under the active profile is still served/refreshed."""
    own = _FakeSession("own_001", "default")
    own.save = lambda touch_updated_at=False: None  # allow refresh
    monkeypatch.setattr(routes.Session, "load", staticmethod(lambda sid: own))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda *a, **k: {})
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda *a, **k: [])
    cap = _capture(monkeypatch)

    routes._handle_session_import_cli(
        _FakeHandler(cookie_profile="default"),
        {"session_id": "own_001"},
    )

    assert "ok" in cap, f"expected success, got {cap}"
    assert cap["ok"]["session"]["id"] == "own_001"


def test_import_cli_all_profiles_requires_matching_profile(monkeypatch):
    """An explicit all_profiles import for a foreign session must still match the
    requested profile — a mismatched profile 404s rather than reading."""
    foreign = _FakeSession("foreign_002", "other")
    monkeypatch.setattr(routes.Session, "load", staticmethod(lambda sid: foreign))
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        routes, "get_cli_session_messages",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("foreign read attempted")),
    )
    cap = _capture(monkeypatch)

    # all_profiles=1 but requested profile "haku" != stored "other" → 404.
    routes._handle_session_import_cli(
        _FakeHandler(cookie_profile="default"),
        {"session_id": "foreign_002", "all_profiles": 1, "profile": "haku"},
    )

    assert "bad" in cap, f"expected 404, got {cap}"
    assert cap["bad"][1] == 404
