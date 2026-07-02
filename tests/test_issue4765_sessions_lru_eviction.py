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

These tests prove the four required invariants:
  1. Eviction happens once the cache grows past the cap.
  2. An active / streaming session is NEVER evicted, even when oldest.
  3. An evicted session lazily reloads from disk with identical content.
  4. No data loss: eviction removes only the in-memory copy, never the file.
"""
import collections
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

def test_cache_cap_reads_config_yaml_key():
    """The cap is configurable via config.yaml webui.sessions_cache_max (#4765)."""
    from api import config as _cfg

    assert _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": 42}}) == 42
    # Invalid / missing values must fall back, never disable the bound.
    fallback = _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": "nope"}})
    assert isinstance(fallback, int) and fallback >= 1
    assert _cfg.get_sessions_cache_max({"webui": {}}) >= 1
    assert _cfg.get_sessions_cache_max({}) >= 1
    # A zero/negative typo must not disable the cap.
    assert _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": 0}}) >= 1
    assert _cfg.get_sessions_cache_max({"webui": {"sessions_cache_max": -5}}) >= 1


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
