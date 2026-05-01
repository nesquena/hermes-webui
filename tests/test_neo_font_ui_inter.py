"""Neo WebUI: neo skin uses Inter font as specified in PRD RNF-10 and Design Spec §3."""

import json
import textwrap
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_neo_skin_uses_inter_font_family():
    """Neo skin must use Inter font family as specified in PRD RNF-10 and Design Spec §3."""
    # Inter is specified in the global --font-ui variable (line 15 of style.css)
    assert '--font-ui:' in CSS, "Missing --font-ui CSS variable"
    assert 'Inter' in CSS, "Inter font not found in --font-ui variable"

    # Verify --font-ui uses Inter as a system font
    # The design spec requires Inter for UI text
    font_ui_section = CSS.split('--font-ui:')[1].split(';')[0]
    assert 'Inter' in font_ui_section, (
        "Neo skin must use Inter font family as specified in Design Spec §3"
    )
