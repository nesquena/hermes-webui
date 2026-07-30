"""Deterministic BEFORE/AFTER repro for the wakeup model-resolve hang.

Simulates the proven thread-stack: a server-initiated Option-Z wakeup turn
reaches start_session_turn with a COLD provider catalog; the live rebuild's
per-provider probe (the Copilot token-exchange HTTPS call) is monkeypatched to
hang. We drive start_session_turn directly (no browser, no real agent) and
measure how long model-resolution takes.

BEFORE (legacy behaviour: HERMES_WEBUI_MODELS_REBUILD_BUDGET=0 AND
prefer_cache forced off): the wakeup blocks on the hung probe — exactly the
"stuck at resolve_model_provider" symptom.

AFTER (shipped behaviour): start_session_turn resolves with
prefer_cached_catalog=True (never touches the live rebuild) AND the rebuild is
budget-bounded as defense-in-depth -- the wakeup turn starts in well under a
second using the persisted session model.

Run (from the repo root, with the Hermes Agent venv python -- any interpreter
that can import this repo's ``api`` package works):

    python tests/manual/repro_wakeup_hang.py

Exits 0 on PASS, 1 on FAIL. Not collected by pytest (see
tests/manual/conftest.py); this is an operator-run reproduction, not a test.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# Repo root is two levels up: <repo>/tests/manual/repro_wakeup_hang.py
_REPO_ROOT = _os.path.dirname(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
)
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)

import sys
import time

HANG_SECONDS = 30.0  # stand-in for an unreachable Copilot endpoint


def _install_fakes():
    import api.config as cfg
    import api.routes as routes

    cfg.invalidate_models_cache()

    # Inject the hang at the cold-rebuild seam. This is faithful to the proven
    # root cause (the live per-provider rebuild — which on the real host does
    # the Copilot token-exchange HTTPS call — is what blocks) and is
    # deterministic regardless of which providers the isolated HERMES_HOME has
    # configured (an isolated home with no providers can't reach the real
    # _read_live_provider_model_ids path; precedent t_9f0184cf).
    def _hung_rebuild(_builder):
        time.sleep(HANG_SECONDS)
        return {
            "active_provider": "anthropic",
            "default_model": "anthropic/claude-sonnet-4",
            "configured_model_badges": {},
            "groups": [],
        }

    cfg._invoke_models_rebuild = _hung_rebuild
    # No disk cache → forces the cold rebuild branch.
    cfg._load_models_cache_from_disk = lambda: None

    fake_stream = "stream-repro-1"
    captured = {}

    def _fake_start(s, **kwargs):
        captured["model"] = kwargs.get("model")
        return {"stream_id": fake_stream, "session_id": s.session_id, "_status": 200}

    class _FakeSession:
        session_id = "sess-repro"
        model = "anthropic/claude-sonnet-4"
        model_provider = "anthropic"

    routes._start_chat_stream_for_session = _fake_start
    routes.get_session = lambda _s: _FakeSession()
    routes._resolve_chat_workspace_with_recovery = lambda s, w: "/tmp/ws"
    return routes, captured, fake_stream


def _time_call(label, fn, timeout):
    import threading

    box = {}

    def _run():
        box["resp"] = fn()

    t = threading.Thread(target=_run, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(timeout=timeout)
    elapsed = time.monotonic() - t0
    if t.is_alive():
        print(f"  {label}: STUCK — still blocked after {elapsed:.1f}s "
              f"(the wakeup turn never starts; matches the bug symptom)")
        return None, elapsed, True
    print(f"  {label}: started in {elapsed:.3f}s "
          f"(model={box.get('resp', {}).get('stream_id')!r})")
    return box.get("resp"), elapsed, False


def main():
    print("=== BEFORE (legacy: budget=0, prefer_cache forced OFF) ===")
    import os
    os.environ["HERMES_WEBUI_MODELS_REBUILD_BUDGET"] = "0"
    # Reimport config so the budget constant picks up env=0.
    for m in ("api.config", "api.routes"):
        sys.modules.pop(m, None)
    routes, captured, _ = _install_fakes()

    # Force the legacy code path: bypass the prefer_cache short-circuit by
    # calling resolve with prefer_cached_catalog=False, like the OLD code did.
    _orig_resolve = routes._resolve_compatible_session_model_state
    routes._resolve_compatible_session_model_state = (
        lambda m, p, **_k: _orig_resolve(m, p, prefer_cached_catalog=False)
    )
    _, _, stuck_before = _time_call(
        "wakeup chat/start",
        lambda: routes.start_session_turn(
            "sess-repro", "[IMPORTANT: bg done]", source="process_wakeup"
        ),
        timeout=8.0,
    )

    print()
    print("=== AFTER (shipped: prefer_cached_catalog=True + bounded rebuild) ===")
    os.environ["HERMES_WEBUI_MODELS_REBUILD_BUDGET"] = "4"
    for m in ("api.config", "api.routes"):
        sys.modules.pop(m, None)
    import api.config as cfg2  # noqa: F401
    routes2, captured2, fake_stream = _install_fakes()
    resp, elapsed_after, stuck_after = _time_call(
        "wakeup chat/start",
        lambda: routes2.start_session_turn(
            "sess-repro", "[IMPORTANT: bg done]", source="process_wakeup"
        ),
        timeout=8.0,
    )

    print()
    print("=== RESULT ===")
    ok = (
        stuck_before is True
        and stuck_after is False
        and resp is not None
        and resp.get("stream_id") == fake_stream
        and captured2.get("model") == "anthropic/claude-sonnet-4"
        and elapsed_after < 2.0
    )
    print(f"  BEFORE stuck on hung probe: {stuck_before}")
    print(f"  AFTER  started fast:        {not stuck_after} "
          f"({elapsed_after:.3f}s, persisted model={captured2.get('model')!r})")
    print(f"  REPRO {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
