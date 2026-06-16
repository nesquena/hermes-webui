import sys
import types
from unittest.mock import patch

# ── Mocking context-length/metadata and setup requirements for routes testing ──
def fake_get_model_context_length(model, base_url="", **kwargs):
    return 1048576

# Ensure a mock of 'agent' exists in sys.modules to prevent ModuleNotFoundError in routes imports.
fake_agent = types.ModuleType("agent")
fake_agent.__path__ = []
metadata = types.ModuleType("agent.model_metadata")
fake_agent.model_metadata = metadata  # type: ignore[attr-defined]
sys.modules["agent"] = fake_agent
sys.modules["agent.model_metadata"] = metadata
metadata.get_model_context_length = fake_get_model_context_length

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
