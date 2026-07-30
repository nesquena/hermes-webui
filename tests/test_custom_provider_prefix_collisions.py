import sys
import types
from unittest.mock import patch

# ── Mocking context-length/metadata and setup requirements for routes testing ──
def fake_get_model_context_length(model, base_url="", **kwargs):
    return 1048576

# Ensure an 'agent' module exists in sys.modules to prevent ModuleNotFoundError
# when api.routes is imported on a CI runner that has ONLY the WebUI repo (no
# hermes-agent package). CRITICAL: only install the fake when the REAL agent
# package is not importable. Unconditionally doing `sys.modules["agent"] = fake`
# with `fake.__path__ = []` clobbers the genuine, importable agent package for
# the WHOLE process (this runs at collection time and is never restored) — a
# later `from agent.<sub> import ...` in the full suite (e.g. hermes_state's
# `from agent.memory_manager import sanitize_context`) then fails with
# ModuleNotFoundError. Guarding on find_spec keeps the real package intact
# locally while preserving the CI import path. Nothing in this module asserts
# against the fake metadata, so the shim is purely an import guard.
def _real_agent_metadata_importable() -> bool:
    """True only if the genuine agent.model_metadata can actually be imported.

    ``importlib.util.find_spec`` RAISES ``ValueError: agent.__spec__ is None``
    when a spec-less partial ``agent`` module is already in ``sys.modules`` (which
    happens during CI collection), so it can't be probed bare. Attempt the real
    import defensively instead: ANY failure means the genuine package isn't
    usable here, so the shim should be installed.
    """
    try:
        import agent.model_metadata as _amm  # noqa: F401
        return getattr(_amm, "get_model_context_length", None) is not None
    except Exception:
        return False


if not _real_agent_metadata_importable():
    fake_agent = sys.modules.get("agent") or types.ModuleType("agent")
    if not hasattr(fake_agent, "__path__"):
        fake_agent.__path__ = []
    metadata = types.ModuleType("agent.model_metadata")
    metadata.get_model_context_length = fake_get_model_context_length
    fake_agent.model_metadata = metadata  # type: ignore[attr-defined]
    sys.modules["agent"] = fake_agent
    sys.modules["agent.model_metadata"] = metadata

from api.routes import _normalize_provider_id, _resolve_compatible_session_model_state

def test_normalize_provider_id_custom_prefix_collision():
    """Verify that _normalize_provider_id does NOT mis-normalize custom CLI/proxy prefixes.
    
    'gemini_cli' starts with 'gemini', but because it is a custom provider prefix with an 
    underscore suffix, it should not collapse into the first-party 'google' family. It 
    must return '' (unknown) so that it is passed through untouched.
    """
    # Colliding custom prefixes (must return "")
    assert _normalize_provider_id("gemini_cli") == ""
    assert _normalize_provider_id("gemini-cli") == ""
    assert _normalize_provider_id("gpt_proxy") == ""
    assert _normalize_provider_id("claude_gateway") == ""
    assert _normalize_provider_id("openai_compat") == ""

    # Supported first-party exact match and aliases (must be normalized correctly)
    assert _normalize_provider_id("gemini") == "google"
    assert _normalize_provider_id("google-gemini") == "google"
    assert _normalize_provider_id("openai-codex") == "openai"
    assert _normalize_provider_id("claude-code") == "anthropic"
    assert _normalize_provider_id("custom:newapi") == "custom"


def test_resolve_session_model_state_custom_prefix_survives():
    """Verify that _resolve_compatible_session_model_state preserves custom prefix model strings.
    
    We pass model_provider=None and profile_provider="custom:newapi" to force the state 
    resolver down the 'slow path' (family repair checks) where our _normalize_provider_id() 
    bug actually resides, verifying it survives with changed=False.
    """
    model_id = "gemini_cli/gemini-3-flash-preview"
    model_provider = None
    profile_provider = "custom:newapi"
    profile_default = "x-ai/grok-composer"

    with patch("api.routes.get_available_models") as mock_gam:
        # Stub catalog return directly (mock_gam.return_value)
        mock_gam.return_value = {
            "active_provider": "custom:newapi",
            "default_model": "x-ai/grok-composer",
            "groups": [
                {
                    "provider": "custom:newapi",
                    "provider_id": "custom:newapi",
                    "models": [{"id": "gemini_cli/gemini-3-flash-preview", "label": "Gemini 3 Flash"}]
                }
            ]
        }
        
        resolved_model, resolved_provider, changed = _resolve_compatible_session_model_state(
            model_id,
            model_provider,
            profile_provider=profile_provider,
            profile_default_model=profile_default,
            prefer_cached_catalog=True,
        )

        assert resolved_model == "gemini_cli/gemini-3-flash-preview"
        assert resolved_provider == "custom:newapi"
        assert changed is False


def test_hyphenated_first_party_provider_ids_still_normalize():
    """Registered first-party provider IDs that use a hyphen separator (not ':' or '/')
    must still normalize to their family via the explicit alias map — the ':/'-only token
    boundary alone would drop them to '' and silently repair the session model to the
    profile default (the #4278 regression class, caught on the openai-api provider id).
    """
    # openai-api is a real registered provider id (_PROVIDER_DISPLAY "OpenAI API").
    assert _normalize_provider_id("openai-api") == "openai"
    # The other hyphenated first-party ids already covered, kept here as a class guard.
    assert _normalize_provider_id("openai-codex") == "openai"
    assert _normalize_provider_id("google-gemini") == "google"
    assert _normalize_provider_id("google-ai-studio") == "google"
    assert _normalize_provider_id("claude-code") == "anthropic"


def test_openai_api_session_model_not_repaired_to_default():
    """A bare non-default GPT model under an openai-api active/profile provider must be
    preserved, not silently repaired to the profile default (regression of the openai-api
    alias gap)."""
    with patch("api.routes.get_available_models") as mock_gam:
        mock_gam.return_value = {
            "active_provider": "openai-api",
            "default_model": "gpt-5.5",
            "groups": [
                {
                    "provider": "OpenAI API",
                    "provider_id": "openai-api",
                    "models": [
                        {"id": "gpt-5.5", "label": "GPT-5.5"},
                        {"id": "gpt-5.5-mini", "label": "GPT-5.5 Mini"},
                    ],
                }
            ],
        }

        resolved_model, resolved_provider, changed = _resolve_compatible_session_model_state(
            "gpt-5.5-mini",
            None,
            profile_provider="openai-api",
            profile_default_model="gpt-5.5",
            prefer_cached_catalog=True,
        )

        assert resolved_model == "gpt-5.5-mini"
        assert changed is False


def test_streaming_send_path_preserves_custom_namespace_models():
    """The chat-SEND path (_apply_profile_provider_context_to_streaming_model in
    api/streaming.py) had its own ungated bare-prefix loop — a custom namespace like
    'gemini_cli/...' starts with 'gemini' and was clobbered to the profile default on
    send (the #4278 collision on the send path, distinct from the session-load path).
    The loop is now gated to un-namespaced model ids."""
    from api.streaming import _apply_profile_provider_context_to_streaming_model as apply_ctx

    # Custom-namespace models must survive the send path untouched.
    for model in (
        "gemini_cli/gemini-3-flash-preview",
        "gpt_proxy/some-model",
        "claude-relay/m",
    ):
        resolved, ctx, changed = apply_ctx(model, None, "custom:newapi", "x-ai/grok-composer")
        assert resolved == model, model
        assert changed is False, model

    # A genuine bare cross-family model must STILL be repaired to the profile default.
    resolved, ctx, changed = apply_ctx("gpt-5.5", None, "google", "gemini-3-pro")
    assert resolved == "gemini-3-pro"
    assert changed is True

    # A slash-qualified cross-family model must STILL be repaired.
    resolved, ctx, changed = apply_ctx("gemini/gemini-3-pro", None, "openai-api", "gpt-5.5")
    assert resolved == "gpt-5.5"
    assert changed is True
