"""Regression tests for issues #907, #908, #909 — model dropdown fixes."""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


# ── #907: Normalized dedup in _addLiveModelsToSelect ─────────────────────────

class TestIssue907LiveModelDedup:
    """Live-fetched models with @provider: prefix must not duplicate server-injected bare entries."""

    def test_addLiveModelsToSelect_has_norm_dedup(self):
        # _normId helper and existingNorm set must be present in _addLiveModelsToSelect
        fn_idx = UI_JS.find('function _addLiveModelsToSelect(')
        assert fn_idx != -1, "_addLiveModelsToSelect not found"
        # Find the closing brace of the function (~800 chars is enough)
        fn_body = UI_JS[fn_idx:fn_idx + 2000]
        assert '_normId' in fn_body or 'existingNorm' in fn_body, (
            "_addLiveModelsToSelect must normalise IDs before dedup check (#907)"
        )

    def test_normId_strips_at_prefix(self):
        # The _normId lambda/function must strip @provider: prefix
        fn_idx = UI_JS.find('function _addLiveModelsToSelect(')
        fn_body = UI_JS[fn_idx:fn_idx + 2000]
        has_at_strip = ("startsWith('@')" in fn_body or "split(':'" in fn_body)
        assert has_at_strip, (
            "_normId in _addLiveModelsToSelect must strip @provider: prefix for dedup (#907)"
        )

    def test_existingNorm_used_as_guard(self):
        fn_idx = UI_JS.find('function _addLiveModelsToSelect(')
        fn_body = UI_JS[fn_idx:fn_idx + 2000]
        assert 'existingNorm.has(' in fn_body, (
            "_addLiveModelsToSelect must check existingNorm before appending (#907)"
        )

    def test_normId_handles_multi_colon_ollama_ids(self):
        """_normId must strip ONLY the first colon so multi-colon Ollama tag IDs
        (e.g. '@ollama-cloud:qwen3-vl:235b-instruct' vs bare 'qwen3-vl:235b-instruct')
        still dedup correctly. JS `split(':',2)[1]` with limit=2 TRUNCATES in JS
        (unlike Python's split), so the naive variant would lose the tag suffix
        and mis-dedup.
        """
        fn_idx = UI_JS.find('function _addLiveModelsToSelect(')
        assert fn_idx != -1
        fn_body = UI_JS[fn_idx:fn_idx + 2000]
        # The implementation must use indexOf/substring or split().slice(1).join(),
        # not split(':', 2)[1] which truncates the tail.
        good = 'indexOf' in fn_body or "slice(1).join(':')" in fn_body
        assert good, (
            "_normId must strip only the first colon to preserve Ollama multi-colon "
            "tag IDs (e.g. @ollama-cloud:qwen3-vl:235b-instruct). Use "
            "substring(indexOf(':')+1) or split(':').slice(1).join(':') — NOT "
            "split(':', 2)[1] which silently truncates in JS."
        )
        assert "split(':',2)[1]" not in fn_body and "split(':', 2)[1]" not in fn_body, (
            "_normId still uses split(':', 2)[1] which truncates multi-colon IDs in JS; "
            "use indexOf/substring instead."
        )


# ── #908: window._defaultModel updated on settings save ─────────────────────

class TestIssue908DefaultModelSync:
    """window._defaultModel must be updated when the user saves a new default in Preferences."""

    def test_applySavedSettingsUi_updates_window_defaultModel(self):
        fn_idx = PANELS_JS.find('function _applySavedSettingsUi(')
        assert fn_idx != -1, "_applySavedSettingsUi not found"
        # Find the end of the function (next function definition)
        fn_end = PANELS_JS.find('\nasync function saveSettings(', fn_idx)
        fn_body = PANELS_JS[fn_idx:fn_end]
        assert 'window._defaultModel' in fn_body, (
            "_applySavedSettingsUi must update window._defaultModel so newSession() "
            "uses the newly saved default without a page reload (#908)"
        )

    def test_defaultModel_update_conditioned_on_body_default_model(self):
        fn_idx = PANELS_JS.find('function _applySavedSettingsUi(')
        fn_end = PANELS_JS.find('\nasync function saveSettings(', fn_idx)
        fn_body = PANELS_JS[fn_idx:fn_end]
        # Must be guarded so we don't clear _defaultModel when body.default_model is absent
        assert "if(body.default_model)" in fn_body or "body.default_model &&" in fn_body, (
            "window._defaultModel assignment must be conditional on body.default_model being set"
        )


# ── #909: Injected default model label quality ───────────────────────────────

class TestIssue909InjectedModelLabel:
    """The server must use a proper label for the injected default model (not raw lowercase ID)."""

    def test_get_label_for_model_helper_exists(self):
        import api.config as config
        assert hasattr(config, '_get_label_for_model'), (
            "api/config.py must define _get_label_for_model() for the injected default label (#909)"
        )

    def test_label_helper_capitalizes_bare_id(self):
        from api.config import _get_label_for_model
        label = _get_label_for_model('minimax/minimax-m2.7', [])
        assert label != 'minimax-m2.7', (
            "_get_label_for_model should not return the raw lowercase ID (#909)"
        )
        # Should capitalize: "Minimax M2.7" or similar
        assert label[0].isupper(), "Label should start with an uppercase letter"

    def test_label_helper_uses_catalog_when_available(self):
        from api.config import _get_label_for_model
        existing_groups = [
            {"provider": "Nous", "models": [
                {"id": "minimax/minimax-m2.7", "label": "Minimax M2.7 (Nous)"}
            ]}
        ]
        label = _get_label_for_model('minimax/minimax-m2.7', existing_groups)
        assert label == "Minimax M2.7 (Nous)", (
            "_get_label_for_model should prefer catalog label over generated one"
        )

    def test_label_helper_strips_at_prefix_for_lookup(self):
        from api.config import _get_label_for_model
        existing_groups = [
            {"provider": "Nous", "models": [
                {"id": "minimax/minimax-m2.7", "label": "Minimax M2.7"}
            ]}
        ]
        # @nous:minimax/minimax-m2.7 should match minimax/minimax-m2.7 in catalog
        label = _get_label_for_model('@nous:minimax/minimax-m2.7', existing_groups)
        assert label == "Minimax M2.7", (
            "_get_label_for_model must strip @provider: prefix before catalog lookup"
        )

    def test_config_uses_label_helper_not_raw_split(self):
        from pathlib import Path
        config_src = (Path(__file__).resolve().parent.parent / "api" / "config.py").read_text()
        # The raw label-building pattern should be replaced by the helper
        assert "_get_label_for_model" in config_src, (
            "api/config.py must call _get_label_for_model() for injected default model labels (#909)"
        )
        # The old raw pattern should NOT be present in the injection block
        old_pattern = 'default_model.split("/")[-1] if "/" in default_model else default_model'
        label_sections = [
            config_src[i:i+200]
            for i in [m.start() for m in re.finditer(r'label\s*=\s*', config_src)]
        ]
        for sec in label_sections:
            assert old_pattern not in sec, (
                "api/config.py still uses raw split-based label for injected default model (#909)"
            )
