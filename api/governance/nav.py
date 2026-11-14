"""Role-aware presentation derived from effective administrative and feature grants.

Members get daily work destinations. Administrators retain the full console.
Backend routes remain the authorization boundary; this module only chooses
navigation. See docs/role-navigation.md for the intentional presentation contract.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# panel id (data-panel in static/index.html) -> permission its API requires.
PANEL_PERMISSIONS = {
    "approvals": "self:read",
    "tasks": "cron:read",
    "kanban": "kanban:read",
    # Projects hub (ticket 12): every aggregated section inside the payload
    # is re-gated by the permission its own route requires, so the panel
    # itself deliberately carries the widest of the set, which is also what
    # /api/projects and the hub route already need.
    "projects": "sessions:read",
    "skills": "skills:read",
    "memory": "chat:use",
    "workspaces": "files:read",
    "files": "files:read",
    "profiles": "profiles:read",
    "todos": "todos:read",
    "insights": "analytics:read",
    "logs": "logs:read",
    "governance": "governance:read",
    "integrations": "integrations:read",
}

# NOT listed above: "dashboard" is an external link out of the app, not a
# panel, so hiding it here would silently do nothing.

# Never hidden, whatever the grants: the user would otherwise lose the ability
# to work, to reach their own preferences, or to ask for help.
ESSENTIAL_PANELS = frozenset({"chat", "settings", "approvals"})
MEMBER_PANELS = frozenset({"modules", "chat", "profiles", "projects", "tasks", "skills", "integrations", "approvals", "memory", "settings"})
WORKSPACE_PANELS = {
    "member": MEMBER_PANELS,
    "operator": MEMBER_PANELS | {"kanban", "insights"},
    "engineer": MEMBER_PANELS | {"kanban", "insights", "workspaces", "files", "todos", "logs"},
}


def _permissions(access) -> frozenset:
    """The caller's effective permissions, or None when unreadable.

    None is distinct from the empty set on purpose: an empty set is a real
    narrow user (hide everything they cannot use), while an unreadable access
    object is our failure and must not lock a person out of their own
    navigation. Hiding is presentation; the APIs stay the real gate.
    """
    try:
        effective = getattr(access, "permissions", None)
        if effective is not None:
            return frozenset(effective)
        return frozenset(getattr(getattr(access, "grants", None), "permissions", None) or ())
    except Exception:
        logger.debug("nav permission read failed", exc_info=True)
        return None


def _has(permissions: frozenset, permission: str) -> bool:
    if "*" in permissions or permission in permissions:
        return True
    # A "<area>:admin" grant implies the area's read.
    return f"{permission.split(':', 1)[0]}:admin" in permissions


def administrative_access(access) -> bool:
    """Administrative identity is independent of route allowlists and mode.

    Explicit owner/admin roles retain the ownership policy's existing contract.
    A wildcard route only allows addressing endpoints; it grants no privilege.
    """
    permissions = _permissions(access) or frozenset()
    roles = set(getattr(access, "roles", ()) or ())
    sources = set(getattr(access, "grant_sources", ()) or ())
    subject = getattr(access, "subject", None)
    if getattr(access, "access_level", "") or getattr(access, "access_mode", ""):
        return ("bootstrap_admin" in sources or
                (access.access_level == "admin" and access.has_permission("governance:write")))
    return (getattr(subject, "provider", "") == "auth_disabled"
            or "bootstrap_admin" in sources
            or bool(roles.intersection({"owner", "admin"}))
            or bool(permissions.intersection({"*", "governance:write", "governance:admin"})))


def navigation_audience(access, policy=None) -> str:
    """Use the same administrative identity as ownership, in every policy mode."""
    return "admin" if administrative_access(access) else "member"


def workspace_kind(access) -> str:
    """Presentation derived from resolved identity, not the allow/deny mode.

    Keep nav_audience's administrator contract separate: an engineer is still
    a member for ownership, approvals, and all backend authorization.
    """
    if administrative_access(access):
        return "admin"
    roles = set(getattr(access, "roles", ()) or ())
    if "engineer" in roles or getattr(access, "access_level", "") == "elevated":
        return "engineer"
    if "operator" in roles:
        return "operator"
    return "member"


def workspace_navigation(access, policy=None) -> dict:
    """Split existing permitted destinations into primary and additional views.

    Module/organisation destinations retain their independent capability gate.
    This projection neither grants access nor changes the canonical policy.
    """
    kind = workspace_kind(access)
    visible = set(visible_panels(access, policy))
    primary = visible if kind == "admin" else visible & WORKSPACE_PANELS[kind]
    return {"kind": kind, "primary": sorted(primary),
            "secondary": sorted(visible - primary)}


def feature_permissions(access) -> dict[str, bool]:
    """Concrete UI decisions use the same deny/ceiling checks as HTTP routes."""
    from .catalog import ROUTE_CATALOG

    known = set(PANEL_PERMISSIONS.values()) | {"profiles:admin", "gateway:read"}
    known.update(permission for rule in ROUTE_CATALOG
                 for permission in (rule.read_permission, rule.write_permission) if permission)
    check = getattr(access, "has_permission", None)
    permissions = _permissions(access) or frozenset()
    return {permission: bool(check(permission) if callable(check) else _has(permissions, permission))
            for permission in sorted(known)}


def hidden_panels(access, policy=None) -> list:
    """Daily work navigation for members; complete navigation for administrators.

    Feature grants still hide inaccessible daily panels. Own approvals and
    preferences always remain reachable. This is presentation only: every
    backend endpoint continues to authorize the request independently.
    Unreadable policy state fails to the member surface with no feature grants.
    """
    capability_navigation = getattr(access, "access_mode", "") == "blacklist"
    if not capability_navigation and navigation_audience(access, policy) == "admin":
        return []
    permissions = _permissions(access) or frozenset()
    panel_permissions = dict(PANEL_PERMISSIONS)
    roles = set(getattr(access, "roles", ()) or ())
    daily_panels = (WORKSPACE_PANELS["engineer"] if "engineer" in roles else
                    WORKSPACE_PANELS["operator"] if "operator" in roles else MEMBER_PANELS)
    if capability_navigation or roles.intersection({"engineer", "operator"}):
        # Kanban routes and the session-backed todo state both use session read.
        # Keep the legacy daily-navigation presentation contract unchanged.
        panel_permissions.update(kanban="sessions:read", todos="sessions:read")
    return sorted(panel for panel, permission in panel_permissions.items()
                  if panel not in ESSENTIAL_PANELS
                  and ((not capability_navigation and panel not in daily_panels) or not
                       (access.has_permission(permission) if callable(getattr(access, "has_permission", None))
                        else _has(permissions, permission))))


def visible_panels(access, policy=None) -> list:
    """The complement of hidden_panels, for a preview of a group's navigation."""
    hidden = set(hidden_panels(access, policy))
    return sorted(
        set(p for p in PANEL_PERMISSIONS if p not in hidden) | ESSENTIAL_PANELS
    )
