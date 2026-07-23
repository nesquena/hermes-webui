"""Regression coverage for the Kanban consolidated-view toggle.

The Kanban board can be rendered as profile lanes or as one consolidated
status-column board. The browser control must persist that choice back to the
shared Kanban config so reloads and other browsers see the same mode.
"""

from pathlib import Path
import re
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]

INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_kanban_header_exposes_accessible_view_toggle():
    match = re.search(
        r'<button[^>]+id="btnKanbanViewToggle"[^>]*>',
        INDEX_HTML,
        re.S,
    )
    assert match, "Kanban board header must expose a view-mode toggle"
    tag = match.group(0)
    assert 'class="panel-head-btn kanban-view-toggle' in tag
    assert 'aria-pressed="false"' in tag
    assert 'onclick="toggleKanbanViewMode()"' in tag
    assert 'data-i18n-title="kanban_view_consolidated"' in tag
    assert 'data-i18n-aria-label="kanban_view_consolidated"' in tag

    kanban_start = INDEX_HTML.index('id="mainKanban"')
    toggle_idx = INDEX_HTML.index('id="btnKanbanViewToggle"', kanban_start)
    preview_idx = INDEX_HTML.index('id="btnKanbanPreviewDispatcher"', kanban_start)
    assert toggle_idx < preview_idx, "view toggle should sit with Kanban board controls"


def test_kanban_view_toggle_persists_to_server_config_and_rerenders():
    assert "function syncKanbanViewToggle" in PANELS_JS
    assert "async function toggleKanbanViewMode" in PANELS_JS
    assert "api('/api/kanban/config', {method: 'PATCH'" in PANELS_JS
    assert "lane_by_profile: nextLaneByProfile" in PANELS_JS
    assert "_kanbanLanesByProfile = saved.lane_by_profile === true" in PANELS_JS
    assert "_kanbanRenderBoard();" in PANELS_JS
    assert "syncKanbanViewToggle();" in PANELS_JS


def test_kanban_config_updates_lane_mode_even_after_defaults_applied():
    apply_start = PANELS_JS.index("function _kanbanApplyConfigDefaults")
    apply_end = PANELS_JS.index("let _kanbanConfigApplied", apply_start)
    body = PANELS_JS[apply_start:apply_end]
    lane_idx = body.index("_kanbanLanesByProfile = config.lane_by_profile === true")
    applied_guard_idx = body.index("if (_kanbanConfigApplied) return")
    assert lane_idx < applied_guard_idx, (
        "lane mode must refresh from server config on every load, even after "
        "one-time filter defaults have already been applied"
    )


def test_kanban_view_toggle_has_css_and_i18n():
    assert ".kanban-view-toggle" in STYLE_CSS
    assert '.kanban-view-toggle[aria-pressed="true"]' in STYLE_CSS
    for key in (
        "kanban_view_lanes",
        "kanban_view_consolidated",
        "kanban_view_lanes_saved",
        "kanban_view_consolidated_saved",
        "kanban_view_update_failed",
    ):
        assert f"{key}:" in I18N_JS


def test_kanban_config_patch_persists_lane_by_profile_to_config_yaml(tmp_path, monkeypatch):
    import api.config as config
    import api.kanban_bridge as bridge

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    lane_by_profile: true\n"
        "    render_markdown: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    monkeypatch.setattr(
        bridge,
        "_config_payload",
        lambda: {
            "columns": [],
            "assignees": [],
            "default_tenant": "",
            "lane_by_profile": True,
            "include_archived_by_default": False,
            "render_markdown": True,
            "read_only": False,
        },
    )
    config.reload_config()

    payload = bridge._update_config_payload({"lane_by_profile": False})

    assert payload["lane_by_profile"] is False
    written = config._load_yaml_config_file(cfg_path)
    assert written["dashboard"]["kanban"]["lane_by_profile"] is False
    assert written["dashboard"]["kanban"]["render_markdown"] is True


def test_kanban_config_patch_rejects_non_boolean_lane_by_profile():
    import api.kanban_bridge as bridge

    with pytest.raises(ValueError, match="lane_by_profile must be boolean"):
        bridge._update_config_payload({"lane_by_profile": "false"})


def test_kanban_config_patch_validation_returns_clean_400(monkeypatch):
    import api.kanban_bridge as bridge

    captured = {}

    def fake_bad(handler, msg, status=400):
        captured["msg"] = msg
        captured["status"] = status
        return True

    monkeypatch.setattr(bridge, "bad", fake_bad)

    result = bridge.handle_kanban_patch(
        object(),
        SimpleNamespace(path="/api/kanban/config", query=""),
        {"lane_by_profile": "false"},
    )

    assert result is True
    assert captured == {
        "msg": "lane_by_profile must be boolean",
        "status": 400,
    }
