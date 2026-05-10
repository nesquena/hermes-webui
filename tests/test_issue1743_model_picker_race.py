"""Regression coverage for #1743 model picker async catalog race."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text()


def _body_between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_model_picker_open_waits_for_async_model_catalog_before_rendering():
    """Opening the visible picker must not render stale static <select> options."""
    body = _body_between(UI_JS, "async function toggleModelDropdown", "function closeModelDropdown")

    assert "window._modelDropdownReady" in body
    assert "await" in body
    assert body.index("await") < body.index("renderModelDropdown()")


def test_populate_model_dropdown_rerenders_if_picker_is_already_open():
    """If the async catalog finishes while open, refresh the visible custom rows."""
    body = _body_between(UI_JS, "async function populateModelDropdown", "// Cache so we don't re-fetch")

    assert "composerModelDropdown" in body
    assert "classList.contains('open')" in body or 'classList.contains("open")' in body
    assert "renderModelDropdown()" in body
