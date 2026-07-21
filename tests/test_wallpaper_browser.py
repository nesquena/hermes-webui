"""Focused browser proof for custom wallpaper controls and rendering."""

from pathlib import Path
import struct
import zlib

import pytest


def _png() -> bytes:
    def chunk(name: bytes, payload: bytes = b"") -> bytes:
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload))
        )

    rows = b"".join(
        b"\x00" + b"\x08\xeb\xf1\xff\xff\x2d\x95\xff" * 4 for _ in range(4)
    )
    ihdr = struct.pack(">IIBBBBB", 8, 4, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND")
    )


def test_wallpaper_upload_scope_refresh_and_clear(base_url, tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    image = tmp_path / "wallpaper.png"
    image.write_bytes(_png())

    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        try:
            page.goto(base_url, wait_until="domcontentloaded")
            page.locator('[data-panel="settings"]').first.click()
            page.locator('[data-settings-section="appearance"]').first.click()
            field = page.locator("#wallpaperSettingsField")
            field.wait_for()
            assert page.locator("#wallpaperSaveBtn").is_disabled()
            assert page.locator("#wallpaperClearBtn").is_disabled()

            page.set_input_files("#wallpaperFileInput", str(image))
            page.locator("#wallpaperPreview:not([hidden])").wait_for()
            assert page.locator("#wallpaperSaveBtn").is_enabled()
            assert page.locator("html").get_attribute("data-wallpaper") is None

            page.locator("#wallpaperSaveBtn").click()
            page.locator('html[data-wallpaper="active"][data-wallpaper-scope="chat"]').wait_for()
            assert page.locator("#wallpaperStatus").inner_text() == "Wallpaper saved."
            assert page.locator("#wallpaperLayer").evaluate(
                "el => getComputedStyle(el).pointerEvents"
            ) == "none"

            page.locator("#wallpaperScopeApp").check()
            page.locator("#wallpaperOpacity").fill("55")
            page.locator("#wallpaperSaveBtn").click()
            page.locator('html[data-wallpaper-scope="app"]').wait_for()
            assert page.locator("html").evaluate(
                "el => getComputedStyle(el).getPropertyValue('--wallpaper-opacity').trim()"
            ) == "0.55"

            page.locator('[data-panel="chat"]').first.click()
            transparent_surfaces = page.locator(
                "#mainChat, #mainChat .messages-shell, #mainChat .messages, #mainChat .empty-state"
            )
            assert transparent_surfaces.count() == 4
            for index in range(transparent_surfaces.count()):
                assert transparent_surfaces.nth(index).evaluate(
                    "el => getComputedStyle(el).backgroundColor"
                ) == "rgba(0, 0, 0, 0)"
            main_color = page.locator("main.main").evaluate(
                "el => getComputedStyle(el).backgroundColor"
            )
            assert main_color != "rgba(0, 0, 0, 0)"
            assert page.locator("#wallpaperLayer").evaluate(
                "el => getComputedStyle(el).pointerEvents"
            ) == "none"
            hit_id = page.locator("#emptyState").evaluate(
                "el => { const r=el.getBoundingClientRect(); const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2); return hit && hit.id; }"
            )
            assert hit_id != "wallpaperLayer"

            page.reload(wait_until="domcontentloaded")
            page.locator('html[data-wallpaper="active"][data-wallpaper-scope="app"]').wait_for()

            page.locator('[data-panel="settings"]').first.click()
            page.locator('[data-settings-section="appearance"]').first.click()
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#wallpaperClearBtn").click()
            page.locator("html:not([data-wallpaper])").wait_for()
            assert page.locator("#wallpaperClearBtn").is_disabled()
        finally:
            context.close()
            browser.close()
