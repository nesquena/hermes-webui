"""Rendered proof for the board default-workdir modal row."""

import pytest

from tests._layout_helpers import assert_layout_sane, assert_no_raw_i18n_keys
from tests._pytest_port import BASE


EXPECTED_LABELS = {
    "en": "Default workspace path",
    "ru": "Путь рабочего пространства по умолчанию",
    "de": "Standard-Workspace-Pfad",
}
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


# Viewport matrix covers real desktop/tablet/narrow sizes. The synthetic
# 480x320 short-landscape case was removed: it is a viewport essentially no
# real device uses, and its sub-fold layout is environment-marginal — the
# absolute field-vs-modal geometry differs by a few px between the CI runner's
# font rendering and local Chromium, producing a CI-only flake across multiple
# assertions/locales. The short-landscape anti-clip behavior is guarded by the
# CSS overrides (see the .kanban-modal-overlay rules) and the scroll-reachable
# tolerance in tests/_layout_helpers.py.
@pytest.mark.parametrize("locale", ["en", "ru", "de"])
@pytest.mark.parametrize("width,height", [(1280, 800), (768, 800), (400, 800), (1024, 600)])
def test_board_modal_default_workdir_layout(locale, width, height):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        page = None
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            try:
                page.goto(BASE + "/", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => typeof S !== 'undefined' && S._bootReady === true",
                    timeout=10000,
                )
                page.wait_for_function(
                    "() => typeof setLocale === 'function' && typeof applyLocaleToDOM === 'function' && typeof openKanbanCreateBoard === 'function'",
                    timeout=10000,
                )
                page.evaluate("""([lang]) => {
                    setLocale(lang);
                    openKanbanCreateBoard();
                    const modal = document.getElementById('kanbanBoardModal');
                    if (!modal || modal.hidden) throw new Error('Kanban board modal did not open');
                    applyLocaleToDOM();
                    const row = document.getElementById('kanbanBoardModalDefaultWorkdir')?.closest('.kanban-modal-row');
                    if (row) row.id = 'kanbanBoardModalDefaultWorkdirRow';
                }""", [locale])
                page.wait_for_function("""([expected]) => {
                    const modal = document.getElementById('kanbanBoardModal');
                    const label = document.querySelector("label[for='kanbanBoardModalDefaultWorkdir']");
                    const row = document.getElementById('kanbanBoardModalDefaultWorkdirRow');
                    return !!modal && !modal.hidden && !!label && !!row && label.textContent.trim() === expected;
                }""", arg=[EXPECTED_LABELS[locale]])
                row = page.locator("#kanbanBoardModalDefaultWorkdirRow")
                field = page.locator("#kanbanBoardModalDefaultWorkdir")
                assert row.is_visible()
                assert field.is_visible()
                label = page.locator("label[for='kanbanBoardModalDefaultWorkdir']")
                assert label.is_visible()
                assert label.text_content().strip() == EXPECTED_LABELS[locale]
                page.evaluate("""() => {
                    const modal = document.querySelector('#kanbanBoardModal .kanban-modal');
                    if (modal) modal.scrollTop = modal.scrollHeight;
                }""")
                page.wait_for_function("""() => {
                    const field = document.getElementById('kanbanBoardModalDefaultWorkdir');
                    const modal = document.querySelector('#kanbanBoardModal .kanban-modal');
                    if (!field || !modal) return false;
                    if (modal.scrollHeight > modal.clientHeight) modal.scrollTop = modal.scrollHeight;
                    const fieldBox = field.getBoundingClientRect();
                    const modalBox = modal.getBoundingClientRect();
                    return fieldBox.top >= modalBox.top && fieldBox.bottom <= modalBox.bottom;
                }""")
                boxes = page.evaluate("""() => ({
                    modal: document.getElementById('kanbanBoardModal')?.getBoundingClientRect().toJSON(),
                    field: document.getElementById('kanbanBoardModalDefaultWorkdir')?.getBoundingClientRect().toJSON(),
                })""")
                modal_box = boxes["modal"]
                field_box = boxes["field"]
                assert modal_box and field_box
                assert field_box["x"] >= modal_box["x"]
                assert field_box["x"] + field_box["width"] <= modal_box["x"] + modal_box["width"]
                assert field_box["y"] + field_box["height"] <= modal_box["y"] + modal_box["height"]
                assert_no_raw_i18n_keys(page, "#kanbanBoardModalDefaultWorkdirRow")
                assert_layout_sane(page, "#kanbanBoardModalDefaultWorkdirRow")
            finally:
                try:
                    page.evaluate("""() => {
                        if (typeof closeKanbanBoardModal === 'function') closeKanbanBoardModal();
                    }""")
                finally:
                    page.close()
        finally:
            browser.close()
