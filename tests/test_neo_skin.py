"""Neo WebUI: skin neo must be selectable without breaking upstream themes."""

from pathlib import Path


REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def test_neo_skin_present_in_picker_and_early_boot_allowlist():
    assert "{name:'Neo'" in BOOT_JS, "Neo skin missing from _SKINS list"
    assert "neo:1" in INDEX_HTML, (
        "Neo missing from early-init skin allowlist; server default or saved "
        "skin would reset to default before boot.js runs"
    )


def test_neo_skin_has_light_and_dark_palette_blocks():
    assert ':root[data-skin="neo"]{' in CSS
    assert ':root.dark[data-skin="neo"]{' in CSS
    for token in ("--accent:#0288A8", "--accent-hover:#016B85", "--accent-text:#016B85"):
        assert token in CSS, f"Neo light palette token missing: {token}"
    for token in ("--accent:#00E5FF", "--accent-hover:#00B8D4", "--accent-text:#5EE9FF"):
        assert token in CSS, f"Neo dark palette token missing: {token}"


def test_neo_skin_is_allowed_by_backend_settings():
    assert '"neo",' in CONFIG_PY, "Neo skin missing from _SETTINGS_SKIN_VALUES"
    assert "HERMES_WEBUI_DEFAULT_SKIN" in CONFIG_PY


def test_skin_command_uses_dynamic_skin_registry():
    assert "const skins=(_SKINS||[]).map(s=>s.name.toLowerCase());" in COMMANDS_JS
    assert "if(skins.includes(val))" in COMMANDS_JS
