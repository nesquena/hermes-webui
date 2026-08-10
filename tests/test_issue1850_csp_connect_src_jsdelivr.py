"""CSP cdn.jsdelivr.net policy tests after partial vendoring.

xterm and Prism are vendored locally. PDF.js and Mermaid are still loaded
on demand from cdn.jsdelivr.net, so script-src and worker-src must allow
it. style-src and connect-src no longer need it.
"""
import re

from api.helpers import _build_csp_enforced_policy


def _policy() -> str:
    return _build_csp_enforced_policy("")


class TestCSPJsdelivrPartialVendoring:
    """cdn.jsdelivr.net must be in script-src/worker-src but not style-src/connect-src."""

    def test_script_src_allows_jsdelivr(self):
        """script-src must allow cdn.jsdelivr.net for dynamic PDF.js/Mermaid imports."""
        policy = _policy()
        script_match = re.search(r"script-src\s+([^;]+);", policy)
        assert script_match, "script-src directive must exist in CSP"
        assert "https://cdn.jsdelivr.net" in script_match.group(1)

    def test_worker_src_allows_jsdelivr(self):
        """worker-src must allow cdn.jsdelivr.net for PDF.js worker."""
        policy = _policy()
        worker_match = re.search(r"worker-src\s+([^;]+);", policy)
        assert worker_match, "worker-src directive must exist in CSP"
        assert "https://cdn.jsdelivr.net" in worker_match.group(1)

    def test_style_src_excludes_jsdelivr(self):
        """style-src no longer needs cdn.jsdelivr.net (Prism CSS vendored)."""
        policy = _policy()
        style_match = re.search(r"style-src\s+([^;]+);", policy)
        assert style_match, "style-src directive must exist in CSP"
        assert "cdn.jsdelivr.net" not in style_match.group(1)

    def test_connect_src_excludes_jsdelivr(self):
        """connect-src no longer needs cdn.jsdelivr.net (xterm source maps vendored)."""
        policy = _policy()
        connect_match = re.search(r"connect-src\s+([^;]+);", policy)
        assert connect_match, "connect-src directive must exist in CSP"
        assert "cdn.jsdelivr.net" not in connect_match.group(1)

    def test_connect_src_still_includes_self(self):
        """connect-src must still include 'self'."""
        policy = _policy()
        connect_match = re.search(r"connect-src\s+([^;]+);", policy)
        assert connect_match, "connect-src directive must exist in CSP"
        assert "'self'" in connect_match.group(1)
