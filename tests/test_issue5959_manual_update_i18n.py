"""Regression tests for localized Docker manual-update guidance (#5959)."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_manual_update_instruction_exists_in_every_locale():
    source = read("static/i18n.js")
    assert source.count("settings_update_manual_docker:") == 15

    values = re.findall(r"settings_update_manual_docker:\s*'([^']*)'", source)
    assert len(values) == 15
    assert all("{0}" in value for value in values)


def test_manual_update_instruction_uses_translation_helper():
    source = read("static/ui.js")
    match = re.search(
        r"function _formatManualUpdateInstruction\b.*?\n\}", source, re.DOTALL
    )
    assert match
    function_source = match.group(0)
    assert "t('settings_update_manual_docker'" in function_source
    assert "docker pull ghcr.io/nesquena/hermes-webui:latest" in function_source
    assert "Manual update required" not in function_source


def test_settings_panel_has_no_hardcoded_manual_update_fallback():
    source = read("static/panels.js")
    assert "Manual update required" not in source
    assert "_formatManualUpdateInstruction(data.webui)" in source
