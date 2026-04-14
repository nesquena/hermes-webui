"""
Tests for issues #373, #374, and #375.

#373: Chat silently swallows errors — no feedback when agent fails to respond
#374: Remove stale OpenAI models from default list (gpt-4o, o3)
#375: Model dropdown should fetch live models from provider
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
CONFIG_PY    = (REPO / "api" / "config.py").read_text(encoding="utf-8")
ROUTES_PY    = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
MESSAGES_JS  = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS        = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


# ── Issue #373: Silent error detection ──────────────────────────────────────

class TestSilentErrorDetection:
    """streaming.py must emit apperror when agent returns no assistant reply."""

    def test_streaming_detects_no_assistant_reply(self):
        """streaming.py must check if any assistant message was produced."""
        assert "_assistant_added" in STREAMING_PY, (
            "streaming.py must check whether an assistant message was produced (#373)"
        )

    def test_streaming_emits_apperror_on_no_response(self):
        """streaming.py must emit apperror event when agent produced no reply."""
        assert "no_response" in STREAMING_PY, (
            "streaming.py must emit apperror with type='no_response' for silent failures (#373)"
        )

    def test_streaming_returns_early_after_apperror(self):
        """streaming.py must return after emitting apperror (not also emit done)."""
        # The return statement must come after the put('apperror') for no_response
        no_resp_pos = STREAMING_PY.find("'no_response'")
        return_pos = STREAMING_PY.find("return  # Don't emit done", no_resp_pos)
        assert no_resp_pos != -1, "no_response type not found in streaming.py"
        assert return_pos != -1, (
            "streaming.py must return after emitting apperror to prevent also emitting done (#373)"
        )
        assert return_pos > no_resp_pos

    def test_streaming_detects_auth_error_in_result(self):
        """streaming.py must detect auth errors from the result object."""
        assert "_is_auth" in STREAMING_PY, (
            "streaming.py must detect auth errors in silent failures (#373)"
        )
        assert "auth_mismatch" in STREAMING_PY, (
            "streaming.py must emit auth_mismatch type for auth failures (#373)"
        )

    def test_messages_js_done_handler_detects_no_reply(self):
        """messages.js done handler must show an error if no assistant reply arrived."""
        # Check for either the variable name or the inlined check pattern
        has_no_reply_guard = (
            "hasAssistantReply" in MESSAGES_JS
            or ("role==='assistant'" in MESSAGES_JS and "No response received" in MESSAGES_JS)
        )
        assert has_no_reply_guard, (
            "messages.js done handler must detect zero assistant replies (#373)"
        )
        assert "No response received" in MESSAGES_JS, (
            "messages.js must show 'No response received' inline message (#373)"
        )

    def test_messages_js_handles_no_response_apperror_type(self):
        """messages.js apperror handler must recognise the no_response type."""
        assert "isNoResponse" in MESSAGES_JS or "no_response" in MESSAGES_JS, (
            "messages.js apperror handler must handle type='no_response' (#373)"
        )

    def test_messages_js_no_response_label(self):
        """messages.js must show a distinct label for no_response errors."""
        assert "No response received" in MESSAGES_JS, (
            "messages.js must display 'No response received' label for no_response errors (#373)"
        )


# ── Issue #374: Stale model list cleanup ─────────────────────────────────────

class TestStaleModelListCleanup:
    """gpt-4o and o3 must be removed from the primary OpenAI model lists."""

    def test_gpt4o_removed_from_fallback_models(self):
        """_FALLBACK_MODELS must not contain gpt-4o (issue #374)."""
        fallback_block_start = CONFIG_PY.find("_FALLBACK_MODELS = [")
        fallback_block_end = CONFIG_PY.find("]", fallback_block_start)
        fallback_block = CONFIG_PY[fallback_block_start:fallback_block_end]
        assert "gpt-4o" not in fallback_block, (
            "_FALLBACK_MODELS still contains gpt-4o — remove it per issue #374"
        )

    def test_o3_removed_from_fallback_models(self):
        """_FALLBACK_MODELS must not contain o3 (issue #374)."""
        fallback_block_start = CONFIG_PY.find("_FALLBACK_MODELS = [")
        fallback_block_end = CONFIG_PY.find("]", fallback_block_start)
        fallback_block = CONFIG_PY[fallback_block_start:fallback_block_end]
        assert '"o3"' not in fallback_block and "'o3'" not in fallback_block, (
            "_FALLBACK_MODELS still contains o3 — remove it per issue #374"
        )

    def test_gpt4o_removed_from_provider_models_openai(self):
        """_PROVIDER_MODELS['openai'] must not contain gpt-4o (issue #374)."""
        openai_start = CONFIG_PY.find('"openai": [')
        openai_end = CONFIG_PY.find("],", openai_start)
        openai_block = CONFIG_PY[openai_start:openai_end]
        assert "gpt-4o" not in openai_block, (
            "_PROVIDER_MODELS['openai'] still contains gpt-4o — remove per issue #374"
        )

    def test_o3_removed_from_provider_models_openai(self):
        """_PROVIDER_MODELS['openai'] must not contain o3 (issue #374)."""
        openai_start = CONFIG_PY.find('"openai": [')
        openai_end = CONFIG_PY.find("],", openai_start)
        openai_block = CONFIG_PY[openai_start:openai_end]
        assert '"o3"' not in openai_block and "'o3'" not in openai_block, (
            "_PROVIDER_MODELS['openai'] still contains o3 — remove per issue #374"
        )

    def test_fallback_still_has_gpt54_mini(self):
        """_FALLBACK_MODELS must still contain gpt-5.4-mini (not over-trimmed)."""
        assert "gpt-5.4-mini" in CONFIG_PY, (
            "_FALLBACK_MODELS must keep gpt-5.4-mini as primary OpenAI model (#374)"
        )

    def test_fallback_still_has_o4_mini(self):
        """_FALLBACK_MODELS must still contain o4-mini (reasoning model)."""
        assert "o4-mini" in CONFIG_PY, (
            "_FALLBACK_MODELS must keep o4-mini as reasoning model (#374)"
        )

    def test_copilot_list_unchanged(self):
        """Copilot provider model list should still include gpt-4o (it's a valid Copilot model)."""
        copilot_start = CONFIG_PY.find('"copilot": [')
        copilot_end = CONFIG_PY.find("],", copilot_start)
        if copilot_start == -1:
            return  # No copilot list — that's fine
        copilot_block = CONFIG_PY[copilot_start:copilot_end]
        assert "gpt-4o" in copilot_block, (
            "Copilot provider model list should keep gpt-4o (it's available via Copilot) (#374)"
        )


# ── Issue #375: Live model fetching ─────────────────────────────────────────

class TestLiveModelFetching:
    """Backend and frontend must support live model fetching from provider APIs."""

    def test_live_models_endpoint_exists_in_routes(self):
        """routes.py must have a /api/models/live endpoint (#375)."""
        assert "/api/models/live" in ROUTES_PY, (
            "routes.py must define /api/models/live endpoint (#375)"
        )

    def test_live_models_handler_function_exists(self):
        """routes.py must define _handle_live_models() function (#375)."""
        assert "def _handle_live_models(" in ROUTES_PY, (
            "routes.py must define _handle_live_models() for live model fetching (#375)"
        )

    def test_live_models_handler_validates_scheme(self):
        """_handle_live_models must validate URL scheme to prevent file:// injection (B310)."""
        assert "nosec B310" in ROUTES_PY or ("scheme" in ROUTES_PY and "http" in ROUTES_PY), (
            "_handle_live_models must validate URL scheme before urlopen (#375)"
        )

    def test_live_models_handler_has_ssrf_guard(self):
        """_handle_live_models must guard against SSRF (private IP access)."""
        assert "ssrf_blocked" in ROUTES_PY or ("is_private" in ROUTES_PY and "live" in ROUTES_PY), (
            "_handle_live_models must have SSRF protection for private IP ranges (#375)"
        )

    def test_live_models_all_providers_handled_via_agent(self):
        """_handle_live_models must delegate to provider_model_ids() which handles all
        providers gracefully — live fetch where possible, static fallback otherwise.
        The old 'not_supported' return for Anthropic/Google is superseded: those
        providers now return live or static model lists via the agent delegate."""
        assert "provider_model_ids" in ROUTES_PY, (
            "_handle_live_models must delegate to hermes_cli.models.provider_model_ids() "
            "so all providers are handled uniformly (#375 upgrade)"
        )

    def test_frontend_has_fetch_live_models_function(self):
        """ui.js must define _fetchLiveModels() for background live model loading (#375)."""
        assert "function _fetchLiveModels(" in UI_JS or "async function _fetchLiveModels(" in UI_JS, (
            "ui.js must define _fetchLiveModels() function (#375)"
        )

    def test_frontend_live_models_cache_exists(self):
        """ui.js must cache live model responses to avoid redundant API calls (#375)."""
        assert "_liveModelCache" in UI_JS, (
            "ui.js must use _liveModelCache to avoid re-fetching on every dropdown open (#375)"
        )

    def test_frontend_calls_live_models_after_static_load(self):
        """populateModelDropdown must call _fetchLiveModels after rendering the static list (#375)."""
        assert "_fetchLiveModels" in UI_JS, (
            "populateModelDropdown must call _fetchLiveModels for background update (#375)"
        )

    def test_frontend_live_fetch_only_adds_new_models(self):
        """_fetchLiveModels must not duplicate models already in the static list (#375)."""
        assert "existingIds" in UI_JS, (
            "_fetchLiveModels must track existing model IDs to avoid duplicates (#375)"
        )

    def test_frontend_live_fetch_covers_all_providers(self):
        """_fetchLiveModels no longer skips any provider — all providers return
        live or fallback models via provider_model_ids() on the backend (#375 upgrade)."""
        # The old skip list (anthropic, google, gemini) must be gone from the guard
        skip_guard_pos = UI_JS.find("includes(provider)")
        if skip_guard_pos != -1:
            guard_line = UI_JS[max(0,skip_guard_pos-100):skip_guard_pos+50]
            assert "anthropic" not in guard_line, (
                "_fetchLiveModels must not skip anthropic — backend now handles it (#375 upgrade)"
            )

    def test_live_models_endpoint_wired_in_routes(self):
        """The /api/models/live path must be handled in handle_get()."""
        # Find handle_get and check our route appears inside it
        handle_get_pos = ROUTES_PY.find("def handle_get(")
        live_route_pos = ROUTES_PY.find('"/api/models/live"')
        assert handle_get_pos != -1 and live_route_pos != -1
        assert live_route_pos > handle_get_pos, (
            "/api/models/live must be inside handle_get() (#375)"
        )
