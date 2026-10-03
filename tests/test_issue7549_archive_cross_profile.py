"""Cross-profile archive contract for POST /api/session/archive (#7549).

The all-profiles sidebar shows sessions owned by *other* profiles as
archivable, but the archive handler resolved the session against the active
profile only:

1. ``get_session(sid)`` raised ``KeyError`` (no sidecar in the active
   profile's store),
2. the ``_lookup_cli_session_metadata(sid)`` fallback was scoped to the
   active profile too -- it never passed ``all_profiles=True`` even though
   the helper has supported the kwarg all along,
3. so the handler answered a bare ``404 Session not found`` for a session
   that is real, visible in the sidebar, and owned by a known other
   profile.

The fix mirrors the detail-load endpoint's cross-profile contract (#7710):
retry the CLI metadata lookup with ``all_profiles=True``, then emit a
structured ``409 session_profile_mismatch`` carrying the owning profile's
name for a KNOWN other profile (the frontend's
``_sessionProfileMismatchFromError`` already turns that into a profile
switch), keep the bare 404 for unknown/legacy None-profile rows so the
browser's stale-URL self-heal still fires, and let same-profile sessions
archive exactly as before.

These tests drive the real ``handle_post`` dispatcher (not source-text
greps) so the contract is pinned by behaviour, per the #7649 lesson that
``assert "<literal>" in SOURCE`` stays green through a broken fix.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROUTES_PY = Path(__file__).resolve().parents[1] / "api" / "routes.py"


class _FakePostHandler:
    """Minimal BaseHTTPRequestHandler stand-in that records the response."""

    def __init__(self, body: dict, *, path: str):
        raw = json.dumps(body).encode("utf-8")
        self.status = None
        self.response_headers = {}
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


def _post_archive(routes_module, body: dict):
    handler = _FakePostHandler(body, path="/api/session/archive")
    parsed = SimpleNamespace(path="/api/session/archive", query="")
    routes_module.handle_post(handler, parsed)
    payload = None
    try:
        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    except (ValueError, AttributeError):
        payload = None
    return handler.status, payload


@pytest.fixture
def routes_module():
    return pytest.importorskip("api.routes")


@pytest.fixture
def archive_env(routes_module, monkeypatch):
    """Isolate the archive handler's external reads.

    ``get_session`` raises KeyError (the bug's entry condition: the session
    has no sidecar in the active profile's store) and the active profile is
    pinned to ``default``; each test patches the CLI metadata lookup itself.
    """
    monkeypatch.setattr(routes_module, "get_session",
                        lambda _sid, *a, **k: (_ for _ in ()).throw(KeyError(_sid)))
    monkeypatch.setattr(routes_module, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes_module, "_is_subagent_child_session_id", lambda _sid: False)
    # Read-only / materialization side effects must not fire for the
    # mismatch-409 tests; tests that walk further override these.
    monkeypatch.setattr(routes_module, "_is_messaging_session_record", lambda _m: False)
    return routes_module


CLI_META_OTHER = {
    "session_id": "20260925_otherprof_abcd",
    "title": "Belongs to profile B",
    "profile": "work",
    "model": "MiniMax-M3",
    "source_tag": "tui",
    "raw_source": "tui",
    "read_only": False,
}


def test_cross_profile_archive_returns_structured_409(
    routes_module, archive_env, monkeypatch
):
    """The exact bug: session exists in another profile → was bare 404.

    Now the handler must emit ``409 session_profile_mismatch`` carrying the
    owning profile's name so the client can offer a profile switch —
    the same envelope the detail-load endpoint already returns (#7710).
    """
    calls = []

    def fake_lookup(sid, *, all_profiles=False):
        calls.append(all_profiles)
        return CLI_META_OTHER if all_profiles else {}

    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", fake_lookup)

    status, payload = _post_archive(routes_module, {
        "session_id": "20260925_otherprof_abcd", "archived": True})

    assert calls[0] is False, "first lookup stays active-profile-scoped"
    assert True in calls, (
        "on a KeyError the handler must retry _lookup_cli_session_metadata "
        "with all_profiles=True before giving up (#7549)")
    assert status == 409, f"expected 409, got {status}: {payload}"
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work"
    assert payload["session_id"] == "20260925_otherprof_abcd"


def test_cross_profile_409_does_not_materialize_a_session(
    routes_module, archive_env, monkeypatch
):
    """The 409 path must not materialize or save anything: archiving a
    profile-B session through profile A would write into A's store."""
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid, *, all_profiles=False: CLI_META_OTHER if all_profiles else {})
    saved = []
    monkeypatch.setattr(routes_module, "Session", type(
        "Session", (), {"__init__": lambda self, *a, **k: saved.append("ctor"),
                        "save": lambda self, *a, **k: saved.append("save")}))
    monkeypatch.setattr(routes_module, "import_cli_session",
                        lambda *a, **k: saved.append("import"))
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))

    status, _ = _post_archive(routes_module, {
        "session_id": "20260925_otherprof_abcd", "archived": True})

    assert status == 409
    assert saved == [], f"409 path must not construct/save a Session: {saved}"
    assert published == [], "409 path must not publish a list-changed event"


def test_same_profile_session_still_archives(
    routes_module, archive_env, monkeypatch
):
    """Regression guard: a session whose profile matches the active one must
    archive exactly as before (the all-profiles retry is transparent)."""
    same_profile_meta = dict(CLI_META_OTHER, profile="default")
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid, *, all_profiles=False: same_profile_meta if all_profiles else {})
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
    monkeypatch.setattr(routes_module, "get_cli_session_messages",
                        lambda _sid: messages)
    fake_session = SimpleNamespace(
        session_id="20260925_otherprof_abcd", archived=False,
        profile="default", messages=[], title="x",
        compact=lambda: {"session_id": "20260925_otherprof_abcd",
                         "archived": True, "profile": "default"},
        save=lambda **kw: saved.append(kw))
    saved = []

    def fake_import(*args, **kwargs):
        fake_session.is_cli_session = True
        return fake_session
    monkeypatch.setattr(routes_module, "import_cli_session", fake_import)
    monkeypatch.setattr(routes_module, "is_cli_session_row", lambda _m: True)
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: None)

    status, payload = _post_archive(routes_module, {
        "session_id": "20260925_otherprof_abcd", "archived": True})

    assert status == 200, f"same-profile archive must still succeed: {payload}"
    assert fake_session.archived is True
    assert saved and saved[-1].get("touch_updated_at") is False


def test_unknown_sid_keeps_404_even_with_all_profiles_retry(
    routes_module, archive_env, monkeypatch
):
    """A genuinely missing sid must still 404 — the retry must not invent a
    session, and the legacy None-profile 404 self-heal must keep firing."""
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})

    status, payload = _post_archive(routes_module, {
        "session_id": "ghost-sid-nowhere", "archived": True})

    assert status == 404, f"unknown sid must stay 404, got {status}: {payload}"


def test_none_profile_session_keeps_bare_404(
    routes_module, archive_env, monkeypatch
):
    """Legacy/unknown None-profile rows keep the bare 404 (#7710 contract):
    a 409 with profile=null would be useless to the frontend's switcher,
    and the browser's stale-URL self-heal must keep firing."""
    legacy_meta = dict(CLI_META_OTHER, profile=None)
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid, *, all_profiles=False: legacy_meta if all_profiles else {})

    status, payload = _post_archive(routes_module, {
        "session_id": "legacy-sid-no-profile", "archived": True})

    assert status == 404, f"None-profile row must stay 404, got {status}: {payload}"


def test_none_profile_messaging_row_404s_without_materializing(
    routes_module, archive_env, monkeypatch
):
    """#7826: a profile=None metadata row must be rejected BEFORE any
    materialization path — even when the row looks like a messaging session.
    The old guard only 409'd for truthy profiles, so a None row fell through
    and could construct/save a writable Session into whichever profile
    happened to be active. Assert the rejection happens up front: no
    Session ctor, no save, no publish, and no import_cli_session."""
    legacy_meta = dict(CLI_META_OTHER, profile=None)
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid, *, all_profiles=False: legacy_meta if all_profiles else {})
    # Make the row look like a messaging-session record — the rejection must
    # happen before this check is even consulted.
    monkeypatch.setattr(routes_module, "_is_messaging_session_record",
                        lambda _m: True)
    side_effects = []

    class _SpySession:
        def __init__(self, *a, **k):
            side_effects.append("Session-ctor")

        def save(self, *a, **k):
            side_effects.append("save")

        def compact(self):
            return {"session_id": "legacy-sid-no-profile", "archived": False}

    monkeypatch.setattr(routes_module, "Session", _SpySession)
    monkeypatch.setattr(routes_module, "import_cli_session",
                        lambda *a, **k: side_effects.append("import"))
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: side_effects.append("publish"))

    status, _ = _post_archive(routes_module, {
        "session_id": "legacy-sid-no-profile", "archived": True})

    assert status == 404, "profile=None messaging row must be rejected with 404"
    assert side_effects == [], (
        f"None-profile rejection must not construct/save/import/publish: "
        f"{side_effects}")


def test_none_profile_non_messaging_nonempty_transcript_404s_without_import(
    routes_module, archive_env, monkeypatch
):
    """#7826: the non-messaging branch with a NONEMPTY CLI transcript must
    also hit the bare 404. The pre-fix test proved only that an empty
    session 404s; a profile-less row with real messages would have been
    imported into the active profile. Rejection must happen before
    get_cli_session_messages/import_cli_session are reached."""
    legacy_meta = dict(CLI_META_OTHER, profile=None)
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid, *, all_profiles=False: legacy_meta if all_profiles else {})
    # Non-messaging row (fixture default) but with a rich transcript — the
    # old bare-404 test's empty-messages 404 must not be what saves us.
    monkeypatch.setattr(routes_module, "_is_messaging_session_record",
                        lambda _m: False)
    calls = []
    monkeypatch.setattr(routes_module, "get_cli_session_messages",
                        lambda _sid: calls.append("get") or [{"role": "user",
                                                              "content": "x"}])
    side_effects = []
    monkeypatch.setattr(routes_module, "import_cli_session",
                        lambda *a, **k: side_effects.append("import"))
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: side_effects.append("publish"))

    status, _ = _post_archive(routes_module, {
        "session_id": "legacy-sid-no-profile", "archived": True})

    assert status == 404, ("None-profile non-messaging row with a nonempty "
                           "transcript must stay 404")
    assert calls == [], (
        f"None-profile rejection must precede get_cli_session_messages: {calls}")
    assert side_effects == [], (
        f"None-profile rejection must not import/publish: {side_effects}")


def test_archive_handler_passes_all_profiles_on_retry():
    """Static pin: the handler's fallback retry uses all_profiles=True."""
    src = ROUTES_PY.read_text(encoding="utf-8")
    i = src.find('if parsed.path == "/api/session/archive":')
    assert i > 0, "archive handler not found"
    block = src[i:i + 8000]
    retry = [ln.strip() for ln in block.splitlines()
             if "_lookup_cli_session_metadata" in ln]
    assert retry, "archive handler lost its CLI metadata fallback"
    assert any("all_profiles=True" in ln for ln in retry), (
        f"the KeyError fallback must retry with all_profiles=True (#7549): {retry}")
    assert 'session_profile_mismatch' in block, (
        "archive handler must emit the structured 409 envelope (#7710 contract)")
