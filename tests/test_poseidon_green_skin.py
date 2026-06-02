"""Poseidon Green skin: seafoam-green sibling to the existing blue Poseidon skin."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_poseidon_green_skin_present_in_picker_list():
    """Poseidon Green must appear in the Appearance skin picker."""
    assert "{name:'Poseidon Green', value:'poseidon-green'" in BOOT_JS
    assert "'#34D399','#10B981','#047857'" in BOOT_JS


def test_poseidon_green_skin_in_client_and_server_allowlists():
    """Saved Poseidon Green selections must survive early boot and settings round-trips."""
    assert "'poseidon-green':1" in INDEX_HTML, "Poseidon Green missing from early-init skin allowlist"
    assert '"poseidon-green"' in CONFIG_PY, "Poseidon Green missing from server settings skin allowlist"
    assert "/poseidon-green/" in I18N_JS, "Poseidon Green missing from /theme help text"


def test_poseidon_green_skin_palette_tokens_exist_for_light_and_dark():
    """Poseidon Green should be an accent-only sibling skin with light and dark variants."""
    assert ':root[data-skin="poseidon-green"]{' in CSS
    assert ':root.dark[data-skin="poseidon-green"]{' in CSS
    for token in ("--accent:#047857", "--accent-hover:#065F46", "--accent-text:#065F46"):
        assert token in CSS, f"Poseidon Green light token missing: {token}"
    for token in ("--accent:#34D399", "--accent-hover:#10B981", "--accent-text:#34D399"):
        assert token in CSS, f"Poseidon Green dark token missing: {token}"


def test_poseidon_green_skin_is_opt_in():
    """Adding Poseidon Green must not change the default appearance or legacy migrations."""
    init_script_idx = INDEX_HTML.find("var themes=")
    end_idx = INDEX_HTML.find("</script>", init_script_idx)
    init_block = INDEX_HTML[init_script_idx:end_idx]
    assert "||'dark'" in init_block, "Default theme must remain dark"
    assert "solarized:['dark','poseidon']" in init_block, "Legacy solarized migration should stay mapped to blue Poseidon"
    forbidden = [
        "poseidon-green-migrated",
        "skin-poseidon-green-migrated",
        "skin='poseidon-green'",
        'skin="poseidon-green"',
    ]
    for marker in forbidden:
        assert marker not in init_block, f"Poseidon Green must be opt-in, found {marker!r}"
