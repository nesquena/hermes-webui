"""Responsive evidence for the French Edge TTS voice picker (PR #7444).

Greptile review asked for desktop / narrow / mobile coverage of the Settings
voice picker populated with the 9 new French Edge voices. This test drives
the real settings panel against the real test server: ``switchPanel('settings')``
runs ``loadSettingsPanel()`` which defines the real ``window._populateTtsVoices``
from ``static/panels.js``, a persisted ``hermes-tts-engine=edge`` preference
makes it render the Edge voice list, and at each viewport width we assert:

  - all 9 French voices are present as options of the real ``<select>``
  - the saved French voice is the one marked selected on population
  - selecting each French voice round-trips through the real element
  - the picker control stays inside the viewport (no off-screen overflow)
  - the surrounding settings field has no layout violations

Set ``PR7444_SCREENSHOT_DIR`` to also drop proof screenshots of the populated
picker at each width (see docs/images/ for the established pattern).
"""
import os
from pathlib import Path

import pytest

from tests._layout_helpers import assert_layout_sane
from tests._pytest_port import BASE

FRENCH_VOICES = [
    "fr-FR-RemyMultilingualNeural",
    "fr-FR-VivienneMultilingualNeural",
    "fr-FR-DeniseNeural",
    "fr-FR-EloiseNeural",
    "fr-FR-HenriNeural",
    "fr-CA-AntoineNeural",
    "fr-CA-JeanNeural",
    "fr-CA-SylvieNeural",
    "fr-CA-ThierryNeural",
]
SELECTED_VOICE = "fr-FR-DeniseNeural"

# desktop / narrow / mobile widths called out by the review thread
VIEWPORTS = [
    ("desktop", 1280, 800),
    ("narrow", 720, 800),
    ("mobile", 390, 844),
]

_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
_SCREENSHOT_DIR = os.environ.get("PR7444_SCREENSHOT_DIR")

# The settings field that owns the voice picker (label + <select> + hint).
FIELD_SELECTOR = '#settingsPanePreferences div.settings-field:has(#settingsTtsVoice)'


def _open_edge_voice_picker(page):
    """Open Settings on the Preferences pane with engine=edge persisted.

    Goes through the real boot + panel-switch path so the picker is populated
    by the real ``_populateTtsVoices`` logic, never by the test itself.
    """
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof S !== 'undefined' && S._bootReady === true", timeout=15000
    )
    # The isolated test server can legitimately present first-run onboarding.
    # Dismiss it through the real UI path before opening Settings so it cannot
    # occlude the picker while geometry, hit-testing, or screenshots are taken.
    onboarding = page.locator("#onboardingOverlay")
    if onboarding.is_visible():
        page.locator("#onboardingSkipBtn").click()
        onboarding.wait_for(state="hidden", timeout=15000)
    # Persist engine + voice BEFORE the settings panel loads, the way a user
    # who previously picked them would arrive at the screen.
    page.evaluate(
        """([engine, voice]) => {
            localStorage.setItem('hermes-tts-engine', engine);
            localStorage.setItem('hermes-tts-voice', voice);
        }""",
        ["edge", SELECTED_VOICE],
    )
    page.evaluate("() => switchPanel('settings')")
    # loadSettingsPanel() is async: wait until it defined the real populator
    # and populated the picker with the persisted Edge voices.
    page.wait_for_function(
        "() => typeof window._populateTtsVoices === 'function'", timeout=15000
    )
    page.evaluate("() => switchSettingsSection('preferences')")
    page.wait_for_function(
        """(voice) => {
            const sel = document.getElementById('settingsTtsVoice');
            return !!sel && !!sel.querySelector('option[value="' + voice + '"]');
        }""",
        arg=SELECTED_VOICE,
        timeout=15000,
    )


@pytest.mark.parametrize("label,width,height", VIEWPORTS, ids=[v[0] for v in VIEWPORTS])
def test_edge_french_voice_picker_usable_across_viewports(label, width, height):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=_BROWSER_ARGS)
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            _open_edge_voice_picker(page)

            picker = page.locator("#settingsTtsVoice")
            picker.scroll_into_view_if_needed()

            # (1) The real populated picker offers every French voice.
            values = page.eval_on_selector(
                "#settingsTtsVoice",
                "sel => Array.from(sel.options).map(o => o.value)",
            )
            french = [v for v in values if v.startswith("fr-")]
            assert french == FRENCH_VOICES, values

            # (2) The real population logic marked the saved voice selected.
            assert page.eval_on_selector(
                "#settingsTtsVoice", "sel => sel.selectedOptions[0].value"
            ) == SELECTED_VOICE

            # (3) The picker control is fully on-screen at this width — no
            # off-screen overflow, whatever the surrounding layout does.
            box = picker.bounding_box()
            assert box is not None, "voice picker has no rendered box"
            assert box["x"] >= -1, (label, box)
            assert box["y"] >= -1, (label, box)
            assert box["x"] + box["width"] <= width + 1, (label, box)
            assert box["y"] + box["height"] <= height + 1, (label, box)
            assert box["width"] > 0 and box["height"] > 0, (label, box)

            # Geometry alone can pass for a control hidden behind an overlay.
            # Prove the collapsed select owns the hit-test at its center.
            hit = page.eval_on_selector(
                "#settingsTtsVoice",
                """sel => {
                    const rect = sel.getBoundingClientRect();
                    const target = document.elementFromPoint(
                        rect.left + rect.width / 2,
                        rect.top + rect.height / 2
                    );
                    return {
                        hitSelect: target === sel,
                        targetId: target ? target.id : null,
                        overlayVisible: getComputedStyle(
                            document.getElementById('onboardingOverlay')
                        ).display !== 'none',
                    };
                }""",
            )
            assert hit == {
                "hitSelect": True,
                "targetId": "settingsTtsVoice",
                "overlayVisible": False,
            }, (label, hit)

            # (4) Each French voice is selectable through the real element.
            for voice in FRENCH_VOICES:
                picker.select_option(voice)
                assert picker.input_value() == voice

            # (5) The settings field holding the picker has no layout
            # violations (overlap / clipping / off-viewport controls).
            assert_layout_sane(page, FIELD_SELECTOR)

            if _SCREENSHOT_DIR:
                out = Path(_SCREENSHOT_DIR)
                out.mkdir(parents=True, exist_ok=True)
                # Browser screenshots cannot capture the native popup of a
                # collapsed <select>. For evidence only, render an explicit
                # numbered view from the real populated French <option> labels.
                page.eval_on_selector(
                    "#settingsTtsVoice",
                    """sel => {
                        const proof = document.createElement('div');
                        proof.id = 'pr7444FrenchVoiceEvidence';
                        proof.style.cssText = [
                            'margin-top:8px',
                            'padding:10px 12px',
                            'border:1px solid var(--border2)',
                            'border-radius:6px',
                            'background:var(--code-bg)',
                            'color:var(--text)',
                            'font-size:13px',
                            'line-height:1.55',
                        ].join(';');
                        const title = document.createElement('strong');
                        title.textContent = 'French Edge voices — picker order';
                        proof.appendChild(title);
                        const list = document.createElement('ol');
                        list.style.cssText = 'margin:6px 0 0;padding-left:24px';
                        const french = Array.from(sel.options).filter(
                            option => option.value.startsWith('fr-')
                        );
                        for (const option of french) {
                            const item = document.createElement('li');
                            item.textContent = option.textContent;
                            list.appendChild(item);
                        }
                        proof.appendChild(list);
                        sel.insertAdjacentElement('afterend', proof);
                    }""",
                )
                picker.scroll_into_view_if_needed()
                field = page.locator(FIELD_SELECTOR)
                field.screenshot(
                    path=str(out / f"pr-7444-edge-tts-picker-{label}.png")
                )
        finally:
            page.close()
            browser.close()
