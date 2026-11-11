"""#8855: a refused cross-profile upload never loads, rewrites or caches the session.

POST /api/upload, /api/upload/extract and /api/workspace/upload take the session
id from a multipart form field, so the generic request-session guard never sees
it and each handler owns its profile boundary. They used to run ``get_session``
first and refuse (404) on the loaded object. ``Session.load`` collapses adjacent
duplicate ``_partial`` rows and saves (with a ``.bak``), and ``get_session``
caches what it loads, so a refused upload still rewrote and cached another
profile's sidecar. #8094 set the rule (applied to the session routes by #8834):
a refused request never writes.

The handlers are called directly with the multipart reader stubbed, as the
issue's repro did. The #8094 fixture is #8834's: a legacy sidecar holding
[user, partial, partial].
"""
from __future__ import annotations

import collections
import io
import json
import zipfile

import pytest

import api.config as config
import api.models as models
import api.upload as upload

SID = "legacy-8855"
PARTIAL = {"role": "assistant", "content": "interrupted", "_partial": True}
NOT_FOUND = ({"error": "Session not found"}, 404)

HANDLERS = {
    "upload": upload.handle_upload,
    "upload_extract": upload.handle_upload_extract,
    "workspace_upload": upload.handle_workspace_upload,
}


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("inside.txt", "inside")
    return buf.getvalue()


FILES = {
    "upload": ("note.txt", b"note"),
    "upload_extract": ("archive.zip", _zip_bytes()),
    "workspace_upload": ("note.txt", b"note"),
}


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    cache = collections.OrderedDict()
    for module in (config, models):
        monkeypatch.setattr(module, "SESSION_DIR", directory)
        monkeypatch.setattr(module, "SESSIONS", cache)
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", directory / "_index.json")
    attachments = tmp_path / "attachments"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(upload, "_attachment_root", lambda: attachments.resolve())
    monkeypatch.setattr(upload, "resolve_trusted_workspace", lambda *_a, **_k: workspace.resolve())
    return directory, cache, attachments, workspace


def _sidecar(directory, *, profile="work", layout="messages_first", messages=None, body_sid=SID, **extra):
    messages = [{"role": "user", "content": "hello"}, PARTIAL, dict(PARTIAL)] if messages is None else messages
    metadata = {"session_id": body_sid, "title": "Legacy", "created_at": 1.0, "updated_at": 2.0,
                "workspace": "/ignored-8855", **extra}
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
        self.headers = {"Content-Type": "multipart/form-data; boundary=x8855", "Content-Length": "1"}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.path = "/"
        self.client_address = ("127.0.0.1", 1)

    def send_response(self, status):
        self.status = status

    def send_header(self, *_a):
        pass

    def end_headers(self):
        pass


def _call(route, monkeypatch, *, active, sid=SID):
    monkeypatch.setattr(upload, "_get_active_profile_name", lambda: active)
    monkeypatch.setattr(
        upload, "parse_multipart",
        lambda *_a, **_k: ({"session_id": sid, "path": ""}, {"file": FILES[route]}),
    )
    seen = {}

    def _j(_handler, obj, *_a, **kwargs):
        seen["response"] = (obj, kwargs.get("status", 200))
        return True

    monkeypatch.setattr(upload, "j", _j)
    HANDLERS[route](_Handler())
    return seen["response"]


def _assert_untouched(path, before, cache, attachments, workspace):
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert SID not in cache
    assert not attachments.exists()
    assert list(workspace.iterdir()) == []


ROUTES = ["upload", "upload_extract", "workspace_upload"]


@pytest.mark.parametrize("layout", ["messages_first", "scenes_first"])
@pytest.mark.parametrize("owner,active", [("work", "default"), (None, "work")], ids=["other_profile", "legacy_none"])
@pytest.mark.parametrize("route", ROUTES)
def test_refused_upload_leaves_sidecar_backup_and_cache_untouched(
    isolated_sessions, monkeypatch, route, owner, active, layout,
):
    directory, cache, attachments, workspace = isolated_sessions
    path = _sidecar(directory, profile=owner, layout=layout)
    before = path.read_bytes()
    assert _call(route, monkeypatch, active=active) == NOT_FOUND
    _assert_untouched(path, before, cache, attachments, workspace)


@pytest.mark.parametrize("route", ROUTES)
def test_refused_upload_never_loads_the_session(isolated_sessions, monkeypatch, route):
    """The refusal is decided before ``get_session``: the loader is never reached."""
    directory, cache, attachments, workspace = isolated_sessions
    path = _sidecar(directory, profile="work")
    before = path.read_bytes()

    def _no_load(*_a, **_k):
        raise AssertionError("a refused upload must not load the session")

    monkeypatch.setattr(upload, "get_session", _no_load)
    assert _call(route, monkeypatch, active="default") == NOT_FOUND
    _assert_untouched(path, before, cache, attachments, workspace)


@pytest.mark.parametrize("route", ROUTES)
def test_resident_foreign_copy_is_refused_without_promotion(isolated_sessions, monkeypatch, route):
    """A foreign session already resident is refused from the readings, cache order intact."""
    directory, cache, attachments, workspace = isolated_sessions
    path = _sidecar(directory, profile="work", messages=[{"role": "user", "content": "hello"}])
    before = path.read_bytes()
    cache[SID] = models.Session(session_id=SID, profile="work", messages=[{"role": "user", "content": "hello"}])
    cache["newer-8855"] = models.Session(session_id="newer-8855", profile="default")
    order = list(cache)
    assert _call(route, monkeypatch, active="default") == NOT_FOUND
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert list(cache) == order
    assert not attachments.exists()
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("route", ROUTES)
def test_unverifiable_owner_fails_closed_and_writes_nothing(isolated_sessions, monkeypatch, route):
    """#8834's fail-closed rule: a sidecar whose body names another session must not reach Session.load,
    whose collapse save would write the file that body names (#8852)."""
    directory, cache, attachments, workspace = isolated_sessions
    path = _sidecar(directory, profile="default", body_sid="other-8855")
    before = path.read_bytes()
    other = directory / "other-8855.json"
    other.write_text(json.dumps({"session_id": "other-8855", "profile": "work",
                                 "messages": [{"role": "user", "content": f"turn {i}"} for i in range(4)]}))
    other_before = other.read_bytes()
    assert _call(route, monkeypatch, active="default") == NOT_FOUND
    assert path.read_bytes() == before
    assert other.read_bytes() == other_before
    assert not path.with_suffix(".json.bak").exists()
    assert not other.with_suffix(".json.bak").exists()
    assert SID not in cache and "other-8855" not in cache


@pytest.mark.parametrize("owner,active", [("work", "work"), (None, "default")], ids=["named_owner", "legacy_root"])
@pytest.mark.parametrize("route", ROUTES)
def test_owner_upload_still_loads_repairs_and_succeeds(isolated_sessions, monkeypatch, route, owner, active):
    """Positive control: the owner's upload still runs the load (collapse + .bak, cached) and succeeds as before."""
    directory, cache, attachments, workspace = isolated_sessions
    path = _sidecar(directory, profile=owner)
    obj, status = _call(route, monkeypatch, active=active)
    assert status == 200, obj
    assert len(json.loads(path.read_bytes())["messages"]) == 2
    assert path.with_suffix(".json.bak").exists()
    assert SID in cache
    if route == "upload":
        assert obj["filename"] == "note.txt"
        assert (attachments / SID / "note.txt").read_bytes() == b"note"
    elif route == "upload_extract":
        assert obj["ok"] is True and obj["extracted"] == 1
        assert (attachments / SID / "archive" / "inside.txt").read_text() == "inside"
    else:
        assert obj["filename"] == "note.txt" and obj["extracted"] is False
        assert (workspace / "note.txt").read_bytes() == b"note"


@pytest.mark.parametrize("route", ROUTES)
def test_owner_resident_copy_beside_a_foreign_sidecar_is_left_to_the_load(isolated_sessions, monkeypatch, route):
    """Any visible copy defers to the load, exactly as before (the post-load check decides)."""
    directory, cache, _attachments, _workspace = isolated_sessions
    _sidecar(directory, profile="work", messages=[{"role": "user", "content": "hello"}])
    cache[SID] = models.Session(session_id=SID, profile="default", messages=[{"role": "user", "content": "hello"}])
    calls = []
    real = upload.get_session

    def _spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(upload, "get_session", _spy)
    _obj, status = _call(route, monkeypatch, active="default")
    assert calls == [(SID,)]
    assert status == 200


@pytest.mark.parametrize("sid,expected", [
    ("missing-8855", {"upload": NOT_FOUND, "upload_extract": NOT_FOUND, "workspace_upload": NOT_FOUND}),
    ("", {"upload": NOT_FOUND, "upload_extract": NOT_FOUND,
          "workspace_upload": ({"error": "Missing session_id"}, 400)}),
    ("../escape", {"upload": NOT_FOUND, "upload_extract": NOT_FOUND, "workspace_upload": NOT_FOUND}),
], ids=["unknown", "empty", "unsafe"])
@pytest.mark.parametrize("route", ROUTES)
def test_missing_session_keeps_masters_response(isolated_sessions, monkeypatch, route, sid, expected):
    directory, cache, attachments, workspace = isolated_sessions
    assert _call(route, monkeypatch, active="default", sid=sid) == expected[route]
    assert list(directory.iterdir()) == []
    assert not cache
    assert not attachments.exists()
    assert list(workspace.iterdir()) == []
