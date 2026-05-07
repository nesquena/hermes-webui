"""Regression test for #1850 — CSP connect-src must allow cdn.jsdelivr.net.

xterm.js, xterm-addon-fit, and xterm-addon-web-links are loaded from
cdn.jsdelivr.net via <script> tags. Their bundled source maps also live on
jsDelivr and are fetched via connect (not script load), so connect-src must
include cdn.jsdelivr.net or browsers block the fetch and emit CSP violations.
"""
import re
from pathlib import Path

_HELPERS_PY = Path(__file__).resolve().parents[1] / "api/helpers.py"


def _helpers_src() -> str:
    return _HELPERS_PY.read_text()


class TestCSPConnectSrcJsdelivr:
    """connect-src must allow cdn.jsdelivr.net for xterm source map fetches."""

    def test_connect_src_includes_jsdelivr(self):
        """connect-src must include https://cdn.jsdelivr.net."""
        src = _helpers_src()
        connect_match = re.search(r"connect-src\s+([^;]+);", src)
        assert connect_match, "connect-src directive must exist in CSP"
        assert "https://cdn.jsdelivr.net" in connect_match.group(1), (
            "connect-src must allow cdn.jsdelivr.net — xterm.js source maps are "
            "fetched from that origin and the CSP blocks them without this entry"
        )

    def test_connect_src_still_includes_self(self):
        """connect-src must still include 'self' alongside the new jsdelivr entry."""
        src = _helpers_src()
        connect_match = re.search(r"connect-src\s+([^;]+);", src)
        assert connect_match, "connect-src directive must exist in CSP"
        assert "'self'" in connect_match.group(1), (
            "connect-src must retain 'self' after adding cdn.jsdelivr.net"
        )
