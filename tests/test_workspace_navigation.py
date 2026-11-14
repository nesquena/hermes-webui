"""Role presets select a workspace, never confer authorization."""
from dataclasses import replace

from api.governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from api.governance.nav import hidden_panels, workspace_navigation


def caller(roles=(), permissions=(), **kwargs):
    grants = GrantSet(permissions=frozenset(permissions))
    return EffectiveAccess(subject=GovernanceSubject(email="person@example.test"),
                           mode="enforce", roles=frozenset(roles),
                           permissions=grants.permissions, grants=grants, **kwargs)


def test_member_blacklist_still_starts_with_daily_work():
    access = caller(permissions=("*",), access_mode="blacklist", access_level="user")
    view = workspace_navigation(access)
    assert view["kind"] == "member"
    assert "insights" not in view["primary"]
    # The existing permitted functions remain reachable under More.
    assert "insights" in view["secondary"]
    assert not set(view["primary"]) & set(view["secondary"])


def test_engineer_role_reveals_only_permitted_technical_views():
    access = caller(roles=("engineer",), permissions=("files:read", "analytics:read"))
    view = workspace_navigation(access)
    assert view["kind"] == "engineer"
    assert {"workspaces", "insights"} <= set(view["primary"])
    assert "governance" in hidden_panels(access)
    assert "logs" in hidden_panels(access)
    assert not set(view["primary"]) & set(hidden_panels(access))


def test_operator_and_unknown_role_have_distinct_defaults():
    operator = caller(roles=("operator",), permissions=("analytics:read", "files:read"))
    assert "insights" in workspace_navigation(operator)["primary"]
    assert "workspaces" not in workspace_navigation(operator)["primary"]
    assert workspace_navigation(replace(operator, roles=frozenset({"custom"})))["kind"] == "member"


def test_denials_override_role_and_elevated_presentation():
    access = caller(roles=("engineer",), permissions=("*",), access_mode="blacklist",
                    access_level="elevated", deny=GrantSet(permissions=frozenset({"files:*", "analytics:*"})))
    view = workspace_navigation(access)
    assert view["kind"] == "engineer"
    assert not {"files", "workspaces", "insights"} & set(view["primary"] + view["secondary"])


def test_admin_keeps_all_permitted_views_and_no_secondary_menu():
    view = workspace_navigation(caller(permissions=("*",)))
    assert view["kind"] == "admin"
    assert "governance" in view["primary"]
    assert view["secondary"] == []

def test_technical_roles_use_the_session_permission_for_kanban():
    for role in ("engineer", "operator"):
        access = caller(roles=(role,), permissions=("sessions:read",))
        assert "kanban" in workspace_navigation(access)["primary"]
        assert "kanban" in hidden_panels(replace(access, permissions=frozenset(), grants=GrantSet()))
