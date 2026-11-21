"""HTTP-level tests for the vault prompt endpoints on the test server.

Covers route wiring on a real server boot:
  - GET /api/vault/pending returns {"pending": None} on an empty session
  - POST /api/vault/respond rejects malformed requests (missing session/prompt_id)
  - POST /api/vault/respond to an unknown prompt returns 409 + stale
"""

import json
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.usefixtures("test_server")


def _get(base_url, path):
    with urllib.request.urlopen(base_url + path, timeout=10) as r:
        return json.loads(r.read())


def _post(base_url, path, body):
    """POST and return (status, parsed-body) for success and error replies."""
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_vault_pending_empty(base_url):
    data = _get(base_url, "/api/vault/pending?session_id=nonexistent-session")
    assert data == {"pending": None}


def test_vault_respond_requires_session(base_url):
    status, body = _post(base_url, "/api/vault/respond", {"prompt_id": "abc"})
    assert status == 400
    assert "session_id is required" in body.get("error", "")


def test_vault_respond_requires_prompt_id(base_url):
    status, body = _post(base_url, "/api/vault/respond", {"session_id": "s", "prompt_id": ""})
    assert status == 400
    assert "prompt_id is required" in body.get("error", "")


def test_vault_respond_unknown_prompt_is_stale_409(base_url):
    status, body = _post(base_url, "/api/vault/respond", {
        "session_id": "ghost-session", "prompt_id": "deadbeef",
        "values": {"password": "x"}})
    assert status == 409
    assert body.get("ok") is False
    assert body.get("stale") is True
