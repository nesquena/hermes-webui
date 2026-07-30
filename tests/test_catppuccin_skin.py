"""Catppuccin skin: Latte light / Mocha dark, opt-in via Settings → Skin."""

from pathlib import Path

REPO = Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


def test_catppuccin_skin_present_in_picker_list():
    """The Catppuccin skin must be exposed in the generated picker grid."""
    assert "{name:'Catppuccin'" in BOOT_JS, "Catppuccin skin missing from _SKINS"
    assert "'#CBA6F7','#B4BEFE','#8839EF'" in BOOT_JS, (
        "Catppuccin preview swatches should use Mauve/Lavender/Mauve-light"
    )


def test_catppuccin_skin_in_client_and_server_allowlists():
    """Saved Catppuccin skin choices must survive boot and settings round trips."""
    assert "catppuccin:1" in INDEX_HTML, (
        "Catppuccin missing from early-init skin allowlist"
    )
    assert '"catppuccin"' in CONFIG_PY, (
        "Catppuccin missing from server settings skin allowlist"
    )
    assert "/catppuccin/" in I18N_JS, "Catppuccin missing from /theme help text"


def test_catppuccin_skin_palette_has_latte_and_mocha_tokens():
    """Catppuccin defines a full light/dark palette, not only an accent tint."""
    assert ':root[data-skin="catppuccin"]{' in CSS, (
        "Catppuccin Latte light palette block missing"
    )
    assert ':root.dark[data-skin="catppuccin"]{' in CSS, (
        "Catppuccin Mocha dark palette block missing"
    )
    for token in ("--bg:#EFF1F5", "--sidebar:#E6E9EF", "--accent:#8839EF"):
        assert token in CSS, f"Catppuccin Latte token missing: {token}"
    for token in ("--bg:#1E1E2E", "--sidebar:#181825", "--accent:#CBA6F7"):
        assert token in CSS, f"Catppuccin Mocha token missing: {token}"


def test_catppuccin_skin_is_opt_in_and_preserves_default_dark():
    """Adding Catppuccin must not silently migrate existing users."""
    init_script_idx = INDEX_HTML.find("var themes=")
    end_idx = INDEX_HTML.find("</script>", init_script_idx)
    init_block = INDEX_HTML[init_script_idx:end_idx]
    assert "||'dark'" in init_block, "Default theme must remain dark"
    forbidden = [
        "catppuccin-migrated",
        "skin-catppuccin-migrated",
        "skin='catppuccin'",
        'skin="catppuccin"',
    ]
    for marker in forbidden:
        assert marker not in init_block, (
            f"Catppuccin must be opt-in, not force-migrated. Found {marker!r}"
        )


def test_catppuccin_new_chat_button_specificity():
    """Solid Mauve new-chat buttons must keep readable text in both modes."""
    assert ':root[data-skin="catppuccin"]:not(.dark) .new-chat-btn' in CSS
    assert ':root.dark[data-skin="catppuccin"] .new-chat-btn' in CSS