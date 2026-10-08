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
    owner_calls = []

    def fake_lookup(sid, *, all_profiles=False):
        calls.append(all_profiles)
        return CLI_META_OTHER if all_profiles else {}

    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", fake_lookup)

    # #7826 security: the cross-profile hop is now the READ-ONLY exact-owner
    # query. ``get_cli_sessions(all_profiles=True)`` was the defect — its
    # projection falls back to ``Session.load`` on a legacy sidecar layout and
    # SAVES, so a denied archive rewrote a foreign sidecar. Assert the new hop
    # runs (and that the writing one is what is no longer called for it).
    def fake_owner_lookup(sid, *, allowed_profiles=None):
        owner_calls.append(allowed_profiles)
        return CLI_META_OTHER

    monkeypatch.setattr(routes_module, "_lookup_cli_session_owner_readonly",
                        fake_owner_lookup)

    status, payload = _post_archive(routes_module, {
        "session_id": "20260925_otherprof_abcd", "archived": True})

    assert calls[0] is False, "first lookup stays active-profile-scoped"
    assert owner_calls, (
        "on a KeyError the handler must retry through the read-only "
        "exact-owner lookup before giving up (#7826 security)")
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
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: CLI_META_OTHER)
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
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: same_profile_meta)
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
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: legacy_meta)

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
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: legacy_meta)
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
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: legacy_meta)
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
    """Static pin: the handler's fallback retry uses the READ-ONLY
    cross-profile lookup (#7826 security).

    The #7549 version of this pin asserted ``all_profiles=True`` on
    ``_lookup_cli_session_metadata``. That call is exactly the defect: it
    routes through ``get_cli_sessions()``, whose projection falls back to
    ``Session.load`` on a legacy sidecar layout and SAVES (with a ``.bak``).
    A denied archive therefore rewrote a foreign sidecar. The pin now
    requires the sidecar-free, read-only, exact-owner query instead — and
    forbids the writing one on this path.
    """
    src = ROUTES_PY.read_text(encoding="utf-8")
    i = src.find('if parsed.path == "/api/session/archive":')
    assert i > 0, "archive handler not found"
    block = src[i:i + 8000]
    retry = [ln.strip() for ln in block.splitlines()
             if "_lookup_cli_session" in ln]
    assert retry, "archive handler lost its CLI metadata fallback"
    assert any("_lookup_cli_session_owner_readonly" in ln for ln in retry), (
        f"the KeyError fallback must use the read-only exact-owner lookup "
        f"(#7826 security — never get_cli_sessions() here): {retry}")
    assert not any("all_profiles=True" in ln for ln in retry), (
        f"the archive fallback must NOT call the writing all-profiles "
        f"projection (#7826 security): {retry}")
    assert 'session_profile_mismatch' in block, (
        "archive handler must emit the structured 409 envelope (#7710 contract)")


# --- #7826 root fix: request-scoped `profile` field --------------------------


def test_profile_scoped_archive_of_foreign_session_succeeds(
    routes_module, archive_env, monkeypatch
):
    """#7826 round 5: the profile field is NOT an authorization. A foreign
    ``work``-owned session must 409 for a default-bound request even when the
    request claims ``profile:"work"``, and NOTHING may be materialized — the
    pre-fix behaviour loaded the sidecar (running its persisted repairs, which
    rewrite disk) on the way to that 409."""
    lookup_calls = []
    active_calls = []

    def fake_lookup(sid, *, all_profiles=False):
        lookup_calls.append(all_profiles)
        return CLI_META_OTHER if all_profiles else {}

    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", fake_lookup)
    # #7826 security: the cross-profile hop uses the read-only exact-owner
    # query (see test_cross_profile_archive_returns_structured_409).
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: CLI_META_OTHER)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: active_calls.append("get") or "default")
    side_effects = []

    class _SpySession:
        def __init__(self, *a, **k):
            side_effects.append("Session-ctor")

        def save(self, *a, **k):
            side_effects.append("save")

    monkeypatch.setattr(routes_module, "get_cli_session_messages",
                        lambda _sid: side_effects.append("messages") or [])
    monkeypatch.setattr(routes_module, "Session", _SpySession)
    monkeypatch.setattr(routes_module, "import_cli_session",
                        lambda *a, **k: side_effects.append("import"))
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: side_effects.append("publish"))

    status, payload = _post_archive(routes_module, {
        "session_id": "20260925_otherprof_abcd", "archived": True,
        "profile": "work"})

    assert status == 409, (
        f"a foreign row must 409 even when the request claims its profile: "
        f"{payload}")
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work"
    assert side_effects == [], (
        f"a denied archive must not load/materialize anything: {side_effects}")


def test_archive_request_profile_claim_does_not_relabel_visibility(
    routes_module, archive_env, monkeypatch
):
    """#7826 round 5 revert-pin (defense in depth): the pre-dispatch
    visibility guard must ignore the body's ``profile`` claim. A default-bound
    request carrying ``profile:"work"`` must be denied by the GUARD itself
    before the archive route is ever reached."""
    denied = []
    real_guard = routes_module._guard_request_session_visibility

    class _SpyGuard:
        pass

    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: "default")

    def spy_guard(handler, parsed, body=None, method="GET"):
        # Pin the guard's real behaviour while recording the denial.
        return real_guard(handler, parsed, body=body, method=method)

    monkeypatch.setattr(routes_module, "_guard_request_session_visibility",
                        spy_guard)

    # The sidecar belongs to 'work'. get_session returns it for the guard's
    # metadata-only visibility probe.
    fake_sidecar = SimpleNamespace(
        session_id="webui-work-sidecar", archived=False, profile="work",
        messages=[], _loaded_metadata_only=False,
        compact=lambda: {"session_id": "webui-work-sidecar", "archived": True},
        save=lambda **kw: denied.append("save"))
    _register_sidecar(routes_module, fake_sidecar)

    def fake_get_session(sid, *a, **k):
        if sid == "webui-work-sidecar":
            return fake_sidecar
        raise KeyError(sid)

    monkeypatch.setattr(routes_module, "get_session", fake_get_session)
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only",
                        lambda _sid: False)
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: denied.append("publish"))

    handler = _FakePostHandler(
        {"session_id": "webui-work-sidecar", "archived": True,
         "profile": "work"},
        path="/api/session/archive")
    parsed = SimpleNamespace(path="/api/session/archive", query="")
    routes_module.handle_post(handler, parsed)
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))

    assert payload["code"] == "session_profile_mismatch", (
        f"the guard must deny a foreign row regardless of the claimed "
        f"profile: {payload}")
    assert payload["profile"] == "work", payload
    assert denied == [], (
        f"a guard-denied archive must never mutate or publish: {denied}")


def test_profile_field_wrong_owner_409s_with_real_owner(
    routes_module, archive_env, monkeypatch
):
    """#7826 security boundary: the `profile` field is a CLAIM, not an
    override. A foreign row stays denied no matter which profile is claimed,
    and nothing is materialized on the way to the 409 — the denial happens
    before the load whose repairs would rewrite the sidecar (round 5)."""
    def fake_lookup(sid, *, all_profiles=False):
        return CLI_META_OTHER if all_profiles else {}

    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata", fake_lookup)
    # #7826 security: the second (all-profiles) hop no longer goes through the
    # writing projection — it uses the read-only exact-owner query.
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: CLI_META_OTHER)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: "default")
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

    assert status == 409, f"a foreign row must 409, got {status}: {payload}"
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work", (
        f"the 409 must name the session's REAL owner, not the request: {payload}")
    assert payload["session_id"] == "20260925_otherprof_abcd"
    assert side_effects == [], (
        f"a denied archive must not construct/save/import/publish: "
        f"{side_effects}")


def test_profile_field_does_not_rescue_profile_less_row(
    routes_module, archive_env, monkeypatch
):
    """#7826: the profile-scoped request must not resurrect the bare-404
    contract — a profile-less metadata row stays 404 even when a profile
    field is supplied, with zero materialization side effects."""
    legacy_meta = dict(CLI_META_OTHER, profile=None)
    monkeypatch.setattr(
        routes_module, "_lookup_cli_session_owner_readonly",
        lambda _sid, *, allowed_profiles=None: legacy_meta)
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
    """#7826: a WebUI sidecar belonging to the ACTIVE profile archives through
    the sidecar (get_session) path normally — including a request whose
    ``profile`` field agrees with the owner. A matching claim is not needed
    for authorization, and its presence must not change the outcome."""
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
                        lambda: active_calls.append("get") or "work")
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    monkeypatch.setattr(routes_module, "publish_session_list_changed", lambda *a, **k: None)

    status, payload = _post_archive(routes_module, {
        "session_id": "webui-work-sidecar", "archived": True, "profile": "work"})

    assert status == 200, (
        f"an owner-visible row must archive even when the request also claims "
        f"its profile: {payload}")
    assert fake_sidecar.archived is True
    assert saved and saved[-1].get("touch_updated_at") is False


def test_sidecar_archive_with_wrong_requested_profile_409s(
    routes_module, archive_env, monkeypatch
):
    """#7826 round 5: the sidecar path ignores the request's ``profile``
    claim entirely. The ACTIVE profile decides, so a ``work`` row stays 409
    for a default-bound request, the envelope names the sidecar's real owner,
    and nothing mutates — critically, the denial now happens BEFORE the load
    whose persisted repairs would rewrite the sidecar."""
    fake_sidecar = SimpleNamespace(
        session_id="webui-work-sidecar", archived=False, profile="work",
        messages=[], _loaded_metadata_only=False,
        compact=lambda: {"session_id": "webui-work-sidecar", "archived": False})
    saved = []
    _register_sidecar(routes_module, fake_sidecar)
    full_loads = []

    def fake_get_session(sid, *a, **k):
        if sid == "webui-work-sidecar":
            if k.get("metadata_only"):
                return fake_sidecar
            full_loads.append("full")
            return fake_sidecar
        raise KeyError(sid)

    monkeypatch.setattr(routes_module, "get_session", fake_get_session)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: "default")
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))
    save_triggered = []
    fake_sidecar.save = lambda **kw: save_triggered.append(kw)

    status, payload = _post_archive(routes_module, {
        "session_id": "webui-work-sidecar", "archived": True,
        "profile": "work"})

    assert status == 409, f"a foreign row must 409: {payload}"
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work", (
        f"the 409 must name the sidecar's real owner: {payload}")
    assert fake_sidecar.archived is False, "the sidecar must not be mutated"
    assert saved == [], f"no save may fire on the 409: {saved}"
    assert save_triggered == [], f"no save may fire on the 409: {save_triggered}"
    assert published == [], f"no publish may fire on the 409: {published}"
    assert full_loads == [], (
        f"the denied archive must not reach the full-disk load whose repairs "
        f"rewrite the sidecar: {full_loads}")


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
        "profile": "work"})

    assert status == 409, (
        f"route-side sidecar check must 409 without the guard: {payload}")
    assert payload["profile"] == "work", payload
    assert saved == [], f"no save may fire on the 409: {saved}"
    assert published == [], f"no publish may fire on the 409: {published}"


# ---------------------------------------------------------------------------
# #7826 round 5 regression: a DENIED archive must not rewrite another
# profile's transcript on disk.
#
# The signature hazard was ordering, not the response: the route validated
# ownership AFTER ``get_session(sid)`` / ``Session.load(sid)``, and those loads
# run the persisted session repairs, which ``save()`` the sidecar and drop a
# ``.bak`` when a repair shrinks the transcript. Codex reproduced it through
# real auth + dispatch: the request answered 409, yet the foreign sidecar went
# from three messages to two and a ``.bak`` appeared.
#
# This test writes a real ``work``-owned sidecar carrying THREE messages, of
# which two are a repeated identical partial -- the exact shape the load-time
# ``_collapse_adjacent_duplicate_partials`` repair rewrites into two. A
# default-bound archive claiming ``profile:"work"`` must leave that file
# BYTE-IDENTICAL (no trim, no ``.bak``).
# ---------------------------------------------------------------------------

def _repair_needing_sidecar_bytes() -> bytes:
    """A sidecar that loses one message (and gains a .bak) if it is ever
    loaded through the persisted session repairs."""
    partial = {
        "role": "assistant",
        "content": "streaming tail",
        "_partial": True,
        "_partial_tool_calls": [],
    }
    payload = {
        "session_id": "repairsid-work-own",
        "title": "Foreign work-ish session",
        "profile": "work",
        "messages": [partial, partial],
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def _write_real_sidecar(session_dir, sid, before):
    """Persist the repair-needing body through the REAL writer.

    Written by hand, a minimal two-message JSON makes
    ``Session.load_metadata_only`` fall back to a full load (it needs the
    metadata prefix keys), which would trim the transcript even on the
    allowed path and make this fixture test the fallback instead of the
    repair. Going through ``Session.save()`` produces a real sidecar whose
    metadata prefix is complete, so the metadata-only probe stays cheap and
    write-free — the production shape for any sidecar the sidebar has seen.
    """
    import api.models as models

    payload = json.loads(before.decode("utf-8"))
    payload["session_id"] = sid
    session = models.Session(**payload)
    session.save(touch_updated_at=False, skip_index=True)
    return (session_dir / f"{sid}.json").read_bytes()


def test_denied_archive_leaves_foreign_sidecar_byte_identical(
    routes_module, archive_env, monkeypatch, tmp_path
):
    """Maintainer's round-5 regression: the denied archive must not rewrite the
    foreign sidecar. Pre-fix (validate-after-load) the 409 was correct and the
    transcript was still trimmed, so this pins BOTH the response and the disk
    state."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE",
                        session_dir / "_index.json")
    sidecar = session_dir / "repairsid-work-own.json"
    before = _write_real_sidecar(
        session_dir, "repairsid-work-own", _repair_needing_sidecar_bytes())
    # Sanity: the sidecar really carries the shape a full load would trim,
    # so a regression cannot pass by having chosen an inert fixture.
    assert json.loads(before.decode("utf-8"))["messages"][0]["_partial"] is True

    def real_get_session(sid, *a, **k):
        return models.get_session(sid, *a, **k)

    monkeypatch.setattr(routes_module, "get_session", real_get_session)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: "default")
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only",
                        lambda _sid: False)
    # Not a CLI row: no metadata lookup may rescue it, and the fallback path
    # must not be the thing that saves us.
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))

    status, payload = _post_archive(routes_module, {
        "session_id": "repairsid-work-own", "archived": True,
        "profile": "work"})

    # (a) The response contract is the structured 409.
    assert status == 409, f"expected 409, got {status}: {payload}"
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work"

    # (b) The sidecar is BYTE-IDENTICAL: the load-time repair never ran.
    after = sidecar.read_bytes()
    assert after == before, (
        "a denied archive must not rewrite another profile's sidecar: the "
        f"file changed on disk ({len(before)} -> {len(after)} bytes)")

    # (c) No backup was produced, and nothing was published.
    bak = session_dir / "repairsid-work-own.json.bak"
    assert not bak.exists(), (
        "a denied archive must not leave a .bak behind")
    assert not list(session_dir.glob("*.tmp.*")), (
        "a denied archive must not leave temporary write artifacts")
    assert published == [], f"no publish may fire on the 409: {published}"


def test_allowed_archive_of_own_sidecar_still_runs_load_repairs(
    routes_module, archive_env, monkeypatch, tmp_path
):
    """The round-5 check must not break the legitimate path: a ``work``-bound
    request archiving its OWN ``work`` sidecar still loads it (repairs and
    all), saves the archived flag, and publishes."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE",
                        session_dir / "_index.json")
    sidecar = session_dir / "repairsid-own.json"
    before = _write_real_sidecar(
        session_dir, "repairsid-own", _repair_needing_sidecar_bytes())

    def real_get_session(sid, *a, **k):
        return models.get_session(sid, *a, **k)

    monkeypatch.setattr(routes_module, "get_session", real_get_session)
    monkeypatch.setattr(routes_module, "_get_active_profile_name",
                        lambda: "work")
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only",
                        lambda _sid: False)
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))

    status, payload = _post_archive(routes_module, {
        "session_id": "repairsid-own", "archived": True})

    assert status == 200, f"own-profile archive must succeed: {payload}"
    assert payload.get("ok") is True
    assert payload["session"]["archived"] is True, payload
    assert published, "a successful archive must publish"
    # The allowed path really DID load and rewrite the sidecar (the repair ran
    # on it), so this pins the contrast against the denied case above: only a
    # denied archive leaves the file untouched.
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    assert saved["archived"] is True
    assert len(saved["messages"]) < len(json.loads(before.decode("utf-8"))["messages"]), (
        "the allowed archive must still run the load-time repair")
    assert (session_dir / "repairsid-own.json.bak").exists(), (
        "the allowed archive's repair must still produce the .bak")
