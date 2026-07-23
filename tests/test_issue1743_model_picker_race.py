"""Regression coverage for #1743 model picker async catalog race."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _body_between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def test_model_picker_opens_before_async_model_catalog_finishes():
    """Opening the visible picker must not block on a slow /api/models request."""
    body = _body_between(UI_JS, "async function toggleModelDropdown", "function closeModelDropdown")

    assert "window._ensureModelDropdownReady" in body
    render_idx = body.index("renderModelDropdown()")
    open_idx = body.index("dd.classList.add('open')")
    await_idx = body.find("await")
    assert render_idx < open_idx
    assert await_idx == -1 or open_idx < await_idx


def test_populate_model_dropdown_rerenders_if_picker_is_already_open():
    """If the async catalog finishes while open, refresh the visible custom rows."""
    body = _body_between(UI_JS, "async function populateModelDropdown", "// Cache so we don't re-fetch")

    assert "composerModelDropdown" in body
    assert "classList.contains('open')" in body or 'classList.contains("open")' in body
    assert "renderModelDropdown()" in body
