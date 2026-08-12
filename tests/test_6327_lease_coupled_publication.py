"""#6327 — canonical publication is 100% lease-coupled (review 15, blocker 1).

Every canonical same-SID cache replacement/removal (refresh, deletion,
eviction, cold-load install, compression rotation) must route through the
per-SID publication primitives so the owner-generation lease authority is
held ACROSS the exact ``SESSIONS`` write:

- ``_publish_owner_lease`` (blocking, lease → LOCK) for replacements;
- ``_publish_owner_removal_lease`` (non-blocking, LOCK already held) for
  removals — a contended lease (in-flight sink) skips the removal instead of
  inverting the lock order;
- the cold-load publisher re-checks current ownership BEFORE installing the
  loaded object, so an owner published while ``Session.load()`` ran is never
  overwritten.

These are deterministic barrier tests (Events, never sleeps, except one
bounded block-while-held check): refresh, deletion, eviction/cold-load
replacement, and compression publication all invalidate claims installed
under the previous lease.
"""
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from api import models


@pytest.fixture
def isolated_session_env():
    """Isolate all SESSIONS-cache global state onto a throwaway temp dir.

    Mirrors tests/test_issue4765_sessions_lru_eviction.py's fixture:
    ``api.models`` imports ``SESSION_DIR`` / ``SESSION_INDEX_FILE`` at module
    load, so both ``api.config`` and ``api.models`` copies must be
    redirected.  Everything is restored on teardown (even on exception).
    """
    import collections

    from api import config as _cfg

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    old = {
        "cfg_SESSION_DIR": _cfg.SESSION_DIR,
        "models_SESSION_DIR": getattr(models, "SESSION_DIR", None),
        "cfg_SESSION_INDEX_FILE": _cfg.SESSION_INDEX_FILE,
        "models_SESSION_INDEX_FILE": getattr(models, "SESSION_INDEX_FILE", None),
        "SESSIONS": _cfg.SESSIONS,
        "LOCK": _cfg.LOCK,
        "SESSIONS_MAX": _cfg.SESSIONS_MAX,
        "cfg": getattr(_cfg, "cfg", None),
    }

    index_file = sessions_dir / "_index.json"
    _cfg.SESSION_DIR = sessions_dir
    models.SESSION_DIR = sessions_dir
    _cfg.SESSION_INDEX_FILE = index_file
    models.SESSION_INDEX_FILE = index_file
    _cfg.LOCK = threading.Lock()
    models.LOCK = _cfg.LOCK
    _cfg.SESSIONS = collections.OrderedDict()
    models.SESSIONS = _cfg.SESSIONS

    try:
        yield sessions_dir
    finally:
        _cfg.SESSION_DIR = old["cfg_SESSION_DIR"]
        if old["models_SESSION_DIR"] is not None:
            models.SESSION_DIR = old["models_SESSION_DIR"]
        _cfg.SESSION_INDEX_FILE = old["cfg_SESSION_INDEX_FILE"]
        if old["models_SESSION_INDEX_FILE"] is not None:
            models.SESSION_INDEX_FILE = old["models_SESSION_INDEX_FILE"]
        _cfg.SESSIONS = old["SESSIONS"]
        models.SESSIONS = old["SESSIONS"]
        _cfg.LOCK = old["LOCK"]
        models.LOCK = old["LOCK"]
        _cfg.SESSIONS_MAX = old["SESSIONS_MAX"]
        if old["cfg"] is not None:
            _cfg.cfg = old["cfg"]
        shutil.rmtree(tmpdir, ignore_errors=True)


class _FakeSession:
    """Minimal Session stand-in carrying the attrs the publication paths read."""

    def __init__(self, session_id, *, created_at=None, messages=None):
        self.session_id = session_id
        self.title = "Lease-coupled publication"
        self.workspace = "/workspace"
        self.model = "test-model"
        self.model_provider = None
        self.profile = None
        self.personality = None
        self.messages = list(messages or [])
        self.context_messages = []
        self.tool_calls = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.llm_title_generated = True
        self.composer_draft = None
        self._loaded_metadata_only = False
        self.created_at = created_at if created_at is not None else time.time()


# ─────────────────────────────────────────────────────────────────────────────
# 1) Cold-load publication re-checks ownership before installing
# ─────────────────────────────────────────────────────────────────────────────


def test_cold_load_never_overwrites_owner_installed_during_load(
    isolated_session_env, monkeypatch
):
    """A canonical owner published while ``Session.load()`` runs must never be
    overwritten by the cold-load install: the cold-load publisher re-checks
    ``SESSIONS`` under LOCK immediately before installing."""
    from api import models as _models

    sid = "sess-cold-load-recheck"
    replacement = _FakeSession(sid)
    load_started = threading.Event()
    release_load = threading.Event()
    results = []

    def _blocking_load(loaded_sid):
        load_started.set()
        assert release_load.wait(10), "cold-load Session.load never released"
        return _FakeSession(loaded_sid)

    monkeypatch.setattr(_models.Session, "load", lambda loaded_sid: _blocking_load(loaded_sid))
    monkeypatch.setattr(_models, "_sync_sidecar_from_state_db_if_newer", lambda s: False)
    monkeypatch.setattr(_models, "_repair_stale_pending", lambda s: False)
    monkeypatch.setattr(_models, "_session_has_pending_journal_retry", lambda s: False)

    def _cold_load():
        try:
            results.append(_models._resolve_session(sid))
        except Exception as exc:  # pragma: no cover - failure surfaced below
            results.append(exc)

    thread = threading.Thread(target=_cold_load, daemon=True)
    thread.start()
    assert load_started.wait(10), "cold-load never entered Session.load"

    # While the load is blocked, a canonical owner is published under the SAME
    # SID (replacement / refresh from another thread).
    with _models.LOCK:
        _models.SESSIONS[sid] = replacement

    release_load.set()
    thread.join(10)
    assert not thread.is_alive(), "cold-load deadlocked"

    with _models.LOCK:
        assert _models.SESSIONS.get(sid) is replacement, (
            "cold-load overwrote an owner installed while Session.load() ran"
        )
    assert len(results) == 1 and not isinstance(results[0], Exception), results


# ─────────────────────────────────────────────────────────────────────────────
# 2) Eviction / cold-load removal skips a session with an in-flight sink
# ─────────────────────────────────────────────────────────────────────────────


def test_eviction_skips_inflight_sink_session_and_bumps_lease_on_removal(
    isolated_session_env,
):
    """``_evict_sessions_over_cap`` routes the canonical removal through the
    per-SID removal lease: while the per-session lease is held (in-flight
    sink), the session is SKIPPED (never evicted, never blocked — LOCK is
    already held); once the sink releases, the removal bumps the lease and
    evicts."""
    from api.routes import _read_owner_lease, _session_owner_lease

    sid_locked = "sess-evict-sink"
    sid_clean = "sess-evict-clean"
    with models.LOCK:
        models.SESSIONS[sid_locked] = _FakeSession(
            sid_locked, created_at=time.time() - 10_000
        )
        models.SESSIONS[sid_clean] = _FakeSession(
            sid_clean, created_at=time.time() - 10_000
        )

    # Simulate an in-flight sink holding the per-session lease lock.
    lease_lock, _ = _session_owner_lease(sid_locked)
    with lease_lock:
        with models.LOCK:
            evicted = models._evict_sessions_over_cap(cap=1)
        assert evicted == 1, "the clean entry must still be evicted"
        with models.LOCK:
            assert models.SESSIONS.get(sid_locked) is not None, (
                "a session with an in-flight sink must never be evicted"
            )
            assert sid_clean not in models.SESSIONS

    # Sink released: the locked session is now evictable and its removal
    # bumps the owner-generation lease (claims under the old lease refused).
    lease_before = _read_owner_lease(sid_locked)
    with models.LOCK:
        models.SESSIONS[sid_clean] = _FakeSession(
            sid_clean, created_at=time.time() - 10_000
        )
        evicted = models._evict_sessions_over_cap(cap=1)
    assert evicted == 1
    with models.LOCK:
        assert sid_locked not in models.SESSIONS
    assert _read_owner_lease(sid_locked) != lease_before, (
        "eviction removal must bump the per-SID owner-generation lease"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3) Every canonical publication bumps the lease → installed claims refused
# ─────────────────────────────────────────────────────────────────────────────


def test_canonical_publication_bump_invalidates_installed_sink_claim(
    isolated_session_env, monkeypatch
):
    """A sink claim installed under lease L1 is accepted; after ANY canonical
    publication (refresh / deletion / compression rotation all use
    ``_publish_owner_lease``) bumps the lease to L2, the same claim is
    REFUSED before its sink body runs."""
    import api.routes as routes
    from api.routes import (
        _SinkClaimRefused,
        _install_worker_sink_claim,
        _publish_owner_lease,
        _worker_sink_claim_guard,
    )

    sid = "sess-claim-invalidation"
    session = _FakeSession(sid)
    stream_id = "stream-claim"
    owner_token = {
        "owner": session,
        "credential_state_fingerprint": "fp-1",
        "profile_home": "/home/test/.hermes",
        "workspace": "/workspace",
        "model": "test-model",
        "provider": "test-provider",
        "normalized_model": False,
    }
    with models.LOCK:
        models.SESSIONS[sid] = session

    # The sink guard re-resolves the canonical owner and compares every token
    # field — stub both to keep the ONLY refusal driver the lease compare.
    monkeypatch.setattr(
        routes, "get_session", lambda live_sid: session if str(live_sid) == sid else None
    )
    monkeypatch.setattr(routes, "_process_wakeup_owner_token_mismatch", lambda t, o: None)

    sink_calls = []

    def _sink():
        sink_calls.append(stream_id)
        return "ok"

    # Success control: a claim installed under the CURRENT lease is accepted
    # and its sink runs.
    _install_worker_sink_claim(owner_token, session, stream_id)
    claim = routes._worker_sink_claim_for(sid, stream_id)
    with _worker_sink_claim_guard(claim, owner_token, session, stream_id):
        sink_calls.append("accepted")
    assert sink_calls == ["accepted"], sink_calls
    sink_calls.clear()

    # Re-install the claim (lease unchanged) — then a canonical publication
    # (refresh / deletion / compression) bumps the lease to L2.
    _install_worker_sink_claim(owner_token, session, stream_id)
    claim = routes._worker_sink_claim_for(sid, stream_id)
    with _publish_owner_lease(sid):
        pass  # the exact SESSIONS write a publisher performs in its body

    # The same claim is now refused BEFORE the sink body runs.
    with pytest.raises(_SinkClaimRefused) as excinfo:
        with _worker_sink_claim_guard(claim, owner_token, session, stream_id):
            sink_calls.append("sink-must-not-run")
    assert excinfo.value.reason == "claim_invalidated", excinfo.value
    assert sink_calls == [], (
        "a claim installed under the previous lease must never reach its sink"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4) Publication lease serializes with an in-flight sink (blocking path)
# ─────────────────────────────────────────────────────────────────────────────


def test_publication_lease_blocks_until_inflight_sink_releases(
    isolated_session_env,
):
    """The blocking publication primitive holds the per-session lease ACROSS
    the exact SESSIONS write: while a sink holds the lease, the publisher
    cannot complete; after the sink releases, the publication completes and
    the lease moved."""
    from api.routes import _publish_owner_lease, _read_owner_lease, _session_owner_lease

    sid = "sess-pub-serialize"
    lease_lock, _ = _session_owner_lease(sid)
    lease_before = _read_owner_lease(sid)
    published = {}
    done = threading.Event()

    def _publisher():
        # The context manager yields the freshly minted lease token (reading
        # it via _read_owner_lease inside the body would self-deadlock on the
        # non-reentrant per-session lease lock).
        with _publish_owner_lease(sid) as token:
            published["token"] = token
        done.set()

    with lease_lock:  # simulated in-flight sink
        thread = threading.Thread(target=_publisher, daemon=True)
        thread.start()
        # Bounded block-while-held check: the publisher MUST wait for the
        # sink's lease (it cannot complete while the sink is in flight).
        time.sleep(0.3)
        assert not done.is_set(), (
            "a publication must serialize with an in-flight sink for the same session"
        )
        assert published == {}
    # Sink released → publication proceeds and bumps the lease.
    assert done.wait(10), "publication deadlocked behind the released sink"
    thread.join(10)
    assert published["token"] != lease_before, (
        "publication must bump the per-SID owner-generation lease"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5) The publication contextmanager propagates body exceptions (single yield)
# ─────────────────────────────────────────────────────────────────────────────


def test_publication_lease_propagates_body_exception(isolated_session_env):
    """#6327 review 15: ``_publish_owner_publication_lease`` selects its
    fallback BEFORE the single yield — an exception thrown through the yield
    (a publication-body failure) propagates to the caller instead of
    becoming ``RuntimeError: generator didn't stop after throw``."""
    from api.models import _publish_owner_publication_lease

    with pytest.raises(RuntimeError, match="publication-body boom"):
        with _publish_owner_publication_lease("sess-body-exc"):
            raise RuntimeError("publication-body boom")


# ─────────────────────────────────────────────────────────────────────────────
# 6) Review 16 blocker 2: refresh publication CAS (disk I/O outside authority)
# ─────────────────────────────────────────────────────────────────────────────


def test_cache_refresh_cas_never_overwrites_newer_owner_installed_during_io(
    isolated_session_env, monkeypatch
):
    """The refresh performs ``Session.load()`` OUTSIDE the per-SID authority,
    so a newer canonical owner installed while the disk I/O was in flight must
    never be overwritten by the refresh publication — and must be RETURNED.
    The publication CAS-succeeds only while the refreshed-from owner is still
    canonical; a CAS-fail must not bump the winner's lease either."""
    from api import models as _models
    from api.routes import _read_owner_lease

    sid = "sess-refresh-cas"
    stale = _FakeSession(sid)  # the cached owner the refresh starts from
    replacement = _FakeSession(sid)  # newer canonical owner installed mid-I/O
    with _models.LOCK:
        _models.SESSIONS[sid] = stale
    lease_before = _read_owner_lease(sid)

    load_started = threading.Event()
    release_load = threading.Event()

    def _blocking_load(loaded_sid):
        load_started.set()
        assert release_load.wait(10), "refresh Session.load never released"
        return _FakeSession(loaded_sid)  # the "disk" version

    monkeypatch.setattr(_models, "_cached_session_lags_disk", lambda cached: True)
    monkeypatch.setattr(_models, "_inactive_cache_tail_needs_disk_check", lambda cached: False)
    monkeypatch.setattr(_models, "_session_has_pending_journal_retry", lambda s: False)
    monkeypatch.setattr(_models, "_sync_sidecar_from_state_db_if_newer", lambda s: False)
    monkeypatch.setattr(_models.Session, "load", lambda loaded_sid: _blocking_load(loaded_sid))

    results = []

    def _refresh():
        try:
            results.append(_models._resolve_session(sid))
        except Exception as exc:  # pragma: no cover - failure surfaced below
            results.append(exc)

    thread = threading.Thread(target=_refresh, daemon=True)
    thread.start()
    assert load_started.wait(10), "refresh never entered Session.load"

    # A newer canonical owner is published while the refresh I/O is in flight.
    with _models.LOCK:
        _models.SESSIONS[sid] = replacement

    release_load.set()
    thread.join(10)
    assert not thread.is_alive(), "refresh CAS deadlocked"

    with _models.LOCK:
        assert _models.SESSIONS.get(sid) is replacement, (
            "refresh overwrote a newer owner installed while Session.load() ran"
        )
    assert len(results) == 1 and not isinstance(results[0], Exception), results
    assert results[0] is replacement, (
        "refresh must return the concurrent canonical winner, not the stale object"
    )
    assert _read_owner_lease(sid) == lease_before, (
        "a CAS-failed refresh must never bump the winner's owner-generation lease"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7) Review 16 blocker 2: cold load never resurrects a session deleted mid-load
# ─────────────────────────────────────────────────────────────────────────────


def test_cold_load_never_resurrects_session_deleted_during_load(
    isolated_session_env, monkeypatch
):
    """A deletion that fires while ``Session.load()`` runs (per-SID lease
    bump + durable tombstone) must never be followed by a stale resurrection:
    the cold-load publication CAS refuses to install, no cache entry appears,
    and ``_resolve_session`` raises KeyError."""
    from api import models as _models
    from api.routes import _publish_owner_lease

    sid = "sess-cold-delete"
    load_started = threading.Event()
    release_load = threading.Event()

    def _blocking_load(loaded_sid):
        load_started.set()
        assert release_load.wait(10), "cold-load Session.load never released"
        return _FakeSession(loaded_sid)

    monkeypatch.setattr(_models.Session, "load", lambda loaded_sid: _blocking_load(loaded_sid))
    monkeypatch.setattr(_models, "_sync_sidecar_from_state_db_if_newer", lambda s: False)
    monkeypatch.setattr(_models, "_repair_stale_pending", lambda s: False)
    monkeypatch.setattr(_models, "_session_has_pending_journal_retry", lambda s: False)

    results = []

    def _cold_load():
        try:
            results.append(_models._resolve_session(sid))
        except Exception as exc:
            results.append(exc)

    thread = threading.Thread(target=_cold_load, daemon=True)
    thread.start()
    assert load_started.wait(10), "cold load never entered Session.load"

    # A deletion fires while the load is in flight: canonical removal under
    # the per-SID lease + durable tombstone (the review-16 delete authority).
    with _publish_owner_lease(sid):
        with _models.LOCK:
            _models.SESSIONS.pop(sid, None)
    _models._record_webui_deleted_session_tombstone(sid)

    release_load.set()
    thread.join(10)
    assert not thread.is_alive(), "cold-load-vs-delete deadlocked"

    with _models.LOCK:
        assert sid not in _models.SESSIONS, (
            "cold load resurrected a session deleted while Session.load() ran"
        )
    assert len(results) == 1 and isinstance(results[0], KeyError), results


# ─────────────────────────────────────────────────────────────────────────────
# 8) Review 16 blocker 2: cold load RETURNS the concurrent canonical winner
# ─────────────────────────────────────────────────────────────────────────────


def test_cold_load_returns_concurrent_canonical_winner(isolated_session_env, monkeypatch):
    """A cold load that loses the publication CAS to a concurrent canonical
    winner must RETURN the winner — never the discarded loaded object (the
    review-15 test only asserted cache identity and non-exception)."""
    from api import models as _models

    sid = "sess-cold-return-winner"
    replacement = _FakeSession(sid)
    load_started = threading.Event()
    release_load = threading.Event()

    def _blocking_load(loaded_sid):
        load_started.set()
        assert release_load.wait(10), "cold-load Session.load never released"
        return _FakeSession(loaded_sid)

    monkeypatch.setattr(_models.Session, "load", lambda loaded_sid: _blocking_load(loaded_sid))
    monkeypatch.setattr(_models, "_sync_sidecar_from_state_db_if_newer", lambda s: False)
    monkeypatch.setattr(_models, "_repair_stale_pending", lambda s: False)
    monkeypatch.setattr(_models, "_session_has_pending_journal_retry", lambda s: False)

    results = []

    def _cold_load():
        try:
            results.append(_models._resolve_session(sid))
        except Exception as exc:  # pragma: no cover - failure surfaced below
            results.append(exc)

    thread = threading.Thread(target=_cold_load, daemon=True)
    thread.start()
    assert load_started.wait(10), "cold load never entered Session.load"

    # A canonical winner is installed while the load is blocked.
    with _models.LOCK:
        _models.SESSIONS[sid] = replacement

    release_load.set()
    thread.join(10)
    assert not thread.is_alive(), "cold load deadlocked"
    assert len(results) == 1 and not isinstance(results[0], Exception), results
    assert results[0] is replacement, (
        "cold load returned the discarded loaded object instead of the canonical winner"
    )
    with _models.LOCK:
        assert _models.SESSIONS.get(sid) is replacement


# ─────────────────────────────────────────────────────────────────────────────
# 9) Review 17 blocker 2: compare-and-publish holds LOCK across the write
# ─────────────────────────────────────────────────────────────────────────────


def test_cas_publication_holds_lock_across_compare_and_write(isolated_session_env):
    """The publication CAS must be ONE atomic lease → LOCK operation: the
    global LOCK is held across the compare AND the caller's exact map write,
    so a publisher that uses only LOCK (generated-title persistence, browser
    foreign-session publication) can never install a winner in a
    check→write gap for the stale refresh to overwrite."""
    from api import models as _models
    from api.routes import _publish_owner_lease_if_current

    sid = "sess-cas-atomic"
    cached = _FakeSession(sid)
    disk_version = _FakeSession(sid)
    with _models.LOCK:
        _models.SESSIONS[sid] = cached

    body_entered = threading.Event()
    release_body = threading.Event()
    results = {}

    def _locked_only_publisher():
        # A publisher that uses ONLY LOCK (the review-17 gap publisher).
        with _models.LOCK:
            results["winner_installed"] = _models.SESSIONS.get(sid)
            _models.SESSIONS[sid] = _FakeSession(sid)

    def _cas_publisher():
        with _publish_owner_lease_if_current(sid, expected=cached) as token:
            results["token"] = token
            # The CAS body must still hold LOCK (compare→write atomicity).
            results["lock_held"] = _models.LOCK.locked()
            body_entered.set()
            assert release_body.wait(10), "CAS body never released"
            _models.SESSIONS[sid] = disk_version

    thread = threading.Thread(target=_cas_publisher, daemon=True)
    thread.start()
    assert body_entered.wait(10), "CAS body never entered"
    assert results.get("lock_held") is True, (
        "LOCK released between the CAS compare and the caller's write"
    )

    # While the CAS body holds LOCK, a LOCK-only publisher must BLOCK — it
    # cannot install a winner in the check→write gap (bounded block check).
    writer = threading.Thread(target=_locked_only_publisher, daemon=True)
    writer.start()
    time.sleep(0.2)
    assert writer.is_alive(), (
        "LOCK-only publisher slipped into the CAS check→write gap"
    )

    release_body.set()
    thread.join(10)
    assert not thread.is_alive(), "CAS publisher deadlocked"
    writer.join(10)
    assert not writer.is_alive(), "LOCK-only publisher deadlocked"

    # The LOCK-only winner landed AFTER the atomic CAS write — never between
    # the compare and the write — so the CAS write was not overwritten by a
    # stale refresh racing a current winner.
    with _models.LOCK:
        final = _models.SESSIONS.get(sid)
    assert final is not disk_version
    assert results["token"] is not None


def test_cas_publication_rechecks_captured_generation_under_lock(isolated_session_env):
    """The cold-load CAS re-verifies the captured owner-generation token UNDER
    LOCK: a deletion that fired while the caller's disk I/O was in flight is
    observed even when the durable tombstone persistence FAILED — the
    publication CAS-fails (yields None) and the deleted session is never
    resurrected behind a successful-looking delete."""
    from api import models as _models
    from api.routes import _publish_owner_lease, _publish_owner_lease_if_current

    sid = "sess-cas-generation"
    s = _FakeSession(sid)
    with _models.LOCK:
        _models.SESSIONS[sid] = s
    # The loader captures the generation BEFORE its in-flight disk load.
    gen_before_load = _models._read_owner_generation(sid)

    # A deletion fires while the load is in flight: canonical removal under
    # the per-SID lease — but the durable tombstone persistence FAILS.
    def _boom(ids):
        raise OSError("tombstone write failed")

    _real_save = _models._save_webui_deleted_session_tombstone
    _models._save_webui_deleted_session_tombstone = _boom
    try:
        with pytest.raises(OSError):
            with _publish_owner_lease(sid):
                with _models.LOCK:
                    _models.SESSIONS.pop(sid, None)
                _models._record_webui_deleted_session_tombstone(sid)
    finally:
        _models._save_webui_deleted_session_tombstone = _real_save
    # The durable revocation was never persisted.
    assert sid not in _models._load_webui_deleted_session_tombstone()

    # The cold-load publication with the STALE captured generation must
    # CAS-fail under LOCK — no tombstone is needed; the bumped generation is
    # the barrier (review 17).
    with _publish_owner_lease_if_current(
        sid, expected=None, expected_generation=gen_before_load
    ) as token:
        assert token is None, "stale captured generation must CAS-fail"
    with _models.LOCK:
        assert sid not in _models.SESSIONS, (
            "cold-load publish resurrected a session deleted while tombstone persistence failed"
        )


def test_cold_load_never_resurrects_when_tombstone_persistence_fails(
    isolated_session_env, monkeypatch
):
    """End-to-end review-17 schedule: a cold loader waits behind a deletion
    whose durable tombstone persistence FAILS.  The lease was bumped by the
    deletion, so the loader must never install the loaded object — no cache
    entry, ``_resolve_session`` raises KeyError, and the deletion that
    reported failure is never undone by a resurrection."""
    from api import models as _models
    from api.routes import _publish_owner_lease

    sid = "sess-cold-delete-tombstone-fail"
    load_started = threading.Event()
    release_load = threading.Event()
    results = []

    def _blocking_load(loaded_sid):
        load_started.set()
        assert release_load.wait(10), "cold-load Session.load never released"
        return _FakeSession(loaded_sid)

    monkeypatch.setattr(_models.Session, "load", lambda loaded_sid: _blocking_load(loaded_sid))
    monkeypatch.setattr(_models, "_sync_sidecar_from_state_db_if_newer", lambda s: False)
    monkeypatch.setattr(_models, "_repair_stale_pending", lambda s: False)
    monkeypatch.setattr(_models, "_session_has_pending_journal_retry", lambda s: False)

    def _boom(ids):
        raise OSError("tombstone write failed")

    monkeypatch.setattr(_models, "_save_webui_deleted_session_tombstone", _boom)

    def _cold_load():
        try:
            results.append(_models._resolve_session(sid))
        except Exception as exc:  # pragma: no cover - failure surfaced below
            results.append(exc)

    thread = threading.Thread(target=_cold_load, daemon=True)
    thread.start()
    assert load_started.wait(10), "cold load never entered Session.load"

    # Deletion fires while the load is blocked: lease bumped + SESSIONS popped
    # + sidecar unlinked — and the durable tombstone write FAILS (the API
    # reports the delete as failed, fail-closed).
    with pytest.raises(OSError):
        with _publish_owner_lease(sid):
            with _models.LOCK:
                _models.SESSIONS.pop(sid, None)
            _models._record_webui_deleted_session_tombstone(sid)
    assert sid not in _models._load_webui_deleted_session_tombstone()

    release_load.set()
    thread.join(10)
    assert not thread.is_alive(), "cold-load-vs-failed-tombstone-delete deadlocked"

    with _models.LOCK:
        assert sid not in _models.SESSIONS, (
            "cold load resurrected a session deleted while tombstone persistence failed"
        )
    assert len(results) == 1 and isinstance(results[0], KeyError), results


# ─────────────────────────────────────────────────────────────────────────────
# 10) Review 17 blocker 2: durable deletion revocation fails closed
# ─────────────────────────────────────────────────────────────────────────────


def test_session_delete_fails_closed_when_tombstone_persistence_fails(
    isolated_session_env, monkeypatch
):
    """``/api/session/delete`` must NOT report success when the durable
    deleted-session tombstone cannot be persisted: the revocation error is
    surfaced as a 500 (fail closed), not swallowed into ``ok: True``."""
    from types import SimpleNamespace

    import api.routes as routes
    from api import models as _models

    sid = "sess-delete-tombstone-fail-closed"
    session = _FakeSession(sid)
    session.save = lambda *a, **kw: None  # sidecar persistence not needed here
    with _models.LOCK:
        _models.SESSIONS[sid] = session

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": sid})
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: captured.update(
            payload={"error": msg}, status=status
        )
        or True,
    )
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda sid: {})
    monkeypatch.setattr(_models, "delete_cli_session", lambda sid: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    # The durable tombstone write fails (disk error).
    monkeypatch.setattr(
        _models,
        "_save_webui_deleted_session_tombstone",
        lambda ids: (_ for _ in ()).throw(OSError("tombstone disk full")),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    assert captured["status"] == 500, captured
    assert "error" in (captured.get("payload") or {}), captured
    # Fail-closed: never ok:True after a lost durable revocation.
    assert captured["payload"].get("ok") is not True, captured
