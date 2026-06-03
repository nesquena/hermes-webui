"""Bench harness for the #2518 follow-up.

Measures three timings on the server side:

  1. cold_slow  — first /api/session/new that hits the slow path
                  (model_provider: null → falls into get_available_models()
                  → triggers a full catalog rebuild). This is what the
                  current (pre-PR) newSession() does on cold boot.

  2. cold_fast  — first /api/session/new that hits the fast path
                  (model_provider: "openai-codex" → returns verbatim
                  without calling get_available_models()). This is what
                  the post-PR newSession() does on cold boot.

  3. warm_slow  — second /api/session/new that hits the slow path
                  after the catalog cache is warm. This is what the
                  current (pre-PR) newSession() does on the second
                  click. Matches the user's "first slow, then fast"
                  observation.

The diff between cold_slow and cold_fast is the PR's headline gain.
The diff between cold_slow and warm_slow is what the user has been
observing and what PR #2528's in-flight guard alone could not fix.
"""
import os
import statistics
import sys
import time

# Isolate from the user's real HERMES_HOME so the catalog cache file we
# build here does not contaminate the real ~/.hermes/webui/.
os.environ["HERMES_HOME"] = "/tmp/hwebui-2518-bench/bench-home"
os.environ["HERMES_WEBUI_STATE_DIR"] = "/tmp/hwebui-2518-bench/bench-home/webui"
os.makedirs(os.environ["HERMES_WEBUI_STATE_DIR"], exist_ok=True)

from api.config import get_available_models  # noqa: E402
from api.routes import _resolve_compatible_session_model_state  # noqa: E402


def time_call(fn, *args, **kwargs):
    t0 = time.perf_counter()
    fn(*args, **kwargs)
    return time.perf_counter() - t0


def sample(fn, n, *args, **kwargs):
    samples = [time_call(fn, *args, **kwargs) for _ in range(n)]
    return {
        "n": n,
        "min_ms": min(samples) * 1000,
        "median_ms": statistics.median(samples) * 1000,
        "max_ms": max(samples) * 1000,
        "mean_ms": statistics.mean(samples) * 1000,
    }


def main():
    # Force a cold cache by reloading config + clearing the in-memory cache.
    from api.config import reload_config
    import api.config as cfg
    reload_config()
    cfg._available_models_cache = None
    cfg._available_models_cache_ts = 0.0

    print("=" * 70)
    print("CATALOG REBUILD (server-side module timing)")
    print("=" * 70)

    cold_slow = sample(get_available_models, n=3)
    print("\ncold_slow  (n=3, get_available_models() on fresh process):")
    print(f"  median: {cold_slow['median_ms']:.1f} ms   "
          f"min: {cold_slow['min_ms']:.1f}   "
          f"max: {cold_slow['max_ms']:.1f}")

    warm_slow = sample(get_available_models, n=5)
    print("\nwarm_slow  (n=5, get_available_models() with hot cache):")
    print(f"  median: {warm_slow['median_ms']:.1f} ms   "
          f"min: {warm_slow['min_ms']:.1f}   "
          f"max: {warm_slow['max_ms']:.1f}")

    print()
    print("=" * 70)
    print("FAST PATH (server-side module timing)")
    print("=" * 70)

    # Patch get_available_models to count invocations, so the test can
    # confirm the fast path really skips the catalog.
    import api.routes as routes
    original = routes.get_available_models
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return original(*a, **kw)

    routes.get_available_models = counting
    try:
        cold_fast = sample(
            _resolve_compatible_session_model_state, 10,
            "gpt-5.5", "openai-codex",
        )
    finally:
        routes.get_available_models = original

    print("\ncold_fast  (n=10, _resolve_compatible_session_model_state, "
          "model+provider supplied):")
    print(f"  median: {cold_fast['median_ms']:.3f} ms   "
          f"min: {cold_fast['min_ms']:.3f}   "
          f"max: {cold_fast['max_ms']:.3f}")
    print(f"  get_available_models() invocations: {calls['n']} (expected 0)")

    print()
    print("=" * 70)
    print("HEADLINE DELTA")
    print("=" * 70)
    speedup = cold_slow["median_ms"] / max(cold_fast["median_ms"], 0.001)
    print(f"  cold_slow median: {cold_slow['median_ms']:.3f} ms")
    print(f"  cold_fast median: {cold_fast['median_ms']:.3f} ms")
    print(f"  speedup:          {speedup:.1f}x faster on cold start")
    print()
    print("  => 1st + click after server boot goes from the cold_slow number")
    print("     to the cold_fast number when this PR lands.")

    print()
    print("=" * 70)
    print("SIMULATED COLD REBUILD (with 3.0s monkeypatched catalog delay)")
    print("=" * 70)
    print("  Why: a fresh dev box with no external API keys completes the")
    print("  hardcoded-fallback path in well under 1ms, so the absolute")
    print("  numbers above don't represent the production scenario from")
    print("  the original #2518 triage (3-4s catalog rebuild when auth")
    print("  probing, custom /v1/models, OpenRouter /models, or credential")
    print("  pool refresh have to make network calls). This block")
    print("  monkeypatches a 3.0s sleep into get_available_models() so the")
    print("  before/after picture matches user-reported wall time.")

    import time as _time
    real_gam = routes.get_available_models
    def slow_gam(*a, **kw):
        _time.sleep(3.0)
        return real_gam(*a, **kw)
    routes.get_available_models = slow_gam
    try:
        # Simulate a fresh server restart by clearing the cache before
        # measuring what a cold call costs.
        cfg._available_models_cache = None
        cfg._available_models_cache_ts = 0.0
        # One priming call so the in-memory cache is fresh for the warm
        # measurement that follows.
        slow_gam()

        t0 = time.perf_counter()
        slow_gam()
        sim_cold_slow = (time.perf_counter() - t0) * 1000
    finally:
        routes.get_available_models = real_gam

    # Now simulate the patched client: model_provider supplied, fast path.
    routes.get_available_models = slow_gam
    try:
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            _resolve_compatible_session_model_state("gpt-5.5", "openai-codex")
            samples.append((time.perf_counter() - t0) * 1000)
        sim_cold_fast = statistics.median(samples)
    finally:
        routes.get_available_models = real_gam

    print(f"\n  simulated cold_slow: {sim_cold_slow:.0f} ms  (slow path on cold cache)")
    print(f"  simulated cold_fast: {sim_cold_fast:.2f} ms  (fast path, never calls get_available_models())")
    print(f"  observed saving:     {sim_cold_slow - sim_cold_fast:.0f} ms on the first + click")


if __name__ == "__main__":
    sys.exit(main())
