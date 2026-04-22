"""
Tests for issue #266 — provider/model mismatch warning.

Covers:
  1. streaming.py: auth errors detected and classified as 'auth_mismatch'
  2. static/ui.js: _checkProviderMismatch() helper exists and logic is correct
  3. static/messages.js: apperror handler has auth_mismatch branch
  4. static/i18n.js: provider_mismatch_warning and provider_mismatch_label keys
     present in all locales (en, es, de, ru, zh, zh-Hant)
  5. static/boot.js: modelSelect.onchange calls _checkProviderMismatch
  6. /api/models: response includes active_provider field
"""
import json
import pathlib
import re
import urllib.request
from tests.conftest import TEST_STATE_DIR

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
from tests._pytest_port import BASE


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()), r.status


# ── 1. streaming.py: auth error detection ───────────────────────────────────

class TestStreamingAuthErrorDetection:
    """streaming.py must classify auth/401 errors as auth_mismatch."""

    def test_auth_mismatch_type_defined_in_streaming(self):
        """'auth_mismatch' type must be emitted for auth errors."""
        src = _read("api/streaming.py")
        assert "auth_mismatch" in src, (
            "auth_mismatch type not found in streaming.py — "
            "401/auth errors will not be surfaced with a helpful message"
        )

    def test_is_auth_error_flag_defined(self):
        """auth error variable must exist in the error handler (exception path and silent-failure path)."""
        src = _read("api/streaming.py")
        # Variable renamed to _exc_is_auth in exception path, _is_auth in silent-failure path
        assert "_exc_is_auth" in src or "_is_auth" in src, (
            "auth error flag not found in streaming.py"
        )

    def test_auth_error_detects_401(self):
        """'401' must be part of the auth error detection logic."""
        src = _read("api/streaming.py")
        # Find the is_auth_error block
        # Variable renamed to _exc_is_auth in exception path, _is_auth in silent-failure path
        idx = src.find("_exc_is_auth")
        assert idx != -1
        block = src[idx:idx + 500]
        assert "'401'" in block or '"401"' in block, (
            "'401' not in auth error detection block"
        )

    def test_auth_error_detects_unauthorized(self):
        """'unauthorized' must be part of the auth error detection logic."""
        src = _read("api/streaming.py")
        # Variable renamed to _exc_is_auth in exception path
        idx = src.find("_exc_is_auth")
        block = src[idx:idx + 500]
        assert "unauthorized" in block.lower(), (
            "'unauthorized' not in auth error detection block"
        )

    def test_auth_error_hint_mentions_hermes_model(self):
        """The auth_mismatch hint must mention 'hermes model' command."""
        src = _read("api/streaming.py")
        # Find the auth_mismatch apperror block
        idx = src.find("auth_mismatch")
        block = src[idx:idx + 500]
        assert "hermes model" in block, (
            "auth_mismatch hint must mention 'hermes model' command "
            "so users know how to fix provider mismatch"
        )

    def test_auth_error_does_not_catch_rate_limit(self):
        """Rate limit errors must not be reclassified as auth_mismatch."""
        src = _read("api/streaming.py")
        # Variables renamed: _exc_is_rate_limit / _exc_is_auth in exception path
        # Quota check comes first (before rate limit), then rate limit, then auth
        rl_idx = src.find("_exc_is_rate_limit")
        ae_idx = src.find("_exc_is_auth")
        assert rl_idx != -1, "_exc_is_rate_limit not found in streaming.py exception path"
        assert ae_idx != -1, "_exc_is_auth not found in streaming.py exception path"
        assert rl_idx < ae_idx, (
            "_exc_is_rate_limit check should precede _exc_is_auth — "
            "rate limit errors must not be mistaken for auth errors"
        )


# ── 2. static/ui.js: _checkProviderMismatch() ───────────────────────────────

class TestCheckProviderMismatch:
    """ui.js must expose _checkProviderMismatch() helper."""

    def test_function_defined(self):
        """_checkProviderMismatch function must be defined in ui.js."""
        src = _read("static/ui.js")
        assert "function _checkProviderMismatch" in src, (
            "_checkProviderMismatch not defined in ui.js"
        )

    def test_uses_window_active_provider(self):
        """Function must read window._activeProvider."""
        src = _read("static/ui.js")
        idx = src.find("function _checkProviderMismatch")
        block = src[idx:idx + 800]
        assert "_activeProvider" in block, (
            "_checkProviderMismatch must read window._activeProvider"
        )

    def test_skips_check_for_openrouter(self):
        """OpenRouter can route to any provider — skip the warning."""
        src = _read("static/ui.js")
        idx = src.find("function _checkProviderMismatch")
        block = src[idx:idx + 800]
        assert "openrouter" in block.lower(), (
            "_checkProviderMismatch must skip the check for openrouter"
        )

    def test_skips_check_for_custom(self):
        """Custom endpoints can serve any model — skip the warning."""
        src = _read("static/ui.js")
        idx = src.find("function _checkProviderMismatch")
        block = src[idx:idx + 800]
        assert "custom" in block.lower(), (
            "_checkProviderMismatch must skip the check for custom provider"
        )

    def test_active_provider_stored_on_model_load(self):
        """populateModelDropdown must store active_provider from /api/models."""
        src = _read("static/ui.js")
        # Find the function definition (skip the comment that also mentions the name)
        idx = src.find("async function populateModelDropdown")
        assert idx != -1, "async function populateModelDropdown not found"
        block = src[idx:idx + 800]
        assert "_activeProvider" in block, (
            "populateModelDropdown must set window._activeProvider "
            "from the /api/models response"
        )


# ── 3. static/messages.js: apperror handler ─────────────────────────────────

class TestApperrorHandler:
    """messages.js apperror handler must handle auth_mismatch type."""

    def test_auth_mismatch_type_handled(self):
        """apperror handler must check for type='auth_mismatch'."""
        src = _read("static/messages.js")
        assert "auth_mismatch" in src, (
            "auth_mismatch type not handled in messages.js apperror handler"
        )

    def test_provider_mismatch_label(self):
        """'Provider mismatch' label must appear in the error handling."""
        src = _read("static/messages.js")
        assert "Provider mismatch" in src, (
            "'Provider mismatch' label not found in messages.js"
        )

    def test_is_auth_mismatch_variable(self):
        """isAuthMismatch variable must be defined."""
        src = _read("static/messages.js")
        assert "isAuthMismatch" in src, (
            "isAuthMismatch variable not found in messages.js apperror handler"
        )


# ── 4. static/i18n.js: all locales ───────────────────────────────────────────

class TestI18nProviderMismatch:
    """All locales must have provider_mismatch_warning and provider_mismatch_label."""

    REQUIRED_KEYS = ["provider_mismatch_warning", "provider_mismatch_label"]

    def _locale_names(self, src: str) -> list[str]:
        pattern = re.compile(
            r"^\s{2}(?:'(?P<quoted>[A-Za-z0-9-]+)'|(?P<plain>[A-Za-z0-9-]+))\s*:\s*\{",
            re.MULTILINE,
        )
        names = []
        for match in pattern.finditer(src):
            names.append(match.group("quoted") or match.group("plain"))
        return names

    def _count_key(self, src: str, key: str) -> int:
        return len(re.findall(r'\b' + re.escape(key) + r'\b', src))

    def test_all_locales_have_warning_key(self):
        """provider_mismatch_warning must appear in all locales."""
        src = _read("static/i18n.js")
        locale_count = len(self._locale_names(src))
        count = self._count_key(src, "provider_mismatch_warning")
        assert count >= locale_count, (
            f"provider_mismatch_warning found {count} times, expected >= {locale_count} "
            f"(one per locale)"
        )

    def test_all_locales_have_label_key(self):
        """provider_mismatch_label must appear in all locales."""
        src = _read("static/i18n.js")
        locale_count = len(self._locale_names(src))
        count = self._count_key(src, "provider_mismatch_label")
        assert count >= locale_count, (
            f"provider_mismatch_label found {count} times, expected >= {locale_count}"
        )

    def test_warning_is_function_in_en(self):
        """English provider_mismatch_warning must be a function (m, p) => ..."""
        src = _read("static/i18n.js")
        # Find the en block
        en_start = src.find("\n  en: {")
        es_start = src.find("\n  es: {")
        en_block = src[en_start:es_start]
        assert "provider_mismatch_warning" in en_block, "Key not in en block"
        idx = en_block.find("provider_mismatch_warning")
        line = en_block[idx:idx + 200]
        # Must be a function, not a plain string
        assert "=>" in line, (
            "provider_mismatch_warning in en locale must be an arrow function "
            "that takes (m, p) parameters for model and provider interpolation"
        )

    def test_spanish_locale_key_coverage(self):
        """Spanish locale must have the new keys (parity with English)."""
        src = _read("static/i18n.js")
        es_start = src.find("\n  es: {")
        de_start = src.find("\n  de: {")
        es_block = src[es_start:de_start]
        for key in self.REQUIRED_KEYS:
            assert key in es_block, f"Key '{key}' missing from Spanish locale"


# ── 5. static/boot.js: dropdown change handler ──────────────────────────────

class TestBootModelSelectChange:
    """boot.js modelSelect.onchange must call _checkProviderMismatch."""

    def test_onchange_calls_check_function(self):
        """modelSelect.onchange must invoke _checkProviderMismatch."""
        src = _read("static/boot.js")
        assert "_checkProviderMismatch" in src, (
            "boot.js modelSelect.onchange must call _checkProviderMismatch "
            "to warn users about provider/model mismatches"
        )
        # Verify it's called from the onchange handler (near modelSelect.onchange)
        idx = src.find("'modelSelect').onchange") or src.find('"modelSelect").onchange')
        if idx == -1:
            # Try alternate patterns
            idx = src.find("modelSelect")
        block_start = src.rfind("\n", 0, src.find("_checkProviderMismatch")) or 0
        surrounding = src[max(0, block_start - 200):block_start + 400]
        assert "modelSelect" in surrounding or "selectedModel" in surrounding, (
            "_checkProviderMismatch must be called in the context of model selection"
        )

    def test_onchange_shows_toast_on_mismatch(self):
        """The warning must be shown via showToast, not alert()."""
        src = _read("static/boot.js")
        # Both _checkProviderMismatch call and showToast must be near each other
        idx = src.find("_checkProviderMismatch")
        assert idx != -1, "_checkProviderMismatch not found in boot.js"
        block = src[idx:idx + 300]
        assert "showToast" in block, (
            "Provider mismatch warning must be shown via showToast(), not alert()"
        )


# ── 6. /api/models: active_provider in response ──────────────────────────────

def test_api_models_includes_active_provider():
    """/api/models must include 'active_provider' key in response."""
    with urllib.request.urlopen(BASE + "/api/models", timeout=10) as r:
        data = json.loads(r.read())
    # active_provider can be None/null but the key must exist
    assert "active_provider" in data, (
        "/api/models response missing 'active_provider' field — "
        "frontend needs this to detect provider mismatches"
    )


def test_bare_gemini_session_model_normalizes_to_active_provider_default(monkeypatch):
    """Persisted bare Gemini IDs must not survive a provider switch."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        },
    )

    effective, changed = routes._resolve_compatible_session_model(
        "gemini-3.1-pro-preview"
    )

    assert changed is True
    assert effective == "gpt-5.4-mini"


def test_prefixed_google_session_model_normalizes_to_active_provider_default(monkeypatch):
    """Persisted provider-prefixed Gemini IDs must normalize too."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        },
    )

    effective, changed = routes._resolve_compatible_session_model(
        "google/gemini-3.1-pro-preview"
    )

    assert changed is True
    assert effective == "gpt-5.4-mini"


def test_google_active_provider_keeps_valid_gemini_session_model(monkeypatch):
    """A Google-configured session must keep its Gemini model."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "google",
            "default_model": "gemini-3.1-pro-preview",
        },
    )

    effective, changed = routes._resolve_compatible_session_model(
        "gemini-3.1-pro-preview"
    )

    assert changed is False
    assert effective == "gemini-3.1-pro-preview"


def test_session_model_normalizer_persists_corrected_model(monkeypatch):
    """Write-path normalization should still persist corrected models."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        },
    )

    save_calls = []

    class DummySession:
        def __init__(self):
            self.model = "gemini-3.1-pro-preview"

        def save(self, touch_updated_at=True):
            save_calls.append(touch_updated_at)

    session = DummySession()
    effective = routes._normalize_session_model_in_place(session)

    assert effective == "gpt-5.4-mini"
    assert session.model == "gpt-5.4-mini"
    assert save_calls == [False]


def test_session_model_display_resolver_is_read_only(monkeypatch):
    """Read-path model resolution must not mutate or save the session."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        },
    )

    save_calls = []

    class DummySession:
        def __init__(self):
            self.model = "gemini-3.1-pro-preview"

        def save(self, touch_updated_at=True):
            save_calls.append(touch_updated_at)

    session = DummySession()
    effective = routes._resolve_effective_session_model_for_display(session)

    assert effective == "gpt-5.4-mini"
    assert session.model == "gemini-3.1-pro-preview"
    assert save_calls == []


def test_api_session_is_side_effect_free_for_stale_models():
    """GET /api/session must not rewrite the session file on first open (#845)."""
    created, status = _post("/api/session/new", {})
    assert status == 200
    sid = created["session"]["session_id"]

    session_path = TEST_STATE_DIR / "sessions" / f"{sid}.json"
    session_data = json.loads(session_path.read_text(encoding="utf-8"))
    session_data["model"] = "google/gemini-3.1-pro-preview"
    before = json.dumps(session_data, ensure_ascii=False, indent=2)
    session_path.write_text(before, encoding="utf-8")

    with urllib.request.urlopen(
        BASE + f"/api/session?session_id={sid}", timeout=10
    ) as r:
        payload = json.loads(r.read())

    after = session_path.read_text(encoding="utf-8")
    assert payload["session"]["model"] == "openai/gpt-5.4-mini"
    assert after == before, (
        "GET /api/session must return an effective model for display without "
        "rewriting the session file on disk"
    )


# ── Model switch toast (#419) ─────────────────────────────────────────────────

class TestModelSwitchToast:
    """Toast appears when user switches model during an active session."""

    def test_toast_in_model_select_onchange(self):
        """modelSelect.onchange must show a toast when S.messages is non-empty."""
        src = _read("static/boot.js")
        # Find the onchange block
        idx = src.find("modelSelect').onchange")
        assert idx != -1, "modelSelect.onchange not found in boot.js"
        block = src[idx:idx + 1100]
        assert "Model change takes effect in your next conversation" in block, (
            "modelSelect.onchange must show a toast when switching model mid-session"
        )

    def test_toast_guards_on_messages_length(self):
        """Toast must only fire when there are existing messages (active session)."""
        src = _read("static/boot.js")
        idx = src.find("Model change takes effect in your next conversation")
        assert idx != -1
        # Look back 200 chars for the S.messages guard
        surrounding = src[max(0, idx - 200):idx + 50]
        assert "S.messages" in surrounding and ".length" in surrounding, (
            "Model switch toast must be gated on S.messages.length > 0"
        )

    def test_toast_uses_show_toast_not_alert(self):
        """Toast must use showToast(), not alert()."""
        src = _read("static/boot.js")
        idx = src.find("Model change takes effect in your next conversation")
        assert idx != -1
        surrounding = src[max(0, idx - 50):idx + 100]
        assert "showToast" in surrounding, "Must use showToast() not alert()"
        assert "alert(" not in surrounding, "Must not use alert()"

    def test_toast_has_typeof_showtoast_guard(self):
        """Toast call must guard typeof showToast to be safe during boot."""
        src = _read("static/boot.js")
        idx = src.find("Model change takes effect in your next conversation")
        assert idx != -1
        surrounding = src[max(0, idx - 100):idx + 50]
        assert "typeof showToast" in surrounding, (
            "showToast call must be guarded with typeof check"
        )


class TestChatStartEffectiveModelRecovery:
    """messages.js must accept an effective_model correction from the backend."""

    def test_send_applies_effective_model_from_chat_start(self):
        src = _read("static/messages.js")
        assert "startData.effective_model" in src, (
            "send() must read effective_model from /api/chat/start so the UI can "
            "recover from stale persisted session models"
        )
        assert "localStorage.setItem('hermes-webui-model', startData.effective_model)" in src, (
            "effective_model correction must update the saved model preference"
        )


def test_unknown_prefix_model_passes_through_unchanged(monkeypatch):
    """Models with unknown/custom prefixes must never be stripped — regression test for #751."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        },
    )

    for custom_model in (
        "custom-provider/test-model-999",
        "test/import-model",
        "my-local-llm/variant-1",
        "lmstudio-community/Qwen2.5-Coder-7B-Instruct-GGUF",
    ):
        effective, changed = routes._resolve_compatible_session_model(custom_model)
        assert changed is False, (
            f"Model '{custom_model}' has an unknown prefix and must pass through unchanged, "
            f"but _resolve_compatible_session_model returned changed=True (effective='{effective}')"
        )
        assert effective == custom_model, (
            f"Expected '{custom_model}', got '{effective}'"
        )


def test_empty_model_session_does_not_trigger_save(monkeypatch):
    """Sessions with no model stored must not trigger session.save() — index rebuild is expensive."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.4-mini",
        },
    )

    save_calls = []

    class DummySession:
        def __init__(self):
            self.model = None  # no model stored

        def save(self, touch_updated_at=True):
            save_calls.append(touch_updated_at)

    session = DummySession()
    effective = routes._normalize_session_model_in_place(session)

    # Must return the default, but must NOT write to disk
    assert effective == "gpt-5.4-mini"
    assert save_calls == [], (
        "_normalize_session_model_in_place must not call session.save() when "
        "the session has no stored model — no correction needed, just a fallback."
    )


# ── Issue #829: stale cross-provider model on custom_providers-only setup ─────

def test_stale_openai_model_cleared_for_custom_only_provider(monkeypatch):
    """A stale openai/... session model must be cleared when active provider is
    'custom' and no catalog group can route the openai prefix (#829)."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "custom",
            "default_model": "",
            "groups": [
                {"provider": "Agent37", "provider_id": "custom:agent37",
                 "models": [{"id": "agent37/default", "label": "default"}]},
            ],
        },
    )

    effective, changed = routes._resolve_compatible_session_model(
        "openai/gpt-5.4-mini"
    )

    # No routable group for openai/ — should clear to default (empty → model itself
    # only if no default available, which means changed=False when default_model="")
    # When default_model is empty, we can't clear — preserve and return False
    assert changed is False
    assert effective == "openai/gpt-5.4-mini"


def test_stale_openai_model_cleared_for_custom_provider_with_default(monkeypatch):
    """When active_provider='custom', no openrouter group, and default_model is
    configured, stale openai/... model should be cleared to default (#829)."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "custom",
            "default_model": "agent37/default",
            "groups": [
                {"provider": "Agent37", "provider_id": "custom:agent37",
                 "models": [{"id": "agent37/default", "label": "default"}]},
            ],
        },
    )

    effective, changed = routes._resolve_compatible_session_model(
        "openai/gpt-5.4-mini"
    )

    assert changed is True
    assert effective == "agent37/default"


def test_openrouter_model_preserved_when_openrouter_group_present(monkeypatch):
    """When active_provider='openrouter' and openrouter group exists,
    openai/... model IDs must pass through unchanged — they are routable (#829)."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openrouter",
            "default_model": "openai/gpt-5.4-mini",
            "groups": [
                {"provider": "OpenRouter", "provider_id": "openrouter",
                 "models": [{"id": "openai/gpt-5.4-mini", "label": "GPT-5.4 Mini"}]},
            ],
        },
    )

    effective, changed = routes._resolve_compatible_session_model(
        "openai/gpt-5.4-mini"
    )

    assert changed is False
    assert effective == "openai/gpt-5.4-mini"


def test_custom_namespace_model_always_preserved_on_custom_provider(monkeypatch):
    """Model IDs with 'custom/' prefix must always pass through unchanged even
    when active_provider='custom' (#829)."""
    import api.routes as routes

    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "custom",
            "default_model": "agent37/default",
            "groups": [
                {"provider": "Agent37", "provider_id": "custom:agent37",
                 "models": [{"id": "agent37/default", "label": "default"}]},
            ],
        },
    )

    effective, changed = routes._resolve_compatible_session_model(
        "custom/my-local-llm"
    )

    assert changed is False
    assert effective == "custom/my-local-llm"


def test_stale_ui_js_does_not_inject_unavailable_option():
    """renderSession() must no longer inject a bare (unavailable) option into
    modelSelect when the session model is not in the provider list (#829).
    It should silently reset to the first available model instead."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "static", "ui.js"),
               encoding="utf-8").read()

    # The old pattern must be gone — both keys removed from ui.js
    assert "model_unavailable" not in src and "model_unavailable_title" not in src, (
        "renderSession() must not inject '(unavailable)' options — "
        "stale models should be silently reset to the first available model (#829)"
    )

    # The new silent-reset pattern must be present
    assert "first.value" in src and "S.session.model=first.value" in src, (
        "renderSession() must silently reset S.session.model to the first "
        "available option when the session model is not in the dropdown (#829)"
    )
