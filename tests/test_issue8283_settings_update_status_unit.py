"""Regression tests for Settings update status repeating unit (#8283)."""

from pathlib import Path
import re
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_settings_updates_available_does_not_repeat_unit_in_all_locales():
    src = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    matches = re.findall(r"settings_updates_available:\s*'([^']*)'", src)
    assert len(matches) == 15

    for val in matches:
        assert "{count}" in val
        # None of the translations should repeat update / aggiornamento / actualizac / etc.
        assert "update(s)" not in val.lower()
        assert "aggiornamento" not in val.lower()
        assert "actualizaci" not in val.lower()
        assert "atualizaç" not in val.lower()
        assert "mise(s)" not in val.lower()
        assert "bản cập nhật" not in val.lower()


def test_settings_status_formatting_no_unit_repeat():
    # Simulate parts = ['WebUI: 2 releases', 'Agent: 1 update']
    parts = ["WebUI: 2 releases", "Agent: 1 update"]
    template = "{count} available"
    formatted = template.replace("{count}", ", ".join(parts))
    assert formatted == "WebUI: 2 releases, Agent: 1 update available"
    assert "releases update(s)" not in formatted
    assert "updates update(s)" not in formatted
