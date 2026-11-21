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
layout. A prefix without ``profile`` now makes the read take the whole file
once, without ``Session.load``'s self-heal write, so the title and the owner
come from one read. Nothing about the owner is cached: the earlier fix cached it
under the file's stat signature, and a rewrite that kept the signature (same
size, same timestamp tick) or a symlink swap then paired the new file's title
with the previous owner (release gate finding).
"""
from __future__ import annotations

import collections
import io
import json
import os
import urllib.parse

import pytest

import time

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
    """Record each ``Session.load`` call as ``(sid, repair)``."""
    calls = []
    real_load = models.Session.load.__func__

    def _counting_load(cls, sid, *a, **k):
        calls.append((sid, k.get("_repair", True)))
        return real_load(cls, sid, *a, **k)

    monkeypatch.setattr(models.Session, "load", classmethod(_counting_load))
    return calls


def _files(directory):
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def _raw(owner, title, layout="counted", messages=MESSAGES, scenes=None):
    """One version of the sidecar, ``profile`` after ``messages`` (an older or outside writer)."""
    head = {"session_id": SID, "title": title, "created_at": 1.0, "updated_at": 2.0}
    if layout == "counted":
        head.update({"message_count": len(messages), "anchor_scene_index": {}})
    if scenes is not None:
        # The pre-#5854 layout: scene bodies before the count and the messages.
        head["anchor_activity_scenes"] = scenes
    return json.dumps({**head, "messages": messages, "profile": owner})


def _same_signature_for_every_version(monkeypatch):
    """The gate's case, made deterministic: every version of the file has one stat signature.

    A same-size rewrite inside one timestamp tick, or a symlink swap between two
    files written in the same tick, keeps ``(mtime_ns, size, ctime_ns)``. The
    live repro needed thousands of rewrites to land one; pinning the signature
    hands any cache keyed by it that collision on every rewrite.
    """
    monkeypatch.setattr(models, "_sidecar_stat_signature", lambda path: ("pinned", str(path)))


def _assert_work_never_sees(monkeypatch, title):
    handler = _get_metadata(monkeypatch, "work")
    assert handler.status in (404, 409), handler.payload()
    assert title.encode() not in bytes(handler.body)


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("profile", ["work", None])
def test_profileless_prefix_takes_one_full_read_per_metadata_read_and_writes_nothing(
    isolated_sessions, monkeypatch, layout, profile,
):
    """No owner cache: every metadata read of this layout is one full read that never writes.

    The owner then comes from the same read as the title, and another
    profile's sidebar poll, which lands here, leaves the file alone.
    """
    directory, _cache = isolated_sessions
    _write_sidecar(directory, layout, profile=profile)
    before = _files(directory)
    calls = _count_full_loads(monkeypatch)
    for _ in range(3):
        loaded = models.Session.load_metadata_only(SID)
        assert (loaded.title, loaded.profile) == (TITLE, profile)
        # A metadata-only stub with the file's counts, like the prefix path's.
        assert getattr(loaded, "_loaded_metadata_only", False)
        assert (loaded.messages, loaded.tool_calls, loaded.anchor_activity_scenes) == ([], [], {})
        assert loaded.compact()["message_count"] == len(MESSAGES)
    assert calls == [(SID, False)] * 3
    assert _files(directory) == before


@pytest.mark.parametrize("layout", LAYOUTS)
def test_rewrite_that_keeps_the_signature_never_keeps_the_previous_owner(isolated_sessions, monkeypatch, layout):
    directory, _cache = isolated_sessions
    _same_signature_for_every_version(monkeypatch)
    path = directory / f"{SID}.json"
    path.write_text(_raw("work", "WORK TITLE", layout), encoding="utf-8")
    assert models.Session.load(SID).profile == "work"
    assert models.Session.load_metadata_only(SID).profile == "work"
    path.write_text(_raw("evil", "EVIL TITLE", layout), encoding="utf-8")
    loaded = models.Session.load_metadata_only(SID)
    assert (loaded.title, loaded.profile) == ("EVIL TITLE", "evil")
    _assert_work_never_sees(monkeypatch, "EVIL TITLE")


def test_symlink_swapped_there_and_back_reads_the_current_target(isolated_sessions, monkeypatch):
    directory, _cache = isolated_sessions
    _same_signature_for_every_version(monkeypatch)
    store = directory.parent / "store"
    store.mkdir()
    work, evil = store / "work.json", store / "evil.json"
    work.write_text(_raw("work", "WORK TITLE"), encoding="utf-8")
    evil.write_text(_raw("evil", "EVIL TITLE"), encoding="utf-8")
    path = directory / f"{SID}.json"

    def point_at(target):
        link = directory / ".swap"
        link.symlink_to(target)
        os.replace(link, path)

    point_at(work)
    assert models.Session.load(SID).profile == "work"
    for target, expected in ((evil, ("EVIL TITLE", "evil")), (work, ("WORK TITLE", "work"))):
        point_at(target)
        loaded = models.Session.load_metadata_only(SID)
        assert (loaded.title, loaded.profile) == expected
    point_at(evil)
    _assert_work_never_sees(monkeypatch, "EVIL TITLE")


def test_duplicate_partial_sidecar_is_not_rewritten_by_a_metadata_read(isolated_sessions, monkeypatch):
    """``Session.load`` collapses adjacent duplicate partials and saves with a ``.bak``.

    A metadata read, which can be another profile's sidebar poll, must keep that
    collapse in memory (#8834, #9007): the file and the directory stay as they were.
    """
    directory, _cache = isolated_sessions
    partial = {"role": "assistant", "content": "interrupted", "_partial": True}
    messages = [{"role": "user", "content": "hello", "timestamp": 1.5}, partial, dict(partial)]
    (directory / f"{SID}.json").write_text(_raw("evil", TITLE, "counted", messages), encoding="utf-8")
    before = _files(directory)
    loaded = models.Session.load_metadata_only(SID)
    assert loaded.profile == "evil"
    assert getattr(loaded, "_loaded_metadata_only", False) and loaded.messages == []
    _assert_work_never_sees(monkeypatch, TITLE)
    assert _files(directory) == before


@pytest.mark.parametrize("layout", LAYOUTS)
def test_malformed_body_is_a_miss_that_never_reaches_the_repairing_load(isolated_sessions, monkeypatch, layout):
    """The prefix parses but the body doesn't: no repairing fallback, no write, no title."""
    directory, _cache = isolated_sessions
    text = _raw("evil", TITLE, layout)
    (directory / f"{SID}.json").write_text(text[: text.index('"messages"') + 20], encoding="utf-8")
    before = _files(directory)
    calls = _count_full_loads(monkeypatch)
    assert models.Session.load_metadata_only(SID) is None
    assert calls == [(SID, False)]
    assert _files(directory) == before


def test_legacy_layout_count_and_scenes_move_with_the_owner(isolated_sessions, monkeypatch):
    """Pre-#5854 layout (scenes before the count): count, scenes and owner come from one read."""
    directory, _cache = isolated_sessions
    _same_signature_for_every_version(monkeypatch)
    path = directory / f"{SID}.json"

    def scene(name):
        return {name: {"version": 1, "updated_at": 1000.0, "scene": {"activity_rows": []}}}

    path.write_text(_raw("work", "WORK TITLE", "no_count", scenes=scene("scene-a")), encoding="utf-8")
    first = models.Session.load_metadata_only(SID)
    assert (first.profile, first.compact()["message_count"]) == ("work", len(MESSAGES))
    assert models._session_scene_keys(first) == {"scene-a"}
    more = MESSAGES + [{"role": "user", "content": "again", "timestamp": 1.7}]
    path.write_text(_raw("evil", "EVIL TITLE", "no_count", more, scene("scene-b")), encoding="utf-8")
    second = models.Session.load_metadata_only(SID)
    assert (second.title, second.profile, second.compact()["message_count"]) == ("EVIL TITLE", "evil", len(more))
    assert models._session_scene_keys(second) == {"scene-b"}
    _assert_work_never_sees(monkeypatch, "EVIL TITLE")


def test_file_replaced_after_the_prefix_read_is_read_whole_from_its_new_version(isolated_sessions, monkeypatch):
    """A file replaced between the prefix read and the full read never mixes versions.

    Version A names no owner (the root profile's session); version B is another
    profile's. The stub must not show A's title under B's owner (gate finding,
    Codex). The prefix only decides that the whole file is needed; the title and
    the owner both come from the full read.
    """
    directory, _cache = isolated_sessions
    path = directory / f"{SID}.json"
    version_a = {"session_id": SID, "title": "Root profile title", "created_at": 1.0, "updated_at": 2.0,
                 "messages": MESSAGES}
    version_b = {"session_id": SID, "title": "Work title", "created_at": 1.0, "updated_at": 3.0,
                 "messages": MESSAGES, "profile": "work"}
    path.write_text(json.dumps(version_a), encoding="utf-8")
    real_prefix = models._read_metadata_json_prefix
    swapped = []

    def _prefix_then_replace(p, *a, **k):
        prefix = real_prefix(p, *a, **k)
        if not swapped:
            swapped.append(True)
            path.write_text(json.dumps(version_b), encoding="utf-8")
            assert models.Session.load(SID).profile == "work"
        return prefix

    monkeypatch.setattr(models, "_read_metadata_json_prefix", _prefix_then_replace)
    stub = models.Session.load_metadata_only(SID)
    assert swapped
    assert (stub.title, stub.profile) == ("Work title", "work")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_stale_stream_cleanup_after_the_read_never_overwrites_a_newer_turn(isolated_sessions, monkeypatch, layout):
    """The read returns a stub, so stale-stream cleanup reloads the canonical session (#1558).

    Gate repro (Codex): ``?messages=0`` reads the session while the file still
    names a dead stream; a send then saves a new live stream and prompt under the
    session lock; the earlier request's cleanup runs on what it read. Given a
    detached full session it saved that older copy over the send, nulling the
    stream and the prompt. Given a stub it reloads the canonical session, whose
    stream is live, and writes nothing.
    """
    directory, cache = isolated_sessions
    path = directory / f"{SID}.json"
    head = {"session_id": SID, "title": TITLE, "workspace": str(directory), "created_at": 1.0, "updated_at": 2.0,
            "active_stream_id": "dead-stream"}
    if layout == "counted":
        head.update({"message_count": len(MESSAGES), "anchor_scene_index": {}})
    path.write_text(json.dumps({**head, "messages": MESSAGES, "profile": None}), encoding="utf-8")
    observed = models.get_session(SID, metadata_only=True)
    assert getattr(observed, "_loaded_metadata_only", False)
    assert observed.active_stream_id == "dead-stream"
    current = models.get_session(SID)
    monkeypatch.setitem(config.STREAMS, "new-live-stream", config.create_stream_channel())
    with config._get_session_agent_lock(SID):
        current.active_stream_id = "new-live-stream"
        current.pending_user_message = "accepted next prompt"
        current.pending_started_at = time.time()
        current.save(touch_updated_at=False)
    before = path.read_bytes()
    routes._clear_stale_stream_state(observed)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert (after["active_stream_id"], after["pending_user_message"]) == ("new-live-stream", "accepted next prompt")
    assert path.read_bytes() == before
    assert len(after["messages"]) == len(MESSAGES)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_stub_visible_count_leaves_out_hidden_rows(isolated_sessions, layout):
    """A stub's compact() trusts its stored visible count and never walks messages.

    The file has no ``visible_message_count`` key, so the stub takes it from the
    same read, leaving out hidden ``delegation_wakeup`` rows, as a full load's
    compact() does. Counting the raw messages would show the hidden row.
    """
    directory, _cache = isolated_sessions
    hidden = {"role": "user", "content": "wakeup", "timestamp": 1.7, "_source": "delegation_wakeup"}
    msgs = MESSAGES + [hidden]
    head = {"session_id": SID, "title": TITLE, "created_at": 1.0, "updated_at": 2.0}
    if layout == "counted":
        head.update({"message_count": len(msgs), "anchor_scene_index": {}})
    (directory / f"{SID}.json").write_text(json.dumps({**head, "messages": msgs, "profile": "work"}), encoding="utf-8")
    stub = models.Session.load_metadata_only(SID)
    assert getattr(stub, "_loaded_metadata_only", False) and stub.profile == "work"
    full = models.Session.load(SID).compact()
    assert full["visible_message_count"] == len(MESSAGES)
    compact = stub.compact()
    assert (compact["message_count"], compact["visible_message_count"]) == (len(msgs), len(MESSAGES))
