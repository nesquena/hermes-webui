"""Idle eviction must not split a draft writer's identity or break the
full-scan session-list iterator (#6351 gate re-review, PR #8240).

Issue #6351 makes cap enforcement traffic-independent: the SessionChannel
reaper's periodic tick now calls
``api.models.sweep_sessions_cache_over_cap()``, so an over-cap ``SESSIONS``
LRU is flattened even on an install with no requests arriving. That is the
right fix, but it makes an idle thread a *concurrent mutator* of ``SESSIONS``
for every code path that resolves a session object, holds it, and later
persists it. Two of those paths were not safe against a tick that lands in
the middle of a request.

P1 — ``/api/session/draft`` POST (``api/routes.py``) resolves the session
BEFORE taking the per-session agent lock, then re-resolves INSIDE it. That
inner re-resolution is the right shape, but the outer resolve can cold-load a
*different* object from the sidecar. When a tick evicts the draft target in
between, draft B's pre-lock resolve republishes the pre-A text, B's in-lock
resolve then hits that stale resident entry (the ``_cached_session_lags_disk``
freshness check compares MESSAGE COUNTS, which a draft-only change leaves
equal), and B's partial-field merge writes the old text back. Both requests
answer 200, and the acknowledged text is rolled back on disk. The hazard class
is the one already documented at ``_adopt_session_state`` (``api/routes.py``):
"Writers fetch the session BEFORE they take its agent lock ... replacing the
cached entry with a different object leaves such a writer holding the
pre-refresh copy."

The fix is writer/object ownership in the eviction predicate: a session whose
agent lock is currently held (or awaited) has a live in-flight metadata
writer, so the periodic sweep must leave it resident. Unlocking is the only
point at which eviction is safe again.

P2 — ``all_sessions()``'s corrupt-index / full-scan fallback
(``api/models.py``) iterated the LIVE ``SESSIONS`` dict with no lock while the
index path above it snapshots its resident values under ``LOCK``. A real idle
tick between iterations raises
``RuntimeError: OrderedDict mutated during iteration``.

Both tests drive the REAL production seams: the real ``handle_post`` draft
branch, the real ``_reaper_loop`` tick with the real
``sweep_sessions_cache_over_cap`` (never a mocked sweep), and the real
``all_sessions()`` fallback reached by writing a genuinely corrupt index.
Neither asserts on source text.
"""

from __future__ import annotations

import collections
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated_session_env():
    """Isolate all SESSIONS-cache global state onto a throwaway temp dir.

    Same contract as tests/test_issue6351_idle_sessions_eviction.py: both the
    ``api.config`` and ``api.models`` copies of the module-level globals have
    to be redirected, and ``api.routes`` binds its own references at import.
    """
    from api import config as _cfg
    from api import models as _models
    from api import routes as _routes

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    old = {
        "cfg_SESSION_DIR": _cfg.SESSION_DIR,
        "models_SESSION_DIR": _models.SESSION_DIR,
        "cfg_SESSION_INDEX_FILE": _cfg.SESSION_INDEX_FILE,
        "models_SESSION_INDEX_FILE": _models.SESSION_INDEX_FILE,
        "cfg_SESSIONS": _cfg.SESSIONS,
        "cfg_LOCK": _cfg.LOCK,
        "models_SESSIONS": _models.SESSIONS,
        "models_LOCK": _models.LOCK,
        "routes_SESSIONS": _routes.SESSIONS,
        "routes_LOCK": _routes.LOCK,
        "routes_SESSION_DIR": _routes.SESSION_DIR,
        "routes_SESSION_INDEX_FILE": _routes.SESSION_INDEX_FILE,
        "cfg_SESSIONS_MAX": _cfg.SESSIONS_MAX,
        "get_sessions_cache_max": _cfg.get_sessions_cache_max,
        "last_applied_cap": _cfg._LAST_APPLIED_SESSIONS_CACHE_MAX,
    }

    index_file = sessions_dir / "_index.json"
    _cfg.SESSION_DIR = sessions_dir
    _models.SESSION_DIR = sessions_dir
    _routes.SESSION_DIR = sessions_dir
    _cfg.SESSION_INDEX_FILE = index_file
    _models.SESSION_INDEX_FILE = index_file
    _routes.SESSION_INDEX_FILE = index_file
    _cfg.LOCK = threading.Lock()
    _cfg.SESSIONS = collections.OrderedDict()
    for mod in (_models, _routes):
        mod.LOCK = _cfg.LOCK
        mod.SESSIONS = _cfg.SESSIONS
    # The sweep resolves the cap through get_sessions_cache_max() BEFORE
    # falling back to SESSIONS_MAX; resolve from the isolated SESSIONS_MAX so
    # an operator's config.yaml cannot override the caps asserted here.
    _cfg.get_sessions_cache_max = lambda config_data=None: _cfg.SESSIONS_MAX

    try:
        yield sessions_dir
    finally:
        _cfg.SESSION_DIR = old["cfg_SESSION_DIR"]
        _models.SESSION_DIR = old["models_SESSION_DIR"]
        _routes.SESSION_DIR = old["routes_SESSION_DIR"]
        _cfg.SESSION_INDEX_FILE = old["cfg_SESSION_INDEX_FILE"]
        _models.SESSION_INDEX_FILE = old["models_SESSION_INDEX_FILE"]
        _routes.SESSION_INDEX_FILE = old["routes_SESSION_INDEX_FILE"]
        _cfg.SESSIONS = old["cfg_SESSIONS"]
        _models.SESSIONS = old["models_SESSIONS"]
        _routes.SESSIONS = old["routes_SESSIONS"]
        _cfg.LOCK = old["cfg_LOCK"]
        _models.LOCK = old["models_LOCK"]
        _routes.LOCK = old["routes_LOCK"]
        _cfg.SESSIONS_MAX = old["cfg_SESSIONS_MAX"]
        _cfg.get_sessions_cache_max = old["get_sessions_cache_max"]
        _cfg._LAST_APPLIED_SESSIONS_CACHE_MAX = old["last_applied_cap"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def _make_persisted_session(idx, *, messages=None, pinned=False, **attrs):
    """Build + save a real session so the eviction pass can prove it safe.

    ``pinned=True`` marks an active stream, which the pass must never evict.
    """
    from api.models import Session

    if messages is None:
        messages = [
            {"role": "user", "content": f"hello {idx}", "timestamp": time.time()},
            {"role": "assistant", "content": f"reply {idx}", "timestamp": time.time()},
        ]
    s = Session(session_id=f"sess{idx:04d}", title=f"Session {idx}", messages=messages)
    for key, value in attrs.items():
        setattr(s, key, value)
    if pinned:
        s.active_stream_id = f"stream-{idx}"
    s.save()
    return s


def _insert_without_eviction(sess):
    """Insert exactly the way the non-evicting insertion sites do (#6351)."""
    from api.config import LOCK, SESSIONS

    with LOCK:
        SESSIONS[sess.session_id] = sess
        SESSIONS.move_to_end(sess.session_id)


def _start_real_reaper(bp, interval=0.02):
    """Start the REAL ``_reaper_loop`` tick, asserting it actually started."""
    original = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = interval
    bp._REAPER_STOP.clear()
    assert bp.start_session_channel_reaper() is True, (
        "the REAL idle reaper must start; a no-op here means the test never "
        "exercised the interleaving it claims to"
    )
    return original


# ───────────────────────────── P1: draft writer ownership ──────────────────

def test_draft_racing_an_idle_tick_does_not_roll_back_acknowledged_text(
    isolated_session_env, monkeypatch
):
    """An idle tick must not let a second draft restore the pre-tick text.

    The schedule, driven through the real handler and a real reaper tick:

      1. draft A changes the composer text and pauses immediately before the
         real draft save, while STILL holding the per-session agent lock. Its
         pre-lock resolve has just promoted the target to newest-in-LRU.
      2. one more clean session makes the cache over cap, so the next real
         idle tick has to evict something;
      3. the tick runs. It must NOT evict the target: a writer is mid-flight
         on it, and that writer holds an object identity the cache would
         otherwise replace with a stale cold copy.
      4. files-only draft B resolves BEFORE blocking on A's lock. B's
         pre-lock resolve must land on the SAME live object A is writing
         through, not on a pre-A copy.
      5. A saves and returns 200 with the new text; B then merges its file
         list on top and returns 200.
      6. a real cold ``Session.load()`` must still hold A's acknowledged text.

    Step 5's "both answer 200 with a consistent-looking body" is the trap: the
    rollback is invisible in both responses and only shows up on a cold read,
    which is why the assertion is made against ``Session.load()``.
    """
    from api import background_process as bp
    from api import config as _cfg
    from api import models
    from api import routes

    # Cap 4 so the sweep has to consider several candidates, and three
    # stream-pinned sessions so the target is reached only after they are
    # correctly skipped as non-evictable.
    _cfg.SESSIONS_MAX = 4
    target = _make_persisted_session(7001)
    target.composer_draft = {"text": "ORIGINAL TEXT", "files": []}
    target.save()
    _insert_without_eviction(target)
    for i in (7002, 7003, 7004):
        _insert_without_eviction(_make_persisted_session(i, pinned=True))

    sid = target.session_id
    assert (models.SESSION_DIR / f"{sid}.json").exists()

    real_save_draft = models._save_session_draft
    real_get_session = routes.get_session

    paused = threading.Event()      # A is at the save seam, holding the lock
    release_a = threading.Event()   # let A finish its real save
    b_resolved = threading.Event()  # B has done its pre-lock resolution
    b_objects: list = []

    def gated_save_draft(session_id, draft):
        """Pause A immediately before the real persistence; B's real save runs."""
        if threading.current_thread().name == "draft-A":
            paused.set()
            assert release_a.wait(20), "test never released draft A"
        return real_save_draft(session_id, draft)

    def signalling_get_session(sid_arg, *args, **kwargs):
        sess = real_get_session(sid_arg, *args, **kwargs)
        if threading.current_thread().name == "draft-B":
            b_objects.append(sess)
            b_resolved.set()
        return sess

    monkeypatch.setattr(routes, "_save_session_draft", gated_save_draft)
    monkeypatch.setattr(routes, "get_session", signalling_get_session)

    results: dict[str, tuple] = {}
    errors: dict[str, BaseException] = {}

    def run_draft(name, body):
        captured: dict = {}
        handler = SimpleNamespace(
            command="POST",
            body=body,
            wfile=None,
            send_response=lambda status: None,
            send_header=lambda key, value: None,
            end_headers=lambda: None,
            _safe_webui_print=lambda msg: None,
        )
        monkeypatch.setattr(routes, "read_body", lambda h: h.body)
        monkeypatch.setattr(routes, "_check_csrf", lambda h: True)
        monkeypatch.setattr(
            routes, "j",
            lambda _h, payload, status=200, extra_headers=None: captured.update(
                payload=payload, status=status
            ) or True,
        )
        try:
            results[name] = (
                routes.handle_post(handler, SimpleNamespace(path="/api/session/draft")),
                captured,
            )
        except BaseException as exc:  # surfaced by the assertions below
            errors[name] = exc

    # ── 1. draft A: new text, pauses before the real save holding the lock ──
    thread_a = threading.Thread(
        target=run_draft,
        args=("A", {"session_id": sid, "text": "ACKNOWLEDGED NEW TEXT"}),
        name="draft-A",
        daemon=True,
    )
    thread_a.start()
    assert paused.wait(20), "draft A never reached the save seam"

    # ── 2. one more clean session puts the cache over cap ─────────────────
    _insert_without_eviction(_make_persisted_session(7005))
    assert len(_cfg.SESSIONS) > _cfg.SESSIONS_MAX

    # ── 3. ONE real idle-reaper tick sweeps the cache ──────────────────────
    original_interval = _start_real_reaper(bp)
    try:
        deadline = time.time() + 20.0
        while (
            time.time() < deadline
            and len(_cfg.SESSIONS) > _cfg.SESSIONS_MAX
        ):
            time.sleep(0.02)

        # The writer/object ownership invariant: the tick must not have
        # replaced the object an in-flight writer is holding the lock for.
        resident = _cfg.SESSIONS.get(sid)
        assert resident is not None, (
            "an idle eviction tick dropped a session whose agent lock a "
            "draft writer holds; the next resolve returns a different object "
            "and the writer's merge writes pre-refresh state back"
        )
        assert resident is target, (
            "an idle eviction tick replaced the cache entry for a session a "
            "draft writer is mid-write on, splitting one session id across "
            "two objects"
        )

        # ── 4. files-only draft B resolves, then blocks on A's lock ────────
        thread_b = threading.Thread(
            target=run_draft,
            args=("B", {"session_id": sid, "files": ["/tmp/attached.txt"]}),
            name="draft-B",
            daemon=True,
        )
        thread_b.start()
        assert b_resolved.wait(20), "draft B never resolved its session"
        thread_b.join(timeout=5)
        assert thread_b.is_alive(), (
            "draft B must be blocked on draft A's agent lock at this point"
        )
        assert all(obj is target for obj in b_objects), (
            "draft B resolved a different object than the one draft A holds "
            "the agent lock on; B will merge onto pre-A state"
        )

        # ── 5. A finishes, then B merges its files on top ──────────────────
        release_a.set()
        thread_a.join(timeout=20)
        thread_b.join(timeout=20)
    finally:
        release_a.set()
        bp.stop_session_channel_reaper()
        bp._REAPER_INTERVAL_SECS = original_interval

    assert not thread_a.is_alive() and not thread_b.is_alive(), (
        f"draft threads did not finish (a={errors.get('A')!r}, b={errors.get('B')!r})"
    )
    assert not errors, f"handler raised: {errors}"
    for name in ("A", "B"):
        assert results[name][0] is True, f"draft {name} did not complete the handler"
        assert results[name][1].get("status", 200) == 200, (
            f"draft {name} did not return 200: {results[name][1]}"
        )

    # ── 6. a real cold load must still hold the acknowledged text ──────────
    with _cfg.LOCK:
        _cfg.SESSIONS.pop(sid, None)  # force a genuine cold read
    cold = models.Session.load(sid)
    assert cold is not None, "the draft session must still be loadable"
    assert (cold.composer_draft or {}).get("text") == "ACKNOWLEDGED NEW TEXT", (
        "an idle eviction tick let a racing draft resolve a pre-refresh copy "
        "and its partial-field merge rolled BACK acknowledged composer text; "
        f"the cold load holds {(cold.composer_draft or {}).get('text')!r}"
    )
    assert (cold.composer_draft or {}).get("files") == ["/tmp/attached.txt"], (
        "draft B's file list must survive alongside A's text"
    )


# ──────────────────── P2: full-scan fallback iterator safety ────────────────

def _build_tripwire_session(fire, armed):
    """A real ``Session`` subclass whose first ``session_id`` read AFTER
    ``armed`` is set fires a REAL idle-reaper tick.

    This pins the cache mutation to the fallback's generator seam rather than
    to wall-clock timing. The arm flag matters: ``Session.save()`` itself
    reads ``session_id``, so an unarmed tripwire would fire its tick during
    setup instead of during the iteration under test.
    """
    from api.models import Session

    class _TripwireSession(Session):
        def __init__(self, *args, **kwargs):
            object.__setattr__(self, "_sid", None)
            object.__setattr__(self, "_fired", False)
            super().__init__(*args, **kwargs)

        @property
        def session_id(self):
            if not object.__getattribute__(self, "_fired") and armed.is_set():
                object.__setattr__(self, "_fired", True)
                fire()
            return object.__getattribute__(self, "_sid")

        @session_id.setter
        def session_id(self, value):
            object.__setattr__(self, "_sid", value)

    return _TripwireSession


def test_full_scan_fallback_survives_a_concurrent_idle_reaper_tick(isolated_session_env):
    """The corrupt-index fallback must not iterate the live SESSIONS dict.

    A real idle tick that lands between two iterations of
    ``all_sessions.full_scan_overlay`` must not raise
    ``RuntimeError: OrderedDict mutated during iteration`` — the index path
    already snapshots its resident values under ``LOCK``, and this fallback
    must be consistent with it.
    """
    from api import background_process as bp
    from api import config as _cfg
    from api import models

    _cfg.SESSIONS_MAX = 2

    # The tripwire must be the OLDEST entry so it is the first value the
    # fallback iterates; the mutation therefore happens mid-iteration. Its
    # active stream keeps it resident, so the tick evicts the clean entries
    # and mutates the dict underneath the live iterator.
    tick_ran = threading.Event()
    armed = threading.Event()
    seen: dict = {}

    def fire_real_tick():
        before = len(_cfg.SESSIONS)
        _start_real_reaper(bp)  # the REAL _reaper_loop tick
        deadline = time.time() + 20.0
        while time.time() < deadline and len(_cfg.SESSIONS) >= before:
            time.sleep(0.01)
        seen["before"] = before
        seen["after"] = len(_cfg.SESSIONS)
        tick_ran.set()

    tripwire = _build_tripwire_session(fire_real_tick, armed)(
        session_id="sess0000-tripwire",
        title="Tripwire",
        messages=[{"role": "user", "content": "hi", "timestamp": time.time()}],
    )
    tripwire.active_stream_id = "stream-tripwire"
    tripwire.save()
    _insert_without_eviction(tripwire)
    for i in range(1, 6):
        _insert_without_eviction(_make_persisted_session(8100 + i))
    assert len(_cfg.SESSIONS) == 6

    original_interval = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = 0.02

    # A genuinely corrupt index is what reaches the full-scan fallback.
    _cfg.SESSION_INDEX_FILE.write_text("{ this is not valid json", encoding="utf-8")

    try:
        # Arm only now: the tripwire fires from inside all_sessions()'s
        # full_scan_overlay loop, which is the generator seam under test.
        armed.set()
        sessions = models.all_sessions()
    finally:
        bp.stop_session_channel_reaper()
        bp._REAPER_INTERVAL_SECS = original_interval

    assert tick_ran.is_set(), (
        "the real idle-reaper tick never ran during the fallback iteration; "
        "the test did not exercise the interleaving it claims to"
    )
    assert seen["after"] < seen["before"], (
        f"the real tick must actually shrink the cache to matter here: "
        f"{seen['before']} -> {seen['after']}"
    )
    ids = {s["session_id"] for s in sessions}
    assert "sess0000-tripwire" in ids, (
        "the full-scan fallback must still return the resident tripwire session"
    )