"""Artifact publish/serve/version/revoke lifecycle (/api/artifact/*, /artifact/<token>).

Covers the opt-in artifact feature: stable versioned URLs for agent-produced
files, sandbox CSP on HTML, deny-listed sources, credential redaction on
public text artifacts, and 404-on-revoke.
"""

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests._pytest_port import BASE


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.read(), e.code, dict(e.headers)


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return json.loads(raw), e.code
        except Exception:
            return {"raw": raw.decode("utf-8", "replace")}, e.code


@pytest.fixture()
def artifacts_on():
    post("/api/settings", {"artifacts_enabled": True})
    yield
    post("/api/settings", {"artifacts_enabled": False})


def _tmp_file(suffix: str, content: bytes) -> str:
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="artifact-test-", dir="/tmp")
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    return name


def test_disabled_flag_hides_all_artifact_routes():
    post("/api/settings", {"artifacts_enabled": False})
    body, status = post("/api/artifact/publish", {"path": "/tmp/whatever.html"})
    assert status == 404
    _, status, _ = get("/artifact/sometoken123")
    assert status == 404
    _, status, _ = get("/api/artifact/list")
    assert status == 404


def test_publish_serve_version_roundtrip(artifacts_on):
    src = _tmp_file(".html", b"<title>V1</title><h1>version one</h1>")
    try:
        body, status = post("/api/artifact/publish", {"path": src, "title": "Report"})
        assert status == 200 and body.get("ok"), body
        art = body["artifact"]
        assert art["version"] == 1
        token = art["token"]
        url = art["url"]
        assert url == f"/artifact/{token}"

        data, status, headers = get(url)
        assert status == 200
        assert b"version one" in data
        assert headers.get("Content-Type", "").startswith("text/html")
        assert headers.get("Content-Security-Policy") == "sandbox allow-scripts"
        assert "inline" in headers.get("Content-Disposition", "")

        # Re-publish same source: version 2 under the SAME url
        Path(src).write_bytes(b"<title>V2</title><h1>version two</h1>")
        body, status = post("/api/artifact/publish", {"path": src})
        assert status == 200
        assert body["artifact"]["version"] == 2
        assert body["artifact"]["token"] == token

        data, status, _ = get(url)
        assert status == 200 and b"version two" in data
        data, status, _ = get(url + "?v=1")
        assert status == 200 and b"version one" in data
        data, status, _ = get(url + "?v=3")
        assert status == 404
    finally:
        os.unlink(src)


def test_publish_denied_sources(artifacts_on):
    # Deny-listed basename, even inside an allowed root
    denied = _tmp_file(".yaml", b"secret: 1")
    denied_named = str(Path(denied).parent / "config.yaml")
    os.rename(denied, denied_named)
    try:
        body, status = post("/api/artifact/publish", {"path": denied_named})
        assert status == 400, body
    finally:
        os.unlink(denied_named)

    # Outside the publishable roots
    body, status = post("/api/artifact/publish", {"path": "/etc/hostname"})
    assert status == 400, body

    # Missing file
    body, status = post("/api/artifact/publish", {"path": "/tmp/does-not-exist-xyz.html"})
    assert status == 400, body


def test_public_text_artifact_is_credential_redacted(artifacts_on):
    secret = "sk-ant-api03-abcdefghij1234567890abcdefghij1234567890"
    src = _tmp_file(".html", f"<p>key={secret}</p>".encode())
    try:
        body, status = post(
            "/api/artifact/publish", {"path": src, "public": True, "title": "Leaky"},
        )
        assert status == 200, body
        data, status, _ = get(body["artifact"]["url"])
        assert status == 200
        assert secret.encode() not in data, "public artifact must be credential-redacted"
    finally:
        os.unlink(src)


def test_revoke_removes_from_serving_and_list(artifacts_on):
    src = _tmp_file(".html", b"<p>bye</p>")
    try:
        body, status = post("/api/artifact/publish", {"path": src})
        assert status == 200
        token = body["artifact"]["token"]

        listed, status = post_get_list()
        assert any(a["token"] == token for a in listed)

        body, status = post("/api/artifact/revoke", {"token": token})
        assert status == 200

        _, status, _ = get(f"/artifact/{token}")
        assert status == 404
        listed, _ = post_get_list()
        assert not any(a["token"] == token for a in listed)

        # Revoked token cannot be re-published onto
        body, status = post("/api/artifact/publish", {"path": src, "token": token})
        assert status == 400
    finally:
        os.unlink(src)


def post_get_list():
    data, status, _ = get("/api/artifact/list")
    payload = json.loads(data)
    return payload.get("artifacts") or [], status


def test_malformed_tokens_404(artifacts_on):
    for bad_token in ("..", "a", "x" * 100, "abc%2F..%2Fdef", "abcdefgh!$"):
        _, status, _ = get(f"/artifact/{bad_token}")
        assert status == 404, bad_token


def test_png_serves_inline_without_csp(artifacts_on):
    png = (
        b"\x89PNG\r\n\x1a\n" + bytes.fromhex(
            "0000000d494844520000000100000001080600000"
            "01f15c4890000000d49444154789c626001000000"
            "05000106a2f8dd0000000049454e44ae426082"
        )
    )
    src = _tmp_file(".png", png)
    try:
        body, status = post("/api/artifact/publish", {"path": src})
        assert status == 200
        data, status, headers = get(body["artifact"]["url"])
        assert status == 200
        assert headers.get("Content-Type", "").startswith("image/png")
        # The server-wide default CSP may be present; what matters is that the
        # HTML-only sandbox directive is NOT applied to image responses.
        assert "sandbox" not in headers.get("Content-Security-Policy", "")
        assert data.startswith(b"\x89PNG")
    finally:
        os.unlink(src)
