"""read_body must reject malformed / non-object JSON bodies with ValueError.

Previously a malformed body silently became {} (so clients got a misleading
'Missing field' 400) and a non-dict JSON body (e.g. `[1,2,3]`) flowed into
handlers where body.get(...) raised AttributeError and surfaced as a generic
500. handle_post already maps read_body ValueError to a clean 400; the
PATCH/DELETE/PUT dispatchers now do the same.
"""
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest


class _Headers:
    def __init__(self, value):
        self._value = str(value)

    def get(self, name, default=None):
        return self._value if name.lower() == "content-length" else default


def _handler(raw: bytes):
    import io

    return SimpleNamespace(
        headers=_Headers(len(raw)), rfile=io.BytesIO(raw), close_connection=False
    )


def test_read_body_returns_parsed_dict():
    from api.helpers import read_body

    assert read_body(_handler(b'{"a": 1}')) == {"a": 1}


def test_read_body_empty_body_defaults_to_empty_dict():
    from api.helpers import read_body

    handler = _handler(b"")
    assert read_body(handler) == {}


@pytest.mark.parametrize("raw", [b" ", b"\n", b" \n\t ", b"\r\n"])
def test_read_body_whitespace_only_body_defaults_to_empty_dict(raw):
    """A body-optional endpoint (e.g. DELETE /api/mcp/servers/{name}) may send a
    whitespace-only body; it must be treated like an empty body ({}), not a 400."""
    from api.helpers import read_body

    assert read_body(_handler(raw)) == {}


@pytest.mark.parametrize("raw", [b"{bad json"])
def test_read_body_raises_on_malformed_json(raw):
    from api.helpers import read_body

    handler = _handler(raw)
    with pytest.raises(ValueError, match="Invalid JSON body"):
        read_body(handler)
    assert handler.close_connection is False


@pytest.mark.parametrize(
    "raw",
    [b"[1, 2, 3]", b'"a string"', b"42", b"true", b"null"],
)
def test_read_body_raises_on_non_object_json(raw):
    from api.helpers import read_body

    with pytest.raises(ValueError, match="JSON body must be an object"):
        read_body(_handler(raw))


def _raw_request(base_url, method, path, body: bytes):
    req = urllib.request.Request(
        base_url + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_post_malformed_json_gets_clean_400(base_url):
    status, payload = _raw_request(base_url, "POST", "/api/session/update", b"{bad json")
    assert status == 400
    assert b"Invalid JSON body" in payload


def test_post_non_object_json_gets_clean_400_not_500(base_url):
    status, payload = _raw_request(base_url, "POST", "/api/session/update", b"[1, 2, 3]")
    assert status == 400
    assert b"JSON body must be an object" in payload


@pytest.mark.parametrize("method", ["PATCH", "PUT"])
def test_patch_put_malformed_json_gets_clean_400_not_500(base_url, method):
    status, payload = _raw_request(base_url, method, "/api/mcp/servers/x", b"{bad json")
    assert status == 400
    assert b"Invalid JSON body" in payload


def test_delete_malformed_json_gets_clean_400_not_500(base_url):
    status, payload = _raw_request(base_url, "DELETE", "/api/prompts", b"{bad json")
    assert status == 400
    assert b"Invalid JSON body" in payload
