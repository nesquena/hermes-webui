"""Route-level tests for the Agent Gateway tile redesign (Task 4).

Covers:
  * POST /api/profile/gateway rejects action='restart' with 400.
  * POST /api/profile/gateway accepts action='start' (route layer; the
    backend may still report ok:false when no real runner is present).
  * GET /api/profile/gateway/status returns 404 for unknown profile,
    400 when name missing, 200 with a valid phase for 'default'.
"""

import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def _get(path):
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def _post(path, body):
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


# ── POST /api/profile/gateway ──────────────────────────────────────────────


def test_post_rejects_restart(cleanup_test_sessions):
    """The redesigned toggle UX uses start+stop only; restart must 400."""
    code, body = _post("/api/profile/gateway", {"name": "default", "action": "restart"})
    assert code == 400, f"expected 400, got {code}: {body}"


def test_post_accepts_start(cleanup_test_sessions):
    """The route layer must accept action='start'.

    The backend may still report ok:false because the test environment
    has no real gateway runner, but the ROUTE itself must not refuse the
    action. We assert status != 400 (the route-level rejection code).
    """
    code, _body = _post("/api/profile/gateway", {"name": "default", "action": "start"})
    assert code != 400, (
        f"route refused a legal action=start (code={code}); the action "
        "validator should accept it even if the backend later reports ok:false"
    )


# ── GET /api/profile/gateway/status ────────────────────────────────────────


def test_get_status_unknown_profile_returns_404(cleanup_test_sessions):
    code, body = _get("/api/profile/gateway/status?name=does-not-exist")
    assert code == 404, f"expected 404, got {code}: {body}"


def test_get_status_missing_name_returns_400(cleanup_test_sessions):
    code, body = _get("/api/profile/gateway/status")
    assert code == 400, f"expected 400, got {code}: {body}"


def test_get_status_returns_phase_for_default(cleanup_test_sessions):
    code, body = _get("/api/profile/gateway/status?name=default")
    assert code == 200, f"expected 200, got {code}: {body}"
    assert body.get("profile") == "default", body
    assert body.get("phase") in {"stopped", "starting", "running", "stopping", "failed"}, body
