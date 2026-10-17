"""Idle-proof the SESSIONS cache cap with a periodic reaper sweep (#6351).

Issue: high RAM usage over time — the in-memory ``SESSIONS`` LRU cap
(``webui.sessions_cache_max`` / ``DEFAULT_SESSIONS_CACHE_MAX = 100``) was only
enforced by an eviction pass that runs where a *session-touching request*
happens to run one. Two shapes leave the cache above its cap with nobody left
to correct it:

  * insert paths that never call ``_evict_sessions_over_cap()`` (the
    projects/delete cache refresh, the archive metadata-only upgrade, the
    chat/start synth session), so the cache grows past the cap and only some
    later request — if any arrives — brings it back;
  * entries pinned as active/unsaved during a busy burst that become clean
    afterwards, while the process goes idle.

On a long-running self-hosted install (cron/headless, no tab attached) no such
request arrives, so the resident working set keeps the shape of the busiest
moment of the process's life — the growth curve this issue reports
(issue Q3: "would a periodic/scheduled eviction pass help flatten the growth
curve?").

Fix under test: ``api.models.sweep_sessions_cache_over_cap()`` — a
LOCK-acquiring wrapper around the SAME safe eviction
(``_evict_sessions_over_cap``) — invoked once per tick of the existing
60-second SessionChannel reaper loop. It is traffic-independent and never
evicts an active or unsaved session, so the #4765 safety invariants still hold.
"""

from __future__ import annotations

import collections
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_session_env():
    """Isolate all SESSIONS-cache global state onto a throwaway temp dir.

    Same isolation contract as tests/test_issue4765_sessions_lru_eviction.py:
    ``api.models`` imports ``SESSION_DIR`` / ``SESSION_INDEX_FILE`` at module
    load, so both the ``api.config`` and ``api.models`` copies must be
    redirected. Everything is restored on teardown (even on exception).
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
        "get_sessions_cache_max": _cfg.get_sessions_cache_max,
        "last_applied_cap": _cfg._LAST_APPLIED_SESSIONS_CACHE_MAX,
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
    # The eviction pass asks get_sessions_cache_max() BEFORE falling back to
    # SESSIONS_MAX, so a webui.sessions_cache_max in the operator's config.yaml
    # would override the caps these tests assert on. Resolve from the isolated
    # SESSIONS_MAX instead, and restore the diagnostics cap the pass publishes
    # on every run, so neither leaks into other tests (#6351).
    _cfg.get_sessions_cache_max = lambda config_data=None: _cfg.SESSIONS_MAX

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
        _cfg.get_sessions_cache_max = old["get_sessions_cache_max"]
        _cfg._LAST_APPLIED_SESSIONS_CACHE_MAX = old["last_applied_cap"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def _make_persisted_session(idx, *, messages=None, **attrs):
    """Build + save a real session (so the eviction pass proves it safe)."""
    from api.models import Session

    if messages is None:
        messages = [
            {"role": "user", "content": f"hello {idx}", "timestamp": time.time()},
            {"role": "assistant", "content": f"reply {idx}", "timestamp": time.time()},
        ]
    s = Session(session_id=f"sess{idx:04d}", title=f"Session {idx}", messages=messages)
    for key, value in attrs.items():
        setattr(s, key, value)
    s.save()
    return s


def _insert_without_eviction(sid_session):
    """Insert exactly the way the non-evicting insertion sites do (#6351)."""
    from api.config import LOCK, SESSIONS

    with LOCK:
        SESSIONS[sid_session.session_id] = sid_session
        SESSIONS.move_to_end(sid_session.session_id)


# ─────────────────── invariant: a traffic-independent eviction pass ─────────

def test_sweep_reclaims_an_over_cap_cache_without_route_traffic(isolated_session_env):
    """An over-cap cache is reclaimed by the sweep alone — no request needed (#6351)."""
    from api import config as _cfg
    from api import models

    _cfg.SESSIONS_MAX = 5
    for i in range(12):
        _insert_without_eviction(_make_persisted_session(i))

    # Precondition: an insert path that never evicts left the cache over cap and
    # nothing else is going to run an eviction pass (idle install, no traffic).
    assert len(_cfg.SESSIONS) > 5

    evicted = models.sweep_sessions_cache_over_cap()

    assert len(_cfg.SESSIONS) == 5, "the sweep must bring the cache back to its cap"
    assert evicted == 7, "every clean, persisted entry above the cap is reclaimable"
    # The enforced cap stays published for the runtime-diagnostics payload (#6351).
    assert _cfg._LAST_APPLIED_SESSIONS_CACHE_MAX == 5


def test_sweep_is_a_noop_at_or_below_the_cap(isolated_session_env):
    """At or under the cap the sweep changes nothing and reports 0."""
    from api import config as _cfg
    from api import models

    _cfg.SESSIONS_MAX = 5
    kept = [_make_persisted_session(i) for i in range(3)]
    for s in kept:
        _insert_without_eviction(s)

    assert models.sweep_sessions_cache_over_cap() == 0
    assert list(_cfg.SESSIONS.keys()) == [s.session_id for s in kept]


def test_sweep_never_evicts_an_active_or_unsaved_session(isolated_session_env):
    """Invariant 2 of #4765 survives the sweep: pinned entries stay resident."""
    from api import config as _cfg
    from api import models

    _cfg.SESSIONS_MAX = 5
    pinned = _make_persisted_session(0, active_stream_id="stream-live-1")
    _insert_without_eviction(pinned)
    for i in range(1, 7):
        _insert_without_eviction(_make_persisted_session(i))

    evicted = models.sweep_sessions_cache_over_cap()

    assert pinned.session_id in _cfg.SESSIONS, (
        "a session with a live stream must never be dropped by the periodic pass"
    )
    assert evicted == 2, (
        "the pass reclaims only clean entries until the cache fits the cap again "
        "(7 resident, cap 5, one pinned)"
    )
    assert len(_cfg.SESSIONS) == 5


def test_sweep_never_evicts_a_session_with_unsaved_messages(isolated_session_env):
    """Invariant 2 of #4765 under the sweep: on-disk-behind entries stay resident.

    The active-stream case above pins the streaming guard; this one pins the
    persistence guard — a session whose newest turn never reached its sidecar
    would be unreadable after an eviction (``get_session()`` reloads from disk).
    """
    from api import config as _cfg
    from api import models

    _cfg.SESSIONS_MAX = 5
    unsaved = _make_persisted_session(0)
    unsaved.messages.append(
        {"role": "assistant", "content": "reply that never hit disk", "timestamp": time.time()}
    )
    _insert_without_eviction(unsaved)
    for i in range(1, 8):
        _insert_without_eviction(_make_persisted_session(i))

    evicted = models.sweep_sessions_cache_over_cap()

    assert unsaved.session_id in _cfg.SESSIONS, (
        "a session with messages its sidecar does not have yet must never be dropped"
    )
    assert evicted == 3, (
        "the pass reclaims only disk-backed entries until the cache fits the cap "
        "(8 resident, cap 5, one pinned by unsaved messages)"
    )
    assert len(_cfg.SESSIONS) == 5


def test_sweep_reclaims_entries_that_pin_then_go_clean_between_ticks(isolated_session_env):
    """Issue shape 2: pinned during a burst, clean afterwards, no request (#6351).

    Tick 1 runs while every protected entry is still streaming, so the cache
    stays over its cap. The streams then complete without any request touching
    the cache, and tick 2 — the periodic pass alone — finishes the reclaim.
    """
    from api import config as _cfg
    from api import models

    _cfg.SESSIONS_MAX = 5
    pinned = [_make_persisted_session(i, active_stream_id=f"stream-burst-{i}") for i in range(6)]
    for s in pinned:
        _insert_without_eviction(s)
    for i in range(6, 9):
        _insert_without_eviction(_make_persisted_session(i))

    first = models.sweep_sessions_cache_over_cap()

    assert first == 3, "tick 1 reclaims every clean entry (9 resident, cap 5)"
    assert len(_cfg.SESSIONS) == 6, (
        "six still-streaming entries keep the cache over its cap after tick 1"
    )
    assert all(s.session_id in _cfg.SESSIONS for s in pinned)

    # The streams complete between ticks — no request runs an eviction pass.
    for s in pinned:
        s.active_stream_id = None

    second = models.sweep_sessions_cache_over_cap()

    assert second == 1, "tick 2 reclaims the now-clean entries with no traffic"
    assert len(_cfg.SESSIONS) == 5, "the periodic pass brings the cache to its cap"
    assert pinned[0].session_id not in _cfg.SESSIONS


def test_sweep_runs_the_eviction_pass_under_the_cache_lock(isolated_session_env, monkeypatch):
    """The sweep owns taking ``LOCK``; the eviction pass never takes it itself."""
    from api import config as _cfg
    from api import models

    _cfg.SESSIONS_MAX = 5
    for i in range(6):
        _insert_without_eviction(_make_persisted_session(i))

    seen: dict[str, bool] = {}
    real_evict = models._evict_sessions_over_cap

    def spy(cap=None):
        seen["locked"] = bool(models.LOCK.locked())
        return real_evict(cap)

    monkeypatch.setattr(models, "_evict_sessions_over_cap", spy)

    models.sweep_sessions_cache_over_cap()

    assert seen.get("locked") is True, (
        "_evict_sessions_over_cap() must be called with the cache LOCK held"
    )


# ───────────────────────── wiring: the periodic pass ───────────────────────

def test_reaper_loop_runs_the_sessions_cache_sweep(isolated_session_env):
    """The existing 60s reaper tick is what makes the cap traffic-independent."""
    from api import background_process as bp
    from api import config as _cfg

    _cfg.SESSIONS_MAX = 4
    for i in range(10):
        _insert_without_eviction(_make_persisted_session(i))
    assert len(_cfg.SESSIONS) > 4

    original_interval = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = 0.05
    bp.start_session_channel_reaper()
    try:
        deadline = time.time() + 10.0
        while time.time() < deadline and len(_cfg.SESSIONS) > 4:
            time.sleep(0.05)
    finally:
        bp.stop_session_channel_reaper()
        bp._REAPER_INTERVAL_SECS = original_interval

    assert len(_cfg.SESSIONS) <= 4, (
        "the reaper tick must run the sessions-cache sweep even with no traffic"
    )


# ─────────── the insertion paths that grew the cache without a pass ─────────

def test_delete_target_session_insert_runs_the_eviction_pass(isolated_session_env):
    """The projects/delete cache refresh inserts WITHOUT evicting (#6351).

    ``_delete_target_session`` rebinds/refreshes a resident entry in place (no
    size change), but the ``resident is None`` arm materialises the sidecar into
    a cache entry that was not there before — a genuine insert. Left unbounded
    it is one of the shapes that puts the cache over its cap for good.
    """
    from api import config as _cfg
    from api import routes as _routes

    _cfg.SESSIONS_MAX = 5
    target = _make_persisted_session(99)  # sidecar on disk, NOT resident
    for i in range(10):
        _insert_without_eviction(_make_persisted_session(i))

    # routes binds SESSIONS/LOCK at import: point it at the isolated globals.
    old_sessions, old_lock = _routes.SESSIONS, _routes.LOCK
    _routes.SESSIONS = _cfg.SESSIONS
    _routes.LOCK = _cfg.LOCK
    try:
        assert len(_routes.SESSIONS) == 10
        resolved = _routes._delete_target_session(target.session_id, set())
        assert resolved is not None, "the delete target must resolve from its sidecar"
        assert len(_routes.SESSIONS) <= 5, (
            "inserting the delete target must not leave the cache over its cap"
        )
    finally:
        _routes.SESSIONS = old_sessions
        _routes.LOCK = old_lock


def test_chat_start_synth_insert_runs_the_eviction_pass():
    """The chat/start foreign-session claim inserts WITHOUT evicting (#6351).

    The ``except KeyError`` arm of ``_handle_chat_start`` materialises a
    WebUI-owned Session for a CLI/TUI/Desktop session that has no sidecar yet
    and binds it into ``SESSIONS`` — the only insert on that path, and the only
    one of its kind with no eviction pass after it.
    """
    from tests.test_chat_start_claim_cli_session import ROUTES_PY, _route_handler_block

    body = _route_handler_block(ROUTES_PY.read_text(encoding="utf-8"), "_handle_chat_start")
    m = re.search(
        r"s = synth\s*\n(?P<arm>.*?)(?=\n\s*except PermissionError)",
        body,
        re.DOTALL,
    )
    assert m, "could not find the synth-claim insert arm of _handle_chat_start"
    assert "_evict_sessions_over_cap()" in m.group("arm"), (
        "the chat/start synth insert must run the cache-cap eviction pass, "
        "otherwise claiming foreign sessions grows SESSIONS past its cap"
    )


# ─── issue Q2: an abandoned stream's offline buffer must not outlive it ─────

def test_abandoned_stream_channel_and_its_offline_buffer_are_reclaimed():
    """Issue Q2: `_offline_buffer` is freed when a stream completes with no subscriber.

    The buffer only exists while its `StreamChannel` is reachable, so the
    guarantee is that the STREAMS entry — the last production reference — is
    dropped for a stream nobody is attached to. This pins the reaper's orphan
    reclaim (the path a worker that died before teardown leaves behind), which
    is what keeps a no-tab turn from pinning its buffered frames for the life
    of the process.
    """
    import gc
    import weakref

    from api import config as cfg

    sid = "test-offline-buffer-reclaim-6351"
    ch = cfg.create_stream_channel()
    for i in range(64):
        ch.put_nowait(("token", f"frame-{i}", f"evt-{i}"))
    assert len(ch._offline_buffer) == 64, "frames buffered with no subscriber"

    with cfg.STREAMS_LOCK:
        cfg.STREAMS[sid] = ch
    cfg.register_stream_owner(sid, "test-offline-buffer-session")
    ref = weakref.ref(ch)
    del ch

    try:
        assert cfg.is_orphaned_stream(sid) is True, (
            "a stream with no worker, no claim and no pending turn is an orphan"
        )
        assert cfg.release_orphaned_stream_if_still_orphaned(sid) is True
        assert sid not in cfg.STREAMS, (
            "the STREAMS entry holds the channel; dropping it is what frees the "
            "offline buffer (issue Q2: no subscriber ever attached)"
        )
        gc.collect()
        assert ref() is None, (
            "the abandoned stream's channel — and with it its 64 buffered "
            "frames — must be garbage-collectable, not pinned for the "
            "process lifetime"
        )
    finally:
        with cfg.STREAMS_LOCK:
            cfg.STREAMS.pop(sid, None)
        cfg.unregister_stream_owner(sid)
