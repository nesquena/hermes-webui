"""Route catalog tests: endpoint family mapping, self routes, segment
boundaries and the unknown-route fail-closed contract.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.governance.catalog import _SELF_ROUTES, route_permission  # noqa: E402


def test_route_catalog_maps_core_endpoint_families():
    cases = [
        ("/api/session", "GET", "sessions:read"),
        ("/api/session/abc123", "GET", "sessions:read"),
        ("/api/session/delete", "POST", "sessions:write"),
        ("/api/sessions", "GET", "sessions:read"),
        ("/api/session/yolo", "POST", "sessions:write"),
        ("/api/chat/start", "POST", "chat:use"),
        ("/api/chat/stream", "GET", "chat:use"),
        ("/api/terminal/start", "POST", "terminal:use"),
        ("/api/terminal/output", "GET", "terminal:use"),
        ("/api/commands/exec", "POST", "terminal:use"),
        ("/api/commands", "GET", "config:read"),
        ("/api/file", "GET", "files:read"),
        ("/api/file/save", "POST", "files:write"),
        ("/api/list", "GET", "files:read"),
        ("/api/upload", "POST", "files:write"),
        ("/api/git-info", "GET", "git:read"),
        ("/api/git/commit", "POST", "git:write"),
        ("/api/git/log", "GET", "git:read"),
        ("/api/settings", "GET", "config:read"),
        # POST /api/settings is config:read at the route level so appearance
        # self-service works; api/settings_scope.py re-imposes config:write
        # for non-cosmetic keys in the handler (26 Aug 2026 report).
        ("/api/settings", "POST", "config:read"),
        ("/api/models", "GET", "model:read"),
        ("/api/model", "POST", "model:write"),
        ("/api/default-model", "POST", "model:write"),
        ("/api/profiles", "GET", "profiles:read"),
        ("/api/profile/active", "GET", "profiles:read"),
        ("/api/profile/create", "POST", "profiles:admin"),
        ("/api/skills", "GET", "skills:read"),
        ("/api/skills/install", "POST", "skills:write"),
        ("/api/mcp/servers", "GET", "mcp:read"),
        ("/api/mcp/reload", "POST", "mcp:write"),
        ("/api/plugins", "GET", "plugins:read"),
        ("/api/extensions", "GET", "plugins:read"),
        ("/api/extensions/x/sidecar/y", "POST", "plugins:write"),
        ("/api/crons", "GET", "cron:read"),
        ("/api/crons/create", "POST", "cron:write"),
        ("/api/crons/abc/run", "POST", "cron:run"),
        ("/api/crons/run", "POST", "cron:run"),
        ("/api/crons/run", "GET", "cron:read"),
        ("/api/gateway/status", "GET", "gateway:read"),
        ("/api/gateway/restart", "POST", "gateway:restart"),
        ("/api/kanban/dispatch", "POST", "chat:use"),
        ("/api/kanban/board", "GET", "sessions:read"),
        ("/api/logs", "GET", "logs:read"),
        ("/api/insights/summary", "GET", "analytics:read"),
        ("/api/memory", "GET", "chat:use"),
        ("/api/memory/write", "POST", "chat:use"),
        ("/api/artifacts/example/download", "GET", "files:read"),
        ("/api/artifacts/example/share", "POST", "files:write"),
        ("/api/health", "GET", "status:read"),
        ("/api/health/restart", "POST", "system:ops"),
        ("/api/system/info", "GET", "system:read"),
        ("/api/shutdown", "POST", "system:ops"),
        ("/api/updates/check", "GET", "system:read"),
        ("/api/updates/apply", "POST", "system:ops"),
        ("/api/onboarding/complete", "POST", "config:write"),
        ("/api/governance/policy", "GET", "governance:read"),
        ("/api/governance/policy", "POST", "governance:write"),
        ("/api/governance/preview", "POST", "governance:preview"),
        ("/api/governance/audit", "GET", "governance:audit:read"),
        ("/api/governance/usage", "GET", "governance:usage:read"),
        ("/api/governance/users/delete", "POST", "governance:write"),
        ("/api/transcribe", "POST", "chat:use"),
        ("/api/transcribe/capability", "GET", "model:read"),
        ("/api/tts", "POST", "chat:use"),
    ]

    for path, method, expected in cases:
        assert route_permission(path, method) == expected, (path, method)


def test_mutation_methods_resolve_write_permission():
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert route_permission("/api/file/save", method) == "files:write", method
    assert route_permission("/api/file", "GET") == "files:read"
    assert route_permission("/api/file", "HEAD") == "files:read"


def test_self_routes_do_not_need_permissions():
    for path in sorted(_SELF_ROUTES):
        assert route_permission(path, "GET") is None, path
        assert route_permission(path, "POST") is None, path
    assert "/api/governance/me" in _SELF_ROUTES
    assert "/api/auth/status" in _SELF_ROUTES


def test_prefixes_require_segment_boundary():
    assert route_permission("/api/gitfoo", "GET") is None
    assert route_permission("/api/sessionx", "GET") is None
    assert route_permission("/api/governancex", "GET") is None
    assert route_permission("/api/modelling", "GET") is None
    assert route_permission("/api/filet", "GET") is None


def test_exact_rules_do_not_match_subpaths():
    # /api/logs is exact: a subpath is unknown, not logs:read
    assert route_permission("/api/logs/tail", "GET") is None
    assert route_permission("/api/memory/other", "GET") is None


def test_unknown_api_route_fails_closed():
    assert route_permission("/api/new-unclassified-route", "GET") is None
    assert route_permission("/api/new-unclassified-route", "POST") is None
