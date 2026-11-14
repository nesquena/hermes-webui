"""Governance admin API tests (api/governance_api.py).

Exercises handle_governance_api against a fake http.server handler with an
isolated HERMES_HOME (tmp policy file, tmp audit file) and injected caller
identities. No real ~/.hermes files are read or written.
"""
import inspect
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import governance_api  # noqa: E402
from api.governance import loader  # noqa: E402
from api.governance.audit import read_audit_events  # noqa: E402
from api.governance.loader import policy_etag  # noqa: E402

BOOTSTRAP = "michael@example.test"

POLICY = {
    "version": 1,
    "mode": "report_only",
    "default_effect": "deny",
    "bootstrap_admins": [BOOTSTRAP],
    "roles": {
        "admin": {
            "grants": {
                "permissions": [
                    "governance:read", "governance:write", "governance:preview",
                    "governance:audit:read", "governance:usage:read",
                    "sessions:read", "chat:use",
                ],
                "profiles": ["*"],
                "routes": ["*"],
            },
        },
        "viewer": {
            "grants": {
                "permissions": ["sessions:read", "files:read"],
                "profiles": ["default"],
                "routes": ["/api/session*"],
            },
        },
    },
    "groups": {
        "sw-admins": {"sso_groups": ["workspace-admins"], "roles": ["admin"]},
    },
    "users": {
        "admin@example.test": {"roles": ["admin"]},
        "viewer@example.test": {"roles": ["viewer"]},
        BOOTSTRAP: {"roles": ["admin"]},
    },
}


class FakeHandler:
    """Minimal handler satisfying j()/read_body()/If-Match plumbing."""

    def __init__(self, body=None, headers=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        self.headers = dict(headers or {})
        if raw:
            self.headers.setdefault("Content-Length", str(len(raw)))
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    @property
    def body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _identity(email, groups=None, method="oidc"):
    return {"email": email, "groups": list(groups or []), "claims_subset": {}, "method": method}


def _parsed(path, query=""):
    return SimpleNamespace(path=path, query=query)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_WEBUI_GOVERNANCE_POLICY", raising=False)
    loader.set_policy_loader(None)
    yield tmp_path
    loader.set_policy_loader(None)


@pytest.fixture
def policy_file(isolated_home):
    def _write(data=POLICY):
        path = isolated_home / "dashboard-governance.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path
    return _write


@pytest.fixture
def as_user(monkeypatch):
    def _set(email, groups=None, method="oidc"):
        identity = _identity(email, groups, method) if email is not None else None
        monkeypatch.setattr(governance_api, "_caller_identity", lambda handler: identity)
    return _set


def _call(path, method="GET", body=None, headers=None, query=""):
    handler = FakeHandler(body=body, headers=headers)
    handled = governance_api.handle_governance_api(handler, _parsed(path, query), method)
    return handled, handler


def _current_etag():
    return policy_etag(dict(loader.get_policy().raw))


# ── Dispatch boundaries ─────────────────────────────────────────────────────

def test_non_governance_path_is_not_claimed(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    handled, _ = _call("/api/sessions")
    assert handled is False


def test_unknown_governance_endpoint_404(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    handled, handler = _call("/api/governance/nope")
    assert handled is True
    assert handler.status == 404
    assert handler.body["error"] == "not_found"


# ── /api/governance/me ──────────────────────────────────────────────────────

def test_me_shape_admin(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    handled, handler = _call("/api/governance/me")
    assert handled is True and handler.status == 200
    me = handler.body
    assert me["workspace_navigation"]["kind"] == "admin"
    assert me["workspace_navigation"]["secondary"] == []
    assert me["email"] == "admin@example.test"
    assert me["mode"] == "report_only"
    assert me["is_bootstrap_admin"] is False
    assert "admin" in me["roles"]
    assert "governance:write" in me["permissions"]
    assert set(me) == {
        "email", "display_name", "method", "mode", "is_bootstrap_admin",
        "roles", "groups", "permissions", "profiles",
        # Left-navigation items this caller may not see, derived from the same
        # permissions that gate the panels' APIs (27 Aug 2026 ticket).
        "hidden_nav", "nav_audience", "workspace_navigation", "feature_permissions", "delegated_scope",
        "access_level", "access_mode", "approval_mode", "can_chat", "effective_access",
    }


def test_me_shape_viewer_and_bootstrap(policy_file, as_user):
    policy_file()
    as_user("viewer@example.test")
    _, handler = _call("/api/governance/me")
    me = handler.body
    assert me["is_bootstrap_admin"] is False
    assert "governance:write" not in me["permissions"]

    as_user(BOOTSTRAP)
    _, handler = _call("/api/governance/me")
    assert handler.body["is_bootstrap_admin"] is True
    assert "*" in handler.body["permissions"]


# ── GET policy ──────────────────────────────────────────────────────────────

def test_policy_get_requires_governance_read(policy_file, as_user):
    policy_file()
    as_user("viewer@example.test")
    _, handler = _call("/api/governance/policy")
    assert handler.status == 403
    assert handler.body == {
        "error": "forbidden", "resource": "governance:read", "reason": "permission_not_allowed",
    }


def test_policy_get_returns_etag_and_access(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call("/api/governance/policy")
    assert handler.status == 200
    body = handler.body
    assert body["policy"]["mode"] == "report_only"
    assert body["etag"] == _current_etag()
    assert handler.response_headers["ETag"] == f'"{body["etag"]}"'
    access = body["effective_access"]
    assert access["is_admin"] is True
    assert "claims" not in access and "claims" not in access["subject"]


# ── POST policy (full replace) ──────────────────────────────────────────────

def test_policy_replace_requires_if_match(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call("/api/governance/policy", "POST", body=dict(POLICY))
    assert handler.status == 412
    assert handler.body["error"] == "policy_conflict"

    _, handler = _call(
        "/api/governance/policy", "POST", body=dict(POLICY), headers={"If-Match": "stale"},
    )
    assert handler.status == 412


def test_policy_replace_happy_path_saves_and_audits(policy_file, as_user, isolated_home):
    policy_file()
    as_user("admin@example.test")
    etag = _current_etag()
    updated = json.loads(json.dumps(POLICY))
    updated["groups"]["sw-viewers"] = {"roles": ["viewer"]}
    _, handler = _call(
        "/api/governance/policy", "POST", body=updated, headers={"If-Match": f'"{etag}"'},
    )
    assert handler.status == 200
    assert handler.body["ok"] is True
    assert handler.body["etag"] != etag

    reloaded = loader.get_policy()
    assert "sw-viewers" in reloaded.raw["groups"]

    events = read_audit_events(10)
    change = [e for e in events if e.get("event") == "policy_change"]
    assert change and change[0]["reason"] == "policy_replace"
    extra = change[0]["extra"]
    assert extra["op"] == "policy_replace"
    assert extra["old_etag"] == etag and extra["new_etag"] == handler.body["etag"]
    # summaries only, never full documents
    assert "grants" not in json.dumps(extra)


def test_policy_replace_invalid_policy_400_keeps_file(policy_file, as_user):
    path = policy_file()
    as_user("admin@example.test")
    before = path.read_text(encoding="utf-8")
    bad = dict(POLICY)
    bad["mode"] = "bananas"
    _, handler = _call(
        "/api/governance/policy", "POST", body=bad, headers={"If-Match": _current_etag()},
    )
    assert handler.status == 400
    assert handler.body["error"] == "invalid_policy"
    assert path.read_text(encoding="utf-8") == before


# ── validate + preview ──────────────────────────────────────────────────────

def test_validate_reports_errors_without_saving(policy_file, as_user):
    path = policy_file()
    as_user("admin@example.test")
    before = path.read_text(encoding="utf-8")
    _, handler = _call(
        "/api/governance/validate", "POST", body={"policy": {"mode": "nope"}},
    )
    assert handler.status == 200
    assert handler.body["valid"] is False and handler.body["errors"]
    assert path.read_text(encoding="utf-8") == before

    _, handler = _call("/api/governance/validate", "POST", body={"policy": dict(POLICY)})
    assert handler.body == {"valid": True}


def test_preview_explains_sources_and_never_leaks_claims(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/preview", "POST",
        body={"email": "someone@example.test", "groups": ["workspace-admins"]},
    )
    assert handler.status == 200
    body = handler.body
    assert "governance:write" in body["effective_access"]["permissions"]
    assert any(src.startswith("group:") for src in body["grant_sources"])
    assert body["permission_sources"]
    assert "claims" not in json.dumps(body)


def test_preview_requires_valid_email(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call("/api/governance/preview", "POST", body={"email": "nope"})
    assert handler.status == 400


# ── groups CRUD ─────────────────────────────────────────────────────────────

def test_group_create_update_delete_cycle(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")

    _, handler = _call(
        "/api/governance/groups", "POST",
        body={"name": "sw-ops", "entry": {"roles": ["viewer"], "sso_groups": ["ops"]}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200 and handler.body["ok"] is True
    assert "sw-ops" in loader.get_policy().raw["groups"]

    _, handler = _call(
        "/api/governance/groups/update", "POST",
        body={"name": "sw-ops", "entry": {"roles": ["admin"]}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200
    assert loader.get_policy().raw["groups"]["sw-ops"] == {"roles": ["admin"]}

    _, handler = _call(
        "/api/governance/groups/delete", "POST",
        body={"name": "sw-ops"},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200
    assert "sw-ops" not in loader.get_policy().raw["groups"]


def test_group_create_conflict_409(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/groups", "POST",
        body={"name": "sw-admins", "entry": {}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 409
    assert handler.body["error"] == "conflict"


def test_group_update_unknown_404(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/groups/update", "POST",
        body={"name": "ghost", "entry": {}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 404
    assert handler.body["error"] == "not_found"


def test_group_invalid_entry_400(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    for entry in ("not-a-dict", {"unknown_key": 1}, {"roles": "admin"}, {"grants": []}):
        _, handler = _call(
            "/api/governance/groups", "POST",
            body={"name": "sw-x", "entry": entry},
            headers={"If-Match": _current_etag()},
        )
        assert handler.status == 400, entry
        assert handler.body["error"] == "invalid_payload"

    _, handler = _call(
        "/api/governance/groups", "POST", body={"entry": {}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 400


def test_group_mutation_stale_etag_412(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/groups", "POST",
        body={"name": "sw-x", "entry": {}},
        headers={"If-Match": "stale"},
    )
    assert handler.status == 412
    assert "sw-x" not in loader.get_policy().raw["groups"]


def test_groups_get_lists_entries(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call("/api/governance/groups")
    assert handler.status == 200
    assert "sw-admins" in handler.body["groups"]
    assert handler.body["etag"] == _current_etag()


# ── users CRUD ──────────────────────────────────────────────────────────────

def test_user_create_normalizes_email_and_conflicts(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/users", "POST",
        body={"email": "New.User@Example.Test", "entry": {"roles": ["viewer"]}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200
    assert "new.user@example.test" in loader.get_policy().raw["users"]

    _, handler = _call(
        "/api/governance/users", "POST",
        body={"email": "VIEWER@example.test", "entry": {}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 409


def test_user_create_requires_valid_email(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/users", "POST", body={"email": "not-an-email", "entry": {}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 400


def test_user_update_and_delete(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/users/update", "POST",
        body={"email": "viewer@example.test", "entry": {"roles": ["admin"]}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200
    assert loader.get_policy().raw["users"]["viewer@example.test"] == {"roles": ["admin"]}

    _, handler = _call(
        "/api/governance/users/delete", "POST",
        body={"email": "viewer@example.test"},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200
    assert "viewer@example.test" not in loader.get_policy().raw["users"]

    _, handler = _call(
        "/api/governance/users/delete", "POST",
        body={"email": "viewer@example.test"},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 404


def test_bootstrap_admin_delete_refused(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/users/delete", "POST",
        body={"email": BOOTSTRAP},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 400
    assert handler.body["error"] == "bootstrap_admin_protected"
    assert BOOTSTRAP in loader.get_policy().raw["users"]


def test_policy_replace_dropping_bootstrap_admin_refused(policy_file, as_user):
    # FINDING 3: a governance:write caller who is NOT a bootstrap admin cannot
    # replace the policy with an empty/reduced bootstrap_admins set, which would
    # permanently lock out the never-deny owner.
    policy_file()
    as_user("admin@example.test")
    etag = _current_etag()
    stripped = json.loads(json.dumps(POLICY))
    stripped["bootstrap_admins"] = []
    stripped["mode"] = "enforce"
    _, handler = _call(
        "/api/governance/policy", "POST",
        body=stripped, headers={"If-Match": f'"{etag}"'},
    )
    assert handler.status == 400
    assert handler.body["error"] == "bootstrap_admin_protected"
    # the on-disk policy still names the bootstrap admin
    assert BOOTSTRAP in loader.get_policy().raw["bootstrap_admins"]


def test_policy_replace_bootstrap_admin_may_rotate_own_set(policy_file, as_user):
    # FINDING 3: the bootstrap admin owns the never-deny set and may
    # deliberately rotate it (here: replace themselves with another admin).
    policy_file()
    as_user(BOOTSTRAP)
    etag = _current_etag()
    rotated = json.loads(json.dumps(POLICY))
    rotated["bootstrap_admins"] = ["successor@example.test"]
    _, handler = _call(
        "/api/governance/policy", "POST",
        body=rotated, headers={"If-Match": f'"{etag}"'},
    )
    assert handler.status is None or handler.status == 200, handler.body
    assert handler.body.get("ok") is True
    assert loader.get_policy().raw["bootstrap_admins"] == ["successor@example.test"]


# ── admin gate holds in every mode ──────────────────────────────────────────

@pytest.mark.parametrize("mode", ["enforce", "report_only", "off"])
def test_mutations_forbidden_for_non_admin_in_every_mode(policy_file, as_user, mode):
    data = json.loads(json.dumps(POLICY))
    data["mode"] = mode
    policy_file(data)
    as_user("viewer@example.test")
    for path, body in (
        ("/api/governance/policy", dict(POLICY)),
        ("/api/governance/groups", {"name": "x", "entry": {}}),
        ("/api/governance/users/delete", {"email": "admin@example.test"}),
    ):
        _, handler = _call(path, "POST", body=body, headers={"If-Match": _current_etag()})
        assert handler.status == 403, (mode, path)
        assert handler.body["reason"] == "permission_not_allowed"


def test_mutations_forbidden_with_no_policy_file(as_user):
    # No file: mode off, no bootstrap admins, nobody may bootstrap a policy.
    as_user("viewer@example.test")
    _, handler = _call(
        "/api/governance/groups", "POST", body={"name": "x", "entry": {}},
        headers={"If-Match": "anything"},
    )
    assert handler.status == 403


def test_bootstrap_admin_passes_admin_gate(policy_file, as_user):
    policy_file()
    as_user(BOOTSTRAP)
    _, handler = _call(
        "/api/governance/groups", "POST",
        body={"name": "sw-new", "entry": {"roles": ["viewer"]}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200


# ── audit + usage endpoints ─────────────────────────────────────────────────

def test_audit_endpoint_requires_permission_and_limits(policy_file, as_user):
    policy_file()
    as_user("viewer@example.test")
    _, handler = _call("/api/governance/audit")
    assert handler.status == 403

    as_user("admin@example.test")
    _, handler = _call(
        "/api/governance/groups", "POST",
        body={"name": "sw-audited", "entry": {}},
        headers={"If-Match": _current_etag()},
    )
    assert handler.status == 200
    _, handler = _call("/api/governance/audit", query="limit=1")
    assert handler.status == 200
    events = handler.body["events"]
    assert len(events) == 1
    assert events[0]["event"] == "policy_change"
    assert "admin@example.test" not in json.dumps(events)  # hashed subjects only


def test_usage_endpoint_shape(policy_file, as_user):
    policy_file()
    as_user("admin@example.test")
    _, handler = _call("/api/governance/usage")
    assert handler.status == 200
    assert set(handler.body) == {"usage", "caps"}


# ── auth-disabled caller mapping ────────────────────────────────────────────

def test_auth_disabled_maps_to_bootstrap_admin(policy_file, monkeypatch):
    policy_file()
    from api import auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    handler = FakeHandler()
    governance_api.handle_governance_api(handler, _parsed("/api/governance/me"), "GET")
    assert handler.status == 200
    assert handler.body["email"] == BOOTSTRAP
    assert handler.body["is_bootstrap_admin"] is True
    assert handler.body["method"] == "auth_disabled"


# ── routes.py wiring (CSRF gate ordering) ───────────────────────────────────

def test_routes_dispatch_governance_after_csrf_gate():
    from api import routes

    assert routes._csrf_exempt_path("/api/governance/users") is False
    assert routes._csrf_exempt_path("/api/governance/policy") is False

    post_src = inspect.getsource(routes.handle_post)
    csrf_at = post_src.index("_check_csrf")
    gov_at = post_src.index("handle_governance_api")
    assert csrf_at < gov_at, "governance POST dispatch must sit after the CSRF gate"

    get_src = inspect.getsource(routes.handle_get)
    assert "handle_governance_api" in get_src
