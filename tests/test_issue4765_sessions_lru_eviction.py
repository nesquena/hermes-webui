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
