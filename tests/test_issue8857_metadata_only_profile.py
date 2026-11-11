"""#8857: a metadata-only read must carry the sidecar's real owner.

``Session.load_metadata_only`` builds its stub from the sidecar's metadata
prefix (everything before ``messages``). It used to accept any prefix holding
``session_id``, ``title``, ``created_at`` and ``updated_at``, so a sidecar whose
``profile`` key comes AFTER ``messages`` got a stub with ``profile=None``, the
legacy no-profile owner:

* a prefix that also holds ``message_count`` built that stub on every read;
* a prefix without it (the issue's repro) built it once the legacy-facts cache
  was warm, and any earlier full load warms it.

Metadata-only routes then judged the session by the wrong owner: the owner got
404 on ``GET /api/session?messages=0`` and the sidebar's missing-index recovery
listed the row under ``default``. The current writer puts ``profile`` in the
prefix (``null`` included), so only older or outside writers produce this
layout. A prefix without ``profile`` now takes its owner from the legacy-facts
entry a full load recorded for that exact file, else from the full read.
"""
from __future__ import annotations

import collections
import io
import json
import urllib.parse

import pytest

import api.config as config
import api.models as models
import api.profiles as profiles
import api.routes as routes

SID = "owner-8857"
TITLE = "Secret title"
MESSAGES = [
    {"role": "user", "content": "hello", "timestamp": 1.5},
    {"role": "assistant", "content": "hi there", "timestamp": 1.6},
]
LAYOUTS = ["no_count", "counted"]
CACHE_STATES = ["cold", "warm_full_load", "warm_metadata_load"]


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
    # A private legacy-facts cache, so "cold" really is cold.
    monkeypatch.setattr(models, "_LEGACY_SIDECAR_FACTS", collections.OrderedDict())
    no_cli = lambda *_a, **_k: {}  # noqa: E731
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", no_cli)
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", no_cli)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda *_a, **_k: [])
    monkeypatch.setattr(routes, "_claim_or_synthesize_cli_session", lambda *_a, **_k: (None, "no_foreign_state"))
    return directory, cache


def _write_sidecar(directory, layout, *, sid=SID, profile="work"):
    """The issue's layout: timestamps before ``messages``, ``profile`` after it."""
    head = {"session_id": sid, "title": TITLE, "created_at": 1.0, "updated_at": 2.0}
    if layout == "counted":
        # The prefix also carries the modern count keys, so no full read and no
        # legacy-facts lookup ever happened: the stub said None on every read.
        head.update({"message_count": len(MESSAGES), "anchor_scene_index": {}})
    data = {**head, "messages": MESSAGES}
    if profile is not None:
        data["profile"] = profile
    path = directory / f"{sid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _prime(cache_state, layout):
    if cache_state == "warm_full_load":
        assert models.Session.load(SID).profile == "work"
    elif cache_state == "warm_metadata_load":
        models.Session.load_metadata_only(SID)
    if cache_state != "cold" and layout == "no_count":
        # The repro needs a warm legacy-facts entry for this exact file.
        assert models._legacy_sidecar_facts_get(SID) is not None
    if cache_state == "cold":
        assert models._legacy_sidecar_facts_get(SID) is None


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


def _get_metadata(monkeypatch, active):
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active)
    handler = _Handler()
    routes._handle_session_get(
        handler, urllib.parse.urlparse(f"/api/session?session_id={SID}&messages=0")
    )
    return handler


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("cache_state", CACHE_STATES)
def test_load_metadata_only_returns_the_owner_from_the_whole_sidecar(isolated_sessions, layout, cache_state):
    directory, _cache = isolated_sessions
    path = _write_sidecar(directory, layout)
    before = path.read_bytes()
    _prime(cache_state, layout)
    for _ in range(2):  # the second read always runs with a warm facts cache
        stub = models.Session.load_metadata_only(SID)
        assert stub is not None
        assert stub.profile == "work"
        assert stub.title == TITLE
    assert path.read_bytes() == before


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("cache_state", CACHE_STATES)
def test_owner_profile_gets_the_metadata_view(isolated_sessions, monkeypatch, layout, cache_state):
    directory, cache = isolated_sessions
    _write_sidecar(directory, layout)
    _prime(cache_state, layout)
    handler = _get_metadata(monkeypatch, "work")
    assert handler.status == 200, handler.payload()
    session = handler.payload()["session"]
    assert session["title"] == TITLE
    assert session["profile"] == "work"
    assert getattr(cache.get(SID), "profile", "work") == "work"


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("cache_state", CACHE_STATES)
def test_other_profile_is_refused_without_the_title(isolated_sessions, monkeypatch, layout, cache_state):
    directory, cache = isolated_sessions
    _write_sidecar(directory, layout)
    _prime(cache_state, layout)
    handler = _get_metadata(monkeypatch, "default")
    assert handler.status in (404, 409), handler.payload()
    assert TITLE.encode() not in bytes(handler.body)
    if handler.status == 409:
        assert handler.payload().get("profile") == "work"
    # A refused request leaves no stub claiming the legacy owner behind.
    assert getattr(cache.get(SID), "profile", "work") == "work"


@pytest.mark.parametrize("cache_state", ["cold", "warm_full_load"])
def test_other_profile_is_refused_after_the_owner_cached_the_stub(isolated_sessions, monkeypatch, cache_state):
    """The owner's metadata load caches a stub; the other profile then reads the resident one."""
    directory, _cache = isolated_sessions
    _write_sidecar(directory, "no_count")
    _prime(cache_state, "no_count")
    owner = _get_metadata(monkeypatch, "work")
    assert owner.status == 200, owner.payload()
    other = _get_metadata(monkeypatch, "default")
    assert other.status in (404, 409), other.payload()
    assert TITLE.encode() not in bytes(other.body)


def _write_index_anchor(directory):
    """A live index row for another session, so all_sessions() serves from the index."""
    anchor_sid = "anchor-8857"
    (directory / f"{anchor_sid}.json").write_text(json.dumps({
        "session_id": anchor_sid, "title": "Anchor", "created_at": 1.0, "updated_at": 3.0,
        "profile": "default", "message_count": 1, "anchor_scene_index": {},
        "messages": [{"role": "user", "content": "x", "timestamp": 2.5}],
    }), encoding="utf-8")
    (directory / "_index.json").write_text(json.dumps([{
        "session_id": anchor_sid, "title": "Anchor", "created_at": 1.0, "updated_at": 3.0,
        "last_message_at": 2.5, "profile": "default", "message_count": 1,
    }]), encoding="utf-8")


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("cache_state", CACHE_STATES)
def test_sidebar_index_recovery_lists_the_session_under_its_owner_only(
    isolated_sessions, monkeypatch, layout, cache_state,
):
    directory, _cache = isolated_sessions
    _write_sidecar(directory, layout)
    _write_index_anchor(directory)
    _prime(cache_state, layout)
    recovered = []
    original = models.Session.load_metadata_only

    def _spy(cls, sid, **kwargs):
        recovered.append(sid)
        return original(sid, **kwargs)

    monkeypatch.setattr(models.Session, "load_metadata_only", classmethod(_spy))
    rows = models.all_sessions(include_lineage_metadata=False, sidebar_metadata_only=True)
    # The row came through the missing-index recovery, which reads metadata only.
    assert SID in recovered
    by_profile = {
        active: [r["session_id"] for r in rows if profiles._profiles_match(r.get("profile"), active)]
        for active in ("default", "work")
    }
    assert SID in by_profile["work"]
    assert SID not in by_profile["default"]
    # The recovered row persisted to the index carries the owner too.
    index = json.loads((directory / "_index.json").read_text(encoding="utf-8"))
    assert [r.get("profile") for r in index if r.get("session_id") == SID] == ["work"]


@pytest.mark.parametrize("profile", ["work", None])
def test_writer_layout_is_still_served_from_the_prefix(isolated_sessions, monkeypatch, profile):
    """Sidecars the current writer saves keep the cheap path, a no-profile one included.

    save() writes every METADATA_FIELDS key, ``profile: null`` included, before
    ``messages``, so the #8857 owner check never sends it to the full read.
    """
    directory, _cache = isolated_sessions
    session = models.Session(session_id=SID, title=TITLE, profile=profile, messages=list(MESSAGES))
    session.save(skip_index=True)
    raw = (directory / f"{SID}.json").read_text(encoding="utf-8")
    assert raw.index('"profile"') < raw.index('"messages"')

    def _no_full_load(cls, sid, *a, **k):
        raise AssertionError(f"metadata-only read of {sid} took the full load")

    monkeypatch.setattr(models.Session, "load", classmethod(_no_full_load))
    stub = models.Session.load_metadata_only(SID)
    assert getattr(stub, "_loaded_metadata_only", False)
    assert stub.profile == profile
    assert stub.title == TITLE


def test_outside_writer_without_any_profile_key_still_loads_as_legacy(isolated_sessions):
    """No ``profile`` anywhere: the full read finds none either, so the owner stays None."""
    directory, _cache = isolated_sessions
    _write_sidecar(directory, "counted", profile=None)
    for _ in range(2):
        loaded = models.Session.load_metadata_only(SID)
        assert loaded is not None
        assert loaded.profile is None
        assert loaded.title == TITLE


def _count_full_loads(monkeypatch):
    calls = []
    real_load = models.Session.load.__func__

    def _counting_load(cls, sid, *a, **k):
        calls.append(sid)
        return real_load(cls, sid, *a, **k)

    monkeypatch.setattr(models.Session, "load", classmethod(_counting_load))
    return calls


@pytest.mark.parametrize("profile", ["work", None])
def test_profileless_legacy_prefix_is_full_read_once_then_served_with_its_owner(
    isolated_sessions, monkeypatch, profile,
):
    """#5854 keeps an unchanged legacy file to one full parse; #8857 keeps that with the owner.

    The facts entry the full load records carries the owner the parse found
    (None when the file names none), so later reads of the same file are served
    from the prefix with that owner instead of the legacy None.
    """
    directory, _cache = isolated_sessions
    _write_sidecar(directory, "no_count", profile=profile)
    calls = _count_full_loads(monkeypatch)
    for _ in range(3):
        stub = models.Session.load_metadata_only(SID)
        assert stub.profile == profile
        assert stub.title == TITLE
    assert calls == [SID]
    assert getattr(stub, "_loaded_metadata_only", False)
    assert stub._metadata_message_count == len(MESSAGES)


def test_facts_entry_without_an_owner_never_vouches_for_one(isolated_sessions, monkeypatch):
    """An entry stored without ``profile`` (any other writer of the cache) means a full read."""
    directory, _cache = isolated_sessions
    path = _write_sidecar(directory, "no_count")
    models._legacy_sidecar_facts_put(
        SID, len(MESSAGES), {}, expected_sig=models._sidecar_stat_signature(path),
    )
    assert models._legacy_sidecar_facts_get(SID) is not None
    calls = _count_full_loads(monkeypatch)
    assert models.Session.load_metadata_only(SID).profile == "work"
    assert calls == [SID]


def test_edited_file_does_not_reuse_the_previous_owner(isolated_sessions):
    """The facts entry is keyed by the file's stat signature: a rewrite re-reads the owner."""
    directory, _cache = isolated_sessions
    path = _write_sidecar(directory, "no_count")
    assert models.Session.load_metadata_only(SID).profile == "work"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["profile"] = "other"
    data["title"] = TITLE + " (moved)"  # a different size, so the signature differs
    path.write_text(json.dumps(data), encoding="utf-8")
    assert models.Session.load_metadata_only(SID).profile == "other"
    assert models.Session.load_metadata_only(SID).profile == "other"
