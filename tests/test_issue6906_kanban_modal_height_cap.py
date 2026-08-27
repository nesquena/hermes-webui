"""Regression coverage for reachable Kanban modal actions in short windows."""

import pytest

from tests._layout_helpers import assert_layout_sane, assert_no_raw_i18n_keys
from tests._pytest_port import BASE


_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_LOCALES = ["en", "ru", "de"]


def _open_modal(page, modal_id, locale):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_function("() => typeof S !== 'undefined' && S._bootReady === true", timeout=10000)
    page.wait_for_function(
        "() => typeof setLocale === 'function' && typeof applyLocaleToDOM === 'function'",
        timeout=10000,
    )
    page.evaluate(
        """([lang, id]) => {
            setLocale(lang);
            if (id === 'kanbanTaskModal') openKanbanCreate();
            else openKanbanCreateBoard();
            applyLocaleToDOM();
            const modal = document.getElementById(id);
            if (!modal || modal.hidden) throw new Error(`${id} did not open`);
        }""",
        [locale, modal_id],
    )


def _assert_reachable(page, modal_id, *, should_scroll, max_height_inset):
    result = page.evaluate(
        """(id) => {
            const overlay = document.getElementById(id);
            const modal = overlay?.querySelector('.kanban-modal');
            const actions = modal?.querySelector('.kanban-modal-actions');
            if (!overlay || !modal || !actions) throw new Error('modal geometry is incomplete');
            if (modal.scrollHeight > modal.clientHeight) modal.scrollTop = modal.scrollHeight;
            const viewport = {width: innerWidth, height: innerHeight};
            const overlayBox = overlay.getBoundingClientRect();
            const modalBox = modal.getBoundingClientRect();
            const actionsBox = actions.getBoundingClientRect();
            return {
                viewport,
                overlay: overlayBox.toJSON(),
                modal: modalBox.toJSON(),
                actions: actionsBox.toJSON(),
                clientHeight: modal.clientHeight,
                scrollHeight: modal.scrollHeight,
                maxHeight: getComputedStyle(modal).maxHeight,
                maxHeightPx: parseFloat(getComputedStyle(modal).maxHeight),
            };
        }""",
        modal_id,
    )
    assert result["overlay"]["x"] == pytest.approx(0)
    assert result["overlay"]["y"] == pytest.approx(0)
    assert result["modal"]["bottom"] <= result["viewport"]["height"] + 1
    assert result["modal"]["top"] >= -1
    assert result["actions"]["bottom"] <= result["modal"]["bottom"] + 1
    assert result["actions"]["top"] >= result["modal"]["top"] - 1
    assert result["maxHeightPx"] == pytest.approx(result["viewport"]["height"] - max_height_inset, abs=1)
    if should_scroll:
        assert result["scrollHeight"] > result["clientHeight"]
        assert result["maxHeight"] != "none"
    else:
        assert result["scrollHeight"] == pytest.approx(result["clientHeight"], abs=1)
    assert_no_raw_i18n_keys(page, f"#{modal_id}")
    assert_layout_sane(page, f"#{modal_id}")


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize(
    "width,height,should_scroll,max_height_inset",
    [
        (1280, 720, True, 48),
        (1440, 800, True, 48),
        (800, 450, True, 48),
        (1920, 1080, False, 48),
        (400, 800, True, 24),
        (640, 480, True, 24),
    ],
)
def test_task_modal_actions_reachable_across_viewports(
    locale, width, height, should_scroll, max_height_inset
):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            _open_modal(page, "kanbanTaskModal", locale)
            _assert_reachable(
                page,
                "kanbanTaskModal",
                should_scroll=should_scroll,
                max_height_inset=max_height_inset,
            )
        finally:
            page.close()
            browser.close()


@pytest.mark.parametrize("locale", _LOCALES)
def test_board_modal_inherits_reachable_modal_geometry(locale):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        page = browser.new_page(viewport={"width": 800, "height": 450})
        try:
            _open_modal(page, "kanbanBoardModal", locale)
            _assert_reachable(
                page,
                "kanbanBoardModal",
                should_scroll=True,
                max_height_inset=48,
            )
        finally:
            page.close()
            browser.close()
