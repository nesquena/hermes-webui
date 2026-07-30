"""Source-level guards for update-check UI status handling."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_manual_update_check_displays_api_errors_before_up_to_date():
    """Manual checks must not translate API error payloads into green success."""
    error_idx = PANELS_JS.index("const errorParts=[]")
    up_to_date_idx = PANELS_JS.index("settings_up_to_date")
    assert error_idx < up_to_date_idx
    assert "data.webui" in PANELS_JS
    assert "data.agent" in PANELS_JS
    assert "settings_update_check_failed')+': '+errorParts.join(', ')" in PANELS_JS


def test_update_error_formatter_strips_generic_fetch_prefix():
    """The UI should show the actionable git detail, not only 'fetch failed'."""
    assert "function _formatUpdateCheckError(label,info)" in UI_JS
    assert "replace(/^fetch failed" in UI_JS
