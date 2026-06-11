"""Offline recovery must reattach softly, not hard-reload the whole page.

Symptom (Android PWA): backgrounding the app for even a second often showed a
"Connection lost" banner, and on returning the whole page did a multi-second
cold reload. Confirmed from the live server journal: the client's offline
recovery probe (`GET /health?offline_probe=...`) fired repeatedly from the
phone, and each successful probe ran `window.location.reload()`.

Root cause: `checkOfflineRecoveryNow()` in static/ui.js called
`window.location.reload()` on the first healthy probe. The offline banner is
raised on any fetch/SSE error — which mobile backgrounding triggers constantly
— so a transient background turned into a full app cold boot (re-run boot,
re-pull /api/sessions + /api/session). Intermittent because it only fired when
a request actually errored that cycle.

Fix: recover softly via `_recoverFromOfflineSoftly()` — hide the banner,
restart the gateway SSE, and re-fetch the active session through the existing
`refreshSession()` reattach path. The server keeps the agent running and
buffers stream events while no subscriber is attached (#2307), so a hard reload
is never required. A full `window.location.reload()` remains only as the catch
fallback if the soft reattach throws.

State layer: this only changes the *client* recovery transition (banner →
reattach). It does not touch server stream buffering, session persistence, or
the compression/replay paths.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent


def _ui_js() -> str:
    return (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _fn_body(src: str, marker: str) -> str:
    idx = src.find(marker)
    assert idx != -1, f"{marker!r} not found in ui.js"
    brace = src.find("{", idx)
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{marker!r} body did not close"
    return src[brace + 1 : i - 1]


class TestOfflineSoftRecovery:
    def test_recovery_does_not_hard_reload_on_success(self):
        """The success branch must call the soft recover, not location.reload()."""
        src = _ui_js()
        body = _fn_body(src, "async function checkOfflineRecoveryNow(")
        assert "_recoverFromOfflineSoftly(" in body, (
            "checkOfflineRecoveryNow must recover via _recoverFromOfflineSoftly() "
            "on a healthy probe instead of hard-reloading the page"
        )
        assert "window.location.reload()" not in body, (
            "checkOfflineRecoveryNow must not call window.location.reload() on "
            "recovery — that is the multi-second cold-boot regression"
        )

    def test_soft_recover_helper_exists(self):
        """The soft recovery helper must exist."""
        assert "async function _recoverFromOfflineSoftly(" in _ui_js(), (
            "_recoverFromOfflineSoftly() helper must exist"
        )

    def test_soft_recover_hides_banner_and_reattaches(self):
        """Soft recovery must hide the banner, restart SSE, and refresh the session."""
        body = _fn_body(_ui_js(), "async function _recoverFromOfflineSoftly(")
        assert "_hideOfflineBanner()" in body, (
            "_recoverFromOfflineSoftly must hide the offline banner"
        )
        assert "startGatewaySSE" in body, (
            "_recoverFromOfflineSoftly must restart the gateway SSE "
            "(background/bfcache kills the connection)"
        )
        assert "refreshSession" in body, (
            "_recoverFromOfflineSoftly must reattach the active session via "
            "refreshSession() so messages that arrived while away appear"
        )

    def test_soft_recover_falls_back_to_hard_reload(self):
        """A failed soft reattach must fall back to a full reload (never stuck)."""
        body = _fn_body(_ui_js(), "async function _recoverFromOfflineSoftly(")
        assert "window.location.reload()" in body, (
            "_recoverFromOfflineSoftly must keep window.location.reload() as the "
            "catch fallback so a failed soft reattach never leaves the user stuck"
        )

    def test_refresh_session_guarded_with_typeof(self):
        """Reattach calls must be typeof-guarded for safe degradation."""
        body = _fn_body(_ui_js(), "async function _recoverFromOfflineSoftly(")
        assert "typeof refreshSession==='function'" in body or \
               "typeof refreshSession === 'function'" in body, (
            "refreshSession() call must be typeof-guarded"
        )
