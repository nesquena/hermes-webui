"""#7881 review findings: an accepted per-session reasoning-effort pick must
survive overlapping and stale ``Session.save()`` calls.

Contract kept from #7881: an object that changes its own ``reasoning_effort``
and saves writes it; an object that did NOT change it since it last
loaded/saved adopts the persisted value instead of writing its stale copy.
The ``/api/reasoning`` pick and its rollbacks do not save an object at all:
``set_session_reasoning_effort()`` writes only the effort against the latest
sidecar under the persistence lock (#8253 a1).

Covers the four findings left open on contributor head ``e71fd219``:

1. an overlapping writer (share create/revoke, no session lock) paused after
   its save read the persisted effort must not erase a pick persisted in that
   pause -- every save path (``skip_index=True`` included) and the pick share
   one reentrant per-session persistence lock;
2. a bounded-prefix miss (metadata over the 1 MiB prefix budget, e.g. a large
   pending prompt) is not an absent field: the full sidecar is parsed, and a
   sidecar that cannot be read at all fails the save closed;
3. an object that adopted the persisted value during a refused or failed save
   must not later write that value back over a newer pick;
4. a writer whose lock-held reload drops the CLI source identity the
   outside-resolved object carried must not persist it as a WebUI session
   (and the carry never overwrites an identity the reload already has);
5. (#8253 a1) a pick must neither erase fields another writer persisted after
   the pick's object was cached (a share token) nor be ignored when it equals
   a stale cached baseline.
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import threading
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHandler:
    def __init__(self, body: bytes = b"{}"):
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.command = "POST"

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def _safe_webui_print(self, *args, **kwargs):  # /api/session/draft's [SLOW] log
        pass


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    SESSIONS.clear()
    return session_dir


class _PerThreadPost:
    """Route ``read_body`` / ``j`` / ``bad`` per thread so two real handlers
    can run at once (or one nested inside another) without sharing state."""

    def __init__(self, monkeypatch):
        self.bodies: dict = {}
        self.results: dict = {}
        self._local = threading.local()
        monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
        monkeypatch.setattr(routes, "read_body", lambda handler: self._body())
        monkeypatch.setattr(routes, "j", self._j)
        monkeypatch.setattr(routes, "bad", self._bad)

    def _key(self):
        return getattr(self._local, "key", None) or threading.current_thread().name

    def _body(self):
        return self.bodies[self._key()]

    def _j(self, handler, payload, status=200, extra_headers=None, **_kw):
        self.results[self._key()] = (status, payload)
        return True

    def _bad(self, handler, msg, status=400, **_kw):
        self.results[self._key()] = (status, {"error": msg})
        return True

    def post(self, key, path, body):
        """Run ``handle_post`` for ``path`` under result key ``key``."""
        previous = getattr(self._local, "key", None)
        self._local.key = key
        self.bodies[key] = body
        try:
            routes.handle_post(_FakeHandler(b"{}"), SimpleNamespace(path=path))
        finally:
            self._local.key = previous
        return self.results.get(key)


def _stub_reasoning_profile_side_effects(monkeypatch):
    """The profile write and capability lookup are covered elsewhere."""
    monkeypatch.setattr(routes, "write_reasoning_effort", lambda effort, *_a: effort)
    monkeypatch.setattr(
        routes, "get_reasoning_status",
        lambda **kw: {"reasoning_effort": kw.get("effort_override")},
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value, **_kw: {})


def _pick(post, sid, effort, key="pick"):
    """Persist ``effort`` through the real ``/api/reasoning`` handler."""
    result = post.post(key, "/api/reasoning", {"effort": effort, "session_id": sid})
    assert result is not None and result[0] == 200, f"effort pick {effort!r} failed: {result}"


def _seed_low_session(tmp_path, sid, **extra):
    s = Session(
        session_id=sid,
        title="t",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        **extra,
    )
    s.save()
    s.reasoning_effort = "low"  # an own change: save() writes it
    s.save()
    assert Session.load(sid).reasoning_effort == "low"
    return s


def _cold_effort(sid):
    SESSIONS.clear()
    return Session.load(sid).reasoning_effort


# ---------------------------------------------------------------------------
# Finding 1 -- an overlapping save paused after its effort read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/share/create", "/api/share/revoke"])
def test_overlapping_share_save_does_not_erase_pick_persisted_in_its_pause(
    tmp_path, monkeypatch, path,
):
    """Codex repro (e71fd219 gate): pause a share create/revoke after its save
    read the persisted effort (Low), accept and persist XHigh, resume the
    share save. A cold reload must report XHigh."""
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "share-overlap-effort"
    seeded = _seed_low_session(tmp_path, sid)
    if path.endswith("revoke"):
        seeded.share_token = "tok"
        seeded.save()
    SESSIONS[sid] = seeded
    _stub_reasoning_profile_side_effects(monkeypatch)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda s: False)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "create_or_refresh_share", lambda s: {
        "share_token": "tok", "share_created_at": 1.0, "share_title": "t",
        "share_message_count": 2, "share_updated_at": 1.0,
    })
    monkeypatch.setattr(routes, "revoke_share", lambda s: None)
    post = _PerThreadPost(monkeypatch)

    paused = threading.Event()
    resume = threading.Event()
    real_dedupe = models._deduplicate_exact_stable_messages

    def _pause_share_save(messages):
        caller = sys._getframe(1).f_code.co_name
        if (
            threading.current_thread().name == "share"
            and caller in {"save", "_save_locked"}
            and not paused.is_set()
        ):
            # The save has already read the persisted effort (Low) by now.
            paused.set()
            assert resume.wait(10), "test never resumed the share save"
        return real_dedupe(messages)

    monkeypatch.setattr(models, "_deduplicate_exact_stable_messages", _pause_share_save)

    share = threading.Thread(
        target=post.post, args=("share", path, {"session_id": sid}), name="share",
    )
    share.start()
    assert paused.wait(10), "the share save never reached its payload construction"
    SESSIONS.pop(sid, None)  # count-cap eviction: the pick reloads its own copy
    pick = threading.Thread(
        target=post.post, args=("pick", "/api/reasoning", {"effort": "xhigh", "session_id": sid}),
        name="pick",
    )
    pick.start()
    pick.join(0.5)  # unserialized: the pick lands during the pause
    pick_waited_for_share_save = pick.is_alive()
    resume.set()
    share.join(10)
    pick.join(10)
    assert not share.is_alive() and not pick.is_alive(), "a writer deadlocked"
    assert post.results["share"][0] == 200, post.results["share"]
    assert post.results["pick"][0] == 200, post.results["pick"]

    assert _cold_effort(sid) == "xhigh", "the overlapping share save erased the accepted pick"
    assert pick_waited_for_share_save, (
        "the pick persisted while a save of the same session was between its "
        "effort read and its replacement"
    )
    # Not asserted: the share's own field. Under the kept #7881 contract the
    # pick saves its whole (cached) object, and that snapshot was resolved
    # before the unlocked share save landed -- the same lost-update class any
    # two full-object writers have; see NOTES residual risks.


def test_persistence_lock_is_shared_and_reentrant():
    a = models._session_persist_lock("same-sid")
    b = models._session_persist_lock("same-sid")
    assert a is b
    with a:
        with b:  # reentrant: the pick holds it across its own save()
            pass
    assert models._session_persist_lock("other-sid") is not a


@pytest.mark.parametrize("skip_index", [True, False])
def test_every_save_path_waits_for_the_persistence_lock(tmp_path, monkeypatch, skip_index):
    """skip_index=True saves take the same per-session lock as indexed ones."""
    _isolate_session_store(tmp_path, monkeypatch)
    sid = f"persist-lock-{skip_index}"
    s = Session(session_id=sid, title="t", workspace=str(tmp_path),
                messages=[{"role": "user", "content": "hi"}])
    s.save()
    done = threading.Event()

    def _save():
        s.title = "second"
        s.save(skip_index=skip_index)
        done.set()

    lock = models._session_persist_lock(sid)
    with lock:
        writer = threading.Thread(target=_save)
        writer.start()
        assert not done.wait(0.3), "a save ran while another holder owned the persistence lock"
    writer.join(10)
    assert done.is_set()
    assert Session.load(sid).title == "second"


# ---------------------------------------------------------------------------
# Finding 2 -- a bounded-prefix miss is not an absent field
# ---------------------------------------------------------------------------


_BIG_PROMPT = "p" * 1_100_000  # metadata alone exceeds the 1 MiB prefix budget


def test_cli_refresh_over_prefix_budget_keeps_pick(tmp_path, monkeypatch):
    """Codex repro (e71fd219 gate): with a 1,100,000-character pending prompt
    the bounded prefix cannot reach ``reasoning_effort``. CLI refresh loads a
    detached Low snapshot, XHigh is accepted, the refresh resumes and saves.
    The disk must stay XHigh."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cli-refresh-big-meta"
    _seed_low_session(tmp_path, sid, pending_user_message=_BIG_PROMPT)
    assert models._read_metadata_json_prefix(session_dir / f"{sid}.json") is None, (
        "precondition: the metadata must overflow the bounded prefix"
    )
    SESSIONS.clear()
    _stub_reasoning_profile_side_effects(monkeypatch)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    post = _PerThreadPost(monkeypatch)

    def _resolve_meta_and_pick(*_a, **_kw):
        # The refresh already holds its detached Session.load() snapshot.
        _pick(post, sid, "xhigh")
        SESSIONS.pop(sid, None)
        return {"session_id": sid, "source_tag": "cli", "raw_source": "cli", "session_source": "cli"}

    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", _resolve_meta_and_pick)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda *a, **kw: [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "from the CLI"},
    ])

    result = post.post("refresh", "/api/session/import_cli", {"session_id": sid})
    assert result is not None and result[0] == 200, result
    cold = Session.load(sid)
    assert len(cold.messages) == 3, "the refresh's own change must still be written"
    assert _cold_effort(sid) == "xhigh", "the CLI refresh wrote its stale effort over the pick"


def test_detached_snapshot_over_prefix_budget_keeps_pick_on_every_save_flag(
    tmp_path, monkeypatch,
):
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "big-meta-flags"
    _seed_low_session(tmp_path, sid, pending_user_message=_BIG_PROMPT)
    SESSIONS.clear()
    _stub_reasoning_profile_side_effects(monkeypatch)
    post = _PerThreadPost(monkeypatch)
    for touch in (True, False):
        for skip_index in (True, False):
            stale = Session.load(sid)
            effort = "xhigh" if stale.reasoning_effort != "xhigh" else "medium"
            _pick(post, sid, effort)
            SESSIONS.clear()
            stale.save(touch_updated_at=touch, skip_index=skip_index)
            assert _cold_effort(sid) == effort, (touch, skip_index)


def test_unreadable_sidecar_after_prefix_miss_fails_closed(tmp_path, monkeypatch):
    """A read miss is not an absence: when neither the prefix nor the full
    sidecar can be read, a save that did not change the effort refuses
    instead of writing its own (stale) copy over the pick."""
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "unreadable"
    _seed_low_session(tmp_path, sid, pending_user_message=_BIG_PROMPT)
    SESSIONS.clear()
    _stub_reasoning_profile_side_effects(monkeypatch)
    post = _PerThreadPost(monkeypatch)
    stale = Session.load(sid)  # detached Low snapshot
    _pick(post, sid, "xhigh")
    target = stale.path
    real_read_text = pathlib.Path.read_text

    def _denied(self, *a, **kw):
        if pathlib.Path(self) == target:
            raise PermissionError("denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(type(pathlib.Path()), "read_text", _denied)
    with pytest.raises(OSError):
        stale.save()
    monkeypatch.setattr(type(pathlib.Path()), "read_text", real_read_text)
    assert _cold_effort(sid) == "xhigh"


# ---------------------------------------------------------------------------
# Finding 3 -- an adopted value must never become the object's "own change"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["refused-empty-active-snapshot", "replace-raises"])
def test_adopted_effort_is_not_written_back_after_refused_or_failed_save(
    tmp_path, monkeypatch, failure,
):
    """Senior review (e71fd219): seed Low, pick XHigh, a stale save adopts
    XHigh but is refused (empty active snapshot) or fails (replace raises),
    pick Medium, the stale object saves again. Disk must end at Medium."""
    _isolate_session_store(tmp_path, monkeypatch)
    sid = f"adopt-{failure}"
    _seed_low_session(tmp_path, sid)
    SESSIONS.clear()
    _stub_reasoning_profile_side_effects(monkeypatch)
    post = _PerThreadPost(monkeypatch)

    stale = Session.load(sid)  # detached Low snapshot
    messages = list(stale.messages)
    _pick(post, sid, "xhigh")

    if failure == "refused-empty-active-snapshot":
        stale.messages = []
        stale.active_stream_id = "a" * 32
        stale.save()  # refused: would empty a populated sidecar mid-stream
        assert len(Session.load(sid).messages) == 2
        stale.messages = messages
        stale.active_stream_id = None
    else:
        real_replace = models._safe_replace
        calls = {"n": 0}

        def _fail_once(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(models, "_safe_replace", _fail_once)
        with pytest.raises(OSError):
            stale.save()
        monkeypatch.setattr(models, "_safe_replace", real_replace)

    assert stale.reasoning_effort == "xhigh", "the stale save adopted the persisted pick"
    _pick(post, sid, "medium")
    stale.title = "stale writer change"
    stale.save()

    assert _cold_effort(sid) == "medium", (
        "the stale object wrote back the value it adopted during its failed save"
    )
    assert Session.load(sid).title == "stale writer change"


# ---------------------------------------------------------------------------
# Finding 4 -- CLI source identity survives every lock-held reload
# ---------------------------------------------------------------------------


_CLI_IDENTITY = {
    "is_cli_session": True,
    "source_tag": "hermes-cli",
    "raw_source": "hermes-cli",
    "session_source": "cli",
    "source_label": "Hermes CLI",
}

# Writers that re-resolve under the session lock without their own CLI lookup.
# /api/session/draft is not one of them since master's draft lifecycle: it
# writes the dedicated .drafts/<sid>.json sidecar and never rewrites the
# session sidecar, so it cannot persist an identity at all. Its lock-held
# reload still carries the identity in memory, see
# tests/test_session_effort_master_compose.py::test_draft_lock_held_reload_carries_cli_source_identity.
_CLI_WRITERS = [
    ("/api/session/toolsets", {"toolsets": ["web"]}, lambda s: s.enabled_toolsets == ["web"]),
    ("/api/personality/set", {"name": ""}, lambda s: s.personality is None),
    ("/api/session/truncate", {"keep_count": 1}, lambda s: len(s.messages) == 1),
    ("/api/session/pin", {"pinned": True}, lambda s: s.pinned is True),
]


@pytest.mark.parametrize(
    "path,extra,check", _CLI_WRITERS, ids=["toolsets", "personality", "truncate", "pin"],
)
def test_lock_held_reload_keeps_cli_source_identity(tmp_path, monkeypatch, path, extra, check):
    """Greptile P1 (e71fd219, "CLI chats lose their source"): materialization
    saves the sidecar before stamping the CLI source fields on the cached
    object. If that object is evicted before a writer takes the session lock,
    the lock-held reload lacks the fields; the writer must not persist the
    chat as WebUI-native."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cli-identity-writer"
    baseline = Session(
        session_id=sid, title="cli title", workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        personality="old",
    )
    baseline.save()  # the import_cli_session save: default (WebUI) identity
    resident = Session.load(sid)
    for key, value in _CLI_IDENTITY.items():
        setattr(resident, key, value)  # _get_or_materialize_session's stamp
    SESSIONS[sid] = resident

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value, **_kw: {})
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda s: None, raising=False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "list_profiles_api", lambda: [])
    monkeypatch.setattr(routes, "all_sessions", lambda: [])
    import api.config as config
    monkeypatch.setattr(config, "_evict_session_agent", lambda *a, **kw: None)

    real_lock_factory = routes._get_session_agent_lock

    class _EvictBeforeLock:
        def __init__(self, inner):
            self._inner = inner

        def acquire(self, *a, **kw):
            SESSIONS.pop(sid, None)  # count-cap eviction in the window
            return self._inner.acquire(*a, **kw)

        def release(self):
            self._inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda value: _EvictBeforeLock(real_lock_factory(value)),
    )
    post = _PerThreadPost(monkeypatch)
    result = post.post("writer", path, {"session_id": sid, **extra})
    assert result is not None and result[0] == 200, result

    on_disk = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    for key, value in _CLI_IDENTITY.items():
        assert on_disk.get(key) == value, f"{path} dropped {key}: {on_disk.get(key)!r}"
    SESSIONS.clear()
    assert check(Session.load(sid)), f"{path} lost its own change"


def test_lock_held_reload_carry_fills_only_a_reload_without_identity(tmp_path, monkeypatch):
    """The carry fills a WebUI-default reload from the outside object, but
    never overwrites an identity the reload already has: a fork keeps
    ``session_source='fork'`` even if the outside copy says otherwise."""
    _isolate_session_store(tmp_path, monkeypatch)
    plain = Session(session_id="plain-identity", title="p", workspace=str(tmp_path),
                    messages=[{"role": "user", "content": "hi"}])
    plain.save()
    outside = Session.load("plain-identity")
    for key, value in _CLI_IDENTITY.items():
        setattr(outside, key, value)
    resolved = routes._resolve_session_under_lock("plain-identity", outside)
    assert resolved is not outside
    for key, value in _CLI_IDENTITY.items():
        assert getattr(resolved, key) == value, key

    fork = Session(session_id="fork-identity", title="f", workspace=str(tmp_path),
                   session_source="fork", messages=[{"role": "user", "content": "hi"}])
    fork.save()
    outside = Session.load("fork-identity")
    outside.session_source = "cli"
    outside.is_cli_session = True
    resolved = routes._resolve_session_under_lock("fork-identity", outside)
    assert resolved is not outside
    assert resolved.session_source == "fork"
    assert resolved.is_cli_session is False


# ---------------------------------------------------------------------------
# #8253 a1 -- the pick writes only the effort, against the latest sidecar
# ---------------------------------------------------------------------------


def test_pick_during_share_create_save_keeps_share_token_and_revoke_unpublishes(
    tmp_path, monkeypatch,
):
    """Codex repro (#8253 a1, CORE): pause a real share create inside its
    sidecar save, evict the cached session, submit a pick. Both return 200;
    the pick must not erase the share token the create persisted, so a later
    real revoke still finds it and the public share stops resolving."""
    import api.shares as shares

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    monkeypatch.setattr(shares, "SHARES_DIR", tmp_path / "shares")
    sid = "share-token-pick"
    seeded = _seed_low_session(tmp_path, sid)
    SESSIONS[sid] = seeded
    _stub_reasoning_profile_side_effects(monkeypatch)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda s: False)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **kw: None)
    post = _PerThreadPost(monkeypatch)

    paused = threading.Event()
    resume = threading.Event()
    real_dedupe = models._deduplicate_exact_stable_messages

    def _pause_share_save(messages):
        caller = sys._getframe(1).f_code.co_name
        if (
            threading.current_thread().name == "share"
            and caller in {"save", "_save_locked"}
            and not paused.is_set()
        ):
            paused.set()  # the share file exists; its token is not on disk yet
            assert resume.wait(10), "test never resumed the share save"
        return real_dedupe(messages)

    monkeypatch.setattr(models, "_deduplicate_exact_stable_messages", _pause_share_save)

    share = threading.Thread(
        target=post.post, args=("share", "/api/share/create", {"session_id": sid}), name="share",
    )
    share.start()
    assert paused.wait(10), "the share create never reached its sidecar save"
    SESSIONS.pop(sid, None)  # count-cap eviction
    pick = threading.Thread(
        target=post.post, args=("pick", "/api/reasoning", {"effort": "xhigh", "session_id": sid}),
        name="pick",
    )
    pick.start()
    pick.join(0.5)
    resume.set()
    share.join(10)
    pick.join(10)
    assert not share.is_alive() and not pick.is_alive(), "a writer deadlocked"
    assert post.results["share"][0] == 200, post.results["share"]
    assert post.results["pick"][0] == 200, post.results["pick"]
    token = post.results["share"][1]["share"]["token"]
    assert shares.load_share(token) is not None, "precondition: the share is public"

    on_disk = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert on_disk["reasoning_effort"] == "xhigh"
    assert on_disk.get("share_token") == token, (
        f"the effort pick erased the share token the create persisted: {on_disk.get('share_token')!r}"
    )

    SESSIONS.clear()  # eviction/restart: the revoke resolves the persisted sidecar
    # The cold cache isolates the pick's own contract. A revoke served from a
    # copy cached by ANY read during the share-save window (a plain
    # get_session() included, no pick involved) also misses the token: the
    # share handlers' cache coherence, unchanged from master and out of scope.
    revoked = post.post("revoke", "/api/share/revoke", {"session_id": sid})
    assert revoked is not None and revoked[0] == 200, revoked
    assert shares.load_share(token) is None, "revoke returned 200 but the share is still public"
    on_disk = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert on_disk.get("share_token") is None
    assert _cold_effort(sid) == "xhigh"


def test_explicit_pick_equal_to_stale_cached_baseline_is_written(tmp_path, monkeypatch):
    """Codex repro (#8253 a1, SILENT): a delayed read republishes a cached Low
    copy after XHigh reached disk. Selecting Low must persist Low -- not answer
    200/Low while save() treats Low == baseline as unchanged and adopts XHigh."""
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "pick-equals-stale-baseline"
    _seed_low_session(tmp_path, sid)
    SESSIONS.clear()
    _stub_reasoning_profile_side_effects(monkeypatch)
    post = _PerThreadPost(monkeypatch)

    delayed = Session.load(sid)  # a read that loaded Low before the pick
    _pick(post, sid, "xhigh")
    assert Session.load(sid).reasoning_effort == "xhigh"
    with models.LOCK:
        SESSIONS[sid] = delayed  # ...and publishes its Low copy afterwards

    status, payload = post.post("low", "/api/reasoning", {"effort": "low", "session_id": sid})
    assert status == 200 and payload.get("reasoning_effort") == "low", (status, payload)
    assert _cold_effort(sid) == "low", "the explicit Low pick was answered 200 but not persisted"
