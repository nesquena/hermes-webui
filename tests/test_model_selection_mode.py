"""Backend tests for model_selection_mode (server-owned auto-default intent).

model_selection_mode replaces the browser-local localStorage approach with a
server-persisted field on the Session sidecar. Tests verify:

1. Session.compact() includes model_selection_mode
2. GET /api/session returns model=__default__ when mode is "auto"
3. POST /api/chat/start persists model_selection_mode
4. POST /api/session/update persists model_selection_mode
5. Frontend sources send model_selection_mode
6. model_selection_mode survives save/load cycles
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
ROUTES_PY = REPO_ROOT / "api" / "routes.py"
MODELS_PY = REPO_ROOT / "api" / "models.py"
UI_JS = REPO_ROOT / "static" / "ui.js"
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
COMMANDS_JS = REPO_ROOT / "static" / "commands.js"
BOOT_JS = REPO_ROOT / "static" / "boot.js"


# === Source-code pattern tests (no Node needed) ===

class TestModelSelectionModeSources:

    def test_compact_includes_model_selection_mode(self):
        """Session.compact() must emit model_selection_mode."""
        src = MODELS_PY.read_text(encoding="utf-8")
        assert "model_selection_mode" in src
        assert "getattr(self, 'model_selection_mode', None)" in src

    def test_session_get_overrides_model_for_auto(self):
        """GET /api/session overrides model to __default__ when model_selection_mode is auto."""
        src = ROUTES_PY.read_text(encoding="utf-8")
        assert 'raw.get("model_selection_mode") == "auto"' in src
        assert 'raw["model"] = "__default__"' in src
        assert 'raw["model_provider"] = None' in src

    def test_chat_start_persists_model_selection_mode(self):
        """_handle_chat_start persists model_selection_mode from request body."""
        src = ROUTES_PY.read_text(encoding="utf-8")
        assert 'body.get("model_selection_mode")' in src

    def test_session_update_persists_model_selection_mode(self):
        """_handle_session_save persists model_selection_mode from request body."""
        src = ROUTES_PY.read_text(encoding="utf-8")
        assert '"model_selection_mode" in body' in src
        assert 'selection_mode == "auto"' in src

    def test_frontend_messages_js_sends_model_selection_mode(self):
        """messages.js sends model_selection_mode in chat/start body."""
        src = MESSAGES_JS.read_text(encoding="utf-8")
        assert "model_selection_mode:" in src

    def test_frontend_boot_js_sends_model_selection_mode(self):
        """boot.js sends model_selection_mode in session/update body."""
        src = BOOT_JS.read_text(encoding="utf-8")
        assert "model_selection_mode:" in src

    def test_frontend_ui_js_checks_model_selection_mode(self):
        """ui.js _isDefaultModelSession checks model_selection_mode first."""
        src = UI_JS.read_text(encoding="utf-8")
        assert "model_selection_mode==='auto'" in src
        assert "session.model==='__default__'" in src

    def test_cmd_model_sends_model_selection_mode_null(self):
        """cmdModel cross-provider POST must include model_selection_mode:null."""
        src = COMMANDS_JS.read_text(encoding="utf-8")
        assert "model_selection_mode:null" in src
        assert "_clearDefaultModelSession" in src


# === Persistent intent tests (no Node needed) ===

class TestModelSelectionModePersistence:

    def test_model_selection_mode_field_is_serialized(self, tmp_path):
        """model_selection_mode survives Session.save() and Session.compact()."""
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        from api.models import Session

        session = Session(
            session_id="mode-save-test",
            title="Mode Save Test",
            model="test-model",
            model_provider="test-provider",
        )
        session.model_selection_mode = "auto"
        compact = session.compact()
        assert compact.get("model_selection_mode") == "auto"

    def test_model_selection_mode_none_when_unset(self, tmp_path):
        """Session without model_selection_mode has None in compact()."""
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        from api.models import Session

        session = Session(
            session_id="mode-none-test",
            title="Mode None Test",
            model="test-model",
        )
        compact = session.compact()
        assert compact.get("model_selection_mode") is None

    def test_model_selection_mode_cleared_explicitly(self, tmp_path):
        """Setting model_selection_mode to None clears the auto intent."""
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        from api.models import Session

        session = Session(
            session_id="mode-clear-test",
            title="Mode Clear Test",
            model="test-model",
        )
        session.model_selection_mode = "auto"
        assert session.compact().get("model_selection_mode") == "auto"
        session.model_selection_mode = None
        assert session.compact().get("model_selection_mode") is None
