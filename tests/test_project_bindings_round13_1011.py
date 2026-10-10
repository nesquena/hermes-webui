"""Round-13 gate for PR #6836 (Greptile P1 2026-10-10T22:39:21Z, inline comment
4239456449, anchored ``d64ed4f3``, ``api/routes.py`` "Empty chats lose their
project"):

    "For a saved, empty chat that is not cached, the earlier
    ``get_session(sid, metadata_only=True)`` puts a metadata-only object into
    ``SESSIONS``.  This ``get_session(sid)`` can return that same object because
    its empty messages match the saved count.  The sweep sets ``project_id``, but
    ``save()`` refuses to write metadata-only objects and the exception is
    swallowed.  The assignment exists only in the cache and disappears after a
    restart.  Upgrade the object with ``_ensure_full_session_before_mutation``
    before assigning and saving it."

HONESTY NOTE — measured, not assumed: the chain as described does **not**
reproduce on this tree.  ``get_session(sid, metadata_only=True)`` on a cache
miss returns the stub **without caching it** (``SESSIONS`` stays empty), so the
follow-up ``get_session(sid)`` returns a real full session and its ``save()``
succeeds (empty chat: project id lands on disk; verified locally).  What IS real
is the hazard the finding is about, for ANY route that leaves a metadata-only
stub resident in the LRU cache — the codebase asserts that state can occur (see
the archive route: "if a sidebar/status preload left one in the LRU cache,
upgrade to a full disk load before mutating").  Before the fix, a resident stub
made the sweep claim the id on the stub, whose ``save()`` then raised inside the
sweep's broad ``except``: the chat kept the id in the cache only and lost it on
restart.  These tests inject the stub state directly, so they pin the sweep's
behaviour regardless of how a stub got resident.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _sweep_source() -> str:
    src = _read("api/routes.py")
    return src[
        src.index("def _auto_assign_sweep_body(") : src.index(
            "def _auto_assign_candidate_count("
        )
    ]


PID = "proj_r13"


def _setup(tmp_path, monkeypatch, *, messages, active_stream_id=""):
    """Real Session files + a real sweep; only the catalog/probe seams are stubbed."""
    from collections import OrderedDict

    import api.models as models
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    ws = tmp_path / "ws-r13"
    ws.mkdir()
    ws_str = str(ws)
    sid = "sess-r13-" + ("stream" if active_stream_id else "plain")

    index_file = session_dir / "_index.json"

    cache: OrderedDict = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", cache)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSIONS", cache)

    session = models.Session(
        session_id=sid,
        title="round13",
        workspace=ws_str,
        model="deepseek-v4.1-flash",
        model_provider="custom",
        created_at=1.0,
        updated_at=2.0,
        active_stream_id=active_stream_id or None,
        messages=messages,
    )
    session.save(skip_index=True)
    index_file.write_text(
        json.dumps(
            [
                {
                    "session_id": sid,
                    "workspace": ws_str,
                    "profile": "default",
                    "project_id": None,
                    "active_stream_id": active_stream_id,
                }
            ]
        ),
        encoding="utf-8",
    )

    row = {
        "project_id": PID,
        "name": "Round13",
        "profile": "default",
        "auto_assign": True,
        "workspaces": [ws_str],
    }
    monkeypatch.setattr(routes, "load_projects", lambda *a, **k: [row])
    monkeypatch.setattr(routes, "save_projects", lambda ps: None)
    monkeypatch.setattr(routes, "_profiles_match", lambda a, b: True)
    monkeypatch.setattr(routes, "_state_db_session_source_strict", lambda s: "")
    monkeypatch.setattr(
        routes, "_active_stream_ids", lambda: set([active_stream_id]) if active_stream_id else set()
    )

    env = SimpleNamespace(
        models=models,
        routes=routes,
        cache=cache,
        sid=sid,
        ws_str=ws_str,
        row=row,
        path=session_dir / f"{sid}.json",
    )
    return env


def _disk(env) -> dict:
    return json.loads(env.path.read_text(encoding="utf-8"))


def _cache_metadata_only_stub(env):
    """Put a REAL metadata-only stub into the LRU cache, as a preload would."""
    stub = env.models.Session.load_metadata_only(env.sid)
    assert stub is not None, "the sidecar must be loadable"
    assert getattr(stub, "_loaded_metadata_only", False) is True, (
        "load_metadata_only() must mark the stub, or the state under test is not "
        "the one the finding describes"
    )
    assert len(stub.messages) == 0, "a metadata-only stub carries no messages"
    env.cache[env.sid] = stub
    return stub


# ---------------------------------------------------------------------------
# Behaviour: a resident metadata-only stub must not swallow the assignment
# ---------------------------------------------------------------------------


def test_sweep_persists_the_project_id_when_a_metadata_only_stub_is_cached(tmp_path, monkeypatch):
    """The finding's own case: an EMPTY chat whose cache entry is a stub.

    Pre-fix: the sweep claims the id on the stub, ``save()`` raises
    ("metadata-only"), the broad ``except`` swallows it and the on-disk sidecar
    never gains the id — the assignment is cache-only and dies on restart.
    """
    env = _setup(tmp_path, monkeypatch, messages=[])
    _cache_metadata_only_stub(env)

    changed = env.routes._apply_project_auto_assign(env.row)

    assert changed == 1, "the sweep must report the session it filed"
    assert _disk(env)["project_id"] == PID, (
        "the assignment must be on disk, not only in the cache"
    )
    assert getattr(env.cache[env.sid], "_loaded_metadata_only", False) is False, (
        "the cache entry must have been upgraded to a full session"
    )


def test_sweep_upgrade_preserves_the_chat_messages(tmp_path, monkeypatch):
    """The upgrade must load the FULL session — #1558 data-loss guard.

    A stub whose ``_metadata_message_count`` matches the saved count must not be
    written back with ``messages=[]``: the fix reloads from disk, so the
    conversation survives AND gains the project id.
    """
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "still here?"},
    ]
    env = _setup(tmp_path, monkeypatch, messages=messages)
    _cache_metadata_only_stub(env)

    changed = env.routes._apply_project_auto_assign(env.row)

    assert changed == 1
    on_disk = _disk(env)
    assert on_disk["project_id"] == PID
    assert len(on_disk["messages"]) == 3, (
        "the upgrade must not truncate the persisted conversation"
    )


def test_cached_streaming_path_upgrades_a_metadata_only_stub(tmp_path, monkeypatch):
    """Same hazard on the actively-streaming assignment (``cached`` object)."""
    active = "stream-r13"
    env = _setup(tmp_path, monkeypatch, messages=[], active_stream_id=active)
    _cache_metadata_only_stub(env)

    changed = env.routes._apply_project_auto_assign(env.row)

    cached = env.cache[env.sid]
    assert changed == 1, (
        "the streaming path must file the chat (the stream's own save persists it)"
    )
    assert cached.project_id == PID
    assert getattr(cached, "_loaded_metadata_only", False) is False, (
        "a stub must be upgraded before the streaming path claims it, otherwise "
        "the stream's save() refuses and the id is lost"
    )


def test_control_a_full_session_is_still_filed(tmp_path, monkeypatch):
    """Control: nothing about the upgrade changes the ordinary path."""
    env = _setup(tmp_path, monkeypatch, messages=[])
    assert env.cache == {} or env.sid not in env.cache

    changed = env.routes._apply_project_auto_assign(env.row)

    assert changed == 1
    assert _disk(env)["project_id"] == PID
    assert getattr(env.cache[env.sid], "_loaded_metadata_only", False) is False


# ---------------------------------------------------------------------------
# Source guard: the upgrade must precede BOTH claim sites, for good
# ---------------------------------------------------------------------------


def test_source_the_sweep_upgrades_before_it_claims_an_id():
    seg = _sweep_source()

    upgrade_persisted = seg.index("s = _ensure_full_session_before_mutation(sid, s)")
    claim_persisted = seg.index("_auto_assign_claim_session(pid, s, s_ws)")
    assert upgrade_persisted < claim_persisted, (
        "the persisted path must upgrade a metadata-only stub before the claim"
    )

    upgrade_cached = seg.index("cached = _ensure_full_session_before_mutation(sid, cached)")
    claim_cached = seg.index("_auto_assign_claim_session(pid, cached, c_ws)")
    assert upgrade_cached < claim_cached, (
        "the cached/streaming path must upgrade a stub before the claim too"
    )


def test_source_the_upgrade_wraps_keyerror_and_rechecks_project_id():
    """The two defensive edges the fix adds must stay shipped."""
    seg = _sweep_source()
    assert "except KeyError:" in seg, (
        "a sidecar that vanished between the two loads must skip the session, "
        "not escape as an exception"
    )
    tail = seg[seg.index("s = _ensure_full_session_before_mutation(sid, s)") :]
    assert 'getattr(s, "project_id", None)' in tail[: tail.index("_auto_assign_claim_session(pid, s, s_ws)")], (
        "after upgrading we must re-check whether someone else filed the chat "
        "(never steal a project id)"
    )
