"""Route catalog: maps hermes-webui API routes to governance permissions.

The RouteRule mechanism is vendored from the hermes-agent dashboard
route_catalog; the catalog CONTENT is rebuilt for the hermes-webui route
surface using ONLY permission names already granted in the canonical
~/.hermes/dashboard-governance.yaml, so the existing owner/admin/operator/
viewer roles work without any policy edit.
"""
from __future__ import annotations

from dataclasses import dataclass

_MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Pre-auth public login surface: these /api/* endpoints are reached BEFORE a
# session identity exists, so governance must let them through under enforce
# and even under a broken policy, otherwise mode=enforce (or a policy-load
# error) would 403 every login attempt and brick the bootstrap admin too.
# Kept in sync with the login endpoints in api.auth.PUBLIC_PATHS; a coverage
# test asserts the two stay aligned so a new public login route cannot silently
# become un-exempt. Non-login public paths (pages, static, manifests) are not
# listed here because they are already handled by the non-/api passthrough.
# /api/csp-report is listed as defense in depth: browsers POST CSP violation
# reports without credentials, and today the endpoint bypasses the hook via a
# server.py dispatch special-case; the entry here keeps it reachable even if
# that special-case ever moves.
_ANON_ROUTES: frozenset[str] = frozenset({
    "/api/auth/login",
    "/api/auth/status",
    "/api/auth/oidc/start",
    "/api/auth/oidc/callback",
    "/api/auth/passkey/options",
    "/api/auth/passkey/login",
    "/api/csp-report",
})

# Authenticated session self-management: no permission required beyond a
# valid identity. route_permission returns None for these; enforcement
# exempts them from the unknown_route fail-closed rule.
_SELF_ROUTES: frozenset[str] = frozenset({
    "/api/auth/status",
    "/api/auth/logout",
    "/api/auth/passkeys",
    "/api/auth/passkey/register/options",
    "/api/auth/passkey/register",
    "/api/auth/passkey/delete",
    "/api/governance/me",
    # The caller's own self-service install requests and their status. Read
    # only, scoped to the requesting identity by the handler, and deliberately
    # not admin-gated: a user must be able to see whether what they asked for
    # is still pending. The admin queue (/api/governance/approvals) stays
    # governance:write.
    "/api/governance/approvals/mine",
    # Owner-scoped consent/cancellation only; cannot grant permissions.
    "/api/governance/approvals/resume",
    # Asking for something is not the same as getting it. These three write
    # nothing but a pending row in the approvals registry (or, for an
    # already-approved item, install what an admin already decided), so they
    # deliberately do not require mcp:write / config:write: that is exactly
    # the permission a non-admin does not have and should not need in order
    # to ASK. The install itself stays gated on an admin decision, and the
    # role's route allowlist still applies (a role without /api/mcp/* or
    # /api/integrations/* in routes cannot reach these at all).
    "/api/crons/notifications",
    "/api/client-errors",
    "/api/apps",
    "/api/bot-screen", "/api/bot-screen/ws", "/api/bot-screen/status",
    "/api/msp/invoice", "/api/msp/order", "/api/msp/job-cost", "/api/msp/package", "/api/msp/overview", "/api/msp/tickets", "/api/msp/ticket", "/api/msp/comment",
    "/api/msp/app-state", "/api/msp/app-request", "/api/msp/app-intake", "/api/msp/app-support",
    # Team structure, organisation chart and Management (8 Nov 2026): every
    # handler decides from org_structure.request_scope itself.
    "/api/org/structure", "/api/org/structure/me", "/api/org/chart",
    "/api/management/overview", "/api/management/job", "/api/management/pricing",
    "/api/value/job", "/api/value/chat", "/api/value/collect",
    "/api/studio/usecases", "/api/studio/usecase", "/api/studio/step", "/api/studio/delete",
    "/api/studio/backlog", "/api/studio/backlog/order", "/api/studio/autopilot", "/api/studio/playbooks",
    "/api/studio/playbook", "/api/studio/playbook/delete", "/api/studio/playbook/draft", "/api/studio/playbook/run", "/api/studio/media",
    "/api/studio/prompts", "/api/studio/profile",
    "/api/org/modules",
    "/api/org/modules/request",
    "/api/org/overview",
    "/api/org/departments",
    "/api/org/people",
    "/api/org/approvals",
    "/api/org/usage",
    "/api/help/events",
    "/api/mcp/servers/request",
    "/api/mcp/requests",
    "/api/integrations/request",
})


@dataclass(frozen=True)
class RouteRule:
    pattern: str
    read_permission: str | None
    write_permission: str | None = None
    match: str = "prefix"

    def matches(self, path: str) -> bool:
        if self.match == "exact":
            return path == self.pattern
        return path == self.pattern or path.startswith(self.pattern.rstrip("/") + "/")

    def permission_for(self, method: str) -> str | None:
        if method.upper() in _MUTATION_METHODS:
            return self.write_permission or self.read_permission
        return self.read_permission


# Ordered most-specific first. Unknown /api/* routes deliberately return None;
# enforcement treats that as unknown_route so new endpoints fail closed until
# classified here (tests/test_governance_catalog_coverage.py is the net).
ROUTE_CATALOG: tuple[RouteRule, ...] = (
    RouteRule("/api/share/create", "sessions:write", "sessions:write", match="exact"),
    RouteRule("/api/share/revoke", "sessions:write", "sessions:write", match="exact"),
    # governance admin (mirror reference)
    RouteRule("/api/governance/policy",   "governance:read",  "governance:write"),
    RouteRule("/api/governance/validate", "governance:read",  "governance:read"),
    RouteRule("/api/governance/preview",  "governance:preview", "governance:preview"),
    RouteRule("/api/governance/audit",    "governance:audit:read"),
    RouteRule("/api/governance/usage",    "governance:usage:read"),
    RouteRule("/api/governance/users",    "governance:read",  "governance:write"),
    RouteRule("/api/governance/groups",   "governance:read",  "governance:write"),
    RouteRule("/api/governance",          "governance:read",  "governance:write"),

    # profiles
    RouteRule("/api/bots/knowledge", "profiles:admin", "profiles:admin", match="exact"),
    # Scout per person: one's own Scout for a bot is chat use; the handler
    # asks profiles:admin for anyone else's entry and the bot's own settings.
    RouteRule("/api/bots/scout", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/bots/builder", "profiles:admin", "profiles:admin", match="exact"),
    RouteRule("/api/profiles",            "profiles:read"),
    RouteRule("/api/profile/active",      "profiles:read", match="exact"),
    # Selecting a permitted bot is chat use, not profile administration.
    RouteRule("/api/profile/switch",      "profiles:read", "chat:use", match="exact"),
    RouteRule("/api/profile",             "profiles:read", "profiles:admin"),

    # sessions, projects, background
    RouteRule("/api/session/skill-updates", "chat:use", "skills:write", match="exact"),
    RouteRule("/api/session/yolo",        "sessions:read", "sessions:write", match="exact"),
    RouteRule("/api/memdash/banks", "chat:use", match="exact"),
    RouteRule("/api/memdash/v1/view", "chat:use", match="exact"),
    RouteRule("/api/memdash/v1/action", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/memdash/access-log", "chat:use", match="exact"),
    RouteRule("/api/memdash/access-log/seen", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/memdash/notice", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/agui/runs", "sessions:read"),
    RouteRule("/api/session",             "sessions:read", "sessions:write"),
    RouteRule("/api/sessions",            "sessions:read", "sessions:write"),
    RouteRule("/api/projects",            "sessions:read", "sessions:write"),
    # The colleague directory behind the group-chat picker: names and addresses
    # only, and only for people who may already start a conversation.
    RouteRule("/api/people",              "sessions:read", match="exact"),
    # Profile photos: anyone who can see the directory may load a photo; the
    # POST only ever targets the caller's own identity (api/user_avatars.py).
    RouteRule("/api/people/avatar",       "sessions:read", match="exact"),
    RouteRule("/api/me/sidebar-organization", "sessions:read", "sessions:read", match="exact"),
    RouteRule("/api/me/avatar",           "sessions:read", "sessions:read", match="exact"),
    # Own bot and own platform ids (Connections, "Your bot"): always the caller.
    RouteRule("/api/me/channels",         "sessions:read", "sessions:read"),
    RouteRule("/api/background",          "sessions:read", "sessions:write"),
    RouteRule("/api/bg-task-complete-ack", "sessions:write", "sessions:write", match="exact"),
    RouteRule("/api/process-complete-ack", "sessions:write", "sessions:write", match="exact"),

    # chat execution (incl. SSE streams, approval/clarify, voice)
    RouteRule("/api/chat",                "chat:use", "chat:use"),
    # Map places in chat answers (interactive answers): anyone who may chat.
    RouteRule("/api/map/resolve",         "chat:use", "chat:use", match="exact"),
    RouteRule("/api/btw",                 "chat:use", "chat:use", match="exact"),
    RouteRule("/api/goal",                "chat:use", "chat:use", match="exact"),
    RouteRule("/api/approval",            "chat:use", "chat:use"),
    RouteRule("/api/clarify",             "chat:use", "chat:use"),
    # Bounded personal clarification preferences; no governance decisions.
    RouteRule("/api/interaction/preferences", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/voice/realtime/",      "chat:use", "chat:use"),
    RouteRule("/api/transcribe/capability", "model:read", match="exact"),
    RouteRule("/api/transcribe",          "chat:use", "chat:use", match="exact"),
    RouteRule("/api/tts",                 "chat:use", "chat:use", match="exact"),

    # terminal + commands (RCE-grade; deliberately split off chat:use so chat
    # access no longer implies shell access; grant terminal:use explicitly)
    RouteRule("/api/terminal",            "terminal:use", "terminal:use"),
    RouteRule("/api/commands/exec",       "terminal:use", "terminal:use", match="exact"),
    RouteRule("/api/commands",            "config:read", "config:read"),

    # files and workspace
    # Artifact handlers additionally enforce the authenticated artifact owner.
    RouteRule("/api/artifacts",           "files:read", "files:write"),
    RouteRule("/api/escape",              "files:read", "files:write"),
    RouteRule("/api/list",                "files:read", match="exact"),
    RouteRule("/api/file",                "files:read", "files:write"),
    RouteRule("/api/media",               "files:read", match="exact"),
    RouteRule("/api/folder/download",     "files:read", match="exact"),
    RouteRule("/api/upload",              "files:write", "files:write"),
    RouteRule("/api/workspace/upload",    "files:write", "files:write", match="exact"),
    RouteRule("/api/workspaces",          "files:read", "files:write"),
    RouteRule("/api/rollback",            "files:read", "files:write"),
    RouteRule("/api/wiki",                "files:read"),
    RouteRule("/api/notes",               "files:read"),

    # git
    RouteRule("/api/git-info",            "git:read", match="exact"),
    RouteRule("/api/git",                 "git:read", "git:write"),

    # config, settings, models, providers
    # POST /api/settings deliberately rides on config:read: appearance-only
    # payloads (theme/skin/font_size) are self-service for anyone who can open
    # the settings panel, and this hook never reads the body. The handler's
    # body-sink guard (api/settings_scope.py) re-imposes config:write for every
    # non-cosmetic key, so full settings writes stay gated exactly as before.
    RouteRule("/api/settings",            "config:read", "config:read", match="exact"),
    RouteRule("/api/reasoning",           "config:read", "config:write", match="exact"),
    RouteRule("/api/models",              "model:read", "model:read"),
    RouteRule("/api/model",               "model:read", "model:write"),
    RouteRule("/api/default-model",       "model:write", "model:write", match="exact"),
    RouteRule("/api/providers",           "config:read", "config:write"),
    # Self-service integrations get their OWN permission pair rather than
    # riding on config:read/config:write. Connecting your own Gmail account
    # must not require the permission that also rewrites dashboard settings,
    # and the approval gate (governance:write) is what stops a user enabling a
    # provider for everyone.
    RouteRule("/api/integrations",        "integrations:read", "integrations:connect"),
    RouteRule("/api/provider",            "analytics:read"),
    RouteRule("/api/personalities",       "config:read", match="exact"),
    RouteRule("/api/personality",         "config:write", "config:write"),
    RouteRule("/api/prompts",             "config:read", "config:write", match="exact"),
    # Personal context belongs to the authenticated person, independent of the
    # selected bot. Shared company memory retains its handler-level admin gate.
    RouteRule("/api/memory/write",        "chat:use", "chat:use", match="exact"),
    RouteRule("/api/memory",              "chat:use", match="exact"),
    RouteRule("/api/admin",               "config:write", "config:write"),
    RouteRule("/api/dashboard",           "dashboard:read", "dashboard:write"),
    RouteRule("/api/insights",            "analytics:read"),
    # Upstream capacity alerts and their thresholds: administrator surface.
    # Read is config:write on purpose (not config:read): the alert rows carry
    # provider diagnostics, which is exactly what normal users must not see.
    RouteRule("/api/capacity",            "config:write", "config:write"),
    RouteRule("/api/project-os",          "analytics:read"),
    RouteRule("/api/logs",                "logs:read", match="exact"),
    RouteRule("/api/client-events/log",   "status:read", "status:read", match="exact"),

    # skills, mcp, plugins, extensions
    # The read-only activity handler returns this actor's counts for one
    # currently accessible conversation, never the skill catalogue or content.
    RouteRule("/api/skills/learning-activity", "chat:use", "skills:write", match="exact"),
    RouteRule("/api/skills",              "skills:read", "skills:write"),
    RouteRule("/api/mcp",                 "mcp:read", "mcp:write"),
    RouteRule("/api/plugins",             "plugins:read", "plugins:write"),
    RouteRule("/api/vault",               "integrations:read", "integrations:connect"),
    RouteRule("/api/extensions",          "plugins:read", "plugins:write"),  # incl. sidecar proxy wildcard

    # cron (route_permission special-cases POST .../run -> cron:run, as reference)
    RouteRule("/api/crons",               "cron:read", "cron:write"),

    # gateway
    RouteRule("/api/gateway/status",      "gateway:read", match="exact"),
    RouteRule("/api/gateway",             "gateway:read", "gateway:restart"),

    # kanban bridge (agent dispatch is chat-run grade)
    RouteRule("/api/kanban/dispatch",     "chat:use", "chat:use", match="exact"),
    RouteRule("/api/kanban",              "sessions:read", "sessions:write"),

    # system, health, updates, onboarding
    RouteRule("/api/health/restart",      "system:ops", "system:ops", match="exact"),
    RouteRule("/api/health",              "status:read"),
    RouteRule("/api/system",              "system:read"),
    RouteRule("/api/shutdown",            "system:ops", "system:ops", match="exact"),
    RouteRule("/api/updates/check",       "system:read", "system:read", match="exact"),
    RouteRule("/api/updates/summary",     "system:read", "system:read", match="exact"),
    RouteRule("/api/updates",             "system:read", "system:ops"),
    RouteRule("/api/onboarding",          "config:read", "config:write"),
)


def route_permission(path: str, method: str) -> str | None:
    if path in _SELF_ROUTES:
        return None
    for rule in ROUTE_CATALOG:
        if rule.matches(path):
            if method.upper() == "POST" and path.startswith("/api/crons") and path.rstrip("/").endswith("/run"):
                return "cron:run"
            return rule.permission_for(method)
    return None
