"""Regression test: CSP must declare an explicit manifest-src directive.

PR #920 added static/manifest.json for PWA support. Without an explicit
manifest-src directive the browser falls back to default-src and emits
a noisy console warning. This test locks the explicit directive in place.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


class TestManifestSrcCSP:
    """manifest-src must be explicitly declared in the Content-Security-Policy."""

    def _csp(self) -> str:
        text = (ROOT / "api" / "helpers.py").read_text(encoding="utf-8")
        start = text.find("Content-Security-Policy")
        assert start != -1, "Content-Security-Policy not found in helpers.py"
        # Grab the full CSP string (up to the closing paren of send_header)
        chunk = text[start : start + 600]
        return chunk

    def test_manifest_src_self_present(self):
        """CSP must contain an explicit manifest-src 'self' directive."""
        assert "manifest-src 'self'" in self._csp(), (
            "manifest-src 'self' missing from CSP — browsers will fall back to "
            "default-src and emit console warnings when loading the PWA manifest"
        )

    def test_manifest_src_is_explicit_not_just_default(self):
        """manifest-src must not rely solely on default-src fallback."""
        csp = self._csp()
        # Ensure manifest-src appears as its own directive keyword
        assert "manifest-src" in csp, "manifest-src directive absent from CSP"

    def test_existing_directives_unchanged(self):
        """Existing CSP directives must still be present after the manifest-src addition."""
        csp = self._csp()
        for directive in (
            "default-src 'self'",
            "script-src",
            "style-src",
            "font-src",
            "connect-src",
            "base-uri 'self'",
            "form-action 'self'",
        ):
            assert directive in csp, f"Expected CSP directive missing: {directive}"
