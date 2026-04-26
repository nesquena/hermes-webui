"""Tests for #1118 — api() retries on network errors (stale keep-alive after idle)."""
import re


def _src() -> str:
    with open("static/workspace.js") as f:
        return f.read()


class TestApiRetryOnNetworkError:
    """The api() function in workspace.js should retry on fetch TypeError (network failure)."""

    def test_api_function_has_retry_loop(self):
        """api() must contain a retry loop (for/while with attempt counter)."""
        src = _src()
        assert re.search(r'for\s*\(\s*let\s+\w+\s*=\s*0\s*;', src), \
            "api() must have a for loop with attempt counter"

    def test_api_retries_on_typeerror(self):
        """api() must retry when fetch throws TypeError (network failure)."""
        src = _src()
        # Find the api function body
        m = re.search(r'async function api\(path,opts.*?\n\}(?!\n[\s]*function)', src, re.DOTALL)
        assert m, "api() function must exist"
        body = m.group(0)
        assert 'TypeError' in body, \
            "api() must check for TypeError to detect network failures"
        assert 'attempt' in body, \
            "api() must track attempt count for retry logic"

    def test_api_does_not_retry_http_errors(self):
        """api() must NOT retry on HTTP error responses (4xx/5xx) — only network failures."""
        src = _src()
        m = re.search(r'async function api\(path,opts.*?\n\}(?!\n[\s]*function)', src, re.DOTALL)
        assert m, "api() function must exist"
        body = m.group(0)
        # The retry should be in catch block (after res.ok check), not in the ok path
        # HTTP errors throw Error (not TypeError), so only TypeError triggers retry
        assert 'e instanceof TypeError' in body, \
            "api() retry must be limited to TypeError (network errors), not all errors"

    def test_api_max_3_attempts(self):
        """api() must limit retries (max 3 attempts total = 1 initial + 2 retries)."""
        src = _src()
        m = re.search(r'async function api\(path,opts.*?\n\}(?!\n[\s]*function)', src, re.DOTALL)
        assert m, "api() function must exist"
        body = m.group(0)
        # Should have attempt < 2 (0, 1, 2 = 3 attempts max)
        assert re.search(r'attempt\s*<\s*2', body), \
            "api() must limit to 3 attempts max (attempt < 2)"

    def test_api_preserves_401_redirect(self):
        """api() must still redirect to /login on 401 (auth expired)."""
        src = _src()
        assert "res.status===401" in src, \
            "api() must still check for 401 status"
        assert "/login?next=" in src, \
            "api() must still redirect to /login on 401"

    def test_api_preserves_error_parsing(self):
        """api() must still parse JSON error bodies for non-200 responses."""
        src = _src()
        assert "JSON.parse(text)" in src, \
            "api() must still parse JSON error responses"
        assert "res.json()" in src, \
            "api() must still parse JSON success responses"
