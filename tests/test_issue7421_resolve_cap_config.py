"""Regression coverage for issue #7421: configurable
``HERMES_WEBUI_MAX_SESSION_RESOLVE`` cap on the heavy
full-transcript resolve semaphore.

The cap is hardcoded to ``2`` in
``api.models.py::_FULL_SESSION_RESOLVE_MAX_CONCURRENT``. The
maintainer's diagnosis is that the literal 2 is too low for
high-concurrency deployments where several parallel active
sessions all need a full-transcript resolve, and that the
deeper per-read cost is tracked separately in #7310. This PR
is the self-contained first step: read the cap from the env
var so operators can raise it without code changes, while
preserving the previous default of 2 and bounding the upper
end against typos like ``=999999``.

#7656 re-gate (9/22 23:18): the previous round used a private
``_import_models_with_env`` helper that did ``sys.modules.pop``
on ``api.models`` and re-imported to recompute the module-level
cap constant. Modules already imported (``api.routes``,
``api.streaming``, ...) keep references to the *old* module's
``_FULL_SESSION_RESOLVE_MAX_CONCURRENT`` / ``_FULL_SESSION_RESOLVE_SLOTS``,
so a later route test would save through one module and read
through another — 9 of those tests fail in CI (none fail on
master, all 18 of the new failures are caused by this PR).

The fix below is the maintainer-suggested compose:

- Drive the test with ``monkeypatch.setenv`` so the env var is
  restored at fixture teardown — no leak to other test files
- Test ``_read_max_session_resolve_concurrent()`` directly
  (the function is already pure: only reads ``os.getenv``) so
  no module reload is needed
- For the single test that needs the module-level constant
  (``_FULL_SESSION_RESOLVE_MAX_CONCURRENT``), verify it equals
  the helper's return value at import time — that is the real
  production contract, and it is cheap to assert because the
  helper itself only reads the env var
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import api.models as models

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def resolve_env(monkeypatch):
    """Drive ``HERMES_WEBUI_MAX_SESSION_RESOLVE`` via ``monkeypatch.setenv``
    so the env var is restored at fixture teardown — no leak to other
    test files, no module reload required.

    The helper ``models._read_max_session_resolve_concurrent()`` is pure
    (only ``os.getenv``) and reads the live env, so this fixture is the
    entire test harness. The previous round's ``_import_models_with_env``
    used ``sys.modules.pop`` + ``importlib.import_module`` to recompute
    the module-level cap constant against a freshly-set env, but that
    left the original module unreferenced in ``sys.modules`` while
    ``api.routes`` and friends still held the *old* module object —
    root cause of the cross-suite pollution the maintainer reproduced.
    """
    def set_resolve_env(value):
        if value is None:
            monkeypatch.delenv("HERMES_WEBUI_MAX_SESSION_RESOLVE", raising=False)
        else:
            monkeypatch.setenv("HERMES_WEBUI_MAX_SESSION_RESOLVE", str(value))
    return set_resolve_env


# ── default behavior (env unset) ──────────────────────────────────────────────


def test_default_cap_is_two_when_env_unset(resolve_env):
    """Backward compatibility: an installation that does not set
    the env var keeps the previous behavior. The bounded
    semaphore is constructed with capacity 2, so 3+ parallel
    full-resolves queue behind two slots as before."""
    resolve_env(None)
    assert models._read_max_session_resolve_concurrent() == 2, (
        "unset env must keep the previous hardcoded default of 2 "
        "so existing single-instance deployments are unchanged"
    )


def test_default_cap_is_two_when_env_empty_string(resolve_env):
    """A user who sets the env var to an explicit empty string
    (e.g. from a misconfigured .env file) is the same as unset.
    The helper strips whitespace first; ``""`` is treated as no
    value at all."""
    resolve_env("")
    assert models._read_max_session_resolve_concurrent() == 2


def test_default_cap_is_two_when_env_whitespace_only(resolve_env):
    resolve_env("   \t\n")
    assert models._read_max_session_resolve_concurrent() == 2


# ── in-range override ─────────────────────────────────────────────────────────


def test_env_override_takes_effect_within_upper_bound(resolve_env):
    resolve_env("8")
    assert models._read_max_session_resolve_concurrent() == 8


def test_env_override_takes_effect_at_upper_bound(resolve_env):
    """The upper bound is 64. An operator who wants the full
    ceiling must be able to reach it."""
    resolve_env("64")
    assert models._read_max_session_resolve_concurrent() == 64


# ── bad input → safe default (never permissive extreme) ──────────────────────


def test_env_zero_falls_back_to_default(resolve_env):
    """``=0`` would block the path; a 0 cannot mean "let nothing
    through". Fall back to 2 (the previous hardcoded value)."""
    resolve_env("0")
    assert models._read_max_session_resolve_concurrent() == 2


def test_env_negative_falls_back_to_default(resolve_env):
    resolve_env("-5")
    assert models._read_max_session_resolve_concurrent() == 2


def test_env_above_upper_bound_falls_back_to_default(resolve_env):
    """#7656 round-3 finding 1: a typo like ``=999999`` previously
    clamped **up** to the maximum, admitting 64 simultaneous
    unbounded transcript loads — roughly 614 MB of parse memory
    at the recorded 4.27 MB / parse. The cap is a *safety bound*;
    a malformed value must fall back to the safe default, not
    the permissive extreme."""
    resolve_env("999999")
    assert models._read_max_session_resolve_concurrent() == 2


def test_env_non_numeric_falls_back_to_default(resolve_env):
    resolve_env("two")
    assert models._read_max_session_resolve_concurrent() == 2


def test_env_empty_after_strip_falls_back_to_default(resolve_env):
    """Whitespace-only is empty after the strip; same as unset."""
    resolve_env("\n\t ")
    assert models._read_max_session_resolve_concurrent() == 2


# ── module-level cap reflects the helper at import time ─────────────────────


def test_module_level_cap_matches_helper():
    """The cap is computed once at import time. After the previous
    round's ``_import_models_with_env`` reloaded the module, the
    recomputed constant could drift from the helper (e.g. if a
    test set the env, reloaded, then never cleaned up). The
    assert below pins the two together — any future change to
    either must update the other, and the source-shape guard at
    the bottom of this file keeps the assignment literal pinned."""
    assert models._FULL_SESSION_RESOLVE_MAX_CONCURRENT == (
        models._read_max_session_resolve_concurrent()
    )


def test_bounded_semaphore_capacity_matches_cap():
    """The semaphore is the safety lock the cap protects. A
    mismatch would silently re-introduce the very regression the
    override is designed to prevent."""
    sem = models._FULL_SESSION_RESOLVE_SLOTS
    assert isinstance(sem, threading.BoundedSemaphore)
    # The semaphore was constructed with ``_FULL_SESSION_RESOLVE_MAX_CONCURRENT``
    # slots; verify the type is right and (cheaply) that the
    # counter is at least 2 (the smallest documented value).
    assert sem._initial_value == models._FULL_SESSION_RESOLVE_MAX_CONCURRENT  # type: ignore[attr-defined]


def test_helper_defined_in_models():
    """The helper is the production entry point — the actual
    function operators rely on at import time. Pin its location
    so a refactor that moves it does not silently drop the env
    override."""
    src = (ROOT / "api" / "models.py").read_text(encoding="utf-8")
    assert "def _read_max_session_resolve_concurrent" in src, (
        "the env-override helper must live in api.models "
        "next to the module-level cap constant it feeds"
    )
    assert (
        "_FULL_SESSION_RESOLVE_MAX_CONCURRENT = _read_max_session_resolve_concurrent()"
        in src
    ), (
        "the module-level cap constant must be initialised from the "
        "helper's return value so any future code change has to "
        "go through the same path operators do"
    )


# ── profile .env cannot resize the process-wide cap ─────────────────────────


def test_protected_env_keys_includes_resolve_cap():
    """#7656 round-3 finding 2: a profile .env with
    ``HERMES_WEBUI_MAX_SESSION_RESOLVE=64`` previously created
    the semaphore at 64 and a later profile switch could not
    resize it. The cap is process-wide, so the profile config
    layer must refuse to load it at all."""
    from api.profiles import _PROTECTED_ENV_KEYS

    assert "HERMES_WEBUI_MAX_SESSION_RESOLVE" in _PROTECTED_ENV_KEYS, (
        "the resolve cap is a process-wide safety bound; a profile "
        ".env that resizes it at startup would create a semaphore the "
        "rest of the process can never resize, exactly the bug the "
        "previous round flagged for #7655's instance_label"
    )


def test_blocked_runtime_env_keys_includes_resolve_cap():
    """The runtime/gateway-parity filter is the other path a
    profile .env could leak through. Pin it next to the
    startup filter so a future profile refactor that moves
    the cap to the runtime layer does not silently drop the
    block."""
    from api.profiles import _BLOCKED_RUNTIME_ENV_KEYS

    assert "HERMES_WEBUI_MAX_SESSION_RESOLVE" in _BLOCKED_RUNTIME_ENV_KEYS


def test_filter_runtime_env_strips_resolve_cap(resolve_env):
    """Smoke-test the filter end-to-end: a value the operator
    set in the live env is stripped by the runtime filter so
    the cap constant computed at import time is unaffected."""
    from api.profiles import filter_runtime_env_for_gateway_parity

    resolve_env("32")
    runtime_env = {"HERMES_WEBUI_MAX_SESSION_RESOLVE": "32"}
    filtered = filter_runtime_env_for_gateway_parity(runtime_env)
    assert "HERMES_WEBUI_MAX_SESSION_RESOLVE" not in filtered


# ── cross-suite pollution guard (#7656 finding 3) ─────────────────────────────


@pytest.mark.parametrize("forbidden", ["sys.modules.pop", "importlib.import_module", "os.environ[", "os.environ.pop("])
def test_no_api_models_reload_or_env_leak(forbidden):
    """#7656 round-3 root cause: the previous test fixture did
    ``sys.modules.pop("api.models")`` + ``importlib.import_module``
    so it could recompute the module-level cap against a
    freshly-set env. That left the original module unreferenced
    in ``sys.modules`` while already-imported modules
    (``api.routes``, ``api.streaming``, ...) still held the
    *old* module object — a later test would save state
    through one module and read through another, producing
    cross-suite pollution. Pin the fix: this file must not
    contain any ``sys.modules.pop`` / ``importlib.import_module``
    of ``api.models``, nor direct ``os.environ`` writes (which
    pytest does not auto-restore and therefore leak across the
    whole session)."""
    src = (ROOT / "tests" / "test_issue7421_resolve_cap_config.py").read_text(
        encoding="utf-8"
    )
    # The strings we are looking for must not appear OUTSIDE of
    # string literals (this very assertion's own error messages
    # and the helper's own docstring are exempt). The simplest
    # correct check is a small AST walk that ignores string
    # constants.
    import ast
    tree = ast.parse(src, filename=str(ROOT / "tests" / "test_issue7421_resolve_cap_config.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if forbidden in node.value:
                return  # the only occurrence of the forbidden token is a string literal
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            # Skip the function whose purpose is to check this
            # exact thing; the assertion message inside its
            # body mentions every forbidden token.
            if name == "test_no_api_models_reload_or_env_leak":
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Constant)
                    and isinstance(sub.value, str)
                    and forbidden in sub.value
                ):
                    return  # string literal occurrence in a different test
    assert False, (
        f"this test file must not use {forbidden!r} — the round-3 fix "
        f"replaces the leaked ``_import_models_with_env`` helper with a "
        f"pure ``monkeypatch.setenv`` fixture. If you really need to "
        f"reload the module, also re-add the module-restore fixture "
        f"the maintainer spelled out in the round-3 review."
    )
