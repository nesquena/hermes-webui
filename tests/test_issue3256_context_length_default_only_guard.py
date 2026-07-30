"""Regression coverage for #3256 / #3263: the global model.context_length cap
must apply ONLY when the session model equals model.default.

Bug: a global `model.context_length` (set for the default model, e.g. 232000)
was applied to EVERY model, silently shrinking a non-default model's real
context window (e.g. a 1M-context variant). The fix scopes the override to
`model.default` only, in `_resolve_context_length_for_session_model`.

This test drives the real resolver with a monkeypatched
`agent.model_metadata.get_model_context_length` that records the
`config_context_length` it receives, so we assert the gating without needing
the real agent metadata catalog.
"""
import sys
import types
from pathlib import Path as _Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse


def _install_fake_get_model_context_length(monkeypatch, recorder, *, default_context=1_000_000):
    """Install a fake get_model_context_length into a stand-in agent.model_metadata."""
    mod = types.ModuleType("agent.model_metadata")

    def _fake(model, base_url="", api_key="", config_context_length=None, provider="", custom_providers=None):
        recorder["model"] = model
        recorder["base_url"] = base_url
        recorder["api_key"] = api_key
        recorder["provider"] = provider
        recorder["custom_providers"] = custom_providers
        recorder["config_context_length"] = config_context_length
        # Pretend the real per-model metadata window is 1,000,000 unless the
        # caller forced a config cap, in which case honor the cap (mirrors the
        # real helper's contract).
        if config_context_length:
            return int(config_context_length)
        return default_context

    mod.get_model_context_length = _fake
    # Ensure a parent `agent` package exists so `from agent.model_metadata import ...` resolves.
    if "agent" not in sys.modules:
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.model_metadata", mod)


def _resolver():
    import api.routes as routes
    return routes._resolve_context_length_for_session_model


def test_global_cap_applies_to_default_model(monkeypatch):
    """When the session model IS model.default, the global cap is passed through."""
    import api.config as config
    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec)
    monkeypatch.setattr(
        config, "get_config",
        lambda *a, **k: {"model": {"default": "claude-opus-4.8", "context_length": 232000}},
    )
    result = _resolver()("claude-opus-4.8")
    assert rec["config_context_length"] == 232000, "default model must receive the global cap"
    assert result == 232000


def test_global_cap_NOT_applied_to_non_default_model(monkeypatch):
    """When the session model is NOT model.default, the global cap is dropped so
    the model's real (larger) window is used — the core #3256/#3263 fix."""
    import api.config as config
    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec)
    monkeypatch.setattr(
        config, "get_config",
        lambda *a, **k: {"model": {"default": "claude-opus-4.8", "context_length": 232000}},
    )
    result = _resolver()("claude-opus-4.7-1m-internal")
    assert rec["config_context_length"] is None, (
        "non-default model must NOT receive the global cap (it would clobber real metadata)"
    )
    assert result == 1_000_000, "non-default model should resolve to its real 1M window, not the 232K cap"


def test_no_default_configured_still_applies_cap(monkeypatch):
    """If model.default is unset, the cap applies (backward-compatible)."""
    import api.config as config
    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec)
    monkeypatch.setattr(
        config, "get_config",
        lambda *a, **k: {"model": {"context_length": 200000}},
    )
    result = _resolver()("some-model")
    assert rec["config_context_length"] == 200000
    assert result == 200000


def test_empty_model_returns_zero(monkeypatch):
    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec)
    assert _resolver()("") == 0
    assert _resolver()(None) == 0


# --- #3263 provider-aware default match (Codex final-gate finding, v0.51.192) ---
# model.default and the session model can be stored in equivalent-but-different
# shapes (bare / provider-prefixed / @provider:model). An exact string compare
# wrongly treats the actual default model as non-default and drops its configured
# context_length cap. _model_matches_configured_default() normalizes the shapes.

def _matcher():
    import api.routes as routes
    return routes._model_matches_configured_default


def test_default_match_exact():
    assert _matcher()("claude-opus-4.8", "claude-opus-4.8") is True


def test_default_match_bare_session_vs_prefixed_default():
    # Config default is provider-prefixed; session model is bare — still the default.
    assert _matcher()("claude-opus-4.8", "anthropic/claude-opus-4.8", "anthropic") is True


def test_default_match_prefixed_session_vs_bare_default():
    assert _matcher()("anthropic/claude-opus-4.8", "claude-opus-4.8", "anthropic") is True


def test_default_match_at_qualified_session():
    assert _matcher()("@anthropic:claude-opus-4.8", "claude-opus-4.8") is True


def test_default_match_distinct_models_do_not_match():
    assert _matcher()("claude-opus-4.7-1m", "claude-opus-4.8", "anthropic") is False
    assert _matcher()("claude-opus-4.7-1m", "anthropic/claude-opus-4.8", "anthropic") is False
    assert _matcher()("gpt-5.5", "claude-opus-4.8", "openai") is False


def test_default_match_same_bare_different_provider_does_NOT_match():
    """Same bare model id on a DIFFERENT provider is NOT the configured default
    and must not receive its cap (Codex final-gate over-match finding)."""
    assert _matcher()("openai/gpt-4o", "openrouter/gpt-4o", "openai") is False
    assert _matcher()("@openai:gpt-4o", "@openrouter:gpt-4o") is False
    assert _matcher()("gpt-4o", "openrouter/gpt-4o", "openai") is False
    # but the SAME provider (or unknown session provider) still matches:
    assert _matcher()("openrouter/gpt-4o", "openrouter/gpt-4o") is True
    assert _matcher()("gpt-4o", "openrouter/gpt-4o", "openrouter") is True


def test_default_match_empty_inputs_are_false():
    assert _matcher()("claude-opus-4.8", "") is False
    assert _matcher()("", "claude-opus-4.8") is False


def test_prefixed_default_still_receives_cap(monkeypatch):
    """The actual default model, configured provider-prefixed, must still get
    the global cap when the session stores it bare (the Codex final-gate bug)."""
    import api.config as config
    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec)
    monkeypatch.setattr(
        config, "get_config",
        lambda *a, **k: {"model": {"default": "anthropic/claude-opus-4.8", "context_length": 232000}},
    )
    # session model is the bare form of the prefixed default
    result = _resolver()("claude-opus-4.8", "anthropic")
    assert rec["config_context_length"] == 232000, (
        "provider-prefixed default model must still receive its configured cap"
    )
    assert result == 232000


def test_resolver_uses_explicit_reload_base_url_and_api_key(monkeypatch):
    """#4248: reload must query metadata with the same endpoint/key shape as streaming."""
    import api.config as config

    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "provider": "deepseek",
                "base_url": "https://config-base.invalid/v1",
                "api_key": "config-key",
            }
        },
    )

    result = _resolver()(
        "deepseek-v4-1m",
        "deepseek",
        base_url="https://runtime-base.invalid/v1",
        api_key="runtime-key",
    )

    assert result == 1_000_000
    assert rec["base_url"] == "https://runtime-base.invalid/v1"
    assert rec["api_key"] == "runtime-key"
    assert rec["provider"] == "deepseek"


def _stub_route_session(*, context_length=1_000_000, threshold_tokens=500_000, model="deepseek-v4-1m"):
    s = MagicMock()
    s.session_id = "test-4248"
    s.title = "test-session"
    s.workspace = "/tmp"
    s.model = model
    s.model_provider = "deepseek"
    s.profile = None
    s.messages = []
    s.tool_calls = []
    s.active_stream_id = None
    s.pending_user_message = None
    s.pending_attachments = []
    s.pending_started_at = None
    s.context_length = context_length
    s.threshold_tokens = threshold_tokens
    s.last_prompt_tokens = 100_000
    s.input_tokens = 100_000
    s.output_tokens = 0
    s.read_only = False
    s._loaded_metadata_only = False
    s.compact.return_value = {
        "session_id": s.session_id,
        "title": s.title,
        "workspace": s.workspace,
        "model": model,
        "model_provider": "deepseek",
        "message_count": 0,
        "input_tokens": s.input_tokens,
        "output_tokens": 0,
        "context_length": context_length,
        "threshold_tokens": threshold_tokens,
        "last_prompt_tokens": s.last_prompt_tokens,
    }
    return s


def test_session_reload_preserves_large_persisted_window_when_recompute_hits_256k_fallback(monkeypatch):
    """#4248: full session reload must not snap a persisted 1M window back to 256k."""
    import api.config as config
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200):
        captured["data"] = data
        captured["status"] = status
        return True

    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec, default_context=256_000)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "provider": "deepseek",
                "base_url": "https://config-base.invalid/v1",
                "api_key": "reload-key",
            }
        },
    )
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda _model: ("deepseek-v4-1m", "deepseek", "https://runtime-base.invalid/v1"),
    )

    s = _stub_route_session()
    handler = MagicMock()
    parsed = urlparse("/api/session?session_id=test-4248&messages=1")

    with patch("api.routes.get_session", return_value=s), \
         patch("api.routes.j", side_effect=fake_j), \
         patch("api.routes._resolve_effective_session_model_for_display", return_value="deepseek-v4-1m"), \
         patch("api.routes._resolve_effective_session_model_provider_for_display", return_value="deepseek"), \
         patch("api.routes._session_visible_to_active_profile", return_value=True), \
         patch("api.routes._clear_stale_stream_state", return_value=None), \
         patch("api.routes._session_requires_cli_metadata_lookup", return_value=False), \
         patch("api.routes._is_messaging_session_record", return_value=False), \
         patch("api.routes.get_state_db_session_messages", return_value=[]), \
         patch("api.routes._webui_sidecar_lineage_messages_for_display", return_value=[]), \
         patch("api.routes.merge_session_messages_append_only", return_value=[]), \
         patch("api.routes._merged_webui_lineage_messages_for_display", return_value=[]), \
         patch("api.routes._active_stream_ids", return_value=set()):
        assert routes.handle_get(handler, parsed) is True

    body = captured["data"]["session"]
    assert captured["status"] == 200
    assert rec["base_url"] == "https://runtime-base.invalid/v1"
    assert rec["api_key"] == "reload-key"
    assert body["context_length"] == 1_000_000, (
        "reload must keep the persisted 1M window instead of clobbering it "
        "with the anonymous 256k fallback"
    )
    assert body["threshold_tokens"] == 500_000, (
        "the existing threshold belongs to the preserved 1M window and must not "
        "be cleared when the 256k recompute is rejected"
    )


def test_session_reload_preserves_large_window_for_slash_qualified_model(monkeypatch):
    """#4248 follow-up (Codex regression gate): a slash-qualified stored model
    (``deepseek/deepseek-v4-1m``, OpenRouter-style) that resolves to its bare id
    must NOT be treated as a model change, or the 256k fallback bypasses the
    accept-guard and clobbers the persisted 1M window.
    """
    import api.config as config
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200):
        captured["data"] = data
        captured["status"] = status
        return True

    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec, default_context=256_000)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "provider": "deepseek",
                "base_url": "https://config-base.invalid/v1",
                "api_key": "reload-key",
            }
        },
    )
    # Resolver returns the BARE model id (slash prefix stripped) — exactly the
    # shape that made `_session_model_identity_matches` report a false change.
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda _model: ("deepseek-v4-1m", "deepseek", "https://runtime-base.invalid/v1"),
    )

    s = _stub_route_session(model="deepseek/deepseek-v4-1m")
    handler = MagicMock()
    parsed = urlparse("/api/session?session_id=test-4248-slash&messages=1")

    with patch("api.routes.get_session", return_value=s), \
         patch("api.routes.j", side_effect=fake_j), \
         patch("api.routes._resolve_effective_session_model_for_display", return_value="deepseek/deepseek-v4-1m"), \
         patch("api.routes._resolve_effective_session_model_provider_for_display", return_value="deepseek"), \
         patch("api.routes._session_visible_to_active_profile", return_value=True), \
         patch("api.routes._clear_stale_stream_state", return_value=None), \
         patch("api.routes._session_requires_cli_metadata_lookup", return_value=False), \
         patch("api.routes._is_messaging_session_record", return_value=False), \
         patch("api.routes.get_state_db_session_messages", return_value=[]), \
         patch("api.routes._webui_sidecar_lineage_messages_for_display", return_value=[]), \
         patch("api.routes.merge_session_messages_append_only", return_value=[]), \
         patch("api.routes._merged_webui_lineage_messages_for_display", return_value=[]), \
         patch("api.routes._active_stream_ids", return_value=set()):
        assert routes.handle_get(handler, parsed) is True

    body = captured["data"]["session"]
    assert captured["status"] == 200
    assert body["context_length"] == 1_000_000, (
        "a slash-qualified stored model resolving to its bare id is the SAME "
        "model, so the 256k fallback must not clobber the persisted 1M window"
    )
    assert body["threshold_tokens"] == 500_000


def test_session_model_identity_matches_slash_qualified_form():
    """Unit: the identity check treats provider/model and bare-model as equal."""
    import api.routes as routes

    # slash-qualified stored vs bare resolved, same provider → same model
    assert routes._session_model_identity_matches(
        "deepseek/deepseek-v4-1m", "deepseek", "deepseek-v4-1m", "deepseek"
    ) is True
    # @provider:model form still resolves (no regression)
    assert routes._session_model_identity_matches(
        "@deepseek:deepseek-v4-1m", "deepseek", "deepseek-v4-1m", "deepseek"
    ) is True
    # different provider on the slash prefix → NOT the same model
    assert routes._session_model_identity_matches(
        "deepseek/deepseek-v4-1m", "deepseek", "deepseek-v4-1m", "openai"
    ) is False
    # genuinely different bare model → not a match
    assert routes._session_model_identity_matches(
        "deepseek/deepseek-v4-1m", "deepseek", "gpt-4o", "openai"
    ) is False


def test_session_reload_accepts_real_256k_when_effective_model_changes(monkeypatch):
    """#4248 follow-up: the 256k fallback guard must not block a real model switch."""
    import api.config as config
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200):
        captured["data"] = data
        captured["status"] = status
        return True

    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec, default_context=256_000)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "provider": "deepseek",
                "base_url": "https://runtime-base.invalid/v1",
                "api_key": "reload-key",
            }
        },
    )
    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda _model: ("deepseek-v4-256k", "deepseek", "https://runtime-base.invalid/v1"),
    )

    s = _stub_route_session(model="deepseek-v4-1m")
    handler = MagicMock()
    parsed = urlparse("/api/session?session_id=test-4248&messages=1")

    with patch("api.routes.get_session", return_value=s), \
         patch("api.routes.j", side_effect=fake_j), \
         patch("api.routes._resolve_effective_session_model_for_display", return_value="deepseek-v4-256k"), \
         patch("api.routes._resolve_effective_session_model_provider_for_display", return_value="deepseek"), \
         patch("api.routes._session_visible_to_active_profile", return_value=True), \
         patch("api.routes._clear_stale_stream_state", return_value=None), \
         patch("api.routes._session_requires_cli_metadata_lookup", return_value=False), \
         patch("api.routes._is_messaging_session_record", return_value=False), \
         patch("api.routes.get_state_db_session_messages", return_value=[]), \
         patch("api.routes._webui_sidecar_lineage_messages_for_display", return_value=[]), \
         patch("api.routes.merge_session_messages_append_only", return_value=[]), \
         patch("api.routes._merged_webui_lineage_messages_for_display", return_value=[]), \
         patch("api.routes._active_stream_ids", return_value=set()):
        assert routes.handle_get(handler, parsed) is True

    body = captured["data"]["session"]
    assert captured["status"] == 200
    assert rec["model"] == "deepseek-v4-256k"
    assert body["context_length"] == 256_000, (
        "a genuine effective-model change to a 256k model must replace the "
        "old 1M snapshot rather than being treated as an anonymous fallback"
    )
    assert body["threshold_tokens"] == 128_000


def test_session_context_lookup_keeps_base_url_when_custom_helper_is_missing(monkeypatch):
    """#4248 follow-up: non-custom base URL resolution must not depend on custom helper imports."""
    import api.config as config
    import api.routes as routes

    monkeypatch.setattr(
        config,
        "resolve_model_provider",
        lambda _model: ("deepseek-v4-1m", "deepseek", "https://runtime-base.invalid/v1"),
    )
    monkeypatch.delattr(config, "resolve_custom_provider_connection", raising=False)

    model, provider, base_url, api_key = routes._session_context_length_lookup_state(
        "deepseek-v4-1m",
        "deepseek",
    )

    assert model == "deepseek-v4-1m"
    assert provider == "deepseek"
    assert base_url == "https://runtime-base.invalid/v1"
    assert api_key == ""


# --- #3263 dual-gate MUST-FIX invariants (Codex regression gate, v0.51.192) ---
# These pin the two consistency fixes applied after the gate found that the
# default-only guard dropped the stale cap but didn't (a) recompute a persisted
# stale context_length, or (b) rescale the terminal SSE threshold. Both live
# deep inside _run_agent_streaming, so we pin them at the source-structure level
# (the live-snapshot path already had behavioral coverage; these guard the two
# sibling paths from silently regressing back to the stale value).
_STREAMING_SRC = (_Path(__file__).resolve().parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")


def test_persistence_fallback_also_runs_when_skip_cc_cl():
    """The per-turn persistence fallback must recompute the real cap when the
    stale compressor cap was skipped — not only when context_length is falsy.
    Otherwise a previously-persisted stale 232K survives forever."""
    assert "(not getattr(s, 'context_length', 0)) or _skip_cc_cl:" in _STREAMING_SRC, (
        "persistence fallback gate must also fire on _skip_cc_cl (#3263 MUST-FIX 1)"
    )


def test_persistence_rescales_threshold_when_cap_skipped():
    """When the stale cap is skipped and the real cap recomputed, the persisted
    threshold_tokens must be rescaled to the real cap (or cleared), so a reload
    matches the live snapshot."""
    assert "if _skip_cc_cl:" in _STREAMING_SRC
    assert "s.threshold_tokens = int(_orig_thresh * _real_cap / _orig_cap)" in _STREAMING_SRC, (
        "persistence path must rescale threshold_tokens to the real cap (#3263 MUST-FIX 2)"
    )


def test_sse_done_payload_rescales_threshold_when_cap_dropped():
    """The terminal SSE usage payload must rescale threshold_tokens when it
    dropped the stale compressor cap, so the indicator doesn't revert on stream
    end (messages.js overwrites S.lastUsage with this payload)."""
    assert "_dropped_stale_cap_sse" in _STREAMING_SRC
    assert "usage['threshold_tokens'] = int(_orig_cc_thresh_sse * _fb_cl / _orig_cc_cl_sse)" in _STREAMING_SRC, (
        "SSE done payload must rescale threshold_tokens to the resolved window (#3263 MUST-FIX 3)"
    )


def test_live_snapshot_guard_broadened_to_any_model_window_mismatch():
    """#4618: the live-usage snapshot stale-compressor guard must correct the
    cached window whenever the resolved real per-model window DIFFERS from the
    compressor's cached value — not only when it exactly equals the global cap.

    A leftover *other-model* cached value (e.g. opus-4.5's 168k lingering on an
    opus-4.8 1M session after an in-place model switch) is != the config cap, so
    the old `_cc_cl_u == _cl_u` condition let it slip through to the live payload
    ('refresh shows 1M, send-a-message reverts to 168k'). Pin the broadened
    condition so it cannot silently revert to the narrow default-only form."""
    assert "_real_u and _real_u != _cc_cl_u" in _STREAMING_SRC, (
        "live-snapshot guard must correct on ANY real-window != cached-window "
        "mismatch (#4618), not only the default-model exact-cap case"
    )
    # The narrow default-only signals must be GONE from the live-snapshot block.
    assert "_model_matches_configured_default as _mmcd_u" not in _STREAMING_SRC, (
        "the old default-only guard (_mmcd_u) must be removed from the "
        "live-snapshot path (#4618 broadened it)"
    )


def test_live_snapshot_reuses_hydration_context_length_helper():
    """#4618: the broadened guard must resolve the real window through the SAME
    helper GET /api/session hydration uses (_context_length_lookup_inputs_for_model
    + get_model_context_length) so the streaming/SSE path and the hydration path
    land on an IDENTICAL value (honors nested per-model config overrides + custom
    providers). Pin the helper reuse against a hand-rolled flat-cap regression."""
    assert "_context_length_lookup_inputs_for_model as _cli_u" in _STREAMING_SRC, (
        "live-snapshot guard must reuse the hydration lookup-inputs helper (#4618)"
    )
    assert "from agent.model_metadata import get_model_context_length as _g_u" in _STREAMING_SRC


def test_live_snapshot_guard_has_legacy_two_arg_fallback():
    """#4618: a TypeError from the modern 6-arg get_model_context_length (older
    hermes-agent builds) must fall back to the legacy 2-arg form rather than
    raising, and the fallback must apply the SAME mismatch condition."""
    assert "from agent.model_metadata import get_model_context_length as _g2_u" in _STREAMING_SRC
    assert "_real_u = _g2_u(_sm_u, _base_u) or 0" in _STREAMING_SRC, (
        "live-snapshot guard must keep a legacy 2-arg get_model_context_length "
        "fallback for older hermes-agent builds (#4618)"
    )


def test_live_snapshot_resolves_session_profile_config_not_ambient():
    """#4618 gate fix (Codex): the streaming worker is a detached thread that
    does NOT inherit the per-request thread-local profile context, so a bare
    get_config() resolves the process-global (default) profile (#3294). For a
    non-default profile pinning a different per-model context_length, that would
    surface the WRONG profile's window in the live payload. The guard must
    resolve the SESSION's own profile config via get_config_for_profile_home."""
    assert "from api.config import get_config_for_profile_home as _gch_u" in _STREAMING_SRC, (
        "live-snapshot guard must read the session's profile config, not ambient "
        "get_config() (#3294 cross-profile leak class)"
    )
    assert "get_hermes_home_for_profile as _ghp_u" in _STREAMING_SRC
    assert "_ghp_u(getattr(_session_obj, 'profile', None))" in _STREAMING_SRC, (
        "profile home must derive from the live session object's profile"
    )


def test_live_snapshot_guard_honors_256k_clobber_acceptance_gate():
    """#4618 gate fix (Opus): the broadened guard aligned resolution with
    hydration but must ALSO honor hydration's #4248 acceptance gate — a transient
    low-confidence 256k metadata fallback must NOT clobber a LARGER cached window
    mid-stream (which would reintroduce the 'drops to a smaller window' regression
    this guard exists to fix). Reuse the exact hydration acceptance helper on both
    the modern and legacy paths."""
    assert "_should_accept_session_context_length_refresh as _accept_u" in _STREAMING_SRC, (
        "live-snapshot guard must reuse the #4248 acceptance gate "
        "(_should_accept_session_context_length_refresh)"
    )
    assert "and _accept_u(_cc_cl_u, _real_u)" in _STREAMING_SRC, (
        "the correction must be gated by the 256k-clobber acceptance check (#4248)"
    )
    # Legacy 2-arg fallback must apply the same gate.
    assert "_should_accept_session_context_length_refresh as _accept2_u" in _STREAMING_SRC
    assert "and _accept2_u(_cc_cl_u, _real_u)" in _STREAMING_SRC, (
        "the legacy 2-arg fallback must also honor the 256k acceptance gate"
    )


def test_session_save_path_broadened_to_any_model_window_mismatch():
    """#4618 (Codex re-gate finding 1): the FINAL session-save stale-compressor
    guard must also broaden beyond the default-only exact-cap test — otherwise a
    leftover other-model window (168k) is PERSISTED to s.context_length and a
    reload shows the wrong window. It must resolve the real window via the same
    helper and honor the #4248 acceptance gate before skipping the compressor
    value."""
    assert "_context_length_lookup_inputs_for_model as _cli_cc" in _STREAMING_SRC, (
        "save path must resolve the real per-model window via the hydration helper (#4618)"
    )
    assert "_should_accept_session_context_length_refresh as _accept_cc" in _STREAMING_SRC
    assert "if _real_cc and _real_cc != _cc_cl and _accept_cc(_cc_cl, _real_cc):" in _STREAMING_SRC, (
        "save path must skip the compressor value on ANY accepted real-window "
        "mismatch, not only the default-model exact-cap case (#4618)"
    )


def test_sse_done_path_broadened_to_any_model_window_mismatch():
    """#4618 (Codex re-gate finding 2): the terminal `done` SSE usage path must
    also broaden — otherwise the indicator REVERTS to the stale window on stream
    end (messages.js overwrites S.lastUsage with the done payload). It must
    resolve the real window via the same helper and honor the #4248 gate before
    dropping the compressor value."""
    assert "_context_length_lookup_inputs_for_model as _cli_sse" in _STREAMING_SRC, (
        "SSE done path must resolve the real per-model window via the hydration helper (#4618)"
    )
    assert "_should_accept_session_context_length_refresh as _accept_sse" in _STREAMING_SRC
    assert "if _real_sse and _real_sse != _cc_cl_sse and _accept_sse(_cc_cl_sse, _real_sse):" in _STREAMING_SRC, (
        "SSE done path must drop the compressor value on ANY accepted real-window "
        "mismatch so the indicator can't snap back to a stale window at turn-end (#4618)"
    )
    # The old narrow default-only matcher must be gone from BOTH sibling paths.
    assert "_model_matches_configured_default as _mmcd_sse" not in _STREAMING_SRC, (
        "the old default-only SSE matcher (_mmcd_sse) must be removed (#4618)"
    )
    assert "_model_matches_configured_default as _mmcd_cc" not in _STREAMING_SRC, (
        "the old default-only save matcher (_mmcd_cc) must be removed (#4618)"
    )
