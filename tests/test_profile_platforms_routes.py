"""Route-level smoke tests for the per-profile messaging-platform endpoints.

  GET    /api/profile/gateway/platforms?name=<profile>
  POST   /api/profile/gateway/platform?name=<profile>   body: {platform, values}
  DELETE /api/profile/gateway/platform?name=<profile>&platform=<key>

These exercise the routes.py string-match dispatch; the helper logic is
covered in test_profile_platforms_api.py.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def _get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def _post(path: str, body: dict):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def _delete(path: str):
    req = urllib.request.Request(BASE + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def test_list_route_missing_name_returns_400(cleanup_test_sessions):
    code, _ = _get("/api/profile/gateway/platforms")
    assert code == 400


def test_list_route_unknown_profile_returns_404(cleanup_test_sessions):
    code, _ = _get("/api/profile/gateway/platforms?name=does-not-exist-xyz")
    assert code == 404


def test_list_route_default_profile_returns_payload(cleanup_test_sessions):
    """Either ok:true with `platforms` (if hermes_cli importable) or
    ok:false with `message` (degraded mode). Both are valid responses
    for the route layer — assert one of those shapes."""
    code, body = _get("/api/profile/gateway/platforms?name=default")
    assert code == 200, body
    assert "ok" in body
    if body.get("ok"):
        assert body.get("profile") == "default"
        assert isinstance(body.get("platforms"), list)
    else:
        assert body.get("message") == "hermes-agent not available"


def test_post_route_missing_name_returns_400(cleanup_test_sessions):
    code, _ = _post("/api/profile/gateway/platform",
                    {"platform": "telegram", "values": {}})
    assert code == 400


def test_delete_route_missing_platform_returns_400(cleanup_test_sessions):
    code, _ = _delete("/api/profile/gateway/platform?name=default")
    assert code == 400
