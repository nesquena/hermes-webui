"""Validity tests for the memoized GET /api/session projected payload.

GET /api/session runs ``public_session_projection()`` on every request, which
deep-copies and credential-masks the whole transcript (0.74 s for a
500-message session). ``_session_projection_cache`` memoizes that payload. These
tests drive the real HTTP handler (like the neighbouring session-GET tests) and
assert the properties the cache must have to be safe:

1. an unchanged session reuses the projection and returns the same payload;
2. any sidecar write invalidates the entry;
3. an active/streaming session is never served from the cache;
4. the two live-state keys are refreshed on a hit instead of being frozen;
5. the LRU is bounded and evicts oldest-first;
6. state.db growth invalidates the entry even when the sidecar is untouched;
7. an unstable state.db read (a commit landing between the bracketing probes)
   disables caching for that request instead of planting a stale entry.
"""

from __future__ import annotations

import json
from io import BytesIO
from urllib.parse import urlparse

import pytest


class _Handler:
    """Minimal BaseHTTPRequestHandler stand-in that captures the JSON body."""

    def __init__(self):
        self.status = None
        self.response_headers = []
        self.wfile = BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode() or "{}")


class _Env:
    """Test rig: temp session dir, patched handler dependencies, call counter."""

    def __init__(self, tmp_path, monkeypatch):
        import api.helpers as helpers
        import api.models as models
        import api.routes as routes
        import api.session_ops as session_ops

        self.models = models
        self.routes = routes
        self.sess_dir = tmp_path / "sessions"
        self.sess_dir.mkdir()
        monkeypatch.setattr(models, "SESSION_DIR", self.sess_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", self.sess_dir / "_index.json")
        monkeypatch.setattr(routes, "SESSION_DIR", self.sess_dir)
        models.SESSIONS.clear()

        # Process-global caches: every test starts from an empty, deterministic
        # state. The assertions below never read them; they go through the
        # handler so the public behaviour is what is under test.
        for _name in ("_session_projection_cache", "_lineage_display_cache", "_display_merge_cache"):
            _cache = getattr(routes, _name, None)
            if _cache is not None:
                _cache.clear()

        self.projection_calls = 0
        self.memory_status = {"value": {"level": "ok", "detail": "gen-1"}}
        self.regeneration_revision = {"value": "rev-1"}
        self.db_sig = {"value": ("db", 1), "sequence": None}
        self.state_db_rows = {"value": []}

        real_projection = helpers.public_session_projection

        def _counting_projection(raw):
            self.projection_calls += 1
            return real_projection(raw)

        def _db_signature(session_id, profile=None):
            sequence = self.db_sig.get("sequence")
            if sequence is not None:
                return next(sequence)
            return self.db_sig["value"]

        monkeypatch.setattr(routes, "public_session_projection", _counting_projection)
        monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda profile, handler: True)
        monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda session: None)
        monkeypatch.setattr(
            routes,
            "get_state_db_session_messages",
            lambda sid, **kwargs: list(self.state_db_rows["value"]),
        )
        monkeypatch.setattr(routes, "_state_db_session_signature", _db_signature)
        monkeypatch.setattr(
            routes,
            "session_memory_status",
            lambda sid, session=None: self.memory_status["value"],
        )
        monkeypatch.setattr(session_ops, "regeneration_state", lambda session: ([], {}))
        monkeypatch.setattr(
            session_ops,
            "regeneration_authority",
            lambda session, **kwargs: self.regeneration_revision["value"],
        )

    def save(self, sid, contents, **attrs):
        """Persist a session sidecar the way a real save would."""
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": text,
             "timestamp": 1_700_000_000.0 + index}
            for index, text in enumerate(contents)
        ]
        session = self.models.Session(
            session_id=sid,
            title=f"Session {sid}",
            messages=messages,
            context_length=200_000,
            **attrs,
        )
        session.save()
        self.models.SESSIONS.pop(sid, None)
        return session

    def get(self, sid, extra=""):
        handler = _Handler()
        url = f"/api/session?session_id={sid}&messages=1&resolve_model=0{extra}"
        self.routes.handle_get(handler, urlparse(url))
        assert handler.status in (None, 200), f"unexpected status {handler.status}"
        payload = handler.payload()
        assert "session" in payload, payload
        return payload["session"]

    @staticmethod
    def contents(session_payload):
        return [m.get("content") for m in session_payload.get("messages") or []]


@pytest.fixture
def env(tmp_path, monkeypatch):
    return _Env(tmp_path, monkeypatch)


def test_repeated_get_reuses_projection_and_returns_identical_payload(env):
    env.save("proj_idle", ["hello", "hi there"])

    first = env.get("proj_idle")
    assert env.projection_calls == 1

    second = env.get("proj_idle")
    assert env.projection_calls == 1, "an unchanged session must reuse the projection"
    assert second == first, "cache hit must return the exact payload of the miss"
    assert env.contents(second) == ["hello", "hi there"]


def test_sidecar_write_invalidates_projection(env):
    env.save("proj_write", ["first question", "first reply"])
    first = env.get("proj_write")
    assert env.projection_calls == 1
    assert env.contents(first) == ["first question", "first reply"]

    # A real write changes the sidecar's stat signature (and, in memory, the
    # transcript the payload is built from).
    env.save("proj_write", ["first question", "first reply", "second question",
                            "a much longer second reply that changes the file size"])
    second = env.get("proj_write")

    assert env.projection_calls == 2, "a sidecar write must not reuse the cached projection"
    assert env.contents(second)[-2:] == ["second question",
                                        "a much longer second reply that changes the file size"]


def test_active_session_is_never_served_from_cache(env):
    env.save("proj_stream", ["start"])
    env.get("proj_stream")
    assert env.projection_calls == 1

    # The session is streaming: its in-memory tail runs ahead of its sidecar.
    session = env.models.get_session("proj_stream")
    session.active_stream_id = "stream-live-1"
    env.models.SESSIONS["proj_stream"] = session

    env.get("proj_stream")
    env.get("proj_stream")
    assert env.projection_calls == 3, "an active session must recompute on every request"


def test_pending_user_message_is_never_served_from_cache(env):
    env.save("proj_pending", ["start"])

    # The session holds an unanswered turn: its projected payload depends on
    # in-memory state, so even an unchanged request must recompute.
    session = env.models.get_session("proj_pending")
    session.pending_user_message = "waiting for the agent"
    session.pending_started_at = 1_700_000_100.0
    env.models.SESSIONS["proj_pending"] = session

    env.get("proj_pending")
    env.get("proj_pending")
    assert env.projection_calls == 2, "a pending turn must never be served from the cache"


def test_volatile_keys_reflect_live_state_on_cache_hit(env):
    env.save("proj_volatile", ["hello"])
    first = env.get("proj_volatile")
    assert first["memory_status"] == {"level": "ok", "detail": "gen-1"}
    assert first["regeneration_revision"] == "rev-1"
    assert env.projection_calls == 1

    env.memory_status["value"] = {"level": "warn", "detail": "gen-2"}
    env.regeneration_revision["value"] = "rev-2"

    second = env.get("proj_volatile")
    assert env.projection_calls == 1, "the transcript is unchanged, so this must be a cache hit"
    assert second["memory_status"] == {"level": "warn", "detail": "gen-2"}, (
        "memory_status must come from live agent state, not the cached payload"
    )
    assert second["regeneration_revision"] == "rev-2", (
        "regeneration_revision must come from live agent state, not the cached payload"
    )
    assert env.contents(second) == ["hello"]

    # A key that disappears from live state must disappear from the payload too.
    env.memory_status["value"] = None
    third = env.get("proj_volatile")
    assert "memory_status" not in third


def test_state_db_growth_invalidates_projection(env):
    env.save("proj_db", ["hello"])
    env.get("proj_db")
    assert env.projection_calls == 1

    # Rows commit before the debounced sidecar save: the sidecar is untouched,
    # only the state.db revision moves.
    env.state_db_rows["value"] = [
        {"role": "user", "content": "committed before the save", "timestamp": 1_700_000_500.0}
    ]
    env.db_sig["value"] = ("db", 2)
    payload = env.get("proj_db")

    assert env.projection_calls == 2, "state.db growth must invalidate the cached projection"
    assert "committed before the save" in env.contents(payload)


def test_unstable_state_db_read_disables_caching(env):
    env.save("proj_unstable", ["hello"])
    # A commit lands between the before/after probes on every request.
    env.db_sig["sequence"] = iter([("db", 1), ("db", 2)] * 10)

    env.get("proj_unstable")
    env.get("proj_unstable")

    assert env.projection_calls == 2, (
        "an unbracketed state.db read must not populate the cache"
    )


def test_cache_is_bounded_and_evicts_oldest_first(env):
    import api.routes as routes

    # Hardcoded: the advertised bound is 8 entries.
    bound = 8
    assert routes._SESSION_PROJECTION_CACHE_MAX == bound

    sids = [f"proj_lru_{index}" for index in range(bound + 1)]
    for sid in sids:
        env.save(sid, [f"content for {sid}"])
        env.get(sid)
    assert env.projection_calls == len(sids)

    # One entry over the bound: the oldest (the first sid) must be gone.
    env.get(sids[0])
    assert env.projection_calls == len(sids) + 1, "the oldest entry must have been evicted"
    assert len(routes._session_projection_cache) <= bound

    # A recently used entry survives.
    env.get(sids[-1])
    assert env.projection_calls == len(sids) + 1, "the newest entry must still be cached"
