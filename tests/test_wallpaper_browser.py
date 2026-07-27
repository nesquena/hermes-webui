"""Focused browser proof for custom wallpaper controls and rendering."""

import json
from pathlib import Path
import struct
import urllib.request
import zlib

import pytest


def _settings(base_url: str, update: dict | None = None) -> dict:
    body = None if update is None else json.dumps(update).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/api/settings",
        data=body,
        method="GET" if update is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


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
    previous_onboarding = _settings(base_url)["onboarding_completed"]
    try:
        _settings(base_url, {"onboarding_completed": True})
        with playwright.sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            try:
                page = context.new_page()
                page.goto(base_url, wait_until="domcontentloaded")
                page.locator('[data-panel="settings"]').first.click()
                page.locator('[data-settings-section="appearance"]').first.click()
                field = page.locator("#wallpaperSettingsField")
                field.wait_for()
                assert page.locator("#wallpaperSaveBtn").is_disabled()
                assert page.locator("#wallpaperClearBtn").is_disabled()

                page.set_input_files("#wallpaperFileInput", str(image))
                page.locator("#wallpaperPreview:not([hidden])").wait_for()
                assert page.locator("#wallpaperLayer").count() == 1
                page.locator(
                    'html[data-wallpaper="active"][data-wallpaper-scope="chat"]'
                ).wait_for()
                assert page.locator("#wallpaperSaveBtn").is_enabled()

                # Unsaved valid draft previews, then disappears when Appearance is left.
                page.locator('[data-panel="chat"]').first.click()
                page.locator("html:not([data-wallpaper])").wait_for()
                page.locator('[data-panel="settings"]').first.click()
                page.locator('[data-settings-section="appearance"]').first.click()
                assert page.locator("#wallpaperSaveBtn").is_disabled()

                # Save authoritative Chat/80 baseline.
                page.set_input_files("#wallpaperFileInput", str(image))
                page.locator("#wallpaperSaveBtn").click()
                page.locator(
                    'html[data-wallpaper="active"][data-wallpaper-scope="chat"]'
                ).wait_for()
                page.locator('#wallpaperStatus:text-is("Wallpaper saved.")').wait_for()
                assert page.locator("#wallpaperStatus").inner_text() == "Wallpaper saved."
                assert page.locator(".app-titlebar").evaluate(
                    "el => getComputedStyle(el).backgroundColor"
                ) != "rgba(0, 0, 0, 0)"

                # Scope and opacity preview before Save on the sole layer.
                page.locator("#wallpaperScopeApp").check()
                page.locator('html[data-wallpaper-scope="app"]').wait_for()
                page.wait_for_function(
                    "() => getComputedStyle(document.querySelector('.app-titlebar')).backgroundColor === 'rgba(0, 0, 0, 0)'"
                )
                assert page.locator(".app-titlebar").evaluate(
                    "el => getComputedStyle(el).backgroundColor"
                ) == "rgba(0, 0, 0, 0)"
                page.locator("#wallpaperOpacity").fill("55")
                assert page.locator("#wallpaperLayer").evaluate(
                    "el => getComputedStyle(el).opacity"
                ) == "0.55"

                # Exit without Save restores authoritative Chat/80.
                page.locator('[data-panel="chat"]').first.click()
                page.locator('html[data-wallpaper-scope="chat"]').wait_for()
                assert page.locator("#wallpaperLayer").evaluate(
                    "el => getComputedStyle(el).opacity"
                ) == "0.8"
                page.wait_for_function(
                    "() => getComputedStyle(document.querySelector('.app-titlebar')).backgroundColor !== 'rgba(0, 0, 0, 0)'"
                )
                assert page.locator(".app-titlebar").evaluate(
                    "el => getComputedStyle(el).backgroundColor"
                ) != "rgba(0, 0, 0, 0)"

                # Re-enter, preview again, and Save app/55.
                page.locator('[data-panel="settings"]').first.click()
                page.locator('[data-settings-section="appearance"]').first.click()
                assert page.locator("#wallpaperScopeChat").is_checked()
                assert page.locator("#wallpaperOpacity").input_value() == "80"
                page.locator("#wallpaperScopeApp").check()
                page.locator("#wallpaperOpacity").fill("55")
                page.locator("#wallpaperSaveBtn").click()
                page.locator('html[data-wallpaper-scope="app"]').wait_for()
                assert page.locator("#wallpaperLayer").evaluate(
                    "el => getComputedStyle(el).opacity"
                ) == "0.55"
                assert page.locator("#wallpaperLayer").evaluate(
                    "el => getComputedStyle(el).pointerEvents"
                ) == "none"

                for skin in (
                    "graphite", "github", "codex", "terracotta", "geist-contrast"
                ):
                    page.locator("html").evaluate(
                        "(el, value) => { el.dataset.skin = value; }", skin
                    )
                    page.wait_for_function(
                        "() => getComputedStyle(document.querySelector('.app-titlebar')).backgroundColor === 'rgba(0, 0, 0, 0)'"
                    )
                    assert page.locator(".app-titlebar").evaluate(
                        "el => getComputedStyle(el).backgroundColor"
                    ) == "rgba(0, 0, 0, 0)"
                    page.wait_for_function(
                        "() => getComputedStyle(document.querySelector('.sidebar .panel-view.active')).backgroundColor === 'rgba(0, 0, 0, 0)'"
                    )
                    assert page.locator(".sidebar .panel-view.active").evaluate(
                        "el => getComputedStyle(el).backgroundColor"
                    ) == "rgba(0, 0, 0, 0)"
                    page.wait_for_function(
                        "() => getComputedStyle(document.querySelector('#emptyState.empty-state')).backgroundColor === 'rgba(0, 0, 0, 0)'"
                    )
                    assert page.locator("#emptyState.empty-state").evaluate(
                        "el => getComputedStyle(el).backgroundColor"
                    ) == "rgba(0, 0, 0, 0)"
                page.locator("html").evaluate("el => { delete el.dataset.skin; }")

                hit_id = page.locator("#emptyState").evaluate(
                    "el => { const r=el.getBoundingClientRect(); const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2); return hit && hit.id; }"
                )
                assert hit_id != "wallpaperLayer"

                page.reload(wait_until="domcontentloaded")
                page.locator('html[data-wallpaper="active"][data-wallpaper-scope="app"]').wait_for()

                page.locator('[data-panel="settings"]').first.click()
                page.locator('[data-settings-section="appearance"]').first.click()
                page.locator("#wallpaperClearBtn").click()
                page.locator("#appDialogConfirm").click()
                page.locator("html:not([data-wallpaper])").wait_for()
                assert page.locator("#wallpaperClearBtn").is_disabled()
            finally:
                context.close()
                browser.close()
    finally:
        _settings(base_url, {"onboarding_completed": previous_onboarding})


def test_wallpaper_settings_retranslate_owned_status_and_preserve_filename(
    base_url, tmp_path: Path
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    valid_image = tmp_path / "用户壁纸.png"
    valid_image.write_bytes(_png())
    invalid_file = tmp_path / "invalid-wallpaper.txt"
    invalid_file.write_text("not an image", encoding="utf-8")
    previous = _settings(base_url)
    previous_language = previous.get("language", "en")
    previous_onboarding = previous["onboarding_completed"]
    try:
        _settings(base_url, {"onboarding_completed": True, "language": "en"})
        with playwright.sync_playwright() as manager:
            browser = manager.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            try:
                page = context.new_page()
                page.goto(base_url, wait_until="domcontentloaded")
                page.locator('[data-panel="settings"]').first.click()
                page.locator('[data-settings-section="appearance"]').first.click()
                page.locator("#wallpaperSettingsField").wait_for()

                page.set_input_files("#wallpaperFileInput", str(valid_image))
                assert page.locator("#wallpaperFileName").inner_text() == "用户壁纸.png"
                page.set_input_files("#wallpaperFileInput", str(invalid_file))
                assert page.locator("#wallpaperStatus").inner_text() == (
                    "Choose a JPEG, PNG, or WebP image."
                )
                assert "is-error" in (
                    page.locator("#wallpaperStatus").get_attribute("class") or ""
                )

                # Keep Appearance active: select_option dispatches the real
                # Preferences language input/change path and locale event.
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url == f"{base_url}/api/settings"
                        and response.ok
                    )
                ) as response_info:
                    page.locator("#settingsLanguage").select_option("zh", force=True)
                settings_response = response_info.value
                settings_response.finished()
                assert settings_response.status == 200

                wallpaper = page.locator("#wallpaperSettingsField")
                assert wallpaper.locator(":scope > label").inner_text() == "壁纸"
                assert page.locator("#wallpaperDescription").inner_text() == (
                    "选择一张不超过 10 MB 的 JPEG、PNG 或 WebP 图片。更改仅在保存后生效。"
                )
                assert wallpaper.locator(
                    '[data-i18n="settings_wallpaper_choose"]'
                ).inner_text() == "选择图片或拖放到此处"
                assert wallpaper.locator(
                    '[data-i18n="settings_wallpaper_drop"]'
                ).inner_text() == "JPEG、PNG 或 WebP · 最大 10 MB"
                assert wallpaper.locator(
                    'label[for="wallpaperOpacity"]'
                ).inner_text() == "图片不透明度"
                assert wallpaper.locator("#wallpaperScope legend").inner_text() == (
                    "壁纸显示范围"
                )
                assert wallpaper.locator(
                    '[data-i18n="settings_wallpaper_scope_chat"]'
                ).inner_text() == "仅聊天"
                assert wallpaper.locator(
                    '[data-i18n="settings_wallpaper_scope_app"]'
                ).inner_text() == "整个应用"
                assert page.locator("#wallpaperSaveBtn").inner_text() == "保存壁纸"
                assert page.locator("#wallpaperClearBtn").inner_text() == "清除"
                assert page.locator("#wallpaperPreview").get_attribute("alt") == "壁纸预览"
                assert page.locator("#wallpaperStatus").inner_text() == (
                    "请选择 JPEG、PNG 或 WebP 图片。"
                )
                assert page.locator("#wallpaperFileName").inner_text() == "用户壁纸.png"
            finally:
                context.close()
                browser.close()
    finally:
        _settings(base_url, {
            "onboarding_completed": previous_onboarding,
            "language": previous_language,
        })
