"""Regression tests — wakeup-turn model-resolve hang (t_46fadfbc).

Proven root cause (from a live thread-stack capture of a hung wakeup
turn, "Slow WebUI request still running"):

    _handle_chat_start → _resolve_compatible_session_model_state
      → get_available_models → _build_available_models_uncached
        → _read_live_provider_model_ids → get_copilot_api_token
          → exchange_copilot_token → urllib HTTPS → BLOCKED

A server-initiated Option-Z wakeup turn (drain thread, idle session, no
browser) reached chat/start with a COLD provider catalog and triggered a
LIVE per-provider rebuild whose Copilot token-exchange HTTPS call hung the
wakeup turn forever on this WSL/corp network — NOT a race.

Two fixes, two tests:

1. test_wakeup_turn_uses_persisted_model_no_live_probe
   start_session_turn(source="process_wakeup") resolves the model from the
   persisted session record via the cache-only path; the live provider
   rebuild (and the Copilot exchange) is never invoked even when the catalog
   is cold. The turn still starts with the persisted model.

2. test_chat_start_survives_slow_provider_probe
   Defense-in-depth: an unbounded/hanging provider probe cannot stall a
   foreground get_available_models() past the wall-clock budget — it falls
   back to a usable model list and lets the rebuild finish out-of-band.
"""

from __future__ import annotations

import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fix 1 — wakeup resolves persisted model with NO live provider probe
# ---------------------------------------------------------------------------

def test_wakeup_turn_uses_persisted_model_no_live_probe(monkeypatch):
    """Cold catalog + a session with a persisted model/provider: a
    server-side wakeup must start the turn using the PERSISTED model and must
    NOT call the live per-provider rebuild (which is what does the blocking
    Copilot token-exchange HTTPS call in the proven thread-stack).
    """
    from api import config as cfg
    import api.routes as routes

    sid = "sess-wakeup-persisted"
    persisted_model = "anthropic/claude-sonnet-4"
    fake_stream_id = "stream-wakeup-persisted-1"

    # HARD ASSERT: the live per-provider probe must never run on the wakeup
    # path. _read_live_provider_model_ids is the function the thread-stack
    # shows calling get_copilot_api_token → exchange_copilot_token.
    def _boom(*_a, **_k):
        raise AssertionError(
            "live provider rebuild ran on a wakeup turn — "
            "_read_live_provider_model_ids must NOT be called (it does the "
            "blocking Copilot token-exchange HTTPS call)"
        )

    monkeypatch.setattr(cfg, "_read_live_provider_model_ids", _boom, raising=True)
    # Cold cache: force the cache-miss branch that used to trigger the live
    # rebuild. The autouse conftest fixture already invalidates, but be
    # explicit so this test documents the precondition.
    cfg.invalidate_models_cache()

    captured: dict = {}

    def _fake_start_chat_stream_for_session(s, **kwargs):
        captured["model"] = kwargs.get("model")
        captured["model_provider"] = kwargs.get("model_provider")
        return {"stream_id": fake_stream_id, "session_id": s.session_id, "_status": 200}

    class _FakeSession:
        session_id = sid
        model = persisted_model
        model_provider = "anthropic"

    monkeypatch.setattr(
        routes, "_start_chat_stream_for_session",
        _fake_start_chat_stream_for_session, raising=True,
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: _FakeSession(), raising=True)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery",
        lambda s, w: "/tmp/ws", raising=True,
    )

    t0 = time.monotonic()
    resp = routes.start_session_turn(
        sid, "[IMPORTANT: Background process done]", source="process_wakeup"
    )
    elapsed = time.monotonic() - t0

    assert resp.get("stream_id") == fake_stream_id, "wakeup turn did not start"
    # The persisted model survives to the turn — never dropped, never blocked.
    assert captured["model"] == persisted_model, (
        f"wakeup turn used {captured.get('model')!r}, expected the persisted "
        f"session model {persisted_model!r}"
    )
    # Fast: no network. Cache-only resolution is sub-second; allow generous
    # slack for slow CI but well under the 10s Copilot timeout that was the
    # original symptom.
    assert elapsed < 5.0, (
        f"wakeup turn took {elapsed:.2f}s — a live provider probe likely ran"
    )


def test_wakeup_resolve_passes_prefer_cached_catalog(monkeypatch):
    """White-box: start_session_turn must route model resolution through the
    cache-only path (prefer_cached_catalog=True). Pins the wiring so a future
    refactor can't silently reintroduce the live-probe hang.
    """
    import api.routes as routes

    sid = "sess-wakeup-wiring"
    seen: dict = {}

    real = routes._resolve_compatible_session_model_state

    def _spy(model_id, model_provider=None, *, profile_provider=None,
             profile_default_model=None, profile_config=None,
             prefer_cached_catalog=False, **kwargs):
        seen["prefer_cached_catalog"] = prefer_cached_catalog
        # The wakeup path now also threads the session's profile model defaults
        # through (greptile fix) so a brand-new session with an empty model
        # falls back to the profile default, not the global DEFAULT_MODEL.
        seen["profile_provider"] = profile_provider
        seen["profile_default_model"] = profile_default_model
        seen["profile_config"] = profile_config
        return ("m", None, False)

    monkeypatch.setattr(
        routes, "_resolve_compatible_session_model_state", _spy, raising=True
    )
    monkeypatch.setattr(
        routes, "_start_chat_stream_for_session",
        lambda s, **k: {"stream_id": "x", "session_id": s.session_id, "_status": 200},
        raising=True,
    )

    class _FakeSession:
        session_id = sid
        model = "m"
        model_provider = None

    monkeypatch.setattr(routes, "get_session", lambda _s: _FakeSession(), raising=True)
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery",
        lambda s, w: "/tmp/ws", raising=True,
    )

    routes.start_session_turn(sid, "[IMPORTANT: x]", source="process_wakeup")
    assert seen.get("prefer_cached_catalog") is True, (
        "start_session_turn must resolve the model with "
        "prefer_cached_catalog=True so a wakeup never triggers a live probe"
    )
    assert real is not None  # the real function still exists (not deleted)


# ---------------------------------------------------------------------------
# Fix 2 — bounded rebuild: a slow probe cannot stall chat/start forever
# ---------------------------------------------------------------------------

def test_chat_start_survives_slow_provider_probe(monkeypatch):
    """Simulate a hanging provider probe on the NORMAL (prefer_cache=False)
    cold path: get_available_models() must return within the wall-clock
    budget with a usable fallback instead of blocking on the network.
    """
    from api import config as cfg

    cfg.invalidate_models_cache()
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.4, raising=True)

    started = {"n": 0}

    def _slow_rebuild(_builder):
        started["n"] += 1
        # >> budget — models the hung Copilot HTTPS call. 0.8s is 2× the
        # monkeypatched 0.4s budget, which is the smallest gap that still
        # robustly proves the contract while keeping suite wall-clock low
        # (was 3.0s; suite-latency cleanup per Copilot review).
        time.sleep(0.8)
        return {
            "active_provider": "anthropic",
            "default_model": "anthropic/claude-sonnet-4",
            "configured_model_badges": {},
            "groups": [],
        }

    # Replace the rebuild seam so no real per-provider network call happens
    # but the foreground still has to wait for (a stand-in for) it.
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_rebuild, raising=True)
    # Ensure no disk cache short-circuits the cold path.
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None, raising=True)

    t0 = time.monotonic()
    result = cfg.get_available_models()
    elapsed = time.monotonic() - t0

    assert started["n"] == 1, "the rebuild worker should have been started"
    assert elapsed < 2.0, (
        f"get_available_models() blocked {elapsed:.2f}s on a hung probe — "
        f"the {cfg._LIVE_REBUILD_BUDGET_SECONDS}s budget did not bound it"
    )
    # The fallback must be a structurally valid, usable catalog.
    assert isinstance(result, dict)
    for k in ("active_provider", "default_model", "configured_model_badges", "groups"):
        assert k in result, f"fallback catalog missing {k!r}"
    assert isinstance(result["groups"], list)


def test_minimal_static_catalog_is_network_free(monkeypatch):
    """The fallback catalog builder must never reach the live provider probe.
    """
    from api import config as cfg

    monkeypatch.setattr(
        cfg, "_read_live_provider_model_ids",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("minimal catalog must not probe providers")
        ),
        raising=True,
    )
    out = cfg._minimal_static_models_catalog()
    assert set(out) >= {
        "active_provider", "default_model", "configured_model_badges", "groups"
    }
    assert isinstance(out["groups"], list)


def test_prefer_cache_kw_exists_and_skips_live_rebuild(monkeypatch):
    """get_available_models(prefer_cache=True) must resolve without invoking
    the rebuild seam at all when the cache is cold.
    """
    from api import config as cfg

    cfg.invalidate_models_cache()
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None, raising=True)

    def _must_not_run(_b):
        raise AssertionError(
            "prefer_cache=True triggered the live rebuild seam — it must "
            "serve cache/minimal-static only"
        )

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _must_not_run, raising=True)

    result = cfg.get_available_models(prefer_cache=True)
    assert isinstance(result, dict)
    assert "default_model" in result and "groups" in result


# ---------------------------------------------------------------------------
# Source-grep wiring guards
# ---------------------------------------------------------------------------

def test_get_available_models_has_prefer_cache_param():
    import inspect

    from api import config as cfg

    sig = inspect.signature(cfg.get_available_models)
    assert "prefer_cache" in sig.parameters
    param = sig.parameters["prefer_cache"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False
    assert "_LIVE_REBUILD_BUDGET_SECONDS" in cfg.__dict__
    assert "_minimal_static_models_catalog" in cfg.__dict__


def test_start_session_turn_uses_cached_catalog():
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    # The wakeup entrypoint must resolve with the cache-only flag.
    i = src.find("def start_session_turn(")
    assert i != -1
    j = src.find("def _handle_process_complete_ack", i)
    body = src[i:j]
    assert "prefer_cached_catalog=True" in body, (
        "start_session_turn must pass prefer_cached_catalog=True"
    )
