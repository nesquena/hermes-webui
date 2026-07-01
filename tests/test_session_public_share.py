import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            data = r.read()
            content_type = r.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(data), r.status, dict(r.headers)
            return data.decode("utf-8"), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        data = e.read()
        content_type = e.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(data), e.code, dict(e.headers)
        return data.decode("utf-8"), e.code, dict(e.headers)


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _make_session_with_messages():
    created, _ = post("/api/session/new", {})
    sid = created["session"]["session_id"]
    from api.models import Session

    session = Session.load(sid)
    assert session is not None
    session.title = "Shared Test"
    session.messages = [
        {"role": "user", "content": "Please summarize this."},
        {
            "role": "assistant",
            "content": "Here is a concise summary.",
            "provider_details": "HTTP 401: expired upstream token",
            "provider_details_label": "Provider details",
        },
        {"role": "tool", "content": "raw tool output should not be public"},
    ]
    session.workspace = "/very/private/workspace"
    session.profile = None
    session.save()
    return sid


def test_share_create_returns_public_url_and_persists_session_fields():
    sid = _make_session_with_messages()
    try:
        payload, status = post("/api/share/create", {"session_id": sid})
        assert status == 200
        assert payload["ok"] is True
        share = payload["share"]
        assert share["token"]
        assert share["url"].startswith("/share/")
        assert payload["session"]["share_token"] == share["token"]
        assert payload["session"]["share_created_at"]
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_public_share_payload_is_sanitized_and_read_only():
    sid = _make_session_with_messages()
    try:
        created, _ = post("/api/share/create", {"session_id": sid})
        token = created["share"]["token"]
        payload, status, headers = get(f"/api/share/{token}")
        assert status == 200
        assert headers.get("X-Robots-Tag") == "noindex, nofollow"
        share = payload["share"]
        assert share["source_session_id"] == sid
        assert share["title"] == "Shared Test"
        assert "workspace" not in share
        assert "profile" not in share
        assert share["message_count"] == 2
        assert [m["role"] for m in share["messages"]] == ["user", "assistant"]
        assert all("tool" != m["role"] for m in share["messages"])
        assert share["messages"][1]["provider_details"] == "HTTP 401: expired upstream token"
        assert share["messages"][1]["provider_details_label"] == "Provider details"
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_share_revoke_makes_link_unavailable():
    sid = _make_session_with_messages()
    try:
        created, _ = post("/api/share/create", {"session_id": sid})
        token = created["share"]["token"]
        revoked, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert revoked["ok"] is True
        missing, status, _ = get(f"/api/share/{token}")
        assert status == 404
        assert missing["error"] == "Shared conversation not found"
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_share_revoke_endpoint_hides_share_token_from_session():
    sid = _make_session_with_messages()
    try:
        post("/api/share/create", {"session_id": sid})
        payload, status = post("/api/share/revoke", {"session_id": sid})
        assert status == 200
        assert payload["session"]["share_token"] is None
        assert payload["session"]["share_created_at"] is None
    finally:
        post("/api/session/delete", {"session_id": sid})


def test_share_page_serves_public_html():
    body, status, _ = get("/share/example-token")
    assert status == 200
    assert "Hermes Shared Conversation" in body
    assert "static/share.js" in body
