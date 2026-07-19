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


@pytest.mark.parametrize("locale", ["en", "ru", "de"])
@pytest.mark.parametrize("width,height", [(1280, 800), (768, 800), (400, 800), (1024, 600), (480, 320)])
def test_board_modal_default_workdir_layout(locale, width, height):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#kanbanBoardModalDefaultWorkdir", state="attached", timeout=10000)
        page.wait_for_function(
            "typeof setLocale === 'function' && typeof applyLocaleToDOM === 'function' && typeof openKanbanCreateBoard === 'function'",
            timeout=10000,
        )
        page.evaluate("""([lang]) => {
            setLocale(lang);
            applyLocaleToDOM();
            openKanbanCreateBoard();
            const row = document.getElementById('kanbanBoardModalDefaultWorkdir')?.closest('.kanban-modal-row');
            if (row) row.id = 'kanbanBoardModalDefaultWorkdirRow';
        }""", [locale])
        page.wait_for_function("""([expected]) => {
            const modal = document.getElementById('kanbanBoardModal');
            const label = document.querySelector("label[for='kanbanBoardModalDefaultWorkdir']");
            const row = document.getElementById('kanbanBoardModalDefaultWorkdirRow');
            return !!modal && !modal.hidden && !!label && !!row && label.textContent.trim() === expected;
        }""", arg=[EXPECTED_LABELS[locale]])
        modal = page.locator("#kanbanBoardModal")
        row = page.locator("#kanbanBoardModalDefaultWorkdirRow")
        field = page.locator("#kanbanBoardModalDefaultWorkdir")
        assert row.is_visible()
        assert field.is_visible()
        label = page.locator("label[for='kanbanBoardModalDefaultWorkdir']")
        assert label.is_visible()
        assert label.text_content().strip() == EXPECTED_LABELS[locale]
        modal_box = modal.bounding_box()
        field_box = field.bounding_box()
        assert modal_box and field_box
        assert field_box["x"] >= modal_box["x"]
        assert field_box["x"] + field_box["width"] <= modal_box["x"] + modal_box["width"]
        assert field_box["y"] + field_box["height"] <= modal_box["y"] + modal_box["height"]
        assert_no_raw_i18n_keys(page, "#kanbanBoardModalDefaultWorkdirRow")
        assert_layout_sane(page, "#kanbanBoardModalDefaultWorkdirRow")
        browser.close()
