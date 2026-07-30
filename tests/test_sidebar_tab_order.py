"""Regression tests for configurable sidebar tab ordering."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(source: str, name: str, limit: int = 5000) -> str:
    start = source.find(f"function {name}(")
    assert start >= 0, f"{name} not found"
    end = source.find("\nfunction ", start + 1)
    if end < 0:
        end = start + limit
    return source[start:end]


def test_backend_round_trip_and_validation_for_tab_order(monkeypatch, tmp_path):
    """tab_order is persisted as a sanitized list and never includes fixed tabs."""
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["tab_order"] == [], "default tab_order must be empty list"

    saved = config.save_settings({"tab_order": ["logs", "tasks", "kanban"]})
    assert saved["tab_order"] == ["logs", "tasks", "kanban"]
    assert config.load_settings()["tab_order"] == ["logs", "tasks", "kanban"]

    bad = config.save_settings({"tab_order": "logs,tasks"})
    assert bad["tab_order"] == ["logs", "tasks", "kanban"], "non-list payload is ignored"

    saved = config.save_settings({"tab_order": ["chat", "logs", "", "logs", "settings", "  ", "tasks"]})
    assert saved["tab_order"] == ["logs", "tasks"], \
        "tab_order must strip fixed tabs, blanks, and duplicates while preserving order"

    assert "tab_order" in config._SETTINGS_ALLOWED_KEYS
    assert "tab_order" not in config._SETTINGS_BOOL_KEYS


def test_frontend_static_contracts_for_tab_order():
    """Frontend must expose order helpers, drag/drop chips, and apply order to both navs."""
    assert "_TAB_ORDER_LS_KEY" in PANELS_JS
    assert "hermes-webui-tab-order" in PANELS_JS
    for fn in (
        "_getTabOrder",
        "_setTabOrder",
        "_orderedSidebarPanels",
        "_applyTabOrder",
        "_moveTabOrderPanel",
        "_handleTabVisibilityChipDrop",
    ):
        assert f"function {fn}(" in PANELS_JS, f"panels.js must define {fn}()"

    apply_body = _function_body(PANELS_JS, "_applyTabOrder")
    assert ".rail" in apply_body and ".sidebar-nav" in apply_body, \
        "tab order must be applied to both rail and mobile/sidebar nav"
    assert "insertBefore" in apply_body, "applying tab order should reorder existing DOM nodes"
    assert "settings" in apply_body and "rail-spacer" in apply_body, "chat/settings must remain fixed"

    render_body = _function_body(PANELS_JS, "_renderTabVisibilityChips")
    assert "draggable" in render_body, "chips must be draggable with the mouse"
    assert "_wireTabChipDrag" in render_body, "chips need drag/drop wiring"
    drag_body = _function_body(PANELS_JS, "_wireTabChipDrag")
    assert "dragstart" in drag_body and "drop" in drag_body, \
        "chips need dragstart/drop wiring"
    assert "_tabVisibilityDragSuppressUntil" in PANELS_JS and "Date.now()+250" in PANELS_JS, \
        "drag/drop click suppression must be short-lived, not a sticky boolean"
    assert "_orderedSidebarPanels" in render_body, "chip order should follow persisted tab_order"

    payload_body = _function_body(PANELS_JS, "_appearancePayloadFromUi")
    assert "tab_order" in payload_body and "_getTabOrder" in payload_body, \
        "Appearance autosave payload must include tab_order"

    assert ".tab-visibility-chip.dragging" in STYLE_CSS
    assert ".tab-visibility-chip.drag-over" in STYLE_CSS


def test_inline_boot_script_applies_hidden_tabs_and_order_without_head_flash():
    """The synchronous body script should apply both visibility and order before panels paint."""
    head_end = INDEX_HTML.find("</head>")
    assert "hermes-webui-tab-order" not in INDEX_HTML[:head_end]

    body_script_start = INDEX_HTML.find("Flash-prevention")
    assert body_script_start >= 0
    body_script = INDEX_HTML[body_script_start: INDEX_HTML.find("</script>", body_script_start)]
    assert "hermes-webui-hidden-tabs" in body_script
    assert "hermes-webui-tab-order" in body_script
    assert "insertBefore" in body_script
