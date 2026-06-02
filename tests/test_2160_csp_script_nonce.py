import re

from api.helpers import apply_script_nonce, t


class _HeaderCapture:
    def __init__(self):
        self.status = None
        self.headers = []
        self.body = b""

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def wfile(self):
        capture = self

        class _WFile:
            def write(self, body):
                capture.body += body

        return _WFile()

    def log_message(self, *args, **kwargs):
        pass


def _header(handler, key):
    for name, value in handler.headers:
        if name.lower() == key.lower():
            return value
    raise AssertionError(f"missing header {key}")


def test_html_responses_can_use_nonce_script_csp_without_unsafe_inline():
    handler = _HeaderCapture()
    t(handler, "<script nonce=\"abc\">ok()</script>", content_type="text/html; charset=utf-8", csp_nonce="abc")

    csp = _header(handler, "Content-Security-Policy")
    script_src = re.search(r"script-src ([^;]+)", csp).group(1)
    assert "'unsafe-inline'" not in script_src
    assert "'nonce-abc'" in script_src


def test_apply_script_nonce_updates_each_script_tag_missing_nonce():
    html = "<script>one()</script><script src=\"/x.js\"></script><script nonce=\"keep\">two()</script>"
    updated = apply_script_nonce(html, "fresh")
    assert updated.count('nonce="fresh"') == 2
    assert 'nonce="keep"' in updated
