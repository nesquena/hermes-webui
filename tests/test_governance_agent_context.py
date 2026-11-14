"""Track A: per-turn agent-side governance binding for in-process AIAgent turns.

Covers api.governance.agent_context: admin bypass, non-admin binding with a
scoped GrantSet (real agent-side module when the hermes-agent checkout is
importable, faked otherwise), fail-closed on broken translation under enforce,
unrestricted-plus-audit under report_only, and unbind-after-turn. The policy is
injected via api.governance.loader.set_policy_loader and reset after every
test, matching test_governance_enforce.py.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.governance import loader  # noqa: E402
from api.governance import agent_context  # noqa: E402
from api.governance.agent_context import (  # noqa: E402
    GovernanceBindingError,
    bind_governed_agent_turn,
    governed_agent_turn,
    reset_governed_agent_turn,
)
from api.governance.loader import GovernancePolicyError, parse_governance_policy  # noqa: E402

BOOTSTRAP = "michael@example.test"
FREELANCER = "steve@example.test"

POLICY = {
    "version": 1,
    "mode": "enforce",
    "default_effect": "deny",
    "bootstrap_admins": [BOOTSTRAP],
    "roles": {
        "freelancer": {
            "grants": {
                "permissions": ["chat:use", "sessions:read"],
                "profiles": ["default"],
                "routes": ["/api/chat*", "/api/session*"],
                "tools": {"toolsets": ["files"], "builtins": ["read_file", "search_files"]},
                "mcp": {"servers": ["notion"], "tools": {"notion": ["notion-search"]}},
                "models": {"providers": ["openai"], "models": ["gpt-5"]},
                "files": {
                    "read_roots": ["/home/example/project"],
                    "write_roots": ["/home/example/project/out"],
                    "denied_globs": ["**/.env"],
                },
                "cli": {"commands": ["git"], "workdir_roots": ["/home/example/project"]},
                "usage_caps": {"tools_per_day": 100},
            },
        },
    },
    "users": {
        FREELANCER: {"roles": ["freelancer"]},
    },
}


@pytest.fixture
def inject_policy():
    def _set(data):
        policy = parse_governance_policy(data)
        loader.set_policy_loader(lambda: policy)
        return policy
    yield _set
    loader.set_policy_loader(None)


@pytest.fixture
def enforce_policy(inject_policy):
    return inject_policy(POLICY)


def _real_agent_module():
    """The real agent-side module when the hermes-agent checkout is on the
    box (the live deployment case); otherwise skip the integration tests."""
    try:
        return agent_context._agent_governance_module()
    except Exception:
        pytest.skip("hermes_cli.dashboard_governance not importable in this environment")


def _fake_agent_module(bound):
    """Minimal agent-module stand-in recording bind/reset calls."""
    def _bind(ctx):
        bound.append(ctx)
        return f"token-{len(bound)}"

    def _reset(token):
        bound.append(("reset", token))

    return SimpleNamespace(
        serialize_context_for_env=lambda ctx: "{}",
        context_from_env_payload=lambda payload: SimpleNamespace(payload=payload),
        bind_governance_context=_bind,
        reset_governance_context=_reset,
    )


@pytest.mark.parametrize("dropped", ["role_ceiling", "approval_configured"])
def test_managed_binding_rejects_engine_control_roundtrip_drift(inject_policy, monkeypatch, dropped):
    from copy import deepcopy
    from dataclasses import replace
    raw = deepcopy(POLICY)
    # Whitelist mode has a role ceiling; blacklist deliberately does not.
    raw["users"][FREELANCER].update(access_level="elevated", access_mode="whitelist",
        approval={"mode": "manual", "prompt": ""})
    policy = inject_policy(raw)
    module = _real_agent_module()
    original = module.context_from_env_payload
    def drift(payload):
        ctx = original(payload)
        assert ctx.access.role_ceiling is not None
        assert ctx.access.approval_configured is True
        return replace(ctx, access=replace(ctx.access, **{dropped: None if dropped == "role_ceiling" else False}))
    monkeypatch.setattr(module, "context_from_env_payload", drift)
    monkeypatch.setattr("api.bot_builder.access_ceiling", lambda *a: None)
    with pytest.raises(GovernanceBindingError, match="policy controls did not round-trip"):
        agent_context._translate_context(module, policy, FREELANCER, (), "default", "s", "r")


# ── unbound (no-op) paths ────────────────────────────────────────────────────

def test_admin_bypass_never_touches_agent_side(enforce_policy, monkeypatch):
    def _boom():
        raise AssertionError("agent side must not be imported for admins")

    monkeypatch.setattr(agent_context, "_agent_governance_module", _boom)
    # bare email, case-insensitive
    assert bind_governed_agent_turn(BOOTSTRAP.upper()) is None
    # identity-dict form
    assert bind_governed_agent_turn({"email": BOOTSTRAP, "groups": []}) is None
    with governed_agent_turn(BOOTSTRAP):
        pass  # unbound: current unrestricted behavior


def test_governance_off_and_ownerless_run_unbound(inject_policy, monkeypatch):
    def _boom():
        raise AssertionError("agent side must not be imported when unbound")

    monkeypatch.setattr(agent_context, "_agent_governance_module", _boom)
    inject_policy({"version": 1, "mode": "off", "default_effect": "deny"})
    assert bind_governed_agent_turn(FREELANCER) is None

    inject_policy(POLICY)
    # ownerless sessions (legacy/cron/gateway-imported) keep the dormant status quo
    assert bind_governed_agent_turn(None) is None
    assert bind_governed_agent_turn("") is None
    assert bind_governed_agent_turn({"email": ""}) is None


def test_policy_load_error_runs_unbound(monkeypatch):
    def _boom():
        raise GovernancePolicyError("bad policy")

    loader.set_policy_loader(_boom)
    monkeypatch.setattr(
        agent_context, "_agent_governance_module",
        lambda: pytest.fail("agent side must not be imported under policy error"),
    )
    try:
        # The route layer already fails closed on policy_error for the request
        # itself; the turn binder keeps current behavior instead of raising.
        assert bind_governed_agent_turn(FREELANCER) is None
    finally:
        loader.set_policy_loader(None)


# ── non-admin binding (scoped GrantSet) ──────────────────────────────────────

def test_non_admin_binds_scoped_context_on_real_agent_side(enforce_policy):
    agent_mod = _real_agent_module()
    assert agent_mod.current_governance_context() is None

    with governed_agent_turn(
        FREELANCER, active_profile="default", session_id="sess-1", request_id="stream-1"
    ):
        ctx = agent_mod.current_governance_context()
        assert ctx is not None
        # properly typed agent-side dataclass, not the webui twin
        assert type(ctx).__module__ == "hermes_cli.dashboard_governance.context"
        assert ctx.subject.email == FREELANCER
        assert ctx.access.mode == "enforce"
        assert ctx.active_profile == "default"
        assert ctx.session_id == "sess-1"
        assert ctx.request_id == "stream-1"
        grants = ctx.access.grants
        # every grant dimension survives the webui -> agent bridge
        assert grants.tools == frozenset({"read_file", "search_files"})
        assert grants.toolsets == frozenset({"files"})
        assert grants.mcp_servers == frozenset({"notion"})
        assert grants.mcp_tools == {"notion": frozenset({"notion-search"})}
        assert grants.model_providers == frozenset({"openai"})
        assert grants.models == frozenset({"gpt-5"})
        assert grants.file_read_roots == frozenset({"/home/example/project"})
        assert grants.file_write_roots == frozenset({"/home/example/project/out"})
        assert grants.file_denied_globs == frozenset({"**/.env"})
        assert grants.cli_commands == frozenset({"git"})
        assert grants.cli_workdir_roots == frozenset({"/home/example/project"})
        assert grants.usage_caps == {"tools_per_day": 100}
        # the dormant tool gate activates: no wildcard, so an unlisted tool
        # would be denied by decide_tool_access under mode enforce
        assert "*" not in grants.tools and "*" not in grants.toolsets

    assert agent_mod.current_governance_context() is None


def test_non_admin_bind_uses_fake_agent_module(enforce_policy, monkeypatch):
    bound = []
    monkeypatch.setattr(agent_context, "_agent_governance_module", lambda: _fake_agent_module(bound))

    token = bind_governed_agent_turn(FREELANCER, session_id="s", request_id="r")
    assert token is not None
    assert len(bound) == 1  # bind called exactly once
    reset_governed_agent_turn(token)
    assert bound[-1] == ("reset", "token-1")


# ── failure policy per mode ──────────────────────────────────────────────────

def test_fail_closed_under_enforce_when_agent_side_missing(enforce_policy, monkeypatch):
    def _broken():
        raise ImportError("hermes_cli unavailable")

    events = []
    monkeypatch.setattr(agent_context, "_agent_governance_module", _broken)
    monkeypatch.setattr(agent_context, "append_audit_event", lambda event, **kw: events.append((event, kw)))

    with pytest.raises(GovernanceBindingError) as excinfo:
        bind_governed_agent_turn(FREELANCER, session_id="sess-x", request_id="stream-x")
    assert "Access restricted" in str(excinfo.value)
    assert events and events[0][0] == "agent_governance_bind_failed"
    assert events[0][1]["report_only"] is False
    assert events[0][1]["mode"] == "enforce"

    # context-manager form refuses the turn the same way
    with pytest.raises(GovernanceBindingError):
        with governed_agent_turn(FREELANCER):
            pytest.fail("turn body must not run unrestricted under enforce")


def test_fail_closed_under_enforce_when_translation_breaks(enforce_policy, monkeypatch):
    # context_from_env_payload returning None would silently bind nothing
    # (governance_inactive = unrestricted); it must refuse instead.
    broken = SimpleNamespace(
        serialize_context_for_env=lambda ctx: "{}",
        context_from_env_payload=lambda payload: None,
        bind_governance_context=lambda ctx: pytest.fail("must not bind a None context"),
        reset_governance_context=lambda token: None,
    )
    monkeypatch.setattr(agent_context, "_agent_governance_module", lambda: broken)
    monkeypatch.setattr(agent_context, "append_audit_event", lambda event, **kw: None)

    with pytest.raises(GovernanceBindingError):
        bind_governed_agent_turn(FREELANCER)


def test_report_only_runs_unbound_but_audits_on_failure(inject_policy, monkeypatch):
    report_policy = dict(POLICY)
    report_policy["mode"] = "report_only"
    inject_policy(report_policy)

    def _broken():
        raise ImportError("hermes_cli unavailable")

    events = []
    monkeypatch.setattr(agent_context, "_agent_governance_module", _broken)
    monkeypatch.setattr(agent_context, "append_audit_event", lambda event, **kw: events.append((event, kw)))

    assert bind_governed_agent_turn(FREELANCER) is None  # unrestricted
    assert events and events[0][0] == "agent_governance_bind_failed"
    assert events[0][1]["report_only"] is True
    assert events[0][1]["mode"] == "report_only"

    with governed_agent_turn(FREELANCER):
        pass  # the turn still runs


# ── unbind-after-turn ────────────────────────────────────────────────────────

def test_unbind_after_turn_even_on_exception(enforce_policy):
    agent_mod = _real_agent_module()
    assert agent_mod.current_governance_context() is None

    with pytest.raises(RuntimeError, match="turn blew up"):
        with governed_agent_turn(FREELANCER):
            assert agent_mod.current_governance_context() is not None
            raise RuntimeError("turn blew up")
    assert agent_mod.current_governance_context() is None


def test_nested_turns_restore_outer_binding(enforce_policy):
    # reset-token semantics: nesting composes, the outer principal returns
    agent_mod = _real_agent_module()
    with governed_agent_turn(FREELANCER, session_id="outer"):
        outer = agent_mod.current_governance_context()
        with governed_agent_turn(FREELANCER, session_id="inner"):
            assert agent_mod.current_governance_context().session_id == "inner"
        assert agent_mod.current_governance_context() is outer
    assert agent_mod.current_governance_context() is None


def test_reset_is_none_safe():
    reset_governed_agent_turn(None)  # must never raise in a teardown finally


# ── webui / agent-side model field parity ────────────────────────────────────

def _agent_models_module():
    """The real agent-side models module when the hermes-agent checkout is on
    the box (the live deployment case); otherwise skip, same posture as
    _real_agent_module."""
    try:
        import api.config  # noqa: F401  (side effect: hermes-agent root on sys.path)
        from hermes_cli.dashboard_governance import models as agent_models
        return agent_models
    except Exception:
        pytest.skip("hermes_cli.dashboard_governance not importable in this environment")


def test_grantset_and_subject_field_parity_with_agent_side():
    """GrantSet and GovernanceSubject are vendored twice (webui and agent
    side) and bridged by duck-typing in _translate_context. The bridge is loud
    when a dimension is added AGENT-side (AttributeError on the shim), but a
    dimension added WEBUI-side only would be dropped silently for every
    governed turn; this parity check makes drift loud in both directions."""
    import dataclasses

    agent_models = _agent_models_module()
    from api.governance import models as webui_models

    for name in ("GrantSet", "GovernanceSubject"):
        webui_fields = {f.name for f in dataclasses.fields(getattr(webui_models, name))}
        agent_fields = {f.name for f in dataclasses.fields(getattr(agent_models, name))}
        assert webui_fields == agent_fields, (
            f"{name} field drift between webui and agent copies: "
            f"webui-only={sorted(webui_fields - agent_fields)}, "
            f"agent-only={sorted(agent_fields - webui_fields)}"
        )


def test_serialized_grant_payload_covers_every_grant_field():
    """The env payload is the only webui-to-agent grant path (only the agent
    side owns the serializer; the webui duck-types through it), so a GrantSet
    field missing from _serialize_grants would silently drop that grant
    dimension for every governed turn even with field-identical dataclasses."""
    import dataclasses

    agent_models = _agent_models_module()
    from hermes_cli.dashboard_governance import context as agent_ctx

    grant_fields = {f.name for f in dataclasses.fields(agent_models.GrantSet)}
    payload_keys = set(agent_ctx._serialize_grants(agent_models.GrantSet()).keys())
    assert payload_keys == grant_fields, (
        f"grant serializer drift: missing={sorted(grant_fields - payload_keys)}, "
        f"extra={sorted(payload_keys - grant_fields)}"
    )


class TestEnvVarGrantCrossesToTheAgent:
    """The webui resolves the grants and the agent enforces them, so a new
    dimension must exist on both sides or the bridge drops it silently. The
    env grant names the environment variables a person's shell may receive,
    which is what replaced reading the Hermes .env from the terminal."""

    def test_the_policy_parses_the_env_grant(self):
        from api.governance.models import GrantSet
        grants = GrantSet.from_mapping({"env": {"vars": ["OMNIROUTE_API_KEY", "NOTION_API_KEY"]}})
        assert grants.env_vars == frozenset({"OMNIROUTE_API_KEY", "NOTION_API_KEY"})

    def test_an_env_only_grant_is_not_treated_as_empty(self):
        from api.governance.models import GrantSet
        assert not GrantSet.from_mapping({"env": {"vars": ["A"]}}).is_empty()

    def test_roles_and_users_merge_their_env_grants(self):
        from api.governance.models import GrantSet
        role = GrantSet.from_mapping({"env": {"vars": ["A"]}})
        user = GrantSet.from_mapping({"env": {"vars": ["B"]}})
        assert role.merge(user).env_vars == frozenset({"A", "B"})

    def test_a_deny_takes_one_variable_back(self):
        from api.governance.models import GrantSet
        granted = GrantSet.from_mapping({"env": {"vars": ["A", "B"]}})
        assert granted.subtract(GrantSet.from_mapping({"env": {"vars": ["A"]}})).env_vars == frozenset({"B"})


def test_original_ask_reaches_real_denial_redacted(enforce_policy,tmp_path,monkeypatch):
    from hermes_cli.dashboard_governance.context import current_governance_context
    from hermes_cli.dashboard_governance import grant_requests
    monkeypatch.setenv('HERMES_WEBUI_STATE_DIR',str(tmp_path))
    monkeypatch.setenv('HERMES_SESSION_LAST_USER_MESSAGE','wrong person stale ask')
    original='Please read the project plan. token=secretvalue '+('details '*100)
    token=bind_governed_agent_turn(FREELANCER,session_id='session-test',request_id='request-test',user_message=original)
    try:
        ctx=current_governance_context()
        assert ctx.user_message_redacted.startswith('Please read the project plan.')
        assert len(ctx.user_message_redacted)<=400
        grant_requests.record_denial(ctx,'terminal','cli_command_not_allowed','example',tool_call_id='call-test',dispatch_session_id='session-test')
        entry=next(iter(grant_requests.load_store().values()))
        assert entry['trigger']==ctx.user_message_redacted
        assert 'secretvalue' not in entry['trigger'] and 'stale ask' not in entry['trigger']
        assert next(iter(entry['operations'].values()))['trigger_redacted']==entry['trigger']
    finally:reset_governed_agent_turn(token)
