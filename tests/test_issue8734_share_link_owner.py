"""#8734: Share and Stop sharing refuse a link owned by another conversation.

Every share snapshot records the conversation that published it
(``source_session_id``). Share (create/refresh) and Stop sharing (revoke) refuse
ONLY when the link a session names belongs to a different conversation that
still exists and is a normal one, outside any compression chain. Every other
case keeps master's behaviour: an owner that no longer exists, a snapshot with
no owner, the session's own link, any compression chain (a pre-compression
snapshot or a continuation, with or without the continuation pointer older
chains lack), and unreadable or corrupted records.

Everything is driven through the real ``/api/share/*``, ``/api/session/clear``,
``/api/session/delete`` and public share handlers against real sidecar and
snapshot files in a temporary state dir.
"""

import io
import json
import secrets
import urllib.parse
from collections import OrderedDict

import pytest

OWNER = "s_8734_owner_conversation"
OTHER = "s_8734_other_conversation"
PARENT = "s_8734_chain_parent"
MIDDLE = "s_8734_chain_middle"
CHILD = "s_8734_chain_continuation"


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for ``handle_post``/``handle_get``."""

    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Sessions, the sidebar index and share snapshots inside tmp_path."""
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes
    import api.shares as shares

    sessions = OrderedDict()
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for module in (models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir)
        monkeypatch.setattr(module, "SESSIONS", sessions)
    index = session_dir / "_index.json"
    for module in (config, models, routes):
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", index)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid, **kw: {})
    monkeypatch.setattr(routes, "_DELETED_SESSION_IDS", set())
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)
    routes._session_list_cache_clear()
    yield tmp_path
    routes._session_list_cache_clear()


# ── helpers ───────────────────────────────────────────────────────────────────


def _save(sid, words, *, title=None, **fields):
    from api import models

    session = models.Session(session_id=sid, title=title or f"Title {sid}", **fields)
    session.messages = [
        {"role": "user", "content": f"{words} question", "timestamp": 100},
        {"role": "assistant", "content": f"{words} answer", "timestamp": 101},
    ]
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    return session.path


def _sidecar(sid):
    from api import models

    return models.SESSION_DIR / f"{sid}.json"


def _edit_sidecar(sid, **fields):
    """Rewrite top-level sidecar fields on disk, as a hand edit or a restore would."""
    from api import models

    path = _sidecar(sid)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")
    models.SESSIONS.clear()


def _pair(sid):
    payload = json.loads(_sidecar(sid).read_text(encoding="utf-8"))
    return payload.get("share_token"), payload.get("share_created_at")


def _post(path, body):
    from api import models
    from api.routes import handle_post

    handler = _FakeHandler(json.dumps(body).encode())
    handle_post(handler, urllib.parse.urlparse(path))
    models.SESSIONS.clear()
    return handler.status, handler.json_body()


def _share(sid):
    status, reply = _post("/api/share/create", {"session_id": sid})
    assert status == 200, reply
    return reply["share"]["token"]


def _snapshot_path(token):
    from api import shares

    return shares.SHARES_DIR / f"{token}.json"


def _snapshot(token):
    return json.loads(_snapshot_path(token).read_text(encoding="utf-8"))


def _public(token):
    from api.routes import handle_get

    handler = _FakeHandler()
    handle_get(handler, urllib.parse.urlparse(f"/api/share/{token}"))
    return handler.status, bytes(handler.body)


def _served_words(token):
    status, body = _public(token)
    assert status == 200, body
    return [m["content"] for m in json.loads(body)["share"]["messages"]]


def _assert_not_served(token):
    from api import shares

    assert shares.load_share(token) is None
    assert _public(token)[0] == 404


def _compress(parent, child, *, pointer=True):
    """The durable shape a compression leaves: the continuation inherits the
    session (share pair included) under a new id and names the parent; the parent
    becomes a pre-compression snapshot. ``pointer=False`` is the shape of chains
    compressed before exp-v0.52.492, whose parent names no continuation."""
    from api import models

    continuation = models.Session.load(parent)
    continuation.session_id = child
    continuation.parent_session_id = parent
    continuation.save(touch_updated_at=False)
    snapshot = models.Session.load(parent)
    snapshot.pre_compression_snapshot = True
    snapshot.pre_compression_continuation_session_id = child if pointer else None
    snapshot.save(touch_updated_at=False)
    models.SESSIONS.clear()


def _seed_other_with_owner_link():
    """OWNER shares; OTHER's sidecar names OWNER's live link (restored or edited)."""
    _save(OWNER, "owner words")
    _save(OTHER, "other words")
    token = _share(OWNER)
    _edit_sidecar(OTHER, share_token=token, share_created_at=1234.5)
    return token


# ── the refusal ───────────────────────────────────────────────────────────────


def test_share_refuses_a_link_owned_by_another_conversation(state):
    token = _seed_other_with_owner_link()
    before = _snapshot_path(token).read_bytes()
    owner_pair = _pair(OWNER)

    status, reply = _post("/api/share/create", {"session_id": OTHER})

    assert status == 409, reply
    assert "token" not in json.dumps(reply.get("share") or {})
    assert _snapshot_path(token).read_bytes() == before
    assert _served_words(token) == ["owner words question", "owner words answer"]
    assert _pair(OTHER) == (token, 1234.5)
    assert _pair(OWNER) == owner_pair


def test_stop_sharing_refuses_a_link_owned_by_another_conversation(state):
    token = _seed_other_with_owner_link()
    before = _snapshot_path(token).read_bytes()
    owner_pair = _pair(OWNER)

    status, reply = _post("/api/share/revoke", {"session_id": OTHER})

    assert status == 409, reply
    assert reply.get("ok") is not True
    assert _snapshot_path(token).read_bytes() == before
    assert _served_words(token) == ["owner words question", "owner words answer"]
    assert _pair(OTHER) == (token, 1234.5)
    assert _pair(OWNER) == owner_pair


def test_refusal_message_is_neutral(state):
    token = _seed_other_with_owner_link()
    for route in ("/api/share/create", "/api/share/revoke"):
        status, reply = _post(route, {"session_id": OTHER})
        assert status == 409, reply
        text = json.dumps(reply)
        assert token not in text
        assert OWNER not in text
        assert "another conversation" in text


def test_the_owner_still_refreshes_and_stops_sharing_after_a_refusal(state):
    token = _seed_other_with_owner_link()
    assert _post("/api/share/create", {"session_id": OTHER})[0] == 409
    assert _post("/api/share/revoke", {"session_id": OTHER})[0] == 409
    assert _share(OWNER) == token
    status, reply = _post("/api/share/revoke", {"session_id": OWNER})
    assert status == 200, reply
    _assert_not_served(token)


def test_module_refresh_refuses_a_link_owned_by_another_conversation(state):
    """``create_or_refresh_share`` reaches the snapshot writer without the route's
    liveness gate; the writer itself refuses."""
    from api import models, shares

    token = _seed_other_with_owner_link()
    before = _snapshot_path(token).read_bytes()
    with pytest.raises(shares.ShareLinkOwnedElsewhere):
        shares.create_or_refresh_share(models.Session.load(OTHER))
    with pytest.raises(shares.ShareLinkOwnedElsewhere):
        shares.revoke_share(models.Session.load(OTHER))
    assert _snapshot_path(token).read_bytes() == before


# ── master behaviour kept: no refusal outside the rule ────────────────────────


def test_pin_owner_that_no_longer_exists(state):
    token = _seed_other_with_owner_link()
    _sidecar(OWNER).unlink()
    status, reply = _post("/api/share/revoke", {"session_id": OTHER})
    assert status == 200, reply
    assert _pair(OTHER) == (None, None)
    _assert_not_served(token)
    _edit_sidecar(OTHER, share_token=token, share_created_at=1234.5)
    status, reply = _post("/api/share/create", {"session_id": OTHER})
    assert status == 200, reply
    assert reply["share"]["token"] != token  # master mints a fresh link here


@pytest.mark.parametrize("owner", ["", None])
def test_pin_snapshot_without_an_owner(state, owner):
    from api import shares

    token = _seed_other_with_owner_link()
    payload = _snapshot(token)
    payload["source_session_id"] = owner
    shares._write_json_atomic(_snapshot_path(token), payload)
    status, reply = _post("/api/share/revoke", {"session_id": OTHER})
    assert status == 200, reply
    assert _snapshot(token).get("revoked_at")
    assert _pair(OTHER) == (None, None)


def test_pin_owner_id_outside_the_session_store(state):
    """An owner id that is not a path-safe session id is never looked up, even when
    a file under that relative path names it; master's behaviour is kept."""
    from api import models, shares

    token = _seed_other_with_owner_link()
    outside = "../outside_8734"
    (models.SESSION_DIR / f"{outside}.json").write_text(json.dumps({"session_id": outside}), encoding="utf-8")
    payload = _snapshot(token)
    payload["source_session_id"] = outside
    shares._write_json_atomic(_snapshot_path(token), payload)
    status, reply = _post("/api/share/revoke", {"session_id": OTHER})
    assert status == 200, reply
    assert _pair(OTHER) == (None, None)
    _assert_not_served(token)


@pytest.mark.parametrize("damage", ["corrupt", "not_a_dict", "names_another_session"])
def test_pin_unreadable_owner_record(state, damage):
    token = _seed_other_with_owner_link()
    if damage == "corrupt":
        _sidecar(OWNER).write_text("{not json", encoding="utf-8")
    elif damage == "not_a_dict":
        _sidecar(OWNER).write_text("[]", encoding="utf-8")
    else:
        _edit_sidecar(OWNER, session_id="s_8734_somebody_else")
    status, reply = _post("/api/share/revoke", {"session_id": OTHER})
    assert status == 200, reply
    assert _pair(OTHER) == (None, None)
    _assert_not_served(token)


def test_pin_corrupted_snapshot_file(state):
    token = _seed_other_with_owner_link()
    _snapshot_path(token).write_text("{not json", encoding="utf-8")
    status, reply = _post("/api/share/revoke", {"session_id": OTHER})
    assert status == 200, reply
    assert _pair(OTHER) == (None, None)
    _assert_not_served(token)


def test_pin_already_revoked_link_named_by_another_conversation(state):
    token = _seed_other_with_owner_link()
    assert _post("/api/share/revoke", {"session_id": OWNER})[0] == 200
    _edit_sidecar(OTHER, share_token=token, share_created_at=1234.5)
    status, reply = _post("/api/share/revoke", {"session_id": OTHER})
    assert status == 200, reply
    assert _pair(OTHER) == (None, None)
    _assert_not_served(token)


def test_pin_module_revoke_without_a_session_id(state):
    """A caller that names only a token (no session id) revokes it, as on master."""
    from types import SimpleNamespace

    from api import shares

    token = _seed_other_with_owner_link()
    assert shares.revoke_share(SimpleNamespace(share_token=token)) is True
    _assert_not_served(token)


def test_pin_refreshing_the_own_link_keeps_the_token(state):
    """Pin 5: a refresh of the session's own link refreshes it, no new token."""
    from api import models

    _save(OWNER, "first words")
    token = _share(OWNER)
    created = _snapshot(token)["created_at"]
    session = models.Session.load(OWNER)
    session.messages.append({"role": "user", "content": "later words", "timestamp": 102})
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    assert _share(OWNER) == token
    assert _snapshot(token)["created_at"] == created
    assert "later words" in _served_words(token)
    assert _pair(OWNER)[0] == token


# ── compression chains keep master's behaviour ────────────────────────────────


@pytest.mark.parametrize("pointer", [True, False], ids=["chain", "chain_before_v0_52_492"])
def test_pin_stop_sharing_from_the_visible_continuation(state, pointer):
    """Pin 2: the parent published the link; the continuation closes it."""
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, CHILD, pointer=pointer)
    assert _pair(CHILD)[0] == token
    status, reply = _post("/api/share/revoke", {"session_id": CHILD})
    assert status == 200, reply
    assert _pair(CHILD) == (None, None)
    _assert_not_served(token)


@pytest.mark.parametrize("pointer", [True, False], ids=["chain", "chain_before_v0_52_492"])
def test_pin_refresh_from_the_continuation_keeps_the_inherited_link(state, pointer):
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, CHILD, pointer=pointer)
    assert _share(CHILD) == token
    assert _snapshot(token)["source_session_id"] == CHILD
    status, reply = _post("/api/share/revoke", {"session_id": PARENT})
    assert status == 200, reply  # the snapshot side of the chain also closes it, as on master
    _assert_not_served(token)


@pytest.mark.parametrize("pointer", [True, False], ids=["chain", "chain_before_v0_52_492"])
def test_pin_multi_hop_chain(state, pointer):
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, MIDDLE, pointer=pointer)
    _compress(MIDDLE, CHILD, pointer=pointer)
    assert _share(CHILD) == token
    _share(MIDDLE)  # the middle snapshot refreshes the inherited link, as on master
    status, reply = _post("/api/share/revoke", {"session_id": CHILD})
    assert status == 200, reply
    _assert_not_served(token)


@pytest.mark.parametrize("damaged", [PARENT, MIDDLE])
def test_pin_corrupted_intermediate_record(state, damaged):
    """Pin 3: a corrupted record inside the chain: no new refusal, no false success."""
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, MIDDLE)
    _compress(MIDDLE, CHILD)
    if damaged == MIDDLE:
        _share(MIDDLE)  # the middle record now owns the snapshot
    _sidecar(damaged).write_text("{not json", encoding="utf-8")
    status, reply = _post("/api/share/revoke", {"session_id": CHILD})
    assert status == 200, reply
    assert _pair(CHILD) == (None, None)
    _assert_not_served(token)


_MARKERS = ("pre_compression_snapshot", "pre_compression_continuation_session_id")


def _keep_only(sid, kept, continuation):
    """Leave ``sid``'s record with only the ``kept`` compression marker (or none)."""
    values = {"pre_compression_snapshot": True, "pre_compression_continuation_session_id": continuation}
    _edit_sidecar(sid, **{k: values[k] if k == kept else None for k in _MARKERS})


@pytest.mark.parametrize("kept", [*_MARKERS, None])
@pytest.mark.parametrize("acting", ["continuation", "parent"])
def test_pin_chain_record_with_a_damaged_marker(state, kept, acting):
    """Pin 3: a parent record that kept only one compression marker, or lost both,
    keeps master's behaviour from either side of the chain."""
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, CHILD)
    if acting == "parent":
        assert _share(CHILD) == token
        if kept is not None:  # clear detaches the continuation: only the parent's marker is left
            assert _post("/api/session/clear", {"session_id": CHILD})[0] == 200
    _keep_only(PARENT, kept, CHILD)
    sid = PARENT if acting == "parent" else CHILD
    status, reply = _post("/api/share/revoke", {"session_id": sid})
    assert status == 200, reply
    assert _pair(sid) == (None, None)
    _assert_not_served(token)


@pytest.mark.parametrize("kept", _MARKERS)
def test_pin_multi_hop_chain_whose_first_record_kept_one_marker(state, kept):
    """Pin 3 across two hops: the continuation does not name the damaged record."""
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, MIDDLE)
    _compress(MIDDLE, CHILD)
    _keep_only(PARENT, kept, MIDDLE)
    status, reply = _post("/api/share/revoke", {"session_id": CHILD})
    assert status == 200, reply
    assert _pair(CHILD) == (None, None)
    _assert_not_served(token)


def test_pin_clear_on_a_shared_conversation_then_stop_sharing(state):
    """Pin 1: clear keeps the pair; Stop sharing still closes the link."""
    _save(OWNER, "owner words")
    token = _share(OWNER)
    status, reply = _post("/api/session/clear", {"session_id": OWNER})
    assert status == 200, reply
    assert _pair(OWNER)[0] == token
    status, reply = _post("/api/share/revoke", {"session_id": OWNER})
    assert status == 200, reply
    assert _pair(OWNER) == (None, None)
    _assert_not_served(token)


@pytest.mark.parametrize("refreshed", [False, True], ids=["parent_owns", "continuation_owns"])
@pytest.mark.parametrize("pointer", [True, False], ids=["chain", "chain_before_v0_52_492"])
def test_pin_clear_on_a_compressed_continuation_then_stop_sharing(state, refreshed, pointer):
    """Pin 1 on a compressed chain: clear detaches the continuation from its parent;
    the link is still closed by Stop sharing from it, or from the parent snapshot."""
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, CHILD, pointer=pointer)
    if refreshed:
        assert _share(CHILD) == token
    status, reply = _post("/api/session/clear", {"session_id": CHILD})
    assert status == 200, reply
    assert _pair(CHILD)[0] == token
    status, reply = _post("/api/share/revoke", {"session_id": CHILD})
    assert status == 200, reply
    assert _pair(CHILD) == (None, None)
    _assert_not_served(token)


@pytest.mark.parametrize("pointer", [True, False], ids=["chain", "chain_before_v0_52_492"])
def test_pin_stop_sharing_from_the_parent_after_the_continuation_was_cleared(state, pointer):
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, CHILD, pointer=pointer)
    assert _share(CHILD) == token
    assert _post("/api/session/clear", {"session_id": CHILD})[0] == 200
    cleared = json.loads(_sidecar(CHILD).read_text(encoding="utf-8"))
    assert not cleared.get("parent_session_id")  # clear detached it: no lineage marker left
    status, reply = _post("/api/share/revoke", {"session_id": PARENT})
    assert status == 200, reply
    _assert_not_served(token)


# ── forks are separate conversations, not chain links ─────────────────────────


def _fork(source):
    """Fork ``source`` through the real branch route, which names it as parent."""
    status, reply = _post("/api/session/branch", {"session_id": source})
    assert status == 200, reply
    fork = reply["session_id"]
    record = json.loads(_sidecar(fork).read_text(encoding="utf-8"))
    assert record.get("parent_session_id") == source, "precondition: the fork names its source as parent"
    assert record.get("session_source") == "fork", "precondition: the fork is marked as a fork"
    return fork


def _assert_both_buttons_refused(acting, token, owner, words):
    before = _snapshot_path(token).read_bytes()
    owner_pair = _pair(owner)
    acting_pair = _pair(acting)
    for path in ("/api/share/create", "/api/share/revoke"):
        status, reply = _post(path, {"session_id": acting})
        assert status == 409, (path, reply)
    assert _snapshot_path(token).read_bytes() == before
    assert _snapshot(token)["source_session_id"] == owner
    assert _served_words(token) == [f"{words} question", f"{words} answer"]
    assert _pair(owner) == owner_pair
    assert _pair(acting) == acting_pair


def test_refuses_another_conversation_acting_on_a_forks_link(state):
    _save(OWNER, "owner words")
    _save(OTHER, "other words")
    fork = _fork(OWNER)
    token = _share(fork)
    _edit_sidecar(OTHER, share_token=token, share_created_at=1234.5)
    _assert_both_buttons_refused(OTHER, token, fork, "owner words")


def test_refuses_a_fork_acting_on_its_sources_link(state):
    _save(OWNER, "owner words")
    token = _share(OWNER)
    fork = _fork(OWNER)
    _edit_sidecar(fork, share_token=token, share_created_at=1234.5)
    _assert_both_buttons_refused(fork, token, OWNER, "owner words")


@pytest.mark.parametrize("source", ["fork", "webui"])
def test_refuses_a_conversation_whose_parent_is_a_third_conversation(state, source):
    token = _seed_other_with_owner_link()
    _edit_sidecar(OTHER, parent_session_id="s_8734_unrelated_conversation", session_source=source)
    _assert_both_buttons_refused(OTHER, token, OWNER, "owner words")


NUMERIC_OWNER = "873400000001"


def test_refuses_a_parent_that_names_the_owner_only_as_a_number(state):
    _save(NUMERIC_OWNER, "owner words")
    _save(OTHER, "other words")
    token = _share(NUMERIC_OWNER)
    _edit_sidecar(OTHER, share_token=token, share_created_at=1234.5, parent_session_id=int(NUMERIC_OWNER))
    _assert_both_buttons_refused(OTHER, token, NUMERIC_OWNER, "owner words")


def test_pin_stop_sharing_from_the_continuation_of_a_compressed_fork(state):
    _save(OWNER, "owner words")
    fork = _fork(OWNER)
    token = _share(fork)
    _compress(fork, CHILD)
    record = json.loads(_sidecar(CHILD).read_text(encoding="utf-8"))
    assert record.get("session_source") == "fork", "precondition: the continuation keeps the fork's source"
    assert record.get("parent_session_id") == fork
    status, reply = _post("/api/share/revoke", {"session_id": CHILD})
    assert status == 200, reply
    _assert_not_served(token)
    assert _pair(CHILD) == (None, None)


# ── deletes are untouched ─────────────────────────────────────────────────────


def test_pin_delete_a_never_shared_conversation(state):
    path = _save(OWNER, "owner words")
    status, reply = _post("/api/session/delete", {"session_id": OWNER})
    assert status == 200, reply
    assert not path.exists()


@pytest.mark.parametrize("target", [PARENT, CHILD])
def test_pin_delete_inside_a_never_shared_chain(state, target):
    _save(PARENT, "chain words")
    _compress(PARENT, CHILD)
    status, reply = _post("/api/session/delete", {"session_id": target})
    assert status == 200, reply
    assert not _sidecar(target).exists()


def test_pin_delete_the_parent_of_a_shared_chain_then_stop_sharing(state):
    _save(PARENT, "chain words")
    token = _share(PARENT)
    _compress(PARENT, CHILD)
    status, reply = _post("/api/session/delete", {"session_id": PARENT})
    assert status == 200, reply
    _assert_not_served(token)
    status, reply = _post("/api/share/revoke", {"session_id": CHILD})
    assert status == 200, reply
    assert _pair(CHILD) == (None, None)


def test_pin_delete_a_conversation_that_names_another_conversations_link(state):
    token = _seed_other_with_owner_link()
    status, reply = _post("/api/session/delete", {"session_id": OTHER})
    assert status == 200, reply
    assert _served_words(token) == ["owner words question", "owner words answer"]


def test_unknown_token_is_still_404(state):
    assert _public(secrets.token_urlsafe(18))[0] == 404
