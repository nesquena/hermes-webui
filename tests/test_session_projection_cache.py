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
   disables caching for that request instead of planting a stale entry;
8. a sidecar save landing after the session was loaded but before the key is
   built never caches the older body under the newer signature (a later GET
   would replay the old transcript);
9. a same-model config refresh that only changes the resolved context window is
   not served from an entry keyed under the old values;
10. a redaction-policy rotation (the rotate-on-registration path a plugin's
   ``register_redaction_patterns()`` triggers) is not served the pre-registration
   mask, and the real installed registry is honoured end to end where available;
11. a write to a stitched snapshot parent — child sidecar untouched — is not
   served the parent rows the entry was built from;
12. a supported backend transition that changes the gateway-chat controls
   reaches the payload even though the transcript is unchanged.
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
        timestamp_base = attrs.pop("_timestamp_base", 1_700_000_000.0)
        messages = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": text,
             "timestamp": timestamp_base + index}
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

    def get(self, sid, extra="", resolve_model=0):
        handler = _Handler()
        url = (
            f"/api/session?session_id={sid}&messages=1"
            f"&resolve_model={int(resolve_model)}{extra}"
        )
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


def test_a_save_after_the_load_never_caches_the_older_body(env, monkeypatch):
    """#8990 review 1: the entry is bound to the generation actually read.

    ``Session.load`` hands back the two-message transcript; a real ``save()``
    lands right after it. The request therefore built its body from generation
    N while the sidecar already holds N+1. Caching under the N+1 signature would
    make every later GET (which loads N+1) replay the two-message body.
    """
    import api.routes as routes

    env.save("proj_race", ["first question", "first reply"])
    real_get_session = env.models.get_session

    def _racing_get_session(sid, metadata_only=False):
        session = real_get_session(sid, metadata_only=metadata_only)
        if sid == "proj_race":
            # A concurrent writer commits the next two turns while this
            # request already holds the previous generation in memory.
            env.save(
                "proj_race",
                ["first question", "first reply", "second question", "second reply"],
            )
        return session

    monkeypatch.setattr(routes, "get_session", _racing_get_session)

    raced = env.get("proj_race")
    assert env.contents(raced) == ["first question", "first reply"], (
        "the raced request may only serve the generation it read"
    )
    assert routes._session_projection_cache == {}, (
        "a body built from a superseded generation must never be published"
    )

    monkeypatch.setattr(routes, "get_session", real_get_session)
    later = env.get("proj_race")

    assert env.contents(later) == [
        "first question", "first reply", "second question", "second reply",
    ], "the next GET must show the transcript that is now on disk"
    assert env.projection_calls == 2, "a mismatch must also disable reuse for that request"


def test_same_model_config_refresh_is_not_served_from_the_old_window(env, monkeypatch):
    """#8990 review 2: a cached hit must not freeze the resolved context window.

    Same model, same provider, unchanged sidecar — only the real config's
    context window moves 200k -> 400k, exactly what a normal config reload
    does. The uncached path returns the new window, so a hit must too.
    """
    import api.routes as routes

    env.save(
        "proj_window",
        ["hello"],
        model="deepseek-v4",
        model_provider="opencode-go",
        threshold_tokens=160_000,
    )
    window = {"value": 200_000}

    monkeypatch.setattr(
        routes,
        "_session_context_length_lookup_state",
        lambda model, provider: (model, provider, None, None),
    )
    monkeypatch.setattr(routes, "_session_context_lookup_uses_chat_route", lambda provider: False)
    monkeypatch.setattr(
        routes, "_resolve_effective_session_model_for_display", lambda session: "deepseek-v4"
    )
    monkeypatch.setattr(
        routes,
        "_resolve_effective_session_model_provider_for_display",
        lambda session: "opencode-go",
    )
    monkeypatch.setattr(
        routes,
        "_resolve_context_length_for_session_model",
        lambda model, provider, **kwargs: window["value"],
    )

    first = env.get("proj_window", resolve_model=1)
    assert (first["context_length"], first["threshold_tokens"]) == (200_000, 160_000)
    assert env.projection_calls == 1

    window["value"] = 400_000
    second = env.get("proj_window", resolve_model=1)

    assert (second["context_length"], second["threshold_tokens"]) == (400_000, 320_000), (
        "a config refresh must reach the payload even when the transcript is unchanged"
    )
    assert env.projection_calls == 2, "the old window must not be reused"


# A token shape the WebUI's fixed redactor patterns do not own, embedded in a
# string the prefilter trips on (the URL marker), so "masked" can only mean
# "the policy in force knows it".
_POLICY_TOKEN = "nvapi-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0"
_POLICY_TEXT = f"see https://example.invalid/docs then pasted {_POLICY_TOKEN} into chat"


def test_redaction_policy_rotation_invalidates_the_projection(env, monkeypatch):
    """#8990 review 3.1: a primed payload must not outlive the mask that built it.

    ``register_redaction_patterns()`` rotates the redaction memo to a fresh LRU
    exactly like the rebind simulated here; the payload an entry was masked
    under must stop being served the moment the policy in force changes.
    """
    import functools

    import api.helpers as helpers

    assert helpers._might_contain_sensitive_text(_POLICY_TEXT), (
        "the probe text must reach the redactor, or this test proves nothing"
    )
    env.save("proj_policy", [_POLICY_TEXT])

    first = env.get("proj_policy")
    assert _POLICY_TOKEN in env.contents(first)[0], "the priming mask did not know the token"
    assert env.projection_calls == 1

    def _masking_policy(text):
        return helpers._redact_fn_uncached(text).replace(_POLICY_TOKEN, "***")

    fresh = functools.lru_cache(maxsize=helpers._REDACT_CACHE_MAXSIZE)(_masking_policy)
    monkeypatch.setattr(helpers, "_redact_fn_lru", fresh)
    assert helpers.redaction_policy_fingerprint() is fresh, (
        "the policy fingerprint must follow the rotated memo"
    )
    assert _POLICY_TOKEN not in helpers._redact_fn_cached(_POLICY_TOKEN), (
        "the new policy is not effective, so the test proves nothing"
    )

    second = env.get("proj_policy")

    assert env.projection_calls == 2, (
        "a policy rotation must not be served the pre-rotation projection"
    )
    assert _POLICY_TOKEN not in env.contents(second)[0], (
        "the payload still carries the pre-registration mask"
    )


def test_registered_agent_pattern_reaches_a_primed_payload(env):
    """#8990 review 3.1, production registry: prime, register, next GET masks.

    Skipped where ``agent.redact`` is not importable; the rotation test above
    covers the same invalidation without the installed registry.
    """
    redact = pytest.importorskip("agent.redact", reason="hermes-agent not installed")
    register = getattr(redact, "register_redaction_patterns", None)
    reset = getattr(redact, "_reset_plugin_redaction_patterns", None)
    if register is None or reset is None:
        pytest.skip("installed agent.redact has no runtime pattern registry")

    import api.helpers as helpers

    pattern = r"nvapi-[A-Za-z0-9]{60}"
    assert helpers._might_contain_sensitive_text(_POLICY_TEXT)

    reset()
    try:
        env.save("proj_registry", [_POLICY_TEXT])
        first = env.get("proj_registry")
        assert _POLICY_TOKEN in env.contents(first)[0]
        assert env.projection_calls == 1

        assert register([pattern], source="test-8990") == 1

        # Oracle: the registry is effective for this text shape.
        assert _POLICY_TOKEN not in helpers._redact_fn_uncached(_POLICY_TEXT)

        second = env.get("proj_registry")
        assert _POLICY_TOKEN not in env.contents(second)[0], (
            "a registered pattern did not reach a previously primed payload"
        )
        assert env.projection_calls == 2
    finally:
        reset()


def test_parent_only_update_is_not_served_from_the_old_projection(env):
    """#8990 review 3.2: rows stitched from a snapshot parent are part of the key.

    Only the parent sidecar moves; the child's own signature, its sidecar and
    the state.db rows are untouched. The uncached path re-reads the parent, so a
    hit that replays the entry's parent rows is a stale display.
    """
    env.save("proj_parent", ["parent question", "parent answer"], pre_compression_snapshot=True)
    env.save(
        "proj_child",
        ["continuation question", "continuation answer"],
        parent_session_id="proj_parent",
        _timestamp_base=1_700_000_100.0,
    )

    first = env.get("proj_child")
    assert env.contents(first) == [
        "parent question",
        "parent answer",
        "continuation question",
        "continuation answer",
    ]
    assert env.projection_calls == 1

    # A writer updates the archived parent transcript only.
    env.save("proj_parent", ["parent question", "parent answer, corrected"], pre_compression_snapshot=True)

    second = env.get("proj_child")

    assert env.projection_calls == 2, (
        "a parent snapshot write must invalidate the child's cached projection"
    )
    assert env.contents(second) == [
        "parent question",
        "parent answer, corrected",
        "continuation question",
        "continuation answer",
    ], "the payload still carries the parent rows the entry was built from"


def test_backend_transition_reaches_a_cached_payload(env, monkeypatch):
    """#8990 review 3.3: the gateway-chat controls are recomputed, never frozen.

    The supported backend override is process-level configuration no sidecar key
    can fingerprint, so a payload primed under the legacy backend must not keep
    its unflagged controls after the switch. The transcript is unchanged: this
    is deliberately a cache hit.
    """
    import api.gateway_chat as gateway_chat

    env.save("proj_backend", ["hello"])
    monkeypatch.setattr(
        gateway_chat, "gateway_session_chat_read_only_reason", lambda *a, **k: None
    )

    first = env.get("proj_backend")
    assert "chat_read_only_reason" not in first
    assert env.projection_calls == 1

    reason = "This session is read-only: the gateway backend owns chat"
    monkeypatch.setattr(
        gateway_chat, "gateway_session_chat_read_only_reason", lambda *a, **k: reason
    )

    second = env.get("proj_backend")

    assert second.get("chat_read_only_reason") == reason, (
        "a backend transition must reach the payload even on a cache hit"
    )
    assert env.projection_calls == 1, "the transcript is unchanged: this must be a cache hit"
