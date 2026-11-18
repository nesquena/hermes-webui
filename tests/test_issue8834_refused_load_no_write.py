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


@pytest.mark.parametrize("profile_value", [123, True, ["default"]], ids=["int", "bool", "list"])
@pytest.mark.parametrize("cached", [False, True], ids=["sidecar", "resident"])
def test_chat_start_naming_the_active_profile_never_claims_an_unverifiable_owner(
    isolated_sessions, monkeypatch, stop_past_ownership, cached, profile_value,
):
    """An empty session whose stored ``profile`` isn't a string has unverifiable ownership, so chat/start answers
    404 before any load even when the request names the active profile (the client always sends it): the
    placeholder exception is for a session another profile verifiably owns, not for one nobody can vouch for."""
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, profile=profile_value, messages=[])
    before = path.read_bytes()
    if cached:
        session = models.Session(session_id=SID, title="Legacy")
        session.messages = []
        session.profile = profile_value
        cache[SID] = session
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert models.session_is_unclaimed_placeholder_readonly(SID) is False
    handler = _call("chat_start", active="default", profile="default")
    assert handler.status == 404
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    if cached:
        assert cache[SID].profile == profile_value  # not retagged, not replaced
    else:
        assert SID not in cache


@pytest.mark.parametrize("profile_value", [123, True, ["default"]], ids=["int", "bool", "list"])
def test_chat_start_never_claims_a_resident_only_session_whose_owner_is_unverifiable(
    isolated_sessions, monkeypatch, stop_past_ownership, profile_value,
):
    """A cached session with no sidecar and a non-string ``profile``: ``POST /api/session/new`` creates one
    from ``{"profile": 123}``, since it passes the value through unchecked. The placeholder reader has to call
    it unclaimable from the resident copy alone, and chat/start naming the active profile answers 404
    without retagging it, replacing it or creating a sidecar."""
    directory, cache, _ = isolated_sessions
    session = models.Session(session_id=SID, title="Legacy")
    session.messages = []
    session.profile = profile_value
    cache[SID] = session
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert models.session_is_unclaimed_placeholder_readonly(SID) is False
    handler = _call("chat_start", active="default", profile="default")
    assert handler.status == 404
    assert cache[SID] is session
    assert session.profile == profile_value
    assert not (directory / f"{SID}.json").exists()


def test_chat_start_placeholder_exception_itself_requires_a_verifiable_owner(
    isolated_sessions, monkeypatch, stop_past_ownership,
):
    """The route's own check, independent of the placeholder reader: when the reader says claimable but the
    ownership readings say unverifiable (duplicate ``profile`` keys whose first bytes and full parse
    disagree can do that), chat/start still answers 404 and writes nothing."""
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, profile=123, messages=[])
    before = path.read_bytes()
    monkeypatch.setattr(routes, "session_is_unclaimed_placeholder_readonly", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="default")
    assert handler.status == 404
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
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


@pytest.mark.parametrize("extra", [
    {"pinned": True}, {"context_messages": [{"role": "user", "content": "c"}]}, {"pending_user_message": "queued"},
    {"read_only": True}, {"source_tag": "subagent"}, {"is_cli_session": True}, {"active_stream_id": "dead-stream"},
])
def test_resident_retag_refusals_are_decided_before_the_load(isolated_sessions, monkeypatch, extra):
    """A foreign session that lives only in memory (no sidecar). The resident half of the placeholder test must
    refuse it before the load: read-only and subagent sessions would otherwise be loaded and get the loader's 403, and
    any load moves the session in the LRU order (the newer entry pins that down)."""
    directory, cache, _ = isolated_sessions
    resident = _resident("work", 0, **extra)
    cache[SID] = resident
    cache["newer-8834"] = models.Session(session_id="newer-8834", profile="default")
    order = list(cache)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="default")
    assert (handler.status, handler.payload().get("code"), handler.payload().get("profile")) == (
        409, "session_profile_mismatch", "work")
    assert cache[SID] is resident and resident.profile == "work"
    assert list(cache) == order
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


def _outcome(route, monkeypatch, *, precheck, active):
    if not precheck:
        for name in ("_refuse_cross_profile_before_load", "_refuse_cross_profile_chat_start_before_load",
                     "_refuse_cross_profile_import_cli_before_load"):
            monkeypatch.setattr(routes, name, lambda *_a, **_k: False)
    try:
        handler = _call(route, active=active)
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
@pytest.mark.parametrize("active", ["default", "personal"])
def test_precheck_never_refuses_what_the_load_then_check_path_serves(
    tmp_path, monkeypatch, route, resident, sidecar, disk_ahead, active,
):
    """Differential: the same stored state is requested with the pre-checks and without them (the load-then-check
    path). Whatever that path serves, the pre-check must serve; every refusal must be the same refusal. ``personal``
    is a named profile that can see neither ``work``, ``default`` nor a legacy no-profile session."""
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
        monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active)
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
        results.append(_outcome(route, monkeypatch, precheck=precheck, active=active))
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


# --- the readings helpers every refusal rests on are side-effect free -------------------------------------------

_SIDE_EFFECT_SHAPES = {
    "legacy_partials_messages_first": dict(layout="messages_first"),
    "legacy_partials_scenes_first": dict(layout="scenes_first"),
    "no_profile": dict(profile=None),
    "empty_placeholder": dict(messages=[]),
    "empty_stream_id": dict(messages=[], active_stream_id="dead-stream-8834"),
    "empty_cli": dict(messages=[], is_cli_session=True),
    "non_string_profile": dict(profile=123),
    "body_names_another_session": dict(sid="someone-else"),
    "malformed": None,
    "missing": "missing",
}


def _store_snapshot(directory, cache):
    files = sorted(
        (p.name, p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir() if p.is_file()
    )
    return files, [(k, id(v)) for k, v in cache.items()]


@pytest.mark.parametrize("cached", ["cold", "resident_full", "resident_stub"])
@pytest.mark.parametrize("shape", list(_SIDE_EFFECT_SHAPES))
def test_readings_helpers_never_load_save_cache_or_reorder(isolated_sessions, monkeypatch, shape, cached):
    """Every refusal before the load rests on these helpers: none may load, save, write, index or touch the cache."""
    directory, cache, _ = isolated_sessions
    spec = _SIDE_EFFECT_SHAPES[shape]
    if spec == "missing":
        pass
    elif spec is None:
        (directory / f"{SID}.json").write_text('{"session_id": "legacy-8834", "profile": "work", "messages": [')
    else:
        _sidecar(directory, **spec)
    other = models.Session(session_id="other-8834", profile="default", messages=[{"role": "user", "content": "x"}])
    cache["other-8834"] = other
    if cached == "resident_full":
        cache[SID] = models.Session(session_id=SID, profile="work", messages=[])
    elif cached == "resident_stub" and spec not in (None, "missing") and "sid" not in (spec or {}):
        try:
            cache[SID] = models.Session.load_metadata_only(SID)
        except Exception:
            pass
    before = _store_snapshot(directory, cache)

    def _forbidden(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"readings helper reached {name}")
        return _raise

    for owner, name in (
        (models.Session, "save"), (models.Session, "load"), (models.Session, "load_metadata_only"),
        (models, "_write_session_index"), (models, "_rebuild_session_index_background"),
        (models, "_sync_sidecar_from_state_db_if_newer"), (models, "_try_retry_journal_recovery_in_place"),
        (models, "_repair_stale_pending"), (models, "get_session"), (routes, "get_session"),
    ):
        monkeypatch.setattr(owner, name, _forbidden(name))
    for full_load in (True, False):
        models.session_owner_readings_readonly(SID, full_load=full_load)
    for helper in (models.get_sidecar_session_profile_readonly, models.get_session_profile_readonly):
        try:
            helper(SID)
        except (KeyError, ValueError):
            pass
    models.session_is_unclaimed_placeholder_readonly(SID)
    routes._is_subagent_child_session_id(SID)
    assert _store_snapshot(directory, cache) == before


def _modern_sidecar(directory, *, profile, messages):
    """Metadata before messages, so ``load_metadata_only`` stays a stub (a legacy layout full-loads)."""
    path = directory / f"{SID}.json"
    path.write_text(json.dumps({
        "session_id": SID, "title": "Legacy", "created_at": 1.0, "updated_at": 2.0, "profile": profile,
        "message_count": len(messages), "anchor_scene_index": {}, "messages": messages,
    }))
    return path


def test_a_metadata_stub_never_vouches_for_a_full_load(isolated_sessions, monkeypatch):
    """A stub read while the sidecar was the active profile's, after which the sidecar moved to another profile.
    A full load replaces the stub from disk, so only the sidecar's owner counts: refused before the load, with no
    collapse write and the stub left as it was."""
    directory, cache, _ = isolated_sessions
    rows = [{"role": "user", "content": "hello"}, PARTIAL, dict(PARTIAL)]
    _modern_sidecar(directory, profile="default", messages=rows)
    stub = models.Session.load_metadata_only(SID)
    assert getattr(stub, "_loaded_metadata_only", False) and stub.profile == "default"
    cache[SID] = stub
    path = _modern_sidecar(directory, profile="work", messages=rows)  # moved on disk
    before = path.read_bytes()
    assert models.session_owner_readings_readonly(SID, full_load=True) == ["work"]
    assert models.session_owner_readings_readonly(SID, full_load=False) == ["default"]
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("get_full", active="default")
    assert (handler.status, handler.payload().get("code")) == (409, "session_profile_mismatch")
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert cache[SID] is stub


def test_a_metadata_stub_is_never_a_claimable_placeholder_even_without_a_sidecar(isolated_sessions):
    directory, cache, _ = isolated_sessions
    path = _modern_sidecar(directory, profile="work", messages=[])
    stub = models.Session.load_metadata_only(SID)
    assert getattr(stub, "_loaded_metadata_only", False)
    cache[SID] = stub
    path.unlink()
    assert models.session_is_unclaimed_placeholder_readonly(SID) is False



# --- round 3 (Codex r2): never a different refusal; key order never hides ownership ----------------------------


@pytest.mark.parametrize("route", ["get_full", "chat_start"])
def test_disagreeing_foreign_copies_get_masters_refusal_from_the_load(isolated_sessions, monkeypatch, route):
    """Codex r2: active ``personal``; the resident copy is a legacy no-profile session with one message, the newer
    sidecar is ``work``'s with two. The load reloads the sidecar, so master answers 409 naming ``work``. Refusing
    early from the resident copy gave 404, which on chat/start clears the view and drops the message."""
    directory, cache, _ = isolated_sessions
    _sidecar(directory, profile="work", messages=_rows(2))
    cache[SID] = _resident(None, 1)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "personal")
    handler = _call(route, active="personal")
    assert (handler.status, handler.payload().get("code"), handler.payload().get("profile")) == (
        409, "session_profile_mismatch", "work")


@pytest.mark.parametrize("route", ["get_full", "get_meta", "chat_start", "import_cli"])
def test_profile_before_messages_and_id_after_is_still_read(isolated_sessions, monkeypatch, stop_past_ownership, route):
    """Codex r2: the metadata prefix holds ``profile`` but not ``session_id``. The whole object must be read; a
    valid session must not become unverifiable (404) for its owner."""
    directory, cache, _ = isolated_sessions
    path = directory / f"{SID}.json"
    path.write_text(json.dumps({"profile": "default", "title": "t", "created_at": 1.0, "updated_at": 2.0,
                                "messages": _rows(2), "session_id": SID}))
    assert models.get_sidecar_session_profile_readonly(SID) == "default"
    assert models.session_owner_readings_readonly(SID, full_load=True) == ["default"]
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    try:
        handler = _call(route, active="default")
    except _PastOwnership:
        return
    assert handler.status in (None, 200), (handler.status, handler.payload())


def test_profile_before_messages_and_id_after_is_refused_for_another_profile(isolated_sessions, monkeypatch):
    directory, cache, _ = isolated_sessions
    path = directory / f"{SID}.json"
    path.write_text(json.dumps({"profile": "work", "title": "t", "created_at": 1.0, "updated_at": 2.0,
                                "messages": [{"role": "user", "content": "hello"}, PARTIAL, dict(PARTIAL)],
                                "session_id": SID}))
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("get_full", active="default")
    assert (handler.status, handler.payload().get("code"), handler.payload().get("profile")) == (
        409, "session_profile_mismatch", "work")
    assert path.read_bytes() == before and not path.with_suffix(".json.bak").exists()



# --- round 4 (senior r3 findings 3 and 4) -----------------------------------------------------------------------


@pytest.mark.parametrize("route", ["get_full", "chat_start"])
def test_a_visible_resident_copy_skips_the_sidecar_read(isolated_sessions, monkeypatch, route):
    """The owner's usual request (its session resident) must not parse the sidecar: one visible copy already means
    the load decides. Senior r3 finding 3 measured 3-13 ms per request for that parse."""
    directory, cache, _ = isolated_sessions
    _sidecar(directory, profile="default", messages=_rows(2))
    cache[SID] = _resident("default", 2)

    def _no_sidecar_read(_sid):
        raise AssertionError("sidecar read although the resident copy is visible")

    monkeypatch.setattr(models, "get_sidecar_session_profile_readonly", _no_sidecar_read)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _Handler()
    if route == "get_full":
        assert routes._refuse_cross_profile_before_load(handler, SID, full_load=True) is False
    else:
        assert routes._refuse_cross_profile_chat_start_before_load(handler, {"session_id": SID}) is False
    assert handler.status is None


def test_an_invisible_resident_copy_still_reads_the_sidecar(isolated_sessions, monkeypatch):
    """The skip is only for a visible resident copy: a foreign one must still be weighed against the sidecar."""
    directory, cache, _ = isolated_sessions
    _sidecar(directory, profile="default", messages=_rows(2))
    cache[SID] = _resident("work", 1)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    readings = routes._readings_for(_Handler(), SID, full_load=True)
    assert readings == ["work", "default"]
    handler = _Handler()
    assert routes._refuse_cross_profile_before_load(handler, SID, full_load=True) is False


@pytest.mark.parametrize("resident_owner,sidecar_owner", [("", None), (None, ""), ("", "")])
@pytest.mark.parametrize("route", ["get_full", "chat_start"])
def test_empty_string_and_none_owners_agree(isolated_sessions, monkeypatch, route, resident_owner, sidecar_owner):
    """Senior r3 finding 4: "" and no profile are the same legacy owner to every route's answer, so the request is
    refused before the load (404, master's answer) and nothing is written. Compared literally they "disagreed", the
    load ran, and the refusal collapse-wrote the sidecar. Every pairing (senior r4 finding 1: normalising only the
    first reading passed the single-pair test)."""
    directory, cache, _ = isolated_sessions
    path = _sidecar(directory, profile=sidecar_owner)  # [user, partial, partial]; None omits the key
    cache[SID] = _resident(resident_owner, 1)
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "personal")
    handler = _call(route, active="personal")
    assert handler.status == 404, (handler.status, handler.payload())
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()



# --- round 5 (senior r4 finding 4) ------------------------------------------------------------------------------


# The JSON parser's nesting limit differs by Python version: 3,000 levels overflow 3.11 but parse on 3.12 and 3.13
# (CI runs all three), and 3.14 parses 10,000. 200,000 overflows every version CI and this host run.
_TOO_DEEP = 200_000


def _deep_sidecar(directory, *, profile="work"):
    deep = "[" * _TOO_DEEP + "]" * _TOO_DEEP
    path = directory / f"{SID}.json"
    path.write_text('{"messages": [' + deep + '], "session_id": "%s", "title": "t", "created_at": 1.0, '
                    '"updated_at": 2.0, "profile": "%s"}' % (SID, profile))
    # Non-vacuity on the running interpreter: the fixture must really be too deep to parse.
    with pytest.raises(RecursionError):
        json.loads(path.read_bytes())
    return path


def test_a_sidecar_too_deep_to_parse_is_unverifiable(isolated_sessions):
    directory, cache, _ = isolated_sessions
    _deep_sidecar(directory)
    with pytest.raises(ValueError):
        models.get_sidecar_session_profile_readonly(SID)
    assert models.session_owner_readings_readonly(SID, full_load=True) == [models.UNVERIFIABLE_SESSION_OWNER]


@pytest.mark.parametrize("profile", ["work", "default"])
def test_chat_start_naming_the_active_profile_on_a_too_deep_sidecar_is_refused_not_a_500(isolated_sessions,
                                                                                        monkeypatch, profile):
    """Senior r5: the real client always sends `profile`, so chat/start reaches the placeholder check, which parsed
    the sidecar itself and let RecursionError through (a 500, as on master). The sidecar is unreadable, so it isn't
    a claimable placeholder: refused (404, ownership unverifiable), nothing written, nothing cached."""
    directory, cache, _ = isolated_sessions
    path = _deep_sidecar(directory, profile=profile)
    before = path.read_bytes()
    assert models.session_is_unclaimed_placeholder_readonly(SID) is False
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call("chat_start", active="default", profile="default")
    assert handler.status == 404, (handler.status, handler.payload())
    assert path.read_bytes() == before
    assert SID not in cache


@pytest.mark.parametrize("route", ["get_full", "chat_start"])
def test_a_sidecar_too_deep_to_parse_beside_a_foreign_resident_copy_gets_masters_409(isolated_sessions, monkeypatch,
                                                                                     route):
    """Senior r4: a foreign resident copy plus a sidecar nested past the parser's recursion limit. The readings
    disagree (work vs unverifiable), so the load decides and gives master's 409 naming work; the pre-check used to
    raise RecursionError (a 500)."""
    directory, cache, _ = isolated_sessions
    _deep_sidecar(directory)
    cache[SID] = _resident("work", 1)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    handler = _call(route, active="default")
    assert (handler.status, handler.payload().get("code"), handler.payload().get("profile")) == (
        409, "session_profile_mismatch", "work")
