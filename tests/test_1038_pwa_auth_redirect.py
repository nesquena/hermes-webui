"""
Tests for issue #1038 — iOS PWA auth-expiry redirect.

When a 401 is returned by any API endpoint, the client-side JS should redirect
to /login rather than showing a raw error toast. On iOS PWA standalone mode a
server-side 302→/login breaks out of the PWA shell into Safari, so the fix is
client-side: workspace.js api() intercepts 401 before throwing and calls
window.location.href = '/login'.

These are static regression tests that verify the JS source contains the
correct guard patterns.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _workspace_js() -> str:
    return (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


def _ui_js() -> str:
    return (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


class TestPWAAuthRedirect:
    def test_workspace_js_has_401_redirect(self):
        """api() in workspace.js must redirect to /login on 401."""
        src = _workspace_js()
        # Guard must appear inside the !res.ok block, before throwing
        assert "res.status===401" in src, \
            "workspace.js api() must check res.status===401"
        assert "window.location.href='/login" in src or 'window.location.href="/login' in src, \
            "workspace.js api() must redirect to /login on 401"

    def test_workspace_js_401_before_throw(self):
        """The 401 redirect must come before the generic error throw."""
        src = _workspace_js()
        idx_401 = src.find("res.status===401")
        idx_throw = src.find("throw new Error")
        assert idx_401 != -1, "401 guard not found in workspace.js"
        assert idx_throw != -1, "throw not found in workspace.js"
        assert idx_401 < idx_throw, \
            "401 redirect must appear before the generic throw in workspace.js"

    def test_ui_js_has_redirect_helper(self):
        """ui.js must define _redirectIfUnauth helper."""
        src = _ui_js()
        assert "_redirectIfUnauth" in src, \
            "ui.js must define _redirectIfUnauth helper function"

    def test_ui_js_models_fetch_uses_redirect(self):
        """populateModelDropdown() must call _redirectIfUnauth on the api/models response."""
        src = _ui_js()
        # The helper must be called after the api/models fetch
        assert "_redirectIfUnauth(_modelsRes)" in src, \
            "populateModelDropdown() must check 401 on api/models fetch"

    def test_ui_js_live_models_fetch_uses_redirect(self):
        """loadLiveModels() must call _redirectIfUnauth on the api/models/live response."""
        src = _ui_js()
        assert "_redirectIfUnauth(_liveRes)" in src, \
            "loadLiveModels() must check 401 on api/models/live fetch"

    def test_ui_js_upload_fetch_uses_redirect(self):
        """File upload must call _redirectIfUnauth on the api/upload response."""
        src = _ui_js()
        assert "_redirectIfUnauth(res)" in src, \
            "upload fetch must call _redirectIfUnauth"
