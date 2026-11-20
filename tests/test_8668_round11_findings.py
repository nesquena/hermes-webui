"""Regression coverage for the four 2026-11-18 #8668 review findings.

1. `_restoreSettledSession()` painted the recovered transcript while `S.busy`
   was still true, hiding Edit/Regenerate.
2. A machine with Playwright but no Chromium exited 1 (driver error) instead
   of the documented skip (exit 2).
3. The apperror gate never asserted the previous answer survived and let the
   error-frame wait expire silently.
4. Both DOM-outcome tests launched the full browser suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
DRIVER_PATH = REPO / "tests" / "browser_8643_busy_message_actions.py"
DRIVER_SOURCE = DRIVER_PATH.read_text(encoding="utf-8")
TEST_SOURCE = (REPO / "tests" / "test_issue8643_busy_message_actions.py").read_text(
    encoding="utf-8"
)


def _load_driver():
    spec = importlib.util.spec_from_file_location(
        "browser_8643_busy_message_actions_under_test", DRIVER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_recovered_session_render_clears_busy_first():
    start = MESSAGES_JS.index("async function _restoreSettledSession")
    end = MESSAGES_JS.index("\n  function _handleStreamError", start)
    body = MESSAGES_JS[start:end]
    busy_at = body.index("S.busy=false;")
    render_at = body.index("renderMessages({preserveScroll:true})")
    assert busy_at < render_at, (
        "the recovery render must paint from the idle state; S.busy is still "
        "true here and renderMessages derives Edit/Regenerate from it"
    )


def test_missing_chromium_is_reported_as_a_skip():
    assert "SKIP: playwright chromium executable is unavailable" in DRIVER_SOURCE
    launch_at = DRIVER_SOURCE.index("playwright.chromium.launch(")
    # The skip branch must return 2 (the pytest callers' skip contract) before
    # the broad driver-error handler turns the same condition into exit 1.
    window = DRIVER_SOURCE[launch_at : launch_at + 1600]
    assert "return 2" in window
    assert "Executable doesn't exist at" in window


def test_apperror_gate_requires_the_frame_and_the_previous_answer():
    driver = _load_driver()
    base = {
        "threw": None,
        "renderBusy": [True, False],
        "afterError": {
            "text": "previous answer and error",
            "copyCount": 1,
            "busy": False,
            "regenerate": [driver.APPERROR_ERROR_ASSISTANT_IDX],
            "edit": [driver.APPERROR_PENDING_USER_IDX],
        },
        "busyRender": {},
        "restored": {},
        "errorFrameSeen": True,
        "previousMarkerPresent": True,
    }
    assert not any(
        "never appeared" in p or "previous answer is gone" in p
        for p in driver._failures_apperror_terminal(dict(base))
    )

    missing_frame = dict(base, errorFrameSeen=False)
    assert any(
        "never appeared" in p
        for p in driver._failures_apperror_terminal(missing_frame)
    )

    missing_answer = dict(base, previousMarkerPresent=False)
    assert any(
        "previous answer is gone" in p
        for p in driver._failures_apperror_terminal(missing_answer)
    )


def test_browser_suite_is_launched_once():
    assert TEST_SOURCE.count("result = _run_driver()") == 1, (
        "the browser suite must run once, from the module-scoped fixture; the "
        "per-test calls repeated the whole server + Chromium run"
    )
    assert "@pytest.fixture(scope=\"module\")" in TEST_SOURCE
    assert "def test_busy_turn_paints_no_dead_action_buttons(driver_result)" in TEST_SOURCE
    assert (
        "def test_rejected_regeneration_restores_the_action_buttons(driver_result)"
        in TEST_SOURCE
    )
