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
        # #7738: the archive handler re-resolves the session under the agent
        # lock (``SESSIONS.get(sid)`` -> ``Session.load(sid)``) before it
        # mutates ``archived``. The real ``import_cli_session`` persists the
        # sidecar, so that re-resolve finds it again; this stand-in has no
        # disk, so it must publish into the LRU the same way. Without this the
        # handler 404s on its own freshly-imported session — a fake-only
        # artifact, not a production regression.
        with routes_module.LOCK:
            routes_module.SESSIONS[fake_session.session_id] = fake_session
            routes_module.SESSIONS.move_to_end(fake_session.session_id)
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


# --- #7826 root fix: request-scoped `profile` field --------------------------


def test_profile_scoped_archive_of_foreign_session_succeeds(
    routes_module, archive_env, monkeypatch
):
    """#7826 root fix: a foreign-profile session archives with a 200 when the
    request carries the owner profile — and the ACTIVE profile is never
    consulted: ``_get_active_profile_name`` must not be called at all, so
    neither the active profile nor its cookie can change."""
    lookup_calls = []
    active_calls = []

    def fake_lookup(sid, *, all_profiles=False):
        lookup_calls.append(all_profiles)
        return CLI_META_OTHER if all_profiles else {}

    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", fake_lookup)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: active_calls.append("get") or "default")
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
    monkeypatch.setattr(routes_module, "get_cli_session_messages",
                        lambda _sid: messages)
    fake_session = SimpleNamespace(
        session_id="20260925_otherprof_abcd", archived=False,
        profile="work", messages=[], title="x",
        compact=lambda: {"session_id": "20260925_otherprof_abcd",
                         "archived": True, "profile": "work"},
        save=lambda **kw: saved.append(kw))
    saved = []

    def fake_import(*args, **kwargs):
        fake_session.is_cli_session = True
        with routes_module.LOCK:
            routes_module.SESSIONS[fake_session.session_id] = fake_session
            routes_module.SESSIONS.move_to_end(fake_session.session_id)
        return fake_session
    monkeypatch.setattr(routes_module, "import_cli_session", fake_import)
    monkeypatch.setattr(routes_module, "is_cli_session_row", lambda _m: True)
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: None)

    status, payload = _post_archive(routes_module, {
        "session_id": "20260925_otherprof_abcd", "archived": True,
        "profile": "work"})

    assert status == 200, f"profile-scoped archive must succeed: {payload}"
    assert fake_session.archived is True
    assert saved and saved[-1].get("touch_updated_at") is False
    assert active_calls == [], (
        "a profile-scoped archive must never consult the active profile: "
        f"{active_calls}")


def test_profile_field_wrong_owner_409s_with_real_owner(
    routes_module, archive_env, monkeypatch
):
    """#7826 security boundary: the `profile` field is a CLAIM, not an
    override. Asking for a profile the session does not belong to must 409
    with the REAL owner (so the client can re-aim) and materialize nothing."""
    active_calls = []

    def fake_lookup(sid, *, all_profiles=False):
        return CLI_META_OTHER if all_profiles else {}

    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", fake_lookup)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: active_calls.append("get") or "default")
    side_effects = []

    class _SpySession:
        def __init__(self, *a, **k):
            side_effects.append("Session-ctor")

        def save(self, *a, **k):
            side_effects.append("save")

    monkeypatch.setattr(routes_module, "Session", _SpySession)
    monkeypatch.setattr(routes_module, "import_cli_session",
                        lambda *a, **k: side_effects.append("import"))
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: side_effects.append("publish"))

    status, payload = _post_archive(routes_module, {
        "session_id": "20260925_otherprof_abcd", "archived": True,
        "profile": "home"})

    assert status == 409, f"wrong owner profile must 409, got {status}: {payload}"
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work", (
        f"the 409 must name the session's REAL owner, not the request: {payload}")
    assert payload["session_id"] == "20260925_otherprof_abcd"
    assert side_effects == [], (
        f"mismatched profile must not construct/save/import/publish: "
        f"{side_effects}")
    assert active_calls == [], (
        f"the wrong-profile check must not consult the active profile: "
        f"{active_calls}")


def test_profile_field_does_not_rescue_profile_less_row(
    routes_module, archive_env, monkeypatch
):
    """#7826: the profile-scoped request must not resurrect the bare-404
    contract — a profile-less metadata row stays 404 even when a profile
    field is supplied, with zero materialization side effects."""
    legacy_meta = dict(CLI_META_OTHER, profile=None)
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_metadata",
        lambda _sid, *, all_profiles=False: legacy_meta if all_profiles else {})
    side_effects = []

    class _SpySession:
        def __init__(self, *a, **k):
            side_effects.append("Session-ctor")

        def save(self, *a, **k):
            side_effects.append("save")

    monkeypatch.setattr(routes_module, "Session", _SpySession)
    monkeypatch.setattr(routes_module, "import_cli_session",
                        lambda *a, **k: side_effects.append("import"))
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: side_effects.append("publish"))

    status, _ = _post_archive(routes_module, {
        "session_id": "legacy-sid-no-profile", "archived": True,
        "profile": "work"})

    assert status == 404, "profile-less row must stay 404 even with a profile field"
    assert side_effects == [], (
        f"profile-less rejection must not construct/save/import/publish: "
        f"{side_effects}")


def _register_sidecar(routes_module, session):
    with routes_module.LOCK:
        routes_module.SESSIONS[session.session_id] = session
        routes_module.SESSIONS.move_to_end(session.session_id)


def test_sidecar_archive_with_matching_requested_profile_succeeds(
    routes_module, archive_env, monkeypatch
):
    """#7826: the sidecar (get_session) path is profile-agnostic, but a
    profile-scoped request must still validate against the sidecar's own
    profile. A matching profile archives fine and the active profile is never
    consulted."""
    active_calls = []
    fake_sidecar = SimpleNamespace(
        session_id="webui-work-sidecar", archived=False, profile="work",
        messages=[], _loaded_metadata_only=False,
        compact=lambda: {"session_id": "webui-work-sidecar", "archived": True},
        save=lambda **kw: saved.append(kw))
    saved = []
    _register_sidecar(routes_module, fake_sidecar)

    def fake_get_session(sid, *a, **k):
        if sid == "webui-work-sidecar":
            return fake_sidecar
        raise KeyError(sid)

    monkeypatch.setattr(routes_module, "get_session", fake_get_session)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: active_calls.append("get") or "default")
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: None)

    status, payload = _post_archive(routes_module, {
        "session_id": "webui-work-sidecar", "archived": True,
        "profile": "work"})

    assert status == 200, f"matching sidecar profile must archive: {payload}"
    assert fake_sidecar.archived is True
    assert saved and saved[-1].get("touch_updated_at") is False
    assert active_calls == [], (
        f"sidecar archive must not consult the active profile: {active_calls}")


def test_sidecar_archive_with_wrong_requested_profile_409s(
    routes_module, archive_env, monkeypatch
):
    """#7826: a profile-scoped request naming the wrong owner must 409 on the
    sidecar path too (the field cannot bypass the ownership check by hitting
    get_session), naming the sidecar's real profile, and mutate nothing."""
    active_calls = []
    fake_sidecar = SimpleNamespace(
        session_id="webui-work-sidecar", archived=False, profile="work",
        messages=[], _loaded_metadata_only=False,
        compact=lambda: {"session_id": "webui-work-sidecar", "archived": False})
    saved = []
    _register_sidecar(routes_module, fake_sidecar)

    def fake_get_session(sid, *a, **k):
        if sid == "webui-work-sidecar":
            return fake_sidecar
        raise KeyError(sid)

    monkeypatch.setattr(routes_module, "get_session", fake_get_session)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: active_calls.append("get") or "default")
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))

    status, payload = _post_archive(routes_module, {
        "session_id": "webui-work-sidecar", "archived": True,
        "profile": "default"})

    assert status == 409, f"wrong sidecar profile must 409: {payload}"
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work", (
        f"the 409 must name the sidecar's real owner: {payload}")
    assert fake_sidecar.archived is False, "the sidecar must not be mutated"
    assert saved == [], f"no save may fire on the 409: {saved}"
    assert published == [], f"no publish may fire on the 409: {published}"
    assert active_calls == [], (
        f"sidecar mismatch must not consult the active profile: {active_calls}")


def test_sidecar_wrong_profile_409s_even_without_predispatch_guard(
    routes_module, archive_env, monkeypatch
):
    """#7826 revert-pin: the archive route's OWN sidecar ownership check must
    fire even when the pre-dispatch visibility guard is bypassed (defense in
    depth — the `profile` field is a claim, never an override). Removing the
    route-side check lets this archive through, so this test goes RED."""
    monkeypatch.setattr(routes_module, "_guard_request_session_visibility",
                        lambda *a, **k: True)
    fake_sidecar = SimpleNamespace(
        session_id="webui-work-sidecar", archived=False, profile="work",
        messages=[], _loaded_metadata_only=False,
        compact=lambda: {"session_id": "webui-work-sidecar", "archived": False})
    saved = []
    _register_sidecar(routes_module, fake_sidecar)

    def fake_get_session(sid, *a, **k):
        if sid == "webui-work-sidecar":
            return fake_sidecar
        raise KeyError(sid)

    monkeypatch.setattr(routes_module, "get_session", fake_get_session)
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))

    status, payload = _post_archive(routes_module, {
        "session_id": "webui-work-sidecar", "archived": True,
        "profile": "default"})

    assert status == 409, (
        f"route-side sidecar check must 409 without the guard: {payload}")
    assert payload["profile"] == "work", payload
    assert saved == [], f"no save may fire on the 409: {saved}"
    assert published == [], f"no publish may fire on the 409: {published}"
