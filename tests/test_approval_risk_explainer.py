"""What an administrator is told before approving a governance request.

Reported 28 Aug 2026 ("Explain to administrators what they are approving"): the
approvals queue named the item and nothing else, so a decision needed prior
knowledge of the policy model. api/capability_risk.py answers it from metadata
keyed off the structures that already decide the behaviour.

Two claims this file exists to keep honest:

* Approving a ROUTE request does not hand out a permission. It only makes the
  address reachable (api/grant_requests.py:47-50), and the spool records no
  HTTP method, so the read and the write permission are both reported as still
  required rather than one of them being presented as granted.
* Nothing expires. There is no TTL anywhere in api/approvals.py or in the
  policy document, so the duration line says "until an administrator takes it
  away" instead of implying a time box.
"""
import io
import json
import pathlib
import sys
import time
from types import SimpleNamespace

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api import approvals, capability_risk, config, governance_api, grant_requests  # noqa: E402
from api.capability_risk import (  # noqa: E402
    GKIND_RISKS,
    KIND_RISKS,
    PERMISSION_RISKS,
    RISKS,
    TOOLSET_RISKS,
    explain_entry,
    risk_digest,
)
from api.governance import loader, nav  # noqa: E402
from api.governance.audit import _SECRET_KEY_RE, read_audit_events  # noqa: E402
from api.governance.catalog import ROUTE_CATALOG, route_permission  # noqa: E402

GOV_JS = (REPO / "static" / "governance.js").read_text(encoding="utf-8")
EN_JS = (REPO / "static" / "i18n" / "en.js").read_text(encoding="utf-8")

BOOTSTRAP = "michael@example.test"
REQUESTER = "hrishi@example.test"

POLICY = {
    "version": 1,
    "mode": "report_only",
    "default_effect": "deny",
    "bootstrap_admins": [BOOTSTRAP],
    "roles": {
        "admin": {
            "grants": {
                "permissions": ["governance:read", "governance:write"],
                "profiles": ["*"],
                "routes": ["*"],
            },
        },
    },
    "groups": {},
    "users": {
        BOOTSTRAP: {"roles": ["admin"]},
        REQUESTER: {"roles": []},
    },
}


# ── Fixtures (same shape as tests/test_governance_approvals_kinds.py) ────────

class FakeHandler:
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


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_WEBUI_GOVERNANCE_POLICY", raising=False)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "webui")
    loader.set_policy_loader(None)
    (tmp_path / "dashboard-governance.yaml").write_text(yaml.safe_dump(POLICY), encoding="utf-8")
    yield tmp_path
    loader.set_policy_loader(None)


@pytest.fixture
def as_user(monkeypatch):
    def _set(email, groups=None, method="oidc"):
        identity = {"email": email, "groups": list(groups or []), "claims_subset": {}, "method": method}
        monkeypatch.setattr(governance_api, "_caller_identity", lambda handler: identity)
    return _set


def _call(path, method="GET", body=None, query=""):
    handler = FakeHandler(body=body)
    handled = governance_api.handle_governance_api(
        handler, SimpleNamespace(path=path, query=query), method
    )
    return handled, handler


def _spool_route_denial(tmp_path, value="/api/crons", gkind="route", email=REQUESTER):
    """Write one denial into the spool the queue ingests."""
    state = tmp_path / "webui"
    state.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (state / "governance-grant-requests.json").write_text(
        json.dumps({
            f"{email}|{gkind}|{value}": {
                "email": email, "gkind": gkind, "value": value, "tool": "",
                "reason": "route_not_allowed", "detail": value, "trigger": "",
                "count": 3, "first_seen": now, "last_seen": now,
            },
        }),
        encoding="utf-8",
    )
    return f"{email}|{gkind}|{value}"


def _spooled_item_shape():
    """The keys record_route_denial writes, read back from a real spool write."""
    grant_requests.record_route_denial(REQUESTER, "/api/crons")
    spool = grant_requests._load_spool()
    return sorted(next(iter(spool.values())).keys()) if spool else []


def _grant(gkind, value, email=REQUESTER):
    return {
        "kind": "grant", "key": f"{email}|{gkind}|{value}", "owner_email": email,
        "status": "pending", "payload": {"email": email, "gkind": gkind, "value": value},
    }


# ── Coverage: metadata cannot drift away from the structures it describes ────

def _catalog_permission_union():
    perms = {"cron:run"}  # route_permission special-cases POST .../run
    for rule in ROUTE_CATALOG:
        for perm in (rule.read_permission, rule.write_permission):
            if perm:
                perms.add(perm)
    return perms | set(nav.PANEL_PERMISSIONS.values())


def test_every_catalog_permission_is_documented():
    missing = _catalog_permission_union() - set(PERMISSION_RISKS)
    assert not missing, f"undocumented permissions: {sorted(missing)}"


def test_no_documented_permission_is_dead():
    """A renamed permission must not leave a stale sentence behind."""
    stale = set(PERMISSION_RISKS) - _catalog_permission_union()
    assert not stale, f"documented but no longer used: {sorted(stale)}"


def test_every_grant_kind_is_documented():
    assert set(GKIND_RISKS) == set(grant_requests._GRANT_TARGETS)


def test_every_approval_kind_is_documented():
    assert set(KIND_RISKS) == set(approvals.KINDS)


def test_every_default_toolset_is_documented():
    assert set(config._DEFAULT_TOOLSETS) <= set(TOOLSET_RISKS)


def test_every_risk_id_used_is_declared():
    for name, table in (("permission", PERMISSION_RISKS), ("gkind", GKIND_RISKS),
                        ("toolset", TOOLSET_RISKS), ("kind", KIND_RISKS)):
        for key, meta in table.items():
            for rid in meta.get("risks", ()):
                assert rid in RISKS, f"{name} {key} uses undeclared risk {rid}"


def test_risk_ids_survive_the_audit_redactor():
    """A risk id matching the audit secret pattern would land as [REDACTED]."""
    for rid in RISKS:
        assert _SECRET_KEY_RE.search(rid) is None
    for field in risk_digest(explain_entry(_grant("route", "/api/crons"))):
        assert _SECRET_KEY_RE.search(field) is None


# ── The route sentence: the correction that mattered most ───────────────────

def test_route_grant_never_claims_to_confer_the_permission():
    ex = explain_entry(_grant("route", "/api/crons"))
    assert ex["permissions"] == ["cron:read", "cron:write"]
    assert ex["source"] == "catalog"
    low = ex["capability"].lower()
    assert "no permission of its own" in low
    assert "they still need" in low
    # The permission's own capability sentence may appear as context, but never
    # as the capability being granted.
    assert PERMISSION_RISKS["cron:write"]["capability"] not in ex["capability"]
    assert any("cron:write" in note for note in ex["permission_notes"])


def test_route_grant_reports_both_permissions_because_the_method_is_unknown():
    """The denial spool stores no HTTP method, so neither one may be picked."""
    spool_item = json.dumps(_spooled_item_shape())
    assert '"method"' not in spool_item, "if the method is spooled, report it instead of both"
    for path, expected in (
        ("/api/file", ["files:read", "files:write"]),
        ("/api/models", ["model:read"]),
        ("/api/crons/abc/run", ["cron:read", "cron:run"]),
    ):
        assert explain_entry(_grant("route", path))["permissions"] == expected


def test_route_grant_policy_target_is_the_routes_allowlist():
    assert explain_entry(_grant("route", "/api/crons"))["policy_target"] == ["grants.routes"]


# ── Policy targets: what approving actually writes ──────────────────────────

def test_skill_grant_target_is_two_lists_not_one_dotted_path():
    ex = explain_entry(_grant("skill", "obsidian"))
    assert ex["policy_target"] == ["grants.skills.view", "grants.skills.load"]


def test_mcp_grant_target_names_the_tool_allowance_it_also_writes():
    ex = explain_entry(_grant("mcp", "notion"))
    assert ex["policy_target"][0] == "grants.mcp.servers"
    assert any("grants.mcp.tools.notion" in path for path in ex["policy_target"])
    assert "every tool" in " ".join(ex["policy_target"])
    assert "every tool" in ex["capability"]


def test_every_grant_kind_reports_a_policy_target():
    for gkind in grant_requests._GRANT_TARGETS:
        ex = explain_entry(_grant(gkind, "value"))
        assert ex["policy_target"], f"{gkind} reports no policy entry"
        assert all(path.startswith("grants.") for path in ex["policy_target"])


# ── Tools: authoritative or nothing ─────────────────────────────────────────

def test_unknown_tool_capability_refuses_to_describe_the_tool():
    ex = explain_entry(_grant("tool", "SomeUndocumentedTool"))
    assert ex["risks"] == []
    assert ex["data"] == ""
    assert "not described here" in ex["capability"]
    assert ex["tools"] == ["SomeUndocumentedTool"]


def test_mcp_prefixed_tool_stays_a_tool_grant_and_only_names_its_server():
    ex = explain_entry(_grant("tool", "mcp__notion__notion-search"))
    assert ex["external_systems"] == ["notion"]
    assert "'notion'" in ex["capability"]
    # A tool grant writes tools.builtins; substituting the mcp description
    # would describe a different grant with a different policy target.
    assert ex["policy_target"] == ["grants.tools.builtins"]
    assert GKIND_RISKS["mcp"]["capability"] not in ex["capability"]


def test_toolset_grant_uses_the_toolset_table():
    ex = explain_entry(_grant("toolset", "terminal"))
    assert ex["source"] == "toolsets"
    assert "file_write" in ex["risks"] and "data_access" in ex["risks"]
    assert "commands" in ex["capability"]


# ── Secrets and sensitive internals ─────────────────────────────────────────

def test_secret_glob_grant_never_quotes_content_or_a_credential():
    ex = explain_entry(_grant("secret_glob", "/home/someone/.env"))
    assert "data_access" in ex["risks"]
    assert ex["mitigations"], "the strongest mitigation copy belongs on this kind"
    joined = " ".join(
        [ex["capability"], ex["data"]] + ex["mitigations"] + ex["alternatives"]
    ).lower()
    for leak in ("password", "api key", "bearer", "authorization", "token", "/home/"):
        assert leak not in joined, f"secret_glob copy leaks {leak!r}"


PROSE_FIELDS = ("capability", "data", "scope_text", "duration")
LIST_PROSE_FIELDS = ("mitigations", "alternatives", "dependencies")


def _prose(ex):
    parts = [str(ex.get(f) or "") for f in PROSE_FIELDS]
    for field in LIST_PROSE_FIELDS:
        parts.extend(str(v) for v in ex.get(field) or [])
    return " ".join(parts)


def test_explanation_is_plain_language_without_internals():
    """Raw permission slugs are fine in this panel (the Preview tab already
    renders them); machine internals and credential words are not."""
    entries = [_grant(gkind, "value") for gkind in grant_requests._GRANT_TARGETS]
    entries += [
        {"kind": kind, "key": "thing", "owner_email": REQUESTER, "status": "pending", "payload": {}}
        for kind in approvals.KINDS if kind != "grant"
    ]
    for entry in entries:
        low = _prose(explain_entry(entry)).lower()
        assert low.strip(), f"{entry} produced no prose at all"
        for leak in ("traceback", ".py", "http/1", "sys.path", "localhost", "yaml",
                     "bearer", "api key", "password", "authorization", "api/"):
            assert leak not in low, f"{entry.get('kind')} copy leaks {leak!r}"


def test_no_explanation_field_carries_a_payload_secret():
    """Even a payload that should never hold one is not copied out wholesale."""
    entry = _grant("mcp", "notion")
    entry["payload"].update({"auth_header": "Authorization", "profile": "default"})
    blob = json.dumps(explain_entry(entry)).lower()
    assert "authorization" not in blob and "profile" not in blob


# ── Scope and duration ──────────────────────────────────────────────────────

def test_scope_is_authoritative_not_guessed():
    assert explain_entry(_grant("route", "/api/crons"))["scope"] == "user"
    owner = {"kind": "mcp", "key": "ctx", "owner_email": REQUESTER, "status": "pending",
             "payload": {"scope": "owner"}}
    assert explain_entry(owner)["scope"] == "owner"
    glob = {"kind": "mcp", "key": "ctx", "owner_email": REQUESTER, "status": "pending", "payload": {}}
    assert explain_entry(glob)["scope"] == "global"
    skill = {"kind": "skill", "key": "ops/deploy", "owner_email": REQUESTER, "status": "pending",
             "payload": {"scope": "owner"}}
    assert explain_entry(skill)["scope"] == "global", "an approved skill is global by rule"


def test_scope_text_says_who_it_applies_to_in_words():
    assert "one person" in explain_entry(_grant("route", "/api/crons"))["scope_text"].lower()
    glob = {"kind": "mcp", "key": "ctx", "owner_email": REQUESTER, "status": "pending", "payload": {}}
    assert "everybody" in explain_entry(glob)["scope_text"].lower()


def test_duration_states_the_truth_that_there_is_no_expiry():
    ex = explain_entry(_grant("route", "/api/crons"))
    assert ex["expires_at"] is None
    low = ex["duration"].lower()
    assert "administrator takes it away" in low
    for fabricated in ("hour", "day", "week", "30", "24"):
        assert fabricated not in low


# ── Skill descriptions come from the skill itself ───────────────────────────

def test_skill_capability_quotes_the_skill_s_own_description(isolated_home, monkeypatch):
    skills = isolated_home / "skills"
    (skills / "obsidian").mkdir(parents=True)
    (skills / "obsidian" / "SKILL.md").write_text(
        "---\nname: obsidian\ndescription: Reads and writes notes in the private vault.\n---\nbody\n",
        encoding="utf-8",
    )
    from api import routes as api_routes

    monkeypatch.setattr(api_routes, "_active_skills_dir", lambda: skills)
    ex = explain_entry(_grant("skill", "obsidian"))
    assert ex["source"] == "skill_frontmatter"
    assert "private vault" in ex["capability"]


def test_skill_text_stays_behind_skills_read(isolated_home, monkeypatch):
    """A skill's own description is skill content. Explaining an approval must
    not become a way around the permission that guards what it explains."""
    skills = isolated_home / "skills"
    (skills / "obsidian").mkdir(parents=True)
    (skills / "obsidian" / "SKILL.md").write_text(
        "---\nname: obsidian\ndescription: Reads and writes notes in the private vault.\n---\nbody\n",
        encoding="utf-8",
    )
    from api import routes as api_routes

    monkeypatch.setattr(api_routes, "_active_skills_dir", lambda: skills)
    without = explain_entry(_grant("skill", "obsidian"), skill_detail=False)
    assert "private vault" not in without["capability"]
    assert without["capability"], "the kind-level sentence still renders"
    assert "private vault" in explain_entry(_grant("skill", "obsidian"))["capability"]


def test_queue_asks_the_caller_s_entitlement_before_quoting_a_skill():
    """The admin queue passes the caller's access, never an assumption."""
    src = (REPO / "api" / "governance_api.py").read_text(encoding="utf-8")
    assert "skill_detail=_may_read_skills(access)" in src
    assert 'access.has_permission("skills:read")' in src
    assert "_approval_row(entry, explain=True, access=access)" in src


def test_missing_skill_description_degrades_instead_of_inventing_one(monkeypatch):
    from api import routes as api_routes

    monkeypatch.setattr(api_routes, "_active_skills_dir", lambda: pathlib.Path("/nonexistent"))
    ex = explain_entry(_grant("skill", "nothing-here"))
    assert ex["source"] == "grant_targets"
    assert "describes itself" not in ex["capability"]


# ── The composer never raises ───────────────────────────────────────────────

@pytest.mark.parametrize("entry", [
    {}, None, "nonsense", [],
    {"kind": "grant"},
    {"kind": "grant", "payload": {"gkind": "nope", "value": "x"}},
    {"kind": "plugin", "key": "x"},
    {"kind": "grant", "payload": {"gkind": "route", "value": ""}},
])
def test_composer_never_raises(entry):
    assert isinstance(explain_entry(entry), dict)


def test_unrecognised_grant_kind_still_says_something_useful():
    ex = explain_entry({"kind": "grant", "payload": {"gkind": "nope", "value": "x"}})
    assert "could not be identified" in ex["capability"]
    assert ex["source"] == "request_kinds"


def test_risk_digest_of_nothing_is_nothing():
    assert risk_digest({}) == {} and risk_digest(None) == {}


# ── API surface ─────────────────────────────────────────────────────────────

def test_admin_queue_row_carries_the_explanation(isolated_home, as_user):
    _spool_route_denial(isolated_home)
    as_user(BOOTSTRAP)
    _, handler = _call("/api/governance/approvals")
    assert handler.status == 200
    rows = handler.body["pending"]
    assert rows and rows[0]["explanation"]["capability"]
    assert rows[0]["explanation"]["permissions"] == ["cron:read", "cron:write"]


def test_mine_view_does_not_carry_reviewer_guidance(isolated_home, as_user):
    """The mitigations and alternatives are written for somebody deciding on
    another person's request, and /mine is reachable by any authenticated user."""
    approvals.request("mcp", "context7", REQUESTER, label="Context7")
    as_user(REQUESTER)
    _, handler = _call("/api/governance/approvals/mine")
    assert handler.status == 200
    rows = handler.body["requests"]
    assert rows and "explanation" not in rows[0]


def test_decide_response_row_is_unchanged(isolated_home, as_user):
    approvals.request("mcp", "context7", REQUESTER, label="Context7")
    as_user(BOOTSTRAP)
    _, handler = _call(
        "/api/governance/approvals/decide", "POST",
        body={"kind": "mcp", "key": "context7", "decision": "approve"},
    )
    assert handler.status == 200
    assert "explanation" not in handler.body["entry"]


def test_decision_audit_records_what_the_admin_was_shown(isolated_home, as_user):
    _spool_route_denial(isolated_home)
    as_user(BOOTSTRAP)
    _call("/api/governance/approvals")  # ingest the spool into the registry
    key = f"{REQUESTER}|route|/api/crons"
    _, handler = _call(
        "/api/governance/approvals/decide", "POST",
        body={"kind": "grant", "key": key, "decision": "approve"},
    )
    assert handler.status == 200
    decisions = [e for e in read_audit_events(limit=50) if e.get("event") == "approval_decision"]
    assert decisions, "approving an access request must leave an approval_decision behind"
    extra = decisions[-1].get("extra") or {}
    assert extra["op"] == "approvals.approve"
    assert extra["scope"] == "user"
    assert "cron:write" in extra["permissions"]
    assert extra["policy_target"] == ["grants.routes"]


def test_rejecting_an_access_request_is_audited_too(isolated_home, as_user):
    _spool_route_denial(isolated_home)
    as_user(BOOTSTRAP)
    _call("/api/governance/approvals")
    _, handler = _call(
        "/api/governance/approvals/decide", "POST",
        body={"kind": "grant", "key": f"{REQUESTER}|route|/api/crons", "decision": "reject"},
    )
    assert handler.status == 200
    ops = [(e.get("extra") or {}).get("op") for e in read_audit_events(limit=50)
           if e.get("event") == "approval_decision"]
    assert "approvals.reject" in ops


def test_a_broken_explanation_never_stops_the_queue(isolated_home, as_user, monkeypatch):
    monkeypatch.setattr(
        capability_risk, "explain_entry",
        lambda entry: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    approvals.request("mcp", "context7", REQUESTER, label="Context7")
    as_user(BOOTSTRAP)
    _, handler = _call("/api/governance/approvals")
    assert handler.status == 200
    explanation = handler.body["pending"][0]["explanation"]
    assert explanation["status"] == "unavailable"
    assert explanation["scope_text"] and explanation["capability"]


# ── The screen itself ───────────────────────────────────────────────────────

def test_the_detail_view_escapes_every_server_string():
    block = GOV_JS[GOV_JS.index("function _govExplainHtml"):][:3000]
    assert "innerHTML" not in block
    assert "<details" in block and "gov-explain" in block
    for raw in ("+ ex.capability", "+ ex.data", "+ id +", "+ label +"):
        assert raw not in block, f"unescaped interpolation: {raw}"
    # Both halves of every detail line, and the risk chip's tooltip, are escaped.
    assert "_govEsc(label)" in block and "_govEsc(text)" in block
    assert """title="' + _govEsc(label)""" in block, "the chip tooltip must be escaped too"
    assert block.count("_govEsc(") >= 6


def test_payload_summary_no_longer_dumps_unknown_keys():
    block = GOV_JS[GOV_JS.index("function _govPayloadSummary"):][:2000]
    assert "Object.keys(payload).forEach" not in block
    assert "auth_header" not in block and "transport" not in block
    assert "payload.url" in block, "the approver still needs the full MCP address"


def test_every_new_string_has_an_english_key():
    for key in (
        "governance_kind_grant", "governance_trigger", "governance_reason_blocked",
        "governance_blocked_times", "governance_explain_toggle",
        "governance_explain_capability", "governance_explain_data",
        "governance_explain_tools", "governance_explain_systems",
        "governance_explain_permission", "governance_explain_permission_notes",
        "governance_explain_scope", "governance_explain_duration",
        "governance_explain_mitigations", "governance_explain_alternatives",
        "governance_explain_dependencies", "governance_explain_target",
        "governance_risk_external_comms", "governance_risk_data_access",
        "governance_risk_file_write", "governance_risk_scheduling",
        "governance_risk_financial",
    ):
        assert f"{key}:" in EN_JS, f"missing English string: {key}"
        assert f"'{key}'" in GOV_JS, f"unused English string: {key}"


def test_the_toggle_click_cannot_decide_an_approval():
    """The disclosure needs no listener of its own: the delegation handler
    still bails unless the click landed on a decision button."""
    block = GOV_JS[GOV_JS.index("const approvals = document.getElementById('govPaneApprovals')"):][:800]
    assert "closest('[data-gov-approval]')" in block
    assert "if (!btn || btn.disabled) return;" in block
