"""Regression coverage for BFCache restore of an in-flight session (#1466).

A browser `pageshow` restore from BFCache does not re-run the boot IIFE. After
#1473, normal reload restores in-flight sessions through `loadSession()` and
`checkInflightOnBoot()`. BFCache restore should align with that path for the
currently viewed session instead of only refreshing layout chrome/sidebar cache.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")


def _pageshow_handler() -> str:
    marker = "window.addEventListener('pageshow'"
    start = BOOT_JS.find(marker)
    assert start != -1, "pageshow listener not found in boot.js"
    brace = BOOT_JS.find("=> {", start)
    assert brace != -1, "pageshow listener body not found"
    brace = BOOT_JS.find("{", brace)
    depth = 1
    i = brace + 1
    while i < len(BOOT_JS) and depth:
        if BOOT_JS[i] == "{":
            depth += 1
        elif BOOT_JS[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, "pageshow listener body did not close"
    return BOOT_JS[brace + 1 : i - 1]


def test_pageshow_restores_active_inflight_session_through_load_session():
    """BFCache restore should refresh active session metadata and reattach stream.

    The normal boot/reload path now calls loadSession(saved), which handles
    active_stream_id / pending_user_message and reattaches via attachLiveStream().
    The BFCache pageshow path must call the same loadSession path for the current
    active session because the boot IIFE does not run again.
    """
    body = _pageshow_handler()
    assert "S.session" in body and "S.session.session_id" in body
    assert "loadSession(S.session.session_id" in body


def test_pageshow_checks_persisted_inflight_marker_after_restoring_session():
    """BFCache restore should preserve the reload recovery marker behavior.

    If localStorage still has an inflight marker, the pageshow path should reuse
    checkInflightOnBoot() after restoring the active session, matching the normal
    reload path without adding new server-side persistence behavior.
    """
    body = _pageshow_handler()
    load_pos = body.find("loadSession(S.session.session_id")
    check_pos = body.find("checkInflightOnBoot(S.session.session_id")
    assert load_pos != -1
    assert check_pos != -1
    assert load_pos < check_pos


def test_pageshow_active_session_restore_is_guarded_and_non_blocking():
    """pageshow handler must degrade safely when optional helpers are absent."""
    body = _pageshow_handler()
    assert "typeof loadSession === 'function'" in body
    assert "typeof checkInflightOnBoot === 'function'" in body
    assert "catch" in body[body.find("loadSession(S.session.session_id") : body.find("startGatewaySSE")]
