"""Regression tests for the bounded, lazy-loading SESSIONS cache (#4765).

Crash cluster: #4765 / #2233 / #4633.

Root cause: the WebUI kept ALL session objects + messages in a global in-memory
``OrderedDict`` (``api.config.SESSIONS``). On long-running self-hosted installs
the cache never shed idle sessions, so RSS climbed unbounded
(~700MB -> 7.5GB@9h -> 17.8GB@44h) until the interpreter segfaulted.

The fix keeps the cache an LRU ``OrderedDict`` but replaces the pre-existing
*blind* ``SESSIONS.popitem(last=False)`` eviction (which could drop an active or
unsaved session and lose data) with ``_evict_sessions_over_cap()``: it only ever
removes clean, persisted, non-active sessions, and ``get_session()`` lazily
reloads an evicted session from its JSON sidecar on next access.

These tests prove the required invariants:
  1. Eviction happens once the cache grows past the entry or byte cap.
  2. An active / streaming session is NEVER evicted, even when oldest.
  3. An evicted session lazily reloads from disk with identical content.
  4. No data loss: eviction removes only the in-memory copy, never the file.
  5. Full loads/saves record serialized weight; metadata stubs stay lightweight.
  6. One oversized most-recent transcript remains warm instead of reload-looping.
"""
import collections
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_session_env():
    """Isolate all SESSIONS-cache global state onto a throwaway temp dir.

    ``api.models`` imports ``SESSION_DIR`` / ``SESSION_INDEX_FILE`` at module
    load, so both ``api.config`` and ``api.models`` copies must be redirected.
    Everything is restored on teardown (even on exception).
    """
    from api import config as _cfg
    from api import models as _models

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    old = {
        "cfg_SESSION_DIR": _cfg.SESSION_DIR,
        "models_SESSION_DIR": getattr(_models, "SESSION_DIR", None),
        "cfg_SESSION_INDEX_FILE": _cfg.SESSION_INDEX_FILE,
        "models_SESSION_INDEX_FILE": getattr(_models, "SESSION_INDEX_FILE", None),
        "SESSIONS": _cfg.SESSIONS,
        "LOCK": _cfg.LOCK,
        "SESSIONS_MAX": _cfg.SESSIONS_MAX,
        "cfg": getattr(_cfg, "cfg", None),
    }

    index_file = sessions_dir / "_index.json"
    _cfg.SESSION_DIR = sessions_dir
    _models.SESSION_DIR = sessions_dir
    _cfg.SESSION_INDEX_FILE = index_file
    _models.SESSION_INDEX_FILE = index_file
    _cfg.LOCK = threading.Lock()
    _models.LOCK = _cfg.LOCK
    _cfg.SESSIONS = collections.OrderedDict()
    _models.SESSIONS = _cfg.SESSIONS

    try:
        yield sessions_dir
    finally:
        _cfg.SESSION_DIR = old["cfg_SESSION_DIR"]
        if old["models_SESSION_DIR"] is not None:
            _models.SESSION_DIR = old["models_SESSION_DIR"]
        _cfg.SESSION_INDEX_FILE = old["cfg_SESSION_INDEX_FILE"]
        if old["models_SESSION_INDEX_FILE"] is not None:
            _models.SESSION_INDEX_FILE = old["models_SESSION_INDEX_FILE"]
        _cfg.SESSIONS = old["SESSIONS"]
        _models.SESSIONS = old["SESSIONS"]
        _cfg.LOCK = old["LOCK"]
        _models.LOCK = old["LOCK"]
        _cfg.SESSIONS_MAX = old["SESSIONS_MAX"]
        if old["cfg"] is not None:
            _cfg.cfg = old["cfg"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def _make_persisted_session(idx, *, messages=None):
    """Build + save a real session with at least one message (so it persists)."""
    from api.models import Session

    if messages is None:
        messages = [
            {"role": "user", "content": f"hello {idx}", "timestamp": time.time()},
            {"role": "assistant", "content": f"reply {idx}", "timestamp": time.time()},
        ]
    s = Session(session_id=f"sess{idx:04d}", title=f"Session {idx}", messages=messages)
    s.save()
    return s


def _insert(sid_session):
    """Insert a session into the cache exactly like the production accessors do."""
    from api.config import SESSIONS, LOCK
    from api.models import _evict_sessions_over_cap

    with LOCK:
        SESSIONS[sid_session.session_id] = sid_session
        SESSIONS.move_to_end(sid_session.session_id)
        _evict_sessions_over_cap()


# ─────────────────────────── config knob ────────────────────────────────────

def test_default_sessions_cache_cap_is_100():
    """The shipped no-override session cache default is 100 (#6351)."""
    from api import config as _cfg

    assert _cfg.DEFAULT_SESSIONS_CACHE_MAX == 100


def test_cache_cap_reads_config_yaml_key():
    """The cap is configurable via config.yaml webui.sessions_cache_max (#4765)."""
    from api import config as _cfg

    old_sessions_max = _cfg.SESSIONS_MAX
    try:
        _cfg.SESSIONS_MAX = 222
        assert _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": 42}}) == 42
        # Invalid / missing values must fall back, never disable the bound.
        assert _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": "nope"}}) == 222
        assert _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": 0}}) == 222
        assert _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": -5}}) == 222
    finally:
        _cfg.SESSIONS_MAX = old_sessions_max


def test_cache_cap_preserves_environment_fallback():
    """The parsed env fallback still wins when config is absent or invalid (#6351)."""
    from api import config as _cfg

    old_sessions_max = _cfg.SESSIONS_MAX
    try:
        _cfg.SESSIONS_MAX = 222
        assert _cfg.get_sessions_cache_max({"webui": {}}) == 222
        assert _cfg.get_sessions_cache_max({}) == 222
        _cfg.SESSIONS_MAX = _cfg.DEFAULT_SESSIONS_CACHE_MAX
        assert _cfg.get_sessions_cache_max({"webui": {}}) == _cfg.DEFAULT_SESSIONS_CACHE_MAX
        assert _cfg.get_sessions_cache_max({}) == _cfg.DEFAULT_SESSIONS_CACHE_MAX
    finally:
        _cfg.SESSIONS_MAX = old_sessions_max


def test_default_sessions_cache_byte_cap_is_128_mib():
    """The entry cap also has a serialized-byte backstop for large transcripts."""
    from api import config as _cfg

    assert _cfg.DEFAULT_SESSIONS_CACHE_MAX_BYTES == 128 * 1024 * 1024


def test_cache_byte_cap_reads_config_yaml_key_and_fails_safe():
    """Operators can tune the byte budget without disabling it accidentally."""
    from api import config as _cfg

    mib = 1024 * 1024
    assert _cfg.get_sessions_cache_max_bytes(
        {"webui": {"sessions_cache_max_mb": 64}}
    ) == 64 * mib
    assert _cfg.get_sessions_cache_max_bytes(
        {"webui": {"sessions_cache_max_mb": "32"}}
    ) == 32 * mib
    for invalid in (0, -1, "nope", None):
        assert _cfg.get_sessions_cache_max_bytes(
            {"webui": {"sessions_cache_max_mb": invalid}}
        ) == _cfg.DEFAULT_SESSIONS_CACHE_MAX_BYTES


def test_eviction_resolves_both_limits_from_one_config_snapshot(
    isolated_session_env, monkeypatch,
):
    """Each pass uses one snapshot whose count and byte limits drive eviction."""
    from api import config as _cfg
    from api.config import LOCK, SESSIONS
    from api.models import _evict_sessions_over_cap

    calls = 0

    def fake_get_config():
        nonlocal calls
        calls += 1
        return {
            "webui": {
                "sessions_cache_max": 2,
                "sessions_cache_max_mb": 1,
            }
        }

    monkeypatch.setattr(_cfg, "get_config", fake_get_config)

    # Count dominates: three tiny sessions are below 1 MiB but exceed cap=2.
    count_sessions = [_make_persisted_session(700 + i) for i in range(3)]
    with LOCK:
        for session in count_sessions:
            SESSIONS[session.session_id] = session
        count_evicted = _evict_sessions_over_cap()
    assert count_evicted == 1
    assert list(SESSIONS) == [s.session_id for s in count_sessions[-2:]]

    SESSIONS.clear()

    # Bytes dominate: two ~700 KiB sessions meet cap=2 but exceed 1 MiB.
    byte_sessions = [
        _make_persisted_session(
            710 + i,
            messages=[{"role": "assistant", "content": "x" * (700 * 1024)}],
        )
        for i in range(2)
    ]
    with LOCK:
        for session in byte_sessions:
            SESSIONS[session.session_id] = session
        byte_evicted = _evict_sessions_over_cap()

    assert byte_evicted == 1
    assert list(SESSIONS) == [byte_sessions[-1].session_id]
    assert calls == 2  # exactly one configuration read per enforcement pass


# ─────────────────────────── invariant 1: eviction ──────────────────────────

def test_eviction_happens_past_the_cap(isolated_session_env):
    """Inserting well past the cap must bound the in-memory cache size (#4765)."""
    from api import config as _cfg
    from api.config import SESSIONS

    _cfg.SESSIONS_MAX = 5
    cap = 5

    created = [_make_persisted_session(i) for i in range(20)]
    for s in created:
        _insert(s)

    # The cache must be bounded — this is the whole point of the fix. Without
    # it, all 20 (and eventually millions) would remain resident forever.
    assert len(SESSIONS) <= cap, (
        f"cache grew to {len(SESSIONS)} entries; expected <= {cap} — the "
        f"unbounded-growth crash (#4765/#2233/#4633) is not fixed"
    )

    # The most-recently-inserted sessions are the ones kept (LRU semantics).
    kept = set(SESSIONS.keys())
    assert created[-1].session_id in kept
    assert created[0].session_id not in kept


def test_eviction_happens_past_the_byte_cap_below_entry_cap(isolated_session_env):
    """A few large transcripts must not bypass the count-only LRU bound."""
    from api.config import LOCK, SESSIONS
    from api.models import _evict_sessions_over_cap

    created = [
        _make_persisted_session(
            i,
            messages=[{"role": "assistant", "content": "x" * (700 * 1024)}],
        )
        for i in range(3)
    ]
    for session in created:
        _insert(session)

    assert len(SESSIONS) == 3  # comfortably below the entry cap used below
    with LOCK:
        evicted = _evict_sessions_over_cap(cap=10, max_bytes=1024 * 1024)

    assert evicted == 2
    assert list(SESSIONS) == [created[-1].session_id]
    assert sum(s._cache_resident_bytes for s in SESSIONS.values()) <= 1024 * 1024


def test_byte_eviction_preserves_same_count_unsaved_edit(isolated_session_env):
    """Byte pressure must not discard edits that do not change message count."""
    from api.config import LOCK, SESSIONS
    from api.models import _evict_sessions_over_cap, get_session

    edited = _make_persisted_session(
        90,
        messages=[{"role": "assistant", "content": "old"}],
    )
    sibling = _make_persisted_session(
        91,
        messages=[{"role": "assistant", "content": "sibling"}],
    )
    _insert(edited)
    _insert(sibling)

    edited.messages[0]["content"] = "new"
    with LOCK:
        evicted = _evict_sessions_over_cap(cap=10, max_bytes=1)

    assert evicted == 0
    assert edited.session_id in SESSIONS
    assert SESSIONS[edited.session_id] is edited
    assert get_session(edited.session_id).messages[0]["content"] == "new"

    # A successful save refreshes the persisted-state fingerprint, so the now
    # clean LRU can be reclaimed and lazy reload returns the edited content.
    edited.save()
    get_session(sibling.session_id)  # make the sibling MRU; edited is evictable LRU
    with LOCK:
        evicted = _evict_sessions_over_cap(cap=10, max_bytes=1)
    assert evicted == 1
    assert edited.session_id not in SESSIONS
    assert get_session(edited.session_id).messages[0]["content"] == "new"


def test_byte_eviction_preserves_unsaved_metadata_edit(isolated_session_env):
    """Persisted message parity cannot justify dropping changed metadata."""
    from api.config import LOCK, SESSIONS
    from api.models import _evict_sessions_over_cap

    edited = _make_persisted_session(92)
    sibling = _make_persisted_session(93)
    _insert(edited)
    _insert(sibling)

    edited.title = "Unsaved title"
    with LOCK:
        evicted = _evict_sessions_over_cap(cap=10, max_bytes=1)

    assert evicted == 0
    assert SESSIONS[edited.session_id] is edited
    assert SESSIONS[edited.session_id].title == "Unsaved title"


def test_save_fingerprint_describes_serialized_snapshot_not_later_mutation(
    isolated_session_env, monkeypatch,
):
    """A mutation after payload capture must remain visibly unsaved."""
    from api import models
    from api.config import LOCK, SESSIONS

    session = _make_persisted_session(
        89,
        messages=[{"role": "assistant", "content": "persisted"}],
    )
    _insert(session)
    real_replace = models._safe_replace
    mutated = False

    def replace_then_mutate(src, dst):
        nonlocal mutated
        real_replace(src, dst)
        if Path(dst) == session.path:
            session.title = "unsaved after payload"
            mutated = True

    monkeypatch.setattr(models, "_safe_replace", replace_then_mutate)
    session.save(touch_updated_at=False)

    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert mutated is True
    assert persisted["title"] != session.title
    assert models._session_matches_persisted_state(session) is False

    sibling = _make_persisted_session(88)
    _insert(sibling)  # make the sibling MRU so the edited session is considered
    with LOCK:
        evicted = models._evict_sessions_over_cap(cap=10, max_bytes=1)

    assert evicted == 0
    assert SESSIONS[session.session_id] is session


def test_save_snapshot_is_not_fooled_by_nested_aba_during_serialization(
    isolated_session_env, monkeypatch,
):
    """The payload and durable fingerprint must describe one immutable snapshot."""
    from api import models
    from api.config import LOCK, SESSIONS

    session = _make_persisted_session(
        87,
        messages=[{"role": "assistant", "content": "A"}],
    )
    _insert(session)
    real_dumps = models.json.dumps
    mutated = False

    def dumps_with_nested_aba(value, *args, **kwargs):
        nonlocal mutated
        if (
            not mutated
            and isinstance(value, dict)
            and value.get("session_id") == session.session_id
            and isinstance(value.get("messages"), list)
        ):
            mutated = True
            session.messages[0]["content"] = "B"
            try:
                return real_dumps(value, *args, **kwargs)
            finally:
                session.messages[0]["content"] = "A"
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(models.json, "dumps", dumps_with_nested_aba)
    session.save(touch_updated_at=False)

    assert mutated is True
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["messages"][0]["content"] == "A"
    assert session.messages[0]["content"] == "A"
    assert models._session_matches_persisted_state(session) is True

    sibling = _make_persisted_session(86)
    _insert(sibling)  # make the sibling MRU before reclaiming the clean LRU
    with LOCK:
        evicted = models._evict_sessions_over_cap(cap=10, max_bytes=1)

    assert evicted == 1
    assert session.session_id not in SESSIONS
    assert models.get_session(session.session_id).messages[0]["content"] == "A"


class _ObservedSessions(collections.OrderedDict):
    """Ordered cache double that exposes the candidate-removal instant."""

    def __init__(self, *args, observed_session_id, removal_started, **kwargs):
        super().__init__(*args, **kwargs)
        self._observed_session_id = observed_session_id
        self._removal_started = removal_started

    def pop(self, key, default=None):
        if key == self._observed_session_id:
            self._removal_started.set()
        return super().pop(key, default)


def test_byte_eviction_coordinates_validation_and_removal_with_session_writer(
    isolated_session_env, monkeypatch,
):
    """A writer that begins after validation cannot lose an unsaved mutation."""
    from api import config as _cfg
    from api import models
    from api.config import LOCK

    candidate = _make_persisted_session(85)
    sibling = _make_persisted_session(84)
    removal_started = threading.Event()
    cache = _ObservedSessions(
        [(candidate.session_id, candidate), (sibling.session_id, sibling)],
        observed_session_id=candidate.session_id,
        removal_started=removal_started,
    )
    monkeypatch.setattr(_cfg, "SESSIONS", cache)
    monkeypatch.setattr(models, "SESSIONS", cache)

    real_matches = models._session_matches_persisted_state
    validation_finished = threading.Event()
    mutation_attempted = threading.Event()
    mutation_finished = threading.Event()
    mutation_before_removal = threading.Event()
    failures = []

    def matches_then_offer_writer(session):
        matches = real_matches(session)
        if session is candidate:
            validation_finished.set()
            assert mutation_attempted.wait(timeout=1.0)
            # The unsafe implementation lets the writer mutate here. A safe
            # implementation either blocks it until removal, or revalidates and
            # retains the changed cache entry.
            mutation_finished.wait(timeout=0.1)
        return matches

    monkeypatch.setattr(models, "_session_matches_persisted_state", matches_then_offer_writer)

    def mutate_and_save():
        try:
            assert validation_finished.wait(timeout=1.0)
            mutation_attempted.set()
            with _cfg._get_session_agent_lock(candidate.session_id):
                if not removal_started.is_set():
                    mutation_before_removal.set()
                candidate.title = "Unsaved concurrent title"
                mutation_finished.set()
                candidate.save(touch_updated_at=False)
        except BaseException as exc:
            failures.append(exc)

    writer = threading.Thread(target=mutate_and_save, daemon=True)
    writer.start()
    with LOCK:
        models._evict_sessions_over_cap(cap=10, max_bytes=1)
    writer.join(timeout=2.0)

    assert writer.is_alive() is False
    assert failures == []
    # If mutation won the race, validation must have been repeated and cache
    # removal skipped. If removal won under the writer lock, the subsequent save
    # is durable and it is safe for the old clean object to have been reclaimed.
    assert not (
        mutation_before_removal.is_set() and candidate.session_id not in cache
    ), "byte eviction removed state changed after its durability check"


def test_state_db_reconcile_refreshes_cached_weight_and_enforces_budget(
    isolated_session_env, monkeypatch,
):
    """Reconciliation through a locked copy must update the cached projection."""
    from api import config as _cfg
    from api import models
    from api.config import SESSIONS

    sibling = _make_persisted_session(
        94,
        messages=[{"role": "assistant", "content": "s" * (400 * 1024), "timestamp": 1.0}],
    )
    cached = _make_persisted_session(
        95,
        messages=[{"role": "user", "content": "u" * (400 * 1024), "timestamp": 1.0}],
    )
    cached.active_stream_id = "dead-stream"
    cached.save(touch_updated_at=False)
    _insert(sibling)
    _insert(cached)  # the get_session reconciliation path promotes this to MRU

    state_messages = list(cached.messages) + [
        {"role": "assistant", "content": "a" * (700 * 1024), "timestamp": 2.0}
    ]
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        models,
        "get_state_db_session_summary",
        lambda *_args, **_kwargs: {"message_count": 2, "last_message_at": 2.0},
    )
    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda *_args, **_kwargs: state_messages,
    )
    monkeypatch.setattr(
        models,
        "reconciled_state_db_messages_for_session",
        lambda *_args, **kwargs: list(state_messages),
    )
    monkeypatch.setattr(
        _cfg,
        "get_config",
        lambda: {
            "webui": {
                "sessions_cache_max": 10,
                "sessions_cache_max_mb": 1,
            }
        },
    )

    assert models._sync_sidecar_from_state_db_if_newer(cached) is True

    assert SESSIONS[cached.session_id] is cached
    assert sibling.session_id not in SESSIONS
    assert cached._cache_resident_bytes == cached.path.stat().st_size
    assert models._session_matches_persisted_state(cached) is True


def test_state_db_reconcile_preserves_unsaved_metadata_and_skips_byte_eviction(
    isolated_session_env, monkeypatch,
):
    """A lock-owned repair cannot make unrelated cached metadata look durable."""
    from api import config as _cfg
    from api import models
    from api.config import SESSIONS

    cached = _make_persisted_session(
        96,
        messages=[{"role": "user", "content": "u" * (400 * 1024), "timestamp": 1.0}],
    )
    cached.active_stream_id = "dead-metadata-stream"
    cached.save(touch_updated_at=False)
    sibling = _make_persisted_session(
        97,
        messages=[{"role": "assistant", "content": "s" * (400 * 1024), "timestamp": 1.0}],
    )
    _insert(cached)
    _insert(sibling)
    cached.title = "Unsaved title"

    recovered = list(cached.messages) + [
        {"role": "assistant", "content": "a" * (700 * 1024), "timestamp": 2.0}
    ]
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        models,
        "get_state_db_session_summary",
        lambda *_args, **_kwargs: {"message_count": 2, "last_message_at": 2.0},
    )
    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda *_args, **_kwargs: list(recovered),
    )
    monkeypatch.setattr(
        models,
        "reconciled_state_db_messages_for_session",
        lambda *_args, **_kwargs: list(recovered),
    )
    monkeypatch.setattr(
        _cfg,
        "get_config",
        lambda: {
            "webui": {
                "sessions_cache_max": 10,
                "sessions_cache_max_mb": 1,
            }
        },
    )

    assert models._sync_sidecar_from_state_db_if_newer(cached) is True

    assert SESSIONS[cached.session_id] is cached
    assert cached.title == "Unsaved title"
    assert cached._cache_persisted_fingerprint is None
    assert sibling.session_id in SESSIONS


def test_save_enforces_byte_budget_after_resident_session_grows(
    isolated_session_env, monkeypatch,
):
    """A hot session growing in place reclaims old cache entries immediately."""
    from api import config as _cfg
    from api.config import SESSIONS

    monkeypatch.setattr(
        _cfg,
        "get_config",
        lambda: {
            "webui": {
                "sessions_cache_max": 10,
                "sessions_cache_max_mb": 1,
            }
        },
    )

    sessions = [
        _make_persisted_session(
            400 + i,
            messages=[{"role": "assistant", "content": "s" * (250 * 1024)}],
        )
        for i in range(3)
    ]
    for session in sessions:
        _insert(session)

    assert list(SESSIONS) == [session.session_id for session in sessions]

    current = sessions[-1]
    current.messages = [
        {"role": "assistant", "content": "g" * (700 * 1024)}
    ]
    current.save()

    assert current._cache_resident_bytes == current.path.stat().st_size
    assert sessions[0].session_id not in SESSIONS
    assert sessions[1].session_id in SESSIONS
    assert current.session_id in SESSIONS
    assert sum(s._cache_resident_bytes for s in SESSIONS.values()) <= 1024 * 1024


def test_cache_hit_full_reload_enforces_grown_sidecar_weight(
    isolated_session_env, monkeypatch,
):
    """Replacing a stale cache hit must immediately reapply the byte budget."""
    from api import config as _cfg
    from api import models
    from api.config import SESSIONS

    monkeypatch.setattr(
        _cfg,
        "get_config",
        lambda: {
            "webui": {
                "sessions_cache_max": 10,
                "sessions_cache_max_mb": 1,
            }
        },
    )
    siblings = [
        _make_persisted_session(
            410 + i,
            messages=[{"role": "assistant", "content": "s" * (400 * 1024)}],
        )
        for i in range(2)
    ]
    stale = _make_persisted_session(
        412,
        messages=[{"role": "assistant", "content": "old" * 1024}],
    )
    for session in [*siblings, stale]:
        _insert(session)

    external = models.Session(
        session_id=stale.session_id,
        title=stale.title,
        messages=[
            {"role": "user", "content": "new prompt"},
            {"role": "assistant", "content": "grown" + "g" * (700 * 1024)},
        ],
        created_at=stale.created_at,
        updated_at=stale.updated_at + 1,
    )
    external.save(touch_updated_at=False)

    refreshed = models.get_session(stale.session_id)

    assert refreshed is SESSIONS[stale.session_id]
    assert refreshed is not stale
    assert refreshed.messages[-1]["content"].startswith("grown")
    assert refreshed._cache_resident_bytes == refreshed.path.stat().st_size
    assert all(session.session_id not in SESSIONS for session in siblings)
    assert sum(models._session_cache_resident_bytes(s) for s in SESSIONS.values()) <= 1024 * 1024


def test_eviction_legacy_partial_self_heal_does_not_reacquire_cache_lock(
    isolated_session_env,
):
    """Eviction can full-load and repair legacy partials without deadlocking LOCK."""
    from api import models
    from api.config import LOCK, SESSIONS

    sid = "legacy-partial-eviction"
    partial = {
        "role": "assistant",
        "content": "working",
        "_partial": True,
        "timestamp": 123.0,
    }
    # Pre-#5854 ordering: scenes precede message_count, so the cheap prefix has
    # no authoritative count and eviction must full-load. Duplicate partials
    # make that load invoke its self-healing save path.
    legacy_payload = {
        "session_id": sid,
        "title": "Legacy partials",
        "workspace": str(isolated_session_env.parent),
        "model": "test",
        "created_at": 100.0,
        "updated_at": 200.0,
        "anchor_activity_scenes": {},
        "message_count": 3,
        "messages": [
            {"role": "user", "content": "run it"},
            partial,
            dict(partial),
        ],
        "tool_calls": [],
    }
    path = isolated_session_env / f"{sid}.json"
    path.write_text(json.dumps(legacy_payload), encoding="utf-8")
    cached = models.Session(
        session_id=sid,
        title="Legacy partials",
        messages=[legacy_payload["messages"][0], partial],
        created_at=100.0,
        updated_at=200.0,
    )
    cached._cache_resident_bytes = path.stat().st_size
    sibling = _make_persisted_session(419)
    SESSIONS[sid] = cached
    SESSIONS[sibling.session_id] = sibling

    failures = []

    def enforce():
        try:
            with LOCK:
                models._evict_sessions_over_cap(cap=1, max_bytes=1 << 30)
        except BaseException as exc:  # surface cleanup errors after breaking a RED deadlock
            failures.append(exc)

    worker = threading.Thread(target=enforce, daemon=True)
    worker.start()
    worker.join(timeout=1.0)
    deadlocked = worker.is_alive()
    if deadlocked:
        # A plain threading.Lock has no ownership, so release the outer hold to
        # let the daemon unwind instead of leaking a stuck thread into pytest.
        LOCK.release()
        worker.join(timeout=1.0)

    assert deadlocked is False, "legacy self-heal recursively acquired the cache LOCK"
    assert failures == []
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert sum(1 for message in persisted["messages"] if message.get("_partial")) == 1


def test_byte_eviction_preserves_active_session_and_reclaims_clean_entries(isolated_session_env):
    """Byte pressure keeps active and MRU state while reclaiming older clean data."""
    from api.config import LOCK, SESSIONS
    from api.models import _evict_sessions_over_cap

    clean = _make_persisted_session(
        100,
        messages=[{"role": "assistant", "content": "b" * (700 * 1024)}],
    )
    active = _make_persisted_session(
        101,
        messages=[{"role": "assistant", "content": "a" * (700 * 1024)}],
    )
    active.active_stream_id = "live-stream"
    current = _make_persisted_session(
        102,
        messages=[{"role": "assistant", "content": "c" * (100 * 1024)}],
    )
    _insert(clean)
    _insert(active)
    _insert(current)

    with LOCK:
        evicted = _evict_sessions_over_cap(cap=10, max_bytes=1024 * 1024)

    assert evicted == 1
    assert clean.session_id not in SESSIONS
    assert active.session_id in SESSIONS
    assert current.session_id in SESSIONS


def test_byte_eviction_keeps_mru_warm_behind_pinned_active_session(isolated_session_env):
    """Pinned work must not force the visible MRU into a cold-load loop."""
    from api.config import LOCK, SESSIONS
    from api.models import _evict_sessions_over_cap

    active = _make_persisted_session(
        190,
        messages=[{"role": "assistant", "content": "a" * (700 * 1024)}],
    )
    active.active_stream_id = "live-stream"
    current = _make_persisted_session(
        191,
        messages=[{"role": "assistant", "content": "c" * (700 * 1024)}],
    )
    _insert(active)
    _insert(current)  # MRU / currently viewed transcript

    with LOCK:
        evicted = _evict_sessions_over_cap(cap=10, max_bytes=1024 * 1024)

    assert evicted == 0
    assert list(SESSIONS) == [active.session_id, current.session_id]


def test_byte_eviction_keeps_one_oversized_mru_to_avoid_reload_loop(isolated_session_env):
    """One transcript may exceed the budget; retaining it avoids cold-loading every access."""
    from api.config import LOCK, SESSIONS
    from api.models import _evict_sessions_over_cap

    oversized = _make_persisted_session(
        200,
        messages=[{"role": "assistant", "content": "z" * (2 * 1024 * 1024)}],
    )
    _insert(oversized)

    with LOCK:
        evicted = _evict_sessions_over_cap(cap=10, max_bytes=1024 * 1024)

    assert evicted == 0
    assert list(SESSIONS) == [oversized.session_id]
    assert oversized._cache_resident_bytes > 1024 * 1024


def test_resident_weight_tracks_save_full_load_and_metadata_stub(isolated_session_env):
    """Weights follow retained data without deep-walking the Python object graph."""
    from api.models import Session

    session = _make_persisted_session(
        300,
        messages=[{"role": "assistant", "content": "w" * (256 * 1024)}],
    )
    sidecar_bytes = session.path.stat().st_size
    assert session._cache_resident_bytes == sidecar_bytes

    loaded = Session.load(session.session_id)
    assert loaded is not None
    assert loaded._cache_resident_bytes == sidecar_bytes
    assert loaded._cache_persisted_fingerprint is not None

    metadata_stub = Session.load_metadata_only(session.session_id)
    assert metadata_stub is not None
    assert 0 < metadata_stub._cache_resident_bytes < sidecar_bytes


def test_full_load_weights_the_exact_bytes_read_across_atomic_replace(
    isolated_session_env, monkeypatch,
):
    """A pre-read stat from the old inode cannot weight replacement JSON."""
    from api import models
    from api.models import Session

    original = _make_persisted_session(
        301,
        messages=[{"role": "assistant", "content": "old"}],
    )
    target = original.path
    old_sig = models._sidecar_stat_signature(target)
    replacement_data = json.loads(target.read_text(encoding="utf-8"))
    replacement_data["messages"] = [
        {"role": "assistant", "content": "new" + "x" * (700 * 1024)}
    ]
    replacement_data["message_count"] = 1
    replacement = target.with_suffix(".replacement")
    replacement.write_text(
        json.dumps(replacement_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    replacement_size = replacement.stat().st_size
    real_signature = models._sidecar_stat_signature
    swapped = False

    def replace_after_signature(path):
        nonlocal swapped
        if Path(path) == target and not swapped:
            swapped = True
            os.replace(replacement, target)
            return old_sig
        return real_signature(path)

    monkeypatch.setattr(models, "_sidecar_stat_signature", replace_after_signature)

    loaded = Session.load(original.session_id)

    assert swapped is True
    assert loaded.messages[0]["content"].startswith("new")
    assert loaded._cache_resident_bytes == replacement_size
    assert replacement_size > old_sig[2]


def test_save_measurement_failure_marks_weight_unknown_and_enforces_conservatively(
    isolated_session_env, monkeypatch,
):
    """A successful save never keeps a stale pre-growth weight after size failure."""
    from api import config as _cfg
    from api import models
    from api.config import SESSIONS

    monkeypatch.setattr(
        _cfg,
        "get_config",
        lambda: {
            "webui": {
                "sessions_cache_max": 10,
                "sessions_cache_max_mb": 1,
            }
        },
    )
    sessions = [
        _make_persisted_session(
            310 + i,
            messages=[{"role": "assistant", "content": "s" * (250 * 1024)}],
        )
        for i in range(3)
    ]
    for session in sessions:
        _insert(session)

    current = sessions[-1]
    replaced = False
    real_replace = models._safe_replace
    real_path_stat = Path.stat

    def marked_replace(src, dst):
        nonlocal replaced
        real_replace(src, dst)
        if Path(dst) == current.path:
            replaced = True

    def fail_target_stat(path, *args, **kwargs):
        if replaced and Path(path) == current.path:
            raise OSError("post-replace stat unavailable")
        return real_path_stat(path, *args, **kwargs)

    def fail_open_file_stat(_fd):
        raise OSError("open-file size unavailable")

    monkeypatch.setattr(models, "_safe_replace", marked_replace)
    monkeypatch.setattr(Path, "stat", fail_target_stat)
    monkeypatch.setattr(models.os, "fstat", fail_open_file_stat)
    current.messages = [
        {"role": "assistant", "content": "grown" + "g" * (700 * 1024)}
    ]

    current.save()

    assert current._cache_resident_bytes is None
    assert sessions[0].session_id not in SESSIONS
    assert current.session_id in SESSIONS


# ────────────────────── invariant 2: never evict active ──────────────────────

def test_active_streaming_session_never_evicted(isolated_session_env):
    """An active/streaming session must survive eviction even as the oldest (#4765)."""
    from api import config as _cfg
    from api.config import SESSIONS
    from api.models import _session_is_evictable

    _cfg.SESSIONS_MAX = 3

    # Oldest entry is actively streaming (has an in-flight turn).
    active = _make_persisted_session(0)
    active.active_stream_id = "live-stream-xyz"
    active.pending_user_message = "in-flight question"
    active.pending_started_at = time.time()
    _insert(active)

    assert _session_is_evictable(active) is False

    # Now flood the cache far past the cap with clean sessions.
    for i in range(1, 30):
        _insert(_make_persisted_session(i))

    assert active.session_id in SESSIONS, (
        "an actively streaming session was evicted — this would drop an "
        "in-flight turn and corrupt live state (#4765 safety invariant)"
    )
    # The live object identity (with its unsaved runtime state) is preserved.
    assert SESSIONS[active.session_id] is active
    assert SESSIONS[active.session_id].active_stream_id == "live-stream-xyz"


def test_unsaved_session_never_evicted(isolated_session_env):
    """A session with unsaved messages (not yet on disk) is never evicted (#4765)."""
    from api import config as _cfg
    from api.config import SESSIONS
    from api.models import Session, _session_is_evictable

    _cfg.SESSIONS_MAX = 3

    # Build a session with messages in memory but DO NOT save it to disk.
    unsaved = Session(
        session_id="unsaved00001",
        title="Unsaved",
        messages=[{"role": "user", "content": "not persisted yet", "timestamp": time.time()}],
    )
    assert not unsaved.path.exists()
    assert _session_is_evictable(unsaved) is False

    _insert(unsaved)
    for i in range(1, 30):
        _insert(_make_persisted_session(i))

    assert unsaved.session_id in SESSIONS, (
        "a session with unsaved in-memory messages was evicted — this loses "
        "data (#4765 safety invariant)"
    )
    assert SESSIONS[unsaved.session_id] is unsaved


def test_stale_disk_copy_blocks_eviction(isolated_session_env):
    """A cached session ahead of its sidecar (unsaved tail) is not evictable (#4765)."""
    from api.models import _session_is_evictable

    s = _make_persisted_session(1)  # 2 messages on disk
    # Simulate new turns appended in memory but not yet flushed to disk.
    s.messages = s.messages + [
        {"role": "user", "content": "newer unsaved turn", "timestamp": time.time()},
        {"role": "assistant", "content": "newer unsaved reply", "timestamp": time.time()},
    ]
    assert _session_is_evictable(s) is False, (
        "a session whose in-memory messages exceed the on-disk copy must not "
        "be evicted — doing so silently loses the unsaved tail"
    )
    # Once flushed, it becomes evictable again.
    s.save()
    assert _session_is_evictable(s) is True


# ───────────────── invariant 3: lazy reload + invariant 4: no data loss ──────

def test_evicted_session_lazily_reloads_identical_content(isolated_session_env):
    """An evicted session transparently reloads from disk with identical content."""
    from api import config as _cfg
    from api.config import SESSIONS
    from api.models import get_session

    _cfg.SESSIONS_MAX = 3

    rich_messages = [
        {"role": "user", "content": "remember: the passphrase is orange-turbine-42",
         "timestamp": time.time()},
        {"role": "assistant", "content": "Got it, I'll remember orange-turbine-42.",
         "timestamp": time.time()},
        {"role": "user", "content": "what was it?", "timestamp": time.time()},
        {"role": "assistant", "content": "orange-turbine-42", "timestamp": time.time()},
    ]
    victim = _make_persisted_session(0, messages=rich_messages)
    _insert(victim)
    victim_id = victim.session_id
    expected = [dict(m) for m in victim.messages]

    # Push the victim out of the in-memory cache with newer sessions.
    for i in range(1, 30):
        _insert(_make_persisted_session(i))

    assert victim_id not in SESSIONS, (
        "the clean, persisted, idle victim should have been evicted from RAM"
    )
    # The sidecar file is untouched (invariant 4: no data loss).
    assert victim.path.exists()

    # Accessing it again must transparently reload from the sidecar (invariant 3).
    reloaded = get_session(victim_id)
    assert reloaded is not None
    assert reloaded.session_id == victim_id
    assert [{"role": m["role"], "content": m["content"]} for m in reloaded.messages] == \
        [{"role": m["role"], "content": m["content"]} for m in expected], (
        "lazily-reloaded session content differs from what was persisted — "
        "the reload path is lossy (#4765)"
    )
    # And it is back in the cache after the lazy reload.
    assert victim_id in SESSIONS


def test_no_data_loss_all_files_survive_heavy_churn(isolated_session_env):
    """Eviction removes only the in-memory copy; every sidecar file survives (#4765)."""
    from api import config as _cfg
    from api.config import SESSIONS
    from api.models import get_session

    _cfg.SESSIONS_MAX = 4

    created = [_make_persisted_session(i) for i in range(25)]
    for s in created:
        _insert(s)

    # Cache is bounded...
    assert len(SESSIONS) <= 4
    # ...but NOT ONE session file was deleted.
    for s in created:
        assert s.path.exists(), f"sidecar for {s.session_id} was deleted — data loss!"

    # Every single session (even long-evicted ones) is still fully retrievable
    # with its original content via the lazy-reload accessor.
    for i, s in enumerate(created):
        loaded = get_session(s.session_id)
        assert loaded is not None
        assert loaded.title == f"Session {i}"
        assert len(loaded.messages) == 2
        assert loaded.messages[0]["content"] == f"hello {i}"


def test_eviction_skips_active_but_still_bounds_clean_entries(isolated_session_env):
    """Mixed workload: active pinned, clean bounded — the realistic steady state."""
    from api import config as _cfg
    from api.config import SESSIONS

    _cfg.SESSIONS_MAX = 5

    # A handful of concurrently-active streams that must all stay resident.
    actives = []
    for i in range(3):
        a = _make_persisted_session(1000 + i)
        a.active_stream_id = f"stream-{i}"
        _insert(a)
        actives.append(a)

    # Plus heavy churn of clean idle sessions.
    for i in range(40):
        _insert(_make_persisted_session(i))

    # All actives survive.
    for a in actives:
        assert a.session_id in SESSIONS, "an active stream was evicted under churn"

    # The cache stays bounded: active (3, pinned) + at most cap clean entries.
    # It may briefly sit slightly above cap because actives are non-evictable,
    # but it must NOT grow unbounded with the 40 churned sessions.
    assert len(SESSIONS) <= _cfg.SESSIONS_MAX + len(actives)


def test_unsaved_new_session_survives_churn_and_stays_startable(isolated_session_env):
    """A brand-new, never-persisted session must not be evicted.

    ``new_session()`` keeps a session in RAM only until its first message
    (#1171), so the cache is its ONLY copy. The original #4765 predicate treated
    any zero-message session as evictable, reasoning that an empty shell "is
    recreated on next access" — but ``get_session()`` has no recreate path and
    raises ``KeyError``, so ``/api/session/draft`` and ``/api/chat/start`` both
    404 and the session can never be started.

    Real-world trigger: a browser password manager autofilled the sidebar
    conversation filter, whose content search pulls every hit through
    ``get_session()``. That churn blew past the cap and dropped the session the
    user was composing in.
    """
    from api import config as _cfg
    from api.config import SESSIONS
    from api.models import get_session, new_session

    _cfg.SESSIONS_MAX = 5

    composing = new_session()
    sid = composing.session_id
    assert not (_cfg.SESSION_DIR / f"{sid}.json").exists(), (
        "precondition: new_session() must not persist before the first message"
    )

    # Content-search-style churn: far more persisted sessions than the cap.
    for i in range(40):
        _insert(_make_persisted_session(i))

    assert sid in SESSIONS, "unsaved new session was evicted — its only copy is gone"

    # The chokepoint both failing routes go through.
    assert get_session(sid, metadata_only=True) is not None
    assert get_session(sid).session_id == sid


def test_stale_draftless_unsaved_shell_is_evictable(isolated_session_env):
    """An OLD, empty, draftless, never-saved shell must NOT be immortal (#6083 follow-up).

    The #6083 fix protects a fresh unsaved shell so a just-opened "New
    Conversation" is not evicted mid-compose. But protecting EVERY zero-message
    never-saved shell forever would let abandoned "New Conversation" tabs
    accumulate past ``sessions_cache_max`` without bound (a slow leak / OOM).
    A shell that is empty AND draftless AND older than the grace window is
    treated as abandoned and becomes evictable again.
    """
    from api.models import _session_is_evictable, _UNSAVED_SHELL_GRACE_S, new_session

    shell = new_session()
    # Freshly created → protected (inside the grace window).
    assert _session_is_evictable(shell) is False, (
        "a fresh empty shell must be protected during the compose window"
    )
    # Age it past the grace window with no draft and no messages → abandoned.
    shell.created_at = time.time() - (_UNSAVED_SHELL_GRACE_S + 60)
    assert _session_is_evictable(shell) is True, (
        "a stale, empty, draftless, never-saved shell must be evictable so these "
        "shells cannot accumulate unbounded past the cache cap"
    )


def test_stale_unsaved_shell_with_draft_stays_resident(isolated_session_env):
    """A stale shell the user is still composing (has a draft) stays protected.

    Even past the grace window, a never-saved shell that carries a composer
    draft is something the user is actively working on and must not be dropped —
    its draft lives only in this cache entry until the first send.
    """
    from api.models import _session_is_evictable, _UNSAVED_SHELL_GRACE_S, new_session

    shell = new_session()
    shell.created_at = time.time() - (_UNSAVED_SHELL_GRACE_S + 60)
    shell.composer_draft = {"text": "half-written thought", "files": []}
    assert _session_is_evictable(shell) is False, (
        "a stale shell with an active composer draft must stay resident"
    )
def test_content_search_scan_does_not_evict_the_working_set(isolated_session_env):
    """A content search must not push the user's open sessions out of the cache.

    /api/sessions/search?content=1 walks EVERY session. Routing that through
    get_session() inserted each one into the LRU and marked it recently-used, so
    a single search over an install with more sessions than the cap flushed the
    whole cache — the classic buffer-pool scan-pollution problem. The sessions
    the user actually had open were the ones evicted.

    get_session_for_scan() reads without promoting or inserting, so a scan is
    transparent to the LRU.
    """
    from api import config as _cfg
    from api.config import SESSIONS
    from api.models import get_session, get_session_for_scan

    _cfg.SESSIONS_MAX = 5

    working = []
    for i in range(4):
        s = _make_persisted_session(900 + i)
        get_session(s.session_id)          # the user opens it -> legitimately cached
        working.append(s.session_id)

    corpus = [_make_persisted_session(i).session_id for i in range(60)]

    for sid in corpus:                     # what the content search does
        assert get_session_for_scan(sid) is not None

    for sid in working:
        assert sid in SESSIONS, "a scan evicted the user's working set"
    assert not any(sid in SESSIONS for sid in corpus), "the scan polluted the LRU"
    assert len(SESSIONS) <= _cfg.SESSIONS_MAX


def test_scan_accessor_reuses_resident_sessions_without_promoting(isolated_session_env):
    """A scan hit must reuse the cached object but must not refresh its recency."""
    from api import config as _cfg
    from api.config import SESSIONS
    from api.models import get_session, get_session_for_scan

    _cfg.SESSIONS_MAX = 50
    first = _make_persisted_session(801)
    second = _make_persisted_session(802)
    get_session(first.session_id)
    get_session(second.session_id)         # second is now the most-recent entry

    order_before = list(SESSIONS.keys())
    scanned = get_session_for_scan(first.session_id)

    assert scanned is SESSIONS[first.session_id], "scan should reuse the resident object"
    assert list(SESSIONS.keys()) == order_before, "scan must not promote in the LRU"


def test_scan_reconciliation_does_not_enforce_cache_bounds(
    isolated_session_env, monkeypatch,
):
    """A scan self-heal may update disk/cache state but must not churn the LRU."""
    from api import config as _cfg
    from api import models
    from api.config import SESSIONS

    stale = _make_persisted_session(
        940,
        messages=[
            {"role": "user", "content": "u" * (400 * 1024), "timestamp": 100.0}
        ],
    )
    stale.active_stream_id = "dead-scan-stream"
    stale.pending_user_message = "recover scan"
    stale.pending_started_at = 102.0
    stale.save(touch_updated_at=False)
    siblings = [
        _make_persisted_session(
            941 + i,
            messages=[
                {"role": "assistant", "content": "s" * (400 * 1024), "timestamp": 100.0}
            ],
        )
        for i in range(2)
    ]
    _insert(stale)
    for sibling in siblings:
        _insert(sibling)
    order_before = list(SESSIONS)

    recovered = list(stale.messages) + [
        {
            "role": "assistant",
            "content": "recovered" + "a" * (700 * 1024),
            "timestamp": 103.0,
        }
    ]
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        models,
        "get_state_db_session_summary",
        lambda *_args, **_kwargs: {"message_count": 2, "last_message_at": 103.0},
    )
    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda *_args, **_kwargs: list(recovered),
    )
    monkeypatch.setattr(
        _cfg,
        "get_config",
        lambda: {
            "webui": {
                "sessions_cache_max": 10,
                "sessions_cache_max_mb": 1,
            }
        },
    )

    scanned = models.get_session_for_scan(stale.session_id)

    assert scanned is stale
    assert scanned.messages[-1]["content"].startswith("recovered")
    assert list(SESSIONS) == order_before, "scan reconciliation must not evict or promote"


def test_content_search_scan_recovers_newer_state_db_without_lru_churn(
    isolated_session_env, monkeypatch,
):
    """The real search path must find state.db-only recovery text without LRU churn."""
    from types import SimpleNamespace
    from urllib.parse import urlparse

    from api import models, routes
    from api.config import SESSIONS

    working = _make_persisted_session(950)
    stale = _make_persisted_session(
        951,
        messages=[{"role": "user", "content": "old prompt", "timestamp": 100.0}],
    )
    stale.active_stream_id = "dead-stream"
    stale.pending_user_message = "recover me"
    stale.pending_started_at = 102.0
    stale.save()
    _insert(working)
    _insert(stale)
    order_before = list(SESSIONS.keys())
    size_before = len(SESSIONS)

    recovered = [
        {"role": "user", "content": "old prompt", "timestamp": 100.0},
        {"role": "user", "content": "recover me", "timestamp": 102.0},
        {"role": "assistant", "content": "state-db-only needle", "timestamp": 103.0},
    ]
    monkeypatch.setattr(
        models,
        "get_state_db_session_summary",
        lambda sid, profile=None: {"message_count": len(recovered), "last_message_at": 103.0},
    )
    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda sid, **kwargs: list(recovered),
    )
    monkeypatch.setattr(
        routes,
        "all_sessions",
        lambda: [{"session_id": stale.session_id, "title": stale.title, "profile": "default"}],
    )
    monkeypatch.setattr(routes, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status,
        ),
    )

    routes._handle_sessions_search(
        SimpleNamespace(),
        urlparse("/api/sessions/search?q=needle&content=1&depth=0"),
    )

    assert captured["status"] == 200
    assert captured["payload"]["count"] == 1
    assert captured["payload"]["sessions"][0]["session_id"] == stale.session_id
    assert list(SESSIONS.keys()) == order_before, "scan recovery must not promote the LRU"
    assert len(SESSIONS) == size_before, "scan recovery must not insert or evict cache entries"
