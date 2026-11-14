"""Enforcement hook e2e tests (governance port, builder B3).

Two layers, both fully isolated (injected policy loader, HERMES_HOME on a
tmp_path, monkeypatched api.auth accessors; no real ~/.hermes files, no
network, no live server):

1. Adapter behavior: api.governance.enforce.enforce_request driven with a
   fake handler covering mode off passthrough, report_only would_deny audit
   + passthrough, enforce 403 JSON vs friendly HTML, allowed-not-audited,
   auth-disabled = bootstrap identity, stale anonymous sessions, policy
   parse errors failing closed, and the never-read-the-body invariant.

2. server.py wiring: Handler.do_GET and Handler._handle_write call
   enforce_request after check_auth and before dispatch, a False return
   short-circuits dispatch, csp-report keeps bypassing both gates, and the
   request body reaches the route handler intact.
"""
import io
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.auth as auth  # noqa: E402
import server  # noqa: E402
from api.governance import loader  # noqa: E402
from api.governance.audit import read_audit_events  # noqa: E402
from api.governance.enforce import enforce_request  # noqa: E402
from api.governance.loader import parse_governance_policy  # noqa: E402

BOOTSTRAP = "michael@example.test"

POLICY = {
    "version": 1,
    "mode": "enforce",
    "default_effect": "deny",
    "bootstrap_admins": [BOOTSTRAP],
    "roles": {
        "admin": {
            "grants": {
                "permissions": [
                    "sessions:read", "sessions:write", "chat:use", "terminal:use",
                    "config:read", "config:write", "files:read", "files:write",
                ],
                "profiles": ["*"],
                "routes": ["*"],
            },
        },
        "viewer": {
            "grants": {
                "permissions": ["sessions:read", "config:read", "status:read"],
                "profiles": ["default"],
                "routes": ["/api/*"],
            },
        },
    },
    "users": {
        "admin@example.test": {"roles": ["admin"]},
        "viewer@example.test": {"roles": ["viewer"]},
    },
}


class _ExplodingBody(io.RawIOBase):
    """A request body stream that fails the test if the hook touches it."""

    def read(self, *a, **kw):  # pragma: no cover - only on regression
        raise AssertionError("enforcement hook must never read the request body")

    readline = read
    readinto = read


class FakeHandler:
    """Minimal check_auth-shaped handler: headers in, response capture out."""

    def __init__(self, path, headers=None):
        parsed = urlparse(path)
        self.path = path
        self.parsed = parsed
        self.headers = dict(headers or {})
        self.rfile = _ExplodingBody()
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    @property
    def body(self):
        return self.wfile.getvalue()


def _identity(email, groups=None, method="oidc"):
    return {"email": email, "groups": list(groups or []), "claims_subset": {}, "method": method}


@pytest.fixture
def governance_env(tmp_path, monkeypatch):
    """Isolated audit sink + injected policy + controllable auth identity."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = {"auth_enabled": True, "identity": None}
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: state["auth_enabled"])
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: "token-under-test")
    monkeypatch.setattr(auth, "get_session_identity", lambda cookie: state["identity"])

    def _set_policy(data):
        policy = parse_governance_policy(data)
        loader.set_policy_loader(lambda: policy)
        return policy

    state["set_policy"] = _set_policy
    try:
        yield state
    finally:
        loader.set_policy_loader(None)


def _audit_events():
    return read_audit_events(100)


# ── Adapter behavior: enforce_request on a fake handler ─────────────────────

def test_mode_off_is_a_passthrough(governance_env):
    governance_env["set_policy"]({"version": 1, "mode": "off", "default_effect": "deny"})
    handler = FakeHandler("/api/settings")

    assert enforce_request(handler, handler.parsed, "POST") is True
    assert handler.status is None
    assert handler.body == b""
    assert _audit_events() == []


@pytest.mark.parametrize("method,permission,owner,status", [
    ("GET", "files:read", "viewer@example.test", 200),
    ("GET", "files:read", "other@example.test", 404),
    ("GET", "", "viewer@example.test", 403),
    ("POST", "files:write", "other@example.test", 404),
    ("POST", "files:read", "viewer@example.test", 403),
])
def test_artifact_route_preserves_permission_and_owner_bounds(governance_env, monkeypatch, method, permission, owner, status):
    from copy import deepcopy
    from api import artifacts
    raw = deepcopy(POLICY)
    raw["roles"]["viewer"]["grants"]["permissions"] = [permission] if permission else []
    governance_env["set_policy"](raw)
    governance_env["identity"] = _identity("viewer@example.test")
    calls = []

    def service(method, path, **kwargs):
        calls.append((method, path))
        assert method == "GET", "another person's artifact must never be shared"
        return 200, {}, json.dumps({"artifact": {"source_profile": artifacts.owner_key(owner)}}).encode()

    monkeypatch.setattr(artifacts, "_request", service)
    monkeypatch.setattr(artifacts, "_admins", lambda: set())
    path = "/api/artifacts/11111111-1111-1111-1111-111111111111" + ("/share" if method == "POST" else "")
    handler = FakeHandler(path)
    if enforce_request(handler, handler.parsed, method):
        if method == "GET":
            artifacts.handle_get(handler, handler.parsed)
        else:
            artifacts.handle_post(handler, handler.parsed, {})
    assert handler.status == status
    assert bool(calls) is (status != 403)


@pytest.mark.parametrize("known", [True, False])
def test_app_intake_reaches_organisation_identity_guard(governance_env, monkeypatch, known):
    from api import msp_service
    from api.governance.models import GovernanceSubject
    policy = governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("viewer@example.test")
    subject = GovernanceSubject(email="viewer@example.test" if known else "outsider@example.test", provider="oidc")
    monkeypatch.setattr(msp_service, "context", lambda _handler: (policy, subject, None))
    calls = []
    monkeypatch.setattr(msp_service.control, "call", lambda *a, **kw: calls.append((a, kw)) or (200, {"ok": True}))
    handler = FakeHandler("/api/msp/app-intake")
    if enforce_request(handler, handler.parsed, "POST"):
        msp_service.handle_post(handler, handler.parsed.path, {"app_id": "qa_app", "scope": "organisation", "departments": []})
    assert handler.status == (200 if known else 403)
    assert bool(calls) is known


# NOTE: these deny-path tests used to POST /api/settings, which was gated on
# config:write. Since the appearance self-service change (26 Aug 2026 report)
# that POST rides on config:read at the route level (the config:write gate for
# non-cosmetic keys moved into the handler via api/settings_scope.py), so the
# viewer identity here would now pass the hook. POST /api/model (model:write)
# exercises the same deny mechanics.
def test_report_only_deny_passes_through_and_audits_would_deny(governance_env):
    governance_env["set_policy"]({**POLICY, "mode": "report_only"})
    governance_env["identity"] = _identity("viewer@example.test")
    handler = FakeHandler("/api/model")

    assert enforce_request(handler, handler.parsed, "POST") is True
    assert handler.status is None  # zero behavior change for the client

    events = _audit_events()
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "would_deny"
    assert event["reason"] == "permission_not_allowed"
    assert event["mode"] == "report_only"
    assert event["path"] == "/api/model"
    assert event["method"] == "POST"
    assert event["extra"]["resource"] == "model:write"
    # Identity is stored hashed, never raw
    raw = json.dumps(event)
    assert "viewer@example.test" not in raw


def test_enforce_deny_sends_403_json_and_audits_deny(governance_env):
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("viewer@example.test")
    handler = FakeHandler("/api/model")

    assert enforce_request(handler, handler.parsed, "POST") is False
    assert handler.status == 403
    assert handler.sent_headers["Content-Type"] == "application/json"
    assert int(handler.sent_headers["Content-Length"]) == len(handler.body)
    payload = json.loads(handler.body)
    assert payload == {
        "error": "forbidden",
        "resource": "model:write",
        "reason": "permission_not_allowed",
    }

    events = _audit_events()
    assert len(events) == 1
    assert events[0]["event"] == "deny"
    assert events[0]["reason"] == "permission_not_allowed"


def test_enforce_deny_get_navigation_gets_friendly_html(governance_env):
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("viewer@example.test")
    handler = FakeHandler(
        "/api/terminal/start",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )

    assert enforce_request(handler, handler.parsed, "GET") is False
    assert handler.status == 403
    assert handler.sent_headers["Content-Type"].startswith("text/html")
    page = handler.body.decode("utf-8")
    assert "Access restricted" in page
    assert "viewer@example.test" not in page  # no identities, no secrets


def test_enforce_deny_post_ignores_html_accept(governance_env):
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("viewer@example.test")
    handler = FakeHandler("/api/model", headers={"Accept": "text/html"})

    assert enforce_request(handler, handler.parsed, "POST") is False
    assert handler.sent_headers["Content-Type"] == "application/json"


def test_allowed_requests_are_not_audited(governance_env):
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("admin@example.test")
    handler = FakeHandler("/api/settings")

    assert enforce_request(handler, handler.parsed, "POST") is True
    assert handler.status is None
    assert _audit_events() == []


def test_auth_disabled_maps_to_bootstrap_and_never_denies(governance_env):
    governance_env["set_policy"](POLICY)
    governance_env["auth_enabled"] = False
    # Unknown route: everyone else fails closed, the bootstrap identity passes
    handler = FakeHandler("/api/not-in-the-catalog")

    assert enforce_request(handler, handler.parsed, "POST") is True
    assert handler.status is None
    assert _audit_events() == []


def test_stale_anonymous_session_denied_as_unauthenticated(governance_env):
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = None  # legacy float session: no identity
    handler = FakeHandler("/api/sessions")

    assert enforce_request(handler, handler.parsed, "GET") is False
    assert handler.status == 403
    assert json.loads(handler.body)["reason"] == "unauthenticated"


def test_policy_error_fails_closed(governance_env):
    def _broken():
        raise loader.GovernancePolicyError("bad yaml")

    loader.set_policy_loader(_broken)
    governance_env["identity"] = _identity("admin@example.test")
    handler = FakeHandler("/api/sessions")

    assert enforce_request(handler, handler.parsed, "GET") is False
    assert handler.status == 403
    assert json.loads(handler.body)["reason"] == "policy_error"


def test_profile_query_target_is_checked(governance_env):
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("viewer@example.test")
    handler = FakeHandler("/api/sessions?profile=other")

    assert enforce_request(handler, handler.parsed, "GET") is False
    assert json.loads(handler.body)["reason"] == "profile_not_allowed"

    allowed = FakeHandler("/api/sessions?profile=active")
    assert enforce_request(allowed, allowed.parsed, "GET") is True


def test_hook_never_reads_the_body(governance_env):
    # Every FakeHandler carries an exploding rfile; exercise both outcomes
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("viewer@example.test")

    denied = FakeHandler("/api/model")
    assert enforce_request(denied, denied.parsed, "POST") is False

    governance_env["identity"] = _identity("admin@example.test")
    allowed = FakeHandler("/api/settings")
    assert enforce_request(allowed, allowed.parsed, "POST") is True


def test_audit_failure_never_changes_the_decision(governance_env, monkeypatch):
    governance_env["set_policy"](POLICY)
    governance_env["identity"] = _identity("viewer@example.test")

    import api.governance.enforce as enforce_mod

    def _broken_audit(*a, **kw):
        raise OSError("audit sink unwritable")

    monkeypatch.setattr(enforce_mod, "append_audit_event", _broken_audit)
    handler = FakeHandler("/api/model")

    # Denial still happens in enforce mode even when auditing is down
    assert enforce_request(handler, handler.parsed, "POST") is False
    assert handler.status == 403


# ── server.py wiring: hook sits between check_auth and dispatch ─────────────

def _server_handler(path, command, body=b""):
    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    handler.command = command
    handler.headers = {}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 12345)
    handler.request_version = "HTTP/1.1"
    return handler


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/git/diff?session_id=s&path=private.txt"),
    ("GET", "/api/git/status?session_id=s"),
    ("GET", "/api/git-info?session_id=s"),
    ("GET", "/api/git/log?session_id=s"),
    ("POST", "/api/git/commit-message"), ("POST", "/api/git/checkout"),
    ("POST", "/api/git/stage"), ("POST", "/api/git/commit"),
])
def test_managed_resource_deny_blocks_git_before_any_subprocess(governance_env, monkeypatch, method, path):
    from copy import deepcopy
    policy = deepcopy(POLICY)
    policy["roles"]["admin"]["grants"]["permissions"].extend(["git:read", "git:write"])
    policy["users"]["admin@example.test"].update(access_level="elevated", access_mode="blacklist",
        deny={"files": {"read_roots": ["/private/restricted"]}})
    governance_env["set_policy"](policy)
    governance_env["identity"] = _identity("admin@example.test")
    monkeypatch.setattr(server, "check_auth", lambda h, p: True)
    def forbidden_dispatch(*args):
        pytest.fail("Git subprocess dispatch must not be reached")
    monkeypatch.setattr(server, "handle_get", forbidden_dispatch)
    handler = _server_handler(path, method)
    handler.requestline = f"{method} {path} HTTP/1.1"
    if method == "GET":
        server.Handler.do_GET(handler)
    else:
        server.Handler._handle_write(handler, forbidden_dispatch)
    assert b" 403 " in handler.wfile.getvalue().split(b"\r\n", 1)[0]


def test_do_get_runs_hook_after_auth_and_blocks_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "check_auth", lambda h, p: calls.append("auth") or True)
    monkeypatch.setattr(
        server, "enforce_request",
        lambda h, p, m: calls.append(("enforce", m)) or False,
    )
    monkeypatch.setattr(server, "handle_get", lambda h, p: calls.append("dispatch") or True)

    server.Handler.do_GET(_server_handler("/api/settings", "GET"))

    assert calls == ["auth", ("enforce", "GET")]  # denied: dispatch never ran


def test_do_get_allows_dispatch_when_hook_passes(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "check_auth", lambda h, p: True)
    monkeypatch.setattr(server, "enforce_request", lambda h, p, m: calls.append("enforce") or True)
    monkeypatch.setattr(server, "handle_get", lambda h, p: calls.append("dispatch") or True)

    server.Handler.do_GET(_server_handler("/api/settings", "GET"))

    assert calls == ["enforce", "dispatch"]


def test_do_get_auth_failure_skips_the_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "check_auth", lambda h, p: False)
    monkeypatch.setattr(server, "enforce_request", lambda h, p, m: calls.append("enforce") or True)
    monkeypatch.setattr(server, "handle_get", lambda h, p: calls.append("dispatch") or True)

    server.Handler.do_GET(_server_handler("/api/settings", "GET"))

    assert calls == []  # authn owns the 401; governance never consulted


@pytest.mark.parametrize("command", ["POST", "PUT", "PATCH", "DELETE"])
def test_handle_write_runs_hook_with_real_method_and_blocks(monkeypatch, command):
    calls = []
    monkeypatch.setattr(server, "check_auth", lambda h, p: calls.append("auth") or True)
    monkeypatch.setattr(
        server, "enforce_request",
        lambda h, p, m: calls.append(("enforce", m)) or False,
    )

    def route(h, p):
        calls.append("dispatch")
        return True

    server.Handler._handle_write(_server_handler("/api/settings", command), route)

    assert calls == ["auth", ("enforce", command)]


def test_csp_report_post_bypasses_auth_and_governance(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "check_auth", lambda h, p: calls.append("auth") or True)
    monkeypatch.setattr(server, "enforce_request", lambda h, p, m: calls.append("enforce") or True)

    def route(h, p):
        calls.append("dispatch")
        return True

    server.Handler._handle_write(_server_handler("/api/csp-report", "POST"), route)

    assert calls == ["dispatch"]  # unauthenticated browser reports keep working


def test_write_body_reaches_route_intact(monkeypatch, tmp_path):
    """The real hook must not consume the request body before dispatch."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "check_auth", lambda h, p: True)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    policy = parse_governance_policy({**POLICY, "mode": "report_only"})
    loader.set_policy_loader(lambda: policy)

    payload = json.dumps({"key": "value"}).encode("utf-8")
    seen = {}

    def route(h, p):
        seen["body"] = h.rfile.read(len(payload))
        return True

    try:
        server.Handler._handle_write(_server_handler("/api/settings", "POST", body=payload), route)
    finally:
        loader.set_policy_loader(None)

    assert seen["body"] == payload
