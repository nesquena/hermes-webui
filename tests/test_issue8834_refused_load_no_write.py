"""#8834: a refused cross-profile request never loads, rewrites or caches the session.

GET /api/session, POST /api/chat/start and POST /api/session/import_cli own their
profile boundary (they are exempt from the generic request-session guard). They
used to full-load the session first and decide ownership on the loaded object.
``Session.load`` collapses adjacent duplicate ``_partial`` rows and saves (with a
``.bak``), and ``get_session`` caches what it loads, so a request answered 409/404
still rewrote and cached another profile's sidecar. #8094 set the rule this
restores: a refused request never writes.
"""
from __future__ import annotations

import collections
import hashlib
import http.client
import io
import json
import threading
import urllib.parse

import pytest

import api.auth as auth
import api.config as config
import api.models as models
import api.profiles as profiles
import api.routes as routes

SID = "legacy-8834"
PARTIAL = {"role": "assistant", "content": "interrupted", "_partial": True}


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    cache = collections.OrderedDict()
    for module in (config, models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", directory)
        monkeypatch.setattr(module, "SESSIONS", cache)
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", directory / "_index.json")
    calls = collections.Counter()

    def _no_cli(*_a, **_k):
        calls["cli_lookup"] += 1
        return {}

    def _no_claim(*_a, **_k):
        calls["claim"] += 1
        return None, "no_foreign_state"

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", _no_cli)
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", _no_cli)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda *_a, **_k: [])
    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", _no_claim)
    # The checks ahead of chat/start's ownership decision are not under test.
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda *_a, **_k: None)
    return directory, cache, calls


class _PastOwnership(Exception):
    """Raised where chat/start (and GET's continuation adoption) go past the ownership check."""


@pytest.fixture
def stop_past_ownership(monkeypatch):
    import api.compression_continuation as continuation

    def _stop(*_a, **_k):
        raise _PastOwnership()

    monkeypatch.setattr(continuation, "durable_compression_continuation", _stop)


def _sidecar(directory, *, profile="work", layout="messages_first", messages=None, sid=SID, **extra):
    messages = [{"role": "user", "content": "hello"}, PARTIAL, dict(PARTIAL)] if messages is None else messages
    metadata = {"session_id": sid, "title": "Legacy", "created_at": 1.0, "updated_at": 2.0, **extra}
    if profile is not None:
        metadata["profile"] = profile
    if layout == "messages_first":
        data = {"messages": messages, **metadata}
    else:
        # Pre-modern layout: scenes precede the count and there is no scene index.
        data = {**metadata, "anchor_activity_scenes": [], "message_count": len(messages), "messages": messages}
    path = directory / f"{SID}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _Handler:
    def __init__(self):
        self.status = None
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(b"")
        self.headers = {"Content-Length": "0"}
        self.request = None
        self.client_address = ("127.0.0.1", 1)

    def send_response(self, status):
        self.status = status

    def send_header(self, *_a):
        pass

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def payload(self):
        return json.loads(bytes(self.body) or b"{}")


def _call(route, *, active, profile=None, all_profiles_profile=None):
    handler = _Handler()
    if route in ("get_full", "get_meta"):
        flag = "1" if route == "get_full" else "0"
        routes._handle_session_get(handler, urllib.parse.urlparse(f"/api/session?session_id={SID}&messages={flag}"))
    elif route == "chat_start":
        body = {"session_id": SID, "message": "hi"}
        if profile is not None:
            body["profile"] = profile
        routes._handle_chat_start(handler, body)
    else:
        body: dict = {"session_id": SID}
        if all_profiles_profile is not None:
            body["all_profiles"] = True
            body["profile"] = all_profiles_profile
        routes._handle_session_import_cli(handler, body)
    return handler


def _assert_untouched(path, before, cache):
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert SID not in cache


REFUSALS = [
    # (route, owner, active, extra call kwargs, expected status, expected code)
    ("get_full", "work", "default", {}, 409, "session_profile_mismatch"),
    ("get_meta", "work", "default", {}, 409, "session_profile_mismatch"),
    ("get_full", None, "work", {}, 404, None),
    ("get_meta", None, "work", {}, 404, None),
    ("chat_start", "work", "default", {}, 409, "session_profile_mismatch"),
    # The request names the active profile, but the session has turns, so the retag refuses it.
    ("chat_start", "work", "default", {"profile": "default"}, 409, "session_profile_mismatch"),
    ("chat_start", None, "work", {}, 404, None),
    ("import_cli", "work", "default", {}, 409, "session_profile_mismatch"),
    ("import_cli", None, "work", {}, 404, None),
    # all_profiles import with a profile that doesn't own the session.
    ("import_cli", "work", "default", {"all_profiles_profile": "default"}, 404, None),
    ("import_cli", None, "default", {"all_profiles_profile": "work"}, 404, None),
]


@pytest.mark.parametrize("layout", ["messages_first", "scenes_first"])
@pytest.mark.parametrize("route,owner,active,kwargs,status,code", REFUSALS)
def test_refused_request_leaves_sidecar_backup_and_cache_untouched(
    isolated_sessions, monkeypatch, layout, route, owner, active, kwargs, status, code,
):
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, profile=owner, layout=layout)
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active)
    handler = _call(route, active=active, **kwargs)
    assert handler.status == status, handler.payload()
    if code:
        assert handler.payload().get("code") == code
        assert handler.payload().get("profile") == owner
    _assert_untouched(path, before, cache)


@pytest.mark.parametrize("route", ["get_full", "get_meta", "chat_start", "import_cli"])
def test_resident_foreign_session_is_refused_without_reload_or_promotion(isolated_sessions, monkeypatch, route):
    """A foreign session already cached (full or metadata-only) is refused from memory, cache order intact."""
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory)
    before = path.read_bytes()
    cache[SID] = models.Session.load_metadata_only(SID)
    path.write_bytes(before)  # a legacy layout full-loads on the metadata path; restore the bytes
    path.with_suffix(".json.bak").unlink(missing_ok=True)
    cache["newer-8834"] = models.Session(session_id="newer-8834", profile="default")
    order = list(cache)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call(route, active="default")
    assert handler.status == 409
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert list(cache) == order


def test_metadata_stub_of_a_nonempty_session_is_not_an_empty_placeholder(isolated_sessions, monkeypatch):
    """A cached metadata-only stub has messages=[] by design; it must not pass as claimable."""
    directory, cache, _ = isolated_sessions
    path = directory / f"{SID}.json"
    # Modern layout: the metadata the prefix reader needs comes before messages, so the stub stays a stub.
    path.write_text(json.dumps({
        "session_id": SID, "title": "Legacy", "created_at": 1.0, "updated_at": 2.0, "profile": "work",
        "message_count": 3, "anchor_scene_index": {},
        "messages": [{"role": "user", "content": "hello"}, PARTIAL, dict(PARTIAL)],
    }))
    before = path.read_bytes()
    stub = models.Session.load_metadata_only(SID)
    assert getattr(stub, "_loaded_metadata_only", False) and stub.messages == []
    cache[SID] = stub
    assert models.session_is_unclaimed_placeholder_readonly(SID) is False
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="default")
    assert (handler.status, handler.payload().get("code")) == (409, "session_profile_mismatch")
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert cache[SID] is stub


@pytest.mark.parametrize("extra", [
    {"pinned": True},
    {"context_messages": [{"role": "user", "content": "kept context"}]},
    {"pending_user_message": "queued"},
])
def test_retag_refusals_are_decided_before_the_load(isolated_sessions, monkeypatch, extra):
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, messages=[], **extra)
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="default")
    assert (handler.status, handler.payload().get("code")) == (409, "session_profile_mismatch")
    _assert_untouched(path, before, cache)


@pytest.mark.parametrize("requested", [None, "work", "other"])
def test_empty_placeholder_is_refused_before_the_load_unless_the_active_profile_asks(
    isolated_sessions, monkeypatch, requested,
):
    """Only a request naming the active profile can reach the retag; any other refusal must not load
    (and so cache) the placeholder first."""
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, messages=[])
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile=requested)
    assert (handler.status, handler.payload().get("code")) == (409, "session_profile_mismatch")
    _assert_untouched(path, before, cache)


@pytest.mark.parametrize("cached", ["cold", "resident"])
def test_empty_placeholder_is_still_claimed_by_the_retag(isolated_sessions, monkeypatch, stop_past_ownership, cached):
    directory, cache, _ = isolated_sessions
    _sidecar(directory, messages=[])
    if cached == "resident":
        cache[SID] = models.Session.load(SID)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert models.session_is_unclaimed_placeholder_readonly(SID) is True
    with pytest.raises(_PastOwnership):
        _call("chat_start", active="default", profile="default")
    assert cache[SID].profile == "default"


def test_new_in_memory_placeholder_is_still_claimed(isolated_sessions, monkeypatch, stop_past_ownership):
    """The real flow: new chat in one profile, switch, send. ``new_session`` keeps the session in memory only
    until its first message, so there is no sidecar to read."""
    directory, cache, _ = isolated_sessions
    cache[SID] = models.Session(session_id=SID, profile="work")
    assert not (directory / f"{SID}.json").exists()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    with pytest.raises(_PastOwnership):
        _call("chat_start", active="default", profile="default")
    assert cache[SID].profile == "default"


_ROWS = [{"role": "user", "content": "from state.db"}, {"role": "assistant", "content": "reply"}]


@pytest.mark.parametrize("marker", ["read_only", "subagent_source", "subagent_child_id", "cli_import", "stream_id"])
@pytest.mark.parametrize("cached", ["cold", "resident"])
def test_placeholder_the_load_would_refuse_or_write_is_refused_before_the_load(
    isolated_sessions, monkeypatch, marker, cached,
):
    """An empty foreign session the retag would accept but the load would not: read-only and subagent sessions
    are refused by the loader (403), a CLI import's load refreshes its transcript (then the retag refuses), and an
    unfinished-turn stream id lets the load sync rows from state.db into the sidecar. Each was loaded (and the last
    one written) before being refused; now each is refused before the load."""
    directory, cache, _ = isolated_sessions
    extra = {"read_only": {"read_only": True}, "subagent_source": {"source_tag": "subagent"},
             "cli_import": {"is_cli_session": True}, "stream_id": {"active_stream_id": "dead-stream-8834"},
             "subagent_child_id": {}}[marker]
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda sid: marker == "subagent_child_id")
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda *_a, **_k: list(_ROWS))

    def _sync_writes(session):
        session.messages = list(_ROWS)
        session.active_stream_id = None
        session.save(touch_updated_at=False, skip_index=True)
        return True

    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", _sync_writes)
    path = _sidecar(directory, messages=[], **extra)
    before = path.read_bytes()
    resident = None
    if cached == "resident":
        resident = models.Session(**json.loads(before))
        cache[SID] = resident
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="default")
    assert (handler.status, handler.payload().get("code"), handler.payload().get("profile")) == (
        409, "session_profile_mismatch", "work")
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    if resident is None:
        assert SID not in cache
    else:
        assert cache[SID] is resident and resident.profile == "work" and resident.messages == []


def test_non_string_session_id_is_not_found_on_chat_start(isolated_sessions, monkeypatch):
    directory, cache, _ = isolated_sessions
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    for sid in (12345, ["x"], {"id": SID}):
        handler = _Handler()
        routes._handle_chat_start(handler, {"session_id": sid, "message": "hi"})
        assert handler.status == 404, sid
    assert list(directory.iterdir()) == [] and not cache


def test_foreign_session_with_an_invalid_requested_profile_keeps_the_400(isolated_sessions, monkeypatch):
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory)
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="not a profile!")
    assert handler.status == 400
    assert handler.payload().get("error") == "invalid profile"
    _assert_untouched(path, before, cache)


@pytest.mark.parametrize("route", ["get_full", "chat_start", "import_cli"])
def test_owner_requests_still_load_and_repair(isolated_sessions, monkeypatch, stop_past_ownership, route):
    """Positive control: the owner's own request still runs the load, so the collapse and its .bak happen."""
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, profile="work")
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    try:
        handler = _call(route, active="work")
    except _PastOwnership:
        handler = None
    if handler is not None:
        assert handler.status in (None, 200), handler.payload()
    assert len(json.loads(path.read_bytes())["messages"]) == 2
    assert path.with_suffix(".json.bak").exists()


@pytest.mark.parametrize("route", ["get_full", "chat_start", "import_cli"])
@pytest.mark.parametrize("payload", [
    '{"session_id":',
    json.dumps({"session_id": "other-8834", "profile": "default",
                "messages": [{"role": "user", "content": "hello"}, PARTIAL, dict(PARTIAL)]}),
    json.dumps({"session_id": SID, "profile": {"name": "default"},
                "messages": [{"role": "user", "content": "hello"}, PARTIAL, dict(PARTIAL)]}),
], ids=["malformed_json", "body_names_another_session", "non_string_profile"])
def test_unverifiable_ownership_fails_closed_and_writes_nothing(isolated_sessions, monkeypatch, route, payload):
    """Same rule as #8094's guard. A body naming another session must not reach Session.load,
    whose collapse save would write the file that body names (#8852)."""
    directory, cache, _ = isolated_sessions
    path = directory / f"{SID}.json"
    path.write_text(payload)
    other = directory / "other-8834.json"
    other.write_text(json.dumps({"session_id": "other-8834", "profile": "work",
                                 "messages": [{"role": "user", "content": f"turn {i}"} for i in range(4)]}))
    other_before = other.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call(route, active="default")
    assert handler.status == 404
    assert path.read_text() == payload
    assert not path.with_suffix(".json.bak").exists()
    assert other.read_bytes() == other_before
    assert not other.with_suffix(".json.bak").exists()
    assert SID not in cache


@pytest.mark.parametrize("route,counter", [("get_full", "cli_lookup"), ("chat_start", "claim"), ("import_cli", "cli_lookup")])
def test_missing_sidecar_still_reaches_the_foreign_session_path(isolated_sessions, monkeypatch, route, counter):
    directory, cache, calls = isolated_sessions
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call(route, active="default")
    assert handler.status == 404
    assert calls[counter] >= 1
    assert list(directory.iterdir()) == []
    assert not cache


# --- round 3: the pre-check decides on every object the load could serve, never on a stale one ------------------


def _rows(n, *, partials=False):
    rows = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(n)]
    return rows + ([dict(PARTIAL), dict(PARTIAL)] if partials else [])


def _resident(profile, n, **extra):
    session = models.Session(session_id=SID, title="Resident", profile=profile, messages=_rows(n), **extra)
    return session


def test_import_cli_decides_on_the_sidecar_not_an_in_memory_retag(isolated_sessions, monkeypatch):
    """Codex r1: a failed chat/start can retag an empty session in memory without saving it. import_cli loads only
    the sidecar, so the sidecar's owner decides: an all_profiles import for that owner is still served."""
    directory, cache, _ = isolated_sessions
    _sidecar(directory, messages=[])  # owned by work on disk
    cache[SID] = _resident("default", 0)  # retagged in memory only
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("import_cli", active="default", all_profiles_profile="work")
    assert handler.status == 200, handler.payload()


def test_full_get_follows_a_sidecar_that_moved_past_a_stale_foreign_copy(
    isolated_sessions, monkeypatch, stop_past_ownership,
):
    """Codex r1: the resident copy says another profile, but the sidecar on disk is newer and owned by the active
    profile. The full load reloads the sidecar and serves it, so the pre-check must not refuse on the stale copy."""
    directory, cache, _ = isolated_sessions
    _sidecar(directory, profile="default", messages=_rows(4))
    cache[SID] = _resident("work", 1)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    with pytest.raises(_PastOwnership):
        _call("get_full", active="default")
    assert cache[SID].profile == "default" and len(cache[SID].messages) == 4


def test_resident_placeholder_with_a_sidecar_that_moved_ahead_is_refused_before_the_load(isolated_sessions, monkeypatch):
    """Senior r1 (2B): the resident copy is an empty placeholder, but another writer advanced the sidecar (with
    duplicate partials). The load would reload it, collapse-write and then refuse; the placeholder test has to hold
    on the sidecar too."""
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, messages=_rows(1, partials=True))
    before = path.read_bytes()
    resident = _resident("work", 0)
    cache[SID] = resident
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert models.session_is_unclaimed_placeholder_readonly(SID) is False
    handler = _call("chat_start", active="default", profile="default")
    assert (handler.status, handler.payload().get("code")) == (409, "session_profile_mismatch")
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert cache[SID] is resident and resident.profile == "work"


@pytest.mark.parametrize("extra", [{"pinned": True}, {"context_messages": [{"role": "user", "content": "c"}]},
                                   {"pending_user_message": "queued"}])
def test_resident_retag_refusals_are_decided_before_the_load(isolated_sessions, monkeypatch, extra):
    directory, cache, _ = isolated_sessions
    resident = _resident("work", 0, **extra)
    cache[SID] = resident
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="default")
    assert (handler.status, handler.payload().get("code")) == (409, "session_profile_mismatch")
    assert cache[SID] is resident and resident.profile == "work"
    assert list(directory.iterdir()) == []


def test_non_list_context_messages_still_claims_like_the_retag(isolated_sessions, monkeypatch, stop_past_ownership):
    """``Session.__init__`` drops a non-list ``context_messages``, so the retag claims this session; the sidecar
    reading must agree."""
    directory, cache, _ = isolated_sessions
    _sidecar(directory, messages=[], context_messages={"a": 1})
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert models.session_is_unclaimed_placeholder_readonly(SID) is True
    with pytest.raises(_PastOwnership):
        _call("chat_start", active="default", profile="default")
    assert cache[SID].profile == "default"


_RESIDENTS = ["none", "work", "default", "legacy", "stub"]
_SIDECARS = ["none", "work", "default", "legacy"]


def _outcome(route, monkeypatch, *, precheck):
    if not precheck:
        for name in ("_refuse_cross_profile_before_load", "_refuse_cross_profile_chat_start_before_load",
                     "_refuse_cross_profile_import_cli_before_load"):
            monkeypatch.setattr(routes, name, lambda *_a, **_k: False)
    try:
        handler = _call(route, active="default")
    except _PastOwnership:
        return ("served",)
    finally:
        monkeypatch.undo()
    if handler.status in (None, 200):
        return ("served",)
    payload = handler.payload()
    return (handler.status, payload.get("code"), payload.get("profile"))


@pytest.mark.parametrize("route", ["get_full", "get_meta", "chat_start", "import_cli"])
@pytest.mark.parametrize("resident", _RESIDENTS)
@pytest.mark.parametrize("sidecar", _SIDECARS)
@pytest.mark.parametrize("disk_ahead", [True, False])
def test_precheck_never_refuses_what_the_load_then_check_path_serves(
    tmp_path, monkeypatch, route, resident, sidecar, disk_ahead,
):
    """Differential: the same stored state is requested with the pre-checks and without them (the load-then-check
    path). Whatever that path serves, the pre-check must serve; every refusal must be the same refusal."""
    if sidecar == "none" and resident in ("none", "stub"):
        pytest.skip("nothing stored")
    results = []
    for precheck in (False, True):
        directory = tmp_path / f"sessions-{precheck}"
        directory.mkdir()
        cache = collections.OrderedDict()
        for module in (config, models, routes):
            monkeypatch.setattr(module, "SESSION_DIR", directory)
            monkeypatch.setattr(module, "SESSIONS", cache)
            if hasattr(module, "SESSION_INDEX_FILE"):
                monkeypatch.setattr(module, "SESSION_INDEX_FILE", directory / "_index.json")
        for name, value in (("_lookup_cli_session_metadata", {}), ("_resolve_cli_import_metadata", {})):
            monkeypatch.setattr(routes, name, lambda *_a, _v=value, **_k: _v)
        monkeypatch.setattr(routes, "get_cli_session_messages", lambda *_a, **_k: [])
        monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", lambda *_a, **_k: (None, "no_foreign_state"))
        monkeypatch.setattr(routes, "run_admission_closed_response", lambda *_a, **_k: None)
        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda *_a, **_k: None)
        monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
        import api.compression_continuation as continuation

        def _stop(*_a, **_k):
            raise _PastOwnership()

        monkeypatch.setattr(continuation, "durable_compression_continuation", _stop)
        owner = {"work": "work", "default": "default", "legacy": None}
        disk_n, cache_n = (4, 1) if disk_ahead else (1, 4)
        if sidecar != "none":
            _sidecar(directory, profile=owner[sidecar], messages=_rows(disk_n))
        if resident == "stub" and sidecar != "none":
            cache[SID] = models.Session.load_metadata_only(SID)
        elif resident in owner:
            cache[SID] = _resident(owner[resident], cache_n)
        # _outcome undoes monkeypatch after the call, so the fixture state above is re-applied per run.
        results.append(_outcome(route, monkeypatch, precheck=precheck))
    without, with_precheck = results
    if without == ("served",):
        assert with_precheck == ("served",), (without, with_precheck)
    else:
        assert with_precheck == without, (without, with_precheck)


# --- the real HTTP server: dispatch, password auth, CSRF and the generic-guard exemption ---------------


@pytest.fixture
def authenticated_http(monkeypatch):
    import server

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "dummy-8834-password")
    auth._invalidate_password_hash_cache()
    monkeypatch.setattr(routes, "_get_active_profile_name", profiles.get_active_profile_name)
    httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(method, path, payload=None, *, cookie=None, csrf=None):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Content-Type": "application/json", "Connection": "close",
                   "Origin": f"http://127.0.0.1:{httpd.server_port}"}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers[auth.CSRF_HEADER_NAME] = csrf
        try:
            connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}"), response.getheaders()
        finally:
            connection.close()

    try:
        status, _, headers = request("POST", "/api/auth/login", {"password": "dummy-8834-password"})
        assert status == 200
        cookie = next(value.split(";", 1)[0] for name, value in headers
                      if name.lower() == "set-cookie" and value.startswith(auth._resolve_cookie_name() + "="))
        csrf = auth.csrf_token_for_session(cookie.split("=", 1)[1])
        assert csrf
        yield request, cookie, csrf
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        auth._invalidate_password_hash_cache()


@pytest.mark.parametrize("method,path,payload", [
    ("GET", f"/api/session?session_id={SID}&messages=1", None),
    ("GET", f"/api/session?session_id={SID}&messages=0", None),
    ("POST", "/api/chat/start", {"session_id": SID, "message": "hi", "profile": "default"}),
    ("POST", "/api/session/import_cli", {"session_id": SID}),
])
def test_real_server_refusal_writes_nothing(isolated_sessions, authenticated_http, method, path, payload):
    directory, cache, _ = isolated_sessions
    sidecar = _sidecar(directory)
    before = sidecar.read_bytes()
    request, cookie, csrf = authenticated_http
    status, body, _ = request(method, path, payload, cookie=cookie + "; hermes_profile=default",
                              csrf=csrf if method == "POST" else None)
    assert (status, body.get("code"), body.get("profile")) == (409, "session_profile_mismatch", "work")
    _assert_untouched(sidecar, before, cache)


# --- the release manager's 18-input malformed-metadata probe (#8523 review), over real HTTP on all three routes ---

PROBE_SID = "malformed-8523"
PROBE_CASES = [
    "profile_int", "profile_list", "profile_dict", "profile_empty_string", "profile_bool",
    "sid_mismatch", "not_a_dict", "corrupt_json", "truncated_json", "empty_file",
    "cached_profile_int", "cached_profile_int_and_sidecar_ok",
    "body_sid_int", "body_sid_list", "body_sid_unsafe", "body_sid_missing_file",
    "readonly_flag_profile_int", "external_source_profile_int",
]
# Every case whose stored ownership can't be verified, or whose request names no session: refused with 404.
# The two cached cases keep a valid sidecar owned by the active profile; only the resident copy carries the int
# profile. A full load may serve either, so the load decides, and like master it serves them (#8834 refuses before
# the load only when no copy the load could serve is visible).
PROBE_SERVED = {"profile_empty_string", "cached_profile_int", "cached_profile_int_and_sidecar_ok"}
PROBE_LOADER_500 = {"corrupt_json"}


def _sha(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return None


def _probe_fixture(directory, cache, case):
    path = directory / f"{PROBE_SID}.json"
    sid = PROBE_SID

    def good(**extra):
        return models.Session(session_id=PROBE_SID, title="t", profile="default",
                              messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                              **extra)

    if case.startswith("profile_") or case in ("readonly_flag_profile_int", "external_source_profile_int"):
        extra = {"readonly_flag_profile_int": {"read_only": True},
                 "external_source_profile_int": {"source_tag": "desktop", "session_source": "other"}}.get(case, {})
        good(**extra).save()
        data = json.loads(path.read_text())
        data["profile"] = {"profile_int": 123, "profile_list": ["default"], "profile_dict": {"name": "default"},
                           "profile_empty_string": "", "profile_bool": True}.get(case, 123)
        path.write_text(json.dumps(data))
    elif case == "sid_mismatch":
        good().save()
        data = json.loads(path.read_text())
        data["session_id"] = "someone-else"
        path.write_text(json.dumps(data))
    elif case == "not_a_dict":
        path.write_text(json.dumps(["not", "a", "dict"]))
    elif case == "corrupt_json":
        path.write_text('{"session_id": "malformed-8523", "profile": "default", "messages": [')
    elif case == "truncated_json":
        good().save()
        path.write_bytes(path.read_bytes()[:40])
    elif case == "empty_file":
        path.write_bytes(b"")
    elif case.startswith("cached_profile_int"):
        session = good()
        session.save()
        session.profile = 123
        cache[PROBE_SID] = session
    else:
        sid = {"body_sid_int": 12345, "body_sid_list": ["x"], "body_sid_unsafe": "../etc/passwd",
               "body_sid_missing_file": "no-such-session-8523"}[case]
    cache.pop("someone-else", None)
    return path, sid


@pytest.mark.parametrize("route", ["get", "chat_start", "import_cli"])
@pytest.mark.parametrize("case", PROBE_CASES)
def test_malformed_ownership_probe_never_500s_or_writes(
    isolated_sessions, authenticated_http, tmp_path, monkeypatch, case, route,
):
    directory, cache, _ = isolated_sessions
    home = tmp_path / "hermes_home"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    started = []
    monkeypatch.setattr(routes, "_start_run",
                        lambda *a, **k: started.append(1) or {"ok": "admitted", "_status": 200})
    path, sid = _probe_fixture(directory, cache, case)
    before = (_sha(path), _sha(directory / "_index.json"), sorted(p.name for p in directory.iterdir()))
    request, cookie, csrf = authenticated_http
    cookie = cookie + "; hermes_profile=default"
    if route == "get":
        from urllib.parse import quote
        status, body, _ = request("GET", f"/api/session?session_id={quote(str(sid))}&messages=1", cookie=cookie)
    elif route == "chat_start":
        status, body, _ = request("POST", "/api/chat/start", {"session_id": sid, "message": "hello"},
                                  cookie=cookie, csrf=csrf)
    else:
        status, body, _ = request("POST", "/api/session/import_cli", {"session_id": sid}, cookie=cookie, csrf=csrf)
    assert status != 500 or case in PROBE_LOADER_500, body
    if case in PROBE_SERVED:
        assert status == 200, body
        if route == "chat_start":
            assert started
        return
    after = (_sha(path), _sha(directory / "_index.json"), sorted(p.name for p in directory.iterdir()))
    assert after == before
    assert not started
    if not case.startswith("cached_"):
        assert PROBE_SID not in cache
    if case in PROBE_LOADER_500:
        # Pre-existing and unchanged by #8834: the prefix names the active profile, so the request is authorized,
        # and the loader then fails on the corrupt body. Not an ownership decision; nothing is written.
        assert status in (404, 500), body
        return
    assert status == 404, body
