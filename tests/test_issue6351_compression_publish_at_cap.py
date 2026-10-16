"""Publishing a compression continuation must not deadlock at cap (#6351, PR #8240).

``api.streaming._migrate_compression_session_ownership()`` rotates the resident
session id when context compression hands the turn to a continuation. The
publish step also runs ``_evict_sessions_over_cap()`` — and that eviction walks
candidates through ``_session_is_evictable()`` →
``_session_has_live_metadata_writer()``, which **acquires
``SESSION_AGENT_LOCKS_LOCK``** to read the agent-lock registry.

``SESSION_AGENT_LOCKS_LOCK`` is a plain ``threading.Lock``: non-reentrant. So
holding it across the eviction call is a self-deadlock the moment the eviction
walk reaches any candidate. A cache that is merely **at** cap is enough to
trigger it: publishing the continuation adds a third key to a cap-two cache, the
walk visits the clean neighbour, and the call never returns while still holding
both the global cache ``LOCK`` and the registry lock — stalling unrelated
session access, not just this compression.

The regression calls the REAL publisher with the real locks on a cache sitting
exactly at cap: one active owner (the session being compressed) plus one clean,
disk-backed neighbour. Completion is asserted with a bound, so a reintroduced
deadlock fails the test instead of hanging the suite. The child is terminated
only to bound that failure.
"""

from __future__ import annotations

import collections
import multiprocessing
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

PUBLISH_TIMEOUT_S = 20.0


@pytest.fixture
def at_cap_env():
    """Isolate SESSIONS / locks / session dir onto a throwaway temp dir.

    Same contract as tests/test_issue6351_idle_sessions_eviction.py, plus the
    three ``api.streaming`` module-level bindings the publisher closes over
    (``LOCK``, ``SESSIONS``, ``SESSION_DIR``). ``api.streaming`` imports those by
    value at import time, so patching ``api.config`` alone would leave the
    publisher reading the real process globals.
    """
    from api import config as _cfg
    from api import models as _models
    from api import streaming as _streaming

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = sessions_dir / "_index.json"

    old = {
        "cfg_SESSION_DIR": _cfg.SESSION_DIR,
        "models_SESSION_DIR": getattr(_models, "SESSION_DIR", None),
        "streaming_SESSION_DIR": _streaming.SESSION_DIR,
        "cfg_INDEX": _cfg.SESSION_INDEX_FILE,
        "models_INDEX": getattr(_models, "SESSION_INDEX_FILE", None),
        "SESSIONS": _cfg.SESSIONS,
        "models_SESSIONS": _models.SESSIONS,
        "streaming_SESSIONS": _streaming.SESSIONS,
        "LOCK": _cfg.LOCK,
        "models_LOCK": _models.LOCK,
        "streaming_LOCK": _streaming.LOCK,
        "SESSIONS_MAX": _cfg.SESSIONS_MAX,
        "streaming_SESSIONS_MAX": _streaming.SESSIONS_MAX,
        "get_sessions_cache_max": _cfg.get_sessions_cache_max,
        "last_applied_cap": _cfg._LAST_APPLIED_SESSIONS_CACHE_MAX,
        "AGENT_LOCKS": _cfg.SESSION_AGENT_LOCKS,
        "AGENT_LOCKS_LOCK": _cfg.SESSION_AGENT_LOCKS_LOCK,
        "streaming_AGENT_LOCKS": _streaming.SESSION_AGENT_LOCKS,
        "streaming_AGENT_LOCKS_LOCK": _streaming.SESSION_AGENT_LOCKS_LOCK,
    }

    _cfg.SESSION_DIR = sessions_dir
    _models.SESSION_DIR = sessions_dir
    _streaming.SESSION_DIR = sessions_dir
    _cfg.SESSION_INDEX_FILE = index_file
    _models.SESSION_INDEX_FILE = index_file
    _cfg.SESSIONS = collections.OrderedDict()
    _models.SESSIONS = _cfg.SESSIONS
    _streaming.SESSIONS = _cfg.SESSIONS
    _cfg.LOCK = threading.Lock()
    _models.LOCK = _cfg.LOCK
    _streaming.LOCK = _cfg.LOCK
    _cfg.SESSIONS_MAX = 2
    _streaming.SESSIONS_MAX = 2
    # The eviction pass resolves the cap through api.config, which would
    # otherwise pick up the operator's real webui.sessions_cache_max.
    _cfg.get_sessions_cache_max = lambda config_data=None: _cfg.SESSIONS_MAX
    # Fresh, non-reentrant registry lock owned by this test.
    _cfg.SESSION_AGENT_LOCKS = {}
    _cfg.SESSION_AGENT_LOCKS_LOCK = threading.Lock()
    _streaming.SESSION_AGENT_LOCKS = _cfg.SESSION_AGENT_LOCKS
    _streaming.SESSION_AGENT_LOCKS_LOCK = _cfg.SESSION_AGENT_LOCKS_LOCK

    try:
        yield sessions_dir
    finally:
        _cfg.SESSION_DIR = old["cfg_SESSION_DIR"]
        if old["models_SESSION_DIR"] is not None:
            _models.SESSION_DIR = old["models_SESSION_DIR"]
        _streaming.SESSION_DIR = old["streaming_SESSION_DIR"]
        _cfg.SESSION_INDEX_FILE = old["cfg_INDEX"]
        if old["models_INDEX"] is not None:
            _models.SESSION_INDEX_FILE = old["models_INDEX"]
        _cfg.SESSIONS = old["SESSIONS"]
        _models.SESSIONS = old["models_SESSIONS"]
        _streaming.SESSIONS = old["streaming_SESSIONS"]
        _cfg.LOCK = old["LOCK"]
        _models.LOCK = old["models_LOCK"]
        _streaming.LOCK = old["streaming_LOCK"]
        _cfg.SESSIONS_MAX = old["SESSIONS_MAX"]
        _streaming.SESSIONS_MAX = old["streaming_SESSIONS_MAX"]
        _cfg.get_sessions_cache_max = old["get_sessions_cache_max"]
        _cfg._LAST_APPLIED_SESSIONS_CACHE_MAX = old["last_applied_cap"]
        _cfg.SESSION_AGENT_LOCKS = old["AGENT_LOCKS"]
        _cfg.SESSION_AGENT_LOCKS_LOCK = old["AGENT_LOCKS_LOCK"]
        _streaming.SESSION_AGENT_LOCKS = old["streaming_AGENT_LOCKS"]
        _streaming.SESSION_AGENT_LOCKS_LOCK = old["streaming_AGENT_LOCKS_LOCK"]
        shutil.rmtree(tmpdir, ignore_errors=True)


def _persisted_session(sid, title):
    """A real, disk-backed session the eviction pass is allowed to drop."""
    from api.models import Session

    now = time.time()
    s = Session(
        session_id=sid,
        title=title,
        messages=[
            {"role": "user", "content": f"hello {sid}", "timestamp": now},
            {"role": "assistant", "content": f"reply {sid}", "timestamp": now},
        ],
    )
    s.save()
    return s


def _run_publisher(old_sid, new_sid, agent_lock, result_q):
    """Child entry point: call the REAL publisher and report what it did.

    Forked so the parent's isolated globals are inherited, and so a reintroduced
    deadlock can be terminated instead of wedging the test session.
    """
    try:
        from api import streaming as _streaming

        outcome = {
            "published": _streaming._migrate_compression_session_ownership(
                _streaming.SESSIONS[old_sid], old_sid, new_sid, agent_lock
            ),
            "new_resident": _streaming.SESSIONS.get(new_sid) is not None,
            "old_resident": old_sid in _streaming.SESSIONS,
            "registry_old": old_sid in _streaming.SESSION_AGENT_LOCKS,
            "registry_new": new_sid in _streaming.SESSION_AGENT_LOCKS,
            "error": None,
        }
    except BaseException as exc:  # pragma: no cover - diagnostic path
        outcome = {"published": None, "error": f"{type(exc).__name__}: {exc}"}
    result_q.put(outcome)


def test_compression_publish_completes_with_neighbour_at_cap(at_cap_env):
    """Publishing into an AT-cap cache completes and republishes ownership.

    Cap 2, one active owner plus one clean disk-backed neighbour. Publishing the
    continuation is the third key, so the eviction walk runs and reaches the
    neighbour — the exact path that re-enters ``SESSION_AGENT_LOCKS_LOCK``.
    """
    from api import config as _cfg
    from api import streaming as _streaming

    old_sid, new_sid = "sess_at_cap_old", "sess_at_cap_new"

    owner = _persisted_session(old_sid, "Owner")
    owner.pre_compression_snapshot = True
    neighbour = _persisted_session("sess_at_cap_neighbour", "Neighbour")

    # Oldest-first so the walk reaches the clean neighbour, not the owner.
    with _cfg.LOCK:
        _cfg.SESSIONS[old_sid] = owner
        _cfg.SESSIONS.move_to_end(old_sid)
        _cfg.SESSIONS[neighbour.session_id] = neighbour
        _cfg.SESSIONS.move_to_end(neighbour.session_id)

    agent_lock = threading.Lock()
    _cfg.SESSION_AGENT_LOCKS[old_sid] = agent_lock
    _cfg.SESSION_AGENT_LOCKS[new_sid] = agent_lock
    agent_lock.acquire()  # the active owner holds its lock for the whole publish

    # Precondition: exactly at cap, and eviction really does consult the writer
    # predicate that re-enters the registry lock.
    assert len(_cfg.SESSIONS) == _cfg.SESSIONS_MAX == 2
    assert _streaming.SESSIONS is _cfg.SESSIONS

    ctx = multiprocessing.get_context("fork")
    result_q = ctx.Queue()
    child = ctx.Process(
        target=_run_publisher, args=(old_sid, new_sid, agent_lock, result_q)
    )
    child.start()
    child.join(PUBLISH_TIMEOUT_S)

    if child.is_alive():
        child.terminate()
        child.join(5)
        pytest.fail(
            "compression publication deadlocked on an at-cap cache: "
            f"_migrate_compression_session_ownership() did not return within "
            f"{PUBLISH_TIMEOUT_S}s (regression: eviction is running while "
            "SESSION_AGENT_LOCKS_LOCK is held)"
        )

    assert not result_q.empty(), "publisher child produced no result"
    outcome = result_q.get(timeout=5)

    assert outcome.get("error") is None, outcome.get("error")
    assert outcome["published"] is True, outcome
    assert outcome["new_resident"] is True, "continuation was not published"
    assert outcome["old_resident"] is False, "old alias was not retired"
    assert outcome["registry_new"] is True, "continuation lost its agent lock"
    assert outcome["registry_old"] is True, "old alias lost its agent lock"

    # The cache lock and the registry lock must both be free again.
    assert _cfg.LOCK.acquire(blocking=False), "cache LOCK still held after publish"
    _cfg.LOCK.release()
    assert _cfg.SESSION_AGENT_LOCKS_LOCK.acquire(
        blocking=False
    ), "SESSION_AGENT_LOCKS_LOCK still held after publish"
    _cfg.SESSION_AGENT_LOCKS_LOCK.release()

    agent_lock.release()
