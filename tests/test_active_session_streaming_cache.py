"""Active-session (streaming) merge cache: key builder + store/probe/evict.

The display-merge cache shipped gated to INACTIVE sessions only (upstream
#7212). This suite covers the streaming twin for ACTIVE (streaming/pending)
sessions: a fail-closed composite key over every input the merge consumed
(sidecar stat sig, lineage parent sigs, in-memory tail marker, target-session
state.db revision, truncation fields, msg_limit), a TTL-bounded LRU
store/probe pair, and eviction helpers. Any unresolvable component must yield
None (never a stale transcript).
"""

import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import pytest


@pytest.fixture()
def routes_env(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    with routes._display_streaming_merge_cache_lock:
        routes._display_streaming_merge_cache.clear()
    yield SimpleNamespace(config=config, models=models, routes=routes)
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    with routes._display_streaming_merge_cache_lock:
        routes._display_streaming_merge_cache.clear()


SID = "20260101_000000_stream1"
STATE_SIG = ("target-session-revision-v1", 42)
MSG_LIMIT = 50


def _write_sidecar(routes_env, sid=SID, n=3):
    rows = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}",
            "timestamp": 1000.0 + i,
            "id": f"row-{i}",
        }
        for i in range(n)
    ]
    path = routes_env.routes.SESSION_DIR / f"{sid}.json"
    path.write_text(json.dumps({"session_id": sid, "messages": rows}))
    return rows


def _make_session(messages, *, sid=SID, active=True):
    return SimpleNamespace(
        session_id=sid,
        profile=None,
        messages=list(messages),
        active_stream_id="stream-live" if active else None,
        pending_user_message=None,
        truncation_watermark=None,
        truncation_boundary=None,
    )


def _patch_state_sig(routes_env, monkeypatch, sig=STATE_SIG):
    """Pin the target-session state.db signature (no real state.db in tests)."""
    seen = []

    def _fake(session_id, profile=None):
        seen.append((session_id, profile))
        return sig

    monkeypatch.setattr(routes_env.routes, "_state_db_session_signature", _fake)
    return seen


def test_key_builder_fail_closed_without_sidecar(routes_env, monkeypatch):
    """No sidecar file on disk -> stat sig unresolvable -> key is None."""
    routes = routes_env.routes
    seen = _patch_state_sig(routes_env, monkeypatch)
    s = _make_session([{"role": "user", "content": "hi", "timestamp": 1.0, "id": "a"}])

    assert routes._display_streaming_merge_cache_key(s, msg_limit=MSG_LIMIT) is None
    assert seen == []  # failed closed before ever touching the state.db signature


def test_key_builder_returns_full_composite(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    seen = _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)

    key = routes._display_streaming_merge_cache_key(s, msg_limit=MSG_LIMIT)

    assert key is not None
    assert key[0] == ("streaming-active",)
    assert key[1] == routes_env.models._sidecar_stat_signature(
        routes.SESSION_DIR / f"{SID}.json"
    )
    assert key[2] == ()  # no lineage parents recorded for this sid
    assert key[3] == len(rows)
    assert key[4] == (rows[-1]["timestamp"], rows[-1]["id"])
    assert key[5] == STATE_SIG
    assert seen == [(SID, None)]
    assert key[6] is None  # truncation_watermark
    assert key[7] is None  # truncation_boundary
    assert key[8] == MSG_LIMIT


def test_key_builder_carries_truncation_fields(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)
    s.truncation_watermark = 17
    s.truncation_boundary = "boundary-x"

    key = routes._display_streaming_merge_cache_key(s, msg_limit=MSG_LIMIT)

    assert key[6] == 17
    assert key[7] == "boundary-x"


def test_store_is_noop_when_key_unbuildable(routes_env, monkeypatch):
    routes = routes_env.routes
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session([{"role": "user", "timestamp": 1.0, "id": "a"}])  # no sidecar

    routes.store_streaming_merge_entry(
        s, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )

    assert routes._display_streaming_merge_cache == {}


def test_store_probe_roundtrip_returns_shallow_copies(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)
    merged = [
        {"role": "user", "content": "m0", "timestamp": 1.0, "id": "a"},
        {"role": "assistant", "content": "m1", "timestamp": 2.0, "id": "b"},
    ]

    routes.store_streaming_merge_entry(s, msg_limit=MSG_LIMIT, messages=merged)
    assert SID in routes._display_streaming_merge_cache

    hit = routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT)
    assert hit == merged
    # Mutating the served copy must not corrupt the cached rows.
    hit[0]["content"] = "MUTATED"
    hit.append({"role": "user", "content": "extra"})

    again = routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT)
    assert again[0]["content"] == "m0"
    assert len(again) == 2
    # msg_limit is part of the composite key: a different window must miss.
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT + 10) is None


def test_probe_skips_inactive_session(routes_env, monkeypatch):
    """Inactive sessions belong to the legacy display-merge cache, not this one."""
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    active = _make_session(rows, active=True)
    routes.store_streaming_merge_entry(
        active, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )

    inactive = _make_session(rows, active=False)
    assert routes.probe_streaming_merge_entry(inactive, msg_limit=MSG_LIMIT) is None


def test_probe_misses_after_ttl_and_evicts_entry(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)
    now = [1000.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])

    routes.store_streaming_merge_entry(
        s, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is not None

    now[0] += routes._DISPLAY_MERGE_STREAMING_TTL_SECONDS + 0.01
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is None
    assert SID not in routes._display_streaming_merge_cache  # expired entry evicted

    # Clock back inside the TTL: still a miss -- the entry is gone for good.
    now[0] = 1001.0
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is None


def test_evict_streaming_merge_entries(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)

    routes.store_streaming_merge_entry(
        s, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )
    assert SID in routes._display_streaming_merge_cache

    routes.evict_streaming_merge_entries(SID)
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is None
    assert SID not in routes._display_streaming_merge_cache
    routes.evict_streaming_merge_entries("20260101_000000_unknown")  # no raise


def test_evict_streaming_redact_entry_calls_helpers_pop(routes_env, monkeypatch):
    """Turn-end belt hook forwards to api.helpers._session_redact_cache_pop.

    That helper lands in Task 5; until then the getattr guard makes this a
    silent no-op, so the hook is wired here via monkeypatch (raising=False
    covers both the pre-Task-5 absence and the post-Task-5 real attribute).
    """
    import api.helpers as helpers

    routes = routes_env.routes
    calls = []
    monkeypatch.setattr(
        helpers, "_session_redact_cache_pop", lambda sid: calls.append(sid),
        raising=False,
    )

    routes.evict_streaming_redact_entry(SID)
    assert calls == [SID]

    routes.evict_streaming_redact_entry("")  # empty sid: no-op, no raise
    assert calls == [SID]


# ---------------------------------------------------------------------------
# Handler-level consume-path tests (Task 2): GET /api/session must probe the
# streaming merge cache for ACTIVE sessions on the tail-poll shape (msg_limit
# set, no msg_before) and skip the state.db row load on a hit — exactly like
# the inactive _display_cache_hit path does. msg_before paging bypasses every
# cache layer (#4070). The cache is populated here via store_streaming_merge_entry
# directly; the produce path itself (store on full merge) lands in Task 3.
# ---------------------------------------------------------------------------

ACTIVE_SID = "20260101_000000_streamact"
INACTIVE_SID = "20260101_000000_inact1"


@pytest.fixture()
def handler_env(routes_env, monkeypatch):
    """routes_env plus handler harness: storage backends off, call recording.

    Every real storage touch the GET handler could make is stubbed so a test
    failure means the handler logic diverged, not a missing fixture file.
    """
    env = routes_env
    routes = env.routes
    import api.models as models
    import api.session_ops as session_ops

    state_calls = []
    state_stub = SimpleNamespace(rows=[])

    def _record_state_rows(*args, **kwargs):
        state_calls.append((args, kwargs))
        # Mirror the sidecar rows when the test provides them: the stubbed
        # state.db agrees with the sidecar, so the legacy display-merge cache
        # path (which needs real state rows -- the merge helper short-circuits
        # on an empty row list without ever storing) exercises its full store
        # branch like prod.
        return [dict(m) for m in state_stub.rows]

    monkeypatch.setattr(routes, "get_state_db_session_messages", _record_state_rows)
    monkeypatch.setattr(models, "get_state_db_session_messages", _record_state_rows)
    # regeneration_state (handler cold-load path, non-truncated windows) reads
    # state.db directly via its own model import — pin it shut too.
    monkeypatch.setattr(
        models, "reconciled_state_db_messages_for_session",
        lambda *a, **k: [], raising=False,
    )
    monkeypatch.setattr(
        session_ops, "regeneration_state", lambda *a, **k: ([], [])
    )
    monkeypatch.setattr(
        session_ops, "regeneration_authority", lambda *a, **k: None
    )
    monkeypatch.setattr(routes, "find_run_summary", lambda *a, **k: None)
    monkeypatch.setattr(
        routes, "_run_journal_live_snapshot", lambda *a, **k: None
    )
    with routes._lineage_display_cache_lock:
        routes._lineage_display_cache.clear()
    yield SimpleNamespace(
        routes=routes,
        models=models,
        monkeypatch=monkeypatch,
        state_calls=state_calls,
        state_stub=state_stub,
    )
    with routes._lineage_display_cache_lock:
        routes._lineage_display_cache.clear()


def _handler_session(messages, *, sid=ACTIVE_SID, active=True):
    """Session stand-in carrying every attr the GET /api/session handler reads."""
    s = SimpleNamespace(
        session_id=sid,
        profile=None,
        title="Streaming handler test",
        workspace="/tmp",
        model="gpt-test",
        model_provider=None,
        messages=list(messages),
        tool_calls=[],
        input_tokens=0,
        output_tokens=0,
        estimated_cost=0,
        context_length=0,
        threshold_tokens=0,
        last_prompt_tokens=0,
        active_stream_id="stream-live" if active else None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        composer_draft={},
        anchor_activity_scenes=None,
        read_only=False,
        is_cli_session=False,
    )
    s.compact = lambda *a, **k: {
        "session_id": s.session_id,
        "title": s.title,
        "workspace": s.workspace,
        "message_count": len(s.messages),
        "context_length": s.context_length,
        "threshold_tokens": s.threshold_tokens,
        "last_prompt_tokens": s.last_prompt_tokens,
        "active_stream_id": s.active_stream_id,
        "pending_user_message": s.pending_user_message,
    }
    return s


def _tail_query(sid=ACTIVE_SID, msg_before=None):
    query = f"session_id={sid}&messages=1&resolve_model=0&msg_limit=50"
    if msg_before is not None:
        query += f"&msg_before={msg_before}"
    return query


def _run_session_handler(env, s, *, query, forbid_state_db_load=False,
                         forbid_full_merge=False):
    """Invoke GET /api/session against a fake session; return the session payload."""
    routes = env.routes
    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        return data

    if forbid_state_db_load:
        def _no_rows(*args, **kwargs):
            raise AssertionError(
                "state.db row load ran; a streaming cache hit must skip it"
            )

        env.monkeypatch.setattr(
            routes, "get_state_db_session_messages", _no_rows
        )
    if forbid_full_merge:
        def _no_merge(*args, **kwargs):
            raise AssertionError(
                "full append-only merge ran; the cache hit should be consumed"
            )

        env.monkeypatch.setattr(
            routes, "merge_session_messages_append_only", _no_merge
        )

    parsed = urlparse(f"/api/session?{query}")
    with patch.object(routes, "get_session", return_value=s), \
         patch.object(routes, "_clear_stale_stream_state", return_value=False), \
         patch.object(routes, "j", side_effect=fake_j), \
         patch.object(routes, "RequestDiagnostics"):
        routes.handle_get(SimpleNamespace(), parsed)
    return captured["data"]["session"]


def test_handler_active_tail_poll_serves_streaming_hit_without_state_db_load(
    handler_env,
):
    """(a) Active + msg_limit + populated cache: probe hit, no state.db load."""
    routes = handler_env.routes
    rows = _write_sidecar(handler_env, ACTIVE_SID, n=4)
    _patch_state_sig(handler_env, handler_env.monkeypatch)
    s = _handler_session(rows)

    first = _run_session_handler(
        handler_env, s, query=_tail_query(), forbid_full_merge=False
    )
    assert [m.get("content") for m in first["messages"]] == [
        "turn 0", "turn 1", "turn 2", "turn 3",
    ]

    merged = [dict(r, merged_from_cache="1") for r in rows]
    routes.store_streaming_merge_entry(s, msg_limit=50, messages=merged)

    second = _run_session_handler(
        handler_env,
        s,
        query=_tail_query(),
        forbid_state_db_load=True,
        forbid_full_merge=True,
    )
    assert second["messages"] == merged
    assert second["_messages_offset"] == 0
    assert second["_messages_truncated"] is False


def test_handler_active_msg_before_poll_bypasses_streaming_cache(handler_env):
    """(b) msg_before paging must bypass every cache layer (#4070 hard rule)."""
    routes = handler_env.routes
    rows = _write_sidecar(handler_env, ACTIVE_SID, n=4)
    _patch_state_sig(handler_env, handler_env.monkeypatch)
    s = _handler_session(rows)

    routes.store_streaming_merge_entry(
        s,
        msg_limit=50,
        messages=[
            {"role": "user", "content": "STREAM-CACHED", "timestamp": 9.0, "id": "c"},
        ],
    )

    payload = _run_session_handler(
        handler_env, s, query=_tail_query(msg_before=2)
    )

    assert [m.get("content") for m in payload["messages"]] == ["turn 0", "turn 1"]
    assert all(m.get("content") != "STREAM-CACHED" for m in payload["messages"])
    assert handler_env.state_calls, (
        "msg_before must take the full state.db load path, never a cache hit"
    )


def test_handler_active_no_msg_limit_takes_full_path(handler_env):
    """(c) Active + msg_limit=None: no streaming probe, full merge path."""
    routes = handler_env.routes
    rows = _write_sidecar(handler_env, ACTIVE_SID, n=2)
    _patch_state_sig(handler_env, handler_env.monkeypatch)
    s = _handler_session(rows)

    routes.store_streaming_merge_entry(
        s,
        msg_limit=None,
        messages=[
            {"role": "user", "content": "STREAM-CACHED", "timestamp": 9.0, "id": "c"},
        ],
    )

    payload = _run_session_handler(
        handler_env,
        s,
        query=f"session_id={ACTIVE_SID}&messages=1&resolve_model=0",
    )

    assert "STREAM-CACHED" not in json.dumps(payload.get("messages") or [])
    assert len(payload["messages"]) == 2
    assert len(handler_env.state_calls) == 1  # msg_limit=None branch loads rows


def test_handler_inactive_tail_poll_keeps_legacy_display_cache(handler_env):
    """(d) Inactive sessions keep the legacy cache; streaming entry untouched."""
    routes = handler_env.routes
    rows = _write_sidecar(handler_env, INACTIVE_SID, n=3)
    _patch_state_sig(handler_env, handler_env.monkeypatch)
    handler_env.state_stub.rows = rows
    s = _handler_session(rows, sid=INACTIVE_SID, active=False)

    routes.store_streaming_merge_entry(
        s,
        msg_limit=50,
        messages=[
            {"role": "user", "content": "STREAM-CACHED", "timestamp": 9.0, "id": "c"},
        ],
    )
    assert routes._display_streaming_merge_cache.get(INACTIVE_SID) is not None

    first = _run_session_handler(handler_env, s, query=_tail_query(sid=INACTIVE_SID))
    assert [m.get("content") for m in first["messages"]] == [
        "turn 0", "turn 1", "turn 2",
    ]  # the streaming entry was NOT served to an inactive session

    routes._display_merge_cache.clear()
    second = _run_session_handler(handler_env, s, query=_tail_query(sid=INACTIVE_SID))
    assert [m.get("content") for m in second["messages"]] == [
        "turn 0", "turn 1", "turn 2",
    ]
    assert len(routes._display_merge_cache) == 1  # legacy store path still live


def test_handler_streaming_hit_rows_are_shallow_copies(handler_env):
    """(e) Rows served from a streaming hit are copies: caller mutation is safe."""
    routes = handler_env.routes
    rows = _write_sidecar(handler_env, ACTIVE_SID, n=2)
    _patch_state_sig(handler_env, handler_env.monkeypatch)
    s = _handler_session(rows)

    merged = [
        {"role": "user", "content": "turn 0", "timestamp": 1000.0, "id": "row-0"},
        {"role": "assistant", "content": "turn 1", "timestamp": 1001.0, "id": "row-1"},
    ]
    routes.store_streaming_merge_entry(s, msg_limit=50, messages=merged)

    first = _run_session_handler(handler_env, s, query=_tail_query())
    served = first["messages"]
    assert served == merged

    # Mutate the served rows the way display-metadata attachment would.
    served[0]["content"] = "MUTATED-BY-CALLER"
    served.append({"role": "user", "content": "phantom"})

    second = _run_session_handler(handler_env, s, query=_tail_query())
    assert [m.get("content") for m in second["messages"]] == ["turn 0", "turn 1"]
    assert len(second["messages"]) == 2
