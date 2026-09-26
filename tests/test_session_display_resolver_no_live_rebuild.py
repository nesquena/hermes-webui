"""Regression: GET /api/session display resolvers must never trigger the
live provider-catalog rebuild.

Root cause (multi-tab streaming interlock RCA, task t_d127953d):
``_resolve_effective_session_model_for_display`` /
``_resolve_effective_session_model_provider_for_display`` are called by the
hot, side-effect-free ``GET /api/session?...&resolve_model=1`` path. When a
session has no persisted ``model_provider`` (common — e.g. kanban/imported
sessions), the fast path in ``_resolve_compatible_session_model_state`` is
skipped and the resolver fell through to ``get_available_models()`` WITHOUT
``prefer_cache``. On a non-AWS / WSL / corp network that cold rebuild blocks
~10s on a botocore IMDS probe (plus anthropic/openrouter /models) and, run
concurrently across browser tabs, serializes on the models-cache lock and
starves SSE/streaming -> BrokenPipe/Cancelled storm.

This is an INVARIANT test, not a change-detector: it asserts the resolvers
resolve from the cache-only path and never reach the live-rebuild seam
``api.config._invoke_models_rebuild`` — regardless of whether the session
carries a model_provider.
"""

import ast
import inspect

import pytest

import api.config as cfg
import api.routes as routes


class _FakeSession:
    """Minimal stand-in for a Session row as seen by the display resolvers."""

    def __init__(self, model, model_provider):
        self.model = model
        self.model_provider = model_provider


@pytest.fixture
def cold_models_cache(monkeypatch):
    """Force a cold in-memory + disk models cache without touching real state.

    Cold cache is what makes the regression observable: a warm cache short-
    circuits before any rebuild decision, hiding the prefer_cache contract.
    """
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    # Never read/write the real on-disk cache during the test.
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", lambda: None)
    yield


@pytest.fixture
def rebuild_seam_tripwire(monkeypatch):
    """Make the live provider-catalog rebuild seam fail loudly if reached.

    ``_invoke_models_rebuild`` is the documented indirection seam around the
    cold, network-touching per-provider rebuild. The display resolvers must
    never reach it (prefer_cache returns the network-free minimal catalog
    *before* this seam). If a future edit drops ``prefer_cached_catalog=True``,
    the resolver falls into the cold rebuild and trips this wire.
    """
    calls = {"n": 0}

    def _boom(_builder):
        calls["n"] += 1
        raise AssertionError(
            "live provider-catalog rebuild ran on the hot GET /api/session "
            "display path — prefer_cached_catalog regression"
        )

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _boom)
    return calls


@pytest.mark.parametrize(
    "model_provider",
    [None, "", "anthropic"],
    ids=["no-provider", "empty-provider", "with-provider"],
)
def test_session_display_resolvers_never_trigger_live_rebuild(
    cold_models_cache, rebuild_seam_tripwire, model_provider
):
    session = _FakeSession("claude-opus-4-7", model_provider)

    # Must not raise (the tripwire raises AssertionError if the live rebuild
    # path is entered) and must return the persisted model verbatim.
    model = routes._resolve_effective_session_model_for_display(session)
    provider = routes._resolve_effective_session_model_provider_for_display(session)

    assert model == "claude-opus-4-7"
    # provider is best-effort; the contract under test is "no live rebuild",
    # not a specific provider string. It must at least be None or a str.
    assert provider is None or isinstance(provider, str)
    assert rebuild_seam_tripwire["n"] == 0


class _WaitTripwire:
    """Proxy a Condition: raise if wait_for is used, or record the wait."""

    def __init__(self, target, *, allow_wait=False):
        self._target = target
        self._allow_wait = allow_wait
        self.wait_calls = []

    def wait_for(self, predicate, timeout=None):
        self.wait_calls.append(timeout)
        if not self._allow_wait:
            raise AssertionError(
                "display resolution attempted to wait on the catalog rebuild"
            )
        if callable(predicate):
            try:
                predicate()
            except Exception:
                pass
        return True

    def __enter__(self):
        return self._target.__enter__()

    def __exit__(self, *exc_info):
        return self._target.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._target, name)


def _force_catalog_lookup(monkeypatch):
    """Skip the model+provider fast path so the catalog wait-gate is reached."""
    monkeypatch.setattr(
        routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, None)
    )
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)
    try:
        monkeypatch.setattr(
            cfg, "_cfg_mtime", cfg._get_config_path().stat().st_mtime, raising=False
        )
    except Exception:
        pass


def test_display_resolver_does_not_wait_on_inflight_rebuild(monkeypatch):
    """Observable contract: a display lookup must not join an in-flight rebuild."""
    _force_catalog_lookup(monkeypatch)
    monkeypatch.setattr(cfg, "_cache_build_cv", _WaitTripwire(cfg._cache_build_cv))

    session = _FakeSession("claude-opus-4-7", None)
    model = routes._resolve_effective_session_model_for_display(session)
    provider = routes._resolve_effective_session_model_provider_for_display(session)

    assert model
    assert provider is None or isinstance(provider, str)


class _AliasPredicateCase:
    """A single alias-equal vs cross-provider decision for the no-wait hint predicate."""

    __slots__ = ("hint", "requested", "expected", "label")

    def __init__(self, hint, requested, expected, label):
        self.hint = hint
        self.requested = requested
        self.expected = expected
        self.label = label


@pytest.mark.parametrize(
    "case",
    [
        # The exact defect from greptile P1 (2026-09-25): a persisted
        # ``@anthropic:claude-opus-4.7`` with ``model_provider="claude"`` was
        # wrongly treated as a cross-provider pair because the previous
        # predicate canonicalized only the hint side. Mirror the test in both
        # directions (alias-on-hint and alias-on-requested) to lock the fix.
        _AliasPredicateCase(
            "anthropic", "claude", True,
            "P1 regression: hint=canonical-anthropic, requested=alias-claude",
        ),
        _AliasPredicateCase(
            "claude", "anthropic", True,
            "symmetric: hint=alias-claude, requested=canonical-anthropic",
        ),
        # Other alias pairs the WebUI ships — must also be preserved, not
        # repaired, on the no-wait display path.
        _AliasPredicateCase("copilot", "github", True, "alias: copilot/github"),
        _AliasPredicateCase("github", "copilot", True, "alias: github/copilot"),
        _AliasPredicateCase("gemini", "google", True, "alias: gemini/google"),
        _AliasPredicateCase("google", "gemini", True, "alias: google/gemini"),
        # Raw-equal and first-party identities must still match.
        _AliasPredicateCase("openai", "openai", True, "raw-equal: openai/openai"),
        _AliasPredicateCase("anthropic", "anthropic", True, "raw-equal: anthropic/anthropic"),
        # A genuinely cross-provider pair must STILL fall through to the
        # compatibility-repair path (return False so the caller repairs it).
        _AliasPredicateCase("ollama", "kilocode", False, "cross-provider: must repair"),
        _AliasPredicateCase("anthropic", "openai", False, "cross-provider: must repair"),
        _AliasPredicateCase("copilot", "openai-codex", False, "cross-provider: must repair"),
        # Empty/None inputs are never preservable.
        _AliasPredicateCase("", "claude", False, "empty hint"),
        _AliasPredicateCase("claude", "", False, "empty requested"),
        _AliasPredicateCase(None, "claude", False, "None hint"),
        _AliasPredicateCase("claude", None, False, "None requested"),
    ],
    ids=lambda c: c.label,
)
def test_non_authoritative_hint_matches_requested_provider_mirrors_authoritative_chain(case):
    """The no-wait display predicate must mirror ``hint_matches_active``.

    P1 (greptile 2026-09-25): the previous one-sided canonicalization
    (``_resolve_alias(hint) == requested``) compared the canonical form of the
    HINT against the RAW form of the requested provider, so a hint of
    ``"anthropic"`` paired with a requested of ``"claude"`` was wrongly
    treated as a cross-provider pair and repaired to the catalog default
    during a no-wait display lookup. The authoritative
    ``hint_matches_active`` chain canonicalizes the active-provider side on
    every non-raw-equal clause; this predicate must do the same on the
    requested-provider side. Asserts both that the alias-equal defect is fixed
    AND that a genuinely cross-provider pair still returns False (i.e. the
    compatibility-repair path is preserved).
    """
    got = routes._non_authoritative_hint_matches_requested_provider(
        case.hint, case.requested
    )
    assert got is case.expected, (
        f"{case.label}: hint={case.hint!r} requested={case.requested!r} "
        f"-> {got!r}, expected {case.expected!r}"
    )


def test_non_authoritative_preserves_alias_equal_persisted_pair(monkeypatch):
    """End-to-end: a non-authoritative no-wait display must preserve an
    alias-equal persisted ``@provider:model`` / ``model_provider`` pair.

    P1 (greptile 2026-09-25): with a non-authoritative catalog and a
    session storing ``@anthropic:claude-opus-4.7`` / ``claude`` (a pair that
    is alias-equal because ``claude`` is an alias of ``anthropic``), the
    resolver previously returned the catalog default — a silent
    cross-provider repair of a valid selection. The display result becomes
    the browser's persisted pair (static/sessions.js:2994-2999) and is sent
    as the next turn's routing state, so a single bad no-wait response can
    silently reroute a subsequent /api/chat/start call.

    This exercises the real ``_resolve_compatible_session_model_state``
    resolver with a minimal non-authoritative catalog and asserts the
    persisted pair is returned unchanged (no repair, model_string preserved).
    """
    _force_catalog_lookup(monkeypatch)
    # Minimal non-authoritative catalog: lacks the anthropic group, so
    # ``@anthropic:claude-opus-4.7`` is not in the snapshot. The active
    # provider is intentionally a DIFFERENT first-party (openai-codex) so
    # that, before the fix, the alias-asymmetric predicate would treat the
    # pair as a stale cross-provider artifact and repair to openai-codex.
    minimal_catalog = {
        "groups": [
            {
                "provider_id": "openai-codex",
                "models": [{"id": "gpt-5.5"}],
            }
        ],
        "active_provider": "openai-codex",
        "default_model": "gpt-5.5",
        "_non_authoritative": True,
    }
    monkeypatch.setattr(cfg, "get_available_models", lambda *a, **kw: minimal_catalog)

    session = _FakeSession("@anthropic:claude-opus-4.7", "claude")
    # Exercise the real resolver directly so we can observe the
    # (model, provider, changed) tuple and assert the persistence-vs-repair
    # decision that the display path ultimately returns.
    model, provider, changed = routes._resolve_compatible_session_model_state(
        "@anthropic:claude-opus-4.7",
        "claude",
        prefer_cached_catalog=True,
        wait_for_inflight_rebuild=False,
    )
    # And assert the display wrappers (the only observable surface on
    # GET /api/session) return the same persisted pair to the browser.
    display_model = routes._resolve_effective_session_model_for_display(session)
    display_provider = routes._resolve_effective_session_model_provider_for_display(session)

    # The persisted pair is alias-equal (claude ↔ anthropic) and the hint
    # is statically known. After the fix the no-wait path returns it
    # unchanged so the browser's echo does not silently reroute.
    assert changed is False, (
        f"non-authoritative no-wait path repaired an alias-equal pair "
        f"(model={model!r}, provider={provider!r}, changed={changed!r})"
    )
    assert model == "@anthropic:claude-opus-4.7", (
        f"@provider:model hint was stripped or replaced: got {model!r}"
    )
    assert provider == "claude", (
        f"alias-equal pair was repaired to a different provider: got {provider!r}"
    )
    # Display wrappers must surface the same pair the resolver decided on.
    assert display_model == "@anthropic:claude-opus-4.7", (
        f"display resolver returned a different model than the resolver: "
        f"got {display_model!r}"
    )
    assert display_provider == "claude", (
        f"display resolver returned a different provider than the resolver: "
        f"got {display_provider!r}"
    )


def test_wakeup_resolution_joins_inflight_rebuild(monkeypatch):
    """Observable contract: wakeup routing must join an in-flight rebuild."""
    _force_catalog_lookup(monkeypatch)
    cv = _WaitTripwire(cfg._cache_build_cv, allow_wait=True)
    monkeypatch.setattr(cfg, "_cache_build_cv", cv)

    sid = "sess-wakeup-wait"
    captured = {}

    class _Sess:
        session_id = sid
        model = "claude-opus-4-7"
        model_provider = None

    monkeypatch.setattr(routes, "get_session", lambda _sid: _Sess())
    monkeypatch.setattr(
        routes, "_resolve_chat_workspace_with_recovery", lambda s, w: "/tmp/ws"
    )
    monkeypatch.setattr(
        routes,
        "_start_chat_stream_for_session",
        lambda s, **k: captured.update(k) or {
            "stream_id": "stream-wakeup-wait",
            "session_id": s.session_id,
            "_status": 200,
        },
    )

    resp = routes.start_session_turn(
        sid, "[IMPORTANT: bg done]", source="process_wakeup"
    )

    assert resp.get("stream_id") == "stream-wakeup-wait"
    assert cv.wait_calls, (
        "wakeup resolution did not wait for the in-flight catalog rebuild"
    )


def _has_prefer_cached_catalog_true_call(fn) -> bool:
    tree = ast.parse(inspect.getsource(fn))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_resolve_compatible_session_model_state":
            continue
        for keyword in node.keywords:
            if keyword.arg == "prefer_cached_catalog" and isinstance(
                keyword.value, ast.Constant
            ):
                return keyword.value.value is True
    return False


def _has_display_no_wait_flag(fn) -> bool:
    """Static guard: display resolvers must pass wait_for_inflight_rebuild=False.

    Pure display resolution is NOT routing-authoritative (its result never
    starts a run), so it must skip the rebuild wait AND the rebuild lock
    entirely. This pins the explicit call-site flag so a refactor cannot
    silently hand the hot GET /api/session path back a multi-second stall
    during an in-flight rebuild (re-review #7568, CORE 2).
    """
    tree = ast.parse(inspect.getsource(fn))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_resolve_compatible_session_model_state":
            continue
        for keyword in node.keywords:
            if keyword.arg == "wait_for_inflight_rebuild" and isinstance(
                keyword.value, ast.Constant
            ):
                return keyword.value.value is False
    return False


def test_resolver_signature_passes_prefer_cached_catalog():
    """Static guard: both resolvers must opt into the cache-only catalog.

    A pure behavioural test can be satisfied by an unrelated short-circuit;
    this pins the explicit contract at the call site so the intent survives
    refactors.
    """
    assert _has_prefer_cached_catalog_true_call(
        routes._resolve_effective_session_model_for_display
    )
    assert _has_prefer_cached_catalog_true_call(
        routes._resolve_effective_session_model_provider_for_display
    )
    # Pure-display call sites must also explicitly skip the rebuild
    # wait/lock (routing-authoritative paths like the wakeup keep the
    # default True — see test_prefer_cache_no_wait / repair tests).
    assert _has_display_no_wait_flag(
        routes._resolve_effective_session_model_for_display
    )
    assert _has_display_no_wait_flag(
        routes._resolve_effective_session_model_provider_for_display
    )


def _has_wakeup_keeps_wait_flag(fn) -> bool:
    """Static guard: the server-initiated wakeup must NOT opt out of the
    rebuild wait.

    ``_start_process_wakeup_turn`` resolves a model/provider that flows into
    ``_start_chat_stream_for_session`` and starts a REAL agent run — it is
    routing-authoritative. Unlike the pure-display resolvers it must keep
    the default wait_for_inflight_rebuild=True (no explicit False anywhere
    at its call site, and no wait_for_inflight_rebuild=False keyword).
    """
    tree = ast.parse(inspect.getsource(fn))
    found_call = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_resolve_compatible_session_model_state":
            continue
        found_call = True
        for keyword in node.keywords:
            if keyword.arg == "wait_for_inflight_rebuild":
                # Any explicit value is a regression: the wakeup must keep
                # the default True (never pass False).
                return False
    return found_call


def test_wakeup_resolution_keeps_rebuild_wait():
    """The wakeup resolver must not adopt the display no-wait contract."""
    assert _has_wakeup_keeps_wait_flag(routes.start_session_turn)
