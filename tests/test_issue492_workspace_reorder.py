"""Tests for issue #492 — workspace drag-to-reorder."""
import json, pytest
from unittest.mock import patch, MagicMock, call
from api.routes import _handle_workspace_reorder


def _make_handler():
    """Create a mock HTTP handler."""
    h = MagicMock()
    h.wfile = MagicMock()
    return h


class TestWorkspaceReorderEndpoint:
    """Backend endpoint /api/workspaces/reorder."""

    @patch("api.routes.save_workspaces")
    @patch("api.routes.load_workspaces")
    def test_reorder_changes_order(self, mock_load, mock_save):
        mock_load.return_value = [
            {"path": "/home/user/a", "name": "Alpha"},
            {"path": "/home/user/b", "name": "Beta"},
            {"path": "/home/user/c", "name": "Gamma"},
        ]
        mock_save.side_effect = lambda wss: wss
        handler = _make_handler()
        _handle_workspace_reorder(handler, {
            "paths": ["/home/user/c", "/home/user/a", "/home/user/b"]
        })
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert saved[0]["path"] == "/home/user/c"
        assert saved[1]["path"] == "/home/user/a"
        assert saved[2]["path"] == "/home/user/b"
        handler.send_response.assert_called()

    @patch("api.routes.save_workspaces")
    @patch("api.routes.load_workspaces")
    def test_reorder_strips_whitespace(self, mock_load, mock_save):
        mock_load.return_value = [
            {"path": "/a", "name": "A"},
            {"path": "/b", "name": "B"},
        ]
        mock_save.side_effect = lambda wss: wss
        handler = _make_handler()
        _handle_workspace_reorder(handler, {"paths": [" /b ", " /a "]})
        saved = mock_save.call_args[0][0]
        assert saved[0]["path"] == "/b"

    @patch("api.routes.save_workspaces")
    @patch("api.routes.load_workspaces")
    def test_reorder_preserves_unmentioned_workspaces(self, mock_load, mock_save):
        mock_load.return_value = [
            {"path": "/a", "name": "A"},
            {"path": "/b", "name": "B"},
            {"path": "/c", "name": "C"},
        ]
        mock_save.side_effect = lambda wss: wss
        handler = _make_handler()
        _handle_workspace_reorder(handler, {"paths": ["/c"]})
        saved = mock_save.call_args[0][0]
        assert len(saved) == 3
        assert saved[0]["path"] == "/c"
        assert saved[1]["path"] == "/a"
        assert saved[2]["path"] == "/b"

    @patch("api.routes.load_workspaces")
    def test_reorder_rejects_empty_paths(self, mock_load):
        mock_load.return_value = [{"path": "/a", "name": "A"}]
        handler = _make_handler()
        _handle_workspace_reorder(handler, {"paths": []})
        handler.send_response.assert_called_with(400)

    @patch("api.routes.load_workspaces")
    def test_reorder_rejects_missing_paths_key(self, mock_load):
        mock_load.return_value = [{"path": "/a", "name": "A"}]
        handler = _make_handler()
        _handle_workspace_reorder(handler, {})
        handler.send_response.assert_called_with(400)

    @patch("api.routes.save_workspaces")
    @patch("api.routes.load_workspaces")
    def test_reorder_deduplicates(self, mock_load, mock_save):
        mock_load.return_value = [
            {"path": "/a", "name": "A"},
            {"path": "/b", "name": "B"},
        ]
        mock_save.side_effect = lambda wss: wss
        handler = _make_handler()
        _handle_workspace_reorder(handler, {
            "paths": ["/b", "/a", "/a", "/b"]
        })
        saved = mock_save.call_args[0][0]
        assert len(saved) == 2
        assert saved[0]["path"] == "/b"
        assert saved[1]["path"] == "/a"

    @patch("api.routes.save_workspaces")
    @patch("api.routes.load_workspaces")
    def test_reorder_ignores_unknown_paths(self, mock_load, mock_save):
        mock_load.return_value = [
            {"path": "/a", "name": "A"},
            {"path": "/b", "name": "B"},
        ]
        mock_save.side_effect = lambda wss: wss
        handler = _make_handler()
        _handle_workspace_reorder(handler, {"paths": ["/nonexistent", "/b"]})
        saved = mock_save.call_args[0][0]
        assert saved[0]["path"] == "/b"
        assert saved[1]["path"] == "/a"


class TestWorkspaceReorderFrontend:
    """Frontend: drag handle and i18n keys."""

    def test_i18n_keys_present_in_all_locales(self):
        """workspace_drag_hint and workspace_reorder_failed must exist in all locales."""
        with open("static/i18n.js", "r", encoding="utf-8") as f:
            content = f.read()
        for key in ("workspace_drag_hint", "workspace_reorder_failed"):
            count = content.count(key)
            assert count >= 7, f"{key} found {count} times, expected >= 7"

    def test_grip_vertical_icon_exists(self):
        with open("static/icons.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "'grip-vertical'" in content

    def test_renderWorkspacesPanel_has_drag_attrs(self):
        with open("static/panels.js", "r", encoding="utf-8") as f:
            content = f.read()
        for attr in ("draggable=true", "dragstart", "dragover", "dragend",
                      "ws-drag-handle", "/api/workspaces/reorder"):
            assert attr in content, f"Missing: {attr}"

    def test_css_drag_classes_exist(self):
        with open("static/style.css", "r", encoding="utf-8") as f:
            content = f.read()
        for cls in (".ws-drag-handle", ".ws-row.dragging", ".ws-row.drag-over"):
            assert cls in content, f"Missing CSS: {cls}"
